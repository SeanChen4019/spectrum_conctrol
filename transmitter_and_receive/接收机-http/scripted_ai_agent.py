"""
Scripted AI Agent — replaces DeepSeek API with a timed state machine for demo.
Runs entirely in the main thread via QTimer. No network required.
"""
from __future__ import annotations

import time
from PyQt5.QtCore import QObject, QTimer, pyqtSignal

CARRIER_SET_GHZ = [2.0, 2.5, 3.0, 3.5, 4.0]
MODE_MAP = {"常规模式": 0, "低速抗扰模式": 1, "切频模式": 2}
MODE_REVERSE = {0: "常规模式", 1: "低速抗扰模式", 2: "切频模式"}


SCENE_SCRIPTS = [
    {
        "id": "base_station_outage",
        "name": "基站退服",
        "snr_threshold": 13.0,       # catches jam drop (9-10 dB), stays below post-low-rate recovery (14-16 dB)
        "confirm_count": 3,           # consecutive low-SNR checks needed
        "anti_jamming_mode": 1,       # 低速抗扰模式
        "mode_name": "低速抗扰模式",
        "carrier_select": 3,          # 3.0 GHz
        "frequency_ghz": 3.0,
        "messages": [
            "⚠️ 检测到异常：当前信噪比骤降，通信质量急剧恶化",
            "🔍 正在分析频谱特征…底噪整体抬升约8MHz，带宽内无离散尖峰",
            "🔍 综合研判：公网基站退服导致局部宽带噪声抬升，信道3同时受扰",
            "📡 应对策略：切换至【低速抗扰模式】，采用BPSK+扩频编码",
            "📡 牺牲速率换取链路可靠性，保障灾情图片与短文本关键信息可达",
            "✅ 已执行。低速抗扰模式生效中。正在持续监控链路状态…",
        ],
        "delays": [1.2, 1.8, 2.0, 1.5, 1.2, 1.0],
    },
    {
        "id": "rescue_congestion",
        "name": "救援拥塞",
        "snr_threshold": 18.0,       # catches jam drop (17-18 dB), stays below post-hop recovery (25.5 dB)
        "confirm_count": 3,
        "anti_jamming_mode": 2,       # 切频模式
        "mode_name": "切频模式",
        "carrier_select": 4,          # 3.5 GHz, displayed as channel 8 in the demo
        "frequency_ghz": 3.5,
        "messages": [
            "⚠️ 信噪比再次异常，频谱中出现多个离散尖峰",
            "🔍 多支救援队伍密集接入，局部频点发生冲突",
            "🔍 判断为救援拥塞场景：强多音干扰覆盖信道5-6区域",
            "📡 应对策略：切换至【切频模式】，跳频至3.5GHz信道8",
            "📡 避开多音频点冲突区域，在干净信道恢复QPSK正常传输",
            "✅ 已切换至信道8(3.5GHz)。多终端图传与状态包恢复。正在持续监控…",
        ],
        "delays": [1.2, 1.8, 2.0, 1.5, 1.2, 1.0],
    },
    {
        "id": "uav_video_pressure",
        "name": "无人机压测",
        "snr_threshold": 13.0,
        "confirm_count": 2,
        "anti_jamming_mode": 0,        # first: gain boost (常规 but power raised)
        "mode_name": "增益补偿",
        "carrier_select": 4,           # 3.5 GHz
        "frequency_ghz": 3.5,
        "messages": [
            "⚠️ 严重干扰告警！信噪比急速下降，信道6-9出现高功率宽带压制",
            "🔍 估算干扰功率约20dB，覆盖6MHz频宽，正在对无人机视频回传实施强力压制",
            "🔍 判断为无人机压测场景。当前视频链路已不可用，需多级应对",
            "📡 第一级：提升发射增益至22dB，尝试维持视频回传",
            "⚠️ 增益提升后信噪比仍不足，高功率压制持续穿过增益余量",
            "📡 第二级：切换至【切频模式】，跳频至3.0GHz信道3",
            "📡 同时业务降级——视频回传转为关键帧图片回传，保障灾情画面连续性",
            "✅ 链路恢复。当前3.0GHz载波，QPSK模式，关键帧图片回传中。正在持续监控…",
        ],
        "delays": [1.2, 2.0, 2.0, 1.5, 1.8, 2.0, 1.5, 1.0],
        # Secondary action: after message index 5, switch to frequency hop
        "secondary": {
            "at_message": 5,
            "anti_jamming_mode": 2,
            "mode_name": "切频模式",
            "carrier_select": 3,       # 3.0 GHz, displayed as channel 3 in the demo
            "frequency_ghz": 3.0,
        },
    },
]


