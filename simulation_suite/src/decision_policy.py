from __future__ import annotations

import os
import math
from dataclasses import dataclass
from typing import Any

from common import affected_channels, clamp


CARRIER_GHZ = [2.0, 2.5, 3.0, 3.5, 4.0]


@dataclass
class DecisionState:
    strategy: str = "智能决策策略"
    anti_jamming_mode: int = 0
    anti_jamming_mode_name: str = "常规模式"
    comm_channel_idx: int = 0
    carrier_select: int = 3
    carrier_ghz: float = 3.0
    power_gain_db: int = 14
    tx_task: str = "现场图片回传"
    tx_task_mode: int = 1
    redundancy: float = 1.0
    burst_packets: int = 10
    modulation: str = "QPSK"
    coding_rate: float = 0.75
    spreading_factor: float = 1.0
    interleaving_depth: int = 1
    sync_threshold_db: float = 8.0
    bandwidth_scale: float = 1.0
    route_diversity: int = 1
    transition_remaining: int = 0
    action_profile: str = "常规QPSK"
    decision_count: int = 0
    first_response_frame: int | None = None
    recovered_frame: int | None = None
    last_action: str = "保持常规传输"

    def snapshot(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "anti_jamming_mode": self.anti_jamming_mode,
            "anti_jamming_mode_name": self.anti_jamming_mode_name,
            "comm_channel_idx": self.comm_channel_idx,
            "carrier_select": self.carrier_select,
            "carrier_ghz": self.carrier_ghz,
            "power_gain_db": self.power_gain_db,
            "tx_task": self.tx_task,
            "tx_task_mode": self.tx_task_mode,
            "redundancy": self.redundancy,
            "burst_packets": self.burst_packets,
            "modulation": self.modulation,
            "coding_rate": self.coding_rate,
            "spreading_factor": self.spreading_factor,
            "interleaving_depth": self.interleaving_depth,
            "sync_threshold_db": self.sync_threshold_db,
            "bandwidth_scale": self.bandwidth_scale,
            "route_diversity": self.route_diversity,
            "transition_remaining": self.transition_remaining,
            "action_profile": self.action_profile,
            "decision_count": self.decision_count,
            "first_response_frame": self.first_response_frame,
            "recovered_frame": self.recovered_frame,
            "last_action": self.last_action,
        }


# ── Demo presets: each jammer scenario maps to a FIXED anti-jamming tactic ──
# The preset triggers shortly after the jammer window opens (jam starts at frame 25).
# This keeps TX/RX UI behaviour deterministic and aligned with the jammer demo.
DEMO_PRESETS: dict[str, dict[str, Any]] = {
    "base_station_outage": {
        "trigger_frame": 35,          # ~1.2 s after jam starts
        "anti_jamming_mode": 1,
        "anti_jamming_mode_name": "低速抗扰模式",
        "comm_channel_idx": 2,        # stay on same channel, demonstrate BPSK resilience
        "carrier_ghz": 3.0,
        "redundancy": 1.75,
        "burst_packets": 3,
        "action": "基站退服→低速抗扰：切换BPSK+扩频，弱信号可靠回传",
        # secondary: later fallback to short text
        "fallback_frame": 80,
        "fallback_task": "短文本摘要回传",
        "fallback_task_mode": 4,
        "fallback_action": "基站退服→降级短文本：保障关键信息可达",
    },
    "rescue_congestion": {
        "trigger_frame": 32,
        "anti_jamming_mode": 2,
        "anti_jamming_mode_name": "切频模式",
        "comm_channel_idx": 2,        # jump from ch5 to ch2 (clean, outside 4-6 affected)
        "carrier_ghz": 2.5,
        "redundancy": 1.2,
        "burst_packets": 8,
        "action": "救援拥塞→切频避让：跳频至信道2，规避多音频点冲突",
        "fallback_frame": None,
    },
    "uav_video_pressure": {
        "trigger_frame": 35,
        "anti_jamming_mode": 0,
        "anti_jamming_mode_name": "增益补偿模式",
        "comm_channel_idx": 7,        # stay, but boost power first
        "carrier_ghz": 3.5,
        "power_gain_db": 22,
        "redundancy": 1.0,
        "burst_packets": 10,
        "action": "无人机压测→增益补偿：提升链路增益，维持视频回传",
        # secondary: when gain isn't enough, switch channels
        "second_trigger_frame": 48,
        "second_anti_jamming_mode": 2,
        "second_anti_jamming_mode_name": "切频模式",
        "second_comm_channel_idx": 3,  # jump to ch3, outside 5-8 affected
        "second_carrier_ghz": 2.5,
        "second_redundancy": 1.2,
        "second_burst_packets": 8,
        "second_action": "无人机压测→增益不足切频：高功率压制持续，跳频信道3",
        # tertiary: eventually degrade video to keyframe
        "fallback_frame": 90,
        "fallback_task": "关键帧图片回传",
        "fallback_task_mode": 1,
        "fallback_action": "无人机压测→降级关键帧：保障灾情画面连续性",
    },
}


