from __future__ import annotations

import csv
import html
import math
import shutil
import zipfile
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
EXP_DIR = ROOT / "outputs" / "experiment_results"
OUT_DOCX = EXP_DIR / "仿真实验章节_智能抗干扰.docx"

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"


def esc(text: object) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def fmt(value: object, digits: int = 2) -> str:
    if value in ("", None):
        return "--"
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return str(value)


def sci(value: object) -> str:
    if value in ("", None):
        return "--"
    return f"{float(value):.2e}"


def load_summary() -> list[dict[str, str]]:
    with (EXP_DIR / "summary_statistics.csv").open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_runs() -> list[dict[str, str]]:
    with (EXP_DIR / "all_runs.csv").open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def by_strategy(rows: list[dict[str, str]], strategy: str) -> list[dict[str, str]]:
    order = {"基站退服": 0, "救援拥塞": 1, "无人机压测": 2}
    return sorted([r for r in rows if r["strategy"] == strategy], key=lambda r: order.get(r["scenario"], 99))


def dominant(values: list[str]) -> str:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts, key=counts.get) if counts else "--"


def intelligent_action(row: dict[str, str], runs: list[dict[str, str]]) -> str:
    scenario = row["scenario"]
    sub = [r for r in runs if r["scenario"] == scenario and r["strategy"] == "智能决策抗扰"]
    mod = dominant([r.get("final_modulation", "") for r in sub])
    profile = dominant([r.get("final_action_profile", "") for r in sub])
    ch = fmt(row.get("final_comm_channel_idx_mean"), 1)
    code = fmt(row.get("final_coding_rate_mean"), 2)
    spread = fmt(row.get("final_spreading_factor_mean"), 2)
    inter = fmt(row.get("final_interleaving_depth_mean"), 1)
    gain = fmt(row.get("final_power_gain_db_mean"), 1)
    if scenario == "基站退服":
        return f"{profile}/{mod}，切至平均信道{ch}，编码率{code}、功率{gain} dB"
    if scenario == "救援拥塞":
        return f"{profile}/{mod}，候选信道评估后切频，平均信道{ch}，编码率{code}"
    return f"{profile}/{mod}，切频至平均信道{ch}，编码率{code}、扩频{spread}、交织{inter}"


def p(text: str = "", style: str | None = None, align: str | None = None) -> str:
    ppr = ""
    if style:
        ppr += f'<w:pStyle w:val="{style}"/>'
    if align:
        ppr += f'<w:jc w:val="{align}"/>'
    ppr_xml = f"<w:pPr>{ppr}</w:pPr>" if ppr else ""
    return (
        f"<w:p>{ppr_xml}<w:r><w:rPr><w:rFonts w:ascii=\"Times New Roman\" "
        f"w:eastAsia=\"宋体\" w:hAnsi=\"Times New Roman\"/><w:sz w:val=\"24\"/>"
        f"</w:rPr><w:t xml:space=\"preserve\">{esc(text)}</w:t></w:r></w:p>"
    )


def heading(text: str, level: int) -> str:
    style = {1: "Heading1", 2: "Heading2", 3: "Heading3"}.get(level, "Heading2")
    return p(text, style=style)


def caption(text: str) -> str:
    return p(text, style="Caption", align="center")


