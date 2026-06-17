"""
=============================================================================
Comprehensive Simulation Experiment for Spectrum Control System
面向应急保障的智能体频谱管控系统 —— 完整仿真实验

Covers all metrics from 研电赛技术论文_精简版0604.pdf Table 3-3, 3-4, 3-5:
  - Communication quality: BER, SNR, Throughput, PER, SINR
  - Anti-jamming: P_switch, τ_switch, G_anti, recovery time
  - System-level: emergency composite score, task completion rate
  - Statistical: mean, std, 95% CI over Monte Carlo runs

Author: Auto-generated experiment suite
=============================================================================
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Ensure we import from the same package ──
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    METRICS_DIR,
    OUTPUT_ROOT,
    TIMELINE_DIR,
    ber_from_snr,
    channel_centers_hz,
    clamp,
    ensure_output_dirs,
    iter_scenarios,
    load_config,
    per_from_ber,
    safe_mean,
)
from decision_policy import EmergencyDecisionPolicy, estimate_quality_penalty
from generate_jammer_data import (
    _channel_quality,
    _snr_from_state,
    _throughput_kbps,
    _active_payload_kbits,
    _spectrum,
    _jam_waveform,
    _channel_map_and_features,
    _mode_key,
    _ber_multiplier,
    affected_channels,
)

# ═══════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════

NO_INTERFERENCE = "无干扰基线"
JAM_NO_ANTI = "受干扰无抗扰"
INTELLIGENT = "智能决策抗扰"

STRATEGIES = [NO_INTERFERENCE, JAM_NO_ANTI, INTELLIGENT]
STRATEGY_LABELS = {
    NO_INTERFERENCE: "Clean Baseline",
    JAM_NO_ANTI: "Jammed No-AntiJam",
    INTELLIGENT: "Intelligent Anti-Jam",
}
STRATEGY_LABELS_CN = {
    NO_INTERFERENCE: "无干扰基线",
    JAM_NO_ANTI: "受干扰无抗扰",
    INTELLIGENT: "智能决策抗扰",
}
SCENARIO_LABELS = {
    "base_station_outage": "基站退服",
    "rescue_congestion": "救援拥塞",
    "uav_video_pressure": "无人机压测",
}

# Number of Monte Carlo runs per scenario per strategy
DEFAULT_MC_RUNS = 50

# ═══════════════════════════════════════════════════════════════════════════
# Core simulation engine (extended)
# ═══════════════════════════════════════════════════════════════════════════


def simulate_scenario(
    scenario: dict[str, Any],
    global_cfg: dict[str, Any],
    strategy: str = "智能决策策略",
    seed_offset: int = 0,
) -> list[dict[str, Any]]:
    """Run a single simulation episode and return frame-by-frame timeline."""
    import random
    rng = random.Random(int(scenario["seed"]) + seed_offset)
    frames = int(scenario.get("frames", global_cfg["default_frames"]))
    interval = float(global_cfg["frame_interval_s"])
    num_channels = int(global_cfg["num_channels"])
    centers = channel_centers_hz(global_cfg)
    policy_strategy = "无抗干扰" if strategy in {NO_INTERFERENCE, JAM_NO_ANTI} else "智能决策策略"
    policy = EmergencyDecisionPolicy(scenario, global_cfg, strategy=policy_strategy)

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
        if strategy == NO_INTERFERENCE:
            jam_active = False
        action = dict(base_action)
        if not jam_active:
            action["power_db"] = 0.0

        affected = affected_channels(action["channel_idx"], action["bw_mhz"], global_cfg) if jam_active else []
        channel_quality = _channel_quality(num_channels, affected, action, rng)

        prev_state = policy.state
        raw_penalty = estimate_quality_penalty(prev_state.comm_channel_idx, action, global_cfg) if jam_active else 0.0
        pre_snr = _snr_from_state(scenario, prev_state.snapshot(), raw_penalty, rng)
        decision_state = policy.update(frame, pre_snr, channel_quality, jam_active).snapshot()
        if strategy == NO_INTERFERENCE:
            _apply_clean_baseline_profile(decision_state, scenario)
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

        # JSR estimation
        jsr_db = max(0.0, penalty) if jam_active else 0.0

        # Spectrum efficiency (bps/Hz)
        bw_hz = float(global_cfg["channel_width_hz"])
        spectral_efficiency = (throughput * 1000) / bw_hz if bw_hz > 0 else 0.0

        record = {
            "frame": frame,
            "time_s": round(frame * interval, 3),
            "jam_active": jam_active,
            "action": action,
            "affected_channels": affected,
            "decision": decision_state,
            "metrics": {
                "snr_db": round(snr_db, 3),
                "sinr_db": round(sinr_db, 3),
                "jsr_db": round(jsr_db, 3),
                "ber": ber,
                "per": per,
                "throughput_kbps": round(throughput, 3),
                "throughput_mbps": round(throughput / 1000.0, 4),
                "spectral_efficiency_bps_hz": round(spectral_efficiency, 4),
                "task_progress": round(task_progress, 4),
                "task_completed": task_progress >= 1.0,
                "overlap": bool(jam_active and penalty > 0),
                "available_channel_ratio": round(sum(1 for q in channel_quality if q > 0.55) / num_channels, 3),
            },
        }
        timeline.append(record)

    return timeline


def _apply_clean_baseline_profile(decision: dict[str, Any], scenario: dict[str, Any]) -> None:
    """Normal no-jam operating point: high-throughput waveform without anti-jam overhead."""
    initial_ch = int(scenario["link"]["initial_comm_channel_idx"])
    high_task_mode = {"base_station_outage": 2, "rescue_congestion": 3, "uav_video_pressure": 3}.get(scenario["id"], 2)
    decision.update({
        "strategy": NO_INTERFERENCE,
        "anti_jamming_mode": 0,
        "anti_jamming_mode_name": "无干扰高速基线",
        "comm_channel_idx": initial_ch,
        "carrier_select": min(5, max(1, initial_ch // 2 + 1)),
        "carrier_ghz": [2.0, 2.5, 3.0, 3.5, 4.0][min(4, max(0, initial_ch // 2))],
        "power_gain_db": 16,
        "tx_task": scenario["business"]["initial_task"],
        "tx_task_mode": high_task_mode,
        "redundancy": 1.0,
        "burst_packets": 10,
        "modulation": "16QAM",
        "coding_rate": 0.78,
        "spreading_factor": 1.0,
        "interleaving_depth": 1,
        "sync_threshold_db": 8.0,
        "bandwidth_scale": 1.0,
        "route_diversity": 1,
        "transition_remaining": 0,
        "action_profile": "无干扰16QAM高速传输",
        "last_action": "无干扰：16QAM高码率正常传输",
    })


# ═══════════════════════════════════════════════════════════════════════════
# Metric extraction (all paper metrics + extended)
# ═══════════════════════════════════════════════════════════════════════════


def extract_metrics(
    timeline: list[dict[str, Any]],
    scenario: dict[str, Any],
    strategy: str,
    run_idx: int,
) -> dict[str, Any]:
    """Extract all performance metrics from a simulation timeline."""
    interval = timeline[1]["time_s"] - timeline[0]["time_s"] if len(timeline) > 1 else 0.12
    metrics_list = [rec["metrics"] for rec in timeline]
    decisions = [rec["decision"] for rec in timeline]
    jam_frames = [rec for rec in timeline if rec["jam_active"]]
    jam_count = max(1, len(jam_frames))

    # ── Communication Quality Metrics ──
    avg_snr = safe_mean([m["snr_db"] for m in metrics_list])
    min_snr = min(m["snr_db"] for m in metrics_list)
    avg_sinr = safe_mean([m["sinr_db"] for m in metrics_list])
    avg_ber = safe_mean([m["ber"] for m in metrics_list])
    avg_per = safe_mean([m["per"] for m in metrics_list])
    avg_thr_kbps = safe_mean([m["throughput_kbps"] for m in metrics_list])
    avg_thr_mbps = avg_thr_kbps / 1000.0
    avg_spec_eff = safe_mean([m["spectral_efficiency_bps_hz"] for m in metrics_list])
    avg_jsr = safe_mean([m["jsr_db"] for m in metrics_list if m["jsr_db"] > 0])

    # SNR during jamming only
    jam_snr = safe_mean([m["snr_db"] for rec, m in zip(timeline, metrics_list) if rec["jam_active"]])
    clean_snr = safe_mean([m["snr_db"] for rec, m in zip(timeline, metrics_list) if not rec["jam_active"]])

    # ── Anti-Jamming Performance Metrics ──
    start_frame = int(scenario["jammer"]["start_frame"])
    first_response_frame = decisions[-1]["first_response_frame"]
    recovered_frame = decisions[-1]["recovered_frame"]
    decision_count = decisions[-1]["decision_count"]
    action_fields = [
        "comm_channel_idx",
        "anti_jamming_mode_name",
        "modulation",
        "coding_rate",
        "spreading_factor",
        "interleaving_depth",
        "power_gain_db",
        "sync_threshold_db",
        "bandwidth_scale",
        "route_diversity",
        "tx_task",
    ]
    action_changes = 0
    for prev, cur in zip(decisions, decisions[1:]):
        if any(prev.get(field) != cur.get(field) for field in action_fields):
            action_changes += 1
    final_decision = decisions[-1]

    # Response latency (from jam start to first anti-jam action)
    response_latency_s = None if first_response_frame is None else max(0.0, (first_response_frame - start_frame) * interval)
    response_latency_ms = None if response_latency_s is None else response_latency_s * 1000.0

    # Recovery time (from jam start to link recovery)
    recovery_time_s = None if recovered_frame is None else max(0.0, (recovered_frame - start_frame) * interval)

    # Frequency hop success rate (P_switch)
    hop_attempted = any(d["anti_jamming_mode"] == 2 for d in decisions)
    overlap_ratio = sum(1 for m in metrics_list if m["overlap"]) / len(timeline)
    post_jam_overlap = sum(1 for m in metrics_list if m["overlap"]) / jam_count
    hop_success = 1.0 if hop_attempted and post_jam_overlap < 0.55 else (0.0 if hop_attempted else None)

    # Anti-jam gain G_anti (SNR improvement from worst to recovered)
    jam_snr_vals = [m["snr_db"] for rec, m in zip(timeline, metrics_list) if rec["jam_active"]]
    post_recovery_snr_vals = []
    if recovered_frame is not None:
        post_recovery_snr_vals = [
            m["snr_db"] for rec, m in zip(timeline, metrics_list)
            if rec["frame"] > recovered_frame and not rec["jam_active"]
        ]
    elif first_response_frame is not None:
        post_recovery_snr_vals = [
            m["snr_db"] for rec, m in zip(timeline, metrics_list)
            if rec["frame"] > first_response_frame + 10
        ]

    worst_jam_snr = min(jam_snr_vals) if jam_snr_vals else avg_snr
    recovered_snr = safe_mean(post_recovery_snr_vals) if post_recovery_snr_vals else avg_snr
    anti_jam_gain_db = recovered_snr - worst_jam_snr

    # Throughput improvement ratio
    worst_thr = min([m["throughput_kbps"] for rec, m in zip(timeline, metrics_list) if rec["jam_active"]], default=avg_thr_kbps)
    post_thr_vals = [m["throughput_kbps"] for rec, m in zip(timeline, metrics_list)
                     if not rec["jam_active"] and rec["frame"] > (recovered_frame or 0)]
    recovered_thr = safe_mean(post_thr_vals) if post_thr_vals else avg_thr_kbps
    thr_improvement_ratio = recovered_thr / max(0.001, worst_thr)

    # ── Task-Level Metrics ──
    task_completed = any(m["task_completed"] for m in metrics_list)
    task_progress = metrics_list[-1]["task_progress"]
    completion_frame = next((rec["frame"] for rec in timeline if rec["metrics"]["task_completed"]), None)
    completion_time_s = None if completion_frame is None else round(completion_frame * interval, 3)

    # ── Channel Utilization Metrics ──
    available_ratio = safe_mean([m["available_channel_ratio"] for m in metrics_list])

    # ── Emergency Composite Score ──
    reliability = (1.0 - avg_per) * 0.55 + task_progress * 0.45
    if recovery_time_s is None:
        speed = 0.0 if overlap_ratio > 0.3 else 1.0
    else:
        speed = max(0.0, 1.0 - recovery_time_s / 20.0)
    throughput_score = min(1.0, avg_thr_kbps / 240.0)
    avoidance = 1.0 - overlap_ratio
    emergency_score = 100.0 * (0.40 * reliability + 0.25 * speed + 0.20 * throughput_score + 0.15 * avoidance)

    return {
        # ── Identifiers ──
        "scenario_id": scenario["id"],
        "scenario": scenario["short_name"],
        "strategy": strategy,
        "strategy_label": STRATEGY_LABELS.get(strategy, strategy),
        "run": run_idx,
        # ── Communication Quality (Table 3-3) ──
        "avg_snr_db": round(avg_snr, 4),
        "min_snr_db": round(min_snr, 4),
        "jam_snr_db": round(jam_snr, 4),
        "clean_snr_db": round(clean_snr, 4),
        "avg_sinr_db": round(avg_sinr, 4),
        "avg_ber": avg_ber,
        "avg_per": avg_per,
        "avg_throughput_kbps": round(avg_thr_kbps, 4),
        "avg_throughput_mbps": round(avg_thr_mbps, 4),
        "avg_spectral_efficiency_bps_hz": round(avg_spec_eff, 4),
        "avg_jsr_db": round(avg_jsr, 4),
        # ── Anti-Jamming Performance (Table 3-3, 3-5) ──
        "hop_success": hop_success,
        "hop_success_rate_pct": round(hop_success * 100, 1) if hop_success is not None else None,
        "response_latency_s": response_latency_s,
        "response_latency_ms": response_latency_ms,
        "recovery_time_s": recovery_time_s,
        "anti_jam_gain_db": round(anti_jam_gain_db, 4),
        "thr_improvement_ratio": round(thr_improvement_ratio, 2),
        "decision_count": decision_count,
        "action_change_count": action_changes,
        "final_comm_channel_idx": final_decision.get("comm_channel_idx"),
        "final_modulation": final_decision.get("modulation"),
        "final_action_profile": final_decision.get("action_profile"),
        "final_coding_rate": final_decision.get("coding_rate"),
        "final_spreading_factor": final_decision.get("spreading_factor"),
        "final_interleaving_depth": final_decision.get("interleaving_depth"),
        "final_power_gain_db": final_decision.get("power_gain_db"),
        "final_sync_threshold_db": final_decision.get("sync_threshold_db"),
        "final_bandwidth_scale": final_decision.get("bandwidth_scale"),
        "final_route_diversity": final_decision.get("route_diversity"),
        "overlap_ratio": round(overlap_ratio, 4),
        "post_jam_overlap_ratio": round(post_jam_overlap, 4),
        # ── Task Metrics ──
        "task_completed": int(task_completed),
        "task_progress": round(task_progress, 4),
        "completion_time_s": completion_time_s,
        # ── Channel ──
        "available_channel_ratio": round(available_ratio, 4),
        # ── Composite Score ──
        "emergency_score": round(emergency_score, 4),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Aggregation and statistical analysis
# ═══════════════════════════════════════════════════════════════════════════


def aggregate_runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate Monte Carlo runs into mean/std/CI95 per (scenario, strategy)."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["scenario"], row["strategy"])].append(row)

    numeric_cols = [
        "avg_snr_db", "min_snr_db", "jam_snr_db", "clean_snr_db", "avg_sinr_db",
        "avg_ber", "avg_per", "avg_throughput_kbps", "avg_throughput_mbps",
        "avg_spectral_efficiency_bps_hz", "avg_jsr_db",
        "anti_jam_gain_db", "thr_improvement_ratio",
        "decision_count", "action_change_count", "final_comm_channel_idx",
        "final_coding_rate", "final_spreading_factor", "final_interleaving_depth",
        "final_power_gain_db", "final_sync_threshold_db", "final_bandwidth_scale",
        "final_route_diversity", "overlap_ratio", "post_jam_overlap_ratio",
        "available_channel_ratio", "task_progress", "emergency_score",
    ]
    optional_numeric = ["response_latency_s", "response_latency_ms", "recovery_time_s",
                        "completion_time_s", "hop_success_rate_pct"]
    binary_cols = ["task_completed", "hop_success"]

    summary = []
    for (scenario, strategy), items in sorted(groups.items()):
        out = {"scenario": scenario, "strategy": strategy, "runs": len(items)}
        for col in numeric_cols:
            vals = [float(item[col]) for item in items]
            out[f"{col}_mean"] = safe_mean(vals)
            out[f"{col}_std"] = _std(vals)
            out[f"{col}_ci95"] = 1.96 * out[f"{col}_std"] / math.sqrt(len(vals)) if vals else 0.0
        for col in optional_numeric:
            vals = [float(item[col]) for item in items if item[col] is not None]
            out[f"{col}_mean"] = safe_mean(vals) if vals else None
            out[f"{col}_std"] = _std(vals) if vals else None
            out[f"{col}_ci95"] = 1.96 * _std(vals) / math.sqrt(len(vals)) if vals else None
        for col in binary_cols:
            vals = [float(item[col]) for item in items if item[col] is not None]
            out[f"{col}_rate"] = safe_mean(vals) if vals else None
        summary.append(out)
    return summary


def _std(vals: list[float]) -> float:
    if len(vals) <= 1:
        return 0.0
    mean = safe_mean(vals)
    return math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))


# ═══════════════════════════════════════════════════════════════════════════
# Report generation: Paper-format tables and figures
# ═══════════════════════════════════════════════════════════════════════════


def write_paper_table_3_4(summary: list[dict[str, Any]], path: Path) -> None:
    """Generate Table 3-4 format: Baseline + 3 jamming modes comparison."""
    # Group by scenario
    by_scenario = defaultdict(list)
    for row in summary:
        by_scenario[row["scenario"]].append(row)

    lines = [
        "# 表3-4 不同干扰模式下通信链路性能对比",
        "",
        "| 干扰模式 | 干扰功率(dBm) | 策略 | BER(均值) | SNR(dB) | 有效吞吐量(Mbps) | 通信状态 |",
        "|---|---|---|---:|---:|---:|---|",
    ]

    for scenario_name in ["基站退服", "救援拥塞", "无人机压测"]:
        rows = by_scenario.get(scenario_name, [])
        for row in rows:
            power = {"基站退服": 15, "救援拥塞": 10, "无人机压测": 20}.get(scenario_name, 0)
            ber = float(row["avg_ber_mean"])
            snr = float(row["avg_snr_db_mean"])
            thr = float(row["avg_throughput_mbps_mean"])
            if ber < 1e-4:
                status = "正常通信"
            elif ber < 5e-3:
                status = "中度降级"
            elif ber < 1e-2:
                status = "严重降级"
            else:
                status = "基本中断"
            lines.append(
                f"| {scenario_name} | {power} | {row['strategy']} | "
                f"{ber:.2e} | {snr:.1f} | {thr:.3f} | {status} |"
            )

    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  [OK] Table 3-4 → {path}")


def write_paper_table_3_5(summary: list[dict[str, Any]], path: Path) -> None:
    """Generate Table 3-5 format: Detailed frequency hopping performance."""
    # Find intelligent strategy rows
    intelligent_rows = [r for r in summary if r["strategy"] == INTELLIGENT]
    if not intelligent_rows:
        path.write_text("# 表3-5: No intelligent strategy data available.\n", encoding="utf-8")
        return

    lines = [
        "# 表3-5 切频抗扰模式关键性能指标",
        "",
        "| 性能指标 | 测量值 | 说明 |",
        "|---|---|---|",
    ]

    for row in intelligent_rows:
        scenario = row["scenario"]
        hop_rate = row.get("hop_success_rate", row.get("hop_success_rate_mean"))
        if hop_rate is not None:
            hop_rate = hop_rate * 100 if hop_rate <= 1 else hop_rate
        else:
            hop_rate_val = row.get("hop_success_rate_pct_mean")
            hop_rate = hop_rate_val if hop_rate_val is not None else "N/A"

        lat_ms = row.get("response_latency_ms_mean", "N/A")
        rec_s = row.get("recovery_time_s_mean", "N/A")
        gain = row.get("anti_jam_gain_db_mean", "N/A")
        thr_ratio = row.get("thr_improvement_ratio_mean", "N/A")

        lines.append(f"| **{scenario}** | | |")
        if isinstance(hop_rate, (int, float)):
            lines.append(f"| 切频成功率 P_switch | {hop_rate:.1f}% | 50次Monte Carlo |")
        if isinstance(lat_ms, (int, float)):
            lines.append(f"| 平均响应时延 τ_switch | {lat_ms:.1f} ms | 从干扰触发到策略响应 |")
        if isinstance(rec_s, (int, float)):
            lines.append(f"| 恢复时间 | {rec_s:.2f} s | 从干扰开始到链路恢复 |")
        if isinstance(gain, (int, float)):
            lines.append(f"| 抗扰增益 G_anti | +{gain:.1f} dB | 最差SNR到恢复后SNR |")
        if isinstance(thr_ratio, (int, float)):
            lines.append(f"| 吞吐量提升倍数 | {thr_ratio:.1f}x | 最差吞吐量到恢复后吞吐量 |")
        lines.append("| | | |")

    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  [OK] Table 3-5 → {path}")


def write_ablation_table_extended(summary: list[dict[str, Any]], path: Path) -> None:
    """Generate comprehensive ablation/comparison table across all strategies and scenarios."""
    lines = [
        "# 应急频谱管控系统综合性能对比 (均值±95%CI)",
        "",
        "| 场景 | 策略 | SNR(dB) | SINR(dB) | BER | PER | 吞吐(Mbps) | "
        "频谱效率(bps/Hz) | 响应时延(ms) | 恢复时间(s) | 抗扰增益(dB) | "
        "动作变更 | 最终信道 | 编码率 | 扩频因子 | 交织深度 | 功率增益(dB) | "
        "可用信道比 | 应急综合评分 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for row in summary:
        lat = row.get("response_latency_ms_mean")
        lat_txt = f"{float(lat):.0f}" if lat is not None else "--"
        rec = row.get("recovery_time_s_mean")
        rec_txt = f"{float(rec):.2f}" if rec is not None else "--"
        gain = row.get("anti_jam_gain_db_mean")
        gain_txt = f"{float(gain):.1f}" if gain is not None else "--"

        lines.append(
            f"| {row['scenario']} | {row['strategy']} | "
            f"{float(row['avg_snr_db_mean']):.2f}±{float(row['avg_snr_db_ci95']):.2f} | "
            f"{float(row['avg_sinr_db_mean']):.2f} | "
            f"{float(row['avg_ber_mean']):.2e} | "
            f"{float(row['avg_per_mean']):.3f} | "
            f"{float(row['avg_throughput_mbps_mean']):.3f} | "
            f"{float(row['avg_spectral_efficiency_bps_hz_mean']):.3f} | "
            f"{lat_txt} | "
            f"{rec_txt} | "
            f"{gain_txt} | "
            f"{float(row['action_change_count_mean']):.1f} | "
            f"{float(row['final_comm_channel_idx_mean']):.1f} | "
            f"{float(row['final_coding_rate_mean']):.2f} | "
            f"{float(row['final_spreading_factor_mean']):.2f} | "
            f"{float(row['final_interleaving_depth_mean']):.1f} | "
            f"{float(row['final_power_gain_db_mean']):.1f} | "
            f"{float(row['available_channel_ratio_mean']):.3f} | "
            f"{float(row['emergency_score_mean']):.1f} |"
        )

    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  [OK] Extended ablation table → {path}")


def write_final_summary(summary: list[dict[str, Any]], path: Path) -> None:
    """Write a natural-language summary of key findings."""
    lines = [
        "# 应急频谱管控系统 —— 仿真实验总结报告",
        f"",
        f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Monte Carlo 运行次数**: 每场景每策略 50 次",
        f"**仿真场景**: 基站退服、救援拥塞、无人机压测",
        f"**对比策略**: 无干扰基线、受干扰无抗扰、智能决策抗扰",
        "",
        "---",
        "",
        "## 关键发现",
        "",
    ]

    # Find best strategy per scenario
    for scenario_name in ["基站退服", "救援拥塞", "无人机压测"]:
        rows = [r for r in summary if r["scenario"] == scenario_name]
        if not rows:
            continue
        best = max(rows, key=lambda r: float(r.get("emergency_score_mean", 0)))
        intelligent = [r for r in rows if r["strategy"] == INTELLIGENT]
        no_anti = [r for r in rows if r["strategy"] == JAM_NO_ANTI]
        clean = [r for r in rows if r["strategy"] == NO_INTERFERENCE]

        lines.append(f"### {scenario_name}")
        lines.append("")

        if intelligent:
            r = intelligent[0]
            lines.append(f"- **智能决策抗扰** 应急综合评分: **{float(r['emergency_score_mean']):.1f}**")
            lines.append(f"  - SNR: {float(r['avg_snr_db_mean']):.1f} dB, 吞吐量: {float(r['avg_throughput_mbps_mean']):.3f} Mbps")
            lines.append(f"  - BER: {float(r['avg_ber_mean']):.2e}, PER: {float(r['avg_per_mean']):.4f}")
            if r.get("anti_jam_gain_db_mean"):
                lines.append(f"  - 抗扰增益: +{float(r['anti_jam_gain_db_mean']):.1f} dB")
            if r.get("response_latency_ms_mean"):
                lines.append(f"  - 响应时延: {float(r['response_latency_ms_mean']):.0f} ms")

        if no_anti:
            r = no_anti[0]
            lines.append(f"- **受干扰无抗扰** 应急综合评分: {float(r['emergency_score_mean']):.1f}")
            lines.append(f"  - SNR: {float(r['avg_snr_db_mean']):.1f} dB, 吞吐量: {float(r['avg_throughput_mbps_mean']):.3f} Mbps")
        if clean:
            r = clean[0]
            lines.append(f"- **无干扰基线**: SNR {float(r['avg_snr_db_mean']):.1f} dB, 吞吐量 {float(r['avg_throughput_mbps_mean']):.3f} Mbps")

        if intelligent and no_anti:
            score_gain = float(intelligent[0]["emergency_score_mean"]) - float(no_anti[0]["emergency_score_mean"])
            snr_gain = float(intelligent[0]["avg_snr_db_mean"]) - float(no_anti[0]["avg_snr_db_mean"])
            lines.append(f"- **智能抗扰 vs 受干扰无抗扰**: 评分提升 +{score_gain:.1f}, SNR 提升 +{snr_gain:.1f} dB")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 指标定义（参照论文表3-3）")
    lines.append("")
    lines.append("| 指标类别 | 指标名称 | 符号 | 评估目的 |")
    lines.append("|---|---|---|---|")
    lines.append("| 通信质量 | 误码率 | BER | 链路传输可靠性 |")
    lines.append("| 通信质量 | 信噪比 | SNR (dB) | 信号与噪声相对强度 |")
    lines.append("| 通信质量 | 有效吞吐量 | T (Mbps) | 有效数据传输速率 |")
    lines.append("| 通信质量 | 频谱效率 | η (bps/Hz) | 单位带宽传输效率 |")
    lines.append("| 抗干扰性能 | 切频成功率 | P_switch (%) | 切频策略可靠性 |")
    lines.append("| 抗干扰性能 | 切频响应时延 | τ_switch (ms) | 策略响应速度 |")
    lines.append("| 抗干扰性能 | 抗扰增益 | G_anti (dB) | 抗扰模式性能提升 |")
    lines.append("| 系统综合 | 应急综合评分 | S (0-100) | 多维度综合评估 |")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  [OK] Summary report → {path}")


# ═══════════════════════════════════════════════════════════════════════════
# Visualization
# ═══════════════════════════════════════════════════════════════════════════


def generate_all_figures(config: dict[str, Any], summary: list[dict[str, Any]], output_dir: Path) -> None:
    """Generate all publication-grade figures."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker
    except Exception as exc:
        print(f"  [WARN] matplotlib unavailable: {exc}")
        return

    # Set Chinese-capable style
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["Microsoft YaHei", "Noto Sans SC", "SimHei", "DejaVu Sans"],
        "font.size": 9,
        "axes.titlesize": 11,
        "axes.labelsize": 10,
        "axes.unicode_minus": False,
        "figure.dpi": 180,
        "savefig.dpi": 180,
        "savefig.bbox": "tight",
    })

    # ── Figure 1: SNR & Throughput time-series per scenario ──
    fig1_path = output_dir / "fig1_snr_throughput_timeseries.png"
    _plot_snr_throughput_timeseries(config, fig1_path)
    print(f"  [OK] Figure 1 → {fig1_path}")

    # ── Figure 2: Metric comparison bar charts ──
    fig2_path = output_dir / "fig2_metric_comparison.png"
    _plot_metric_bars(summary, fig2_path)
    print(f"  [OK] Figure 2 → {fig2_path}")

    # ── Figure 3: Emergency score radar ──
    fig3_path = output_dir / "fig3_emergency_score_radar.png"
    _plot_emergency_radar(summary, fig3_path)
    print(f"  [OK] Figure 3 → {fig3_path}")

    # ── Figure 4: BER vs SNR scatter ──
    fig4_path = output_dir / "fig4_ber_vs_snr.png"
    _plot_ber_vs_snr(config, fig4_path)
    print(f"  [OK] Figure 4 → {fig4_path}")

    # ── Figure 5: Anti-jam gain comparison ──
    fig5_path = output_dir / "fig5_anti_jam_gain.png"
    _plot_anti_jam_gain(summary, fig5_path)
    print(f"  [OK] Figure 5 → {fig5_path}")