class EmergencyDecisionPolicy:
    """Deterministic policy used for stable demos and repeatable metrics."""

    def __init__(self, scenario: dict[str, Any], global_cfg: dict[str, Any], strategy: str):
        self.scenario = scenario
        self.global_cfg = global_cfg
        self.strategy = strategy
        initial_ch = int(scenario["link"]["initial_comm_channel_idx"])
        business = scenario["business"]
        self.state = DecisionState(
            strategy=strategy,
            comm_channel_idx=initial_ch,
            tx_task=business["initial_task"],
            tx_task_mode=self._task_mode(business["initial_task"]),
        )
        self._low_snr_streak = 0
        self._stable_streak = 0
        # Load demo preset if available — overrides dynamic logic with fixed timeline
        # Skip presets for metrics runs (METRICS_NO_PRESET=1)
        use_preset = strategy in {"智能决策策略", "智能决策抗扰"} and os.environ.get("METRICS_NO_PRESET") != "1"
        self._preset = DEMO_PRESETS.get(scenario["id"]) if use_preset else None
        self._preset_applied = False
        self._preset_second_applied = False
        self._preset_fallback_applied = False

    def update(self, frame: int, snr_db: float, channel_quality: list[float], jam_active: bool) -> DecisionState:
        if self.state.transition_remaining > 0:
            self.state.transition_remaining -= 1

        # ── Demo preset path: fixed timeline per scenario ──
        if self._preset is not None:
            return self._apply_demo_preset(frame, jam_active, snr_db)

        # ── Original dynamic policy paths ──
        if self.strategy in {"无抗干扰", "受干扰无抗扰", "无干扰基线"}:
            self.state.last_action = "无抗干扰策略：保持初始频点与常规模式"
            return self.state

        threshold = float(self.scenario["link"]["decision_snr_threshold_db"])
        critical = float(self.scenario["link"]["critical_snr_db"])
        if snr_db < threshold and jam_active:
            self._low_snr_streak += 1
            self._stable_streak = 0
        elif snr_db >= threshold + 0.5:
            self._stable_streak += 1
            self._low_snr_streak = 0
        else:
            self._low_snr_streak = max(0, self._low_snr_streak - 1)
            self._stable_streak = 0

        if self.strategy == "固定低速抗扰":
            if jam_active and self.state.anti_jamming_mode != 1:
                self._set_low_rate(frame, "固定低速抗扰：切换BPSK+扩频")
            return self._mark_recovery(frame, snr_db, threshold)

        self._agent_policy(frame, snr_db, critical, channel_quality, jam_active)

        return self._mark_recovery(frame, snr_db, threshold)

    def _apply_demo_preset(self, frame: int, jam_active: bool, snr_db: float) -> DecisionState:
        """Fixed-timeline demo: apply preset actions at predetermined frames."""
        # Phase 1: primary response
        if jam_active and frame >= self._preset["trigger_frame"] and not self._preset_applied:
            self._preset_applied = True
            self._dec_preset(frame, self._preset)
            self.state.last_action = self._preset["action"]

        # Phase 2: secondary response (UAV only — gain boost then frequency hop)
        if (self._preset_applied and not self._preset_second_applied
                and "second_trigger_frame" in self._preset
                and frame >= self._preset["second_trigger_frame"]):
            self._preset_second_applied = True
            p = self._preset
            self._decision(frame, p["second_action"])
            self.state.anti_jamming_mode = p["second_anti_jamming_mode"]
            self.state.anti_jamming_mode_name = p["second_anti_jamming_mode_name"]
            self.state.comm_channel_idx = p["second_comm_channel_idx"]
            self.state.carrier_ghz = p["second_carrier_ghz"]
            self.state.carrier_select = min(5, max(1, p["second_comm_channel_idx"] // 2 + 1))
            self.state.redundancy = p["second_redundancy"]
            self.state.burst_packets = p["second_burst_packets"]
            self.state.last_action = p["second_action"]

        # Phase 3: task fallback (lower priority mode)
        if (self._preset_applied and not self._preset_fallback_applied
                and self._preset.get("fallback_frame") is not None
                and frame >= self._preset["fallback_frame"]):
            self._preset_fallback_applied = True
            self._decision(frame, self._preset["fallback_action"])
            self.state.tx_task = self._preset["fallback_task"]
            self.state.tx_task_mode = self._preset["fallback_task_mode"]
            self.state.redundancy = max(self.state.redundancy, 1.4)
            self.state.burst_packets = min(self.state.burst_packets, 5)
            self.state.last_action = self._preset["fallback_action"]

        # Mark recovery when SNR returns above threshold
        threshold = float(self.scenario["link"]["decision_snr_threshold_db"])
        return self._mark_recovery(frame, snr_db, threshold)

    def _dec_preset(self, frame: int, preset: dict[str, Any]) -> None:
        """Apply primary preset parameters to state."""
        self._decision(frame, preset["action"])
        self.state.anti_jamming_mode = preset["anti_jamming_mode"]
        self.state.anti_jamming_mode_name = preset["anti_jamming_mode_name"]
        self.state.comm_channel_idx = preset["comm_channel_idx"]
        self.state.carrier_ghz = preset.get("carrier_ghz", self.state.carrier_ghz)
        self.state.carrier_select = min(5, max(1, preset["comm_channel_idx"] // 2 + 1))
        if "power_gain_db" in preset:
            self.state.power_gain_db = preset["power_gain_db"]
        self.state.redundancy = preset["redundancy"]
        self.state.burst_packets = preset["burst_packets"]

    def _base_station_policy(self, frame: int, snr_db: float, critical: float) -> None:
        if self._low_snr_streak >= 4 and self.state.anti_jamming_mode == 0:
            self._set_low_rate(frame, "基站退服：切换低速抗扰，提升弱信号可靠性")
        elif snr_db < critical and self.state.tx_task_mode != 4:
            self._set_task(frame, self.scenario["business"]["fallback_task"], 4, "基站退服：降级为短文本摘要，保障关键信息")

    def _rescue_congestion_policy(self, frame: int, snr_db: float, channel_quality: list[float]) -> None:
        if self._low_snr_streak >= 3:
            best_ch = max(range(len(channel_quality)), key=lambda i: channel_quality[i])
            if best_ch != self.state.comm_channel_idx:
                self._set_frequency_hop(frame, best_ch, "救援拥塞：切换到空闲信道，避让多音冲突")
            elif self.state.anti_jamming_mode == 0:
                self._set_low_rate(frame, "救援拥塞：同频冲突持续，启用低速抗扰")

    def _uav_policy(self, frame: int, snr_db: float, critical: float, channel_quality: list[float]) -> None:
        if self._low_snr_streak >= 3 and self.state.power_gain_db < 22:
            self._decision(frame, "无人机压测：先提升链路增益，维持视频回传")
            self.state.power_gain_db = 22
            self.state.anti_jamming_mode = 0
            self.state.anti_jamming_mode_name = "增益补偿模式"
            return
        if self._low_snr_streak >= 7:
            best_ch = max(range(len(channel_quality)), key=lambda i: channel_quality[i])
            if best_ch != self.state.comm_channel_idx:
                self._set_frequency_hop(frame, best_ch, "无人机压测：高功率压制持续，切频恢复链路")
        if snr_db < critical and self.state.tx_task_mode != 1:
            self._set_task(frame, self.scenario["business"]["fallback_task"], 1, "无人机压测：视频降级为关键帧图片")
        elif snr_db < critical and self.state.tx_task != self.scenario["business"]["fallback_task"]:
            self._set_task(frame, self.scenario["business"]["fallback_task"], 1, "无人机压测：视频降级为关键帧图片")

    def _agent_policy(self, frame: int, snr_db: float, critical: float, channel_quality: list[float], jam_active: bool) -> None:
        if not jam_active:
            return

        medium = self._low_snr_streak >= 3
        if not medium:
            return

        action = self._select_best_action(snr_db, channel_quality)
        if self._same_action(action):
            return
        self._apply_action(frame, action)

    def _best_channel(self, channel_quality: list[float]) -> int:
        return max(range(len(channel_quality)), key=lambda i: channel_quality[i])

    def _select_best_action(self, snr_db: float, channel_quality: list[float]) -> dict[str, Any]:
        current = self.state.comm_channel_idx
        ranked = sorted(range(len(channel_quality)), key=lambda i: channel_quality[i], reverse=True)
        candidate_channels = []
        for ch in [current] + ranked[:4]:
            if ch not in candidate_channels:
                candidate_channels.append(ch)

        best: dict[str, Any] | None = None
        best_score = -1e9
        for profile in self._action_profiles():
            for ch in candidate_channels:
                action = dict(profile)
                action["comm_channel_idx"] = ch
                action["carrier_select"] = min(5, max(1, ch // 2 + 1))
                action["carrier_ghz"] = CARRIER_GHZ[action["carrier_select"] - 1]
                score = self._score_action(action, ch, snr_db, channel_quality)
                if score > best_score:
                    best = action
                    best_score = score
        assert best is not None
        return best

    def _action_profiles(self) -> list[dict[str, Any]]:
        fallback_task = self.scenario["business"]["fallback_task"]
        profiles = [
            self._profile("QPSK保持", "QPSK", 0.75, 1.0, 1, 14, 8.0, 1.00, 1.00, 10, 0, "常规模式"),
            self._profile("QPSK+LDPC", "QPSK", 0.62, 1.0, 2, 16, 7.0, 0.94, 1.18, 8, 1, "编码增强"),
            self._profile("8PSK高吞吐", "8PSK", 0.72, 1.0, 2, 17, 7.0, 1.00, 1.05, 9, 0, "高吞吐保持"),
            self._profile("16QAM快速图传", "16QAM", 0.78, 1.0, 1, 18, 7.5, 1.00, 1.00, 10, 0, "高阶调制"),
            self._profile("BPSK强FEC", "BPSK", 0.50, 1.4, 4, 16, 6.0, 0.82, 1.45, 5, 1, "鲁棒编码"),
            self._profile("DSSS-BPSK扩频", "DSSS-BPSK", 0.55, 2.6, 5, 16, 5.8, 0.62, 1.60, 4, 1, "直接序列扩频"),
            self._profile("OFDM-QPSK交织", "OFDM-QPSK", 0.67, 1.1, 6, 17, 6.5, 0.90, 1.20, 7, 2, "OFDM交织抗频选"),
            self._profile("FHSS-QPSK跳频", "FHSS-QPSK", 0.66, 1.25, 3, 17, 6.5, 0.88, 1.25, 7, 2, "跳频扩频"),
            self._profile("CSS低速鲁棒", "CSS", 0.46, 3.2, 6, 15, 5.5, 0.45, 1.80, 3, 1, "线性调频扩频"),
        ]
        if self._low_snr_streak >= 8:
            profiles.append({
                **self._profile("业务降级保底", "DSSS-BPSK", 0.48, 2.8, 6, 16, 5.5, 0.58, 1.85, 3, 1, "业务降级鲁棒"),
                "tx_task": fallback_task,
                "tx_task_mode": self._task_mode(fallback_task),
            })
        return profiles

    def _profile(
        self,
        name: str,
        modulation: str,
        coding_rate: float,
        spreading_factor: float,
        interleaving_depth: int,
        power_gain_db: int,
        sync_threshold_db: float,
        bandwidth_scale: float,
        redundancy: float,
        burst_packets: int,
        mode: int,
        mode_name: str,
    ) -> dict[str, Any]:
        return {
            "action_profile": name,
            "modulation": modulation,
            "coding_rate": coding_rate,
            "spreading_factor": spreading_factor,
            "interleaving_depth": interleaving_depth,
            "power_gain_db": power_gain_db,
            "sync_threshold_db": sync_threshold_db,
            "bandwidth_scale": bandwidth_scale,
            "redundancy": redundancy,
            "burst_packets": burst_packets,
            "anti_jamming_mode": mode,
            "anti_jamming_mode_name": mode_name,
            "route_diversity": 1,
            "tx_task": self.state.tx_task,
            "tx_task_mode": self.state.tx_task_mode,
        }

    def _score_action(self, action: dict[str, Any], ch: int, snr_db: float, channel_quality: list[float]) -> float:
        jam = self.scenario["jammer"]
        penalty = estimate_quality_penalty(
            ch,
            {
                "channel_idx": int(jam["channel_idx"]),
                "waveform_mode": int(jam["waveform_mode"]),
                "power_db": float(jam["power_db"]),
                "bw_mhz": int(jam["bw_mhz"]),
            },
            self.global_cfg,
        )
        predicted_snr = self._predict_snr(action, penalty, channel_quality[ch])
        predicted_thr = self._predict_throughput(action, predicted_snr)
        reliability = 1.0 / (1.0 + math.exp(-(predicted_snr - float(self.scenario["link"]["decision_snr_threshold_db"])) / 1.8))
        avoid = 1.0 if penalty <= 0.1 else 0.0
        switch_cost = 0.04 if ch != self.state.comm_channel_idx else 0.0
        power_cost = max(0.0, (float(action["power_gain_db"]) - 14.0) / 10.0) * 0.035
        complexity_cost = (
            max(0.0, float(action["spreading_factor"]) - 1.0) * 0.025
            + max(0, int(action["interleaving_depth"]) - 1) * 0.008
            + (0.035 if action["modulation"] in {"CSS", "DSSS-BPSK"} else 0.0)
        )
        degrade_cost = 0.12 if action["tx_task"] != self.scenario["business"]["initial_task"] else 0.0
        if self.scenario["id"] == "base_station_outage":
            weights = (0.56, 0.22, 0.16)
        elif self.scenario["id"] == "rescue_congestion":
            weights = (0.35, 0.42, 0.18)
        else:
            weights = (0.42, 0.38, 0.14)
        return (
            weights[0] * reliability
            + weights[1] * min(1.0, predicted_thr / 230.0)
            + weights[2] * avoid
            - switch_cost
            - power_cost
            - complexity_cost
            - degrade_cost
        )

    def _predict_snr(self, action: dict[str, Any], penalty: float, quality: float) -> float:
        base = float(self.scenario["link"]["base_snr_db"])
        coding = max(0.0, (0.75 - float(action["coding_rate"])) * 3.0)
        spread = max(0.0, float(action["spreading_factor"]) - 1.0) * 0.9
        interleave = max(0.0, int(action["interleaving_depth"]) - 1) * 0.12
        power = max(0.0, float(action["power_gain_db"]) - 14.0) * 0.14
        mod = {"BPSK": 1.4, "DSSS-BPSK": 1.7, "CSS": 1.9, "QPSK": 0.0, "OFDM-QPSK": 0.2, "FHSS-QPSK": 0.3, "8PSK": -0.7, "16QAM": -1.5}.get(action["modulation"], 0.0)
        raw = base - penalty + coding + spread + interleave + power + mod + (quality - 0.7) * 2.5
        return clamp(raw, -3.0, base + 1.2)

    def _predict_throughput(self, action: dict[str, Any], snr_db: float) -> float:
        task_base = {1: 165.0, 2: 360.0, 3: 300.0, 4: 54.0}.get(int(action["tx_task_mode"]), 150.0)
        mod_eff = {"BPSK": 0.55, "DSSS-BPSK": 0.42, "CSS": 0.26, "QPSK": 1.0, "OFDM-QPSK": 1.05, "FHSS-QPSK": 0.88, "8PSK": 1.35, "16QAM": 1.7}.get(action["modulation"], 1.0)
        robust_overhead = 1.0 / max(1.0, float(action["spreading_factor"]) ** 0.55)
        coding = float(action["coding_rate"]) / 0.75
        bandwidth = float(action["bandwidth_scale"])
        snr_factor = clamp((snr_db + 2.0) / 20.0, 0.05, 1.0)
        return task_base * mod_eff * coding * robust_overhead * bandwidth * snr_factor

    def _same_action(self, action: dict[str, Any]) -> bool:
        fields = [
            "comm_channel_idx",
            "modulation",
            "coding_rate",
            "spreading_factor",
            "interleaving_depth",
            "power_gain_db",
            "bandwidth_scale",
            "tx_task",
        ]
        return all(getattr(self.state, field) == action[field] for field in fields)

    def _apply_action(self, frame: int, action: dict[str, Any]) -> None:
        changed_channel = action["comm_channel_idx"] != self.state.comm_channel_idx
        self._decision(frame, f"Agent效用评估选择：{action['action_profile']}，信道{action['comm_channel_idx']}")
        for key, value in action.items():
            if hasattr(self.state, key):
                setattr(self.state, key, value)
        self.state.transition_remaining = 3 if changed_channel else 2

    def _mark_recovery(self, frame: int, snr_db: float, threshold: float) -> DecisionState:
        if (
            self.state.first_response_frame is not None
            and self.state.recovered_frame is None
            and self._stable_streak >= 3
            and snr_db >= threshold
        ):
            self.state.recovered_frame = frame
        return self.state

    def _set_low_rate(self, frame: int, action: str) -> None:
        self._decision(frame, action)
        self.state.anti_jamming_mode = 1
        self.state.anti_jamming_mode_name = "低速抗扰模式"
        self.state.redundancy = 1.75
        self.state.burst_packets = 3
        self.state.modulation = "BPSK"
        self.state.coding_rate = 0.5
        self.state.spreading_factor = 2.0
        self.state.interleaving_depth = 4
        self.state.sync_threshold_db = 6.0
        self.state.bandwidth_scale = 0.65

    def _set_frequency_hop(self, frame: int, channel_idx: int, action: str) -> None:
        self._decision(frame, action)
        self.state.anti_jamming_mode = 2
        self.state.anti_jamming_mode_name = "切频模式"
        self.state.comm_channel_idx = channel_idx
        self.state.carrier_select = min(5, max(1, channel_idx // 2 + 1))
        self.state.carrier_ghz = CARRIER_GHZ[self.state.carrier_select - 1]
        self.state.redundancy = 1.2
        self.state.burst_packets = 8
        self.state.modulation = "QPSK"
        self.state.coding_rate = min(self.state.coding_rate, 0.67)
        self.state.spreading_factor = max(self.state.spreading_factor, 1.3)
        self.state.interleaving_depth = max(self.state.interleaving_depth, 2)

    def _set_power_boost(self, frame: int, gain_db: int, action: str) -> None:
        self._decision(frame, action)
        self.state.power_gain_db = gain_db
        self.state.anti_jamming_mode_name = "增益补偿模式"
        self.state.sync_threshold_db = min(self.state.sync_threshold_db, 6.5)

    def _set_waveform_profile(
        self,
        frame: int,
        action: str,
        *,
        mode: int,
        mode_name: str,
        modulation: str,
        coding_rate: float,
        spreading_factor: float,
        interleaving_depth: int,
        redundancy: float,
        burst_packets: int,
        sync_threshold_db: float,
        bandwidth_scale: float,
    ) -> None:
        self._decision(frame, action)
        self.state.anti_jamming_mode = mode
        self.state.anti_jamming_mode_name = mode_name
        self.state.modulation = modulation
        self.state.coding_rate = coding_rate
        self.state.spreading_factor = spreading_factor
        self.state.interleaving_depth = interleaving_depth
        self.state.redundancy = redundancy
        self.state.burst_packets = burst_packets
        self.state.sync_threshold_db = sync_threshold_db
        self.state.bandwidth_scale = bandwidth_scale

    def _set_route_diversity(self, frame: int, action: str) -> None:
        self._decision(frame, action)
        self.state.route_diversity = 2
        self.state.redundancy = max(self.state.redundancy, 1.5)
        self.state.burst_packets = min(self.state.burst_packets, 5)

    def _set_task(self, frame: int, task: str, mode: int, action: str) -> None:
        self._decision(frame, action)
        self.state.tx_task = task
        self.state.tx_task_mode = mode
        self.state.redundancy = max(self.state.redundancy, 1.4)
        self.state.burst_packets = min(self.state.burst_packets, 5)

    def _decision(self, frame: int, action: str) -> None:
        if self.state.first_response_frame is None:
            self.state.first_response_frame = frame
        self.state.decision_count += 1
        self.state.last_action = action

    @staticmethod
    def _task_mode(task: str) -> int:
        if "视频" in task:
            return 2
        if "文本" in task:
            return 4
        return 1


def estimate_quality_penalty(comm_channel_idx: int, action: dict[str, Any], global_cfg: dict[str, Any]) -> float:
    if comm_channel_idx in affected_channels(int(action["channel_idx"]), float(action["bw_mhz"]), global_cfg):
        mode_factor = 1.0 if int(action["waveform_mode"]) == 0 else 0.72
        return float(action["power_db"]) * mode_factor
    return 0.0
