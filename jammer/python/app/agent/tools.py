"""
Tool definitions (OpenAI function-calling format) for the interference simulator agent.
"""
import json
from typing import Callable


def _tool(name: str, desc: str, params: dict | None = None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": params or {},
                "required": list(params.keys()) if params else [],
            },
        },
    }


# ── Tool definitions ──────────────────────────────────────────────

TOOL_GET_SYSTEM_STATE = _tool(
    "get_system_state",
    "获取当前系统实时状态，包括当前信道、发射功率、干扰带宽、干扰模式、信噪比和最新更新时间等KPI指标。",
)

TOOL_GET_CHANNEL_FEATURES = _tool(
    "get_channel_features",
    "获取所有10个信道的详细特征数据，包括每个信道的占用度(0-1)、干扰水平(0-1)、效用值(0-1)和类信噪比指标(0-1)。",
)

TOOL_GET_TREND_HISTORY = _tool(
    "get_trend_history",
    "获取最近N帧(默认60)的历史趋势数据，包括信噪比序列、带宽序列和功率序列，用于分析系统运行趋势。",
    {
        "num_frames": {
            "type": "integer",
            "description": "获取最近多少帧的数据，默认60帧，最大120帧",
        },
    },
)

TOOL_GET_CHANNEL_MAP_HISTORY = _tool(
    "get_channel_map_history",
    "获取信道状态热力图的历史数据。返回最近N帧的10信道状态矩阵，状态值: 0=空闲 1=用户占用 2=被干扰 3=重叠。",
    {
        "num_frames": {
            "type": "integer",
            "description": "获取最近多少帧的数据，默认30帧，最大120帧",
        },
    },
)

TOOL_SET_INTERFERENCE_PARAMS = _tool(
    "set_interference_params",
    "设置干扰模拟器参数。可以修改目标信道、发射功率、干扰带宽和干扰模式。修改后的参数将通过控制通道发送到后端执行。",
    {
        "channel_idx": {
            "type": "integer",
            "description": "目标信道编号 0-9 (对应信道1-10)，不修改则省略",
        },
        "power_db": {
            "type": "number",
            "description": "发射功率 0-20 dB，常用值: 0/5/10/15/20，不修改则省略",
        },
        "bw_mhz": {
            "type": "integer",
            "description": "干扰带宽 2/4/6/8 MHz，不修改则省略",
        },
        "waveform_mode": {
            "type": "integer",
            "description": "干扰模式: 0=宽带噪声(全频段压制) 1=多音(精准频点干扰)，不修改则省略",
        },
    },
)

TOOL_ANALYZE_SITUATION = _tool(
    "analyze_current_situation",
    "获取当前完整的频谱态势数据(系统状态+信道特征+近期趋势+信道热力图)，用于综合分析。",
)

TOOL_SWITCH_CHANNEL = _tool(
    "switch_channel",
    "快速切换干扰目标信道。输入信道编号即可切换到指定信道。",
    {
        "channel_idx": {
            "type": "integer",
            "description": "目标信道编号 0-9 (对应信道1-10)",
        },
    },
)

TOOL_QUERY_SYSTEM_INFO = _tool(
    "query_system_info",
    "查询系统综合信息，包括当前干扰参数配置、频谱环境概况和运行状态。",
)

ALL_TOOL_DEFS = [
    TOOL_GET_SYSTEM_STATE,
    TOOL_GET_CHANNEL_FEATURES,
    TOOL_GET_TREND_HISTORY,
    TOOL_GET_CHANNEL_MAP_HISTORY,
    TOOL_SET_INTERFERENCE_PARAMS,
    TOOL_ANALYZE_SITUATION,
    TOOL_SWITCH_CHANNEL,
    TOOL_QUERY_SYSTEM_INFO,
]


# ── Handler registry ──────────────────────────────────────────────

