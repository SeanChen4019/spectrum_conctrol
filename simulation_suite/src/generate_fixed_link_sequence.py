from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any

from common import OUTPUT_ROOT, channel_centers_hz, ensure_output_dirs, load_config


FIXED_PATH = OUTPUT_ROOT / "fixed_link_sequence.json"

SCENE_NARRATION = {
    "base_station_outage": (
        "场景说明：灾后局部公网基站退服，现场临时应急专网附近出现8MHz宽带噪声抬升。"
        "干扰机设置信道3、宽带噪声、15dB、8MHz，用来模拟大范围噪声底抬升，"
        "对应灾后公网覆盖受损、临时通信链路被噪声抬高的应急保障场景。"
    ),
    "rescue_congestion": (
        "场景说明：多支救援队伍同时携带图传、专网手台和传感节点进入现场，局部频点发生拥塞。"
        "干扰机设置信道6、多音干扰、10dB、4MHz，用来模拟多个离散频点冲突，"
        "对应救援队伍密集接入导致的频点碰撞和局部拥堵场景。"
    ),
    "uav_video_pressure": (
        "场景说明：无人机执行灾情侦察时，视频回传链路遭遇高功率压制。"
        "干扰机设置信道8、宽带噪声、20dB、6MHz，用来模拟强压制电磁环境，"
        "对应无人机视频回传在复杂灾区电磁环境下受到高功率压制的场景。"
    ),
}

# ── Per-scenario communication link state (steady-state after anti-jam response) ──
# This is what the comm link looks like AFTER the intelligent agent has reacted.
# The jammer UI only sees the RESULT — a comm signal at a specific channel with
# a specific bandwidth, possibly overlapping the jam zone.
FIXED_LINK: dict[str, dict[str, Any]] = {
    "base_station_outage": {
        "comm_channel_idx": 5,        # displayed ch6, stays in place under full-band outage
        "carrier_ghz": 3.0,
        "anti_jamming_mode": "低速抗扰模式",
        "modulation": "BPSK+扩频",
        "bw_mhz": 1.0,               # narrowband — BPSK + spreading reduces effective BW
        "tx_task": "现场图片回传（低速可靠）",
        "snr_db": 15.5,
        "description": "宽带噪声环境下，低速抗扰模式以窄带BPSK+扩频维持弱信号回传",
    },
    "rescue_congestion": {
        "comm_channel_idx": 7,        # displayed ch8, outside affected displayed ch5-6
        "carrier_ghz": 3.5,
        "anti_jamming_mode": "切频模式",
        "modulation": "QPSK",
        "bw_mhz": 2.0,               # normal bandwidth after successful hop
        "tx_task": "多终端图传与状态包（信道2）",
        "snr_db": 25.5,
        "description": "多音冲突环境下，切频至信道2避开干扰，恢复正常吞吐",
    },
    "uav_video_pressure": {
        "comm_channel_idx": 2,        # displayed ch3, outside affected displayed ch6-9
        "carrier_ghz": 3.0,
        "anti_jamming_mode": "切频模式",
        "modulation": "QPSK",
        "bw_mhz": 2.0,               # normal bandwidth, boosted power
        "tx_task": "关键帧图片回传（增益补偿）",
        "snr_db": 24.0,
        "description": "高功率压制环境下，切频至信道3并提升增益，保障关键帧回传",
    },
}

RESPONSE_DELAY_FRAMES = 12

PRE_RESPONSE_LINK: dict[str, dict[str, Any]] = {
    "base_station_outage": {
        "comm_channel_idx": 5,
        "carrier_ghz": 3.0,
        "anti_jamming_mode": "常规模式",
        "modulation": "QPSK",
        "bw_mhz": 2.0,
        "tx_task": "现场图片回传（AI判决延迟）",
        "snr_db": 7.0,
        "description": "干扰刚出现，通信仍停留在6号信道，等待AI完成链路判决。",
    },
    "rescue_congestion": {
        "comm_channel_idx": 5,
        "carrier_ghz": 3.0,
        "anti_jamming_mode": "常规模式",
        "modulation": "QPSK",
        "bw_mhz": 2.0,
        "tx_task": "多终端图传与状态包（AI判决延迟）",
        "snr_db": 10.0,
        "description": "干扰切换到5-6号信道后，通信短暂停留在6号信道并发生重叠。",
    },
    "uav_video_pressure": {
        "comm_channel_idx": 7,
        "carrier_ghz": 3.5,
        "anti_jamming_mode": "常规模式",
        "modulation": "QPSK",
        "bw_mhz": 2.0,
        "tx_task": "无人机视频回传（AI判决延迟）",
        "snr_db": 6.0,
        "description": "干扰切换到6-9号信道后，通信短暂停留在8号信道并发生重叠。",
    },
}