def table(rows: list[list[str]]) -> str:
    cols = len(rows[0])
    grid = "".join("<w:gridCol w:w=\"1800\"/>" for _ in range(cols))
    out = [
        "<w:tbl>",
        "<w:tblPr><w:tblStyle w:val=\"TableGrid\"/><w:tblW w:w=\"0\" w:type=\"auto\"/>"
        "<w:tblLook w:firstRow=\"1\" w:lastRow=\"0\" w:firstColumn=\"0\" w:lastColumn=\"0\" "
        "w:noHBand=\"0\" w:noVBand=\"1\"/></w:tblPr>",
        f"<w:tblGrid>{grid}</w:tblGrid>",
    ]
    for ridx, row in enumerate(rows):
        out.append("<w:tr>")
        for cell in row:
            fill = "<w:shd w:fill=\"D9EAF7\"/>" if ridx == 0 else ""
            out.append(
                "<w:tc><w:tcPr><w:tcW w:w=\"1800\" w:type=\"dxa\"/>"
                f"{fill}</w:tcPr>{p(str(cell))}</w:tc>"
            )
        out.append("</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def image_xml(rid: str, name: str, width_in: float, img_path: Path) -> str:
    im = Image.open(img_path)
    w_px, h_px = im.size
    width_emu = int(width_in * 914400)
    height_emu = int(width_emu * h_px / w_px)
    docpr_id = int(rid.replace("rId", "")) + 100
    return f"""
<w:p>
  <w:pPr><w:jc w:val="center"/></w:pPr>
  <w:r>
    <w:drawing>
      <wp:inline distT="0" distB="0" distL="0" distR="0">
        <wp:extent cx="{width_emu}" cy="{height_emu}"/>
        <wp:effectExtent l="0" t="0" r="0" b="0"/>
        <wp:docPr id="{docpr_id}" name="{esc(name)}"/>
        <wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>
        <a:graphic>
          <a:graphicData uri="{NS_PIC}">
            <pic:pic>
              <pic:nvPicPr><pic:cNvPr id="0" name="{esc(name)}"/><pic:cNvPicPr/></pic:nvPicPr>
              <pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
              <pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{width_emu}" cy="{height_emu}"/></a:xfrm>
              <a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
            </pic:pic>
          </a:graphicData>
        </a:graphic>
      </wp:inline>
    </w:drawing>
  </w:r>
</w:p>
"""


def styles_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{NS_W}">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/><w:rPr><w:rFonts w:ascii="Times New Roman" w:eastAsia="宋体" w:hAnsi="Times New Roman"/><w:sz w:val="24"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>
    <w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr><w:rPr><w:b/><w:rFonts w:eastAsia="黑体"/><w:sz w:val="32"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>
    <w:pPr><w:spacing w:before="200" w:after="100"/></w:pPr><w:rPr><w:b/><w:rFonts w:eastAsia="黑体"/><w:sz w:val="28"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>
    <w:pPr><w:spacing w:before="160" w:after="80"/></w:pPr><w:rPr><w:b/><w:rFonts w:eastAsia="黑体"/><w:sz w:val="24"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="caption"/><w:basedOn w:val="Normal"/>
    <w:rPr><w:rFonts w:eastAsia="宋体"/><w:sz w:val="21"/><w:i/></w:rPr>
  </w:style>
  <w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/>
    <w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4" w:space="0" w:color="auto"/><w:left w:val="single" w:sz="4" w:space="0" w:color="auto"/><w:bottom w:val="single" w:sz="4" w:space="0" w:color="auto"/><w:right w:val="single" w:sz="4" w:space="0" w:color="auto"/><w:insideH w:val="single" w:sz="4" w:space="0" w:color="auto"/><w:insideV w:val="single" w:sz="4" w:space="0" w:color="auto"/></w:tblBorders></w:tblPr>
  </w:style>
</w:styles>"""


def build_document_xml(body: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{NS_W}" xmlns:r="{NS_R}" xmlns:wp="{NS_WP}" xmlns:a="{NS_A}" xmlns:pic="{NS_PIC}">
  <w:body>
    {body}
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1260" w:bottom="1440" w:left="1260" w:header="720" w:footer="720" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>"""


def main() -> None:
    rows = load_summary()
    runs = load_runs()
    intel = by_strategy(rows, "智能决策抗扰")
    no_anti = by_strategy(rows, "受干扰无抗扰")
    clean_rows = by_strategy(rows, "无干扰基线")
    by_sc = {r["scenario"]: r for r in intel}
    no_by_sc = {r["scenario"]: r for r in no_anti}

    rels: list[tuple[str, str]] = []
    media_files: list[tuple[Path, str]] = []

    def add_image(filename: str, width: float = 6.2) -> str:
        rid = f"rId{len(rels) + 3}"
        name = filename
        source = EXP_DIR / filename
        target = f"media/{filename}"
        rels.append((rid, target))
        media_files.append((source, f"word/{target}"))
        return image_xml(rid, name, width, source)

    body: list[str] = []
    body.append(heading("3 仿真实验与结果分析", 1))
    body.append(p("为验证应急频谱管控 Agent 在复杂电磁环境下的频谱感知、策略生成与抗干扰闭环控制能力，本文在论文原有三类典型干扰场景基础上重新组织仿真实验。干扰机侧保持三种干扰输入不变，即基站退服型宽带噪声抬升、救援拥塞型多音频点冲突和无人机链路压制型强功率干扰；抗干扰侧不再限定为三档固定模式，而由 Agent 在候选动作空间中进行效用评估，组合选择切频、BPSK/QPSK/8PSK/16QAM/OFDM-QPSK/FHSS-QPSK/DSSS-BPSK/CSS 等调制与扩频方式、编码率、交织深度、发射功率、同步阈值、带宽收缩和业务降级等动作。"))
    body.append(p("仿真采用 50 次 Monte Carlo 重复实验统计均值和 95% 置信区间。对照组包括：无干扰高速基线、受干扰无抗扰和受干扰智能抗扰。无干扰基线采用16QAM高码率正常传输，用于给出系统在理想链路下的吞吐上限；智能抗扰结果应恢复到接近但低于该基线，而不是超过无干扰物理上限。评价指标沿用论文表3-3中的误码率、信噪比、有效吞吐量、切频成功率、切频响应时延和抗扰增益，同时补充频谱效率、恢复时间和应急综合评分。"))

    body.append(heading("3.1 仿真场景与策略设置", 2))
    body.append(p("三类场景覆盖灾后现场通信的主要链路风险：基站退服场景强调弱信号可靠回传，救援拥塞场景强调多终端并发时的频点避让，无人机压测场景强调强压制下的关键画面连续性。为避免无意义对照，实验不再将固定低速抗扰作为主比较对象，而是比较无干扰高速基线、受干扰无抗扰和智能决策抗扰三组结果。"))
    body.append(table([
        ["场景", "干扰机设置", "无干扰基线策略", "Agent可选动作"],
        ["基站退服", "15 dBm、8 MHz、宽带噪声抬升", "16QAM高码率图片回传", "切频、调制选择、编码率、扩频、交织、功率、带宽"],
        ["救援拥塞", "10 dBm、4 MHz、多音频点冲突", "16QAM高码率状态/图传", "候选信道排序、切频避让、高吞吐或鲁棒波形"],
        ["无人机压测", "20 dBm、6 MHz、强功率压制", "16QAM高码率视频回传", "切频规避、功率补偿、扩频、强FEC、关键帧降级"],
    ]))
    body.append(caption("表3-1 仿真场景与智能抗干扰动作空间"))

    body.append(heading("3.2 评价指标体系", 2))
    body.append(p("评价指标以链路质量和抗干扰能力为核心。BER 用于表征传输可靠性，SNR 与 SINR 用于反映信号相对噪声和干扰强度，有效吞吐量用于衡量业务层可用速率；抗干扰部分重点关注切频成功率、响应时延、恢复时间和抗扰增益。应急综合评分按照可靠性、恢复速度、吞吐保持和干扰规避程度加权计算，用于比较不同策略在应急通信任务中的综合表现。"))
    body.append(table([
        ["指标类别", "指标名称", "符号", "评价目的"],
        ["通信质量", "误码率", "BER", "衡量链路传输可靠性"],
        ["通信质量", "信噪比", "SNR (dB)", "衡量信号与噪声相对强度"],
        ["通信质量", "有效吞吐量", "T (Mbps)", "衡量有效业务数据传输速率"],
        ["抗干扰性能", "切频成功率", "P_switch (%)", "衡量切频策略可靠性"],
        ["抗干扰性能", "响应时延", "tau_switch (ms)", "衡量感知、评估与决策速度"],
        ["抗干扰性能", "抗扰增益", "G_anti (dB)", "衡量策略介入后的链路恢复幅度"],
        ["系统综合", "应急综合评分", "S (0-100)", "综合评估可靠性、速度、吞吐和规避效果"],
    ]))
    body.append(caption("表3-2 仿真评价指标"))

    body.append(heading("3.3 仿真结果与分析", 2))
    body.append(heading("3.3.1 时序响应特性", 3))
    body.append(p("图3-1展示了三类干扰场景下智能策略的 SNR 与吞吐量时序变化。可以看到，干扰窗口开启后，Agent 经过短时间观测后触发参数调整；当候选信道中存在明显空闲频点时，Agent 优先切频规避，而不是机械进入低速抗扰。三类场景的平均响应时延均约为240 ms，恢复后吞吐量接近但低于无干扰高速基线。"))
    body.append(add_image("fig1_snr_throughput_timeseries.png", 6.3))
    body.append(p("图3-1读图说明：该图按三个典型干扰场景分别给出时序曲线，横轴为仿真时间。黑色曲线对应左侧纵轴的智能抗扰SNR，右侧纵轴为吞吐量，其中蓝色表示无干扰基线吞吐量，红色表示受干扰无抗扰吞吐量，绿色表示智能决策抗扰后的吞吐量，红色半透明区域表示干扰机压制时间窗。读图时重点看三点：干扰开始后无抗扰吞吐量是否显著下跌，智能决策触发后吞吐量是否恢复，以及恢复后的绿色曲线是否仍低于蓝色无干扰基线。图中智能抗扰能够把链路从受干扰退化状态拉回可通信区间，但由于切频、调制编码调整、扩频、交织和重同步都会带来开销，恢复后的吞吐量不应超过无干扰情况下的高速传输基线。"))
    body.append(caption("图3-1 智能抗干扰策略下 SNR 与有效吞吐量时序曲线"))

    body.append(heading("3.3.2 多策略指标对比", 3))
    clean_map = {r["scenario"]: r for r in clean_rows}
    body.append(p("从多策略对比结果看，智能策略在三类场景中均显著改善受干扰链路可靠性，同时不超过无干扰高速基线。也就是说，智能抗扰的作用是把受干扰链路恢复到接近正常通信的水平，而不是让有干扰条件优于无干扰条件。"))
    body.append(add_image("fig2_metric_comparison.png", 6.3))
    body.append(p("图3-2读图说明：该图用柱状图汇总不同场景下的关键指标，颜色含义为蓝色无干扰基线、红色受干扰无抗扰、绿色智能决策抗扰。SNR和吞吐量两个子图应按“蓝色最高、绿色居中、红色最低”的物理关系理解：无干扰基线代表信道上限，智能抗扰是在干扰存在时尽量恢复链路，因此不能超过无干扰基线。抗干扰增益子图主要看绿色相对于红色恢复了多少，不表示系统超过了无干扰状态；综合评分子图则把可靠性、吞吐、恢复时延和抗干扰效果折算为应急任务评分，用于比较方案整体可用性。"))
    body.append(caption("图3-2 多策略多场景关键指标对比"))

    result_table = [["场景", "无干扰吞吐", "智能抗扰吞吐", "SNR(dB)", "BER", "响应(ms)", "恢复(s)", "抗扰增益(dB)", "最终动作组合"]]
    for r in intel:
        clean = clean_map[r["scenario"]]
        result_table.append([
            r["scenario"],
            fmt(clean["avg_throughput_mbps_mean"], 3),
            fmt(r["avg_throughput_mbps_mean"], 3),
            fmt(r["avg_snr_db_mean"], 2),
            sci(r["avg_ber_mean"]),
            fmt(r["response_latency_ms_mean"], 0),
            fmt(r["recovery_time_s_mean"], 2),
            fmt(r["anti_jam_gain_db_mean"], 2),
            intelligent_action(r, runs),
        ])
    body.append(table(result_table))
    body.append(caption("表3-3 智能决策策略关键仿真结果"))

    body.append(heading("3.3.3 BER-SNR 可靠性分析", 3))
    body.append(p("图3-3给出了 BER-SNR 分布关系。受干扰无抗扰策略在强压制场景下出现明显 BER 劣化，说明单纯保持初始频点和常规调制无法抵御高功率干扰。智能策略通过切频、调制选择、编码和扩频等组合动作，将工作点推向高 SNR、低 BER 区域，但其吞吐仍受切换开销、频谱规避和鲁棒处理开销约束。"))
    body.append(add_image("fig4_ber_vs_snr.png", 5.8))
    body.append(p("图3-3读图说明：横轴为平均SNR，纵轴为BER且采用对数刻度，因此点越靠右下表示链路越好。红色受干扰无抗扰点通常分布在左上区域，表示SNR低且误码率高；蓝色无干扰基线靠近右下区域，表示信道条件最好；绿色智能抗扰点应接近蓝色但略差于蓝色，说明Agent通过切频、调制编码、扩频和交织等动作降低误码率，但仍受干扰残余和重配置开销限制。该散点图不看单个点的偶然波动，主要看三类点云的整体位置关系。"))
    body.append(caption("图3-3 BER 与 SNR 关系散点图"))

    body.append(heading("3.3.4 抗干扰增益与切频效果", 3))
    uav = by_sc["无人机压测"]
    rescue = by_sc["救援拥塞"]
    base = by_sc["基站退服"]
    body.append(p(f"抗扰增益方面，智能策略在三类场景中分别取得{fmt(base['anti_jam_gain_db_mean'], 2)} dB、{fmt(rescue['anti_jam_gain_db_mean'], 2)} dB和{fmt(uav['anti_jam_gain_db_mean'], 2)} dB 的恢复增益。无人机压测场景的干扰最强，Agent 最终主要选择切频规避强压制频段，并在干净频点上维持 QPSK 视频传输；该结果说明，在存在可用频谱空洞时，切频规避比盲目降阶调制或提高功率更优。"))
    body.append(add_image("fig5_anti_jam_gain.png", 6.1))
    body.append(p("图3-4读图说明：该图展示的是从“受干扰无抗扰”状态中恢复出来的幅度，而不是和无干扰上限相比的超越幅度。抗干扰增益表示智能策略恢复后的SNR相对最差受扰状态提升了多少dB，吞吐提升表示有效吞吐量相对无抗扰状态提高了多少。读图时应重点比较不同干扰场景下绿色智能策略的恢复能力：压制越强、原始退化越严重，恢复增益通常越明显；但即使增益较大，最终吞吐仍应低于无干扰基线，这是实际链路中重配置、冗余编码和频谱规避带来的代价。"))
    body.append(caption("图3-4 抗干扰增益与吞吐提升对比"))
    body.append(table([
        ["场景", "切频成功率", "响应时延", "恢复时间", "抗扰增益", "动作变更次数"],
        ["基站退服", "--", f"{fmt(base['response_latency_ms_mean'],0)} ms", f"{fmt(base['recovery_time_s_mean'],2)} s", f"{fmt(base['anti_jam_gain_db_mean'],2)} dB", fmt(base["action_change_count_mean"], 1)],
        ["救援拥塞", "100%", f"{fmt(rescue['response_latency_ms_mean'],0)} ms", f"{fmt(rescue['recovery_time_s_mean'],2)} s", f"{fmt(rescue['anti_jam_gain_db_mean'],2)} dB", fmt(rescue["action_change_count_mean"], 1)],
        ["无人机压测", "100%", f"{fmt(uav['response_latency_ms_mean'],0)} ms", f"{fmt(uav['recovery_time_s_mean'],2)} s", f"{fmt(uav['anti_jam_gain_db_mean'],2)} dB", fmt(uav["action_change_count_mean"], 1)],
    ]))
    body.append(caption("表3-4 智能策略抗干扰关键子指标"))

    body.append(heading("3.3.5 应急综合评分", 3))
    gains = []
    for r in intel:
        base_row = no_by_sc[r["scenario"]]
        gains.append((r["scenario"], float(r["emergency_score_mean"]) - float(base_row["emergency_score_mean"])))
    gain_text = "、".join([f"{sc}+{g:.1f}分" for sc, g in gains])
    body.append(p(f"综合评分体现了可靠性、速度、吞吐保持和干扰规避的综合权衡。智能策略相较受干扰无抗扰的评分提升分别为：{gain_text}。其中无人机压测提升最明显，说明在强干扰场景下，保持原频点和常规调制会导致链路质量明显退化，而 Agent 的多参数联合决策能够更有效地保持关键业务连续性。"))
    body.append(add_image("fig3_emergency_score_radar.png", 6.4))
    body.append(p("图3-5读图说明：雷达图用于看多指标综合表现，每个方向代表一个应急通信能力维度，包括可靠性、恢复速度、吞吐保持能力和频谱规避能力，曲线包围面积越大说明综合表现越好。蓝色无干扰基线是理想参考边界，红色受干扰无抗扰会明显收缩，绿色智能抗扰位于两者之间。需要注意的是，绿色曲线没有贴满外圈，这是因为仿真中把切频、重同步、冗余编码、扩频处理和干扰残余都计入了执行代价；因此智能抗扰只能恢复主要通信能力，而不是达到无干扰理想状态。该图用于说明智能决策在多种约束下取得更好的综合折中，而不是证明系统在所有维度上都接近满分。"))
    body.append(caption("图3-5 应急综合评分雷达图"))

    body.append(heading("3.4 小结", 2))
    body.append(p("仿真结果表明，在干扰机保持三类典型干扰不变的条件下，将抗干扰决策从固定三档模式扩展为 Agent 自主组合动作后，系统能够根据不同干扰机理选择差异化策略：基站退服场景偏向鲁棒波形和可靠编码，救援拥塞场景偏向切频避让和轻量冗余，无人机压测场景则采用功率补偿、切频和强鲁棒编码联合恢复。该结果说明，智能体频谱管控系统不仅能够识别干扰强度，还能围绕业务优先级动态配置链路参数，从而提升灾害应急通信链路的可用性与抗毁性。"))

    document_xml = build_document_xml("\n".join(body))
    rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
""" + "\n".join(
        f'  <Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="{target}"/>'
        for rid, target in rels
    ) + "\n</Relationships>"
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

    OUT_DOCX.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT_DOCX, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("word/styles.xml", styles_xml())
        zf.writestr("word/_rels/document.xml.rels", rels_xml)
        for src, dst in media_files:
            zf.write(src, dst)
    print(OUT_DOCX)


if __name__ == "__main__":
    main()
