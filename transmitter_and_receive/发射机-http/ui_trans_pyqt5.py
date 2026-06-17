import sys
import os
import time
import numpy as np
import threading
import cv2
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QGridLayout, QLabel, QGroupBox, QPushButton, QLineEdit, QSplitter,
                             QScrollArea, QSizePolicy)
from PyQt5.QtCore import Qt, pyqtSignal, QThread, QTimer, QSize
from PyQt5.QtGui import QPixmap, QImage
import pyqtgraph as pg
from flask import Flask, request, jsonify
from flask_cors import CORS
import logging

# AI Agent 已替换为本地剧本模式，无需外部 API

app = Flask(__name__)
CORS(app)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

data_mutex = threading.Lock()
decision_mutex = threading.Lock()

# 数据存储 (MATLAB → UI)
data_store = {
    'has_data': False,
    'tx_spec': {'freq': [], 'amp': []},
    'tx_time': {'time': [], 'amp': []},
    'rx_const': {'i': [], 'q': []},
    'rx_time': {'time': [], 'amp': []},
    'status': {},
    'sending_image': None
}

# 发射任务决策存储 (UI/AI → MATLAB)
tx_decision_store = {
    'tx_mode': 1,              # 1=仅图像, 2=仅视频, 3=图像+视频, 4=文本
    'image_file': 'p2.jpg',
    'video_file': '视频.mp4',
    'text_string': 'Hello World! 这是一段通过USRP无线传输的测试文本。',
    'needs_update': False,
    'decision_version': 0,
    'command_source': '',      # 'ai_agent' / 'ui'
    'command_description': '',
    'task_pending': False,
    'pending_task_type': '',
    'available_files': [],
}

# 旧版控制存储 (兼容旧 MATLAB 文本切换)
control_store = {
    'apply': False,
    'new_str': ''
}

_data_recv_count = 0
@app.route('/api/data', methods=['POST'])
def receive_data():
    global _data_recv_count
    _data_recv_count += 1
    data = request.json
    data_mutex.acquire()
    try:
        data_store['has_data'] = True
        if 'tx_spec' in data: data_store['tx_spec'] = data['tx_spec']
        if 'tx_time' in data: data_store['tx_time'] = data['tx_time']
        if 'rx_const' in data: data_store['rx_const'] = data['rx_const']
        if 'rx_time' in data: data_store['rx_time'] = data['rx_time']
        if 'status' in data: data_store['status'] = data['status']
        if 'sending_image' in data: data_store['sending_image'] = data['sending_image']
    finally:
        data_mutex.release()
    return jsonify({'status': 'success'})

@app.route('/api/control', methods=['GET'])
def get_control():
    """旧版兼容：MATLAB 获取文本修改指令"""
    decision_mutex.acquire()
    try:
        res = {'apply': control_store['apply'], 'str': control_store['new_str']}
        control_store['apply'] = False
        return jsonify(res)
    finally:
        decision_mutex.release()

@app.route('/api/tx_decision', methods=['GET'])
def get_tx_decision():
    """MATLAB 轮询发射任务变更"""
    decision_mutex.acquire()
    try:
        res = {
            'tx_mode': tx_decision_store['tx_mode'],
            'image_file': tx_decision_store['image_file'],
            'video_file': tx_decision_store['video_file'],
            'text_string': tx_decision_store['text_string'],
            'needs_update': tx_decision_store['needs_update'],
            'decision_version': tx_decision_store['decision_version'],
            'command_source': tx_decision_store['command_source'],
            'command_description': tx_decision_store['command_description'],
        }
        tx_decision_store['needs_update'] = False
        return jsonify(res)
    finally:
        decision_mutex.release()

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok'})

def run_flask():
    app.run(host='127.0.0.1', port=5001, debug=False, use_reloader=False, threaded=True)

class DataUpdateThread(QThread):
    data_ready = pyqtSignal(dict)
    def __init__(self):
        super().__init__()
        self.running = True

    def run(self):
        while self.running:
            data_mutex.acquire()
            has_new = data_store['has_data']
            if has_new:
                copy = {
                    'tx_spec': data_store['tx_spec'].copy(),
                    'tx_time': data_store['tx_time'].copy(),
                    'rx_const': data_store['rx_const'].copy(),
                    'rx_time': data_store['rx_time'].copy(),
                    'status': data_store['status'].copy(),
                    'sending_image': data_store['sending_image']
                }
                data_store['has_data'] = False
                data_mutex.release()
                self.data_ready.emit(copy)
                if _data_recv_count == 1:
                    tx_amp = np.array(copy['tx_spec'].get('amp', []))
                    print(f'[UI-DEBUG] 收到第1帧数据！频谱点数={len(tx_amp)} | 峰值={float(np.max(tx_amp)):.4f}')
                elif _data_recv_count <= 5 or _data_recv_count % 20 == 0:
                    tx_amp = np.array(copy['tx_spec'].get('amp', []))
                    print(f'[UI-DEBUG] 已收到{_data_recv_count}帧 | 频谱峰值={float(np.max(tx_amp)):.4f}')
            else:
                data_mutex.release()
            self.msleep(100)

    def stop(self):
        self.running = False
        self.wait()