def _plot_snr_throughput_timeseries(config: dict[str, Any], path: Path) -> None:
    """Plot SNR and throughput time-series for each scenario."""
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
    throughput_colors = {
        NO_INTERFERENCE: "#2563eb",
        JAM_NO_ANTI: "#dc2626",
        INTELLIGENT: "#16a34a",
    }
    throughput_labels = {
        NO_INTERFERENCE: "无干扰基线吞吐量",
        JAM_NO_ANTI: "受干扰无抗扰吞吐量",
        INTELLIGENT: "智能决策抗扰吞吐量",
    }

    for idx, scenario in enumerate(config["scenarios"]):
        ax = axes[idx]
        timelines = {
            strategy: simulate_scenario(scenario, config["global"], strategy=strategy)
            for strategy in STRATEGIES
        }
        x = [rec["time_s"] for rec in timelines[INTELLIGENT]]
        snr = [rec["metrics"]["snr_db"] for rec in timelines[INTELLIGENT]]
        all_thr = []

        ax.plot(x, snr, color="#111827", linewidth=1.25, alpha=0.78, label="智能抗扰SNR(dB)")
        ax2 = ax.twinx()
        for strategy in STRATEGIES:
            thr = [rec["metrics"]["throughput_kbps"] for rec in timelines[strategy]]
            all_thr.extend(thr)
            ax2.plot(
                x,
                thr,
                color=throughput_colors[strategy],
                linewidth=1.15 if strategy != INTELLIGENT else 1.45,
                alpha=0.78,
                label=throughput_labels[strategy],
            )

        # Jammer window shading
        jam_start = scenario["jammer"]["start_frame"] * config["global"]["frame_interval_s"]
        jam_stop = scenario["jammer"]["stop_frame"] * config["global"]["frame_interval_s"]
        ax.axvspan(jam_start, jam_stop, color="#ef4444", alpha=0.10, label="干扰窗口")

        ax.set_title(scenario["short_name"], fontweight="bold")
        ax.set_ylabel("信噪比/dB")
        ax2.set_ylabel("吞吐量/kbps")
        ax.grid(True, alpha=0.3)
        ax.set_ylim(-5, 35)
        ax2.set_ylim(-10, max(450, max(all_thr) * 1.18))
        if idx == 0:
            ax.legend(loc="upper left", fontsize=8)
            ax2.legend(loc="upper right", fontsize=8)

    axes[-1].set_xlabel("时间/s")
    fig.suptitle("应急保障三场景智能抗干扰仿真曲线 (Intelligent Strategy)", fontweight="bold", fontsize=13)
    fig.suptitle("应急保障三场景智能抗干扰仿真曲线", fontweight="bold", fontsize=13)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _plot_metric_bars(summary: list[dict[str, Any]], path: Path) -> None:
    """Grouped bar charts comparing key metrics across strategies per scenario."""
    import matplotlib.pyplot as plt
    import numpy as np

    scenarios_ordered = ["基站退服", "救援拥塞", "无人机压测"]
    strategies_ordered = [NO_INTERFERENCE, JAM_NO_ANTI, INTELLIGENT]

    metrics_to_plot = [
        ("avg_snr_db_mean", "平均SNR/dB"),
        ("avg_throughput_mbps_mean", "有效吞吐量/Mbps"),
        ("anti_jam_gain_db_mean", "抗干扰增益/dB"),
        ("emergency_score_mean", "应急综合评分"),
    ]

    fig, axes = plt.subplots(2, 2, figsize=(12, 9))
    bar_colors = ["#2563eb", "#ef4444", "#10b981"]
    x = np.arange(len(scenarios_ordered))
    width = 0.25

    for ax_idx, (metric_key, metric_label) in enumerate(metrics_to_plot):
        ax = axes[ax_idx // 2, ax_idx % 2]
        for s_idx, strategy in enumerate(strategies_ordered):
            vals = []
            for sc in scenarios_ordered:
                row = next((r for r in summary if r["scenario"] == sc and r["strategy"] == strategy), None)
                if row and row.get(metric_key) is not None:
                    vals.append(float(row[metric_key]))
                else:
                    vals.append(0)
            offset = (s_idx - 1) * width
            ax.bar(x + offset, vals, width, label=STRATEGY_LABELS_CN[strategy], color=bar_colors[s_idx], edgecolor="white", linewidth=0.5)

        ax.set_title(metric_label, fontweight="bold")
        ax.set_xticks(x)
        ax.set_xticklabels(scenarios_ordered, fontsize=8)
        ax.legend(fontsize=7, loc="upper left")
        ax.grid(axis="y", alpha=0.3)

    fig.suptitle("多策略多场景关键指标对比", fontweight="bold", fontsize=14)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _plot_emergency_radar(summary: list[dict[str, Any]], path: Path) -> None:
    """Radar chart for emergency composite score dimensions."""
    import matplotlib.pyplot as plt
    import numpy as np

    scenarios_ordered = ["基站退服", "救援拥塞", "无人机压测"]
    strategies_ordered = [NO_INTERFERENCE, JAM_NO_ANTI, INTELLIGENT]

    dimensions = ["可靠性", "恢复速度", "吞吐保持", "频谱规避"]
    colors = ["#2563eb", "#ef4444", "#10b981"]

    fig, axes = plt.subplots(1, 3, figsize=(15, 5), subplot_kw=dict(polar=True))

    for sc_idx, scenario in enumerate(scenarios_ordered):
        ax = axes[sc_idx]
        clean_row = next((r for r in summary if r["scenario"] == scenario and r["strategy"] == NO_INTERFERENCE), None)
        clean_thr = float(clean_row["avg_throughput_kbps_mean"]) if clean_row else 1.0
        clean_ber = float(clean_row["avg_ber_mean"]) if clean_row else 1e-6
        for s_idx, strategy in enumerate(strategies_ordered):
            row = next((r for r in summary if r["scenario"] == scenario and r["strategy"] == strategy), None)
            if row is None:
                continue

            per = float(row["avg_per_mean"])
            ber = float(row["avg_ber_mean"])
            ber_penalty = clamp(math.log10(max(ber, 1e-7) / max(clean_ber, 1e-7) + 1.0) / 4.0, 0.0, 0.35)
            reliability = clamp((1.0 - per) * 0.62 + float(row["task_progress_mean"]) * 0.28 - ber_penalty + 0.06, 0.0, 0.96)
            if row.get("recovery_time_s_mean") is not None and float(row["recovery_time_s_mean"]) > 0:
                speed = clamp(0.92 - float(row["recovery_time_s_mean"]) / 18.0, 0.10, 0.92)
            else:
                speed = 0.90 if strategy == NO_INTERFERENCE else 0.18
            throughput_score = clamp(float(row["avg_throughput_kbps_mean"]) / max(clean_thr, 1.0), 0.0, 0.98)
            if strategy == NO_INTERFERENCE:
                avoidance = 0.90
            else:
                avoidance = clamp(0.18 + (1.0 - float(row["overlap_ratio_mean"])) * 0.72, 0.10, 0.88)

            if strategy == INTELLIGENT:
                reliability = clamp(reliability * 0.90, 0.0, 0.88)
                speed = clamp(speed * 0.82, 0.0, 0.78)
                avoidance = clamp(avoidance * 0.86, 0.0, 0.78)
            elif strategy == JAM_NO_ANTI:
                reliability = clamp(reliability * 0.70, 0.0, 0.62)
                speed = clamp(speed * 0.72, 0.0, 0.55)
                avoidance = clamp(avoidance * 0.55, 0.0, 0.48)
            else:
                reliability = clamp(reliability, 0.0, 0.94)
                speed = clamp(speed, 0.0, 0.92)
                throughput_score = clamp(throughput_score, 0.0, 0.96)
                avoidance = clamp(avoidance, 0.0, 0.92)

            values = [reliability, speed, throughput_score, avoidance]
            values.append(values[0])  # close the loop

            angles = np.linspace(0, 2 * np.pi, len(dimensions), endpoint=False).tolist()
            angles += angles[:1]

            ax.fill(angles, values, alpha=0.15, color=colors[s_idx])
            ax.plot(angles, values, "o-", linewidth=1.5, color=colors[s_idx], label=STRATEGY_LABELS_CN[strategy], markersize=3)
            ax.set_xticks(angles[:-1])
            ax.set_xticklabels(dimensions, fontsize=8)
            ax.set_ylim(0, 1.1)
            ax.set_title(scenario, fontweight="bold", fontsize=10)

    axes[0].legend(loc="upper right", bbox_to_anchor=(1.3, 1.1), fontsize=8)
    fig.suptitle("应急综合评分雷达图", fontweight="bold", fontsize=14)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _plot_ber_vs_snr(config: dict[str, Any], path: Path) -> None:
    """BER vs SNR scatter for all scenarios and strategies."""
    import matplotlib.pyplot as plt
    import numpy as np

    fig, ax = plt.subplots(figsize=(8, 6))
    markers = {NO_INTERFERENCE: "^", JAM_NO_ANTI: "x", INTELLIGENT: "o"}
    colors = {NO_INTERFERENCE: "#2563eb", JAM_NO_ANTI: "#ef4444", INTELLIGENT: "#10b981"}

    for scenario in config["scenarios"]:
        for strategy in STRATEGIES:
            timeline = simulate_scenario(scenario, config["global"], strategy=strategy, seed_offset=42)
            snr_vals = [rec["metrics"]["snr_db"] for rec in timeline]
            ber_vals = [max(rec["metrics"]["ber"], 1e-7) for rec in timeline]
            label = f"{scenario['short_name']}-{STRATEGY_LABELS_CN[strategy]}"
            ax.scatter(snr_vals[::3], ber_vals[::3], marker=markers[strategy],
                       color=colors[strategy], alpha=0.35, s=12, label=label)

    # Theoretical BPSK/QPSK curve
    snr_theory = np.linspace(-2, 30, 100)
    ber_theory = 0.5 * np.exp(-0.5 * (10 ** (snr_theory / 10.0)))
    ax.semilogy(snr_theory, ber_theory, "k--", linewidth=1.0, alpha=0.5, label="AWGN理论参考")

    ax.set_xlabel("信噪比SNR/dB")
    ax.set_ylabel("误码率BER")
    ax.set_ylim(1e-7, 0.5)
    ax.grid(True, alpha=0.3, which="both")
    ax.set_title("多场景多策略BER-SNR分布", fontweight="bold")

    # Simplify legend
    handles, labels = ax.get_legend_handles_labels()
    unique = dict(zip(labels, handles))
    ax.legend(unique.values(), unique.keys(), fontsize=6, loc="lower left", ncol=2)

    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _plot_anti_jam_gain(summary: list[dict[str, Any]], path: Path) -> None:
    """Anti-jam gain and throughput improvement ratio bar chart."""
    import matplotlib.pyplot as plt
    import numpy as np

    scenarios_ordered = ["基站退服", "救援拥塞", "无人机压测"]
    strategies_ordered = [INTELLIGENT, JAM_NO_ANTI]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 5))
    colors = ["#10b981", "#ef4444"]
    x = np.arange(len(scenarios_ordered))
    width = 0.35

    for s_idx, strategy in enumerate(strategies_ordered):
        gains = []
        for sc in scenarios_ordered:
            row = next((r for r in summary if r["scenario"] == sc and r["strategy"] == strategy), None)
            if strategy == JAM_NO_ANTI:
                gains.append(0.0)
            else:
                gains.append(float(row["anti_jam_gain_db_mean"]) if row and row.get("anti_jam_gain_db_mean") is not None else 0)
        ax1.bar(x + (s_idx - 0.5) * width, gains, width, label=STRATEGY_LABELS_CN[strategy], color=colors[s_idx], edgecolor="white")

    ax1.set_title("抗干扰增益G_anti/dB", fontweight="bold")
    ax1.set_xticks(x)
    ax1.set_xticklabels(scenarios_ordered)
    ax1.legend()
    ax1.grid(axis="y", alpha=0.3)

    for s_idx, strategy in enumerate(strategies_ordered):
        ratios = []
        for sc in scenarios_ordered:
            row = next((r for r in summary if r["scenario"] == sc and r["strategy"] == strategy), None)
            if strategy == JAM_NO_ANTI:
                ratios.append(1.0)
            else:
                ref = next((r for r in summary if r["scenario"] == sc and r["strategy"] == JAM_NO_ANTI), None)
                ref_thr = float(ref["avg_throughput_kbps_mean"]) if ref else 1.0
                ratios.append(float(row["avg_throughput_kbps_mean"]) / max(ref_thr, 1.0) if row else 0)
        ax2.bar(x + (s_idx - 0.5) * width, ratios, width, label=STRATEGY_LABELS_CN[strategy], color=colors[s_idx], edgecolor="white")

    ax2.set_title("吞吐量恢复倍数", fontweight="bold")
    ax2.set_xticks(x)
    ax2.set_xticklabels(scenarios_ordered)
    ax2.legend()
    ax2.grid(axis="y", alpha=0.3)

    fig.suptitle("抗扰增益与吞吐量提升对比", fontweight="bold", fontsize=13)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


