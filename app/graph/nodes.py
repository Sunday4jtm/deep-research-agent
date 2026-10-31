"""图节点工厂：每个节点是"闭包住 sink 的函数"。

为什么用工厂而不是裸函数：事件出口（EventSink）由装配层注入，
节点本身保持可测试——离线测试注入 ListSink 断言事件序列。

D2 阶段全部是**确定性骨架实现**（PLAN D-4：先验证业务逻辑，再上 LLM）；
D3~D5 逐个替换为 LLM 版本，骨架版保留为离线测试的基准线。
"""
from __future__ import annotations

import re
from dataclasses import replace

from app.config import get_settings
from app.contracts import (
    EVENT_DONE,
    EVENT_EVIDENCE_ADDED,
    EVENT_NODE_FINISHED,
    EVENT_NODE_STARTED,
    EVENT_REPORT_DELTA,
    EVENT_ROUND_STARTED,
    EVENT_WARNING,
    AgentEvent,
    EventSink,
)
from app.tools import get_provider
from app.tools.base import Evidence
from app.tools.sanitize import detect_injection

# FakeProvider 的测试钩子：查询带此前缀时返回空结果，
# 用于离线验证"证据缺失 → 追问回环 → 熔断"路径（保持确定性）。
EMPTY_QUERY_PREFIX = "EMPTY::"


def make_plan_node(sink: EventSink):
    """拆解研究问题。D2 骨架：单子问题直通；D3 换 LLM 拆解 + 三级降级。"""

    def plan(state):
        sink.emit(AgentEvent(EVENT_NODE_STARTED, {"node": "planner"}))
        settings = get_settings()
        if settings.planner_mode == "llm":
            # 延迟 import：template 模式（离线测试）不应该碰 LLM 模块
            from app.planner import plan_subquestions

            sub_questions, usage = plan_subquestions(
                state["question"], state.get("focus", "")
            )
        else:
            from app.planner import template_plan

            sub_questions = template_plan(state["question"])
            usage = {"planner": 0}
        sink.emit(
            AgentEvent(
                EVENT_NODE_FINISHED,
                {
                    "node": "planner",
                    "subq": len(sub_questions),
                    "mode": settings.planner_mode,
                },
            )
        )
        # 初始化字段：让后续节点永远面对"完整状态"，不处理缺键
        return {
            "question": state["question"],
            "focus": state.get("focus", ""),
            "sub_questions": sub_questions,
            "attempted_queries": [],
            "learnings": [],
            "evidence": [],
            "subq_evidence": {},
            "conflicts": [],
            "gaps": [],
            "round": 0,
            "token_usage": usage,
            "errors": [],
        }

    return plan


def make_retrieve_node(sink: EventSink):
    """取证节点：对每个子问题查公网 + 知识库，统一编号入册。

    去重依据 attempted_queries：同一查询绝不重复提交（借鉴 dzhng/deep-research）。
    """

    def retrieve(state):
        sink.emit(AgentEvent(EVENT_NODE_STARTED, {"node": "retriever"}))
        provider = get_provider()
        settings = get_settings()
        evidence: list[Evidence] = list(state.get("evidence", []))
        attempted: list[str] = list(state.get("attempted_queries", []))
        subq_evidence: dict[str, list[str]] = dict(state.get("subq_evidence", {}))
        round_no = int(state.get("round", 0)) + 1

        sink.emit(AgentEvent(EVENT_ROUND_STARTED, {"round": round_no}))
        k = max(1, settings.breadth)
        errors: list[str] = list(state.get("errors", []))
        for subq in state.get("sub_questions", []):
            if subq in attempted:
                # 跨轮去重：查过【且有证据】的不再重复花钱；零证据的 gap 例外——
                # 上一轮很可能是瞬时失败（限流/熔断/超时），回环的价值正在于重试。
                # dedup 防的是"重复成功的花费"，不是"失败不许再试"（D8 前回环空转）
                if subq_evidence.get(subq):
                    continue
            else:
                attempted.append(subq)
            eids = subq_evidence.get(subq, [])
            # 分源容错（PLAN §3.4）：单源失败只记 warning + errors，
            # 不拖垮整轮——kb 未接入（D6 前）也不影响 web 取证
            for source, fetch in (
                # 默认参数绑定 subq：避免闭包晚绑定陷阱（ruff B023 同款关注点）
                ("web", lambda q=subq: provider.web_search(q, k=k)),
                ("kb", lambda q=subq: provider.kb_search(q, k=k)),
            ):
                try:
                    hits = fetch()
                except Exception as exc:  # NotImplementedError/网络/熔断一律兜住
                    errors.append(f"{source}/{subq[:40]}: {type(exc).__name__}")
                    sink.emit(
                        AgentEvent(
                            EVENT_WARNING,
                            {
                                "where": source,
                                "query": subq[:60],
                                "error": f"{type(exc).__name__}: {exc}"[:120],
                            },
                        )
                    )
                    continue
                for hit in hits:
                    if any(prev.locator == hit.locator for prev in evidence):
                        # 跨子问题去重：同一 URL 只入册一次——
                        # 不同子问题常搜到同一篇热门文章，重复证据会
                        # 稀释报告并烧 token（D4 实调中 E8/E12 撞车后修复）
                        continue
                    flags = detect_injection(hit.snippet)
                    if flags:
                        # 只报告不拦截：内容仍作为"数据"进入证据，
                        # 真正的隔离靠 D5 报告提示词的数据/指令分离
                        sink.emit(
                            AgentEvent(
                                EVENT_WARNING,
                                {
                                    "where": "injection-scan",
                                    "patterns": flags[:5],
                                    "locator": hit.locator[:80],
                                },
                            )
                        )
                    eid = f"E{len(evidence) + 1}"
                    ev = replace(hit, eid=eid)  # Evidence 是 frozen dataclass
                    evidence.append(ev)
                    eids.append(eid)
                    sink.emit(
                        AgentEvent(
                            EVENT_EVIDENCE_ADDED,
                            {
                                "eid": ev.eid,
                                "source_type": ev.source_type,
                                "title": ev.title,
                                "locator": ev.locator,
                            },
                        )
                    )
            subq_evidence[subq] = eids

        sink.emit(
            AgentEvent(
                EVENT_NODE_FINISHED,
                {"node": "retriever", "round": round_no, "evidence": len(evidence)},
            )
        )
        return {
            "evidence": evidence,
            "attempted_queries": attempted,
            "subq_evidence": subq_evidence,
            "round": round_no,
            "errors": errors,
        }

    return retrieve