class ChatWidget(QWidget):
    """AI Agent 对话组件 — 气泡式聊天"""
    send_message_signal = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题栏
        header = QWidget()
        header.setFixedHeight(36)
        header.setStyleSheet("background-color: #5b1a3a; border-radius: 8px 8px 0 0;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 0, 8, 0)
        title = QLabel('AI 发射控制助手')
        title.setStyleSheet("color: #f9a8d4; font-weight: bold; font-size: 10pt; background: transparent;")
        header_layout.addWidget(title)
        header_layout.addStretch()
        clear_btn = QPushButton('清空')
        clear_btn.setFixedSize(40, 22)
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet("""
            QPushButton { background-color: #334155; color: #94a3b8; border-radius: 4px; font-size: 9pt; }
            QPushButton:hover { background-color: #475569; color: #e2e8f0; }
        """)
        clear_btn.clicked.connect(self.clear_history)
        header_layout.addWidget(clear_btn)
        layout.addWidget(header)

        # 消息区
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setMinimumHeight(180)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet("""
            QScrollArea { border: none; background-color: #0f172a; }
            QScrollBar:vertical { border: none; background-color: #0f172a; width: 6px; margin: 0; }
            QScrollBar::handle:vertical { background-color: #475569; min-height: 30px; border-radius: 3px; }
            QScrollBar::handle:vertical:hover { background-color: #64748b; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
            QScrollBar:horizontal { height: 0; }
        """)
        self.msg_container = QWidget()
        self.msg_container.setStyleSheet("background-color: #0f172a;")
        self.msg_layout = QVBoxLayout(self.msg_container)
        self.msg_layout.setAlignment(Qt.AlignTop)
        self.msg_layout.setSpacing(6)
        self.msg_layout.setContentsMargins(10, 12, 10, 12)
        self.scroll_area.setWidget(self.msg_container)
        layout.addWidget(self.scroll_area, stretch=1)

        # 输入区
        input_widget = QWidget()
        input_widget.setStyleSheet("background-color: #1e293b; border-top: 1px solid #334155;")
        input_layout = QHBoxLayout(input_widget)
        input_layout.setContentsMargins(10, 8, 10, 8)
        input_layout.setSpacing(8)
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText('输入指令，如：切换到视频模式...')
        self.input_edit.setMinimumHeight(34)
        self.input_edit.setStyleSheet("""
            QLineEdit { background-color: #0f172a; color: #e2e8f0; border: 1px solid #334155;
                        border-radius: 8px; padding: 6px 12px; font-size: 10.5pt; }
            QLineEdit:focus { border-color: #f472b6; }
        """)
        self.input_edit.returnPressed.connect(self._on_send)
        input_layout.addWidget(self.input_edit)
        self.send_btn = QPushButton('发送')
        self.send_btn.setFixedSize(56, 34)
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setStyleSheet("""
            QPushButton { background-color: #f472b6; color: black; border-radius: 8px;
                          font-weight: bold; font-size: 10.5pt; }
            QPushButton:hover { background-color: #fbcfe8; }
            QPushButton:pressed { background-color: #ec4899; }
        """)
        self.send_btn.clicked.connect(self._on_send)
        input_layout.addWidget(self.send_btn)
        layout.addWidget(input_widget)

        # 欢迎语
        self.add_message('你好！我是面向应急保障的频谱智能管控系统的智能体，可以保障灾区与救援队伍间通信。', is_user=False)
        self.add_message('灾情牵动人心，如果你想快速与救援中心建立通信，你完全可以将通信任务交给我负责！', is_user=False)

    def _on_send(self):
        text = self.input_edit.text().strip()
        if text:
            self.input_edit.clear()
            self.send_message_signal.emit(text)

    def _remove_temp_message(self):
        cnt = self.msg_layout.count()
        if cnt > 0:
            item = self.msg_layout.itemAt(cnt - 1)
            w = item.widget()
            if w and getattr(w, '_is_temp', False):
                self.msg_layout.removeWidget(w)
                w.deleteLater()

    def add_message(self, text, is_user=True, is_temp=False):
        clean_text = text.replace('**', '').replace('*', '')

        row = QHBoxLayout()
        row.setSpacing(8)

        avatar = QLabel()
        avatar.setFixedSize(30, 30)
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        if is_user:
            avatar.setText('U')
            avatar.setStyleSheet("""
                QLabel { background-color: #3b82f6; color: white; border-radius: 15px;
                         font-weight: bold; font-size: 10pt; }
            """)
        else:
            if is_temp:
                avatar.setText('⋯')
                avatar.setStyleSheet("""
                    QLabel { background-color: #475569; color: #94a3b8; border-radius: 15px;
                             font-weight: bold; font-size: 10pt; }
                """)
            else:
                avatar.setText('AI')
                avatar.setStyleSheet("""
                    QLabel { background-color: #f472b6; color: white; border-radius: 15px;
                             font-weight: bold; font-size: 9pt; }
                """)

        bubble = QLabel(clean_text)
        bubble._is_temp = is_temp
        bubble.setWordWrap(True)
        bubble.setTextFormat(Qt.PlainText)
        bubble.setContentsMargins(12, 8, 12, 8)
        bubble.setMaximumWidth(280)
        bubble.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Minimum)

        if is_user:
            bubble.setStyleSheet("""
                QLabel { background-color: #1d4ed8; color: #eff6ff; border-radius: 14px 4px 14px 14px;
                         padding: 10px 14px; font-size: 10.5pt; }
            """)
            row.addStretch()
            row.addWidget(bubble)
            row.addWidget(avatar)
        else:
            if is_temp:
                bubble.setStyleSheet("""
                    QLabel { background-color: #334155; color: #64748b; border-radius: 4px 14px 14px 14px;
                         padding: 10px 14px; font-size: 10.5pt; font-style: italic; }
                """)
            else:
                bubble.setStyleSheet("""
                    QLabel { background-color: #1e293b; color: #cbd5e1; border-radius: 4px 14px 14px 14px;
                         padding: 10px 14px; font-size: 10.5pt; }
                """)
            row.addWidget(avatar)
            row.addWidget(bubble)
            row.addStretch()

        wrapper = QWidget()
        wrapper.setStyleSheet("background: transparent;")
        wrapper._is_temp = is_temp
        wrapper.setLayout(row)
        self.msg_layout.addWidget(wrapper)

        QTimer.singleShot(50, lambda: self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()
        ))

    def clear_history(self):
        while self.msg_layout.count():
            item = self.msg_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.add_message('对话已清空，请继续输入指令。', is_user=False)

class TransMainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle('全双工 SDR 发射机控制系统 (AI Agent)')
        self.setGeometry(100, 100, 1500, 900)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowSystemMenuHint | Qt.WindowMinMaxButtonsHint)

        self._last_label_texts = {}
        self._last_status = {}

        pg.setConfigOptions(antialias=True, background='#0f172a', foreground='#94a3b8')
        self.setStyleSheet("""
            QMainWindow { background-color: #0f172a; }
            QWidget { color: #e2e8f0; font-family: 'Microsoft YaHei'; font-size: 10pt; }
            QGroupBox { border: 1px solid #334155; border-radius: 8px; margin-top: 15px; font-weight: bold; color: #f472b6; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QLineEdit { background-color: #1e293b; border: 1px solid #475569; padding: 5px; color: white; }
            QPushButton { background-color: #f472b6; color: black; font-weight: bold; padding: 8px; border-radius: 4px; }
            QPushButton:hover { background-color: #fbcfe8; }
            QSplitter::handle { background-color: #334155; width: 2px; }
        """)

        # 副本系统（纯本地演示）
        self._tx_state = "IDLE"
        self._tx_timeline = []
        self._tx_frame = 0
        self._load_tx_timeline()

        # 播放定时器
        self._tx_timer = QTimer(self)
        self._tx_timer.timeout.connect(self._tx_play_tick)
        self._tx_timer.setInterval(120)

        # 视频播放器
        self._video_timer = QTimer(self)
        self._video_timer.timeout.connect(self._video_tick)
        self._video_playing = False
        self._video_frames = []
        self._video_frame_idx = 0

        print('TX 演示系统就绪（自驱动模式）')

        self.init_ui()

        # 初始状态显示
        if 'tx_task' in self.task_labels:
            self.task_labels['tx_task'].setText('图像传输')
        if 'tx_image' in self.task_labels:
            self.task_labels['tx_image'].setText('p2.jpg')
        if 'tx_video' in self.task_labels:
            self.task_labels['tx_video'].setText('救援现场.mp4')
        if 'tx_text' in self.task_labels:
            self.task_labels['tx_text'].setText('待输入')

        self.data_thread = DataUpdateThread()
        self.data_thread.data_ready.connect(self.update_ui)
        self.data_thread.start()
        threading.Thread(target=run_flask, daemon=True).start()

    def create_plot(self, title, xl, yl):
        p = pg.PlotWidget(title=title)
        p.showGrid(x=True, y=True, alpha=0.3)
        p.setLabel('bottom', xl)
        p.setLabel('left', yl)
        return p

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 标题栏
        self.title_bar = QWidget()
        self.title_bar.setFixedHeight(40)
        self.title_bar.setStyleSheet("background-color: #1e293b; border-bottom: 1px solid #334155;")
        title_bar_layout = QHBoxLayout(self.title_bar)
        title_bar_layout.setContentsMargins(10, 0, 10, 0)

        self.title_label = QLabel('全双工 SDR 发射机控制系统 (AI Agent)')
        self.title_label.setStyleSheet("color: #e2e8f0; font-weight: bold;")
        title_bar_layout.addWidget(self.title_label)
        title_bar_layout.addStretch()

        self.minimize_btn = QPushButton('−')
        self.minimize_btn.setFixedSize(30, 30)
        self.minimize_btn.setStyleSheet("QPushButton { background-color: #f59e0b; color: white; border-radius: 5px; font-weight: bold; }")
        self.minimize_btn.clicked.connect(self.showMinimized)
        title_bar_layout.addWidget(self.minimize_btn)

        self.maximize_btn = QPushButton('□')
        self.maximize_btn.setFixedSize(30, 30)
        self.maximize_btn.setStyleSheet("QPushButton { background-color: #10b981; color: white; border-radius: 5px; font-weight: bold; }")
        self.maximize_btn.clicked.connect(self.toggle_maximize)
        title_bar_layout.addWidget(self.maximize_btn)

        self.close_btn = QPushButton('✕')
        self.close_btn.setFixedSize(30, 30)
        self.close_btn.setStyleSheet("QPushButton { background-color: #ef4444; color: white; border-radius: 5px; font-weight: bold; }")
        self.close_btn.clicked.connect(self.close)
        title_bar_layout.addWidget(self.close_btn)

        main_layout.addWidget(self.title_bar)

        # 内容区
        content_widget = QWidget()
        layout = QHBoxLayout(content_widget)

        # === 左侧面板 ===
        left = QWidget()
        left.setFixedWidth(310)
        vbox = QVBoxLayout(left)

        # 发射任务状态
        task_gp = QGroupBox('发射任务状态')
        task_grid = QGridLayout()
        self.task_labels = {}
        task_items = [
            ('当前任务:', 'tx_task'),
            ('图片文件:', 'tx_image'),
            ('视频文件:', 'tx_video'),
            ('文本内容:', 'tx_text'),
        ]
        for r, (l, k) in enumerate(task_items):
            lbl = QLabel(l)
            lbl.setFixedWidth(70)
            task_grid.addWidget(lbl, r, 0)
            val = QLabel('--')
            val.setMaximumWidth(200)
            val.setWordWrap(True)
            val.setStyleSheet("color: #f472b6; font-weight: bold;")
            task_grid.addWidget(val, r, 1)
            self.task_labels[k] = val
        task_gp.setLayout(task_grid)
        vbox.addWidget(task_gp)

        # 发送端配置信息
        tx_gp = QGroupBox('发送端配置信息')
        tx_grid = QGridLayout()
        self.tx_labels = {}
        tx_items = [('发送有效指示:', 'tx_valid'), ('系统调制方式:', 'tx_mod'), ('发送模式:', 'tx_mode'),
                    ('当前载波频率:', 'tx_carrier'), ('基带采样率:', 'tx_samp'), ('当前发射增益:', 'tx_gain')]
        for r, (l, k) in enumerate(tx_items):
            lbl = QLabel(l)
            lbl.setFixedWidth(100)
            tx_grid.addWidget(lbl, r, 0)
            val = QLabel('--')
            val.setMaximumWidth(180)
            val.setWordWrap(True)
            val.setStyleSheet("color: #38bdf8; font-weight: bold;")
            tx_grid.addWidget(val, r, 1)
            self.tx_labels[k] = val
        tx_gp.setLayout(tx_grid)
        vbox.addWidget(tx_gp)

        # 指令快捷输入 — 文本输入框
        ctrl_gp = QGroupBox('指令输入')
        cv = QVBoxLayout()
        self.cmd_edit = QLineEdit()
        self.cmd_edit.setPlaceholderText("请输入灾区情况描述...")
        self.cmd_edit.setStyleSheet("background-color: #0f172a; color: #e2e8f0; border: 1px solid #334155; border-radius: 6px; padding: 6px 10px; font-size: 10pt;")
        self.cmd_edit.returnPressed.connect(self._on_cmd_send)
        cv.addWidget(self.cmd_edit)
        ctrl_gp.setLayout(cv)
        vbox.addWidget(ctrl_gp)

        # 接收信令配置信息
        rx_gp = QGroupBox('接收信令配置信息')
        rx_grid = QGridLayout()
        self.rx_labels = {}
        rx_items = [('信令链路状态:', 'rx_state'), ('信令载波频率:', 'rx_carrier'),
                    ('发射增益配置:', 'rx_tx_gain'), ('发送载频配置:', 'rx_tx_carrier'),
                    ('反馈链路健康:', 'fb_health')]
        for r, (l, k) in enumerate(rx_items):
            lbl = QLabel(l)
            lbl.setFixedWidth(100)
            rx_grid.addWidget(lbl, r, 0)
            val = QLabel('--')
            val.setMaximumWidth(180)
            val.setWordWrap(True)
            val.setStyleSheet("color: #4ade80; font-weight: bold;")
            rx_grid.addWidget(val, r, 1)
            self.rx_labels[k] = val
        rx_gp.setLayout(rx_grid)
        vbox.addWidget(rx_gp)

        # 系统状态
        sys_gp = QGroupBox('系统状态')
        sv = QVBoxLayout()
        self.time_label = QLabel('--:--:--')
        sv.addWidget(self.time_label)
        sys_gp.setLayout(sv)
        vbox.addWidget(sys_gp)

        # 灾区情况发送 — 图片+视频+进度条
        self.send_gp = QGroupBox('灾区情况发送')
        send_layout = QVBoxLayout()
        self.send_img_label = QLabel()
        self.send_img_label.setAlignment(Qt.AlignCenter)
        self.send_img_label.setMinimumHeight(140)
        self.send_img_label.setStyleSheet("background-color: #ffffff; border-radius: 6px;")
        self.send_img_label.setText('等待发射任务...')
        self.send_task_label = QLabel('等待发射任务...')
        self.send_task_label.setStyleSheet("color: #f472b6; font-weight: 700; font-size: 10pt;")
        # 播放按钮
        btn_row = QHBoxLayout()
        self.send_play_btn = QPushButton('▶ 播放救援现场视频')
        self.send_play_btn.setCursor(Qt.PointingHandCursor)
        self.send_play_btn.setStyleSheet("""
            QPushButton { background-color: #ef4444; color: white; border-radius: 6px;
                          padding: 6px 14px; font-weight: 700; font-size: 10pt; }
            QPushButton:hover { background-color: #dc2626; }
        """)
        self.send_play_btn.clicked.connect(self._toggle_video_play)
        self.send_play_btn.hide()
        btn_row.addWidget(self.send_play_btn)
        btn_row.addStretch()
        send_layout.addWidget(self.send_img_label)
        send_layout.addWidget(self.send_task_label)
        send_layout.addLayout(btn_row)
        self.send_gp.setLayout(send_layout)
        vbox.addWidget(self.send_gp)

        # 加载救援图片循环
        self._rescue_imgs = self._load_rescue_images_for_tx()
        self._rescue_img_idx = 0
        self._rescue_img_frame = 0

        vbox.addStretch()

        # === 右侧绘图区 ===
        right = QWidget()
        grid_plots = QGridLayout(right)

        self.p_spec = self.create_plot('发端信号频谱', '频率 (kHz)', '幅度 (dB)')
        self.c_spec = self.p_spec.plot(pen='#38bdf8')

        self.p_time = self.create_plot('发射端时域信号波形', '时间 (ms)', '幅值 (V)')
        self.c_time = self.p_time.plot(pen='#f472b6')

        self.p_const = self.create_plot('信令信号星座图', 'I', 'Q')
        self.s_const = pg.ScatterPlotItem(size=6, brush=pg.mkBrush('#fbbf24'))
        self.p_const.addItem(self.s_const)

        self.p_time_mes = self.create_plot('信令时域信号波形', '时间 (ms)', '幅值 (V)')
        self.c_time_mes = self.p_time_mes.plot(pen='#a78bfa')

        grid_plots.addWidget(self.p_spec, 0, 0)
        grid_plots.addWidget(self.p_time, 0, 1)
        grid_plots.addWidget(self.p_const, 1, 0)
        grid_plots.addWidget(self.p_time_mes, 1, 1)

        plot_split = QSplitter(Qt.Horizontal)
        plot_split.addWidget(left)
        plot_split.addWidget(right)
        plot_split.setSizes([310, 890])
        plot_split.setStretchFactor(0, 0)  # 左侧固定不拉伸
        plot_split.setStretchFactor(1, 1)  # 右侧绘图区自适应

        # === AI 对话区 ===
        chat_panel = QWidget()
        chat_panel.setMinimumWidth(260)
        chat_panel.setMaximumWidth(360)
        chat_panel.setStyleSheet("background-color: #1e293b;")
        chat_panel_layout = QVBoxLayout(chat_panel)
        chat_panel_layout.setContentsMargins(0, 0, 0, 0)
        chat_group = QGroupBox('AI 发射控制助手')
        chat_layout_inner = QVBoxLayout()
        self.chat_widget = ChatWidget()
        self.chat_widget.send_message_signal.connect(self._on_chat_message)
        chat_layout_inner.addWidget(self.chat_widget)
        chat_group.setLayout(chat_layout_inner)
        chat_panel_layout.addWidget(chat_group)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(plot_split)
        split.addWidget(chat_panel)
        split.setSizes([1200, 300])
        split.setStretchFactor(0, 1)  # 绘图+左侧面板区域自适应
        split.setStretchFactor(1, 0)  # 聊天区固定不拉伸
        layout.addWidget(split)

        main_layout.addWidget(content_widget)

    def _load_tx_timeline(self):
        """加载预生成发射端时间线数据。"""
        import json as _json
        path = os.path.join(os.path.dirname(__file__), "..", "..",
                            "simulation_suite", "outputs", "demo_tx_data.json")
        path = os.path.abspath(path)
        try:
            with open(path, "r") as f:
                self._tx_timeline = _json.load(f)
            print(f"[TX] 加载发射端数据: {len(self._tx_timeline)} 帧")
        except Exception as e:
            print(f"[TX] 数据加载失败: {e}，请先运行 generate_demo_tx_data.py")
            self._tx_timeline = []

    def _load_rescue_images_for_tx(self):
        """加载救援1-7.png 图片列表用于循环显示。"""
        fnames = ["救援1.png", "救援2.png", "救援3.png", "救援4.png", "救援5.png", "救援7.png"]
        root = os.path.join(os.path.dirname(__file__), "..", "..")
        imgs = []
        for fn in fnames:
            path = os.path.abspath(os.path.join(root, fn))
            if os.path.exists(path):
                imgs.append((fn, QPixmap(path)))
        if imgs:
            print(f"[TX] 加载救援图片: {len(imgs)} 张")
        return imgs

    def _tx_cycle_rescue_image(self):
        """每 ~40 帧切一张救援图，在发送区循环显示。"""
        if not self._rescue_imgs:
            return
        self._rescue_img_frame += 1
        if self._rescue_img_frame % 40 == 0 or self._rescue_img_frame == 1:
            self._rescue_img_idx = (self._rescue_img_idx + 1) % len(self._rescue_imgs)
            fname, pix = self._rescue_imgs[self._rescue_img_idx]
            scaled = pix.scaled(self.send_img_label.width(), self.send_img_label.height(),
                                Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.send_img_label.setPixmap(scaled)
            self.send_task_label.setText(f"正在发送: {fname}")
            # 同步显示到发射任务状态
            if 'tx_image' in self.task_labels:
                self.task_labels['tx_image'].setText(fname)
                self._last_label_texts['tx_image'] = fname

    def _tx_play_tick(self):
        """每 120ms 推送一帧发射端数据 → data_store。"""
        if not self._tx_timeline or self._tx_frame >= len(self._tx_timeline):
            return
        fd = self._tx_timeline[self._tx_frame]
        data_mutex.acquire()
        try:
            data_store["tx_spec"] = fd["tx_spec"]
            data_store["tx_time"] = fd["tx_time"]
            data_store["rx_const"] = fd["rx_const"]
            data_store["rx_time"] = fd["rx_time"]
            data_store["status"] = fd["status"]
            data_store["sending_image"] = fd.get("sending_image")
            data_store["has_data"] = True
        finally:
            data_mutex.release()

        # 救援图片循环 + 任务状态更新
        self._tx_cycle_rescue_image()
        self._tx_frame += 1

    def _on_cmd_send(self):
        """指令输入框回车 → 转发到聊天处理。"""
        text = self.cmd_edit.text().strip()
        if text:
            self.cmd_edit.clear()
            self._handle_task_command(text)

    def _handle_task_command(self, text):
        """解析任务切换指令并更新 UI。"""
        text_lower = text.lower()
        script_dir = os.path.dirname(os.path.abspath(__file__))

        # ── 视频模式 ──
        if "视频" in text and ("切换" in text or "传输" in text):
            self._start_video_mode()
            return

        # ── 图像模式 ──
        if "图像" in text or "图片" in text:
            img_path = os.path.join(script_dir, "p2.jpg")
            if os.path.exists(img_path):
                pix = QPixmap(img_path)
                scaled = pix.scaled(self.send_img_label.width(), self.send_img_label.height(),
                                    Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.send_img_label.setPixmap(scaled)
                self.send_task_label.setText("正在发送: p2.jpg")
                self._set_task_state(1, "图像", "p2.jpg", "", "AI Agent通信 | 现场图片回传")
                self._set_task_state(1, "图像", "p2.jpg", "", "AI Agent通信 | 现场图片回传")
                if 'tx_image' in self.task_labels:
                    self.task_labels['tx_image'].setText("p2.jpg")
                    self._last_label_texts['tx_image'] = "p2.jpg"
                self.chat_widget.add_message("✅ 已切换到图像传输模式。正在发送 p2.jpg", is_user=False)
            else:
                self.chat_widget.add_message("⚠ 图片文件未找到。", is_user=False)
            return

        # ── 文本模式 ──
        if "文本" in text:
            content = "灾区A区受灾严重，请求加派人手！B区可设临时指挥所。"
            short = content[:20]
            self.send_img_label.setText("📝 文本传输中")
            self.send_task_label.setText(f"正在发送: {short}...")
            self._set_task_state(4, "文本", "", "", f"AI Agent通信 | {short}...")
            if 'tx_text' in self.task_labels:
                self.task_labels['tx_text'].setText(content[:40] + "...")
                self._last_label_texts['tx_text'] = content[:40] + "..."
            self.chat_widget.add_message(f"✅ 已切换到文本传输模式。内容: {content}", is_user=False)
            return

        # ── 状态查询 ──
        if "状态" in text or "查询" in text:
            self.chat_widget.add_message(self._get_task_status(), is_user=False)
            return

        self.chat_widget.add_message("收到。可用指令：切换到视频/图像/文本传输模式", is_user=False)

    def _set_task_state(self, mode, task_name, img_file, vid_file, tx_mode_text):
        """统一更新任务状态存储和标签显示。"""
        decision_mutex.acquire()
        try:
            tx_decision_store['tx_mode'] = mode
            tx_decision_store['image_file'] = img_file
            tx_decision_store['video_file'] = vid_file
            tx_decision_store['needs_update'] = True
            tx_decision_store['decision_version'] += 1
            tx_decision_store['command_source'] = 'ui'
            tx_decision_store['command_description'] = f'UI 切换发射任务到: {task_name}'
        finally:
            decision_mutex.release()

        mode_names = {1: '图像传输', 2: '视频传输', 3: '图像+视频', 4: '文本传输'}
        if 'tx_task' in self.task_labels:
            self.task_labels['tx_task'].setText(mode_names.get(mode, task_name))
            self._last_label_texts['tx_task'] = mode_names.get(mode, task_name)
        if 'tx_image' in self.task_labels:
            self.task_labels['tx_image'].setText(img_file or '--')
            self._last_label_texts['tx_image'] = img_file or '--'
        if 'tx_video' in self.task_labels:
            self.task_labels['tx_video'].setText(vid_file or '--')
            self._last_label_texts['tx_video'] = vid_file or '--'
        if 'tx_mode' in self.tx_labels:
            self.tx_labels['tx_mode'].setText(tx_mode_text)
            self._last_label_texts['tx_mode'] = tx_mode_text

    def _get_task_status(self):
        decision_mutex.acquire()
        try:
            m = tx_decision_store['tx_mode']
            img = tx_decision_store.get('image_file', '')
            vid = tx_decision_store.get('video_file', '')
            txt = tx_decision_store.get('text_string', '')
        finally:
            decision_mutex.release()
        names = {1: '图像传输', 2: '视频传输', 3: '图像+视频', 4: '文本传输'}
        return f"当前模式: {names.get(m, '未知')} | 图片: {img or '无'} | 视频: {vid or '无'}\n文本片段: {txt[:40]}"

    # ── 视频播放 ──────────────────────────────────────────
    def _start_video_mode(self):
        """加载救援现场.mp4并显示播放按钮。"""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        vid_path = os.path.join(script_dir, "救援现场.mp4")
        if not os.path.exists(vid_path):
            self.chat_widget.add_message("⚠ 视频文件未找到。", is_user=False)
            return

        self._set_task_state(2, "视频", "", "救援现场.mp4", "AI Agent通信 | 无人机视频回传")
        self.send_task_label.setText("救援现场实时视频")

        if 'tx_video' in self.task_labels:
            self.task_labels['tx_video'].setText("救援现场.mp4")
            self._last_label_texts['tx_video'] = "救援现场.mp4"
        self.send_play_btn.show()

        # 预载第一帧作为封面
        cap = cv2.VideoCapture(vid_path)
        ret, frame = cap.read()
        if ret:
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = frame_rgb.shape
            qimg = QImage(frame_rgb.data, w, h, ch * w, QImage.Format_RGB888)
            pix = QPixmap.fromImage(qimg)
            scaled = pix.scaled(self.send_img_label.width(), self.send_img_label.height(),
                                Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.send_img_label.setPixmap(scaled)
        cap.release()
        self.chat_widget.add_message("✅ 已切换到视频传输模式。点击红色按钮播放救援现场视频。", is_user=False)

    def _toggle_video_play(self):
        if self._video_playing:
            self._stop_video()
            self.send_play_btn.setText('▶ 播放救援现场视频')
        else:
            self._play_video()
            self.send_play_btn.setText('⏸ 暂停')

    def _play_video(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        vid_path = os.path.join(script_dir, "救援现场.mp4")
        if not os.path.exists(vid_path):
            return
        cap = cv2.VideoCapture(vid_path)
        if not cap.isOpened():
            return
        frames = []
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = frame_rgb.shape
            qimg = QImage(frame_rgb.data, w, h, ch * w, QImage.Format_RGB888)
            frames.append(QPixmap.fromImage(qimg))
        cap.release()
        self._video_frames = frames
        self._video_frame_idx = 0
        self._video_playing = True
        self._video_timer.start(80)  # ~12 fps

    def _stop_video(self):
        self._video_timer.stop()
        self._video_playing = False

    def _video_tick(self):
        if not self._video_frames or not self._video_playing:
            return
        self._video_frame_idx = (self._video_frame_idx + 1) % len(self._video_frames)
        pix = self._video_frames[self._video_frame_idx]
        scaled = pix.scaled(self.send_img_label.width(), self.send_img_label.height(),
                            Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.send_img_label.setPixmap(scaled)

    def _on_chat_message(self, text):
        """聊天入口 — 处理指令或启动回放。"""
        self.chat_widget.add_message(text, is_user=True)
        msg = text.strip()

        # 任务切换指令优先
        if any(k in msg for k in ["切换到", "切换成", "换成", "改为"]):
            self._handle_task_command(msg)
            return

        if "全权" in msg or "负责" in msg or "自行选择" in msg:
            if self._tx_state == "IDLE":
                self._tx_state = "PLAYING"
                self._tx_frame = 0

                # 先思考，再回放
                self.chat_widget.add_message("正在分析您的意图...", is_user=False)
                def _delayed_boot():
                    self.chat_widget.add_message("收到指令。正在激活发射链路...", is_user=False)
                    QTimer.singleShot(800, lambda: self.chat_widget.add_message(
                        "链路已建立，AI Agent接管发射任务。", is_user=False))
                    QTimer.singleShot(1600, lambda: [
                        self.chat_widget.add_message(
                            "当前任务：AI Agent通信 | 最大化传输质量", is_user=False),
                        self._tx_timer.start()  # 启动波形回放
                    ])
                QTimer.singleShot(1200, _delayed_boot)
                return

        self.chat_widget.add_message("收到。输入「全权负责」启动AI全权接管发射。", is_user=False)

    def update_ui(self, d):
        # 更新图表
        if d['tx_spec']['freq']:
            self.c_spec.setData(d['tx_spec']['freq'], d['tx_spec']['amp'])
        if d['tx_time']['time']:
            self.c_time.setData(d['tx_time']['time'], d['tx_time']['amp'])
        if d.get('rx_const', {}).get('i'):
            self.s_const.setData(x=d['rx_const']['i'], y=d['rx_const']['q'])
        else:
            self.s_const.clear()
        if d.get('rx_time', {}).get('time'):
            self.c_time_mes.setData(d['rx_time']['time'], d['rx_time']['amp'])

        # 更新状态文本
        s = d.get('status', {})
        for k in self.tx_labels:
            if k in s and self._last_label_texts.get(k) != str(s[k]):
                self.tx_labels[k].setText(str(s[k]))
                self._last_label_texts[k] = str(s[k])
        for k in self.rx_labels:
            if k in s and self._last_label_texts.get(k) != str(s[k]):
                self.rx_labels[k].setText(str(s[k]))
                self._last_label_texts[k] = str(s[k])
        if 'time' in s and self._last_label_texts.get('_time') != s['time']:
            self.time_label.setText(s['time'])
            self._last_label_texts['_time'] = s['time']

        # 更新任务标签和进度条
        task_mode = s.get('tx_mode', 'AI Agent通信')
        self.send_task_label.setText(task_mode)

        img_name = s.get('tx_image', '')
        if img_name and 'tx_image' in self.task_labels:
            if self._last_label_texts.get('tx_image') != img_name:
                self.task_labels['tx_image'].setText(img_name)
                self._last_label_texts['tx_image'] = img_name

        # 同步更新发射端配置信息中的载波频率（随场景变化）
        carrier = s.get('tx_carrier', '')
        if carrier and 'tx_carrier' in self.tx_labels:
            if self._last_label_texts.get('tx_carrier') != carrier:
                self.tx_labels['tx_carrier'].setText(carrier)
                self._last_label_texts['tx_carrier'] = carrier

    def toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
            self.maximize_btn.setText('□')
        else:
            self.showMaximized()
            self.maximize_btn.setText('−')

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.y() <= 40:
            self.drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and hasattr(self, 'drag_pos'):
            self.move(event.globalPos() - self.drag_pos)
            event.accept()

    def closeEvent(self, event):
        self._stop_video()
        if hasattr(self, 'data_thread'):
            self.data_thread.stop()
            self.data_thread.wait()
        event.accept()

if __name__ == '__main__':
    aq = QApplication(sys.argv)
    window = TransMainWindow()
    window.show()
    sys.exit(aq.exec_())