def discrete_affected_channels(channel_idx, bw_mhz, global_cfg):
    n = int(global_cfg["num_channels"])
    width_mhz = float(global_cfg["channel_width_hz"]) / 1e6
    count = max(1, int(round(float(bw_mhz) / width_mhz)))
    count = min(n, count)
    start = int(channel_idx) - count // 2
    start = max(0, min(n - count, start))
    return list(range(start, start + count))


def _build_channel_state(comm_ch, comm_bw_mhz, affected, num_channels, channel_width_mhz):
    """
    Map communication signal's occupied channels given its centre channel and bandwidth.
    A narrowband signal (e.g. 1 MHz in a 2 MHz channel grid) occupies only the centre channel.
    A normal signal (2 MHz) occupies exactly 1 channel.
    """
    comm_channels = set()
    half_bw_ch = comm_bw_mhz / (2 * channel_width_mhz)
    for ch in range(num_channels):
        dist = abs(ch - comm_ch) * channel_width_mhz
        if dist < half_bw_ch + 0.01:
            comm_channels.add(ch)

    ch_map = []
    for ch in range(num_channels):
        in_comm = ch in comm_channels
        in_jam = ch in affected
        if in_comm and in_jam:
            state = 3  # overlap
        elif in_comm:
            state = 1  # user
        elif in_jam:
            state = 2  # jammer
        else:
            state = 0  # idle
        ch_map.append(state)
    return ch_map


def _spectrum(global_cfg, affected, action, comm_ch, comm_bw_mhz, rng):
    n = 512
    num_channels = int(global_cfg["num_channels"])
    center = float(global_cfg["center_freq_hz"])
    span = num_channels * float(global_cfg["channel_width_hz"])
    freq_axis = [round((center - span / 2 + span * i / (n - 1)) / 1e9, 6) for i in range(n)]

    # Comm signal bandwidth in fraction of total span
    bw_ratio = comm_bw_mhz / (num_channels * float(global_cfg["channel_width_hz"]) / 1e6)
    # In sample-point units: how many bins the comm signal occupies
    comm_bins = max(2, int(n * bw_ratio))
    comm_center_bin = int(comm_ch / num_channels * n + n / (2 * num_channels))

    spectrum = []
    for i in range(n):
        ch = min(int(i / n * num_channels), num_channels - 1)
        amp = -88.0 + rng.gauss(0, 1.3)

        # Communication signal — Gaussian-shaped peak, width = comm_bins
        dist_bins = abs(i - comm_center_bin)
        sigma = comm_bins / 3.0
        if dist_bins < comm_bins * 1.5:
            amp += 27.0 * math.exp(-0.5 * (dist_bins / sigma) ** 2) + 2.0 * math.sin(i * 0.05)

        if ch in affected:
            amp += float(action["power_db"]) * (1.35 if action["waveform_mode"] == 0 else 1.05)
            if action["waveform_mode"] == 1 and abs(math.sin(i * 0.16)) > 0.93:
                amp += 12.0
        spectrum.append(round(amp, 3))
    return spectrum, freq_axis


def _waveform(action, rng):
    if float(action["power_db"]) <= 0:
        return [0.0] * 256
    vals = []
    for i in range(256):
        if action["waveform_mode"] == 0:
            v = abs(rng.gauss(0.0, 0.34) + 0.22 * math.sin(i * 0.2))
        else:
            v = abs(0.58 * math.sin(i * 0.18) + 0.36 * math.sin(i * 0.43))
        vals.append(round(min(1.5, v), 4))
    return vals


