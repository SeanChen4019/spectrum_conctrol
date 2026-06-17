import sys
import os
from datetime import datetime
import numpy as np
import threading
import cv2

# 导入 PyQt5 组件
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QGridLayout, QLabel, QGroupBox, QPushButton,
                             QRadioButton, QScrollArea, QSplitter, QSizePolicy,
                             QTextEdit)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread, QSize
from PyQt5.QtGui import QFont, QPixmap, QImage

# 导入高性能绘图库 PyQtGraph
import pyqtgraph as pg

# 导入 Flask
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

data_mutex = threading.Lock()
decision_mutex = threading.Lock()
data_store = {
    'has_data': False,
    'spectrum': {'freq': [], 'amp': []},
    'spectrum_mes': {'freq': [], 'amp': []},
    'time_domain': {'time': [], 'amp': []},
    'constellation': {'i': [], 'q': []},
    'waterfall': np.full((80, 2048), -90.0),
    'waterfall_linear': np.full((80, 2048), 1e-10),  # 线性幅度，用于时频图
    'status': {
        'data_rec_valid': '无效',
        'rx_mode_name': '仅图像',
        'current_send_mode': '等待接收',
        'current_mod': '未知',
        'center_frequency': 0,
        'samp_rate': 0,
        'snr': '信噪比无效',
        'mes_valid': '无效',
        'mes_rate': 0,
        'power_gain': '0 dB',
        'carrier_gain': '0 GHz',
        'ber': '未测试',
        'current_time': '--:--:--',
        'received_text': '等待接收数据...'
    },
    'received_image': None,
    'image_rebuild_status': '等待接收图片数据'
}

decision_store = {
    'anti_jamming_mode': 0,       # 0:常规 1:低速抗扰 2:切频
    'carrier_select': 3,           # Carrier_set 索引 (MATLAB 1-indexed), 初始=2.5GHz
    'power_gain_select': 1,        # Power_gain_set 索引 (1=0dB)
    'threshold': 300,              # 同步检测阈值
    'mod_selection': 1,            # 0:低速抗扰 1:增益模式 2:切频模式
    'needs_update': False,         # 旧版布尔标志（兼容）
    'decision_version': 0,         # 新版版本号（每次指令+1）
    'command_source': 'ui',        # 'ui' / 'ai_agent'
    'command_description': ''      # 最近指令描述
}

_data_recv_count = 0

@app.route('/api/task_sync', methods=['POST'])
def task_sync():
    """MATLAB通过信令链路收到发射端任务模式变更后，通知Flask UI"""
    try:
        data = request.json
        rx_mode = data.get('rx_mode', 1)
        tx_mode_name = data.get('tx_mode_name', '仅图像')
        print(f'[Flask] 信令链路任务同步: rx_mode={rx_mode} ({tx_mode_name})')

        data_mutex.acquire()
        try:
            data_store['status']['current_send_mode'] = tx_mode_name
            data_store['status']['data_rec_valid'] = f'信令同步: {tx_mode_name}'
        finally:
            data_mutex.release()

        return jsonify({'status': 'success', 'rx_mode': rx_mode})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok'})

@app.route('/api/rx_params', methods=['GET'])
def get_rx_params():
    """只读端点：返回当前参数，不消耗 needs_update（供发射机轮询备用通道）"""
    decision_mutex.acquire()
    try:
        return jsonify({
            'anti_jamming_mode': decision_store['anti_jamming_mode'],
            'carrier_select': decision_store['carrier_select'],
            'power_gain_select': decision_store['power_gain_select'],
            'trans_power_select': decision_store.get('trans_power_select', 1),
            'threshold': decision_store['threshold'],
            'decision_version': decision_store['decision_version'],
        })
    finally:
        decision_mutex.release()

@app.route('/api/data', methods=['POST'])
def receive_data():
    global _data_recv_count
    try:
        data = request.json
        _data_recv_count += 1
        data_mutex.acquire()
        try:
            data_store['has_data'] = True
            if 'spectrum' in data: data_store['spectrum'] = data['spectrum']
            if 'spectrum_mes' in data: data_store['spectrum_mes'] = data['spectrum_mes']
            if 'time_domain' in data: data_store['time_domain'] = data['time_domain']
            if 'constellation' in data: data_store['constellation'] = data['constellation']

            if 'waterfall_line' in data:
                line_data = np.array(data['waterfall_line'])
                if line_data.size == 2048:
                    data_store['waterfall'][1:] = data_store['waterfall'][:-1]
                    data_store['waterfall'][0] = line_data

            if 'waterfall_linear' in data:
                line_linear = np.array(data['waterfall_linear'])
                if line_linear.size == 2048:
                    data_store['waterfall_linear'][1:] = data_store['waterfall_linear'][:-1]
                    data_store['waterfall_linear'][0] = line_linear


            if 'status' in data: data_store['status'].update(data['status'])
            if 'received_image' in data: data_store['received_image'] = data['received_image']
            if 'image_rebuild_status' in data: data_store['image_rebuild_status'] = data['image_rebuild_status']
        finally:
            data_mutex.release()
        if _data_recv_count <= 3 or _data_recv_count % 20 == 0:
            keys = list(data.keys())
            wf_size = len(data.get('waterfall_line', []))
            print(f'[RX-Flask] 收到第{_data_recv_count}帧 | keys={keys} | wf_line_size={wf_size}')
        return jsonify({'status': 'success'})
    except Exception as e:
        print(f'[RX-Flask] 错误: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/decision', methods=['GET'])
def get_decision():
    decision_mutex.acquire()
    try:
        decision = {
            'anti_jamming_mode': decision_store['anti_jamming_mode'],
            'carrier_select': decision_store['carrier_select'],
            'power_gain_select': decision_store['power_gain_select'],
            'threshold': decision_store['threshold'],
            'mod_selection': decision_store['mod_selection'],
            'needs_update': decision_store['needs_update'],
            'decision_version': decision_store['decision_version'],
            'command_source': decision_store['command_source'],
            'command_description': decision_store['command_description']
        }
        decision_store['needs_update'] = False
        return jsonify(decision)
    finally:
        decision_mutex.release()


@app.route('/api/decision', methods=['POST'])
def post_decision():
    """MATLAB decision_making_flask 发送状态并获取决策"""
    try:
        data = request.json
        decision_mutex.acquire()
        try:
            response = {
                'Carrier_select_desion': data.get('Carrier_select_cur', decision_store['carrier_select']),
                'Anti_Jamming_Mode_desion': data.get('Anti_Jamming_Mode', decision_store['anti_jamming_mode']),
                'Power_gain_desion': data.get('Power_gain_cur', decision_store['power_gain_select']),
                'Par_valid': 0
            }

            # 如果 AI Agent 或 UI 有新的指令，返回新值
            if decision_store['command_source'] in ('ai_agent', 'ui'):
                store_carrier = decision_store['carrier_select']
                store_mode = decision_store['anti_jamming_mode']
                store_power = decision_store['power_gain_select']

                cur_carrier = data.get('Carrier_select_cur', store_carrier)
                cur_mode = data.get('Anti_Jamming_Mode', store_mode)
                cur_power = data.get('Power_gain_cur', store_power)

                if store_carrier != cur_carrier or store_mode != cur_mode or store_power != cur_power:
                    response['Carrier_select_desion'] = store_carrier
                    response['Anti_Jamming_Mode_desion'] = store_mode
                    response['Power_gain_desion'] = store_power
                    response['Par_valid'] = 1

            return jsonify(response)
        finally:
            decision_mutex.release()
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

