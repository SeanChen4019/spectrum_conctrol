import json
import os
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from telemetry_server import TelemetryServer
from agent.agent_brain import AgentBrain
from agent.agent_panel import AgentPanel
from command_bridge import CommandBridge


def make_kpi_card(title: str, value: str = "--"):
    frame = QtWidgets.QFrame()
    frame.setObjectName("KpiCard")
    layout = QtWidgets.QVBoxLayout(frame)
    layout.setContentsMargins(14, 10, 14, 10)
    layout.setSpacing(4)
    title_label = QtWidgets.QLabel(title)
    title_label.setObjectName("CardTitle")
    value_label = QtWidgets.QLabel(value)
    value_label.setObjectName("CardValue")
    value_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
    layout.addWidget(title_label)
    layout.addWidget(value_label)
    return frame, value_label


class LogWidget(QtWidgets.QTableWidget):
    def __init__(self):
        super().__init__(0, 6)
        self.setHorizontalHeaderLabels(
            ["时间", "序号", "信道", "功率(dB)", "带宽(MHz)", "干扰模式"]
        )
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)

    def append_log(self, row_data):
        row = self.rowCount()
        self.insertRow(row)
        for col, value in enumerate(row_data):
            self.setItem(row, col, QtWidgets.QTableWidgetItem(str(value)))
        self.scrollToBottom()
        while self.rowCount() > 200:
            self.removeRow(0)


