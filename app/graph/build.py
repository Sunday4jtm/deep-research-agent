"""StateGraph 装配与研究入口。

为什么图由工厂函数装配（闭包注入 sink）：同一张图既能接 UI 的事件流，
也能接测试的 ListSink——三端共用一份编排逻辑，不复制粘贴。
"""
from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.config import get_settings
from app.contracts import (
    EVENT_ERROR,
    EVENT_RESEARCH_STARTED,
    AgentEvent,
    EventSink,
    ListSink,
)
from app.graph.nodes import (
    make_plan_node,
    make_retrieve_node,
    make_synth_node,
    make_verify_node,
)
from app.graph.state import ResearchState
from app.tracing import flush as flush_tracing


def route_after_verify(state: ResearchState) -> str:
    """条件边：有缺口且未到熔断轮数 → 回检索；否则进综合。"""
    settings = get_settings()
    if state.get("gaps") and state.get("round", 0) < settings.max_rounds:
        return "retrieve"
    return "synthesize"


def build_research_app(sink: EventSink):
    """装配研究图。节点顺序：规划 → 检索 → 验证 →（回环）→ 综合。"""
    graph = StateGraph(ResearchState)
    graph.add_node("planner", make_plan_node(sink))
    graph.add_node("retriever", make_retrieve_node(sink))
    graph.add_node("verifier", make_verify_node(sink))
    graph.add_node("synthesizer", make_synth_node(sink))
    graph.set_entry_point("planner")
    graph.add_edge("planner", "retriever")
    graph.add_edge("retriever", "verifier")
    graph.add_conditional_edges(
        "verifier", route_after_verify, {"retrieve": "retriever", "synthesize": "synthesizer"}
    )
    graph.add_edge("synthesizer", END)
    return graph.compile()


def run_research(question: str, focus: str = "", sink: EventSink | None = None) -> dict:
    """同步入口：一次研究 = 一个完整的图执行。返回最终 State。

    图执行任何异常都必须先以 error 事件收尾再向上抛：调用方跑在后台线程里
    （Streamlit / D8 SSE），线程外拿不到 traceback——没有 error 事件的话，
    UI 只会看到线程悄悄死亡、界面停在"研究中"假死（评审 P0-3）。这正
    是契约里 error{code,msg} 的存在意义：失败也是事件流的一部分。
    """
    active_sink = sink if sink is not None else ListSink()
    # provider_mode 进首事件：UI 据此亮"离线演示"徽标——fake 模式的证据长得
    # 跟真的一样，不讲明来源，现场演示忘切配置就会拿固定台词当真数据（评审 P2-10）
    active_sink.emit(
        AgentEvent(
            EVENT_RESEARCH_STARTED,
            {"question": question, "provider_mode": get_settings().provider_mode},
        )
    )
    try:
        app = build_research_app(active_sink)
        final = app.invoke(
            {"question": question, "focus": focus}, config={"recursion_limit": 50}
        )
    except Exception as exc:
        active_sink.emit(
            AgentEvent(
                EVENT_ERROR,
                {"code": "graph_failed", "msg": f"{type(exc).__name__}: {exc}"[:200]},
            )
        )
        raise
    finally:
        # 追踪冲刷放 finally：崩溃路径的 trace（含 error 现场）同样要落云，
        # 排障时最值钱的恰恰是失败 trace（D9）
        flush_tracing()
    # 跨 run 记忆沉淀（v2.3）：learnings + 来源清单落 data/memory/（gitignored）
    from app import memory as app_memory

    app_memory.append_run(
        question,
        list(final.get("learnings", [])),
        [ev.locator for ev in final.get("evidence", [])],
        len(str(final.get("report", ""))),
    )
    return dict(final)
