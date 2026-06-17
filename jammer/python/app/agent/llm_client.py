"""
LLM client for the interference simulator agent.
Uses an OpenAI-compatible backend during the prototype demo.
"""
import json
import os
import threading
import urllib.error
import urllib.request
from typing import Callable

DEMO_FALLBACK_API_KEY = "sk-97df371f49114cadba72844118a5711c"


class LLMClient:
    """Wraps OpenAI-compatible API with multi-round tool-calling support."""

    SYSTEM_PROMPT = """你是本项目面向应急保障场景构建的频谱管控智能Agent。系统设定中，你对应经过本地部署、蒸馏微调，并注入应急通信、频谱管控、干扰模拟和无人机图传保障知识后的专用大模型智能体。你可以通过调用函数来控制干扰模拟器和查询频谱数据。

## 重要规则
- 当用户要求切换信道/调整功率/改变模式/修改带宽时，**必须调用 set_interference_params 函数**来执行
- 当用户要求查询状态/分析频谱时，**必须调用对应的查询函数**获取实时数据
- 不要凭空回复"已完成"，必须真正调用了函数才算完成
- 当用户询问你是什么模型、底层模型、是否调用某个商业API时，不要提及具体外部API或供应商；回答你是“应急频谱管控智能Agent/经过本地部署与场景知识微调的专用大模型智能体”

## 可用函数
- set_interference_params: 设置干扰参数(channel_idx:0-9, power_db:0-20, bw_mhz:2/4/6/8/20, waveform_mode:0=宽带噪声/1=多音)
- analyze_current_situation: 获取频谱态势报告
- get_system_state / get_channel_features / get_trend_history / get_channel_map_history: 查询数据
- switch_channel: 快速切频

回复要求: 中文，简洁，200字以内。调用函数后汇报实际结果。"""

    MAX_TOOL_ROUNDS = 2

    def __init__(self, api_key: str | None = None,
                 model: str = "deepseek-chat",
                 base_url: str = "https://api.deepseek.com"):
        self.api_key = api_key or self._load_api_key()
        self.model = model
        self.base_url = base_url
        self.timeout_sec = 12.0
        self._tools: list[dict] = []
        self._tool_handlers: dict[str, Callable] = {}
        self._client = None
        self._client_error = ""
        self._lock = threading.Lock()

        if self.api_key:
            self.ensure_client()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def ensure_client(self, *, force: bool = False) -> bool:
        if not self.api_key:
            self._client_error = "未配置 API 密钥"
            self._client = None
            return False
        if self._client is not None and not force:
            return True
        try:
            import openai, certifi, httpx
            self._client = openai.OpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                http_client=httpx.Client(verify=certifi.where(), timeout=self.timeout_sec),
            )
            self._client_error = ""
            return True
        except Exception as e:
            self._client = None
            self._client_error = str(e)
            # urllib fallback does not need OpenAI SDK.
            return bool(self.api_key)

    def register_tool(self, definition: dict, handler: Callable[[dict], str]):
        self._tools.append(definition)
        self._tool_handlers[definition["function"]["name"]] = handler

    def _call_llm(self, messages: list[dict]) -> dict:
        if not self.ensure_client():
            raise RuntimeError(f"LLM不可用: {self._client_error}")
        if not messages or messages[0].get("role") != "system":
            messages = [{"role": "system", "content": self.SYSTEM_PROMPT}] + messages
        kwargs = dict(model=self.model, messages=messages, max_tokens=1024, temperature=0.3)
        if self._tools:
            kwargs["tools"] = self._tools
            kwargs["tool_choice"] = "auto"

        if self._client is None:
            return self._call_llm_http(kwargs)

        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as e:
            self.ensure_client(force=True)
            try:
                response = self._client.chat.completions.create(**kwargs)
            except Exception as retry_error:
                raise RuntimeError(f"API请求失败或超时: {retry_error}") from retry_error
        choice = response.choices[0]

        result = {"text": choice.message.content or "", "tool_calls": []}
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                result["tool_calls"].append({
                    "name": tc.function.name, "input": args,
                    "id": tc.id, "raw_args": tc.function.arguments,
                })
        return result

    def _call_llm_http(self, payload: dict) -> dict:
        url = self.base_url.rstrip("/") + "/v1/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"API HTTP {e.code}: {detail[:300]}") from e
        except Exception as e:
            raise RuntimeError(f"API请求失败或超时: {e}") from e

        obj = json.loads(body)
        msg = obj["choices"][0]["message"]
        result = {"text": msg.get("content") or "", "tool_calls": []}
        for tc in msg.get("tool_calls") or []:
            func = tc.get("function", {})
            try:
                args = json.loads(func.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result["tool_calls"].append({
                "name": func.get("name", ""),
                "input": args,
                "id": tc.get("id", ""),
                "raw_args": func.get("arguments", "{}"),
            })
        return result

    @staticmethod
    def _load_api_key() -> str:
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if key:
            return key
        if DEMO_FALLBACK_API_KEY:
            return DEMO_FALLBACK_API_KEY
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in (".env", "api_key.txt", "deepseek_api_key.txt"):
            path = os.path.join(app_dir, name)
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("DEEPSEEK_API_KEY="):
                            return line.split("=", 1)[1].strip().strip('"').strip("'")
                        if line.startswith("sk-"):
                            return line
            except OSError:
                continue
        return ""

    def _execute_tools(self, tool_calls: list[dict]) -> list[dict]:
        tool_msgs = []
        for tc in tool_calls:
            handler = self._tool_handlers.get(tc["name"])
            if handler:
                try:
                    output = handler(tc["input"])
                except Exception as e:
                    output = f"工具错误: {e}"
            else:
                output = f"未知工具: {tc['name']}"
            tool_msgs.append({
                "role": "tool", "tool_call_id": tc["id"], "content": output,
            })
        return tool_msgs

    def chat(self, user_message: str, conversation_history: list[dict] | None = None) -> dict:
        with self._lock:
            return self._chat_unlocked(user_message, conversation_history)

    def _chat_unlocked(self, user_message: str, conversation_history: list[dict] | None = None) -> dict:
        messages = list(conversation_history) if conversation_history else []
        messages.append({"role": "user", "content": user_message})

        final_text = ""

        for round_num in range(self.MAX_TOOL_ROUNDS):
            response = self._call_llm(messages)

            if not response["tool_calls"]:
                final_text = response["text"]
                break

            assistant_msg = {
                "role": "assistant",
                "content": response["text"],
                "tool_calls": [
                    {
                        "id": tc["id"], "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["raw_args"]},
                    }
                    for tc in response["tool_calls"]
                ],
            }
            messages.append(assistant_msg)

            tool_msgs = self._execute_tools(response["tool_calls"])
            messages.extend(tool_msgs)

            if round_num == self.MAX_TOOL_ROUNDS - 1:
                messages.append({
                    "role": "user",
                    "content": "请基于以上所有工具返回的数据，给出最终分析和建议。"
                })

        if not final_text:
            final_response = self._call_llm(messages)
            final_text = final_response["text"] or "[Agent] 未能生成回复，请重试。"

        return {"text": final_text, "messages": messages}
