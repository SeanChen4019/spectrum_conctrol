"""
AI Agent — 自然语言控制 USRP 接收机
通过 DeepSeek API 解析用户意图，映射为硬件控制指令写入 decision_store
"""
import json
import os
import threading
from openai import OpenAI

# ---------- 控制参数映射表 ----------

CARRIER_SET_GHZ = [2.0, 2.5, 3.0, 3.5, 4.0]  # MATLAB Carrier_set / 1e9
POWER_GAIN_RANGE = list(range(0, 31))          # MATLAB Power_gain_set: 0:1:30
MODE_MAP = {"常规模式": 0, "低速抗扰模式": 1, "切频模式": 2}
MODE_REVERSE = {0: "常规模式", 1: "低速抗扰模式", 2: "切频模式"}

# ---------- Tool Definitions (OpenAI function-calling format) ----------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "switch_mode",
            "description": "切换抗干扰模式：常规模式(QPSK)、低速抗扰模式(BPSK+扩频)、切频模式(跳频QPSK)",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["常规模式", "低速抗扰模式", "切频模式"],
                        "description": "抗干扰模式名称"
                    }
                },
                "required": ["mode"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "switch_frequency",
            "description": "切换载波中心频率，可选值为 2.0, 2.5, 3.0, 3.5, 4.0 GHz",
            "parameters": {
                "type": "object",
                "properties": {
                    "frequency_ghz": {
                        "type": "number",
                        "description": "载波频率(GHz)，只能从 2.0, 2.5, 3.0, 3.5, 4.0 中选择"
                    }
                },
                "required": ["frequency_ghz"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "adjust_power",
            "description": "调整发射功率增益，范围 0-30 dB，步进 1 dB",
            "parameters": {
                "type": "object",
                "properties": {
                    "power_db": {
                        "type": "integer",
                        "description": "功率增益(dB)，0 到 30 之间的整数"
                    }
                },
                "required": ["power_db"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_threshold",
            "description": "设置接收端帧同步检测阈值，影响信号捕获灵敏度",
            "parameters": {
                "type": "object",
                "properties": {
                    "threshold": {
                        "type": "integer",
                        "description": "同步阈值，建议范围 150-500，越大越严格"
                    }
                },
                "required": ["threshold"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_status",
            "description": "查询当前系统运行状态，包括信噪比、当前模式、频率、功率、误码率等",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    }
]

SYSTEM_PROMPT_TEMPLATE = """你是一个 SDR 软件无线电接收机控制助手。你必须通过调用工具函数来控制 USRP 接收机。

## 你的能力
- 切换抗干扰模式（常规/低速抗扰/切频）
- 切换载波频率（2.0-4.0 GHz）
- 调整发射功率（0-30 dB）
- 设置同步检测阈值
- 查询系统运行状态

## 当前系统状态
- 抗干扰模式: {anti_jamming_mode}
- 载波频率: {carrier_frequency} GHz
- 功率增益: {power_gain} dB
- 同步阈值: {threshold}
- 信噪比: {snr}
- 调制方式: {current_mod}
- 数据接收: {data_rec_valid}
- 误码率: {ber}

## 强制规则（必须遵守）
1. 用户让你执行任何操作（切换模式、改频率、调功率、改阈值、查状态），你必须调用对应的工具函数，绝对不要用文字回复代替
2. 切频只能选择 2.0, 2.5, 3.0, 3.5, 4.0 GHz
3. 功率增益为 0-30 dB 整数
4. 回答简洁，用中文
5. 即使用户说"好的"、"知道了"等不包含指令的话，也不要调用工具"""

class AIAgent:
    """DeepSeek API 驱动的 USRP 控制 Agent"""

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

        # 多轮对话历史 (OpenAI 格式: list of {"role": ..., "content": ...})
        self.conversation = []
        self._conv_lock = threading.Lock()

    # ---- 工具执行 ----

    def _execute_tool(self, tool_name, arguments):
        """执行工具调用，返回结果字符串"""
        dm = self.decision_mutex
        ds = self.decision_store

        if tool_name == "switch_mode":
            mode_name = arguments["mode"]
            mode_val = MODE_MAP[mode_name]
            dm.acquire()
            try:
                ds["anti_jamming_mode"] = mode_val
                ds["needs_update"] = True
                ds["decision_version"] += 1
                ds["command_source"] = "ai_agent"
                ds["command_description"] = f"AI Agent 切换模式到: {mode_name}"
            finally:
                dm.release()
            return f"已切换到{mode_name}。"

        elif tool_name == "switch_frequency":
            freq = arguments["frequency_ghz"]
            closest = min(CARRIER_SET_GHZ, key=lambda x: abs(x - freq))
            idx = CARRIER_SET_GHZ.index(closest) + 1  # MATLAB 1-indexed
            dm.acquire()
            try:
                ds["carrier_select"] = idx
                ds["needs_update"] = True
                ds["decision_version"] += 1
                ds["command_source"] = "ai_agent"
                ds["command_description"] = f"AI Agent 切换频率到: {closest} GHz"
            finally:
                dm.release()
            if abs(closest - freq) > 0.01:
                return f"频率 {freq} GHz 不在可选列表中，已自动匹配到最近的 {closest} GHz。"
            return f"已切换载波频率到 {closest} GHz。"

        elif tool_name == "adjust_power":
            power = max(0, min(30, arguments["power_db"]))
            idx = power + 1  # MATLAB 1-indexed, Power_gain_set = 0:1:30
            dm.acquire()
            try:
                ds["power_gain_select"] = idx
                ds["needs_update"] = True
                ds["decision_version"] += 1
                ds["command_source"] = "ai_agent"
                ds["command_description"] = f"AI Agent 调整功率到: {power} dB"
            finally:
                dm.release()
            return f"已调整发射功率增益到 {power} dB。"

        elif tool_name == "set_threshold":
            thr = max(50, min(1000, arguments["threshold"]))
            dm.acquire()
            try:
                ds["threshold"] = thr
                ds["needs_update"] = True
                ds["decision_version"] += 1
                ds["command_source"] = "ai_agent"
                ds["command_description"] = f"AI Agent 设置阈值: {thr}"
            finally:
                dm.release()
            return f"已设置同步阈值为 {thr}。"

        elif tool_name == "query_status":
            self.data_mutex.acquire()
            try:
                status = dict(self.data_store["status"])
            finally:
                self.data_mutex.release()

            self.decision_mutex.acquire()
            try:
                current_mode_idx = ds["anti_jamming_mode"]
                current_carrier_idx = ds["carrier_select"]
                current_power_idx = ds["power_gain_select"]
                current_thr = ds["threshold"]
            finally:
                self.decision_mutex.release()

            mode_name = MODE_REVERSE.get(current_mode_idx, "未知")
            if 1 <= current_carrier_idx <= len(CARRIER_SET_GHZ):
                freq_str = f"{CARRIER_SET_GHZ[current_carrier_idx - 1]} GHz"
            else:
                freq_str = "未知"
            if 1 <= current_power_idx <= len(POWER_GAIN_RANGE):
                power_str = f"{POWER_GAIN_RANGE[current_power_idx - 1]} dB"
            else:
                power_str = "未知"

            lines = [
                f"=== 当前系统状态 ===",
                f"抗干扰模式: {mode_name}",
                f"载波频率: {freq_str}",
                f"功率增益: {power_str}",
                f"同步阈值: {current_thr}",
                f"调制方式: {status.get('current_mod', '未知')}",
                f"数据接收: {status.get('data_rec_valid', '未知')}",
                f"信噪比: {status.get('snr', '未知')}",
                f"误码率: {status.get('ber', '未知')}",
                f"当前时间: {status.get('current_time', '--:--:--')}",
            ]
            return "\n".join(lines)

        return f"未知工具: {tool_name}"

    # ---- 构建系统提示 ----

    def _get_current_state(self):
        """读取当前系统状态并构建系统提示"""
        self.data_mutex.acquire()
        try:
            status = dict(self.data_store["status"])
        finally:
            self.data_mutex.release()

        self.decision_mutex.acquire()
        try:
            mode_idx = self.decision_store["anti_jamming_mode"]
            carrier_idx = self.decision_store["carrier_select"]
            power_idx = self.decision_store["power_gain_select"]
            thr = self.decision_store["threshold"]
        finally:
            self.decision_mutex.release()

        mode_name = MODE_REVERSE.get(mode_idx, "未知")
        if 1 <= carrier_idx <= len(CARRIER_SET_GHZ):
            freq_str = f"{CARRIER_SET_GHZ[carrier_idx - 1]}"
        else:
            freq_str = "未知"
        if 1 <= power_idx <= len(POWER_GAIN_RANGE):
            power_str = f"{POWER_GAIN_RANGE[power_idx - 1]}"
        else:
            power_str = "未知"

        return SYSTEM_PROMPT_TEMPLATE.format(
            anti_jamming_mode=mode_name,
            carrier_frequency=freq_str,
            power_gain=power_str,
            threshold=thr,
            snr=status.get("snr", "未知"),
            current_mod=status.get("current_mod", "未知"),
            data_rec_valid=status.get("data_rec_valid", "未知"),
            ber=status.get("ber", "未知"),
        )

    # ---- 正则兜底：LLM 不调工具时从文本提取意图 ----

    def _fallback_parse(self, text):
        """从 LLM 文本回复中正则提取控制意图，返回 action_summary"""
        import re

        # 检测模式切换
        mode_match = re.search(r'(常规模式|低速抗扰模式|切频模式)', text)
        if mode_match:
            mode_name = mode_match.group(1)
            self._execute_tool("switch_mode", {"mode": mode_name})
            return self.decision_store.get("command_description", "")

        # 检测频率切换
        freq_match = re.search(r'(\d+\.?\d*)\s*GHz', text)
        if freq_match:
            freq = float(freq_match.group(1))
            self._execute_tool("switch_frequency", {"frequency_ghz": freq})
            return self.decision_store.get("command_description", "")

        # 检测功率调整
        power_match = re.search(r'(\d+)\s*dB', text)
        if power_match:
            power = int(power_match.group(1))
            self._execute_tool("adjust_power", {"power_db": power})
            return self.decision_store.get("command_description", "")

        # 检测阈值
        thr_match = re.search(r'阈值[设为改调].*?(\d+)', text)
        if thr_match:
            thr = int(thr_match.group(1))
            self._execute_tool("set_threshold", {"threshold": thr})
            return self.decision_store.get("command_description", "")

        return ""

    # ---- 主对话接口 ----

    def chat(self, user_message):
        """
        处理用户消息，返回 (reply_text, action_summary)
        工具调用后直接返回结果，不发起二次 API 调用，避免 400 错误
        """
        system_prompt = self._get_current_state()

        with self._conv_lock:
            messages = [{"role": "system", "content": system_prompt}]
            messages += self.conversation[-40:]
            messages.append({"role": "user", "content": user_message})

        action_summary = ""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=TOOLS,
                temperature=0.1,
                max_tokens=1024,
            )

            msg = response.choices[0].message

            # 如果 LLM 直接返回文本（无需工具调用），尝试正则提取意图兜底
            if not msg.tool_calls:
                reply_text = (msg.content or "").strip()

                # 正则兜底：从 LLM 文本中提取控制意图
                action_summary = self._fallback_parse(reply_text)

                with self._conv_lock:
                    self.conversation.append({"role": "user", "content": user_message})
                    self.conversation.append({"role": "assistant", "content": reply_text})
                    if len(self.conversation) > 80:
                        self.conversation = self.conversation[-40:]
                return reply_text, action_summary

            # 需要工具调用：执行后直接用工具返回值作为回复，不发起二次 API 调用
            tool_results = []
            natural_replies = []  # 存对话历史用，避免 LLM 模仿工具格式
            for tc in msg.tool_calls:
                tool_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                print(f"[AI Agent] 执行工具: {tool_name}({args})")
                result = self._execute_tool(tool_name, args)

                if tool_name != "query_status":
                    action_summary = self.decision_store.get("command_description", "")
                    tool_results.append(f"✅ {result}")
                    natural_replies.append(f"好的，{result}")
                else:
                    tool_results.append(result)
                    natural_replies.append(result)

            # UI 显示用 tool_result 格式
            reply_text = "\n".join(tool_results)
            # 对话历史用自然语言，防止 LLM 下轮模仿 ✅ 格式而不调用工具
            history_reply = "\n".join(natural_replies)
            with self._conv_lock:
                self.conversation.append({"role": "user", "content": user_message})
                self.conversation.append({"role": "assistant", "content": history_reply})
                if len(self.conversation) > 80:
                    self.conversation = self.conversation[-40:]

            print(f"[AI Agent] 回复: {reply_text[:100]}")
            return reply_text, action_summary

        except Exception as e:
            error_detail = str(e)
            print(f"[AI Agent] API 调用失败: {error_detail}")
            if action_summary:
                return f"指令已下发（{action_summary}），但AI回复生成失败: {error_detail[:80]}", action_summary
            return f"AI 服务调用失败: {error_detail[:120]}", ""
