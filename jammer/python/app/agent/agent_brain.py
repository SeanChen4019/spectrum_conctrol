"""
Agent orchestrator: local command parser first, LLM fallback for complex requests.
"""
from .llm_client import LLMClient
from .tools import ALL_TOOL_DEFS, ToolHandlers
from .strategy_engine import CommandParser


class AgentBrain:

    def __init__(self, api_key: str | None = None,
                 model: str = "deepseek-chat",
                 base_url: str = "https://api.deepseek.com"):
        self.tools = ToolHandlers()
        self.parser = CommandParser(self.tools)
        self.llm = LLMClient(api_key=api_key, model=model, base_url=base_url)
        self._enabled = True

        _handler_map = {
            "get_system_state": self.tools.handle_get_system_state,
            "get_channel_features": self.tools.handle_get_channel_features,
            "get_trend_history": self.tools.handle_get_trend_history,
            "get_channel_map_history": self.tools.handle_get_channel_map_history,
            "set_interference_params": self.tools.handle_set_interference_params,
            "analyze_current_situation": self.tools.handle_analyze_situation,
            "switch_channel": self.tools.handle_switch_channel,
            "query_system_info": self.tools.handle_query_system_info,
        }
        for tool_def in ALL_TOOL_DEFS:
            name = tool_def["function"]["name"]
            if name in _handler_map:
                self.llm.register_tool(tool_def, _handler_map[name])

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_api_key(self, key: str):
        self.llm.api_key = key
        if key:
            self.llm.ensure_client()

    def update_state(self, **kwargs):
        self.tools.update_state(**kwargs)

    def try_local_message(self, user_message: str) -> str | None:
        """Try deterministic local parsing without touching the LLM/API path."""
        return self.parser.try_parse(user_message)

    def send_message(self, user_message: str) -> str:
        # ── Fast path: local command parser ─────────────────
        try:
            local_result = self.try_local_message(user_message)
            if local_result is not None:
                return local_result
        except Exception:
            import traceback
            traceback.print_exc()

        if not self.llm.available:
            fallback = self.parser.try_domain_qa(user_message)
            if fallback is not None:
                return fallback + "\n\n[提示] 当前未配置可用 API key，上述为本地兜底回答。"
            return ("[Agent] 智能体推理链路暂未就绪。"
                    "明确控制指令仍可本地执行；开放式问答需要可用的模型服务。")

        # ── Slow path: LLM ──────────────────────────────────
        try:
            result = self.llm.chat(user_message, None)
            text = result.get("text", "[Agent] 未能获取回复。")
            return f"{text}\n\n[模型] 已调用应急频谱智能体推理服务"
        except Exception as e:
            fallback = self.parser.try_domain_qa(user_message)
            if fallback is not None:
                return fallback + f"\n\n[提示] 模型服务调用失败，已使用本地兜底回答。错误: {e}"
            import traceback
            detail = traceback.format_exc()
            lines = detail.strip().split('\n')
            short = '\n'.join(lines[-4:]) if len(lines) > 4 else detail
            return f"[Agent] 调用智能体推理服务出错: {e}\n{short}"

    def set_on_set_params(self, callback):
        self.tools.set_on_set_params(callback)