def _build_frame(frame, global_cfg, affected, action, link, rng):
    num_channels = int(global_cfg["num_channels"])
    channel_width_mhz = float(global_cfg["channel_width_hz"]) / 1e6
    comm_ch = int(link["comm_channel_idx"])
    comm_bw_mhz = float(link["bw_mhz"])
    snr = float(link["snr_db"]) + 1.4 * math.sin(frame * 0.08) + rng.gauss(0, 0.35)

    channel_map = _build_channel_state(comm_ch, comm_bw_mhz, affected, num_channels, channel_width_mhz)

    features = []
    for ch in range(num_channels):
        state = channel_map[ch]
        if state == 3:  # overlap
            interference, occupancy, utility, snr_like = 0.82, 0.92, 0.18, 0.25
        elif state == 1:  # user
            interference, occupancy, utility = 0.08, 0.92, 0.88
            snr_like = min(1.0, (snr + 2) / 34.0)
        elif state == 2:  # jammer
            interference, occupancy, utility, snr_like = 0.82, 0.10, 0.18, 0.12
        else:  # idle
            interference = 0.08 + rng.random() * 0.14
            occupancy = 0.08 + rng.random() * 0.18
            utility = max(0.15, 1.0 - interference)
            snr_like = utility * 0.65
        features.append([round(occupancy, 3), round(interference, 3), round(utility, 3), round(snr_like, 3)])

    spectrum, freq_axis = _spectrum(global_cfg, affected, action, comm_ch, comm_bw_mhz, rng)
    return {
        "frame": frame,
        "time_s": round(frame * float(global_cfg["frame_interval_s"]), 3),
        "action": action,
        "telemetry": {
            "snr_est": round(snr, 3),
            "bw_est": float(action["bw_mhz"]) * 1e6,
            "sig_type": 1,
            "tx_state": "fixed_sequence_running",
            "spectrum": spectrum,
            "freq_axis_ghz": freq_axis,
            "jam_waveform_abs": _waveform(action, rng),
            "channel_map": channel_map,
            "channel_features": features,
        },
    }


def _build_initial_environment(global_cfg):
    rng = random.Random(2026060299)
    jam = {
        "channel_idx": 5,
        "waveform_mode": 0,
        "power_db": 0.0,
        "bw_mhz": 2,
    }
    action = {"channel_idx": jam["channel_idx"], "waveform_mode": jam["waveform_mode"],
              "power_db": jam["power_db"], "bw_mhz": jam["bw_mhz"]}
    affected = []
    link = {"comm_channel_idx": 5, "bw_mhz": 2.0, "snr_db": 25.0}
    frames = [_build_frame(i, global_cfg, affected, action, link, rng) for i in range(180)]
    return {
        "name": "初始无干扰通信环境",
        "short_name": "无干扰",
        "description": "UI启动后通信信号位于6号信道，干扰机未发射。",
        "jammer": jam,
        "affected_channels": affected,
        "frames": frames,
    }


def _build_scene_frames(global_cfg, sid, affected, action, final_link, rng):
    frames = []
    pre_link = PRE_RESPONSE_LINK.get(sid)
    if pre_link:
        for i in range(RESPONSE_DELAY_FRAMES):
            frames.append(_build_frame(i, global_cfg, affected, action, pre_link, rng))
    for i in range(RESPONSE_DELAY_FRAMES, 180):
        frames.append(_build_frame(i, global_cfg, affected, action, final_link, rng))
    return frames

def generate_fixed_link_sequence() -> dict:
    cfg = load_config()
    global_cfg = cfg["global"]
    sequence = {
        "version": "2.0",
        "description": "固定序列：180帧稳态。通信链路已执行抗干扰策略，信道状态=几何覆盖关系。",
        "frame_interval_s": global_cfg["frame_interval_s"],
        "num_channels": global_cfg["num_channels"],
        "center_freq_hz": global_cfg["center_freq_hz"],
        "channel_width_hz": global_cfg["channel_width_hz"],
        "initial_environment": _build_initial_environment(global_cfg),
        "scenarios": {},
    }
    for scenario in cfg["scenarios"]:
        sid = scenario["id"]
        rng = random.Random(int(scenario["seed"]) + 7707)
        jam = scenario["jammer"]
        action = {"channel_idx": int(jam["channel_idx"]), "waveform_mode": int(jam["waveform_mode"]),
                  "power_db": float(jam["power_db"]), "bw_mhz": int(jam["bw_mhz"])}
        affected = discrete_affected_channels(jam["channel_idx"], jam["bw_mhz"], global_cfg)
        link = FIXED_LINK[sid]
        frames = _build_scene_frames(global_cfg, sid, affected, action, link, rng)
        sequence["scenarios"][sid] = {
            "scenario_id": sid, "name": scenario["name"],
            "short_name": scenario["short_name"],
            "narration": SCENE_NARRATION.get(sid, ""),
            "jammer": jam, "fixed_link": link,
            "affected_channels": affected, "frames": frames,
        }
    return sequence


def main() -> None:
    ensure_output_dirs()
    sequence = generate_fixed_link_sequence()
    FIXED_PATH.write_text(json.dumps(sequence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] fixed sequence saved to {FIXED_PATH}")
    for sid, scene in sequence["scenarios"].items():
        link = scene["fixed_link"]
        aff = scene["affected_channels"]
        overlap = "重叠" if link["comm_channel_idx"] in aff else "独立"
        print(f"  {scene['short_name']}: comm=ch{link['comm_channel_idx']}({link['bw_mhz']}MHz {link['anti_jamming_mode']}) | jam→{aff} | {overlap}")


if __name__ == "__main__":
    main()

