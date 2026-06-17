from __future__ import annotations

import argparse
import json
import math
import random
import time
from pathlib import Path
from typing import Any

from common import (
    OUTPUT_ROOT,
    REPO_ROOT,
    TIMELINE_DIR,
    affected_channels,
    ber_from_snr,
    channel_centers_hz,
    clamp,
    ensure_output_dirs,
    iter_scenarios,
    load_config,
    per_from_ber,
)
from decision_policy import EmergencyDecisionPolicy, estimate_quality_penalty


def generate_scenario_timeline(
    scenario: dict[str, Any],
    global_cfg: dict[str, Any],
    strategy: str = "智能决策策略",
    seed_offset: int = 0,
) -> list[dict[str, Any]]:
    rng = random.Random(int(scenario["seed"]) + seed_offset)
    frames = int(scenario.get("frames", global_cfg["default_frames"]))
    interval = float(global_cfg["frame_interval_s"])
    num_channels = int(global_cfg["num_channels"])
    centers = channel_centers_hz(global_cfg)
    policy = EmergencyDecisionPolicy(scenario, global_cfg, strategy=strategy)

    timeline: list[dict[str, Any]] = []
    task_delivered_kbits = 0.0
    jam = dict(scenario["jammer"])
    base_action = {
        "channel_idx": int(jam["channel_idx"]),
        "waveform_mode": int(jam["waveform_mode"]),
        "power_db": float(jam["power_db"]),
        "bw_mhz": int(jam["bw_mhz"]),
    }

    for frame in range(frames):
        jam_active = int(jam["start_frame"]) <= frame <= int(jam["stop_frame"])
        action = dict(base_action)
        if not jam_active:
            action["power_db"] = 0.0

        affected = affected_channels(action["channel_idx"], action["bw_mhz"], global_cfg) if jam_active else []
        channel_quality = _channel_quality(num_channels, affected, action, rng)

        # First estimate with previous decision state, then update policy and recalc.
        prev_state = policy.state
        raw_penalty = estimate_quality_penalty(prev_state.comm_channel_idx, action, global_cfg) if jam_active else 0.0
        pre_snr = _snr_from_state(scenario, prev_state.snapshot(), raw_penalty, rng)
        decision_state = policy.update(frame, pre_snr, channel_quality, jam_active).snapshot()
        penalty = estimate_quality_penalty(decision_state["comm_channel_idx"], action, global_cfg) if jam_active else 0.0
        snr_db = _snr_from_state(scenario, decision_state, penalty, rng)
        sinr_db = snr_db - (1.5 if jam_active and penalty > 0 else 0.0)
        mode_name = _mode_key(decision_state["anti_jamming_mode"], decision_state["anti_jamming_mode_name"], decision_state)
        ber = ber_from_snr(snr_db, mode_name)
        ber *= _ber_multiplier(decision_state)
        ber = clamp(ber, 1e-7, 0.48)
        per = per_from_ber(ber)
        throughput = _throughput_kbps(
            scenario,
            decision_state,
            per,
            jam_active,
            snr_db=snr_db,
            frame=frame,
            rng=rng,
            penalty=penalty,
        )
        payload_kbits = _active_payload_kbits(scenario, decision_state)
        task_delivered_kbits = min(payload_kbits, task_delivered_kbits + throughput * interval)
        task_progress = min(1.0, task_delivered_kbits / payload_kbits)

        spectrum, freq_axis = _spectrum(global_cfg, affected, action, decision_state, rng, frame=frame)
        waveform = _jam_waveform(global_cfg, action, rng)
        channel_map, features = _channel_map_and_features(
            num_channels, affected, decision_state["comm_channel_idx"], channel_quality, snr_db
        )

        record = {
            "scenario_id": scenario["id"],
            "scenario_name": scenario["name"],
            "short_name": scenario["short_name"],
            "strategy": strategy,
            "frame": frame,
            "time_s": round(frame * interval, 3),
            "jam_active": jam_active,
            "action": action,
            "affected_channels": affected,
            "decision": decision_state,
            "metrics": {
                "snr_db": round(snr_db, 3),
                "sinr_db": round(sinr_db, 3),
                "jsr_db": round(max(0.0, penalty), 3),
                "ber": ber,
                "per": per,
                "throughput_kbps": round(throughput, 3),
                "task_progress": round(task_progress, 4),
                "task_completed": task_progress >= 1.0,
                "overlap": bool(jam_active and penalty > 0),
                "available_channel_ratio": round(sum(1 for q in channel_quality if q > 0.55) / num_channels, 3),
            },
            "signals": {
                "spectrum": spectrum,
                "freq_axis_ghz": freq_axis,
                "jam_waveform_abs": waveform,
                "channel_map": channel_map,
                "channel_features": features,
            },
            "ui_payloads": {},
        }
        record["ui_payloads"] = build_ui_payloads(record, scenario, global_cfg, centers)
        timeline.append(record)

    return timeline