def run_flask():
    # 禁用 werkzeug 的请求日志，防止控制台刷屏影响性能
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False, threaded=True)

class DataUpdateThread(QThread):
    data_ready = pyqtSignal(dict)
    def __init__(self):
        super().__init__()
        self.running = True

    def run(self):
        while self.running:
            data_mutex.acquire()
            has_new_data = data_store['has_data']
            if has_new_data:
                # 时域数据预处理：降采样 & 单位转换
                td = data_store['time_domain']
                td_time = td.get('time', [])
                td_amp = td.get('amp', [])
                if len(td_time) > 0:
                    step = max(1, len(td_time) // 1000)
                    td_processed = {
                        'time': [x * 1000 for x in td_time[::step]],
                        'amp': td_amp[::step],
                    }
                else:
                    td_processed = {'time': [], 'amp': []}

                data_copy = {
                    'spectrum': data_store['spectrum'].copy(),
                    'spectrum_mes': data_store['spectrum_mes'].copy(),
                    'time_domain': td_processed,
                    'constellation': data_store['constellation'].copy(),
                    'waterfall': data_store['waterfall'].copy(),
                    'status': data_store['status'].copy(),
                    'received_image': data_store['received_image'],
                    'image_rebuild_status': data_store['image_rebuild_status'],
                }
                data_store['has_data'] = False
            data_mutex.release()

            if has_new_data:
                self.data_ready.emit(data_copy)

            self.msleep(20)

    def stop(self):
        self.running = False
        self.wait()

class ChatWidget(QWidget):
    """AI Agent 对话组件 — 气泡式聊天"""
    send_message_signal = pyqtSignal(str)
    interactive_clicked = pyqtSignal(str)  # callback_id from interactive buttons

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bubble_fixed_width = 240  # 固定气泡宽度
        self._interactive_widgets = []  # track interactive button groups
        self.setup_ui()
    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        # ---- 标题栏 ----
        header = QWidget()
        header.setFixedHeight(36)
        header.setStyleSheet("background-color: #1e3a5f; border-radius: 8px 8px 0 0;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 0, 8, 0)
        title = QLabel('AI 控制助手')
        title.setStyleSheet("color: #93c5fd; font-weight: bold; font-size: 10pt; background: transparent;")
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
        # ---- 消息区 ----
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setMinimumHeight(220)
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
        # ---- 输入区（自适应高度） ----
        input_widget = QWidget()
        input_widget.setStyleSheet("background-color: #1e293b; border-top: 1px solid #334155;")
        input_layout = QHBoxLayout(input_widget)
        input_layout.setContentsMargins(10, 8, 10, 8)
        input_layout.setSpacing(8)
        self.input_edit = QTextEdit()
        self.input_edit.setPlaceholderText('输入指令，如：切换到切频模式...')
        self.input_edit.setMinimumHeight(34)
        self.input_edit.setMaximumHeight(120)
        self.input_edit.setAcceptRichText(False)
        self.input_edit.setStyleSheet("""
            QTextEdit { background-color: #0f172a; color: #e2e8f0; border: 1px solid #334155;
                        border-radius: 8px; padding: 6px 10px; font-size: 10.5pt; }
            QTextEdit:focus { border-color: #3b82f6; }
        """)
        self.input_edit.textChanged.connect(self._on_input_text_changed)
        self.input_edit.installEventFilter(self)
        input_layout.addWidget(self.input_edit, stretch=1)
        self.send_btn = QPushButton('发送')
        self.send_btn.setFixedSize(56, 34)
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setStyleSheet("""
            QPushButton { background-color: #3b82f6; color: white; border-radius: 8px;
                          font-weight: bold; font-size: 10.5pt; }
            QPushButton:hover { background-color: #2563eb; }
            QPushButton:pressed { background-color: #1d4ed8; }
        """)
        self.send_btn.clicked.connect(self._on_send)
        input_layout.addWidget(self.send_btn)
        layout.addWidget(input_widget)
        # 欢迎语
        self.add_message('你好！我是面向应急保障的频谱智能管控系统的智能体，可以保障灾区与救援队伍间通信。', is_user=False)
        self.add_message('灾情牵动人心，如果你想快速与灾区建立通信，你可以完全将通信交与我负责！', is_user=False)
    def _on_send(self):
        text = self.input_edit.toPlainText().strip()
        if text:
            self.input_edit.clear()
            self.send_message_signal.emit(text)

    def _on_input_text_changed(self):
        """Auto-grow input area based on text content."""
        doc = self.input_edit.document()
        text_height = int(doc.size().height() + 10)
        new_h = max(34, min(120, text_height))
        self.input_edit.setFixedHeight(new_h)

    def eventFilter(self, obj, event):
        from PyQt5.QtCore import QEvent
        if obj is self.input_edit and event.type() == QEvent.KeyPress:
            if (event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter) and not (event.modifiers() & Qt.ShiftModifier):
                self._on_send()
                return True
            if (event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter) and (event.modifiers() & Qt.ShiftModifier):
                return False  # let QTextEdit handle newline
        return super().eventFilter(obj, event)

    def _remove_temp_message(self):
        cnt = self.msg_layout.count()
        if cnt > 0:
            item = self.msg_layout.itemAt(cnt - 1)
            w = item.widget()
            if w and getattr(w, '_is_temp', False):
                self.msg_layout.removeWidget(w)
                w.deleteLater()
    def add_message(self, text, is_user=True, is_temp=False):
        """聊天气泡：头像 + 气泡（强制固定宽度）"""
        # 自动移除Markdown加粗符号
        clean_text = text.replace('**', '').replace('*', '')
        
        row = QHBoxLayout()
        row.setSpacing(8)
        # 头像（强制固定尺寸，永远不会被拉伸）
        avatar = QLabel()
        avatar.setFixedSize(30, 30)
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)  # 关键：锁定头像尺寸
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
                    QLabel { background-color: #10b981; color: white; border-radius: 15px;
                             font-weight: bold; font-size: 9pt; }
                """)
        # 气泡（设置唯一objectName，确保resizeEvent不会误改头像）
        bubble = QLabel(clean_text)
        bubble.setObjectName("chat_bubble")  # 关键：给气泡设置唯一标识
        bubble._is_temp = is_temp
        bubble.setWordWrap(True)
        bubble.setTextFormat(Qt.PlainText)
        bubble.setContentsMargins(12, 8, 12, 8)
        bubble.setFixedWidth(self.bubble_fixed_width)
        bubble.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Minimum)
        # 气泡样式
        if is_user:
            bubble.setStyleSheet("""
                QLabel#chat_bubble { background-color: #1d4ed8; color: #eff6ff; border-radius: 14px 4px 14px 14px;
                         padding: 10px 14px; font-size: 10.5pt; }
            """)
            row.addStretch()
            row.addWidget(bubble)
            row.addWidget(avatar)
        else:
            if is_temp:
                bubble.setStyleSheet("""
                    QLabel#chat_bubble { background-color: #334155; color: #64748b; border-radius: 4px 14px 14px 14px;
                         padding: 10px 14px; font-size: 10.5pt; font-style: italic; }
                """)
            else:
                bubble.setStyleSheet("""
                    QLabel#chat_bubble { background-color: #1e293b; color: #cbd5e1; border-radius: 4px 14px 14px 14px;
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
        # 强制滚动到底部
        QTimer.singleShot(50, lambda: self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()
        ))
    def add_interactive_message(self, text: str, buttons: list):
        """Add a message with clickable buttons below it.
        buttons is a list of (label, callback_id) tuples."""
        self.add_message(text, is_user=False)
        btn_widget = QWidget()
        btn_widget.setStyleSheet("background: transparent;")
        btn_layout = QHBoxLayout(btn_widget)
        btn_layout.setContentsMargins(56, 2, 20, 6)
        btn_layout.setSpacing(10)
        btn_layout.addStretch()
        for label, cb_id in buttons:
            btn = QPushButton(label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet("""
                QPushButton { background-color: #1d4ed8; color: white; border-radius: 8px;
                              padding: 6px 16px; font-size: 10pt; font-weight: 600; }
                QPushButton:hover { background-color: #2563eb; }
            """)
            btn.clicked.connect(lambda checked, cid=cb_id: self.interactive_clicked.emit(cid))
            btn_layout.addWidget(btn)
        btn_layout.addStretch()
        self.msg_layout.addWidget(btn_widget)
        self._interactive_widgets.append(btn_widget)
        QTimer.singleShot(50, lambda: self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()))

    def clear_interactive(self):
        """Remove all interactive button groups."""
        for w in self._interactive_widgets:
            self.msg_layout.removeWidget(w)
            w.deleteLater()
        self._interactive_widgets.clear()

    def clear_history(self):
        while self.msg_layout.count():
            item = self.msg_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.add_message('对话已清空，请继续输入指令。', is_user=False)
    def resizeEvent(self, event):
        """窗口大小变化时自动调整气泡宽度（只改气泡，不改头像）"""
        super().resizeEvent(event)
        # 计算新的气泡宽度
        new_width = self.scroll_area.viewport().width() - 20 - 30 - 8 - 6
        if new_width < 200:
            new_width = 200
        if new_width == self.bubble_fixed_width:
            return
        self.bubble_fixed_width = new_width
        # 只更新带有chat_bubble objectName的标签（绝对不会误改头像）
        for i in range(self.msg_layout.count()):
            item = self.msg_layout.itemAt(i)
            wrapper = item.widget()
            if wrapper and not getattr(wrapper, '_is_temp', False):
                bubble = wrapper.findChild(QLabel, "chat_bubble", Qt.FindDirectChildrenOnly)
                if bubble:
                    bubble.setFixedWidth(self.bubble_fixed_width)
        event.accept()

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle('全双工 SDR 接收机监控系统 (AI Agent + 分块渐进式媒体恢复)')
        self.setGeometry(100, 100, 1600, 900)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowSystemMenuHint | Qt.WindowMinMaxButtonsHint)

        self.last_image_path = None
        self._last_label_texts = {}          # 标签缓存：仅文本变化时才 setText
        self._last_wf_freq = None            # 瀑布图频率缓存：避免重复 setRect
        self._last_rx_mode = ''              # 上一次接收模式，检测模式切换

        # 视频循环播放（接收端）
        self._video_timer = QTimer(self)
        self._video_timer.timeout.connect(self._video_tick)
        self._video_frames = []
        self._video_frame_idx = 0
        self._video_path = ''
        self._video_mtime = 0
        self._video_active = False

        # ====== 配置 PyQtGraph 全局主题 ======
        pg.setConfigOptions(antialias=False, enableExperimental=True)
        pg.setConfigOption('background', '#0f172a')
        pg.setConfigOption('foreground', '#94a3b8')

        # ====== 判决系统 — 预生成瀑布图回放剧本 ======
        self._ds_state = "IDLE"
        self._ds_step = 0
        self._ds_timer = QTimer(self)
        self._ds_timer.timeout.connect(self._ds_advance)
        self._ds_timer.setSingleShot(True)

        # Pre-baked waterfall timeline
        self._ds_wf_timeline = []
        self._ds_wf_timer = QTimer(self)
        self._ds_wf_timer.timeout.connect(self._ds_wf_tick)
        self._ds_wf_timer.setInterval(120)
        self._ds_wf_frame = 0
        self._load_demo_waterfall()

        # Progressive image reconstruction — continuous cycle
        self._ds_rescue_imgs = []
        self._ds_rescue_current = None
        self._ds_rescue_rows = 0
        self._ds_rescue_total = 0
        self._ds_rescue_img_idx = 0
        self._ds_disaster_locked = False
        self._load_rescue_images()

        print('判决系统就绪（预生成瀑布图回放模式）')

        self.setup_theme()
        self.init_ui()

        self.data_thread = DataUpdateThread()
        self.data_thread.data_ready.connect(self.update_ui)
        self.data_thread.start()

        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        print('Flask服务器已启动: http://127.0.0.1:5000')

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.y() <= 40:
            self.drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()
    
    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and hasattr(self, 'drag_pos'):
            self.move(event.globalPos() - self.drag_pos)
            event.accept()
    
    def toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
            self.maximize_btn.setText('□')
        else:
            self.showMaximized()
            self.maximize_btn.setText('−')
    
    def setup_theme(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #0f172a; }
            QWidget { color: #e2e8f0; font-family: 'Microsoft YaHei', 'SimHei', Arial; font-size: 10pt; }
            QGroupBox { border: 1px solid #475569; border-radius: 10px; margin-top: 16px; padding-top: 20px; font-weight: 600; color: #60a5fa; background-color: #1e293b; }
            QGroupBox::title { subcontrol-origin: margin; left: 20px; padding: 0 10px; }
            QLabel { color: #cbd5e1; }
            QRadioButton { color: #e2e8f0; spacing: 10px; }
            QRadioButton::indicator { width: 18px; height: 18px; border: 2px solid #475569; border-radius: 9px; background-color: #334155; }
            QRadioButton::indicator:checked { background-color: #3b82f6; border-color: #3b82f6; }
            QScrollArea { border: 1px solid #475569; border-radius: 8px; background-color: #1e293b; }
                           
            /* ====== 新增：美化垂直滚动条 ====== */
            QScrollBar:vertical {
                border: none;
                background-color: #0f172a; /* 滚动条底色 */
                width: 10px;               /* 滚动条宽度变窄 */
                margin: 0px 0px 0px 0px;
            }
            QScrollBar::handle:vertical {
                background-color: #475569; /* 滚动块颜色 */
                min-height: 30px;
                border-radius: 5px;        /* 圆角设计 */
            }
            QScrollBar::handle:vertical:hover {
                background-color: #64748b; /* 鼠标悬停时变亮 */
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;               /* 隐藏上下箭头 */
                background: none;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: none;          /* 隐藏滚动块上下的背景颜色 */
            }
            /* ====== 新增：美化左右拖拽分割线 (隐藏白条) ====== */
            QSplitter::handle {
                background-color: #334155; /* 使用深灰色作为分割线 */
                width: 2px;                /* 把粗白条变成 2 像素的细线 */
            }

        """)
    
    def _stop_video(self):
        """停止视频循环播放"""
        self._video_timer.stop()
        self._video_frames = []
        self._video_frame_idx = 0
        self._video_path = ''
        self._video_mtime = 0
        self._video_active = False

    def _load_video_frames(self, video_path):
        """预加载视频帧为 QPixmap 列表"""
        frames = []
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return frames
        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_interval = max(1, int(fps / 8)) if fps > 0 else 3
        i = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if i % frame_interval == 0:
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = frame_rgb.shape
                qimg = QImage(frame_rgb.data, w, h, ch * w, QImage.Format_RGB888)
                pix = QPixmap.fromImage(qimg)
                frames.append(pix)
            i += 1
        cap.release()
        return frames

    def _video_tick(self):
        """定时器回调：切换到下一帧"""
        if not self._video_frames:
            return
        self._video_frame_idx = (self._video_frame_idx + 1) % len(self._video_frames)
        pix = self._video_frames[self._video_frame_idx]
        self.image_label.setPixmap(pix.scaled(
            self.image_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def closeEvent(self, event):
        self._stop_video()
        self._ds_wf_timer.stop()
        if hasattr(self, 'data_thread'):
            self.data_thread.stop()
        event.accept()
    
    def set_mode(self, mode):
        decision_mutex.acquire()
        try:
            decision_store['anti_jamming_mode'] = mode
            decision_store['needs_update'] = True
            decision_store['decision_version'] += 1
            decision_store['command_source'] = 'ui'
            decision_store['command_description'] = f'UI 切换模式: {mode}'
        finally:
            decision_mutex.release()

    def _sync_radio_buttons(self, mode_idx):
        """同步 RadioButton 选中状态，阻塞信号防止循环触发"""
        radios = [self.radio_regular, self.radio_low_speed, self.radio_freq_hop]
        for i, radio in enumerate(radios):
            radio.blockSignals(True)
            radio.setChecked(i == mode_idx)
            radio.blockSignals(False)

    def _on_chat_message(self, text):
        """判决系统入口：识别用户意图并启动剧本"""
        self.chat_widget.add_message(text, is_user=True)
        msg = text.strip()

        if self._ds_state == "IDLE":
            if "全权" in msg or "负责" in msg or "自行选择" in msg:
                self._ds_state = "AWAIT_AUTH"
                self.chat_widget.add_message("正在分析您的意图...", is_user=False)
                QTimer.singleShot(1200, self._ds_show_auth_prompt)
                return

        self.chat_widget.add_message("收到。如需AI全权接管链路控制，请输入「全权负责」「自行选择」等关键词。", is_user=False)

    def _ds_show_auth_prompt(self):
        self.chat_widget.add_interactive_message(
            "是否让我全权负责通信链路，即是否赋予我管理员权限？",
            [("是，赋予管理员权限", "auth_yes"), ("否，保持手动控制", "auth_no")])

    def _on_interactive_clicked(self, callback_id):
        if callback_id == "auth_yes":
            self._ds_state = "AUTH_OK"
            self.chat_widget.clear_interactive()
            self.chat_widget.add_message("✅ 已获得管理员权限。", is_user=False)
            self._ds_boot_msgs = [
                "正在初始化频谱监测系统…",
                "加载应急通信场景知识库…",
                "启动10信道实时频谱感知…",
                "链接智能决策引擎…",
                "🟢 全自动频谱管控系统就绪。正在持续监控链路状态…",
            ]
            self._ds_boot_idx = 0
            self._ds_last_boot_t = None
            self._ds_timer.start(800)
        elif callback_id == "auth_no":
            self.chat_widget.clear_interactive()
            self.chat_widget.add_message("好的，保持手动控制模式。", is_user=False)
            self._ds_state = "IDLE"

    # ═══════════════════════════════════════════════════════════════
    # Single-timer timeline: waterfall + chat together
    # ═══════════════════════════════════════════════════════════════

    CHAT_WAYPOINTS = [
        (65, "基站退服", [
            "检测到异常：信噪比骤降，通信质量急剧恶化",
            "频谱分析：底噪整体抬升约8MHz，带宽内无离散尖峰",
            "研判：公网基站退服导致局部宽带噪声抬升。切换【低速抗扰模式】，BPSK+扩频编码",
            "已执行。低速抗扰模式生效，牺牲速率换取链路可靠性。持续监控中…",
        ], [1500, 2200, 2800, 2000], (1, 3, 3.0, "低速抗扰模式")),
        (262, "救援拥塞", [
            "⚠ 信噪比再次异常！信道5出现多音频点冲突",
            "频谱分析：多支救援队伍密集接入，信道4-5出现离散尖峰",
            "检测到当前传输信道受到干扰，立即启动切频避让！",
            "已切换至信道7(3.5GHz)，避开多音冲突区域。恢复QPSK正常传输。持续监控中…",
        ], [1500, 2000, 2500, 2000], (2, 4, 3.5, "切频模式")),
        (462, "无人机压测", [
            "⚠ 严重干扰告警！信道7出现高功率宽带压制",
            "频谱分析：估算干扰功率约20dB，覆盖6MHz频宽",
            "检测到当前传输信道受到强力压制，立即启动切频+增益补偿！",
            "已切换至信道2(3.0GHz)。业务降级为关键帧图片回传，链路恢复。持续监控中…",
        ], [1500, 2200, 2800, 2000], (2, 2, 3.0, "切频模式")),
    ]

    def _load_demo_waterfall(self):
        import json, os
        # Try multiple paths
        candidates = [
            os.path.join(os.path.dirname(__file__), "..", "..", "simulation_suite", "outputs", "demo_waterfall.json"),
            os.path.join(os.path.dirname(__file__), "demo_waterfall.json"),
            os.path.abspath("simulation_suite/outputs/demo_waterfall.json"),
        ]
        path = None
        for p in candidates:
            if os.path.exists(os.path.abspath(p)):
                path = os.path.abspath(p)
                break
        if path is None:
            print(f"[判决] 瀑布图未找到，尝试了: {candidates}")
            return
        try:
            with open(path, "r") as f:
                self._ds_wf_timeline = json.load(f)
            print(f"[判决] 加载瀑布图: {path} -> {len(self._ds_wf_timeline)} 帧")
        except Exception as e:
            print(f"[判决] 瀑布图加载失败: {e}")

    def _load_rescue_images(self):
        """Load all images as QPixmaps. Cycle shows first one at startup."""
        import os
        fnames = ["救援1.png", "救援2.png", "救援3.png", "救援4.png", "救援5.png", "救援7.png"]
        root = os.path.join(os.path.dirname(__file__), "..", "..")
        pixmaps = []
        for fn in fnames:
            path = os.path.abspath(os.path.join(root, fn))
            if os.path.exists(path):
                pixmaps.append(QPixmap(path))
        self._ds_rescue_imgs = pixmaps
        self._ds_rescue_img_idx = 0
        self._ds_rescue_rows = 0
        if pixmaps:
            self._ds_rescue_current = pixmaps[0]
            self._ds_rescue_total = pixmaps[0].height()
            print(f"[判决] 加载救援图片: {len(pixmaps)} 张")
        else:
            self._ds_rescue_current = None

    def _ds_reveal_step(self):
        """Reveal current image row-by-row. When done, advance to next image."""
        pix = self._ds_rescue_current
        pixmaps = self._ds_rescue_imgs
        if pix is None or not pixmaps:
            return

        total_h = self._ds_rescue_total
        if total_h <= 0:
            return

        # ~15 rows/frame → full image in ~40 frames = 5s
        step = max(1, total_h // 40)
        self._ds_rescue_rows += step

        if self._ds_rescue_rows >= total_h:
            # Current image fully revealed — move to next image immediately
            self._ds_rescue_img_idx = (self._ds_rescue_img_idx + 1) % len(pixmaps)
            self._ds_rescue_current = pixmaps[self._ds_rescue_img_idx]
            self._ds_rescue_total = self._ds_rescue_current.height()
            self._ds_rescue_rows = step  # start with a small sliver of the next
            pix = self._ds_rescue_current

        shown = self._ds_rescue_rows
        ratio = min(1.0, shown / total_h)

        w = self.image_label.width()
        h = self.image_label.height()
        if w <= 0 or h <= 0:
            return

        scaled = pix.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        crop_h = max(1, int(scaled.height() * ratio))
        cropped = scaled.copy(0, 0, scaled.width(), crop_h)
        self.image_label.setPixmap(cropped)

    def _ds_flash_disaster_info(self):
        """Show disaster situation report and lock it permanently."""
        msg = (
            "灾区A区域受灾严重，主要是房屋倒塌，众多居民被压在废墟之下，请求加派人手！"
            "并且借调搜救犬及挖掘设备！目前到达A区域的线路1未被大规模损毁，可开辟救援通道。"
            "灾区B区域有大片平地，可供救援直升机起降，可搭建为临时指挥所和救援中心！"
            "灾区情况正在持续勘察！"
        )
        self.received_text_label.setText(msg)
        self._ds_disaster_locked = True

    def _ds_advance(self):
        """Single entry point — handles boot sequence only."""
        if self._ds_state == "AUTH_OK":
            msgs = self._ds_boot_msgs
            idx = self._ds_boot_idx
            if idx < len(msgs):
                self.chat_widget.add_message(msgs[idx], is_user=False)
                self._ds_boot_idx += 1
                self._ds_timer.start(1200)
                if idx == len(msgs) - 1:
                    # Boot done → start waterfall playback
                    self._ds_wf_frame = 0
                    self._ds_wf_last_chat = None
                    self._ds_chat_idx = 0
                    self._ds_chat_msgs = None
                    self._ds_chat_delays = None
                    self._ds_chat_step = 0
                    self._ds_chat_frame = 0
                    self._ds_wf_timer.start()
                    self._ds_state = "WF_PLAY"
                    # Flash disaster info text ~3.5s after system ready
                    QTimer.singleShot(3500, self._ds_flash_disaster_info)

    # ── Waterfall frame playback (120ms per frame) ──────────
    def _ds_wf_tick(self):
        timeline = self._ds_wf_timeline
        if not timeline:
            self._load_demo_waterfall()
            timeline = self._ds_wf_timeline
        if not timeline or self._ds_wf_frame >= len(timeline):
            return

        fd = timeline[self._ds_wf_frame]
        N = 2048
        freq_khz = [round(-195 + 390 * i / (N - 1), 3) for i in range(N)]
        line = np.array(fd["waterfall_line"], dtype=float)

        data_mutex.acquire()
        try:
            # Roll waterfall matrix (same as Flask endpoint would do)
            if line.size == 2048:
                data_store['waterfall'][1:] = data_store['waterfall'][:-1]
                data_store['waterfall'][0] = line
                data_store['waterfall_linear'][1:] = data_store['waterfall_linear'][:-1]
                data_store['waterfall_linear'][0] = np.array(fd["waterfall_linear"], dtype=float)

            data_store["spectrum"] = {"freq": freq_khz, "amp": fd["spectrum"]}
            data_store["time_domain"] = {"time": fd["time_domain_t"], "amp": fd["time_domain_amp"]}
            data_store["constellation"] = {"i": fd["constellation_i"], "q": fd["constellation_q"]}
            data_store["status"].update(fd["status"])
            data_store["has_data"] = True
        finally:
            data_mutex.release()

        self._ds_wf_frame += 1

        # Progressive reveal — runs every frame, cycles images continuously
        self._ds_reveal_step()

        # ── Chat waypoint check ──────────────────────────────
        if self._ds_chat_msgs is None and self._ds_chat_idx < len(self.CHAT_WAYPOINTS):
            wp_frame, wp_name, wp_msgs, wp_delays, wp_params = self.CHAT_WAYPOINTS[self._ds_chat_idx]
            if self._ds_wf_frame >= wp_frame:
                self._ds_chat_msgs = wp_msgs
                self._ds_chat_delays = wp_delays
                self._ds_chat_step = 0
                self._ds_chat_params = wp_params
                self._ds_chat_name = wp_name
                self._ds_chat_frame = self._ds_wf_frame
                self._ds_chat_tick()

    def _ds_chat_tick(self):
        """Deliver one message from the current chat waypoint."""
        if self._ds_chat_msgs is None:
            return
        step = self._ds_chat_step
        msgs = self._ds_chat_msgs
        delays = self._ds_chat_delays

        if step >= len(msgs):
            # Done with this waypoint
            self._ds_chat_msgs = None
            self._ds_chat_idx += 1
            if self._ds_chat_idx >= len(self.CHAT_WAYPOINTS):
                return

        self.chat_widget.add_message(msgs[step], is_user=False)

        # Apply params at specific message indices (step 2 = "研判" msg, step 3 = "已执行" msg)
        params = self._ds_chat_params
        if params and step == 2:
            self._ds_apply_params(params[0], params[1], params[2], params[3])

        self._ds_chat_step += 1
        delay = delays[step] if step < len(delays) else 2000
        QTimer.singleShot(delay, self._ds_chat_tick)

    def _ds_apply_params(self, mode: int, carrier_idx: int, freq_ghz: float, mode_name: str):
        decision_mutex.acquire()
        try:
            decision_store["anti_jamming_mode"] = mode
            decision_store["carrier_select"] = carrier_idx
            decision_store["needs_update"] = True
            decision_store["decision_version"] += 1
            decision_store["command_source"] = "判决系统"
            decision_store["command_description"] = f"判决系统自动切换：{mode_name}"
        finally:
            decision_mutex.release()

        self._sync_radio_buttons(mode)
        mode_names = {0: "常规模式", 1: "低速抗扰模式", 2: "切频模式"}
        mod_names = {0: "QPSK", 1: "BPSK+扩频", 2: "QPSK"}
        for key, label in self.status_labels.items():
            if key == "current_send_mode":
                label.setText(mode_names.get(mode, mode_name))
            elif key == "current_mod":
                label.setText(mod_names.get(mode, "QPSK"))
            elif key == "center_frequency":
                label.setText(f"{freq_ghz:.2f} GHz")


    def create_custom_plot(self, title, x_label, y_label):
        plot = pg.PlotWidget(title=title)
        plot.showGrid(x=True, y=True, alpha=0.3)
        plot.setLabel('bottom', x_label)
        plot.setLabel('left', y_label)
        plot.getAxis('bottom').setPen(pg.mkPen(color='#475569'))
        plot.getAxis('left').setPen(pg.mkPen(color='#475569'))
        plot.setClipToView(True)                             # 只渲染视口内的数据点
        return plot

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # ======== 标题栏 ========
        self.title_bar = QWidget()
        self.title_bar.setFixedHeight(40)
        self.title_bar.setStyleSheet("background-color: #1e293b; border-bottom: 1px solid #334155;")
        title_bar_layout = QHBoxLayout(self.title_bar)
        title_bar_layout.setContentsMargins(10, 0, 10, 0)
        self.title_label = QLabel('全双工 SDR 接收机监控系统 (AI + 媒体恢复)')
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
        
        # ======== 内容区 ========
        content_widget = QWidget()
        content_layout = QHBoxLayout(content_widget)
        content_layout.setContentsMargins(8, 8, 8, 8)
        
        # 左侧面板
        left_widget = QWidget()
        left_widget.setStyleSheet("background-color: #1e293b;")
        left_layout = QVBoxLayout(left_widget)
        
        # 模式选择
        mode_group = QGroupBox('抗干扰模式切换')
        mode_layout = QVBoxLayout()
        self.radio_regular = QRadioButton('常规模式')
        self.radio_regular.setChecked(True)
        self.radio_regular.toggled.connect(lambda: self.set_mode(0) if self.radio_regular.isChecked() else None)
        self.radio_low_speed = QRadioButton('低速抗扰模式')
        self.radio_low_speed.toggled.connect(lambda: self.set_mode(1) if self.radio_low_speed.isChecked() else None)
        self.radio_freq_hop = QRadioButton('切频模式')
        self.radio_freq_hop.toggled.connect(lambda: self.set_mode(2) if self.radio_freq_hop.isChecked() else None)
        mode_layout.addWidget(self.radio_regular)
        mode_layout.addWidget(self.radio_low_speed)
        mode_layout.addWidget(self.radio_freq_hop)
        mode_group.setLayout(mode_layout)
        left_layout.addWidget(mode_group)
        
        # 状态面板
        status_group = QGroupBox('实时链路状态')
        status_layout = QGridLayout()
        self.status_labels = {}
        status_items = [
            ('数据接收', 'data_rec_valid'), ('任务模式', 'rx_mode_name'),
            ('发送模式', 'current_send_mode'),
            ('调制方式', 'current_mod'), ('中心频率', 'center_frequency'),
            ('采样率', 'samp_rate'), ('信噪比', 'snr'),
            ('消息状态', 'mes_valid'), ('消息速率', 'mes_rate'),
            ('功率增益', 'power_gain'), ('载波增益', 'carrier_gain'),
            ('反馈链路健康', 'fb_health'),
            ('当前时间', 'current_time')
        ]
        for row, (label_text, key) in enumerate(status_items):
            label = QLabel(f'{label_text}:')
            label.setStyleSheet('color: #94a3b8;')
            value_label = QLabel('--')
            value_label.setStyleSheet('color: #38bdf8; font-weight: bold;')
            status_layout.addWidget(label, row, 0)
            status_layout.addWidget(value_label, row, 1)
            self.status_labels[key] = value_label
        status_group.setLayout(status_layout)
        left_layout.addWidget(status_group)
        # 文本和图片显示区域保持不变
        text_group = QGroupBox('灾区情况信息回传')
        text_group.setStyleSheet("QGroupBox { color: #facc15; font-weight: 700; font-size: 11pt; border: 1px solid #475569; border-radius: 10px; margin-top: 16px; padding-top: 20px; background-color: #1e293b; } QGroupBox::title { subcontrol-origin: margin; left: 20px; padding: 0 10px; }")
        text_layout = QVBoxLayout()
        self.received_text_label = QLabel('')
        self.received_text_label.setWordWrap(True)
        self.received_text_label.setMinimumHeight(100)
        self.received_text_label.setStyleSheet("background-color: #ffffff; padding: 10px; border-radius: 5px; color: #cc0000; font-size: 10pt; font-weight: 700;")
        text_layout.addWidget(self.received_text_label)
        text_group.setLayout(text_layout)
        left_layout.addWidget(text_group, stretch=2)

        image_group = QGroupBox('灾区情况实时回传')
        image_group.setStyleSheet("QGroupBox { color: #facc15; font-weight: 700; font-size: 11pt; border: 1px solid #475569; border-radius: 10px; margin-top: 16px; padding-top: 20px; background-color: #1e293b; } QGroupBox::title { subcontrol-origin: margin; left: 20px; padding: 0 10px; }")
        image_layout = QVBoxLayout()
        image_layout.setContentsMargins(4, 4, 4, 4)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet("background-color: #e8ecf0; border-radius: 5px;")
        self.image_label.setFixedSize(280, 200)
        self.image_label.setScaledContents(False)
        image_layout.addWidget(self.image_label, alignment=Qt.AlignCenter)
        image_group.setLayout(image_layout)
        left_layout.addWidget(image_group)

        left_layout.addStretch()
        
        left_scroll = QScrollArea()
        left_scroll.setWidget(left_widget)
        left_scroll.setWidgetResizable(True)
        left_scroll.setMinimumWidth(320)
        
        # ================= 右侧高性能绘图区 (PyQtGraph) =================
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        
        # 上半部分：频谱图
        charts_top = QWidget()
        charts_top_layout = QHBoxLayout(charts_top)
        charts_top_layout.setContentsMargins(0, 0, 0, 0)
        
        self.plot_spectrum = self.create_custom_plot('收端信号频谱', '频率 (kHz)', '幅度 (dB)')
        self.curve_spectrum = self.plot_spectrum.plot(pen=pg.mkPen('#60a5fa', width=1.5))
        self.curve_spectrum.setDownsampling(auto=True, method='peak')
        charts_top_layout.addWidget(self.plot_spectrum)
        
        self.plot_constellation = self.create_custom_plot('收端信号星座图', 'I', 'Q')
        self.scatter_constellation = pg.ScatterPlotItem(size=4, pen=pg.mkPen(None), brush=pg.mkBrush('#a78bfa90'))
        self.plot_constellation.addItem(self.scatter_constellation)
        charts_top_layout.addWidget(self.plot_constellation)
        right_layout.addWidget(charts_top)
        
        # 中间部分：时域与星座图
        charts_mid = QWidget()
        charts_mid_layout = QHBoxLayout(charts_mid)
        charts_mid_layout.setContentsMargins(0, 0, 0, 0)
        
        self.plot_time = self.create_custom_plot('收端时域信号波形', '时间 (ms)', '幅度')
        self.curve_time = self.plot_time.plot(pen=pg.mkPen('#4ade80', width=1))
        self.curve_time.setDownsampling(auto=True, method='peak')
        charts_mid_layout.addWidget(self.plot_time)
        
        # 【新增图表】10信道占用决策热力图
        self.plot_channel = pg.PlotWidget(title="信道占用决策状态 (10信道)")
        self.plot_channel.setLabel('bottom', '信道编号 (1-10)')
        self.plot_channel.setLabel('left', '时间历史')
        
        # 1. 关闭默认模糊网格
        self.plot_channel.showGrid(x=False, y=False)
        
        self.channel_im = pg.ImageItem()
        self.plot_channel.addItem(self.channel_im)
        self.channel_history = np.full((20, 10), -90.0)  # 初始值接近典型噪声底，减少启动时的色阶偏移
        
        # 2. 绘制纯黑边框，切分 10x40 的标准方格
        black_pen = pg.mkPen(color='black', width=2)
        for x in range(1, 10):
            self.plot_channel.addItem(pg.InfiniteLine(pos=x, angle=90, pen=black_pen))
        for y in range(1, 20):
            self.plot_channel.addItem(pg.InfiniteLine(pos=y, angle=0, pen=black_pen))

        # 自定义红绿 Colormap：绿(空闲) -> 黄(过渡) -> 红(占用)
        # 将区间严格三等分: [0~33%]为绿, [33%~66%]为黄, [66%~100%]为红
        pos = np.array([0.0, 0.33, 0.33001, 0.66, 0.66001, 1.0])
        colors = np.array([
            [34, 197, 94, 255],   # 纯绿 
            [34, 197, 94, 255],   # 纯绿 (边界)
            [234, 179, 8, 255],   # 纯黄 
            [234, 179, 8, 255],   # 纯黄 (边界)
            [239, 68, 68, 255],   # 纯红 
            [239, 68, 68, 255]    # 纯红 (边界)
        ], dtype=np.ubyte)
        cmap = pg.ColorMap(pos, colors)
        self.channel_im.setLookupTable(cmap.getLookupTable())
        
        # 能量阈值设定：使用动态阈值，根据实际信号自动调整
        # 初始值会在 update_ui 中动态更新
        self.channel_im.setLevels([-100, -20])
        self._channel_levels = [-100, -20]  # 缓存当前阈值
        
        # 让X轴刻度居中显示 1 到 10
        x_axis = self.plot_channel.getAxis('bottom')
        x_axis.setTicks([[(i + 0.5, str(i + 1)) for i in range(10)]])
        
        self.plot_channel.invertY(True) # 最新数据从上往下流
        charts_mid_layout.addWidget(self.plot_channel)
        right_layout.addWidget(charts_mid)


        # 下半部分：高刷瀑布图
        self.plot_waterfall = pg.PlotWidget(title="收端时频瀑布图 (-40dB 到 0dB)")
        self.plot_waterfall.setLabel('bottom', '频段索引 (X)')
        self.plot_waterfall.setLabel('left', '时间历史 (Y)')
        
        # 使用 ImageItem 渲染矩阵
        self.waterfall_im = pg.ImageItem()
        self.plot_waterfall.addItem(self.waterfall_im)
        
        # 设置瀑布图的伪彩色 (类似 matplotlib 的 plasma)
        colormap = pg.colormap.get('turbo')
        self.waterfall_im.setLookupTable(colormap.getLookupTable())
        self.waterfall_im.setLevels([-100, -20])  # 初始值，会在 update_ui 中动态更新
        self._waterfall_levels = [-100, -20]  # 缓存当前阈值
        
        # 翻转Y轴，让最新的数据从上方往下流
        self.plot_waterfall.invertY(True)
        
        right_layout.addWidget(self.plot_waterfall)

        # ================= 最右侧 AI 对话区 =================
        chat_panel = QWidget()
        chat_panel.setStyleSheet("background-color: #1e293b;")
        chat_panel_layout = QVBoxLayout(chat_panel)
        chat_panel_layout.setContentsMargins(0, 0, 0, 0)
        chat_group = QGroupBox('AI 控制助手')
        chat_layout_inner = QVBoxLayout()
        self.chat_widget = ChatWidget()
        self.chat_widget.send_message_signal.connect(self._on_chat_message)
        self.chat_widget.interactive_clicked.connect(self._on_interactive_clicked)
        chat_layout_inner.addWidget(self.chat_widget)
        chat_group.setLayout(chat_layout_inner)
        chat_panel_layout.addWidget(chat_group)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_scroll)
        splitter.addWidget(right_widget)
        splitter.addWidget(chat_panel)
        splitter.setSizes([300, 780, 320])
        content_layout.addWidget(splitter)
        
        main_layout.addWidget(content_widget)
    
    def update_ui(self, data_copy):
        # 1. 刷新频谱
        spectrum = data_copy.get('spectrum')
        if spectrum and len(spectrum['freq']) > 0:
            self.curve_spectrum.setData(spectrum['freq'], spectrum['amp'])

            # 信道热力图 — 将 2048 个频点切分为 10 份，计算每个信道的能量峰值
            amp_array = np.array(spectrum['amp'])
            splits = np.array_split(amp_array, 10)
            channel_energy = np.array([np.max(s) for s in splits])

            # LO 泄露已在 MATLAB 端剔除
            self.channel_history[1:] = self.channel_history[:-1]
            self.channel_history[0] = channel_energy
            self.channel_im.setImage(self.channel_history.T, autoLevels=False)
            self.channel_im.setLevels([-100, -20])

        # 2. 刷新时域（已在后台线程降采样）
        time_domain = data_copy.get('time_domain')
        if time_domain and len(time_domain['time']) > 0:
            self.curve_time.setData(time_domain['time'], time_domain['amp'])

        # 3. 刷新星座图
        constellation = data_copy.get('constellation')
        if constellation and len(constellation['i']) > 0:
            i_data = np.array(constellation['i'])
            q_data = np.array(constellation['q'])
            step = max(1, len(i_data) // 1000)
            self.scatter_constellation.setData(x=i_data[::step], y=q_data[::step])
        else:
            self.scatter_constellation.clear()

        # 4. 刷新瀑布图
        wf = data_copy.get('waterfall')
        if wf is not None:
            # LO 泄露已被频率偏移 (Low-IF) 搬至 -80 kHz，信号在 0 Hz
            # 直接使用原始瀑布数据，不再剔除中心列
            self.waterfall_im.setImage(wf.T, autoLevels=False)
            self.waterfall_im.setLevels([-100, -35])

            st = data_copy.get('status', {})
            cf = st.get('center_frequency', 0) / 1e6
            sr = st.get('samp_rate', 0) / 1e6
            if cf > 0 and sr > 0:
                wf_key = (cf, sr)
                if self._last_wf_freq != wf_key:
                    self.waterfall_im.setRect(pg.QtCore.QRectF(cf - sr/2, 0, sr, 80))
                    self.plot_waterfall.setLabel('bottom', '物理频率 (MHz)')
                    self._last_wf_freq = wf_key

        # 5. 状态文本 / 切频清屏 / 模式切换检测
        status = data_copy.get('status')
        if status:
            cf = status.get('center_frequency', 0)

            if cf != 0:
                self.current_cf = cf

            # —— 批量更新标签，跳过值未变化的 ——
            new_texts = {
                'data_rec_valid':    status.get('data_rec_valid', '无效'),
                'rx_mode_name':      status.get('rx_mode_name', '仅图像'),
                'current_send_mode': status.get('current_send_mode', '等待接收'),
                'current_mod':       status.get('current_mod', '未知'),
                'center_frequency':  f'{cf / 1e9:.2f} GHz' if cf else '0 GHz',
                'samp_rate':         f'{status.get("samp_rate", 0) / 1e3:.2f} kHz' if status.get('samp_rate', 0) else '0 kHz',
                'snr':               status.get('snr', '信噪比无效'),
                'mes_valid':         status.get('mes_valid', '无效'),
                'mes_rate':          f"{status.get('mes_rate', 0):.2f} bps",
                'power_gain':        status.get('power_gain', '0 dB'),
                'carrier_gain':      status.get('carrier_gain', '0 GHz'),
                'fb_health':         status.get('fb_health', '--'),
                'current_time': datetime.now().strftime('%H:%M:%S'),
            }
            for key, text in new_texts.items():
                if self._last_label_texts.get(key) != text:
                    self.status_labels[key].setText(text)
                    self._last_label_texts[key] = text

        # Status text — only overwrite if disaster info hasn't been shown yet
        if getattr(self, "_ds_disaster_locked", False):
            pass  # keep disaster report
        else:
            received_text = status.get('received_text', '等待接收数据...')
            if self._last_label_texts.get('received_text') != received_text:
                self.received_text_label.setText(received_text)
                self._last_label_texts['received_text'] = received_text

            # ——— AI Agent 指令覆盖 ———
            decision_mutex.acquire()
            try:
                if decision_store.get('command_source') == 'ai_agent':
                    ai_mode = decision_store['anti_jamming_mode']
                    mode_names = {0: '常规模式', 1: '低速抗扰模式', 2: '切频模式'}
                    mod_names = {0: 'QPSK', 1: 'BPSK', 2: 'QPSK'}
                    self._sync_radio_buttons(ai_mode)

                    mode_str = mode_names.get(ai_mode, '未知')
                    mod_str = mod_names.get(ai_mode, 'QPSK')
                    if self._last_label_texts.get('current_send_mode') != mode_str:
                        self.status_labels['current_send_mode'].setText(mode_str)
                        self._last_label_texts['current_send_mode'] = mode_str
                    if self._last_label_texts.get('current_mod') != mod_str:
                        self.status_labels['current_mod'].setText(mod_str)
                        self._last_label_texts['current_mod'] = mod_str

                    ai_carrier = decision_store['carrier_select']
                    carrier_ghz = [2.0, 2.5, 3.0, 3.5, 4.0]
                    if 1 <= ai_carrier <= 5:
                        freq_str = f'{carrier_ghz[ai_carrier - 1]:.2f} GHz'
                        if self._last_label_texts.get('center_frequency') != freq_str:
                            self.status_labels['center_frequency'].setText(freq_str)
                            self._last_label_texts['center_frequency'] = freq_str

                    ai_power = decision_store['power_gain_select']
                    if 1 <= ai_power <= 31:
                        pwr_str = f'{ai_power - 1} dB'
                        if self._last_label_texts.get('power_gain') != pwr_str:
                            self.status_labels['power_gain'].setText(pwr_str)
                            self._last_label_texts['power_gain'] = pwr_str
            finally:
                decision_mutex.release()

        # 6. 刷新图片 / 视频 — skip if decision system is driving progressive reveal
        if self._ds_rescue_current is not None:
            return  # image is managed by _ds_reveal_step

        rx_mode = status.get('rx_mode_name', '') if status else ''
        image_file = data_copy.get('received_image')

        if rx_mode in ('仅视频', '图像+视频'):
            # 视频模式：加载 recovered_video.avi 并循环播放
            script_dir = os.path.dirname(os.path.abspath(__file__))
            video_path = os.path.join(script_dir, 'recovered', 'recovered_video.avi')
            if os.path.exists(video_path):
                mtime = os.path.getmtime(video_path)
                if self._video_path != video_path or self._video_mtime != mtime:
                    self._stop_video()
                    self._video_path = video_path
                    self._video_mtime = mtime
                    self._video_frames = self._load_video_frames(video_path)
                    if self._video_frames:
                        self._video_frame_idx = 0
                        self._video_active = True
                        self._video_tick()
                        self._video_timer.start(120)
                        # self.image_status_label.setText(f'视频播放中 ({len(self._video_frames)}帧)')
                    else:
                        pass  # self.image_status_label.setText('视频文件无法解码')
            elif self._video_active and not os.path.exists(video_path):
                # 视频文件尚未生成，显示预览图
                if image_file and os.path.exists(image_file):
                    pixmap = QPixmap(image_file)
                    if not pixmap.isNull():
                        scaled_pixmap = pixmap.scaled(
                            self.image_label.size().expandedTo(QSize(250, 180)),
                            Qt.KeepAspectRatio, Qt.SmoothTransformation)
                        self.image_label.setPixmap(scaled_pixmap)
        elif rx_mode == '仅图像':
            # 图片模式：停止视频，显示图片
            if self._video_active:
                self._stop_video()
                self.image_label.clear()

            if image_file and os.path.exists(image_file):
                try:
                    pixmap = QPixmap(image_file)
                    if not pixmap.isNull():
                        scaled_pixmap = pixmap.scaled(
                            self.image_label.size().expandedTo(QSize(250, 180)),
                            Qt.KeepAspectRatio, Qt.SmoothTransformation)
                        self.image_label.setPixmap(scaled_pixmap)
                        self.last_image_path = image_file
                except Exception as e:
                    print(f'加载图片错误: {str(e)}')
            elif image_file == '' or (image_file is None and self.last_image_path):
                self.image_label.clear()
                self.image_label.setText('当前非图片模式')
                self.last_image_path = None
        else:
            # 文本模式或其他：清除显示
            if self._video_active:
                self._stop_video()
            if image_file == '' or (image_file is None and self.last_image_path):
                self.image_label.clear()
                self.image_label.setText('等待接收数据...')
                self.last_image_path = None

if __name__ == '__main__':
    app_qt = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app_qt.exec_())