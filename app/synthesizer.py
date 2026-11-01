"""LLM 综合器：证据 → 带引用编号的研究报告（流式输出 + 三级降级）。

设计要点：
- 提示词三铁律：只依据证据、每个论断带 [E#]、未覆盖就明说；
- 证据正文用分隔符包裹并声明"是数据不是指令"——PLAN §7.1 的第 2 道防线
  （明示：分隔符可被绕过，真正的兜底是 citation pass + 最小权限工具）；
- 证据先过字符预算再进 prompt（PLAN §3.2"双熔断之二"，评审 P1-4 落地）；
- 降级链：①流式生成 → ②非流式生成 → ③模板兜底，任何优化不能成为新故障点；
  ①流到一半断掉时先发 report_reset 清场（评审 P1-6），UI 才不会把降级全文
  拼在残文后面；
- 生成完立即过 citation pass（app/citation.py），并对最终报告做注入二次
  扫描（评审 P2-9：输入侧扫描拦不住"模型被证据带偏"，报告才是事实判据）。
"""
from __future__ import annotations

import logging

from app.citation import check_citations
from app.config import get_settings
from app.contracts import (
    EVENT_REPORT_DELTA,
    EVENT_REPORT_RESET,
    EVENT_THOUGHT,
    EVENT_WARNING,
    AgentEvent,
    EventSink,
)
from app.llm import _build_chat, _model_chain, estimate_tokens
from app.tools.base import Evidence
from app.tools.sanitize import detect_injection
from app.tracing import callbacks as tracing_callbacks

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


def _evidence_within_budget(
    evidence: list[Evidence], budget: int
) -> tuple[list[Evidence], int]:
    """证据总字符预算（PLAN §3.2"双熔断之二"，评审 P1-4 落地）。

    按 eid 顺序累加 snippet 长度、超预算即止：eid 顺序≈取证顺序，先到先得
    简单可预期；按相关性裁剪是 D10 压缩节点的事。返回 (入选证据, 被裁条数)
    ——裁了几条必须让用户知道，静默丢证据 = 报告覆盖率悄悄缩水。
    """
    kept: list[Evidence] = []
    used = 0
    for ev in evidence:
        if kept and used + len(ev.snippet) > budget:
            break
        kept.append(ev)
        used += len(ev.snippet)
    return kept, len(evidence) - len(kept)


def _within_budget_or_compress(
    evidence: list[Evidence], sink: EventSink
) -> tuple[list[Evidence], int, str]:
    """证据预算的两级策略（v2.3 上下文压缩）：先硬截断；被裁的量够大且
    LLM 可用时，把被裁内容压缩成一段摘要作为"补充素材"注入 prompt——
    摘要无 eid，模型被明确禁止引用它（引用对账天然拦截幻觉）。
    LLM 不可用/失败 → 退回纯截断（D5 语义），永不因压缩挂掉。"""
    kept, dropped = _evidence_within_budget(
        evidence, get_settings().evidence_char_budget
    )
    if not dropped:
        return kept, 0, ""
    dropped_items = evidence[len(kept):]
    if len(dropped_items) < 2:
        sink.emit(
            AgentEvent(
                EVENT_WARNING,
                {
                    "where": "synthesizer",
                    "error": f"证据超出字符预算，已裁剪 {dropped} 条"
                    f"（budget={get_settings().evidence_char_budget}）",
                },
            )
        )
        return kept, dropped, ""

    chain = _model_chain()  # 模块级名字：测试桩/配置覆盖才能生效（局部 import 会绕过）
    if not chain:
        return kept, dropped, ""
    raw = " ".join(ev.snippet[:400] for ev in dropped_items)[:6000]
    provider, base_url, model, api_key = chain[0]
    try:
        resp = _build_chat(provider, base_url, model, api_key, 0.0).invoke(
            [
                (
                    "user",
                    "把下面这些被预算裁掉的检索要点压缩成 ≤250 字的要点清单，"
                    "保留所有数字、结论与来源名，不要任何开场白：\n\n" + raw,
                )
            ],
            config={"callbacks": tracing_callbacks()},
        )
        summary = str(resp.content).strip()
        if not summary:
            return kept, dropped, ""
        sink.emit(
            AgentEvent(
                EVENT_THOUGHT,
                {
                    "stage": "compress",
                    "text": f"{dropped} 条超预算证据已压缩为 {len(summary)} 字摘要"
                    "（无编号，禁止引用）",
                },
            )
        )
        return kept, dropped, summary
    except Exception as exc:  # noqa: BLE001 —— 压缩失败退纯截断
        logger.warning("证据压缩失败，退回纯截断：%s", exc)
        return kept, dropped, ""


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