_CLAIM_RE = re.compile(r"([\u4e00-\u9fffA-Za-z]{2,8})[\s:：]*(\d+(?:\.\d+)?%?)")


def _claims(text: str) -> dict[str, set[str]]:
    """抽"指标上下文 → 数值集合"：冲突比较的是同名指标的取值，
    不是裸数字——"BM25" 里的 25、"GPT-4" 里的 4 这类模型名数字
    必须挂在字母上下文下，才不会被当成两边的"口径"互相污染。"""
    claims: dict[str, set[str]] = {}
    for ctx, num in _CLAIM_RE.findall(text):
        claims.setdefault(ctx, set()).add(num)
    return claims


def _find_conflict(kb_text: str, web_text: str) -> tuple[str, str, str] | None:
    """返回 (指标名, kb 值, web 值)：同名指标在两源取值不同即冲突。"""
    kb = _claims(kb_text)
    web = _claims(web_text)
    for ctx, kb_vals in kb.items():
        if ctx in web and web[ctx] != kb_vals:
            return ctx, sorted(kb_vals)[0], sorted(web[ctx])[0]
    return None


def make_verify_node(sink: EventSink):
    """验证节点：gaps（零证据子问题 → 追问回环）+ 跨源指标分歧标记。

    冲突检测是【启发式】而不是语义裁决（如实定位）：同一子问题下 kb 与
    公网证据出现"同名指标、不同数值"（如两边都说"准确率"但一个 90% 一
    个 95%）时才标记——多半是统计口径/时间点不同，报告需要向读者披露
    而不是默默采信一边。与注入扫描同哲学：只报告不拦截（PLAN §7.1）。
    D10 的 gold conflict 夹具会检验这个启发式的召回。
    """

    def verify(state):
        sink.emit(AgentEvent(EVENT_NODE_STARTED, {"node": "verifier"}))
        subq_evidence = state.get("subq_evidence", {})
        evidence = state.get("evidence", [])
        by_eid = {ev.eid: ev for ev in evidence}
        gaps = [q for q, eids in subq_evidence.items() if not eids]

        conflicts: list[dict[str, str]] = []
        for subq, eids in subq_evidence.items():
            hits = [by_eid[eid] for eid in eids if eid in by_eid]
            kb_hits = [ev for ev in hits if ev.source_type == "kb"]
            web_hits = [ev for ev in hits if ev.source_type == "web"]
            if not kb_hits or not web_hits:
                continue  # 单源无对照，谈不上"跨源分歧"
            # 跨源合并指标口径后两两比对（一个子问题一条冲突，避免刷屏）
            kb_text = " ".join(ev.snippet for ev in kb_hits)
            web_text = " ".join(ev.snippet for ev in web_hits)
            found = _find_conflict(kb_text, web_text)
            if found is None:
                continue
            metric, kb_val, web_val = found
            conflicts.append(
                {"query": subq, "metric": metric, "kb": kb_val, "web": web_val}
            )
            sink.emit(
                AgentEvent(
                    EVENT_WARNING,
                    {
                        "where": "cross-source",
                        "query": subq[:60],
                        "metric": metric,
                        "kb": kb_val,
                        "web": web_val,
                    },
                )
            )

        sink.emit(
            AgentEvent(
                EVENT_NODE_FINISHED,
                {"node": "verifier", "gaps": len(gaps), "conflicts": len(conflicts)},
            )
        )
        return {"gaps": gaps, "conflicts": conflicts}

    return verify


def make_synth_node(sink: EventSink):
    """综合节点：LLM 版带引用报告 + citation pass（D5）；模板模式作离线基准。"""

    def synthesize(state):
        sink.emit(AgentEvent(EVENT_NODE_STARTED, {"node": "synthesizer"}))
        settings = get_settings()
        if settings.synth_mode == "llm":
            # 延迟 import：模板模式（离线测试）不碰 LLM 模块
            from app.synthesizer import synthesize_report

            report, usage, citation = synthesize_report(state, sink)
        else:
            from app.citation import check_citations
            from app.synthesizer import template_report

            report = template_report(state)
            usage = {"synthesizer": 0}
            citation = check_citations(
                report, {ev.eid for ev in state.get("evidence", [])}
            )
            sink.emit(AgentEvent(EVENT_REPORT_DELTA, {"text": report}))
        sink.emit(AgentEvent(EVENT_NODE_FINISHED, {"node": "synthesizer"}))
        # done 必须是终止事件：三端 UI 以它判定"研究结束"
        sink.emit(AgentEvent(EVENT_DONE, {"citation_report": citation}))
        merged_usage = dict(state.get("token_usage", {}))
        merged_usage.update(usage)
        return {
            "report": report,
            "citation_report": citation,
            "token_usage": merged_usage,
        }

    return synthesize
