"""
Local command parser — intercepts common commands, executes directly.
Only falls back to LLM for genuinely complex requests.
"""
import json
import re
import traceback


MODE_NAMES = {
    0: "宽带噪声",
    1: "多音",
}


CONTROL_KEYS = ("channel_idx", "power_db", "bw_mhz", "waveform_mode")


class CommandParser:

    def __init__(self, tool_handlers):
        self.tools = tool_handlers

    def try_parse(self, text: str) -> str | None:
        text_stripped = self._normalize(text)
        print(f"[Parser] input: {text_stripped!r}")

        # ── Identity / project-positioning questions should stay local ──
        result = self._try_identity(text_stripped)
        if result is not None:
            print(f"[Parser] LOCAL identity -> {result[:60]!r}")
            return result

        # ── Emergency scenario presets ─────────────────────────
        result = self._try_scenario_preset(text_stripped)
        if result is not None:
            print(f"[Parser] LOCAL scenario -> {result[:60]!r}")
            return result

        # ── Control first (higher priority for parameter changes) ──
        result = self._try_control(text_stripped)
        if result is not None:
            print(f"[Parser] LOCAL control -> {result[:60]!r}")
            return result

        # ── Query commands ─────────────────────────────────────────
        result = self._try_query(text_stripped)
        if result is not None:
            print(f"[Parser] LOCAL query -> {result[:60]!r}")
            return result

        print("[Parser] no match -> LLM")
        return None

    def try_domain_qa(self, text: str) -> str | None:
        text_norm = self._normalize(text)
        return self._try_identity(text_norm) or self._try_domain_qa(text_norm)

    # ═══════════════════════════════════════════════════════════════
    # Identity / positioning
    # ═══════════════════════════════════════════════════════════════

    def _try_identity(self, text: str) -> str | None:
        t = text.lower()
        if any(kw in t for kw in [
            "你是什么模型", "你是哪个模型", "你用的什么模型", "你调用的什么模型",
            "你是不是deepseek", "deepseek", "大模型是什么", "底层模型", "模型身份",
            "你是谁", "你的定位", "系统定位", "项目中的定位", "本项目中的定位",
        ]):
            return self._format_identity()
        return None

    @staticmethod
    def _format_identity() -> str:
        return (
            "**智能体身份说明**\n\n"
            "我是本项目面向应急保障场景构建的频谱管控智能 Agent，不是通用聊天机器人。"
            "在系统设定中，我对应的是经过本地部署、蒸馏微调，并注入应急通信、频谱管控、"
            "干扰模拟和无人机图传保障等场景知识后的专用大模型智能体。\n\n"
            "我的任务是把操作者的自然语言意图转换为可执行的干扰模拟与频谱态势分析动作，"
            "支持切换目标信道、设置干扰功率、调整干扰带宽、切换宽带噪声/多音模式，"
            "并结合实时遥测数据给出应急频谱态势研判。比赛演示时，确定性控制命令优先由"
            "本地规则直接执行，以保证响应稳定；复杂问答由智能体能力辅助完成。"
        )

    def _try_scenario_preset(self, text: str) -> str | None:
        presets = [
            (
                ("基站退服", "基站损毁", "断站", "宽带抬升", "灾后宽带"),
                {"channel_idx": 5, "waveform_mode": 0, "power_db": 15.0, "bw_mhz": 20},
                "已加载“基站退服/宽带噪声抬升”场景",
            ),
            (
                ("救援拥塞", "多设备拥塞", "多队伍", "多音冲突", "频点冲突"),
                {"channel_idx": 5, "waveform_mode": 1, "power_db": 10.0, "bw_mhz": 4},
                "已加载“救援设备密集接入/多音频点冲突”场景",
            ),
            (
                ("链路压测", "视频回传压测", "高功率压制", "极端压测", "强干扰压测"),
                {"channel_idx": 7, "waveform_mode": 0, "power_db": 20.0, "bw_mhz": 8},
                "已加载“无人机视频回传链路压测”场景",
            ),
        ]
        for keywords, params, title in presets:
            if any(kw in text for kw in keywords):
                return self._apply_params(params, f"{title}: " + self._format_control_ack(params))
        return None

    # ═══════════════════════════════════════════════════════════════
    # Query
    # ═══════════════════════════════════════════════════════════════

    def _try_query(self, text: str) -> str | None:
        if any(kw in text for kw in [
            "频谱分析", "态势分析", "态势报告", "当前频谱", "频谱态势",
            "分析一下", "研判", "评估", "当前情况", "应急态势",
        ]):
            return self._format_situation_report(
                json.loads(self.tools.handle_analyze_situation({})))

        if any(kw in text for kw in [
            "查询状态", "系统状态", "当前配置", "运行状态", "查询系统", "系统信息",
            "当前参数", "配置是什么", "现在配置", "查看信息", "查询信息",
        ]):
            return self._format_system_info(
                json.loads(self.tools.handle_query_system_info({})))

        if any(kw in text for kw in ["信道状态", "信道分布", "信道摘要"]):
            return self._format_channel_status(
                json.loads(self.tools.handle_get_channel_map_history({"num_frames": 30})))

        if any(kw in text for kw in ["趋势评估", "趋势分析", "最近趋势", "运行趋势"]):
            return self._format_trend(
                json.loads(self.tools.handle_get_trend_history({"num_frames": 60})))

        return None

    def _try_domain_qa(self, text: str) -> str | None:
        """Answer common competition-demo questions locally to avoid API stalls."""
        if any(kw in text for kw in ["响应时间", "一直思考", "api链路", "api没有", "api不通", "不能回答", "没有响应"]):
            return (
                "**响应时间说明**\n\n"
                "如果问题没有命中本地规则，系统会尝试走 LLM/API 链路；一旦网络、API key、模型服务或 SDK 调用异常，"
                "界面就可能长时间停在“思考中”。\n\n"
                "为保证比赛演示稳定，Jammer 相关的控制、查询和项目说明已经改成本地优先："
                "切频、切模式、改功率、改带宽、查配置、态势研判、项目定位和常见概念都不依赖 API。"
            )

        if any(kw in text for kw in [
            "支持哪些功能", "可以做什么", "能做什么", "有什么功能", "项目主题",
            "面向应急保障", "智能体网络频谱",
        ]):
            return (
                "**我在本项目中的定位**\n\n"
                "我是面向应急保障场景的 Jammer 智能控制 Agent，定位是把自然语言指令转成可执行的干扰参数，"
                "帮助系统快速构造极端频谱干扰场景，并对当前频谱状态做简要研判。\n\n"
                "我主要支持四类能力：\n"
                "1. 参数控制：切换目标信道、设置干扰带宽、调整发射功率、切换宽带噪声/多音干扰。\n"
                "2. 组合下发：例如“切频到信道7，多音，功率15dB，带宽6MHz”。\n"
                "3. 状态查询：查看当前配置、信道状态、SNR趋势和干扰覆盖情况。\n"
                "4. 场景解释：解释信道干扰、带宽含义、干扰模式区别，以及为什么某些可视化会变化。\n\n"
                "在比赛演示中，我承担的是“Jammer 子系统的人机交互与参数编排入口”：让操作者不用手动改配置，"
                "直接用自然语言驱动干扰模拟器。"
            )

        if any(kw in text for kw in ["带宽可以", "带宽能", "支持哪些带宽", "可设置带宽", "干扰带宽有哪些"]):
            return (
                "**干扰带宽设置**\n\n"
                "当前系统支持 2/4/6/8/20 MHz 五档干扰带宽。后端配置中总观测带宽为 20 MHz，"
                "划分为 10 个信道，因此默认单个信道宽度是 2 MHz。\n\n"
                "在演示中可直接说：带宽2MHz、带宽4MHz、带宽6MHz、带宽8MHz、带宽20MHz，"
                "也可以组合为：切频到信道7，多音，功率15dB，带宽6MHz。"
            )

        if any(kw in text for kw in ["默认信道带宽", "一个信道", "单个信道", "信道带宽"]):
            return (
                "**信道带宽说明**\n\n"
                "本系统默认把 3 GHz 附近 20 MHz 观测频段划成 10 个信道，"
                "所以每个信道宽度为 2 MHz。信道编号 1-10 对应这 10 个离散频段。"
            )

        if any(kw in text for kw in ["宽度没有变化", "黑色矩形", "矩形宽度", "为什么带宽", "看不出带宽"]):
            return (
                "**带宽显示说明**\n\n"
                "10信道状态图是离散信道状态图，不是连续频谱瀑布图。此前后端只把目标信道标记为"
                "被干扰/重叠，所以无论 2/4/6/8/20 MHz，黑色矩形都会按覆盖范围显示。\n\n"
                "现在黑色干扰覆盖区会以目标信道中心为中心，按设置带宽向左右均匀展开。"
                "如果 MATLAB 后端已经在运行，需要重启后端脚本才能看到新的覆盖宽度。"
            )

        if any(kw in text for kw in ["什么是信道干扰", "信道干扰是什么", "什么叫信道干扰"]):
            return (
                "**信道干扰概念**\n\n"
                "信道干扰是指在目标通信信道附近注入噪声、多音或其他占用信号，"
                "抬高接收端噪声底或制造频点冲突，使有效通信的 SNR 下降、误码率升高，"
                "从而降低链路可靠性。本系统用它来模拟应急保障中的极端频谱压制场景。"
            )

        if any(kw in text for kw in ["宽带噪声", "多音", "干扰模式", "模式区别", "有什么区别"]):
            return (
                "**干扰模式区别**\n\n"
                "宽带噪声：在设定带宽内铺开随机噪声，适合模拟区域压制和噪声底抬升。\n"
                "多音干扰：在设定带宽内生成多个离散音调，适合模拟精准频点打击或多载波干扰。\n\n"
                "可以直接说：切换宽带压制、切换多音干扰。"
            )

        if any(kw in text for kw in ["什么是", "为什么", "介绍", "说明", "解释一下", "区别"]):
            return (
                "**Jammer Agent 本地回答**\n\n"
                "我可以回答本项目中 jammer 子系统相关问题，也可以直接执行控制指令。"
                "常用问题包括：我在项目中的定位、支持哪些功能、干扰带宽可以设置哪些、"
                "默认信道带宽是多少、什么是信道干扰、宽带噪声和多音有什么区别。"
            )

        return None

    # ═══════════════════════════════════════════════════════════════
    # Control
    # ═══════════════════════════════════════════════════════════════

    def _try_control(self, text: str) -> str | None:
        params = {}

        ch = self._parse_channel(text)
        if ch is not None:
            params["channel_idx"] = ch

        bw = self._parse_bandwidth(text)
        if bw is not None:
            params["bw_mhz"] = bw

        mode = self._parse_mode(text)
        if mode is not None:
            params["waveform_mode"] = mode

        pwr = self._parse_power(text)
        if pwr is not None:
            params["power_db"] = pwr

        if params:
            return self._apply_params(params, self._format_control_ack(params))
        return None

    def _apply_params(self, params: dict, msg: str) -> str:
        cb = self.tools._on_set_params
        if cb:
            try:
                cb(params)
                print(f"[Parser] command sent: {params}")
                return msg
            except Exception as e:
                traceback.print_exc()
                return f"发送命令失败: {e}"
        return "控制通道未就绪"

    def _format_control_ack(self, params: dict) -> str:
        parts = []
        if "channel_idx" in params:
            parts.append(f"目标信道=信道 {params['channel_idx'] + 1}")
        if "waveform_mode" in params:
            parts.append(f"干扰模式={MODE_NAMES.get(params['waveform_mode'], params['waveform_mode'])}")
        if "power_db" in params:
            parts.append(f"发射功率={params['power_db']:.1f} dB")
        if "bw_mhz" in params:
            parts.append(f"干扰带宽={params['bw_mhz']} MHz")
        return "已下发干扰控制指令: " + "，".join(parts)

    # ═══════════════════════════════════════════════════════════════
    # Matchers
    # ═══════════════════════════════════════════════════════════════

    def _parse_channel(self, text: str) -> int | None:
        # "切换到信道3", "3信道", "切频到3", "ch 3", "目标频点=3"
        for pat in [
            r'(?:信道|通道|频道|channel|ch)\s*[:：#]?\s*(\d+)',
            r'(\d+)\s*号?\s*(?:信道|通道|频道)',
            r'(?:切频|跳频|转频|换频|切到|切换到|切换至|目标频点|目标信道)\s*(?:信道|通道|频道)?\s*[:：]?\s*(\d+)',
        ]:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                ch = self._to_channel_idx(int(m.group(1)))
                if ch is not None:
                    return ch
        return None

    def _parse_bandwidth(self, text: str) -> int | None:
        # "带宽6", "带宽8MHz", "bw 4", "覆盖带宽 4 MHz"
        m = re.search(r'(?:带宽|频宽|覆盖带宽|干扰带宽|bw)\s*[:：]?\s*(\d+)', text, re.IGNORECASE)
        if m:
            bw = int(m.group(1))
            if bw in (2, 4, 6, 8, 20):
                return bw
        return None

    def _parse_mode(self, text: str) -> int | None:
        t = text.lower()
        if any(kw in t for kw in [
            "宽带噪声", "噪声模式", "噪声干扰", "噪声", "宽带",
            "noise", "wideband", "宽带压制", "压制干扰", "扫频压制",
        ]):
            return 0
        if any(kw in t for kw in [
            "多音", "多载波", "点频", "窄带", "tone", "multi",
            "multitone", "多音干扰", "多音模式", "精准干扰",
        ]):
            return 1
        if any(kw in t for kw in ["切换干扰", "切换模式", "换一种干扰", "更换干扰"]):
            return 1 - self._get_current_mode()
        return None

    def _parse_power(self, text: str) -> float | None:
        # Relative up: "功率+5", "功率增加5", "功率增大5dB"
        m = re.search(r'(?:功率|power)\s*[+＋]\s*(\d+(?:\.\d+)?)', text, re.IGNORECASE)
        if m:
            return min(20, self._get_current_power() + float(m.group(1)))

        m = re.search(r'(?:功率|power)\s*(?:增加|增大|提高|上调)\s*(\d+(?:\.\d+)?)', text, re.IGNORECASE)
        if m:
            return min(20, self._get_current_power() + float(m.group(1)))

        # Relative down: "功率-5", "功率降低5", "功率减小5dB"
        m = re.search(r'(?:功率|power)\s*[-−]\s*(\d+(?:\.\d+)?)', text, re.IGNORECASE)
        if m:
            return max(0, self._get_current_power() - float(m.group(1)))

        m = re.search(r'(?:功率|power)\s*(?:降低|减小|下降|下调)\s*(\d+(?:\.\d+)?)', text, re.IGNORECASE)
        if m:
            return max(0, self._get_current_power() - float(m.group(1)))

        if any(kw in text for kw in ["最大功率", "满功率", "压满", "强干扰"]):
            return 20.0
        if any(kw in text for kw in ["最小功率", "低功率", "静默功率", "弱干扰"]):
            return 0.0

        # "功率15dB", "发射功率=15", "pwr 12.5", "power 10"
        m = re.search(r'(?:功率|发射功率|输出功率|power|pwr)\s*[:：=]?\s*(\d+(?:\.\d+)?)\s*(?:db|dB)?', text, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            if 0 <= val <= 20:
                return val

        return None

    def _get_current_power(self) -> float:
        state = self.tools._system_state
        pwr_str = state.get("发射功率", "0 dB")
        try:
            return float(pwr_str.replace("dB", "").strip())
        except (ValueError, AttributeError):
            return 0.0

    def _get_current_mode(self) -> int:
        mode = str(self.tools._system_state.get("干扰模式", "宽带噪声"))
        return 1 if "多音" in mode else 0

    @staticmethod
    def _normalize(text: str) -> str:
        table = str.maketrans({
            "，": ",", "。": ".", "；": ";", "：": ":", "＋": "+",
            "－": "-", "—": "-", "　": " ",
        })
        return text.strip().translate(table)

    @staticmethod
    def _to_channel_idx(value: int) -> int | None:
        if 1 <= value <= 10:
            return value - 1
        if 0 <= value <= 9:
            return value
        return None

    # ═══════════════════════════════════════════════════════════════
    # Formatters
    # ═══════════════════════════════════════════════════════════════

    def _format_situation_report(self, data: dict) -> str:
        lines = ["**态势研判**\n"]
        feats = data.get("信道特征", [])
        trend = data.get("趋势分析", {})
        coverage = data.get("干扰覆盖", {})

        if trend:
            lines.append(
                f"态势: SNR {trend.get('SNR当前','?')} dB，{trend.get('SNR变化','未知')}；"
                f"当前功率 {trend.get('功率当前','?')} dB，估计带宽 {trend.get('带宽当前','?')} MHz。"
            )
        if coverage:
            lines.append(
                f"覆盖: 被干扰信道 {coverage.get('被干扰信道数','?')}/{coverage.get('总信道数','?')}，"
                f"用户占用信道 {coverage.get('用户占用信道数','?')}。"
            )
        if feats:
            ranked = sorted(feats, key=lambda ch: (ch.get("占用度", 0), ch.get("干扰水平", 0)), reverse=True)
            lines.append("\n重点信道:")
            for ch in ranked[:3]:
                lines.append(
                    f"  信道{ch.get('信道','?')}: 占用{ch.get('占用度',0):.2f} "
                    f"干扰{ch.get('干扰水平',0):.2f} SNR指标{ch.get('SNR指标',0):.2f}"
                )
        if not trend and not coverage and not feats:
            lines.append("暂无足够遥测数据，等待 MATLAB 后端上报频谱与信道特征。")
        lines.append("\n建议: 可用“切频到信道N，多音/宽带，功率XdB，带宽YMHz”快速构造极端干扰场景。")
        return "\n".join(lines)

    def _format_system_info(self, data: dict) -> str:
        lines = ["**系统信息 / 当前配置**\n"]
        state = data.get("系统运行状态")
        if state:
            lines.append(f"运行状态: {state}")
        for k, v in data.get("当前配置", {}).items():
            lines.append(f"  {k}: {v}")
        overview = data.get("频谱概况", {})
        if overview:
            lines.append("\n频谱概况:")
            for k, v in overview.items():
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    def _format_channel_status(self, data: dict) -> str:
        lines = [f"**信道状态 (近{data.get('帧数','?')}帧)**\n"]
        for ch in data.get("逐信道摘要", []):
            lines.append(f"  信道{ch['信道']}: {ch['主要状态']}")
        return "\n".join(lines)

    def _format_trend(self, data: dict) -> str:
        lines = ["**趋势分析**\n"]
        snr = data.get("信噪比统计", {})
        if snr:
            lines.append(f"信噪比: 当前{snr.get('当前','?')}dB 均值{snr.get('均值','?')}dB 趋势{snr.get('趋势','?')}")
        pwr = data.get("功率统计", {})
        if pwr:
            lines.append(f"功率: 当前{pwr.get('当前','?')}dB 均值{pwr.get('均值','?')}dB")
        return "\n".join(lines)
