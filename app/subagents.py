"""检索层子 Agent（Subagents 模式，v2.5）：每个子问题一个独立上下文的迷你 ReAct 循环。

为什么存在（对照 2026-11-05 评估的 P2 项）：parallel 模式的并行是"工具调用级"
——查询由 planner 定死、retriever 照单执行，决策权在图代码；subagents 模式把
决策权下放到检索层：每个子问题由一个自带消息历史的子 Agent 驱动，自主决定查
哪个工具、查几次、要不要 web_fetch 深读。这是档①（pipeline）→档②（subagent
编排）的最小改造路径：主图 4 节点不变，变的只是 retriever 内部从"执行清单"
换成"编排子 Agent"。

红线对齐：LangGraph 只用 StateGraph/条件边/循环，不引 subgraph/checkpoint——
子 Agent 是纯 Python 消息循环（与 agent_loop.py 同款形态），不是 LangGraph 子图。

成本与失败语义：SUBAGENT_MAX_STEPS 步数熔断；LLM 主备逐个降级全败才抛（与
agent_loop 同口径）；单子 Agent 整体失败 → warning + errors，缺口交 verifier
的 gap 回环兜底。要点清单只作子 Agent 的"停止信号"，不进综合——引用口径统一
走 Evidence（全局编号由主图统一分配，避免局部/全局两套编号打架）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from app.contracts import (
    EVENT_THOUGHT,
    EVENT_WARNING,
    AgentEvent,
    EventSink,
)
from app.llm import _build_chat, _model_chain, estimate_tokens
from app.tools.base import Evidence, canonicalize_locator
from app.tools.sanitize import sanitize_web_text
from app.tracing import callbacks as tracing_callbacks

logger = logging.getLogger(__name__)

_SYSTEM = """你是检索子 Agent，只负责一个子问题的取证。

工具说明：
- web_search(query)：公网搜索（返回已编号的证据片段）
- kb_search(query)：私有知识库混合检索
- web_fetch(url)：抓取某条公网证据的完整正文

