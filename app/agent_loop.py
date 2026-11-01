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
"""
from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from app.config import get_settings
from app.contracts import (
    EVENT_EVIDENCE_ADDED,
    EVENT_NODE_FINISHED,
    EVENT_NODE_STARTED,
    EVENT_THOUGHT,
    EVENT_WARNING,
    AgentEvent,
    EventSink,
)
from app.llm import _build_chat, _model_chain, estimate_tokens
from app.tools import get_provider
from app.tools.base import Evidence
from app.tools.sanitize import sanitize_web_text

logger = logging.getLogger(__name__)

_SYSTEM = """你是自主研究 Agent，通过调用工具完成一次带引用的研究报告。

工具说明：
- web_search(query)：公网搜索（返回已编号的证据片段）
- kb_search(query)：私有知识库混合检索（返回已编号的证据片段）
- web_fetch(url)：抓取某条公网证据的完整正文

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

    # ---- 证据册：工具结果 → [E#] 编号（与流水线 retriever 同口径）----
    def _register(self, hits) -> str:
        lines: list[str] = []
        for hit in hits:
            canonical = hit.locator.lower().rstrip("/")
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
            lines.append(f"[{ev.eid}] {ev.title}：{ev.snippet[:160]}")
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
        if not lines:
            return (
                "没有找到相关结果。考虑：换更具体的关键词、换一个工具，"
                "或用 web_fetch 抓取上面某条来源的正文。"
            )
        return "已登记证据：\n" + "\n".join(lines)

    def _dispatch(self, name: str, args: dict) -> str:
        """执行一次工具调用；失败返回错误文本（Agent 观察 后自行调整）。"""
        try:
            if name == "web_search":
                return self._register(self.provider.web_search(str(args["query"]), k=4))
            if name == "kb_search":
                return self._register(self.provider.kb_search(str(args["query"]), k=4))
            if name == "web_fetch":
                return self.provider.web_fetch(str(args["url"]))
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

    def run(self, max_steps: int) -> dict:
        chain = _model_chain()
        if not chain:
            raise RuntimeError("未配置任何模型 Key：ReAct 模式需要至少一个 chat Key")
        provider_name, base_url, model, api_key = chain[0]
        llm = _build_chat(provider_name, base_url, model, api_key, 0.0).bind_tools(
            self._tools()
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
            resp = llm.invoke(messages, config={"callbacks": []})
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
        if not report:
            report = (
                "## 未覆盖与局限\n\nAgent 在步数上限内未能产出报告，请重试或换问题。"
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