def build_ui_payloads(
    record: dict[str, Any],
    scenario: dict[str, Any],
    global_cfg: dict[str, Any],
    centers_hz: list[float],
) -> dict[str, Any]:
    frame = int(record["frame"])
    action = record["action"]
    decision = record["decision"]
    metrics = record["metrics"]
    signals = record["signals"]
    timestamp_ms = int(time.time() * 1000) + frame * int(float(global_cfg["frame_interval_s"]) * 1000)

    jammer_pkt = {
        "type": "telemetry",
        "version": "2.0",
        "session_id": f"sim_{record['scenario_id']}",
        "seq": frame + 1,
        "timestamp_ms": timestamp_ms,
        "telemetry": {
            "snr_est": metrics["snr_db"],
            "bw_est": float(action["bw_mhz"]) * 1e6,
            "sig_type": 1,
            "tx_state": "sim_running",
            "spectrum": signals["spectrum"],
            "freq_axis_ghz": signals["freq_axis_ghz"],
            "jam_waveform_abs": signals["jam_waveform_abs"],
            "channel_map": signals["channel_map"],
            "channel_features": signals["channel_features"],
        },
        "action": action,
        "rl_meta": {
            "policy": decision["strategy"],
            "value": round(1.0 - metrics["per"], 4),
            "latency_ms": 80 if decision["first_response_frame"] is not None else 0,
        },
    }

    tx_payload = _tx_ui_payload(record, scenario)
    rx_payload = _rx_ui_payload(record, scenario, centers_hz)
    return {"jammer": jammer_pkt, "tx": tx_payload, "rx": rx_payload}