class ScriptedAIAgent(QObject):
    """Scripted AI agent that replaces the DeepSeek-based AIAgent for demos."""

    # Signals to MainWindow
    chat_message = pyqtSignal(str)               # display in chat bubble
    show_interactive = pyqtSignal(str, object)    # show message with buttons [(label, callback_id)]
    request_clear = pyqtSignal()                  # clear interactive buttons
    scene_changed = pyqtSignal(int, int, float)   # (anti_jamming_mode, carrier_select, freq_ghz)
    image_progress = pyqtSignal(int)              # 0-100 progress

    def __init__(self, decision_store, data_store, data_mutex, decision_mutex, parent=None):
        super().__init__(parent)
        self.decision_store = decision_store
        self.data_store = data_store
        self.data_mutex = data_mutex
        self.decision_mutex = decision_mutex

        # ── State machine ──
        self._state = "IDLE"           # IDLE | AWAITING_AUTH | AUTH_CONFIRMED | MONITORING | RESPONDING
        self._scene_index = 0          # which scene we're on (0, 1, 2)
        self._msg_idx = 0              # which message in current scene's message list
        self._low_snr_count = 0        # consecutive low-SNR readings
        self._monitor_checks = 0       # total monitor ticks
        self._cooldown_checks = 0      # checks to wait after responding before next scene
        self._last_snr = 99.0
        self._scenes_finished = False

        # ── Timers ──
        self._monitor_timer = QTimer(self)
        self._monitor_timer.timeout.connect(self._monitor_tick)
        self._monitor_timer.setInterval(1500)  # check every 1.5s

        self._response_timer = QTimer(self)
        self._response_timer.timeout.connect(self._response_tick)
        self._response_timer.setSingleShot(True)

        self._chat_timer = QTimer(self)
        self._chat_timer.timeout.connect(self._deliver_next_message)
        self._chat_timer.setSingleShot(True)

    # ── Public API ──────────────────────────────────────────

    def chat(self, user_message: str):
        """Called from background thread when user sends a message.
        Returns (reply_text, action_summary) — but for scripted flow we use signals."""
        msg = user_message.strip()

        if self._state == "IDLE":
            if "全权" in msg or "负责" in msg or "最大化" in msg or "自行选择" in msg:
                self._state = "AWAITING_AUTH"
                # Will show interactive buttons from main thread via signal
                self.show_interactive.emit(
                    "是否让我全权负责通信链路，即是否赋予我管理员权限？",
                    [("是，赋予管理员权限", "auth_yes"), ("否，保持手动控制", "auth_no")],
                )
                return ("正在分析您的请求…", "")
            return ("收到。如需AI全权接管链路控制，请告知。", "")

        if self._state == "AWAITING_AUTH":
            return ("请点击上方按钮确认是否授权。", "")

        if self._state == "MONITORING" or self._state == "RESPONDING":
            return ("AI正在自动监控链路状态，无需手动操作。", "")

        return ("收到。", "")

    def handle_interactive(self, callback_id: str):
        """Called when user clicks an interactive button."""
        if callback_id == "auth_yes":
            self._state = "AUTH_CONFIRMED"
            self.request_clear.emit()
            self.chat_message.emit("✅ 已获得管理员权限。")
            # Start the boot sequence
            self._chat_timer.start(800)
            self._msg_idx = 0
            self._boot_sequence = [
                "正在初始化频谱监测系统…",
                "加载应急通信场景知识库…",
                "启动10信道实时频谱感知…",
                "链接智能决策引擎…",
                "🟢 全自动频谱管控系统就绪。正在持续监控链路状态，将在检测到异常时自动响应。",
            ]
            self._delivering_boot = True
        elif callback_id == "auth_no":
            self.request_clear.emit()
            self.chat_message.emit("好的，保持手动控制模式。如需AI接管，请随时告知。")
            self._state = "IDLE"

    # ── Boot sequence delivery ─────────────────────────────

    def _deliver_next_message(self):
        if getattr(self, "_delivering_boot", False):
            seq = getattr(self, "_boot_sequence", [])
            idx = self._msg_idx
            if idx < len(seq):
                self.chat_message.emit(seq[idx])
                self._msg_idx += 1
                self._chat_timer.start(1000 if idx < len(seq) - 1 else 1500)
                if idx == len(seq) - 1:
                    self._delivering_boot = False
                    # Start monitoring after last boot message
                    self._chat_timer.singleShot(2000, self._start_monitoring)
            return

        # Delivering scene response messages
        scene = SCENE_SCRIPTS[self._scene_index]
        msgs = scene["messages"]
        delays = scene["delays"]
        idx = self._msg_idx
        if idx < len(msgs):
            self.chat_message.emit(msgs[idx])

            # Check for secondary action
            secondary = scene.get("secondary")
            if secondary and idx == secondary["at_message"]:
                self._apply_secondary(secondary)

            self._msg_idx += 1
            if idx < len(delays):
                self._chat_timer.start(int(delays[idx] * 1000))
            else:
                self._chat_timer.start(1000)
        else:
            # All messages delivered, transition back to monitoring
            self._state = "MONITORING"
            self._scene_index += 1
            if self._scene_index >= len(SCENE_SCRIPTS):
                self._scenes_finished = True
                self.chat_message.emit("✅ 所有预设应急场景已完成响应。系统保持当前最优配置持续运行。")
                return
            self._cooldown_checks = 0
            self._low_snr_count = 0
            self._msg_idx = 0

    # ── Monitoring ──────────────────────────────────────────

    def _start_monitoring(self):
        self._state = "MONITORING"
        self._scene_index = 0
        self._low_snr_count = 0
        self._cooldown_checks = 0
        self._monitor_checks = 0
        self._baseline_established = False
        self._baseline_checks = 0
        self._monitor_timer.start()

    def _read_snr(self) -> float:
        try:
            self.data_mutex.acquire()
            snr_text = str(self.data_store.get("status", {}).get("snr", "99 dB"))
            self.data_mutex.release()
            return float(snr_text.replace("dB", "").strip().split()[0])
        except Exception:
            try:
                self.data_mutex.release()
            except Exception:
                pass
            return 99.0

    def _monitor_tick(self):
        if self._state != "MONITORING":
            return
        if self._scenes_finished:
            return
        if self._scene_index >= len(SCENE_SCRIPTS):
            return

        snr = self._read_snr()
        self._last_snr = snr
        self._monitor_checks += 1

        # ── Establish baseline: need 2 consecutive high-SNR readings (>20 dB)
        # before we consider a drop as a real "event".
        if not getattr(self, "_baseline_established", False):
            if snr > 20.0:
                self._baseline_checks = getattr(self, "_baseline_checks", 0) + 1
                if self._baseline_checks >= 2:
                    self._baseline_established = True
                    self._low_snr_count = 0
            else:
                self._baseline_checks = 0
            return

        scene = SCENE_SCRIPTS[self._scene_index]
        threshold = scene["snr_threshold"]
        need_count = scene["confirm_count"]

        if snr < threshold:
            self._low_snr_count += 1
        else:
            self._low_snr_count = max(0, self._low_snr_count - 1)

        if self._low_snr_count >= need_count:
            self._monitor_timer.stop()
            self._state = "RESPONDING"
            self._msg_idx = 0
            self._low_snr_count = 0
            self._trigger_scene(scene)

    def _trigger_scene(self, scene: dict):
        """Apply scene parameters and start delivering response messages."""
        dm = self.decision_mutex
        ds = self.decision_store

        dm.acquire()
        try:
            ds["anti_jamming_mode"] = scene["anti_jamming_mode"]
            ds["carrier_select"] = scene["carrier_select"]
            ds["power_gain_select"] = 1
            ds["needs_update"] = True
            ds["decision_version"] += 1
            ds["command_source"] = "ai_agent"
            ds["command_description"] = f"AI全权接管：检测到{scene['name']}，自动切换{scene['mode_name']}"
        finally:
            dm.release()

        # Signal for UI update (RadioButtons etc.)
        self.scene_changed.emit(
            scene["anti_jamming_mode"],
            scene["carrier_select"],
            scene["frequency_ghz"],
        )

        # Start delivering messages
        self._chat_timer.start(500)

    def _apply_secondary(self, secondary: dict):
        """Apply a secondary (phase 2) scene action to decision store."""
        dm = self.decision_mutex
        ds = self.decision_store
        dm.acquire()
        try:
            ds["anti_jamming_mode"] = secondary["anti_jamming_mode"]
            ds["carrier_select"] = secondary["carrier_select"]
            ds["needs_update"] = True
            ds["decision_version"] += 1
            ds["command_source"] = "ai_agent"
            ds["command_description"] = f"AI全权接管：第二阶段{secondary['mode_name']}"
        finally:
            dm.release()
        self.scene_changed.emit(
            secondary["anti_jamming_mode"],
            secondary["carrier_select"],
            secondary["frequency_ghz"],
        )

    def get_state_for_prompt(self) -> str:
        """Return a status string for any UI that needs it."""
        dm = self.decision_mutex
        dm.acquire()
        try:
            mode_idx = self.decision_store["anti_jamming_mode"]
            carrier_idx = self.decision_store["carrier_select"]
            power_idx = self.decision_store["power_gain_select"]
        finally:
            dm.release()

        mode_name = MODE_REVERSE.get(mode_idx, "未知")
        carrier = CARRIER_SET_GHZ[carrier_idx - 1] if 1 <= carrier_idx <= 5 else 3.0
        return f"模式:{mode_name} | 频率:{carrier}GHz | 功率:{power_idx - 1}dB"