规则：
1. 工具返回的结果已自动编号 [E#]；工具失败是"观察"，换工具或换查询再试。
2. 自主决定调用顺序与次数：证据足够（能支撑 2~4 条要点）就停止调用，直接
   输出该子问题的要点清单（≤150 字，不带客套话）。
3. 只依据工具返回的内容，禁止编造；换查询时与子问题措辞明显不同以扩召回。
"""


@dataclass
class SubagentResult:
    """单个子 Agent 的产出：证据（局部编号，主图重编为全局 E#）+ 步数/token/失败。"""

    subq: str
    evidence: list[Evidence] = field(default_factory=list)
    steps: int = 0
    tokens: int = 0
    errors: list[str] = field(default_factory=list)


def has_chat_model() -> bool:
    """是否有可用 chat 模型链——nodes 用它决定 subagents 是否回落 parallel。"""
    return bool(_model_chain())


def _tools(provider):
    """function-calling schema（与 agent_loop 同款三件套）。执行走 dispatch 注册路径。"""
    @tool
    def web_search(query: str) -> str:
        """公网搜索。返回带 [E#] 编号的证据片段，编号用于要点引用。"""
        return provider.web_search(query, k=4) or "没有找到相关结果。"

    @tool
    def kb_search(query: str) -> str:
        """私有知识库混合检索（向量+BM25）。优先用它回答与用户资料相关的问题。"""
        return provider.kb_search(query, k=4) or "私有知识库中没有相关内容。"

    @tool
    def web_fetch(url: str) -> str:
        """抓取公网页面完整正文（把某条 [E#] 证据读透）。"""
        return provider.web_fetch(url)

    return [web_search, kb_search, web_fetch]


def _invoke(bound: list, messages: list, sink: EventSink, subq: str):
    """单步调用：主备逐个试 + Langfuse trace，全败才抛（与 agent_loop 同口径）。"""
    errors: list[str] = []
    for name, llm in bound:
        try:
            return llm.invoke(messages, config={"callbacks": tracing_callbacks()})
        except Exception as exc:  # 限流/网络/Key 问题降下一个，不中断循环
            errors.append(f"{name}: {type(exc).__name__}")
            logger.warning("子 Agent 模型调用失败，切换下一个：%s - %s", name, exc)
            sink.emit(
                AgentEvent(
                    EVENT_WARNING,
                    {
                        "where": "subagent/failover",
                        "subq": subq[:40],
                        "error": f"{type(exc).__name__}: {exc}"[:120],
                    },
                )
            )
    raise RuntimeError("主备模型均失败（subagent）：" + " | ".join(errors))


def run_subagent(
    subq: str,
    question: str,
    focus: str,
    provider,
    sink: EventSink,
    max_steps: int,
) -> SubagentResult:
    """单个子 Agent：独立消息循环，产出子问题的证据册。LLM 全败时向上抛。"""
    result = SubagentResult(subq=subq)
    chain = _model_chain()
    if not chain:
        # 调用方（nodes）已预检回落；这里是并发窗口下的安全网
        raise RuntimeError("未配置任何模型 Key：subagents 模式需要 chat Key")
    tools = _tools(provider)
    bound = []
    for name, base_url, model, api_key in chain:
        bound.append(
            (name, _build_chat(name, base_url, model, api_key, 0.0).bind_tools(tools))
        )

    focus_line = f"\n用户关注点（优先覆盖）：{focus}" if focus else ""
    messages: list = [
        SystemMessage(content=_SYSTEM + focus_line),
        HumanMessage(content=f"研究问题：{question}\n本 Agent 负责的子问题：{subq}"),
    ]
    seen: set[str] = set()

    def register(hits) -> str:
        """工具结果 → 局部 [E#] 入册（子 Agent 内部编号，主图消费时重编）。"""
        lines: list[str] = []
        for hit in hits:
            canonical = canonicalize_locator(hit.locator)
            if canonical in seen:
                continue
            seen.add(canonical)
            ev = Evidence(
                eid=f"E{len(result.evidence) + 1}",
                source_type=hit.source_type,
                title=hit.title[:120],
                locator=hit.locator,
                snippet=sanitize_web_text(hit.snippet),
            )
            result.evidence.append(ev)
            lines.append(f"[{ev.eid}] {ev.title}：{ev.snippet[:160]}")
        if not lines:
            return (
                "没有找到相关结果。考虑：换更具体的关键词、换一个工具，"
                "或用 web_fetch 深读上面某条来源。"
            )
        return "已登记证据：\n" + "\n".join(lines)

    def register_fetch(url: str, text: str) -> str:
        """fetch 深读：同源已登记条目就地替换为全文摘录（编号不变，消息引用不失效）；
        新来源正常入册。这比"丢弃同源全文"保留更多上下文，又不会让全局去重
        丢掉深读结果。"""
        if not (text or "").strip():
            return "抓取正文为空。换一条来源或换查询。"
        canonical = canonicalize_locator(url)
        snippet = sanitize_web_text(text)
        for i, ev in enumerate(result.evidence):
            if canonicalize_locator(ev.locator) == canonical:
                result.evidence[i] = Evidence(
                    eid=ev.eid,
                    source_type=ev.source_type,
                    title=ev.title,
                    locator=ev.locator,
                    snippet=snippet,
                )
                return f"[{ev.eid}] 已更新为全文摘录（{len(snippet)} 字）。"
        new_ev = Evidence(
            eid=f"E{len(result.evidence) + 1}",
            source_type="web",
            title=f"网页正文：{url[:80]}",
            locator=url,
            snippet=snippet,
        )
        result.evidence.append(new_ev)
        return f"[{new_ev.eid}] {new_ev.title}：{new_ev.snippet[:160]}"

    def dispatch(name: str, args: dict) -> str:
        """执行一次工具调用；失败返回错误文本（子 Agent 观察后自行调整）。"""
        try:
            if name == "web_search":
                return register(provider.web_search(str(args["query"]), k=4))
            if name == "kb_search":
                return register(provider.kb_search(str(args["query"]), k=4))
            if name == "web_fetch":
                return register_fetch(str(args["url"]), provider.web_fetch(str(args["url"])))
            return f"未知工具 {name}"
        except Exception as exc:  # noqa: BLE001 —— 工具失败是"观察"的一部分
            result.errors.append(f"{name}: {type(exc).__name__}")
            sink.emit(
                AgentEvent(
                    EVENT_WARNING,
                    {
                        "where": f"subagent/{name}",
                        "subq": subq[:40],
                        "error": f"{type(exc).__name__}: {exc}"[:120],
                    },
                )
            )
            return f"工具 {name} 失败：{exc}。请换个工具或换查询重试。"

    for result.steps in range(1, max_steps + 1):
        resp = _invoke(bound, messages, sink, subq)
        result.tokens += estimate_tokens(str(resp.content))
        if getattr(resp, "tool_calls", None):
            messages.append(resp)
            for tc in resp.tool_calls:
                # 思考链上屏：检索决策可见（与流水线 thought/ReAct act 同款）
                args_preview = ", ".join(
                    f"{k}={str(v)[:40]}" for k, v in (tc.get("args") or {}).items()
                )
                sink.emit(
                    AgentEvent(
                        EVENT_THOUGHT,
                        {
                            "stage": "subagent",
                            "text": f"[{subq[:20]}] 调用 {tc['name']}({args_preview})",
                        },
                    )
                )
                observation = dispatch(tc["name"], tc.get("args") or {})
                messages.append(ToolMessage(content=observation, tool_call_id=tc["id"]))
            continue
        report = str(resp.content).strip()
        if report:
            break  # 要点清单 = 停止信号（内容不进综合，引用统一走 Evidence）
        sink.emit(
            AgentEvent(EVENT_WARNING, {"where": "subagent", "msg": "模型返回空内容，重试"})
        )
    else:
        logger.warning("子 Agent 达到步数上限（%d 步）：%s", max_steps, subq[:40])

    return result
