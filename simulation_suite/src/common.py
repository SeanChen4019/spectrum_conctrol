from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
CONFIG_PATH = ROOT / "configs" / "scenarios.yaml"
OUTPUT_ROOT = ROOT / "outputs"
TIMELINE_DIR = OUTPUT_ROOT / "timelines"
METRICS_DIR = OUTPUT_ROOT / "metrics"


def ensure_output_dirs() -> None:
    TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
    METRICS_DIR.mkdir(parents=True, exist_ok=True)


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load the scenario config.

    The file uses JSON syntax inside a .yaml file so it remains YAML-compatible
    while avoiding a hard dependency on PyYAML on demo machines.
    """
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def scenario_by_id(config: dict[str, Any], scenario_id: str) -> dict[str, Any]:
    for scenario in config["scenarios"]:
        if scenario["id"] == scenario_id:
            return scenario
    raise KeyError(f"Unknown scenario: {scenario_id}")


def iter_scenarios(config: dict[str, Any], scenario: str) -> list[dict[str, Any]]:
    if scenario == "all":
        return list(config["scenarios"])
    return [scenario_by_id(config, scenario)]


def channel_centers_hz(global_cfg: dict[str, Any]) -> list[float]:
    n = int(global_cfg["num_channels"])
    width = float(global_cfg["channel_width_hz"])
    center = float(global_cfg["center_freq_hz"])
    left = center - width * (n - 1) / 2
    return [left + k * width for k in range(n)]


def affected_channels(channel_idx: int, bw_mhz: float, global_cfg: dict[str, Any]) -> list[int]:
    centers = channel_centers_hz(global_cfg)
    width = float(global_cfg["channel_width_hz"])
    center_hz = centers[max(0, min(len(centers) - 1, channel_idx))]
    half_bw_hz = bw_mhz * 1e6 / 2
    left = center_hz - half_bw_hz
    right = center_hz + half_bw_hz
    affected: list[int] = []
    for idx, ch_center in enumerate(centers):
        ch_left = ch_center - width / 2
        ch_right = ch_center + width / 2
        if ch_right > left and ch_left < right:
            affected.append(idx)
    return affected or [channel_idx]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def qfunc_like(x: float) -> float:
    """Cheap monotonic BER surrogate for paper-level comparative simulation."""
    return 0.5 * math.erfc(x / math.sqrt(2.0))


def ber_from_snr(snr_db: float, mode: str) -> float:
    coding_gain = {
        "normal": 0.0,
        "low_rate": 4.5,
        "frequency_hop": 2.0,
        "power_boost": 1.2,
    }.get(mode, 0.0)
    effective = max(-4.0, snr_db + coding_gain)
    return clamp(qfunc_like(math.sqrt(2 * 10 ** (effective / 10.0))), 1e-6, 0.48)


def per_from_ber(ber: float, packet_bits: int = 4096) -> float:
    return clamp(1.0 - (1.0 - ber) ** packet_bits, 0.0, 1.0)


def safe_mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