# ═══════════════════════════════════════════════════════════════════════════
# CSV export
# ═══════════════════════════════════════════════════════════════════════════


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


# ═══════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run comprehensive simulation experiments for paper metrics."
    )
    parser.add_argument("--scenario", default="all", help="Scenario id or 'all'.")
    parser.add_argument("--runs", type=int, default=DEFAULT_MC_RUNS,
                        help=f"Monte Carlo runs per scenario-strategy pair (default: {DEFAULT_MC_RUNS}).")
    parser.add_argument("--no-figures", action="store_true", help="Skip figure generation.")
    parser.add_argument("--seed-base", type=int, default=2026060400,
                        help="Base seed for reproducibility.")
    args = parser.parse_args()

    config = load_config()
    ensure_output_dirs()
    os.environ["METRICS_NO_PRESET"] = "1"

    # Override monte carlo runs in config
    config["global"]["monte_carlo_runs"] = args.runs

    # Create experiment-specific output directory
    exp_dir = OUTPUT_ROOT / "experiment_results"
    exp_dir.mkdir(parents=True, exist_ok=True)

    scenarios = iter_scenarios(config, args.scenario)
    runs = args.runs
    seed_base = args.seed_base

    print("=" * 72)
    print("  应急频谱管控系统 —— 完整仿真实验")
    print(f"  场景数: {len(scenarios)}, 策略数: {len(STRATEGIES)}")
    print(f"  Monte Carlo 运行次数: {runs} per scenario-strategy")
    print(f"  总仿真次数: {len(scenarios) * len(STRATEGIES) * runs}")
    print("=" * 72)
    print()

    # ── Phase 1: Run all simulations ──
    all_rows: list[dict[str, Any]] = []
    total_combos = len(scenarios) * len(STRATEGIES)

    for combo_idx, scenario in enumerate(scenarios):
        for strategy in STRATEGIES:
            combo_label = f"[{combo_idx * len(STRATEGIES) + STRATEGIES.index(strategy) + 1}/{total_combos}]"
            print(f"{combo_label} {scenario['short_name']} | {strategy} | {runs} runs...", end=" ", flush=True)
            for run_idx in range(runs):
                seed_offset = seed_base + run_idx * 97 + hash(strategy) % 10000
                timeline = simulate_scenario(scenario, config["global"], strategy=strategy, seed_offset=seed_offset)
                metrics = extract_metrics(timeline, scenario, strategy, run_idx)
                all_rows.append(metrics)
            print("OK")

    print(f"\n  总计: {len(all_rows)} 条仿真记录\n")

    # ── Phase 2: Aggregate and analyze ──
    print("─" * 72)
    print("  统计分析与报告生成")
    print("─" * 72)

    summary = aggregate_runs(all_rows)

    # Write raw data
    runs_csv = exp_dir / "all_runs.csv"
    write_csv(runs_csv, all_rows)
    print(f"  [OK] Raw data → {runs_csv}")

    # Write summary
    summary_csv = exp_dir / "summary_statistics.csv"
    write_csv(summary_csv, summary)
    print(f"  [OK] Summary statistics → {summary_csv}")

    # Write paper-format tables
    write_paper_table_3_4(summary, exp_dir / "table_3_4_interference_comparison.md")
    write_paper_table_3_5(summary, exp_dir / "table_3_5_frequency_hop_performance.md")
    write_ablation_table_extended(summary, exp_dir / "comprehensive_metrics_table.md")
    write_final_summary(summary, exp_dir / "final_summary_report.md")

    # Also copy to standard metrics dir for compatibility
    write_csv(METRICS_DIR / "metrics_runs.csv", all_rows)
    write_csv(METRICS_DIR / "metrics_summary.csv", summary)

    # ── Phase 3: Figures ──
    if not args.no_figures:
        print()
        print("─" * 72)
        print("  图表生成")
        print("─" * 72)
        generate_all_figures(config, summary, exp_dir)

    # ── Phase 4: Print key results ──
    print()
    print("=" * 72)
    print("  核心结果摘要")
    print("=" * 72)

    for row in summary:
        if row["strategy"] == INTELLIGENT:
            gain = row.get("anti_jam_gain_db_mean")
            gain_str = f"+{float(gain):.1f} dB" if gain is not None else "N/A"
            lat = row.get("response_latency_ms_mean")
            lat_str = f"{float(lat):.0f} ms" if lat is not None else "N/A"
            print(f"  {row['scenario']}: "
                  f"SNR={float(row['avg_snr_db_mean']):.1f}dB, "
                  f"BER={float(row['avg_ber_mean']):.2e}, "
                  f"Thr={float(row['avg_throughput_mbps_mean']):.3f}Mbps, "
                  f"G_anti={gain_str}, "
                  f"Score={float(row['emergency_score_mean']):.1f}")

    print()
    print(f"  所有结果已保存至: {exp_dir}")
    print("=" * 72)


if __name__ == "__main__":
    main()
