from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from common import METRICS_DIR, ensure_output_dirs, iter_scenarios, load_config, safe_mean
from generate_jammer_data import generate_scenario_timeline


STRATEGIES = ["无抗干扰", "固定低速抗扰", "智能决策策略"]


def summarize_timeline(timeline: list[dict[str, Any]], scenario: dict[str, Any], strategy: str, run_idx: int) -> dict[str, Any]:
    metrics = [rec["metrics"] for rec in timeline]
    decisions = [rec["decision"] for rec in timeline]
    interval = timeline[1]["time_s"] - timeline[0]["time_s"] if len(timeline) > 1 else 0.12
    jam_frames = [rec for rec in timeline if rec["jam_active"]]
    post_response = [rec for rec in timeline if rec["decision"]["first_response_frame"] is not None]
    first_response_frame = decisions[-1]["first_response_frame"]
    recovered_frame = decisions[-1]["recovered_frame"]
    start_frame = int(scenario["jammer"]["start_frame"])
    response_latency = None if first_response_frame is None else max(0.0, (first_response_frame - start_frame) * interval)
    recovery_time = None if recovered_frame is None else max(0.0, (recovered_frame - start_frame) * interval)
    overlap_frames = sum(1 for m in metrics if m["overlap"])
    jam_count = max(1, len(jam_frames))
    task_completed = any(m["task_completed"] for m in metrics)
    completion_frame = next((rec["frame"] for rec in timeline if rec["metrics"]["task_completed"]), None)
    completion_time = "" if completion_frame is None else round(completion_frame * interval, 3)
    avg_snr = safe_mean([m["snr_db"] for m in metrics])
    min_snr = min(m["snr_db"] for m in metrics)
    avg_ber = safe_mean([m["ber"] for m in metrics])
    avg_per = safe_mean([m["per"] for m in metrics])
    avg_thr = safe_mean([m["throughput_kbps"] for m in metrics])
    task_progress = metrics[-1]["task_progress"]
    available_ratio = safe_mean([m["available_channel_ratio"] for m in metrics])
    decision_count = decisions[-1]["decision_count"]
    hop_success = any(d["anti_jamming_mode"] == 2 for d in decisions) and overlap_frames / jam_count < 0.55
    reliability = (1.0 - avg_per) * 0.55 + task_progress * 0.45
    if recovery_time is None:
        speed = 0.0 if overlap_frames > 0 else 1.0
    else:
        speed = max(0.0, 1.0 - recovery_time / 20.0)
    throughput_score = min(1.0, avg_thr / 240.0)
    avoidance = 1.0 - overlap_frames / len(timeline)
    emergency_score = 100.0 * (0.40 * reliability + 0.25 * speed + 0.20 * throughput_score + 0.15 * avoidance)

    return {
        "scenario_id": scenario["id"],
        "scenario": scenario["short_name"],
        "strategy": strategy,
        "run": run_idx,
        "avg_snr_db": round(avg_snr, 4),
        "min_snr_db": round(min_snr, 4),
        "avg_ber": avg_ber,
        "avg_per": avg_per,
        "avg_throughput_kbps": round(avg_thr, 4),
        "task_completion_rate": 1.0 if task_completed else task_progress,
        "task_completed": int(task_completed),
        "completion_time_s": completion_time,
        "first_response_latency_s": "" if response_latency is None else round(response_latency, 4),
        "recovery_time_s": "" if recovery_time is None else round(recovery_time, 4),
        "decision_count": decision_count,
        "frequency_hop_success": int(hop_success),
        "overlap_ratio": round(overlap_frames / len(timeline), 4),
        "post_jam_overlap_ratio": round(overlap_frames / jam_count, 4),
        "available_channel_ratio": round(available_ratio, 4),
        "emergency_score": round(emergency_score, 4),
    }


def aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["scenario"], row["strategy"])].append(row)

    summary = []
    numeric_cols = [
        "avg_snr_db",
        "min_snr_db",
        "avg_ber",
        "avg_per",
        "avg_throughput_kbps",
        "task_completion_rate",
        "task_completed",
        "decision_count",
        "frequency_hop_success",
        "overlap_ratio",
        "post_jam_overlap_ratio",
        "available_channel_ratio",
        "emergency_score",
    ]
    optional_numeric_cols = ["first_response_latency_s", "recovery_time_s"]

    for (scenario, strategy), items in sorted(groups.items()):
        out = {"scenario": scenario, "strategy": strategy, "runs": len(items)}
        for col in numeric_cols:
            vals = [float(item[col]) for item in items]
            mean = safe_mean(vals)
            std = _std(vals)
            ci95 = 1.96 * std / math.sqrt(len(vals)) if vals else 0.0
            out[f"{col}_mean"] = mean
            out[f"{col}_std"] = std
            out[f"{col}_ci95"] = ci95
        for col in optional_numeric_cols:
            vals = [float(item[col]) for item in items if item[col] != ""]
            out[f"{col}_mean"] = safe_mean(vals) if vals else ""
            out[f"{col}_std"] = _std(vals) if vals else ""
            out[f"{col}_ci95"] = 1.96 * _std(vals) / math.sqrt(len(vals)) if vals else ""
        summary.append(out)
    return summary


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_ablation_table(path: Path, summary: list[dict[str, Any]]) -> None:
    lines = [
        "| 场景 | 策略 | SNR均值(dB) | BER均值 | PER均值 | 吞吐(kbps) | 完成率 | 恢复时间(s) | 重叠率 | 应急综合评分 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary:
        recovery = row["recovery_time_s_mean"]
        recovery_txt = "--" if recovery == "" else f"{float(recovery):.2f}"
        lines.append(
            "| {scenario} | {strategy} | {snr:.2f} | {ber:.2e} | {per:.3f} | {thr:.1f} | {done:.2f} | {rec} | {overlap:.3f} | {score:.1f} |".format(
                scenario=row["scenario"],
                strategy=row["strategy"],
                snr=float(row["avg_snr_db_mean"]),
                ber=float(row["avg_ber_mean"]),
                per=float(row["avg_per_mean"]),
                thr=float(row["avg_throughput_kbps_mean"]),
                done=float(row["task_completion_rate_mean"]),
                rec=recovery_txt,
                overlap=float(row["overlap_ratio_mean"]),
                score=float(row["emergency_score_mean"]),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_curves(path: Path, config: dict[str, Any]) -> None:
    try:
        import matplotlib.pyplot as plt
    except Exception as exc:
        _write_curves_ppm_png_fallback(path, config, f"matplotlib unavailable: {exc}")
        return

    fig, axes = plt.subplots(3, 1, figsize=(9, 8), sharex=True)
    for ax, scenario in zip(axes, config["scenarios"]):
        timeline = generate_scenario_timeline(scenario, config["global"], strategy="智能决策策略")
        x = [rec["time_s"] for rec in timeline]
        snr = [rec["metrics"]["snr_db"] for rec in timeline]
        thr = [rec["metrics"]["throughput_kbps"] for rec in timeline]
        ax.plot(x, snr, label="SNR(dB)", color="#2563eb", linewidth=1.7)
        ax2 = ax.twinx()
        ax2.plot(x, thr, label="Throughput(kbps)", color="#16a34a", linewidth=1.2, alpha=0.85)
        ax.axvspan(
            scenario["jammer"]["start_frame"] * config["global"]["frame_interval_s"],
            scenario["jammer"]["stop_frame"] * config["global"]["frame_interval_s"],
            color="#ef4444",
            alpha=0.12,
            label="干扰窗口",
        )
        ax.set_title(scenario["short_name"])
        ax.set_ylabel("SNR(dB)")
        ax2.set_ylabel("kbps")
        ax.grid(True, alpha=0.25)
    axes[-1].set_xlabel("Time(s)")
    fig.suptitle("应急保障三场景智能抗干扰仿真曲线")
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def _write_curves_ppm_png_fallback(path: Path, config: dict[str, Any], reason: str) -> None:
    """Write a simple PNG chart without third-party dependencies."""
    import binascii
    import struct
    import zlib

    width, height = 1100, 760
    rows = len(config["scenarios"])
    margin_l, margin_r, margin_t, margin_b = 70, 40, 48, 45
    plot_h = (height - margin_t - margin_b) // rows
    img = bytearray([255, 255, 255] * width * height)

    def px(x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < width and 0 <= y < height:
            idx = (y * width + x) * 3
            img[idx:idx + 3] = bytes(color)

    def line(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            for ox in (0, 1):
                for oy in (0, 1):
                    px(x0 + ox, y0 + oy, color)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    for row, scenario in enumerate(config["scenarios"]):
        top = margin_t + row * plot_h
        bottom = top + plot_h - 25
        left = margin_l
        right = width - margin_r
        timeline = generate_scenario_timeline(scenario, config["global"], strategy="智能决策策略")
        max_t = max(rec["time_s"] for rec in timeline)
        snr_vals = [rec["metrics"]["snr_db"] for rec in timeline]
        thr_vals = [rec["metrics"]["throughput_kbps"] for rec in timeline]
        max_thr = max(max(thr_vals), 1.0)

        for x in range(left, right):
            px(x, bottom, (180, 180, 180))
        for y in range(top, bottom):
            px(left, y, (180, 180, 180))

        jam_l = left + int((scenario["jammer"]["start_frame"] * config["global"]["frame_interval_s"]) / max_t * (right - left))
        jam_r = left + int((scenario["jammer"]["stop_frame"] * config["global"]["frame_interval_s"]) / max_t * (right - left))
        for x in range(jam_l, min(jam_r, right)):
            for y in range(top, bottom):
                if (x + y) % 7 == 0:
                    px(x, y, (255, 225, 225))

        prev_snr = prev_thr = None
        for rec, snr, thr in zip(timeline, snr_vals, thr_vals):
            x = left + int(rec["time_s"] / max_t * (right - left))
            y_snr = bottom - int(max(0, min(32, snr)) / 32 * (bottom - top))
            y_thr = bottom - int(thr / max_thr * (bottom - top))
            if prev_snr:
                line(prev_snr[0], prev_snr[1], x, y_snr, (37, 99, 235))
            if prev_thr:
                line(prev_thr[0], prev_thr[1], x, y_thr, (22, 163, 74))
            prev_snr = (x, y_snr)
            prev_thr = (x, y_thr)

    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw.extend(img[y * stride:(y + 1) * stride])

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    path.with_suffix(".txt").write_text(
        f"{reason}\nGenerated dependency-free fallback PNG: {path.name}\n"
        "Blue=SNR, green=throughput, pale red=interference window.\n",
        encoding="utf-8",
    )


def _std(vals: list[float]) -> float:
    if len(vals) <= 1:
        return 0.0
    mean = safe_mean(vals)
    return math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run paper metrics for the emergency spectrum-control simulation.")
    parser.add_argument("--scenario", default="all", help="Scenario id or 'all'.")
    parser.add_argument("--runs", type=int, default=None, help="Monte Carlo runs per scenario and strategy.")
    args = parser.parse_args()

    config = load_config()
    ensure_output_dirs()
    runs = args.runs or int(config["global"]["monte_carlo_runs"])
    scenarios = iter_scenarios(config, args.scenario)
    rows: list[dict[str, Any]] = []

    for scenario in scenarios:
        for strategy in STRATEGIES:
            for run_idx in range(runs):
                timeline = generate_scenario_timeline(
                    scenario,
                    config["global"],
                    strategy=strategy,
                    seed_offset=run_idx * 97,
                )
                rows.append(summarize_timeline(timeline, scenario, strategy, run_idx))
            print(f"[METRICS] {scenario['short_name']} | {strategy} | runs={runs}")

    summary = aggregate(rows)
    write_csv(METRICS_DIR / "metrics_runs.csv", rows)
    write_csv(METRICS_DIR / "metrics_summary.csv", summary)
    write_ablation_table(METRICS_DIR / "ablation_table.md", summary)
    write_curves(METRICS_DIR / "scenario_curves.png", config)
    print(f"[OK] metrics written to {METRICS_DIR}")


if __name__ == "__main__":
    main()
