"""
AI Agent — 自然语言控制 USRP 发射机
通过 DeepSeek API 解析用户意图，切换发射任务（图片/视频/文本等）
"""
import json
import os
import glob
import threading
from openai import OpenAI

# ---------- 发射任务映射 ----------
TX_MODE_MAP = {"图片": 1, "视频": 2, "图片+视频": 3, "文本": 4}
TX_MODE_REVERSE = {1: "仅图像", 2: "仅视频", 3: "图像+视频", 4: "文本"}
VIDEO_EXTS = ['.mp4', '.avi', '.mov', '.mkv', '.wmv']
IMAGE_EXTS = ['.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.gif']

# 发射机脚本目录（媒体文件存放位置）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------- Tool Definitions ----------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_status",
            "description": "查询当前发射机系统运行状态，包括当前发射任务、发送文件、载波频率、发射增益等",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "switch_task",
            "description": "切换发射任务类型：图片、视频、图片+视频、或文本。切换到图片或视频时，如未指定文件名，会列出当前目录下可用的媒体文件供选择。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_type": {
                        "type": "string",
                        "enum": ["图片", "视频", "图片+视频", "文本"],
                        "description": "发射任务类型"
                    },
                    "file_name": {
                        "type": "string",
                        "description": "媒体文件名（可选）。图片模式需.jpg/.png等，视频模式需.mp4/.avi等。不指定则列出可用文件供选择。"
                    }
                },
                "required": ["task_type"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_text",
            "description": "设置文本模式下要发送的文本内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "要发送的文本内容"
                    }
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出当前发射机目录下可用的媒体文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_type": {
                        "type": "string",
                        "enum": ["图片", "视频", "全部"],
                        "description": "要列出的文件类型"
                    }
                },
                "required": ["file_type"]
            }
        }
    }
]

SYSTEM_PROMPT_TEMPLATE = """你是一个 SDR 软件无线电发射机控制助手。你必须通过调用工具函数来控制 USRP 发射机。

## 你的能力
- 查询发射机运行状态
- 切换发射任务（图片 / 视频 / 图片+视频 / 文本）
- 设置要发送的文本内容
- 列出可用媒体文件

## 当前发射机状态
- 发射任务: {tx_task}
- 图片文件: {image_file}
- 视频文件: {video_file}
- 文本内容: {text_string}
- 载波频率: {carrier_frequency}
- 发射增益: {tx_gain}
- 发送模式: {tx_mode}
- 调制方式: {tx_mod}
- {task_pending_info}

## 文件目录
当前发射机脚本目录: {script_dir}

## 切换任务流程（重要）
1. 如果用户要切换到"图片"或"视频"或"图片+视频"模式，**先调用 switch_task 工具且不填 file_name 参数**，工具会列出可用文件
2. 如果只有一个文件，工具会自动选择并完成切换
3. 如果有多个文件，工具会列出选项，你需要把列表展示给用户，让用户选择后再调用 switch_task 并填入 file_name
4. 如果用户要切换到"文本"模式，调用 switch_task(task_type="文本")，然后根据需要调用 set_text 设置文本内容

## 强制规则
1. 用户让你执行任何操作，必须调用对应的工具函数，不要用文字代替
2. 回答简洁，用中文
3. 即使用户说"好的"、"知道了"等不包含指令的话，也不要调用工具
4. 当用户说"第一个"、"第二个"、"选1"等，对应之前列出文件的编号"""

