"""ReAct 自主循环（v2.4）：让 LLM 当司机，而不是流水线上的工人。

为什么有它（对"为什么感觉不像 AI agent"的回答）：D1~D8 的 StateGraph 是
"结构化流水线"——查询由 planner 预先拆好，检索由图按清单跑，LLM 只在
固定点被调用，**决策权在图代码里**。真正的 Agent 是模型在一个
思考→行动→观察 循环里自主决定：调用哪个工具、用什么参数、要不要继续。
本模块把 ToolProvider 的三个只读工具以 function-calling 交给模型，
循环由模型驱动（它不再调用工具 = 它认为可以写报告了）。

为什么两套范式并存（答辩口径）：同一事件契约下，"流水线"可评测、可靠、
行为可复现；"ReAct 循环"自主、透明、适应性强。quiz_agent（老仓库）是
create_agent 风格——本模块补齐后，"两层抽象对比"升级为"三种范式对比"。

引用纪律：工具结果进入证据册时自动编号 [E#]，观察文本里带编号回给模型，
模型自然引用 → citation pass 原样生效，零特判。

健壮性与可观测（2026-11-05 评估修正）：单步调用逐 provider 降级、全败才
抛（此前只取 chain[0]，主模型限流即死且无兜底），并挂 Langfuse trace
（此前显式空 callbacks，ReAct 全程不可观测）；INJECTION_FILTER=strict 时
命中注入特征的证据入册但观察文本脱敏——隔离必须发生在进消息之前，
因为观察文本就是模型的直接输入。
"""
from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from app.config import get_settings
from app.contracts import (
    EVENT_DONE,
    EVENT_EVIDENCE_ADDED,
    EVENT_NODE_FINISHED,
    EVENT_NODE_STARTED,
    EVENT_REPORT_DELTA,
    EVENT_THOUGHT,
    EVENT_WARNING,
    AgentEvent,
    EventSink,
)
from app.llm import _build_chat, _model_chain, estimate_tokens
from app.tools import get_provider
from app.tools.base import Evidence, canonicalize_locator
from app.tools.sanitize import detect_injection, sanitize_web_text, strip_injected_lines
from app.tracing import callbacks as tracing_callbacks

logger = logging.getLogger(__name__)

_SYSTEM = """你是自主研究 Agent，通过调用工具完成一次带引用的研究报告。

工具说明：
- web_search(query)：公网搜索（返回已编号的证据片段）
- kb_search(query)：私有知识库混合检索（返回已编号的证据片段）
- web_fetch(url)：抓取某条公网证据的完整正文（正文同样自动编号入册 [E#]）

规则：
1. 每次工具返回的结果都已自动编号 [E#]；最终报告里每个论断必须标注 [E#]，
   只能引用工具返回过的编号，禁止编造。
2. 自己决定调用顺序与次数：证据足够（能支撑 3~5 条结论）就停止调用，
   直接输出最终报告，结构：## 核心结论（3~5 条，每条带 [E#]）→ ## 分节详述 →
   ## 未覆盖与局限 → ## 引用清单。
3. 某个工具连续失败就换工具或换查询；证据不足时如实写"未覆盖"，禁止编造。
4. 用户关注点优先覆盖。"""


