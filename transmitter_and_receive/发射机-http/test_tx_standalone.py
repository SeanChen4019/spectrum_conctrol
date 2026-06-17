"""
发射机独立调试脚本 — 无需 MATLAB / USRP 硬件
模拟 MATLAB 轮询行为，验证 AI Agent 任务切换全流程

用法:
    python test_tx_standalone.py

先启动发射机 UI (python ui_trans_pyqt5.py)，再运行此脚本。
如果接收机 UI 也在运行，可以同时验证 task_sync 通知。
"""
import time
import json
import requests

TX_URL = 'http://127.0.0.1:5001'
RX_URL = 'http://127.0.0.1:5000'

MODE_NAMES = {1: '仅图像', 2: '仅视频', 3: '图像+视频', 4: '文本'}


def check_health():
    """检查发射机 Flask 是否在线"""
    try:
        r = requests.get(f'{TX_URL}/api/health', timeout=2)
        if r.status_code == 200:
            print('[OK] 发射机 Flask 在线 (端口 5001)')
            return True
    except Exception:
        pass
    print('[FAIL] 发射机 Flask 未启动，请先运行: python ui_trans_pyqt5.py')
    return False


def check_receiver():
    """检查接收机 Flask 是否在线"""
    try:
        r = requests.get(f'{RX_URL}/api/health', timeout=2)
        if r.status_code == 200:
            print('[OK] 接收机 Flask 在线 (端口 5000) — 可验证 task_sync 通知')
            return True
    except Exception:
        pass
    print('[INFO] 接收机 Flask 未启动 — task_sync 通知将失败（不影响发射机调试）')
    return False


def poll_tx_decision(last_version=0):
    """模拟 MATLAB 的 /api/tx_decision 轮询"""
    try:
        r = requests.get(f'{TX_URL}/api/tx_decision', timeout=2)
        data = r.json()
        if data.get('needs_update') and data.get('decision_version', 0) != last_version:
            print(f"\n{'='*60}")
            print(f"检测到任务变更! version={data['decision_version']}")
            print(f"  发射任务: {MODE_NAMES.get(data['tx_mode'], '未知')} (mode={data['tx_mode']})")
            print(f"  图片文件: {data['image_file']}")
            print(f"  视频文件: {data['video_file']}")
            print(f"  文本内容: {data['text_string'][:50]}...")
            print(f"  指令来源: {data['command_source']}")
            print(f"  指令描述: {data['command_description']}")
            print(f"{'='*60}\n")
            return data['decision_version']
    except Exception as e:
        print(f'[轮询错误] {e}')
    return last_version


def check_task_sync():
    """检查接收机是否收到 task_sync 通知"""
    try:
        r = requests.get(f'{RX_URL}/api/decision', timeout=2)
        data = r.json()
        rx_mode = data.get('rx_mode', 1)
        rx_changed = data.get('rx_mode_changed', False)
        cmd_src = data.get('command_source', '')
        cmd_desc = data.get('command_description', '')
        print(f'  接收机 rx_mode={rx_mode} ({MODE_NAMES.get(rx_mode, "未知")}) '
              f'changed={rx_changed} src={cmd_src}')
        if cmd_desc:
            print(f'  接收机描述: {cmd_desc}')
        return True
    except Exception:
        return False


def main():
    print('发射机独立调试模式\n')
    print('说明: 此脚本模拟 MATLAB 轮询 /api/tx_decision 的行为')
    print('在发射机 UI 的 AI 聊天框中输入指令即可触发任务切换\n')

    if not check_health():
        return

    has_receiver = check_receiver()

    print('\n开始轮询，等待任务切换指令... (Ctrl+C 退出)\n')
    print('试试在 AI 聊天框输入:')
    print('  - "查询状态"')
    print('  - "切换到视频"')
    print('  - "切换到文本"')
    print('  - "列出图片文件"\n')

    last_version = 0
    try:
        while True:
            last_version = poll_tx_decision(last_version)

            # 轮询间隔：匹配 MATLAB 的 ctrl_period (20帧 × 循环耗时)
            time.sleep(2.0)

    except KeyboardInterrupt:
        print('\n调试结束。')


if __name__ == '__main__':
    main()