class MainWindow(QtWidgets.QMainWindow):
    packet_received = QtCore.pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("应急保障智能体网络频谱管控系统 - Jammer 控制台")
        self.resize(1920, 1040)

        self.max_history = 120
        self.num_channels = 10
        self.channel_history = deque(
            [np.zeros(self.num_channels, dtype=np.int32) for _ in range(self.max_history)],
            maxlen=self.max_history,
        )
        self.action_history = deque(
            [{"channel_idx": 0, "bw_mhz": 4, "power_db": 0.0} for _ in range(self.max_history)],
            maxlen=self.max_history,
        )
        self.channel_width_mhz = 2.0
        self.map_bins_per_channel = 2  # one visible bin = 1 MHz
        self._agent_request_id = 0
        self.snr_history = deque([0.0] * self.max_history, maxlen=self.max_history)
        self.bw_history = deque([0.0] * self.max_history, maxlen=self.max_history)
        self.power_history = deque([0.0] * self.max_history, maxlen=self.max_history)
        self._agent_worker = None
        self._fixed_sequence = None
        self._fixed_scene_id = None
        self._fixed_frame_idx = 0

        # ── Agent ──────────────────────────────────────────────
        # Competition demo fallback: prefer environment variable, then use the
        # local key that was originally bundled with this project.
        api_key = os.environ.get(
            "DEEPSEEK_API_KEY",
            "sk-97df371f49114cadba72844118a5711c",
        )
        self._agent_init_error: str | None = None
        try:
            self.agent_brain = AgentBrain(api_key=api_key)
        except Exception as e:
            import traceback
            self.agent_brain = None
            self._agent_init_error = f"Agent初始化失败:\n{traceback.format_exc()}"

        # ── Command bridge ─────────────────────────────────────
        self.cmd_bridge = CommandBridge("127.0.0.1", 5557)
        self.cmd_bridge.start()

        # ── Wire agent callbacks ───────────────────────────────
        if self.agent_brain:
            self.agent_brain.set_on_set_params(self._on_agent_set_params)

        # ── Telemetry server ───────────────────────────────────
        self.packet_received.connect(self.handle_packet)
        self.server = TelemetryServer("127.0.0.1", 5555, self.packet_received.emit)
        self.server.start()

        self._build_ui()
        self._apply_style()
        self._wire_agent()
        self._init_fixed_sequence_player()

    # ═══════════════════════════════════════════════════════════════
    # Layout:
    #  ┌──────────────────────────────────────┬─────────────────┐
    #  │  Header                              │                 │
    #  │  KPI Cards (2x3)                     │  AI 智能助手     │
    #  ├────────────────┬─────────────────────┤  (右侧全高)      │
    #  │  实时频谱      │  10信道状态图        │                 │
    #  ├────────────────┴─────────────────────┤                 │
    #  │  Tabs: 波形|趋势|日志|信道特征        │                 │
    #  └──────────────────────────────────────┴─────────────────┘
    # ═══════════════════════════════════════════════════════════════

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        master_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        master_split.setHandleWidth(2)

        # ═══════════════════════════════════════════════════════
        # LEFT SIDE — monitoring panels
        # ═══════════════════════════════════════════════════════
        left_widget = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(8)

        # ── Header ─────────────────────────────────────────
        header = QtWidgets.QHBoxLayout()
        title_col = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel("应急保障智能体网络频谱管控系统")
        title.setObjectName("MainTitle")
        subtitle = QtWidgets.QLabel("灾后应急通信保障 · 极端电磁干扰模拟 · 10信道频谱监测 · Agent 指令闭环控制")
        subtitle.setObjectName("SubTitle")
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        header.addLayout(title_col)
        header.addStretch(1)

        self.conn_badge = QtWidgets.QLabel("等待连接  127.0.0.1:5555")
        self.conn_badge.setObjectName("ConnBadge")
        header.addWidget(self.conn_badge)

        left_layout.addLayout(header)

        # ── KPI Cards (2x3 = 6 cards) ──────────────────────
        cards = QtWidgets.QGridLayout()
        cards.setHorizontalSpacing(10)
        cards.setVerticalSpacing(8)
        self.card_ch, self.lbl_ch = make_kpi_card("当前信道", "--")
        self.card_pwr, self.lbl_pwr = make_kpi_card("发射功率", "-- dB")
        self.card_bw, self.lbl_bw = make_kpi_card("干扰带宽", "-- MHz")
        self.card_mode, self.lbl_mode = make_kpi_card("干扰模式", "--")
        self.card_snr, self.lbl_snr = make_kpi_card("全局信噪比", "-- dB")
        self.card_time, self.lbl_time = make_kpi_card("最近更新", "--")
        for i, widget in enumerate([
            self.card_ch, self.card_pwr, self.card_bw,
            self.card_mode, self.card_snr, self.card_time,
        ]):
            cards.addWidget(widget, i // 3, i % 3)
        left_layout.addLayout(cards)

        # ── Middle: Spectrum | Channel Map ──────────────────
        middle_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)

        # Spectrum
        spectrum_panel = self._panel("实时频谱", "3GHz 频段实时频谱")
        spec_layout = spectrum_panel.layout()
        self.spectrum_plot = pg.PlotWidget()
        self._style_plot(self.spectrum_plot, "频率 (GHz)", "幅度")
        self.spectrum_curve = self.spectrum_plot.plot(pen=pg.mkPen("#66d9ef", width=2))
        spec_layout.addWidget(self.spectrum_plot)
        middle_split.addWidget(spectrum_panel)

        # Channel Map — white background for clean look
        map_panel = self._panel("信道状态图", "灰空闲  绿通信  红干扰  黑重叠")
        map_layout = map_panel.layout()
        self.map_plot = pg.PlotWidget()
        self.map_plot.setBackground("w")
        self.map_plot.showGrid(x=True, y=True, alpha=0.12)
        self.map_plot.setLabel("bottom", "信道编号")
        self.map_plot.setLabel("left", "历史帧（底部最新）")
        for axis_name in ["bottom", "left"]:
            self.map_plot.getAxis(axis_name).setPen(pg.mkPen("#333333"))
            self.map_plot.getAxis(axis_name).setTextPen(pg.mkPen("#333333"))
        self.map_img = pg.ImageItem()
        self.map_plot.addItem(self.map_img)
        self.map_img.setRect(QtCore.QRectF(0.5, 0, self.num_channels, self.max_history))
        self.map_plot.setXRange(0.5, self.num_channels + 0.5, padding=0)
        self.map_plot.setYRange(0, self.max_history, padding=0)
        self.map_plot.disableAutoRange()

        bottom_axis = self.map_plot.getAxis("bottom")
        bottom_ticks = [(i + 1, str(i + 1)) for i in range(self.num_channels)]
        bottom_axis.setTicks([bottom_ticks])

        left_axis = self.map_plot.getAxis("left")
        left_ticks = [(i, str(i)) for i in range(0, self.max_history + 1, 20)]
        left_axis.setTicks([left_ticks])

        cmap = pg.ColorMap(
            pos=np.array([0.0, 1 / 3, 2 / 3, 1.0]),
            color=np.array([
                [220, 225, 232, 255],  # 0: idle — light gray
                [34, 197, 94, 255],    # 1: user occupied — green
                [239, 68, 68, 255],    # 2: jammed — red
                [0, 0, 0, 255],        # 3: overlap — black
            ], dtype=np.ubyte),
        )
        self.map_img.setLookupTable(cmap.getLookupTable(0.0, 1.0, 256))
        self.map_img.setLevels([0, 3])
        map_layout.addWidget(self.map_plot)
        middle_split.addWidget(map_panel)

        middle_split.setSizes([720, 680])
        left_layout.addWidget(middle_split, stretch=4)

        # ── Bottom: Tabs ────────────────────────────────────
        tab_panel = self._panel("", "")
        tab_layout = tab_panel.layout()
        self.tabs = QtWidgets.QTabWidget()

        # Tab 1: Waveform
        waveform_tab = QtWidgets.QWidget()
        wf_layout = QtWidgets.QVBoxLayout(waveform_tab)
        self.wave_plot = pg.PlotWidget()
        self._style_plot(self.wave_plot, "采样点", "幅度")
        self.wave_curve = self.wave_plot.plot(pen=pg.mkPen("#c792ea", width=2))
        wf_layout.addWidget(self.wave_plot)
        self.tabs.addTab(waveform_tab, "干扰波形")

        # Tab 2: Trend
        trend_tab = QtWidgets.QWidget()
        trend_tab_layout = QtWidgets.QVBoxLayout(trend_tab)
        self.trend_plot = pg.PlotWidget()
        self._style_plot(self.trend_plot, "帧索引", "数值")
        self.trend_plot.addLegend(offset=(10, 10))
        self.snr_curve = self.trend_plot.plot(name="信噪比(dB)", pen=pg.mkPen("#4dff88", width=2))
        self.bw_curve = self.trend_plot.plot(name="带宽(MHz)", pen=pg.mkPen("#ffb84d", width=2))
        self.power_curve = self.trend_plot.plot(name="功率(dB)", pen=pg.mkPen("#ff5c73", width=2))
        trend_tab_layout.addWidget(self.trend_plot)
        self.tabs.addTab(trend_tab, "运行趋势")

        # Tab 3: Event Log
        log_tab = QtWidgets.QWidget()
        log_layout = QtWidgets.QVBoxLayout(log_tab)
        self.log_widget = LogWidget()
        log_layout.addWidget(self.log_widget)
        self.tabs.addTab(log_tab, "事件日志")

        self.tabs.setCurrentIndex(0)
        tab_layout.addWidget(self.tabs)
        left_layout.addWidget(tab_panel, stretch=3)

        master_split.addWidget(left_widget)

        # ═══════════════════════════════════════════════════════
        # RIGHT SIDE — AI Agent control panel
        # ═══════════════════════════════════════════════════════
        self.agent_panel = AgentPanel()
        master_split.addWidget(self.agent_panel)

        master_split.setSizes([1400, 460])

        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 10)
        root.addWidget(master_split)

    @staticmethod
    def _panel(title: str, desc: str):
        box = QtWidgets.QGroupBox(title)
        box.setObjectName("PanelBox")
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        if desc:
            desc_label = QtWidgets.QLabel(desc)
            desc_label.setObjectName("PanelDesc")
            layout.addWidget(desc_label)
        return box

    def _style_plot(self, plot, xlabel, ylabel):
        plot.setBackground("#0d1117")
        plot.showGrid(x=True, y=True, alpha=0.18)
        plot.setLabel("bottom", xlabel)
        plot.setLabel("left", ylabel)
        axis_pen = pg.mkPen("#9aa4b2")
        for axis_name in ["bottom", "left"]:
            plot.getAxis(axis_name).setPen(axis_pen)
            plot.getAxis(axis_name).setTextPen(axis_pen)

    def _apply_style(self):
        self.setStyleSheet("""
            QWidget {
                background: #08111c; color: #e8eef5;
                font-family: "Microsoft YaHei UI", "SimHei", "Segoe UI", sans-serif;
                font-size: 11pt;
            }
            #MainTitle { font-size: 20pt; font-weight: 700; color: #f8fbff; }
            #SubTitle { color: #9fb3c8; font-size: 9pt; }
            #ConnBadge {
                background: #10251f; border: 1px solid #1f8a60; border-radius: 8px;
                padding: 6px 12px; color: #7dffbf; font-weight: 600; font-size: 10pt;
            }
            #KpiCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #102033, stop:1 #0c1725);
                border: 1px solid #23405f; border-radius: 8px;
            }
            #CardTitle { color: #8fa8c3; font-size: 9pt; }
            #CardValue { color: #ffffff; font-size: 16pt; font-weight: 700; }
            #PanelBox {
                font-size: 11pt; font-weight: 600; border: 1px solid #203a56;
                border-radius: 8px; margin-top: 10px; padding-top: 8px;
                background: #0d1826;
            }
            #PanelBox QGroupBox::title {
                subcontrol-origin: margin; left: 14px; padding: 0 8px;
                color: #dbe6f1;
            }
            #PanelDesc { color: #7f8ea3; font-size: 8pt; margin-bottom: 2px; }
            QTabWidget::pane { border: 1px solid #203a56; background: #0d1826; }
            QTabBar::tab {
                background: #112033; color: #c6d1dd; border: 1px solid #203a56;
                padding: 7px 12px; min-width: 70px; font-size: 10pt;
            }
            QTabBar::tab:selected { background: #174168; color: #ffffff; }
            QHeaderView::section {
                background: #102033; color: #dbe6f1; padding: 5px; border: none;
                border-bottom: 1px solid #203a56; font-size: 10pt;
            }
            QTableWidget {
                background: #08111c; alternate-background-color: #0d1826;
                border: 1px solid #203a56; color: #e8eef5; gridline-color: #203a56;
                font-size: 10pt;
            }
            QSplitter::handle {
                background: #1b344d; margin: 0 2px;
            }
            #AgentTitle { font-size: 14pt; font-weight: 700; color: #7dd3fc; }
            #AgentStatus { color: #6ee7b7; font-size: 9pt; }
            #ScenarioBanner {
                background: #0c1f26; border: 1px solid #1f7a8c; border-radius: 8px;
            }
            #ScenarioTitle { color: #e6faff; font-size: 11pt; font-weight: 700; }
            #ScenarioDesc { color: #95b8c7; font-size: 8pt; }
            #MiniLabel { color: #8fa8c3; min-width: 34px; font-size: 9pt; }
            #AgentClearBtn {
                background: #102033; border: 1px solid #203a56; border-radius: 8px;
                color: #c6d1dd; padding: 4px 10px; font-size: 9pt;
            }
            #AgentClearBtn:hover { background: #174168; }
            #AgentMessageArea {
                background: #050b12; border: 1px solid #203a56; border-radius: 8px;
                color: #e8eef5; font-size: 10pt;
            }
            #QuickCmdBtn {
                background: #102b3c; border: 1px solid #256f8b; border-radius: 8px;
                color: #a7e6ff; font-size: 8pt; padding: 3px 8px;
            }
            #QuickCmdBtn:hover { background: #174d68; }
            #ScenarioPresetBtn {
                background: #163222; border: 1px solid #2f9b64; border-radius: 8px;
                color: #b9ffd8; font-size: 8pt; font-weight: 700; padding: 3px 8px;
            }
            #ScenarioPresetBtn:hover { background: #1c4a31; border-color: #61d394; }
            #ChannelBtn {
                background: #172536; border: 1px solid #37506b; border-radius: 6px;
                color: #f2f7ff; font-weight: 700; font-size: 9pt;
            }
            #ChannelBtn:hover { background: #255073; border-color: #58c7ff; }
            #AgentInput {
                background: #050b12; border: 1px solid #2b4d69; border-radius: 8px;
                color: #e8eef5; padding: 7px 10px; font-size: 10pt;
            }
            #AgentSendBtn {
                background: #146c43; border: 1px solid #2dd47f; border-radius: 8px;
                color: #eafff5; font-weight: 700; padding: 7px; font-size: 10pt;
            }
            #AgentSendBtn:hover { background: #158553; }
        """)

    # ═══════════════════════════════════════════════════════════════
    # Agent wiring
    # ═══════════════════════════════════════════════════════════════

    def _wire_agent(self):
        self.agent_panel.send_message.connect(self._on_agent_message)
        self.agent_panel.direct_command.connect(self._on_direct_command)
        self.agent_panel.clear_requested.connect(self._on_clear_agent)
        self.agent_panel.stop_requested.connect(self._on_stop_agent)
        if self._agent_init_error:
            self.agent_panel.show_error(f"Agent初始化失败:\n{self._agent_init_error}")
            self.agent_panel.set_connected(False)
        elif self.agent_brain:
            self.agent_panel.set_connected(self.agent_brain.llm.available)
        else:
            self.agent_panel.set_connected(False)

    def _on_clear_agent(self):
        self._agent_request_id += 1
        self._stop_agent_timeout()

    def _on_stop_agent(self):
        self._agent_request_id += 1
        self._stop_agent_timeout()
        if self._agent_worker_is_running():
            self._agent_worker.requestInterruption()
            self._agent_worker.terminate()
            self._agent_worker.wait(300)

    def _on_agent_message(self, text: str):
        if self._agent_init_error:
            self.agent_panel.show_error(f"Agent初始化失败:\n{self._agent_init_error}")
            return
        if not self.agent_brain:
            self.agent_panel.show_error("Agent未就绪，请检查初始化错误。")
            return

        # Deterministic control/query commands should never wait for an API call.
        # This keeps competition demos responsive even when the network or LLM is slow.
        try:
            local_result = self.agent_brain.try_local_message(text)
            if local_result is not None:
                self.agent_panel.show_response(local_result)
                return
        except Exception as e:
            self.agent_panel.show_error(f"本地指令解析失败: {e}")
            return

        if self._agent_worker_is_running():
            self._agent_request_id += 1
            self._agent_worker.requestInterruption()
            self._agent_worker.terminate()
            self._agent_worker.wait(300)
        self._agent_request_id += 1
        request_id = self._agent_request_id
        self._agent_worker = AgentWorker(self.agent_brain, text, request_id)
        self._agent_worker.response_ready.connect(self._on_agent_worker_response)
        self._agent_worker.error_ready.connect(self._on_agent_worker_error)
        self._agent_worker.response_ready.connect(self._stop_agent_timeout)
        self._agent_worker.error_ready.connect(self._stop_agent_timeout)
        self._agent_worker.finished.connect(self._on_agent_worker_finished)
        self._agent_worker.finished.connect(self._agent_worker.deleteLater)
        self._start_agent_timeout()
        self._agent_worker.start()

    def _agent_worker_is_running(self) -> bool:
        if self._agent_worker is None:
            return False
        try:
            return self._agent_worker.isRunning()
        except RuntimeError:
            self._agent_worker = None
            return False

    def _on_agent_worker_response(self, request_id: int, text: str):
        if request_id != self._agent_request_id:
            return
        self.agent_panel.show_response(text)

    def _on_agent_worker_error(self, request_id: int, text: str):
        if request_id != self._agent_request_id:
            return
        self.agent_panel.show_error(text)

    def _on_agent_worker_finished(self):
        if self.sender() is self._agent_worker:
            self._agent_worker = None

    def _start_agent_timeout(self):
        if not hasattr(self, "_agent_timeout_timer"):
            self._agent_timeout_timer = QtCore.QTimer(self)
            self._agent_timeout_timer.setSingleShot(True)
            self._agent_timeout_timer.timeout.connect(self._on_agent_timeout)
        self._agent_timeout_timer.start(15000)

    def _stop_agent_timeout(self, *_args):
        if hasattr(self, "_agent_timeout_timer"):
            self._agent_timeout_timer.stop()

    def _on_agent_timeout(self):
        self._agent_request_id += 1
        if self._agent_worker_is_running():
            self._agent_worker.requestInterruption()
            self._agent_worker.terminate()
            self._agent_worker.wait(300)
        self.agent_panel.show_error(
            "API调用超过15秒未返回，已中止。本地控制指令仍可执行；请检查 DEEPSEEK_API_KEY、网络和模型服务。"
        )

    def _on_agent_set_params(self, params: dict):
        error = self._validate_command_params(params)
        if error:
            raise ValueError(error)
        self.cmd_bridge.enqueue_command(params)
        self._apply_kpi(params)
        self._push_state_to_agent()
        print(f"[Agent] 已入队控制命令: {params}")

    def _on_direct_command(self, params: dict):
        """Handle direct commands from quick buttons (no parser/LLM)."""
        params = dict(params)
        # ── query type ──────────────────────────────────────
        if params.get("type") == "query":
            result = self._handle_direct_query(params["query"])
            self.agent_panel.show_response(result)
            return

        # ── control type ────────────────────────────────────
        fixed_scene_id = self._fixed_scene_from_params(params)
        if fixed_scene_id:
            if self._start_fixed_scene(fixed_scene_id):
                try:
                    self._on_agent_set_params(params)
                except ValueError:
                    pass
                scene = self._fixed_sequence["scenarios"][fixed_scene_id]
                self.agent_panel.show_response(
                    scene.get("narration", scene["name"])
                )
                return

        if "power_delta" in params:
            delta = params.pop("power_delta")
            try:
                current = float(self.lbl_pwr.text().replace("dB", "").strip())
            except (ValueError, AttributeError):
                current = 0.0
            params["power_db"] = max(0, min(20, current + delta))
        try:
            self._on_agent_set_params(params)
            self.agent_panel.show_response(
                "已执行: " + json.dumps(params, ensure_ascii=False))
        except ValueError as e:
            self.agent_panel.show_error(str(e))

    def _handle_direct_query(self, query: str) -> str:
        """Execute a query locally and return formatted result."""
        if not self.agent_brain:
            return "Agent未就绪"
        tools = self.agent_brain.tools
        if query == "situation":
            data = json.loads(tools.handle_analyze_situation({}))
            return self._format_situation_report(data)
        elif query == "system":
            data = json.loads(tools.handle_query_system_info({}))
            return self._format_system_info(data)
        elif query == "channels":
            data = json.loads(tools.handle_get_channel_map_history({"num_frames": 30}))
            if "error" in data:
                return data["error"]
            lines = [f"**信道状态摘要 (近{data.get('帧数', '?')}帧)**\n"]
            for ch in data.get("逐信道摘要", []):
                lines.append("  信道{}: {}".format(ch.get("信道", "?"), ch.get("主要状态", "未知")))
            return "\n".join(lines)
        elif query == "identity":
            return self.agent_brain.parser.try_domain_qa("你是什么模型") or "应急频谱管控智能 Agent"
        return "未知查询: " + query

    @staticmethod
    def _format_situation_report(data: dict) -> str:
        lines = ["**态势研判**\n"]
        feats = data.get("信道特征", [])
        trend = data.get("趋势分析", {})
        coverage = data.get("干扰覆盖", {})

        if trend:
            snr = trend.get("SNR当前", "?")
            change = trend.get("SNR变化", "未知")
            power = trend.get("功率当前", "?")
            bw = trend.get("带宽当前", "?")
            lines.append("态势: SNR {} dB，{}；当前功率 {} dB，估计带宽 {} MHz。".format(
                snr, change, power, bw))
        if coverage:
            lines.append("覆盖: 被干扰信道 {}/{}，用户占用信道 {}。".format(
                coverage.get("被干扰信道数", "?"),
                coverage.get("总信道数", "?"),
                coverage.get("用户占用信道数", "?")))
        if feats:
            ranked = sorted(
                feats,
                key=lambda ch: (ch.get("占用度", 0), ch.get("干扰水平", 0)),
                reverse=True,
            )
            lines.append("\n重点信道:")
            for ch in ranked[:3]:
                lines.append("  信道{}: 占用{:.2f} 干扰{:.2f} SNR指标{:.2f}".format(
                    ch.get("信道", "?"),
                    ch.get("占用度", 0),
                    ch.get("干扰水平", 0),
                    ch.get("SNR指标", 0)))
        if not trend and not coverage and not feats:
            lines.append("暂无足够遥测数据，等待 MATLAB 后端上报频谱与信道特征。")
        lines.append("\n建议: 应急演示中可使用“切频到信道N + 多音/宽带 + 功率档位 + 带宽”组合指令快速构造极端干扰场景。")
        return "\n".join(lines)

    @staticmethod
    def _format_system_info(data: dict) -> str:
        lines = ["**系统信息 / 当前配置**\n"]
        state = data.get("系统运行状态")
        if state:
            lines.append("运行状态: {}".format(state))
        for k, v in data.get("当前配置", {}).items():
            lines.append("  {}: {}".format(k, v))
        overview = data.get("频谱概况", {})
        if overview:
            lines.append("\n频谱概况:")
            for k, v in overview.items():
                lines.append("  {}: {}".format(k, v))
        return "\n".join(lines)

    def _apply_kpi(self, params: dict):
        """Update KPI cards instantly from command params."""
        if "channel_idx" in params:
            self.lbl_ch.setText("信道 " + str(params["channel_idx"] + 1))
        if "power_db" in params:
            self.lbl_pwr.setText("{:.1f} dB".format(params["power_db"]))
        if "bw_mhz" in params:
            self.lbl_bw.setText("{} MHz".format(params["bw_mhz"]))
        if "waveform_mode" in params:
            self.lbl_mode.setText("宽带噪声" if params["waveform_mode"] == 0 else "多音")

    def _build_channel_map_image(self):
        """Build a stable channel map image with 1 MHz visual bins.

        Coordinate convention:
        - tick labels 1..N are the channel center-frequency lines
        - adjacent channel-center lines are 2 MHz apart
        - each channel is split into two 1 MHz visual bins
        - jammer coverage is centered on the selected channel center line
        - overflow beyond the left/right visible boundary is clipped, not shifted
        """
        base = np.stack(list(self.channel_history), axis=0)  # frames x channels
        cols = self.num_channels * self.map_bins_per_channel
        rendered = np.zeros((base.shape[0], cols), dtype=np.int32)

        for col in range(cols):
            ch = min(self.num_channels - 1, col // self.map_bins_per_channel)
            rendered[:, col] = base[:, ch]

        for row, action in enumerate(self.action_history):
            if float(action.get("power_db", 0.0)) <= 0.0:
                continue
            ch_idx = max(0, min(self.num_channels - 1, int(action.get("channel_idx", 0))))
            bw_mhz = float(action.get("bw_mhz", 2))
            affected_count = max(1, int(round(bw_mhz / self.channel_width_mhz)))
            affected_count = min(self.num_channels, affected_count)
            affected_start = max(0, min(self.num_channels - affected_count, ch_idx - affected_count // 2))
            affected_end = affected_start + affected_count - 1

            for col in range(cols):
                ch = min(self.num_channels - 1, col // self.map_bins_per_channel)
                if affected_start <= ch <= affected_end:
                    # State transition rules to preserve overlap (black):
                    #   idle(0) + jammer → jammed(2) = red
                    #   user(1) + jammer → overlap(3)  = black
                    #   overlap(3)       → stay 3       = black
                    existing = rendered[row, col]
                    if existing == 1:
                        rendered[row, col] = 3  # user in jam zone → black
                    elif existing == 0:
                        rendered[row, col] = 2  # idle hit by jammer → red

        return rendered.T

    @staticmethod
    def _validate_command_params(params: dict) -> str | None:
        if "channel_idx" in params and not 0 <= int(params["channel_idx"]) <= 9:
            return "信道必须在 1-10 范围内。"
        if "power_db" in params and not 0 <= float(params["power_db"]) <= 20:
            return "功率必须在 0-20 dB 范围内。"
        if "bw_mhz" in params and int(params["bw_mhz"]) not in (2, 4, 6, 8, 20):
            return "带宽必须为 2/4/6/8/20 MHz。"
        if "waveform_mode" in params and int(params["waveform_mode"]) not in (0, 1):
            return "干扰模式必须为宽带噪声或多音。"
        return None

    def _push_state_to_agent(self):
        if not self.agent_brain:
            return
        system_state = {}
        if self.lbl_ch.text() != "--":
            system_state["当前信道"] = self.lbl_ch.text()
        if self.lbl_pwr.text() != "-- dB":
            system_state["发射功率"] = self.lbl_pwr.text()
        if self.lbl_bw.text() != "-- MHz":
            system_state["干扰带宽"] = self.lbl_bw.text()
        if self.lbl_mode.text() != "--":
            system_state["干扰模式"] = self.lbl_mode.text()
        if self.lbl_snr.text() != "-- dB":
            system_state["信噪比"] = self.lbl_snr.text()
        if self.lbl_time.text() != "--":
            system_state["最新更新"] = self.lbl_time.text()

        self.agent_brain.update_state(
            system_state=system_state,
            snr_history=list(self.snr_history),
            bw_history=list(self.bw_history),
            power_history=list(self.power_history),
            channel_map_history=list(self.channel_history),
        )

    # ═══════════════════════════════════════════════════════════════
    # Telemetry handling
    # ═══════════════════════════════════════════════════════════════

    def _init_fixed_sequence_player(self):
        # When demo_ui_driver.py pushes external telemetry (TCP 5555),
        # the fixed-sequence player would conflict.  Honour an env var to
        # skip it so the three-UIs demo is driven from a single source.
        if os.environ.get("JAMMER_DISABLE_FIXED_SEQ", "").strip() in ("1", "true", "yes"):
            print("[FixedSequence] disabled by JAMMER_DISABLE_FIXED_SEQ")
            self.conn_badge.setText("外部遥测模式  127.0.0.1:5555")
            return
        seq_path = self._fixed_sequence_path()
        if not seq_path.exists():
            print(f"[FixedSequence] not found: {seq_path}")
            return
        try:
            self._fixed_sequence = json.loads(seq_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[FixedSequence] load failed: {e}")
            self._fixed_sequence = None
            return
        scenarios = self._fixed_sequence.get("scenarios", {})
        if not scenarios:
            print("[FixedSequence] no scenarios in file")
            return
        self._fixed_timer = QtCore.QTimer(self)
        self._fixed_timer.timeout.connect(self._tick_fixed_sequence)
        self._fixed_timer.start(int(float(self._fixed_sequence.get("frame_interval_s", 0.12)) * 1000))
        self._start_initial_fixed_environment()
        self.conn_badge.setText("本地随机背景  " + seq_path.name)
        print(f"[FixedSequence] loaded: {seq_path}")

    @staticmethod
    def _fixed_sequence_path() -> Path:
        env_path = os.environ.get("SPECTRUM_FIXED_SEQUENCE")
        if env_path:
            return Path(env_path)
        repo_root = Path(__file__).resolve().parents[3]
        return repo_root / "simulation_suite" / "outputs" / "fixed_link_sequence.json"

    @staticmethod
    def _fixed_scene_from_params(params: dict) -> str | None:
        if (
            int(params.get("channel_idx", -1)) == 5
            and int(params.get("waveform_mode", -1)) == 0
            and float(params.get("power_db", -1)) == 15.0
            and int(params.get("bw_mhz", -1)) == 20
        ):
            return "base_station_outage"
        if (
            int(params.get("channel_idx", -1)) == 5
            and int(params.get("waveform_mode", -1)) == 1
            and float(params.get("power_db", -1)) == 10.0
            and int(params.get("bw_mhz", -1)) == 4
        ):
            return "rescue_congestion"
        if (
            int(params.get("channel_idx", -1)) == 7
            and int(params.get("waveform_mode", -1)) == 0
            and float(params.get("power_db", -1)) == 20.0
            and int(params.get("bw_mhz", -1)) == 8
        ):
            return "uav_video_pressure"
        return None

    def _start_fixed_scene(self, scene_id: str) -> bool:
        if not self._fixed_sequence:
            return False
        scenarios = self._fixed_sequence.get("scenarios", {})
        if scene_id not in scenarios:
            return False
        self._fixed_scene_id = scene_id
        self._fixed_frame_idx = 0
        scene = scenarios[scene_id]
        action = scene["jammer"]
        self.conn_badge.setText("固定序列场景  " + scene.get("short_name", scene_id))
        self._apply_kpi({
            "channel_idx": action["channel_idx"],
            "power_db": action["power_db"],
            "bw_mhz": action["bw_mhz"],
            "waveform_mode": action["waveform_mode"],
        })
        return True

    def _start_initial_fixed_environment(self):
        if not self._fixed_sequence:
            return False
        initial = self._fixed_sequence.get("initial_environment")
        if not initial:
            return False
        self._fixed_scene_id = "__initial__"
        self._fixed_frame_idx = 0
        action = initial["jammer"]
        self.conn_badge.setText("随机背景电磁环境")
        self._apply_kpi({
            "channel_idx": action["channel_idx"],
            "power_db": action["power_db"],
            "bw_mhz": action["bw_mhz"],
            "waveform_mode": action["waveform_mode"],
        })
        return True

    def _tick_fixed_sequence(self):
        if not self._fixed_sequence or not self._fixed_scene_id:
            return
        if self._fixed_scene_id == "__initial__":
            scene = self._fixed_sequence.get("initial_environment")
        else:
            scene = self._fixed_sequence["scenarios"].get(self._fixed_scene_id)
        if not scene:
            return
        frames = scene.get("frames", [])
        if not frames:
            return
        frame = frames[self._fixed_frame_idx % len(frames)]
        self._fixed_frame_idx += 1
        pkt = {
            "type": "telemetry",
            "version": "2.0",
            "session_id": "fixed_" + self._fixed_scene_id,
            "seq": self._fixed_frame_idx,
            "timestamp_ms": int(datetime.now().timestamp() * 1000),
            "telemetry": frame["telemetry"],
            "action": frame["action"],
            "rl_meta": {
                "policy": "fixed_precomputed_link_sequence",
                "value": 1.0,
                "latency_ms": 0,
            },
        }
        self.handle_packet(pkt)

    def closeEvent(self, event):
        if hasattr(self, "_fixed_timer"):
            self._fixed_timer.stop()
        self.server.stop()
        self.cmd_bridge.stop()
        self.server.join(timeout=2.0)
        self.cmd_bridge.join(timeout=2.0)
        super().closeEvent(event)

    @QtCore.pyqtSlot(dict)
    def handle_packet(self, pkt: dict):
        telemetry = pkt.get("telemetry", {})
        action = pkt.get("action", {})
        rl_meta = pkt.get("rl_meta", {})
        seq = pkt.get("seq", "-")

        snr = float(telemetry.get("snr_est", 0.0))
        bw_est = float(telemetry.get("bw_est", 0.0)) / 1e6
        tx_state = telemetry.get("tx_state", "running")
        spectrum = np.asarray(telemetry.get("spectrum", []), dtype=float)
        freq_axis = np.asarray(telemetry.get("freq_axis_ghz", []), dtype=float)
        waveform = np.asarray(telemetry.get("jam_waveform_abs", []), dtype=float)
        ch_map = np.asarray(telemetry.get("channel_map", []), dtype=np.int32)
        ch_feat = np.asarray(telemetry.get("channel_features", []), dtype=float)

        channel_idx = int(action.get("channel_idx", 0))
        power_db = float(action.get("power_db", 0.0))
        bw_mhz = int(action.get("bw_mhz", 4))
        waveform_mode = int(action.get("waveform_mode", 0))

        # KPI cards
        self.conn_badge.setText("已连接  127.0.0.1:5555")

        self.lbl_ch.setText(f"信道 {channel_idx + 1}")
        self.lbl_pwr.setText(f"{power_db:.1f} dB")
        self.lbl_bw.setText(f"{bw_mhz} MHz")
        self.lbl_mode.setText("宽带噪声" if waveform_mode == 0 else "多音")
        self.lbl_snr.setText(f"{snr:.2f} dB")
        self.lbl_time.setText(datetime.now().strftime("%H:%M:%S"))

        # Spectrum
        if spectrum.size > 0:
            self.spectrum_curve.setData(
                freq_axis if freq_axis.size == spectrum.size else None, spectrum
            )
        # Waveform
        if waveform.size > 0:
            self.wave_curve.setData(waveform)

        # Channel map
        if ch_map.size > 0:
            if ch_map.size != self.num_channels:
                self.num_channels = int(ch_map.size)
                self.channel_history = deque(
                    [np.zeros(self.num_channels, dtype=np.int32) for _ in range(self.max_history)],
                    maxlen=self.max_history,
                )
                self.action_history = deque(
                    [{"channel_idx": channel_idx, "bw_mhz": bw_mhz, "power_db": power_db}
                     for _ in range(self.max_history)],
                    maxlen=self.max_history,
                )
                bottom_axis = self.map_plot.getAxis("bottom")
                bottom_ticks = [(i + 1, str(i + 1)) for i in range(self.num_channels)]
                bottom_axis.setTicks([bottom_ticks])
                self.map_plot.setXRange(0.5, self.num_channels + 0.5, padding=0)
                self.map_img.setRect(QtCore.QRectF(0.5, 0, self.num_channels, self.max_history))

            self.channel_history.append(np.asarray(ch_map, dtype=np.int32))
            self.action_history.append({
                "channel_idx": channel_idx,
                "bw_mhz": bw_mhz,
                "power_db": power_db,
            })
            mat = self._build_channel_map_image()
            self.map_img.setImage(mat, autoLevels=False)
            self.map_img.setRect(QtCore.QRectF(0.5, 0, self.num_channels, self.max_history))

        # Trend history
        self.snr_history.append(snr)
        self.bw_history.append(bw_est)
        self.power_history.append(power_db)
        x = np.arange(len(self.snr_history))
        self.snr_curve.setData(x, np.array(self.snr_history))
        self.bw_curve.setData(x, np.array(self.bw_history))
        self.power_curve.setData(x, np.array(self.power_history))

        # Event log
        self.log_widget.append_log([
            datetime.now().strftime("%H:%M:%S"),
            seq,
            channel_idx + 1,
            f"{power_db:.1f}",
            bw_mhz,
            "噪声" if waveform_mode == 0 else "多音",
        ])

        self._push_state_to_agent()


class AgentWorker(QtCore.QThread):
    response_ready = QtCore.pyqtSignal(int, str)
    error_ready = QtCore.pyqtSignal(int, str)

    def __init__(self, brain: AgentBrain, message: str, request_id: int):
        super().__init__()
        self.brain = brain
        self.message = message
        self.request_id = request_id

    def run(self):
        try:
            result = self.brain.send_message(self.message)
            if not self.isInterruptionRequested():
                self.response_ready.emit(self.request_id, result)
        except Exception as e:
            if not self.isInterruptionRequested():
                self.error_ready.emit(self.request_id, str(e))


def main():
    app = QtWidgets.QApplication(sys.argv)
    font = QtGui.QFont("Microsoft YaHei UI")
    font.setPointSize(10)
    app.setFont(font)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
