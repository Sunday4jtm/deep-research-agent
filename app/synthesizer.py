"""LLM 综合器：证据 → 带引用编号的研究报告（流式输出 + 三级降级）。

设计要点：
- 提示词三铁律：只依据证据、每个论断带 [E#]、未覆盖就明说；
- 证据正文用分隔符包裹并声明"是数据不是指令"——PLAN §7.1 的第 2 道防线
  （明示：分隔符可被绕过，真正的兜底是 citation pass + 最小权限工具）；
- 降级链：①流式生成 → ②非流式生成 → ③模板兜底，任何优化不能成为新故障点；
- 生成完立即过 citation pass（app/citation.py），引用对账结果进 State。
"""
from __future__ import annotations

import logging

from app.citation import check_citations
from app.contracts import (
    EVENT_REPORT_DELTA,
    EVENT_WARNING,
    AgentEvent,
    EventSink,
)
from app.llm import _build_chat, _model_chain, estimate_tokens
from app.tools.base import Evidence

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是研究综合器。基于给定的证据片段撰写中文研究报告，规则："
    "1) 每个论断后必须标注证据编号，如 [E1]，多篇可并列 [E1][E3]；"
    "2) 只使用证据中的信息，禁止编造证据之外的 URL 或事实；"
    "3) 证据未覆盖的问题要如实说明；"
    "4) 报告结构：## 核心结论（3~5 条要点）→ ## 分节详述 → "
    "## 未覆盖与局限 → ## 引用清单。"
)

TEMPERATURE = 0.0  # 评测需要确定性，报告场景不用发散


def template_report(state: dict) -> str:
    """确定性兜底报告（D2 骨架逻辑原样保留，作为降级终点与离线基准）。"""
    evidence: list[Evidence] = state.get("evidence", [])
    lines = [f"- {ev.title} [{ev.eid}]（{ev.locator}）" for ev in evidence]
    report = "## 研究报告（模板版）\n\n### 证据清单\n" + "\n".join(lines)
    gaps = state.get("gaps", [])
    if gaps:
        report += "\n\n### 未覆盖（诚实披露）\n" + "\n".join(f"- {q}" for q in gaps)
    return report


def _format_evidence(evidence: list[Evidence]) -> str:
    blocks = []
    for ev in evidence:
        # 证据正文是"数据"不是"指令"：显式分隔符包裹（纵深防御的一层）
        blocks.append(
            f"[{ev.eid}]（来源类型: {ev.source_type}）{ev.title}\n"
            f"链接：{ev.locator}\n"
            f"<<<证据内容开始\n{ev.snippet}\n>>>证据内容结束"
        )
    return "\n\n".join(blocks)


def _user_prompt(state: dict) -> str:
    parts = [f"研究问题：{state['question']}"]
    if state.get("focus"):
        parts.append(f"用户关注点：{state['focus']}")
    if state.get("gaps"):
        parts.append("已知未覆盖的子问题：" + "；".join(state["gaps"]))
    if state.get("errors"):
        parts.append(
            "（注意：以下来源本次取证失败，不要臆造它们的内容："
            + "; ".join(state["errors"][:5])
            + "）"
        )
    parts.append("证据列表：\n" + _format_evidence(state.get("evidence", [])))
    return "\n\n".join(parts)


def _messages(state: dict) -> list:
    return [
        ("system", SYSTEM_PROMPT),
        ("user", _user_prompt(state)),
    ]


def _emit_delta(sink: EventSink, text: str) -> None:
    if text:
        sink.emit(AgentEvent(EVENT_REPORT_DELTA, {"text": text}))


def synthesize_report(state: dict, sink: EventSink) -> tuple[str, dict[str, int], dict]:
    """入口：永不抛异常（③兜底保证）。返回 (报告, token_usage, citation_report)。"""
    evidence: list[Evidence] = state.get("evidence", [])
    chain = _model_chain()
    if not chain:
        logger.warning("synthesizer：未配置任何模型 Key，走模板兜底")

    # ① 流式生成：边生成边发 report_delta，UI 逐字呈现
    for provider, base_url, model, api_key in chain:
        try:
            llm = _build_chat(provider, base_url, model, api_key, TEMPERATURE)
            chunks: list[str] = []
            for chunk in llm.stream(_messages(state)):
                text = str(chunk.content) if chunk.content else ""
                if text:
                    chunks.append(text)
                    _emit_delta(sink, text)
            report = "".join(chunks).strip()
            if report:
                logger.info("synthesizer ① 成功：%s/%s，%d 字", provider, model, len(report))
                # 流式 usage 以估算为准（分块 usage 汇总各 provider 行为不一）
                return (
                    report,
                    {"synthesizer": estimate_tokens(report)},
                    check_citations(report, {ev.eid for ev in evidence}),
                )
        except Exception as exc:  # 限流/断流都降下一级
            logger.warning("synthesizer ① 失败（%s/%s）：%s", provider, model, exc)

    # ② 非流式生成（链上逐个试）
    for provider, base_url, model, api_key in chain:
        try:
            llm = _build_chat(provider, base_url, model, api_key, TEMPERATURE)
            resp = llm.invoke(_messages(state))
            report = str(resp.content).strip()
            if report:
                _emit_delta(sink, report)
                return (
                    report,
                    {"synthesizer": estimate_tokens(report)},
                    check_citations(report, {ev.eid for ev in evidence}),
                )
        except Exception as exc:
            logger.warning("synthesizer ② 失败（%s/%s）：%s", provider, model, exc)

    # ③ 模板兜底
    report = template_report(state)
    sink.emit(
        AgentEvent(
            EVENT_WARNING,
            {"where": "synthesizer", "error": "LLM 生成失败，已用模板兜底"},
        )
    )
    _emit_delta(sink, report)
    return (
        report,
        {"synthesizer": estimate_tokens(report)},
        check_citations(report, {ev.eid for ev in evidence}),
    )
