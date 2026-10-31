"""图节点工厂：每个节点是"闭包住 sink 的函数"。

为什么用工厂而不是裸函数：事件出口（EventSink）由装配层注入，
节点本身保持可测试——离线测试注入 ListSink 断言事件序列。

D2 阶段全部是**确定性骨架实现**（PLAN D-4：先验证业务逻辑，再上 LLM）；
D3~D5 逐个替换为 LLM 版本，骨架版保留为离线测试的基准线。
"""
from __future__ import annotations

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
            if subq in attempted:  # 跨轮去重：查过的不再查
                continue
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


def make_verify_node(sink: EventSink):
    """验证节点：找出零证据子问题作为 gaps；冲突检测 D8 实现。"""

    def verify(state):
        sink.emit(AgentEvent(EVENT_NODE_STARTED, {"node": "verifier"}))
        gaps = [q for q, eids in state.get("subq_evidence", {}).items() if not eids]
        sink.emit(
            AgentEvent(EVENT_NODE_FINISHED, {"node": "verifier", "gaps": len(gaps)})
        )
        return {"gaps": gaps}

    return verify


def make_synth_node(sink: EventSink):
    """综合节点：D2 骨架版——证据清单 + 未覆盖披露；D5 换 LLM 带引用报告。"""

    def synthesize(state):
        sink.emit(AgentEvent(EVENT_NODE_STARTED, {"node": "synthesizer"}))
        evidence: list[Evidence] = state.get("evidence", [])
        lines = [f"- {ev.title} [{ev.eid}]（{ev.locator}）" for ev in evidence]
        report = "## 研究报告（D2 骨架版）\n\n### 证据清单\n" + "\n".join(lines)
        gaps = state.get("gaps", [])
        if gaps:
            report += "\n\n### 未覆盖（诚实披露）\n" + "\n".join(f"- {q}" for q in gaps)
        citation_report = {"total": len(evidence), "mapped": len(evidence)}
        sink.emit(AgentEvent(EVENT_NODE_FINISHED, {"node": "synthesizer"}))
        sink.emit(AgentEvent(EVENT_REPORT_DELTA, {"text": report}))
        # done 必须是终止事件：三端 UI 以它判定"研究结束"，前面不能还有事件
        sink.emit(AgentEvent(EVENT_DONE, {"citation_report": citation_report}))
        return {"report": report, "citation_report": citation_report}

    return synthesize