def _user_prompt(state: dict, evidence: list[Evidence]) -> str:
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
    parts.append("证据列表：\n" + _format_evidence(evidence))
    return "\n\n".join(parts)


def _messages(state: dict, evidence: list[Evidence], compressed: str = "") -> list:
    user = _user_prompt(state, evidence)
    if compressed:
        # 压缩摘要无 eid：明确禁止引用（引用对账会拦幻觉），仅作背景素材
        user += ("\n\n[被预算裁掉证据的压缩摘要——仅作背景，禁止引用]\n" + compressed)
    return [
        ("system", SYSTEM_PROMPT),
        ("user", user),
    ]


def _emit_delta(sink: EventSink, text: str) -> None:
    if text:
        sink.emit(AgentEvent(EVENT_REPORT_DELTA, {"text": text}))


def _finalize(
    report: str, usage: dict[str, int], evidence_ids: set[str], sink: EventSink
) -> tuple[str, dict[str, int], dict]:
    """三条产出路径的共同收尾：报告注入二次扫描 + citation 对账。

    为什么扫报告而不只扫输入：输入侧扫描命中≠注入成功，模型可能没理它；
    反过来输入侧漏检的绕过也可能被模型执行。最终报告是否带出注入指令
    才是"注入防御"指标的事实判据（评审 P2-9）——只报告不拦截。
    """
    flags = detect_injection(report)
    if flags:
        sink.emit(
            AgentEvent(
                EVENT_WARNING,
                {
                    "where": "report-scan",
                    "patterns": flags[:5],
                    "hint": "最终报告含注入特征——模型可能已被证据内容带偏，引用前人工核读",
                },
            )
        )
    return report, usage, check_citations(report, evidence_ids)


def synthesize_report(state: dict, sink: EventSink) -> tuple[str, dict[str, int], dict]:
    """入口：永不抛异常（③兜底保证）。返回 (报告, token_usage, citation_report)。"""
    evidence: list[Evidence] = state.get("evidence", [])
    kept, dropped_n, compressed = _within_budget_or_compress(
        evidence, sink
    )
    evidence_ids = {ev.eid for ev in kept}
    chain = _model_chain()
    if not chain:
        logger.warning("synthesizer：未配置任何模型 Key，走模板兜底")

    # ① 流式生成：边生成边发 report_delta，UI 逐字呈现
    for provider, base_url, model, api_key in chain:
        emitted_any = False  # 每个 provider 独立记账：上一家的残文与本家无关
        try:
            llm = _build_chat(provider, base_url, model, api_key, TEMPERATURE)
            chunks: list[str] = []
            for chunk in llm.stream(
                _messages(state, kept, compressed), config={"callbacks": tracing_callbacks()}
            ):
                text = str(chunk.content) if chunk.content else ""
                if text:
                    chunks.append(text)
                    emitted_any = True
                    _emit_delta(sink, text)
            report = "".join(chunks).strip()
            if report:
                logger.info("synthesizer ① 成功：%s/%s，%d 字", provider, model, len(report))
                # 流式 usage 以估算为准（分块 usage 汇总各 provider 行为不一）
                return _finalize(
                    report, {"synthesizer": estimate_tokens(report)}, evidence_ids, sink
                )
        except Exception as exc:  # 限流/断流都降下一级
            if emitted_any:
                # 流到一半断掉：已推给 UI 的残文必须先清场再降级，否则 ②③ 的
                # 全文会拼在半截后面（评审 P1-6；report_reset 的唯一发射点）
                sink.emit(AgentEvent(EVENT_REPORT_RESET, {}))
            logger.warning("synthesizer ① 失败（%s/%s）：%s", provider, model, exc)

    # ② 非流式生成（链上逐个试）
    for provider, base_url, model, api_key in chain:
        try:
            llm = _build_chat(provider, base_url, model, api_key, TEMPERATURE)
            resp = llm.invoke(
                _messages(state, kept, compressed),
                config={"callbacks": tracing_callbacks()},
            )
            report = str(resp.content).strip()
            if report:
                _emit_delta(sink, report)
                return _finalize(
                    report, {"synthesizer": estimate_tokens(report)}, evidence_ids, sink
                )
        except Exception as exc:
            logger.warning("synthesizer ② 失败（%s/%s）：%s", provider, model, exc)

    # ③ 模板兜底（同样只展示预算内的证据，与 LLM 路径口径一致）
    report = template_report({**state, "evidence": kept})
    sink.emit(
        AgentEvent(
            EVENT_WARNING,
            {"where": "synthesizer", "error": "LLM 生成失败，已用模板兜底"},
        )
    )
    _emit_delta(sink, report)
    return _finalize(
        report, {"synthesizer": estimate_tokens(report)}, evidence_ids, sink
    )