def write_timeline(timeline: list[dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for rec in timeline:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_timeline(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _channel_quality(num_channels: int, affected: list[int], action: dict[str, Any], rng: random.Random) -> list[float]:
    qualities = []
    for ch in range(num_channels):
        base = 0.72 + 0.18 * math.sin(ch * 0.9) + rng.uniform(-0.08, 0.08)
        if ch in affected:
            base -= 0.22 + 0.025 * float(action["power_db"])
            if int(action["waveform_mode"]) == 1:
                base -= 0.08 * (1 if ch == int(action["channel_idx"]) else 0.5)
        qualities.append(clamp(base, 0.02, 0.98))
    return qualities


def _snr_from_state(
    scenario: dict[str, Any],
    decision: dict[str, Any],
    penalty: float,
    rng: random.Random,
) -> float:
    base = float(scenario["link"]["base_snr_db"])
    if decision.get("action_profile") == "无干扰16QAM高速传输":
        return clamp(base + 1.2 + rng.gauss(0.0, 0.55), -3.0, 32.0)
    mode_gain = {0: 0.0, 1: 1.4, 2: 0.8}.get(int(decision["anti_jamming_mode"]), 0.0)
    if "增益" in str(decision["anti_jamming_mode_name"]):
        mode_gain += 0.6
    gain_bonus = max(0.0, (float(decision["power_gain_db"]) - 14.0) * 0.12)
    redundancy_bonus = (float(decision["redundancy"]) - 1.0) * 0.55
    modulation_bonus = {
        "BPSK": 0.8,
        "DSSS-BPSK": 0.9,
        "CSS": 1.0,
        "QPSK": 0.0,
        "OFDM-QPSK": 0.2,
        "FHSS-QPSK": 0.3,
        "8PSK": -0.6,
        "16QAM": -1.4,
    }.get(str(decision.get("modulation", "QPSK")), 0.0)
    coding_bonus = max(0.0, (0.75 - float(decision.get("coding_rate", 0.75))) * 1.6)
    spreading_bonus = max(0.0, float(decision.get("spreading_factor", 1.0)) - 1.0) * 0.45
    interleaving_bonus = max(0.0, float(decision.get("interleaving_depth", 1)) - 1.0) * 0.08
    sync_bonus = max(0.0, 8.0 - float(decision.get("sync_threshold_db", 8.0))) * 0.08
    diversity_bonus = max(0.0, float(decision.get("route_diversity", 1)) - 1.0) * 0.25
    fading = rng.gauss(0.0, 0.75)
    raw = (
        base + mode_gain + gain_bonus + redundancy_bonus + modulation_bonus + coding_bonus
        + spreading_bonus + interleaving_bonus + sync_bonus + diversity_bonus - penalty + fading
    )
    clean_cap = base + 1.2 + min(0.6, gain_bonus)
    return clamp(min(raw, clean_cap), -3.0, 32.0)


def _legacy_throughput_kbps(
    scenario: dict[str, Any],
    decision: dict[str, Any],
    per: float,
    jam_active: bool,
) -> float:
    task_mode = int(decision["tx_task_mode"])
    base_by_task = {1: 165.0, 2: 360.0, 3: 300.0, 4: 54.0}
    base = base_by_task.get(task_mode, 150.0)
    mode_factor = {0: 1.0, 1: 0.66, 2: 0.88}.get(int(decision["anti_jamming_mode"]), 0.9)
    if "增益" in str(decision["anti_jamming_mode_name"]):
        mode_factor = 0.95
    modulation_factor = {
        "BPSK": 0.56,
        "DSSS-BPSK": 0.40,
        "CSS": 0.25,
        "QPSK": 1.0,
        "OFDM-QPSK": 1.05,
        "FHSS-QPSK": 0.86,
        "8PSK": 1.30,
        "16QAM": 1.62,
    }.get(str(decision.get("modulation", "QPSK")), 1.0)
    coding_factor = min(1.05, max(0.5, float(decision.get("coding_rate", 0.75)) / 0.75))
    spread_factor = 1.0 / max(1.0, float(decision.get("spreading_factor", 1.0)) ** 0.62)
    interleave_factor = 1.0 - min(0.16, max(0, int(decision.get("interleaving_depth", 1)) - 1) * 0.025)
    bandwidth_factor = min(1.0, max(0.45, float(decision.get("bandwidth_scale", 1.0))))
    diversity_factor = 0.86 if int(decision.get("route_diversity", 1)) > 1 else 1.0
    transition_factor = 0.28 if int(decision.get("transition_remaining", 0)) > 0 else 1.0
    congestion = 0.92 if jam_active and scenario["id"] == "rescue_congestion" else 1.0
    return max(
        0.0,
        base * mode_factor * modulation_factor * coding_factor * spread_factor
        * interleave_factor * bandwidth_factor * diversity_factor * transition_factor * congestion * (1.0 - per),
    )


def _throughput_kbps(
    scenario: dict[str, Any],
    decision: dict[str, Any],
    per: float,
    jam_active: bool,
    snr_db: float | None = None,
    frame: int = 0,
    rng: random.Random | None = None,
    penalty: float = 0.0,
) -> float:
    """Effective application throughput with load, channel, and MAC dynamics."""
    rng = rng or random.Random(int(scenario["seed"]) + frame)
    snr_db = float(snr_db if snr_db is not None else scenario["link"]["base_snr_db"])
    modulation = str(decision.get("modulation", "QPSK"))

    bits_per_symbol = {
        "BPSK": 1.0,
        "DSSS-BPSK": 1.0,
        "CSS": 0.65,
        "QPSK": 2.0,
        "OFDM-QPSK": 2.1,
        "FHSS-QPSK": 1.8,
        "8PSK": 3.0,
        "16QAM": 4.0,
    }.get(modulation, 2.0)
    required_snr = {
        "BPSK": 4.0,
        "DSSS-BPSK": 2.5,
        "CSS": 1.5,
        "QPSK": 8.0,
        "OFDM-QPSK": 7.5,
        "FHSS-QPSK": 7.0,
        "8PSK": 13.5,
        "16QAM": 18.5,
    }.get(modulation, 8.0)

    channel_width_khz = 2000.0
    coding = clamp(float(decision.get("coding_rate", 0.75)), 0.32, 0.90)
    bandwidth = clamp(float(decision.get("bandwidth_scale", 1.0)), 0.35, 1.0)
    spread = max(1.0, float(decision.get("spreading_factor", 1.0)))
    interleave_depth = max(1, int(decision.get("interleaving_depth", 1)))
    phy_kbps = channel_width_khz * bits_per_symbol * coding * bandwidth
    phy_kbps /= spread ** 0.72
    phy_kbps *= 1.0 - min(0.18, (interleave_depth - 1) * 0.026)

    margin = snr_db - required_snr
    snr_efficiency = clamp(1.0 / (1.0 + math.exp(-margin / 2.2)), 0.04, 0.99)
    mac_efficiency = 0.70 + 0.08 * math.sin(frame * 0.071 + int(scenario["seed"]) % 11)
    mac_efficiency += rng.gauss(0.0, 0.035)
    if scenario["id"] == "rescue_congestion":
        mac_efficiency -= 0.10 + 0.04 * math.sin(frame * 0.17)
    mac_efficiency = clamp(mac_efficiency, 0.42, 0.86)

    offered = _offered_load_kbps(scenario, decision, frame, rng)
    retransmission_eff = clamp((1.0 - per) ** 1.7, 0.02, 1.0)
    transition_eff = 0.34 if int(decision.get("transition_remaining", 0)) > 0 else 1.0
    if jam_active and int(decision.get("anti_jamming_mode", 0)) == 0:
        jam_eff = clamp(0.42 - 0.018 * max(0.0, penalty), 0.06, 0.48)
    elif jam_active:
        jam_eff = clamp(0.82 - 0.010 * max(0.0, penalty), 0.58, 0.90)
    else:
        jam_eff = 1.0
    fading_eff = clamp(1.0 + rng.gauss(0.0, 0.075) + 0.05 * math.sin(frame * 0.113), 0.70, 1.22)

    link_capacity = phy_kbps * snr_efficiency * mac_efficiency
    return max(0.0, min(offered, link_capacity) * retransmission_eff * transition_eff * jam_eff * fading_eff)


def _offered_load_kbps(
    scenario: dict[str, Any],
    decision: dict[str, Any],
    frame: int,
    rng: random.Random,
) -> float:
    sid = scenario["id"]
    clean_profile = str(decision.get("action_profile", "")).startswith("æ— å¹²æ‰°")
    nominal = {
        "base_station_outage": 720.0,
        "rescue_congestion": 1050.0,
        "uav_video_pressure": 1750.0,
    }.get(sid, 760.0)
    fallback = int(decision.get("tx_task_mode", 1)) == 4 or "ä¿åº•" in str(decision.get("action_profile", ""))
    if fallback:
        nominal *= {"base_station_outage": 0.42, "rescue_congestion": 0.48, "uav_video_pressure": 0.55}.get(sid, 0.5)
    if not clean_profile and sid == "uav_video_pressure":
        nominal *= 0.92
    wave = 1.0 + 0.13 * math.sin(frame * 0.049 + int(scenario["seed"]) % 7)
    wave += 0.07 * math.sin(frame * 0.181 + int(scenario["seed"]) % 5)
    burst = 1.0
    if sid == "uav_video_pressure" and frame % 42 in range(0, 9):
        burst += 0.22
    if sid == "rescue_congestion" and frame % 55 in range(16, 29):
        burst += 0.16
    if sid == "base_station_outage" and frame % 64 in range(8, 16):
        burst += 0.11
    stochastic = clamp(1.0 + rng.gauss(0.0, 0.09), 0.72, 1.32)
    return max(30.0, nominal * wave * burst * stochastic)


def _active_payload_kbits(scenario: dict[str, Any], decision: dict[str, Any]) -> float:
    """Emergency fallback tasks intentionally reduce information volume."""
    if decision["tx_task"] == scenario["business"]["initial_task"]:
        return float(scenario["business"]["payload_kbits"])
    if int(decision["tx_task_mode"]) == 4:
        return 120.0
    if int(decision["tx_task_mode"]) == 1:
        return 360.0
    return float(scenario["business"]["payload_kbits"]) * 0.65


def _spectrum(
    global_cfg: dict[str, Any],
    affected: list[int],
    action: dict[str, Any],
    decision: dict[str, Any],
    rng: random.Random,
    frame: int = 0,
) -> tuple[list[float], list[float]]:
    n = int(global_cfg["sample_points"])
    center = float(global_cfg["center_freq_hz"])
    span = int(global_cfg["num_channels"]) * float(global_cfg["channel_width_hz"])
    freq_axis = [round((center - span / 2 + span * i / (n - 1)) / 1e9, 6) for i in range(n)]
    spectrum = []
    comm_ch = int(decision["comm_channel_idx"])
    for i in range(n):
        ch = min(int(i / max(1, n - 1) * int(global_cfg["num_channels"])), int(global_cfg["num_channels"]) - 1)
        amp = -88.0 + rng.gauss(0, 1.8)
        if ch == comm_ch:
            amp += 24.0 + rng.uniform(-2.0, 2.0)
        if ch in affected:
            amp += float(action["power_db"]) * (1.45 if int(action["waveform_mode"]) == 0 else 1.1)
            if int(action["waveform_mode"]) == 1:
                tone_phase = abs(math.sin(i * 0.12))
                amp += 16.0 if tone_phase > 0.94 else -3.0
        spectrum.append(round(amp, 3))
    return spectrum, freq_axis


def _jam_waveform(global_cfg: dict[str, Any], action: dict[str, Any], rng: random.Random) -> list[float]:
    n = int(global_cfg["waveform_points"])
    power = float(action["power_db"])
    if power <= 0:
        return [0.0 for _ in range(n)]
    waveform = []
    for i in range(n):
        if int(action["waveform_mode"]) == 0:
            v = abs(rng.gauss(0.0, 0.35) + 0.25 * math.sin(i * 0.21))
        else:
            v = abs(0.55 * math.sin(i * 0.19) + 0.35 * math.sin(i * 0.47))
        waveform.append(round(clamp(v * (0.55 + power / 35.0), 0.0, 1.6), 4))
    return waveform


def _channel_map_and_features(
    num_channels: int,
    affected: list[int],
    comm_channel_idx: int,
    qualities: list[float],
    snr_db: float,
) -> tuple[list[int], list[list[float]]]:
    channel_map = []
    features = []
    for ch in range(num_channels):
        occupied = 1.0 if ch == comm_channel_idx else max(0.0, 0.45 - qualities[ch] * 0.3)
        interference = 1.0 - qualities[ch]
        if ch in affected:
            interference = max(interference, 0.75)
        utility = qualities[ch]
        snr_like = clamp((snr_db + 4) / 34, 0.0, 1.0) if ch == comm_channel_idx else qualities[ch] * 0.7
        if ch == comm_channel_idx and ch in affected:
            state = 3
        elif ch == comm_channel_idx:
            state = 1
        elif ch in affected:
            state = 2
        else:
            state = 0
        channel_map.append(state)
        features.append([round(occupied, 3), round(interference, 3), round(utility, 3), round(snr_like, 3)])
    return channel_map, features


def _mode_key(mode: int, mode_name: str, decision: dict[str, Any] | None = None) -> str:
    if decision and str(decision.get("modulation")) in {"BPSK", "DSSS-BPSK", "CSS"}:
        return "low_rate"
    if decision and str(decision.get("modulation")) in {"OFDM-QPSK", "FHSS-QPSK"}:
        return "frequency_hop"
    if mode == 1:
        return "low_rate"
    if mode == 2:
        return "frequency_hop"
    if "增益" in mode_name:
        return "power_boost"
    return "normal"


def _ber_multiplier(decision: dict[str, Any]) -> float:
    coding = float(decision.get("coding_rate", 0.75))
    spreading = float(decision.get("spreading_factor", 1.0))
    interleaving = int(decision.get("interleaving_depth", 1))
    diversity = int(decision.get("route_diversity", 1))
    modulation = str(decision.get("modulation", "QPSK"))
    multiplier = 1.0
    multiplier *= max(0.35, coding / 0.75)
    multiplier *= max(0.45, 1.0 / (spreading ** 0.45))
    multiplier *= max(0.55, 1.0 - 0.055 * max(0, interleaving - 1))
    multiplier *= {
        "BPSK": 0.72,
        "DSSS-BPSK": 0.60,
        "CSS": 0.55,
        "QPSK": 1.0,
        "OFDM-QPSK": 0.86,
        "FHSS-QPSK": 0.80,
        "8PSK": 1.35,
        "16QAM": 2.0,
    }.get(modulation, 1.0)
    if diversity > 1:
        multiplier *= 0.75
    return multiplier


def _tx_ui_payload(record: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    decision = record["decision"]
    metrics = record["metrics"]
    n = 512
    freq = [round(-195 + 390 * i / (n - 1), 3) for i in range(n)]
    center_bin = 256 + (int(decision["comm_channel_idx"]) - 5) * 12
    amp = []
    for i in range(n):
        envelope = -75 + 45 * math.exp(-((i - center_bin) / 22) ** 2)
        amp.append(round(envelope + 4 * math.sin(i * 0.07), 3))
    t = [round(i * 0.002, 4) for i in range(600)]
    time_amp = [round(abs(math.sin(i * 0.08)) * (0.35 + metrics["throughput_kbps"] / 800), 4) for i in range(600)]
    fb_amp = [round(abs(math.sin(i * 0.13)) * max(0.05, 1.0 - metrics["per"]), 4) for i in range(420)]
    # QPSK constellation points for the reverse-link scatter plot
    rng_con = random.Random(int(scenario["seed"]) + record["frame"] + 901)
    const_n = 300
    noise = min(0.28, 0.04 + metrics["per"] * 1.5)
    const_i = [round((1 if i % 4 in (0, 1) else -1) + noise * rng_con.gauss(0, 1), 4) for i in range(const_n)]
    const_q = [round((1 if i % 4 in (0, 2) else -1) + noise * rng_con.gauss(0, 1), 4) for i in range(const_n)]
    return {
        "tx_spec": {"freq": freq, "amp": amp},
        "tx_time": {"time": t, "amp": time_amp},
        "rx_const": {"i": const_i, "q": const_q},
        "rx_time": {"time": [round(i * 0.002, 4) for i in range(420)], "amp": fb_amp},
        "status": {
            "tx_valid": "有效",
            "tx_mod": "QPSK/BPSK自适应",
            "tx_mode": f"{decision['tx_task']} | {decision['anti_jamming_mode_name']} | 进度{metrics['task_progress']*100:.1f}%",
            "tx_carrier": f"{decision['carrier_ghz']:.2f} GHz",
            "tx_samp": "390.62 kHz",
            "tx_gain": f"{decision['power_gain_db']} dB",
            "rx_state": f"反向参数链路: {decision['last_action']}",
            "rx_carrier": "1.45 GHz",
            "rx_tx_gain": f"{decision['power_gain_db']} dB",
            "rx_tx_carrier": f"{decision['carrier_ghz']:.2f} GHz",
            "fb_health": f"{(1-metrics['per'])*100:.1f}% 仿真反馈",
            "time": f"仿真时间: {record['time_s']:.2f}s",
        },
        "sending_image": str(REPO_ROOT / "transmitter_and_receive" / "发射机-http" / "p2.jpg"),
    }


def _rx_ui_payload(record: dict[str, Any], scenario: dict[str, Any], centers_hz: list[float]) -> dict[str, Any]:
    decision = record["decision"]
    metrics = record["metrics"]
    spectrum = record["signals"]["spectrum"]
    n = len(spectrum)
    freq = [round(-195 + 390 * i / (n - 1), 3) for i in range(n)]
    constellation_n = 400
    spread = min(0.9, 0.08 + metrics["per"] * 2.2)
    const_i = [round((1 if i % 4 in (0, 1) else -1) + spread * math.sin(i * 1.7), 4) for i in range(constellation_n)]
    const_q = [round((1 if i % 4 in (0, 2) else -1) + spread * math.cos(i * 1.3), 4) for i in range(constellation_n)]
    time = [round(i * 0.002, 4) for i in range(650)]
    time_amp = [round(abs(math.sin(i * 0.08)) * max(0.04, 1.0 - metrics["per"]), 4) for i in range(650)]
    progress_text = f"{scenario['business']['initial_task']} 恢复进度 {metrics['task_progress']*100:.1f}%"
    if decision["tx_task"] != scenario["business"]["initial_task"]:
        progress_text = f"{decision['tx_task']} | {progress_text}"
    return {
        "spectrum": {"freq": freq, "amp": spectrum},
        "spectrum_mes": {"freq": freq, "amp": spectrum},
        "time_domain": {"time": time, "amp": time_amp},
        "constellation": {"i": const_i, "q": const_q},
        "waterfall_line": spectrum,
        "waterfall_linear": [10 ** (v / 20.0) for v in spectrum],
        "status": {
            "data_rec_valid": "有效" if metrics["per"] < 0.55 else "弱信号保持",
            "rx_mode_name": decision["tx_task"],
            "current_send_mode": decision["anti_jamming_mode_name"],
            "current_mod": "BPSK+扩频" if decision["anti_jamming_mode"] == 1 else "QPSK",
            "center_frequency": centers_hz[int(decision["comm_channel_idx"])],
            "samp_rate": 390625.0,
            "snr": f"{metrics['snr_db']:.2f} dB",
            "mes_valid": f"智能决策: {decision['last_action']}",
            "mes_rate": metrics["throughput_kbps"] * 1000,
            "power_gain": f"{decision['power_gain_db']} dB",
            "carrier_gain": f"{decision['carrier_ghz']:.2f} GHz",
            "ber": f"{metrics['ber']:.3e}",
            "fb_health": f"{(1-metrics['per'])*100:.1f}% 仿真反馈",
            "current_time": f"{record['time_s']:.2f}s",
            "received_text": progress_text,
        },
        "received_image": "",
        "image_rebuild_status": progress_text,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate deterministic emergency-jamming timelines.")
    parser.add_argument("--scenario", default="all", help="Scenario id or 'all'.")
    parser.add_argument("--strategy", default="智能决策策略", choices=["智能决策策略", "无抗干扰", "固定低速抗扰"])
    parser.add_argument("--seed-offset", type=int, default=0)
    args = parser.parse_args()

    config = load_config()
    ensure_output_dirs()
    generated = []
    for scenario in iter_scenarios(config, args.scenario):
        timeline = generate_scenario_timeline(
            scenario,
            config["global"],
            strategy=args.strategy,
            seed_offset=args.seed_offset,
        )
        suffix = "" if args.strategy == "智能决策策略" else "_" + args.strategy
        out_path = TIMELINE_DIR / f"{scenario['id']}{suffix}.jsonl"
        write_timeline(timeline, out_path)
        generated.append(out_path)
        print(f"[OK] {scenario['short_name']} -> {out_path}")

    manifest = {
        "generated": [str(p.relative_to(OUTPUT_ROOT)) for p in generated],
        "strategy": args.strategy,
        "seed_offset": args.seed_offset,
    }
    (OUTPUT_ROOT / "last_generation_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