class ToolHandlers:
    """Registry of tool handler functions that bridge LLM tools to UI data."""

    def __init__(self):
        self._system_state: dict = {}
        self._channel_features: list = []
        self._snr_history: list = []
        self._bw_history: list = []
        self._power_history: list = []
        self._channel_map_history: list = []
        self._on_set_params: Callable | None = None

    def update_state(self, *, system_state: dict | None = None,
                     channel_features: list | None = None,
                     snr_history: list | None = None,
                     bw_history: list | None = None,
                     power_history: list | None = None,
                     channel_map_history: list | None = None):
        if system_state is not None:
            self._system_state = system_state
        if channel_features is not None:
            self._channel_features = channel_features
        if snr_history is not None:
            self._snr_history = snr_history
        if bw_history is not None:
            self._bw_history = bw_history
        if power_history is not None:
            self._power_history = power_history
        if channel_map_history is not None:
            self._channel_map_history = channel_map_history

    def set_on_set_params(self, callback: Callable):
        self._on_set_params = callback

    # ── Handler methods ──────────────────────────────────────────

    def handle_get_system_state(self, _input: dict) -> str:
        if not self._system_state:
            return json.dumps({"error": "暂无系统状态数据，请等待遥测数据到达"}, ensure_ascii=False)
        return json.dumps(self._system_state, ensure_ascii=False, indent=2)

    def handle_get_channel_features(self, _input: dict) -> str:
        if not self._channel_features:
            return json.dumps({"error": "暂无信道特征数据"}, ensure_ascii=False)
        feats = []
        for i, f in enumerate(self._channel_features):
            if len(f) >= 4:
                feats.append({
                    "信道": i + 1,
                    "占用度": round(float(f[0]), 3),
                    "干扰水平": round(float(f[1]), 3),
                    "效用值": round(float(f[2]), 3),
                    "SNR指标": round(float(f[3]), 3),
                })
        return json.dumps(feats, ensure_ascii=False, indent=2)

    def handle_get_trend_history(self, inp: dict) -> str:
        n = min(inp.get("num_frames", 60), 120)
        snr_list = list(self._snr_history)[-n:] if self._snr_history else []
        bw_list = list(self._bw_history)[-n:] if self._bw_history else []
        pw_list = list(self._power_history)[-n:] if self._power_history else []
        if not snr_list:
            return json.dumps({"error": "暂无趋势数据"}, ensure_ascii=False)
        result = {
            "帧数": len(snr_list),
            "信噪比_dB": [round(v, 2) for v in snr_list],
            "带宽_MHz": [round(v, 2) for v in bw_list],
            "功率_dB": [round(v, 2) for v in pw_list],
            "信噪比统计": {
                "当前": round(snr_list[-1], 2) if snr_list else 0,
                "均值": round(sum(snr_list) / len(snr_list), 2),
                "最小": round(min(snr_list), 2),
                "最大": round(max(snr_list), 2),
                "趋势": "上升" if len(snr_list) >= 2 and snr_list[-1] > snr_list[0] * 1.05 else (
                    "下降" if len(snr_list) >= 2 and snr_list[-1] < snr_list[0] * 0.95 else "稳定"),
            },
            "功率统计": {
                "当前": round(pw_list[-1], 2) if pw_list else 0,
                "均值": round(sum(pw_list) / len(pw_list), 2),
            },
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    def handle_get_channel_map_history(self, inp: dict) -> str:
        n = min(inp.get("num_frames", 30), 120)
        history = list(self._channel_map_history)[-n:] if self._channel_map_history else []
        if not history:
            return json.dumps({"error": "暂无信道状态历史数据"}, ensure_ascii=False)
        mat = []
        for frame in history:
            if hasattr(frame, 'tolist'):
                mat.append(frame.tolist())
            else:
                mat.append(list(frame))
        summary = []
        for ch in range(len(mat[0]) if mat else 0):
            states = [int(row[ch]) for row in mat if ch < len(row)]
            state_names = {0: "空闲", 1: "用户占用", 2: "被干扰", 3: "重叠"}
            counts = {}
            for s in states:
                counts[state_names.get(s, str(s))] = counts.get(state_names.get(s, str(s)), 0) + 1
            dominant = max(counts, key=counts.get) if counts else "未知"
            summary.append({
                "信道": ch + 1,
                "主要状态": dominant,
                "状态分布": {k: f"{v}/{len(states)}" for k, v in counts.items()},
            })
        return json.dumps({
            "帧数": len(mat),
            "信道数": len(mat[0]) if mat else 0,
            "逐信道摘要": summary,
        }, ensure_ascii=False, indent=2)

    def handle_set_interference_params(self, inp: dict) -> str:
        if self._on_set_params is None:
            return json.dumps({"error": "控制通道未就绪，无法发送参数"}, ensure_ascii=False)
        params = {}
        for key in ["channel_idx", "power_db", "bw_mhz", "waveform_mode"]:
            if key in inp:
                val = inp[key]
                if key in ("channel_idx", "bw_mhz", "waveform_mode"):
                    params[key] = int(val)
                else:
                    params[key] = float(val)
        if not params:
            return json.dumps({"error": "未提供要修改的参数"}, ensure_ascii=False)
        error = self._validate_params(params)
        if error:
            return json.dumps({"error": error}, ensure_ascii=False)
        try:
            self._on_set_params(params)
            return json.dumps({"success": True, "已发送参数": params}, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"error": f"发送失败: {e}"}, ensure_ascii=False)

    @staticmethod
    def _validate_params(params: dict) -> str | None:
        if "channel_idx" in params and not 0 <= params["channel_idx"] <= 9:
            return "信道编号必须在 0-9 之间"
        if "power_db" in params and not 0 <= params["power_db"] <= 20:
            return "发射功率必须在 0-20 dB 之间"
        if "bw_mhz" in params and params["bw_mhz"] not in (2, 4, 6, 8):
            return "干扰带宽必须是 2/4/6/8 MHz"
        if "waveform_mode" in params and params["waveform_mode"] not in (0, 1):
            return "干扰模式必须是 0=宽带噪声 或 1=多音"
        return None

    def handle_analyze_situation(self, _input: dict) -> str:
        state = self._system_state or {}
        feats = self._channel_features or []
        snr_list = list(self._snr_history)[-60:] if self._snr_history else []
        bw_list = list(self._bw_history)[-60:] if self._bw_history else []
        pw_list = list(self._power_history)[-60:] if self._power_history else []
        chmap = list(self._channel_map_history)[-30:] if self._channel_map_history else []

        report = {}
        if state:
            report["系统状态"] = state

        if feats:
            ch_data = []
            for i, f in enumerate(feats):
                if len(f) >= 4:
                    ch_data.append({
                        "信道": i + 1,
                        "占用度": round(float(f[0]), 3),
                        "干扰水平": round(float(f[1]), 3),
                        "效用值": round(float(f[2]), 3),
                        "SNR指标": round(float(f[3]), 3),
                    })
            report["信道特征"] = ch_data

        if snr_list:
            snr_current = snr_list[-1]
            snr_mean = sum(snr_list) / len(snr_list)
            report["趋势分析"] = {
                "SNR当前": round(snr_current, 2),
                "SNR均值": round(snr_mean, 2),
                "SNR变化": "改善中" if len(snr_list) >= 5 and snr_list[-1] > snr_list[-5] * 1.03 else (
                    "恶化中" if len(snr_list) >= 5 and snr_list[-1] < snr_list[-5] * 0.97 else "基本稳定"),
                "功率当前": round(pw_list[-1], 2) if pw_list else 0,
                "带宽当前": round(bw_list[-1], 2) if bw_list else 0,
            }

        if chmap:
            last_frame = chmap[-1]
            if hasattr(last_frame, 'tolist'):
                last_frame = last_frame.tolist()
            jammed_channels = sum(1 for s in last_frame if s in (2, 3))
            user_channels = sum(1 for s in last_frame if s in (1, 3))
            report["干扰覆盖"] = {
                "被干扰信道数": jammed_channels,
                "用户占用信道数": user_channels,
                "总信道数": len(last_frame),
            }

        return json.dumps(report, ensure_ascii=False, indent=2)

    def handle_switch_channel(self, inp: dict) -> str:
        ch = int(inp.get("channel_idx", 0))
        if ch < 0 or ch > 9:
            return json.dumps({"error": "信道编号必须在 0-9 之间"}, ensure_ascii=False)
        if self._on_set_params:
            self._on_set_params({"channel_idx": ch})
            return json.dumps({
                "success": True,
                "已切换到信道": ch + 1,
            }, ensure_ascii=False, indent=2)
        return json.dumps({"error": "控制通道未就绪"}, ensure_ascii=False)

    def handle_query_system_info(self, _input: dict) -> str:
        state = self._system_state or {}
        feats = self._channel_features or []

        info = {
            "系统运行状态": "正常运行" if state else "等待遥测数据",
        }
        if state:
            info["当前配置"] = state

        if feats:
            occupied = sum(1 for f in feats if len(f) > 0 and float(f[0]) > 0.3)
            high_interf = sum(1 for f in feats if len(f) > 1 and float(f[1]) > 0.5)
            info["频谱概况"] = {
                "总信道数": len(feats),
                "被占用信道": occupied,
                "高干扰信道": high_interf,
                "可用信道": len(feats) - occupied - high_interf,
            }

        return json.dumps(info, ensure_ascii=False, indent=2)
