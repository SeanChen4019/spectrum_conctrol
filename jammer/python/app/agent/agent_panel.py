"""
PyQt5 chat panel widget for interacting with the interference simulator agent.
"""
from PyQt5 import QtCore, QtGui, QtWidgets

MAX_BLOCKS = 500


class AgentPanel(QtWidgets.QWidget):
    """Chat panel with message display, loading animation, and quick commands."""

    send_message = QtCore.pyqtSignal(str)
    direct_command = QtCore.pyqtSignal(dict)  # bypass parser/LLM entirely
    clear_requested = QtCore.pyqtSignal()
    stop_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dot_timer = QtCore.QTimer(self)
        self._dot_timer.timeout.connect(self._tick_dots)
        self._dot_count = 0
        self._pending = False
        self._build_ui()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # ── Header ─────────────────────────────────────────────
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("应急频谱 Agent")
        title.setObjectName("AgentTitle")

        self.status_label = QtWidgets.QLabel("本地规则就绪")
        self.status_label.setObjectName("AgentStatus")

        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.status_label)

        new_chat_btn = QtWidgets.QPushButton("重置")
        new_chat_btn.setObjectName("AgentClearBtn")
        new_chat_btn.setFixedWidth(60)
        new_chat_btn.clicked.connect(self._on_new_chat)
        header.addWidget(new_chat_btn)

        clear_btn = QtWidgets.QPushButton("清屏")
        clear_btn.setObjectName("AgentClearBtn")
        clear_btn.setFixedWidth(50)
        clear_btn.clicked.connect(self._on_clear_screen)
        header.addWidget(clear_btn)

        layout.addLayout(header)

        scenario = QtWidgets.QFrame()
        scenario.setObjectName("ScenarioBanner")
        scenario_layout = QtWidgets.QVBoxLayout(scenario)
        scenario_layout.setContentsMargins(10, 8, 10, 8)
        scenario_layout.setSpacing(2)
        scene_title = QtWidgets.QLabel("灾后极端电磁环境模拟")
        scene_title.setObjectName("ScenarioTitle")
        scene_desc = QtWidgets.QLabel("面向无人机图传保底回传：干扰构造、态势研判与参数闭环")
        scene_desc.setObjectName("ScenarioDesc")
        scenario_layout.addWidget(scene_title)
        scenario_layout.addWidget(scene_desc)
        layout.addWidget(scenario)

        # ── Message area ───────────────────────────────────────
        self.message_area = QtWidgets.QTextEdit()
        self.message_area.setReadOnly(True)
        self.message_area.setObjectName("AgentMessageArea")
        self.message_area.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOn)
        layout.addWidget(self.message_area, stretch=1)

        # ── Quick commands: query row ───────────────────────
        query_row = QtWidgets.QHBoxLayout()
        query_row.setSpacing(4)
        layout.addLayout(query_row)
        for label, params in [
            ("态势研判", {"type": "query", "query": "situation"}),
            ("当前配置", {"type": "query", "query": "system"}),
            ("信道状态", {"type": "query", "query": "channels"}),
            ("模型身份", {"type": "query", "query": "identity"}),
        ]:
            btn = QtWidgets.QPushButton(label)
            btn.setObjectName("QuickCmdBtn")
            btn.setFixedHeight(28)
            btn.clicked.connect(lambda checked, p=params: self.direct_command.emit(dict(p)))
            query_row.addWidget(btn)

        # ── Emergency scenario presets ──────────────────────
        scenario_row = QtWidgets.QHBoxLayout()
        scenario_row.setSpacing(4)
        layout.addLayout(scenario_row)
        for label, params, tip in [
            (
                "基站退服",
                {"channel_idx": 2, "waveform_mode": 0, "power_db": 15.0, "bw_mhz": 8},
                "模拟灾后局部基站退服与宽带噪声抬升",
            ),
            (
                "救援拥塞",
                {"channel_idx": 5, "waveform_mode": 1, "power_db": 10.0, "bw_mhz": 4},
                "模拟多套救援图传/专网设备造成的多音频点冲突",
            ),
            (
                "链路压测",
                {"channel_idx": 7, "waveform_mode": 0, "power_db": 20.0, "bw_mhz": 6},
                "对无人机视频回传链路施加高功率干扰压力",
            ),
        ]:
            btn = QtWidgets.QPushButton(label)
            btn.setObjectName("ScenarioPresetBtn")
            btn.setToolTip(tip)
            btn.setFixedHeight(30)
            btn.clicked.connect(lambda checked, p=params: self.direct_command.emit(dict(p)))
            scenario_row.addWidget(btn)

        # ── Quick commands: channel row ─────────────────────
        for start, end, prefix in [(1, 5, "目标"), (6, 10, "信道")]:
            channel_row = QtWidgets.QHBoxLayout()
            channel_row.setSpacing(4)
            layout.addLayout(channel_row)
            channel_label = QtWidgets.QLabel(prefix)
            channel_label.setObjectName("MiniLabel")
            channel_row.addWidget(channel_label)
            for ch in range(start, end + 1):
                btn = QtWidgets.QPushButton(str(ch))
                btn.setObjectName("ChannelBtn")
                btn.setToolTip(f"切换到信道 {ch}")
                btn.setFixedSize(34, 28)
                btn.clicked.connect(lambda checked, c=ch: self.direct_command.emit({"channel_idx": c - 1}))
                channel_row.addWidget(btn)
            channel_row.addStretch(1)

        # ── Quick commands: control row (direct, no LLM) ────
        ctrl_row = QtWidgets.QHBoxLayout()
        ctrl_row.setSpacing(4)
        layout.addLayout(ctrl_row)
        for label, params in [
            ("宽带压制", {"waveform_mode": 0}),
            ("多音点扰", {"waveform_mode": 1}),
            ("功率+5", {"power_delta": +5}),
            ("功率-5", {"power_delta": -5}),
        ]:
            btn = QtWidgets.QPushButton(label)
            btn.setObjectName("QuickCmdBtn")
            btn.setFixedHeight(28)
            btn.clicked.connect(lambda checked, p=params: self.direct_command.emit(dict(p)))
            ctrl_row.addWidget(btn)

        preset_row = QtWidgets.QHBoxLayout()
        preset_row.setSpacing(4)
        layout.addLayout(preset_row)
        for label, params in [
            ("低功率", {"power_db": 5.0}),
            ("中功率", {"power_db": 10.0}),
            ("高功率", {"power_db": 15.0}),
            ("满功率", {"power_db": 20.0}),
        ]:
            btn = QtWidgets.QPushButton(label)
            btn.setObjectName("QuickCmdBtn")
            btn.setFixedHeight(28)
            btn.clicked.connect(lambda checked, p=params: self.direct_command.emit(dict(p)))
            preset_row.addWidget(btn)

        bw_row = QtWidgets.QHBoxLayout()
        bw_row.setSpacing(4)
        layout.addLayout(bw_row)
        for label, params in [
            ("2MHz", {"bw_mhz": 2}),
            ("4MHz", {"bw_mhz": 4}),
            ("6MHz", {"bw_mhz": 6}),
            ("8MHz", {"bw_mhz": 8}),
        ]:
            btn = QtWidgets.QPushButton(label)
            btn.setObjectName("QuickCmdBtn")
            btn.setFixedHeight(28)
            btn.clicked.connect(lambda checked, p=params: self.direct_command.emit(dict(p)))
            bw_row.addWidget(btn)

        # ── Input area ─────────────────────────────────────────
        input_layout = QtWidgets.QHBoxLayout()
        input_layout.setSpacing(6)

        self.input_field = QtWidgets.QLineEdit()
        self.input_field.setObjectName("AgentInput")
        self.input_field.setPlaceholderText("例：模拟救援拥塞；切频到信道3，多音，功率15dB，带宽6MHz")
        self.input_field.returnPressed.connect(self._on_send)

        self.stop_btn = QtWidgets.QPushButton("停止")
        self.stop_btn.setObjectName("AgentClearBtn")
        self.stop_btn.setFixedWidth(50)
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.setVisible(False)

        self.send_btn = QtWidgets.QPushButton("下发")
        self.send_btn.setObjectName("AgentSendBtn")
        self.send_btn.setFixedWidth(60)
        self.send_btn.clicked.connect(self._on_send)

        input_layout.addWidget(self.input_field)
        input_layout.addWidget(self.stop_btn)
        input_layout.addWidget(self.send_btn)
        layout.addLayout(input_layout)

    # ── Input handling ──────────────────────────────────────────

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Return and (
            event.modifiers() & QtCore.Qt.ControlModifier
        ):
            self._on_send()
        else:
            super().keyPressEvent(event)

    def _on_send(self):
        text = self.input_field.text().strip()
        if not text:
            return
        self.input_field.clear()
        self._append_message("user", text)
        self._start_loading()
        self._pending = True
        self.send_btn.setEnabled(False)
        self.input_field.setEnabled(False)
        self.stop_btn.setVisible(True)
        self.send_message.emit(text)

    def _on_stop(self):
        if self._pending:
            self._pending = False
            self._stop_loading()
            self._append_message("error", "请求已被用户取消。")
            self.send_btn.setEnabled(True)
            self.input_field.setEnabled(True)
            self.stop_btn.setVisible(False)
            self.stop_requested.emit()

    def _reset_pending_state(self):
        self._pending = False
        self._stop_loading()
        self.send_btn.setEnabled(True)
        self.input_field.setEnabled(True)
        self.stop_btn.setVisible(False)

    # ── Response handling ───────────────────────────────────────

    def show_response(self, response_text: str):
        self._pending = False
        self._stop_loading()
        self._append_message("agent", response_text)
        self.send_btn.setEnabled(True)
        self.input_field.setEnabled(True)
        self.stop_btn.setVisible(False)
        self.input_field.setFocus()

    def show_error(self, error_text: str):
        self._pending = False
        self._stop_loading()
        self._append_message("error", error_text)
        self.send_btn.setEnabled(True)
        self.input_field.setEnabled(True)
        self.stop_btn.setVisible(False)

    # ── Chat management ─────────────────────────────────────────

    def _on_new_chat(self):
        self.stop_requested.emit()
        self._reset_pending_state()
        self.message_area.clear()
        self.clear_requested.emit()
        self._append_message("agent", "新对话已开始。请随时输入指令控制干扰模拟器。")

    def _on_clear_screen(self):
        self.stop_requested.emit()
        self._reset_pending_state()
        self.message_area.clear()

    # ── Loading animation ──────────────────────────────────────

    def _start_loading(self):
        self._dot_count = 0
        self._tick_dots()
        self._dot_timer.start(400)

    def _stop_loading(self):
        self._dot_timer.stop()
        self.status_label.setText("本地规则就绪")
        self.status_label.setStyleSheet("color: #6ee7b7;")

    def _tick_dots(self):
        self._dot_count = (self._dot_count + 1) % 4
        dots = "." * self._dot_count if self._dot_count > 0 else ""
        self.status_label.setText(f"思考中{dots}")
        self.status_label.setStyleSheet("color: #ffb84d; font-weight: bold;")

    def set_connected(self, connected: bool):
        if connected:
            self.status_label.setText("本地+LLM就绪")
            self.status_label.setStyleSheet("color: #6ee7b7;")
        else:
            self.status_label.setText("本地规则就绪")
            self.status_label.setStyleSheet("color: #6ee7b7;")

    # ── Message formatting ─────────────────────────────────────

    def _append_message(self, role: str, text: str):
        cursor = self.message_area.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)

        if role == "user":
            label, color = "你", "#66d9ef"
        elif role == "agent":
            label, color = "Agent", "#4dff88"
        elif role == "error":
            label, color = "系统", "#ff5c73"
        else:
            label, color = role, "#98a6b8"

        fmt = QtGui.QTextCharFormat()
        fmt.setForeground(QtGui.QColor(color))
        fmt.setFontWeight(QtGui.QFont.Bold)
        cursor.insertText(f"\n[{label}] ", fmt)

        text_fmt = QtGui.QTextCharFormat()
        text_fmt.setForeground(QtGui.QColor("#e8eef5"))
        cursor.insertText(f"{text}\n", text_fmt)

        sep_fmt = QtGui.QTextCharFormat()
        sep_fmt.setForeground(QtGui.QColor("#243244"))
        cursor.insertText("─" * 50 + "\n", sep_fmt)

        self.message_area.setTextCursor(cursor)
        self._prune_if_needed()

    def _prune_if_needed(self):
        doc = self.message_area.document()
        if doc.blockCount() > MAX_BLOCKS:
            root = doc.rootFrame()
            block = root.firstChild()
            removed = 0
            while block and removed < 100:
                cursor = QtGui.QTextCursor(block)
                cursor.select(QtGui.QTextCursor.BlockUnderCursor)
                cursor.removeSelectedText()
                cursor.deleteChar()
                block = root.firstChild()
                removed += 1
