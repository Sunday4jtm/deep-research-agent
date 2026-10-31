"""研知 · Deep Research Agent —— Streamlit 界面（事件契约的首个消费端）。

为什么是"后台线程 + 事件队列轮询"：Streamlit 每次交互都会重跑整个脚本，
同步跑长任务会把页面锁死——老项目踩过的坑，这里复用"后台任务 + 轮询"
模式，只是数据源从 HTTP 轮询换成了进程内事件队列（QueueSink）。

启动（项目根目录）：
    /opt/anaconda3/envs/agent-research/bin/streamlit run frontend/app.py
"""
from __future__ import annotations

import queue
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # streamlit 只加脚本目录进 sys.path

import streamlit as st  # noqa: E402

from app.contracts import (  # noqa: E402
    EVENT_DONE,
    EVENT_REPORT_DELTA,
    AgentEvent,
)
from app.graph.build import run_research  # noqa: E402

st.set_page_config(page_title="研知 · Deep Research Agent", page_icon="🔬", layout="wide")


class QueueSink:
    """把图的事件流推进线程安全队列，UI 端轮询消费（跨线程的事件契约）。"""

    def __init__(self) -> None:
        self.q: queue.Queue[AgentEvent] = queue.Queue()

    def emit(self, event: AgentEvent) -> None:
        self.q.put(event)


for key, default in {
    "thread": None,
    "sink": None,
    "report_text": "",
    "done": False,
    "events": [],
    "citation": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

st.title("🔬 研知 · Deep Research Agent")
st.caption("规划 → 混合检索 → 验证回环 → 带引用报告｜事件契约 v1 实时呈现")

question = st.text_input(
    "研究问题",
    placeholder="例：LangGraph 和手写 Agent 循环哪个更适合生产？",
)
focus = st.text_input("关注点（可选）", placeholder="例：本地 CPU 部署")
running = st.session_state.thread is not None and st.session_state.thread.is_alive()
if st.button("开始研究", type="primary", disabled=running or not question.strip()):
    st.session_state.sink = QueueSink()
    st.session_state.report_text = ""
    st.session_state.done = False
    st.session_state.events = []
    st.session_state.citation = None
    st.session_state.thread = threading.Thread(
        target=run_research,
        args=(question.strip(), focus.strip()),
        kwargs={"sink": st.session_state.sink},
        daemon=True,
    )
    st.session_state.thread.start()

st.divider()
status_ph = st.empty()
report_ph = st.empty()
side_ph = st.container()


@st.fragment(run_every="1s")
def live_panel() -> None:
    """每秒取空事件队列：报告逐字增长，过程事件即时可见。"""
    sink = st.session_state.sink
    if sink is None:
        return
    while True:
        try:
            event = sink.q.get_nowait()
        except queue.Empty:
            break
        if event.type == EVENT_REPORT_DELTA:
            st.session_state.report_text += str(event.payload.get("text", ""))
        elif event.type == EVENT_DONE:
            st.session_state.done = True
            st.session_state.citation = event.payload.get("citation_report")
        else:
            st.session_state.events.append(event)

    alive = st.session_state.thread is not None and st.session_state.thread.is_alive()
    if st.session_state.done:
        status_ph.success("✅ 研究完成")
    elif alive:
        status_ph.info(
            f"⏳ 研究中…（事件 {len(st.session_state.events)} 条，"
            f"报告 {len(st.session_state.report_text)} 字）"
        )
    else:
        status_ph.info("输入问题，点「开始研究」")

    with side_ph:
        with st.expander("⚙️ 研究过程事件（实时）", expanded=not st.session_state.done):
            for event in st.session_state.events[-25:]:
                st.caption(f"`[{event.type}]` {event.payload}")
        if st.session_state.citation:
            c = st.session_state.citation
            st.caption(
                f"📎 引用对账：出现 {c['refs_total']} 次 / 去重 {c['refs_unique']} 个 / "
                f"可映射 {c['mapped']} 个｜证据 {c['evidence_total']} 条"
                + (f"｜⚠️ 幻觉引用：{c['unmapped']}" if c["unmapped"] else "")
            )

    if st.session_state.report_text:
        tail = " ▌" if alive and not st.session_state.done else ""
        report_ph.markdown(st.session_state.report_text + tail)


live_panel()