class _ReactLoop:
    """单次研究的 ReAct 循环：维护消息历史与证据册，事件实时出流。"""

    def __init__(self, question: str, focus: str, sink: EventSink) -> None:
        self.question = question
        self.focus = focus
        self.sink = sink
        self.provider = get_provider()
        self.evidence: list[Evidence] = []
        self.seen: set[str] = set()
        self.steps = 0
        self.errors: list[str] = []
        self._tokens = 0  # 每步输出 token 粗估累计（口径见 run 内注释）

    # ---- 证据册：工具结果 → [E#] 编号（与流水线 retriever 同口径）----
    def _register(self, hits) -> str:
        lines: list[str] = []
        for hit in hits:
            canonical = canonicalize_locator(hit.locator)
            if canonical in self.seen:
                continue
            self.seen.add(canonical)
            ev = Evidence(
                eid=f"E{len(self.evidence) + 1}",
                source_type=hit.source_type,
                title=hit.title[:120],
                locator=hit.locator,
                snippet=sanitize_web_text(hit.snippet),
            )
            self.evidence.append(ev)
            self.sink.emit(
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
            # strict 注入过滤（与流水线 _quarantine_flagged 同哲学）：证据册
            # 保留原文（与流水线 state.evidence 同口径，可人工核读），但观察
            # 文本只给标题不给内容——模型看不到指令就不会执行，报告侧剥除
            # 只作绕过兜底。与流水线的口径差异（如实）：流水线把隔离证据
            # 记为"不可引用"（不在对账账册），这里 eid 对模型可见、引用合法
            # ——它至少看到了"这条来源存在"。
            flags = detect_injection(ev.snippet)
            strict = flags and get_settings().injection_filter == "strict"
            if strict:
                self.sink.emit(
                    AgentEvent(
                        EVENT_WARNING,
                        {
                            "where": "injection-quarantine",
                            "eids": [ev.eid],
                            "locator": hit.locator[:80],
                            "hint": "命中注入特征：证据入册，但内容不进模型观察",
                        },
                    )
                )
                lines.append(f"[{ev.eid}] {ev.title}：（该来源命中注入特征，内容已隔离）")
                continue
            lines.append(f"[{ev.eid}] {ev.title}：{ev.snippet[:160]}")
        if not lines:
            if not hits:
                return (
                    "没有找到相关结果。考虑：换更具体的关键词、换一个工具，"
                    "或用 web_fetch 抓取上面某条来源的正文。"
                )
            # 全部命中已有证据 ≠ 没找到结果：误导文案会让模型白白换查询（评审 P3）
            return (
                "本次结果都已登记过（无新增）。考虑：换更具体的关键词、"
                "换一个工具，或用 web_fetch 读透某条来源。"
            )
        return "已登记证据：\n" + "\n".join(lines)

    def _register_fetch(self, url: str, text: str) -> str:
        """抓取正文入册编号：让提示词「每次工具返回都已编号 [E#]」成真。

        fetch 正文此前裸返——模型基于它的论断要么违反引用规则裸写（引用
        存在率掉）、要么幻觉编号（citation pass unmapped）。同 URL 重复
        抓取返回已有编号不重复入册（fetch 是该来源的全文扩展）。
        """
        canonical = canonicalize_locator(url)
        for ev in self.evidence:
            if canonicalize_locator(ev.locator) == canonical:
                return (
                    f"正文已抓取（{len(text)} 字）。该来源已登记为 [{ev.eid}]，"
                    f"引用使用 [{ev.eid}]。"
                )
        ev = Evidence(
            eid=f"E{len(self.evidence) + 1}",
            source_type="web",
            title=url.split("//", 1)[-1][:120],
            locator=url,
            snippet=sanitize_web_text(text),
        )
        self.evidence.append(ev)
        self.sink.emit(
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
        return f"[{ev.eid}] 已抓取并登记 {url} 的正文（{len(text)} 字）。引用使用 [{ev.eid}]。"

    def _dispatch(self, name: str, args: dict) -> str:
        """执行一次工具调用；失败返回错误文本（Agent 观察 后自行调整）。"""
        try:
            if name == "web_search":
                return self._register(self.provider.web_search(str(args["query"]), k=4))
            if name == "kb_search":
                return self._register(self.provider.kb_search(str(args["query"]), k=4))
            if name == "web_fetch":
                text = self.provider.web_fetch(str(args["url"]))
                return self._register_fetch(str(args["url"]), text)
            return f"未知工具 {name}"
        except Exception as exc:  # noqa: BLE001 —— 工具失败是"观察"的一部分
            self.errors.append(f"{name}: {type(exc).__name__}")
            self.sink.emit(
                AgentEvent(
                    EVENT_WARNING,
                    {"where": f"react/{name}", "error": f"{type(exc).__name__}: {exc}"[:120]},
                )
            )
            return f"工具 {name} 失败：{exc}。请换个工具或换查询重试。"

    # ---- 工具定义（function-calling schema）----
    def _tools(self) -> list:
        provider = self.provider

        @tool
        def web_search(query: str) -> str:
            """公网搜索。返回带 [E#] 编号的证据片段，编号用于最终报告引用。"""
            return provider.web_search(query, k=4) or "没有找到相关结果。"

        @tool
        def kb_search(query: str) -> str:
            """私有知识库混合检索（向量+BM25）。优先用它回答与用户资料相关的问题。"""
            return provider.kb_search(query, k=4) or "私有知识库中没有相关内容。"

        @tool
        def web_fetch(url: str) -> str:
            """抓取公网页面完整正文（用于把某条 [E#] 证据读透）。"""
            return provider.web_fetch(url)

        return [web_search, kb_search, web_fetch]

    def _invoke_step(self, bound: list, messages: list):
        """单步调用：主备逐个试 + Langfuse trace，全败才抛。

        此前这里显式传 config={"callbacks": []}，ReAct 全程不可观测（评估
        P1 项）；现在与流水线口径一致挂 tracing_callbacks()——开关关闭时
        返回空列表，离线测试零风险。
        """
        errors: list[str] = []
        for provider_name, llm in bound:
            try:
                return llm.invoke(messages, config={"callbacks": tracing_callbacks()})
            except Exception as exc:  # 限流/网络/Key 问题降下一个，不中断循环
                errors.append(f"{provider_name}: {type(exc).__name__}")
                logger.warning("ReAct 模型调用失败，切换下一个：%s - %s", provider_name, exc)
                self.sink.emit(
                    AgentEvent(
                        EVENT_WARNING,
                        {
                            "where": "react/failover",
                            "error": f"{type(exc).__name__}: {exc}"[:120],
                        },
                    )
                )
        raise RuntimeError("主备模型均失败（ReAct）：" + " | ".join(errors))

    def run(self, max_steps: int) -> dict:
        chain = _model_chain()
        if not chain:
            raise RuntimeError("未配置任何模型 Key：ReAct 模式需要至少一个 chat Key")
        # 每个候选模型预绑定工具（bind_tools 产物依赖 provider，不能走
        # lru_cache）；单步内逐 provider 降级、全败才抛——与
        # invoke_with_fallback 同口径。此前只取 chain[0]，主模型高峰限流
        # 时 ReAct 直接死（评估 P1 项）。
        tools = self._tools()
        bound = []
        for provider_name, base_url, model, api_key in chain:
            bound.append(
                (
                    provider_name,
                    _build_chat(provider_name, base_url, model, api_key, 0.0).bind_tools(tools),
                )
            )

        focus_line = f"\n用户关注点（优先覆盖）：{self.focus}" if self.focus else ""
        messages: list = [
            SystemMessage(content=_SYSTEM + focus_line),
            HumanMessage(content=f"研究问题：{self.question}"),
        ]

        self.sink.emit(
            AgentEvent(
                EVENT_NODE_STARTED,
                {"node": "agent", "mode": "react", "max_steps": max_steps},
            )
        )
        report = ""
        for self.steps in range(1, max_steps + 1):
            resp = self._invoke_step(bound, messages)
            # 口径如实（评审 P1：旧实现 = 报告字数估算×步数，无物理含义）：
            # 每步调用的输出按统一粗估累加，与流水线各档「输出粗估」同源
            self._tokens += estimate_tokens(str(getattr(resp, "content", "") or ""))
            if getattr(resp, "tool_calls", None):
                messages.append(resp)
                for tc in resp.tool_calls:
                    # 思考链上屏：Agent 决定调用什么（这就是"看得见的决策"）
                    args_preview = ", ".join(
                        f"{k}={str(v)[:40]}" for k, v in (tc.get("args") or {}).items()
                    )
                    self.sink.emit(
                        AgentEvent(
                            EVENT_THOUGHT,
                            {
                                "stage": "act",
                                "text": f"调用 {tc['name']}({args_preview})",
                            },
                        )
                    )
                    observation = self._dispatch(tc["name"], tc.get("args") or {})
                    messages.append(
                        ToolMessage(content=observation, tool_call_id=tc["id"])
                    )
                continue
            # 没有工具调用 = 模型认为可以交付报告
            report = str(resp.content).strip()
            if report:
                break
            self.sink.emit(
                AgentEvent(EVENT_WARNING, {"where": "react", "msg": "模型返回空内容，重试"})
            )
        else:
            logger.warning("ReAct 达到步数上限（%d 步）", max_steps)

        self.sink.emit(
            AgentEvent(
                EVENT_NODE_FINISHED,
                {"node": "agent", "steps": self.steps, "evidence": len(self.evidence)},
            )
        )
        if report and get_settings().injection_filter == "strict":
            # 交付前报告侧剥除（与流水线 _finalize 同款）：输入侧隔离拦的是
            # 已知证据，模型把观察内容改写/绕过写进报告时由这里兜底。
            report, removed = strip_injected_lines(report)
            if removed:
                self.sink.emit(
                    AgentEvent(
                        EVENT_WARNING,
                        {
                            "where": "report-strip",
                            "lines": removed,
                            "hint": "strict 模式已剥除报告中的注入命中行",
                        },
                    )
                )
        if not report:
            report = (
                "## 未覆盖与局限\n\nAgent 在步数上限内未能产出报告，请重试或换问题。"
            )
        # 终止事件补发（契约红线）：ReAct 没有流水线的 synth 节点，报告与
        # done 必须在这里发——否则网关兜底会误判"线程死亡"，前端也永远
        # 拿不到报告（实机踩过：研究完成却显示"线程异常退出"）
        self.sink.emit(AgentEvent(EVENT_REPORT_DELTA, {"text": report}))
        self.sink.emit(
            AgentEvent(
                EVENT_DONE,
                {
                    "citation_report": self._cite(report),
                    "token_usage": {"agent": self._tokens},
                },
            )
        )
        return {
            "question": self.question,
            "focus": self.focus,
            "evidence": self.evidence,
            "report": report,
            "citation_report": self._cite(report),
            "token_usage": {"agent": estimate_tokens(report) * self.steps},
            "round": self.steps,
            "subq_evidence": {},
            "gaps": [],
            "learnings": [],
            "rewritten": {},
            "errors": self.errors,
        }

    def _cite(self, report: str) -> dict:
        """与流水线 citation pass 同口径（独立函数避免循环 import）。"""

        from app.citation import check_citations

        return check_citations(report, {ev.eid for ev in self.evidence})


def run_react_research(
    question: str, focus: str, sink: EventSink, max_steps: int | None = None
) -> dict:
    """入口：一次 ReAct 研究 = 模型驱动的工具循环。返回与流水线同构的 State。"""
    steps = max_steps or int(get_settings().agent_max_steps)
    sink.emit(
        AgentEvent(
            EVENT_THOUGHT, {"stage": "boot", "text": f"自主 Agent 上线，步数上限 {steps}"}
        )
    )
    return _ReactLoop(question, focus, sink).run(steps)