class AIAgentTX:
    """DeepSeek API 驱动的 USRP 发射机控制 Agent"""

    def __init__(self, decision_store, data_store, data_mutex, decision_mutex):
        api_key = "sk-372d4d3733b04b88926aba6df840cf81"
        self.client = OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com"
        )
        self.model = "deepseek-v4-flash"
        self.decision_store = decision_store
        self.data_store = data_store
        self.data_mutex = data_mutex
        self.decision_mutex = decision_mutex
        self.conversation = []
        self._conv_lock = threading.Lock()

    # ---- 文件扫描 ----

    def _scan_files(self, file_type):
        """扫描脚本目录下的媒体文件"""
        if file_type == "图片":
            exts = IMAGE_EXTS
        elif file_type == "视频":
            exts = VIDEO_EXTS
        else:
            exts = IMAGE_EXTS + VIDEO_EXTS

        files = []
        for ext in exts:
            pattern = os.path.join(SCRIPT_DIR, '*' + ext)
            for f in glob.glob(pattern):
                name = os.path.basename(f)
                if name not in files:
                    files.append(name)
        return sorted(files)

    # ---- 工具执行 ----

    def _execute_tool(self, tool_name, arguments):
        dm = self.decision_mutex
        ds = self.decision_store

        if tool_name == "query_status":
            self.data_mutex.acquire()
            try:
                status = dict(self.data_store.get('status', {}))
            finally:
                self.data_mutex.release()

            dm.acquire()
            try:
                mode_idx = ds['tx_mode']
                img_f = ds.get('image_file', '')
                vid_f = ds.get('video_file', '')
                txt = ds.get('text_string', '')
            finally:
                dm.release()

            task_name = TX_MODE_REVERSE.get(mode_idx, '未知')
            lines = [
                "=== 发射机当前状态 ===",
                f"发射任务: {task_name}",
                f"图片文件: {img_f or '未指定'}",
                f"视频文件: {vid_f or '未指定'}",
                f"文本内容: {txt[:50]}{'...' if len(txt) > 50 else ''}",
                f"载波频率: {status.get('tx_carrier', '未知')}",
                f"发射增益: {status.get('tx_gain', '未知')}",
                f"发送模式: {status.get('tx_mode', '未知')}",
                f"调制方式: {status.get('tx_mod', '未知')}",
                f"反向链路: {status.get('rx_state', '未知')}",
                f"更新时间: {status.get('time', '--:--:--')}",
            ]
            return "\n".join(lines)

        elif tool_name == "switch_task":
            task_type = arguments["task_type"]
            file_name = arguments.get("file_name", "").strip()

            mode_val = TX_MODE_MAP[task_type]

            # 文本模式不需要文件
            if task_type == "文本":
                dm.acquire()
                try:
                    changed = (ds['tx_mode'] != mode_val)
                    ds['tx_mode'] = mode_val
                    if changed:
                        ds['needs_update'] = True
                        ds['decision_version'] += 1
                        ds['command_source'] = 'ai_agent'
                        ds['command_description'] = f'AI Agent 切换发射任务到: {task_type}'
                    ds['task_pending'] = False
                    ds['pending_task_type'] = ''
                    ds['available_files'] = []
                finally:
                    dm.release()
                if changed:
                    return f"已切换到文本模式。当前文本内容: {ds.get('text_string', '')[:30]}...\n如需修改文本，请对我说「修改文本内容为xxx」。"
                else:
                    return f"当前已是文本模式，无需切换。如需修改文本内容，请对我说「修改文本内容为xxx」。"

            # 图片+视频模式需要两个文件
            if task_type == "图片+视频":
                if not file_name:
                    img_files = self._scan_files("图片")
                    vid_files = self._scan_files("视频")
                    dm.acquire()
                    try:
                        ds['task_pending'] = True
                        ds['pending_task_type'] = '图片+视频'
                        ds['available_files'] = []
                    finally:
                        dm.release()
                    lines = ["切换到图片+视频模式，请分别指定文件。", ""]
                    lines.append("可用图片文件:")
                    for i, f in enumerate(img_files, 1):
                        lines.append(f"  {i}. {f}")
                    lines.append("")
                    lines.append("可用视频文件:")
                    for i, f in enumerate(vid_files, 1):
                        lines.append(f"  {i}. {f}")
                    lines.append("")
                    lines.append('请告诉我图片和视频文件名，例如：「图片用p2.jpg，视频用视频.mp4」')
                    return "\n".join(lines)
                else:
                    # 单个文件名不合适，需要分别指定
                    return '图片+视频模式需要分别指定图片和视频文件。请说「图片用xxx，视频用xxx」。\n\n可用图片:\n' + "\n".join(f"  - {f}" for f in self._scan_files("图片")) + "\n\n可用视频:\n" + "\n".join(f"  - {f}" for f in self._scan_files("视频"))

            # 图片或视频模式
            if task_type == "图片":
                exts = IMAGE_EXTS
            else:
                exts = VIDEO_EXTS

            available = self._scan_files(task_type)

            if not file_name:
                # 用户没指定文件，列出可用的
                if len(available) == 0:
                    return f"当前目录下没有找到{'图片' if task_type == '图片' else '视频'}文件（{'/'.join(exts)}），请确保文件存在于 {SCRIPT_DIR}"

                if len(available) == 1:
                    # 只有一个文件，自动选择
                    file_name = available[0]
                else:
                    # 多个文件，让用户选择
                    dm.acquire()
                    try:
                        ds['task_pending'] = True
                        ds['pending_task_type'] = task_type
                        ds['available_files'] = available
                    finally:
                        dm.release()
                    lines = [f"当前目录下有这些{task_type}文件："]
                    for i, f in enumerate(available, 1):
                        lines.append(f"  {i}. {f}")
                    lines.append(f"\n请告诉我要发送哪个{task_type}？（可以说序号或文件名）")
                    return "\n".join(lines)

            # 验证文件存在
            if not os.path.exists(os.path.join(SCRIPT_DIR, file_name)):
                # 尝试匹配
                matched = [f for f in available if file_name in f]
                if len(matched) == 1:
                    file_name = matched[0]
                elif len(matched) > 1:
                    return f"找到多个匹配文件: {', '.join(matched)}，请更精确地指定文件名。"
                else:
                    return f"未找到文件 '{file_name}'。可用文件: {', '.join(available) if available else '无'}"

            # 应用切换
            dm.acquire()
            try:
                changed = (ds['tx_mode'] != mode_val)
                if task_type == "图片":
                    changed = changed or (ds.get('image_file', '') != file_name)
                    ds['image_file'] = file_name
                else:
                    changed = changed or (ds.get('video_file', '') != file_name)
                    ds['video_file'] = file_name
                ds['tx_mode'] = mode_val
                if changed:
                    ds['needs_update'] = True
                    ds['decision_version'] += 1
                    ds['command_source'] = 'ai_agent'
                    ds['command_description'] = f'AI Agent 切换发射任务到: {task_type} ({file_name})'
                ds['task_pending'] = False
                ds['pending_task_type'] = ''
                ds['available_files'] = []
            finally:
                dm.release()

            if changed:
                return f"已切换到{task_type}模式，将发送文件: {file_name}"
            else:
                return f"当前已是{task_type}模式，文件: {file_name}，无需切换。"

        elif tool_name == "set_text":
            text = arguments["text"]
            dm.acquire()
            try:
                ds['text_string'] = text
                if ds['tx_mode'] == 4:
                    ds['needs_update'] = True
                    ds['decision_version'] += 1
                    ds['command_source'] = 'ai_agent'
                    ds['command_description'] = f'AI Agent 更新文本内容'
                ds['task_pending'] = False
                ds['pending_task_type'] = ''
                ds['available_files'] = []
            finally:
                dm.release()

            if ds['tx_mode'] == 4:
                return f"已更新文本内容: {text[:80]}{'...' if len(text) > 80 else ''}"
            else:
                return f"已保存文本内容，但当前不是文本模式。请先说「切换到文本」以启用文本发送。\n已保存: {text[:60]}{'...' if len(text) > 60 else ''}"

        elif tool_name == "list_files":
            file_type = arguments["file_type"]
            files = self._scan_files(file_type)
            if not files:
                return f"当前目录下没有找到{file_type}文件。"
            lines = [f"当前目录下的{file_type}文件:"]
            for i, f in enumerate(files, 1):
                size = os.path.getsize(os.path.join(SCRIPT_DIR, f))
                size_str = f"{size/1024:.1f} KB" if size < 1024*1024 else f"{size/1024/1024:.1f} MB"
                lines.append(f"  {i}. {f} ({size_str})")
            return "\n".join(lines)

        return f"未知工具: {tool_name}"

    # ---- 构建系统提示 ----

    def _get_current_state(self):
        self.data_mutex.acquire()
        try:
            status = dict(self.data_store.get('status', {}))
        finally:
            self.data_mutex.release()

        self.decision_mutex.acquire()
        try:
            mode_idx = ds = self.decision_store['tx_mode']
            img_f = self.decision_store.get('image_file', '')
            vid_f = self.decision_store.get('video_file', '')
            txt = self.decision_store.get('text_string', '')
            pending = self.decision_store.get('task_pending', False)
            pending_type = self.decision_store.get('pending_task_type', '')
            avail = self.decision_store.get('available_files', [])
        finally:
            self.decision_mutex.release()

        task_name = TX_MODE_REVERSE.get(mode_idx, '未知')

        if pending and avail:
            pending_info = f"⚠ 正在等待用户选择{pending_type}文件: {', '.join(avail)}"
        else:
            pending_info = ""

        return SYSTEM_PROMPT_TEMPLATE.format(
            tx_task=task_name,
            image_file=img_f or '未指定',
            video_file=vid_f or '未指定',
            text_string=txt[:60] + ('...' if len(txt) > 60 else ''),
            carrier_frequency=status.get('tx_carrier', '未知'),
            tx_gain=status.get('tx_gain', '未知'),
            tx_mode=status.get('tx_mode', '未知'),
            tx_mod=status.get('tx_mod', '未知'),
            task_pending_info=pending_info,
            script_dir=SCRIPT_DIR,
        )

    # ---- 正则兜底 ----

    def _fallback_parse(self, text):
        import re

        # 检测"图片用X，视频用Y"格式
        img_match = re.search(r'图片用\s*(\S+\.(?:jpg|jpeg|png|bmp|tiff|gif))', text, re.IGNORECASE)
        vid_match = re.search(r'视频用\s*(\S+\.(?:mp4|avi|mov|mkv|wmv))', text, re.IGNORECASE)
        if img_match or vid_match:
            dm = self.decision_mutex
            ds = self.decision_store
            img_f = img_match.group(1) if img_match else ds.get('image_file', 'p2.jpg')
            vid_f = vid_match.group(1) if vid_match else ds.get('video_file', '视频.mp4')
            dm.acquire()
            try:
                ds['tx_mode'] = 3
                ds['image_file'] = img_f
                ds['video_file'] = vid_f
                ds['needs_update'] = True
                ds['decision_version'] += 1
                ds['command_source'] = 'ai_agent'
                ds['command_description'] = f'AI Agent 切换发射任务到: 图片+视频 ({img_f}, {vid_f})'
                ds['task_pending'] = False
                ds['pending_task_type'] = ''
                ds['available_files'] = []
            finally:
                dm.release()
            return ds.get('command_description', '')

        # 检测任务切换意图
        for task_name, mode_val in TX_MODE_MAP.items():
            if task_name in text:
                self._execute_tool("switch_task", {"task_type": task_name})
                dm = self.decision_mutex
                dm.acquire()
                try:
                    desc = self.decision_store.get('command_description', '')
                finally:
                    dm.release()
                return desc

        return ""

    def _call_api(self, messages):
        """带重试的 API 调用"""
        import time
        max_retries = 3
        for attempt in range(max_retries):
            try:
                return self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=TOOLS,
                    temperature=0.1,
                    max_tokens=1024,
                )
            except Exception as e:
                err = str(e)
                if '503' in err or 'busy' in err.lower() or 'overload' in err.lower():
                    if attempt < max_retries - 1:
                        wait = (attempt + 1) * 2
                        print(f'[TX AI Agent] 服务繁忙，{wait}秒后重试 ({attempt+1}/{max_retries})...')
                        time.sleep(wait)
                        continue
                raise
        raise RuntimeError('API 重试次数已用完')

    # ---- 主对话接口 ----

    def chat(self, user_message):
        system_prompt = self._get_current_state()

        with self._conv_lock:
            messages = [{"role": "system", "content": system_prompt}]
            messages += self.conversation[-40:]
            messages.append({"role": "user", "content": user_message})

        action_summary = ""

        try:
            response = self._call_api(messages)

            msg = response.choices[0].message

            if not msg.tool_calls:
                reply_text = (msg.content or "").strip()
                # _fallback_parse 只在用户消息上执行，不在 LLM 回复上执行
                action_summary = self._fallback_parse(user_message)

                with self._conv_lock:
                    self.conversation.append({"role": "user", "content": user_message})
                    self.conversation.append({"role": "assistant", "content": reply_text})
                    if len(self.conversation) > 80:
                        self.conversation = self.conversation[-40:]
                return reply_text, action_summary

            tool_results = []
            natural_replies = []
            for tc in msg.tool_calls:
                tool_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                print(f"[TX AI Agent] 执行工具: {tool_name}({args})")
                result = self._execute_tool(tool_name, args)

                if tool_name != "query_status":
                    action_summary = self.decision_store.get("command_description", "")
                    tool_results.append(f"✅ {result}")
                    natural_replies.append(f"好的，{result}")
                else:
                    tool_results.append(result)
                    natural_replies.append(result)

            reply_text = "\n".join(tool_results)
            history_reply = "\n".join(natural_replies)
            with self._conv_lock:
                self.conversation.append({"role": "user", "content": user_message})
                self.conversation.append({"role": "assistant", "content": history_reply})
                if len(self.conversation) > 80:
                    self.conversation = self.conversation[-40:]

            print(f"[TX AI Agent] 回复: {reply_text[:100]}")
            return reply_text, action_summary

        except Exception as e:
            error_detail = str(e)
            print(f"[TX AI Agent] API 调用失败: {error_detail}")
            if action_summary:
                return f"指令已下发（{action_summary}），但AI回复生成失败: {error_detail[:80]}", action_summary
            return f"AI 服务调用失败: {error_detail[:120]}", ""
