"""FastAPI SSE 网关：图的事件流出网边界（D8，事件契约 v1 的第二个消费端起点）。

为什么单独一层（PLAN §3.1）：三端（Streamlit / FastAPI SSE / Next.js）共用
同一份事件契约——网关只做"把 EventSink 的事件逐条转成 SSE 帧"这一件事，
不夹带任何私有结构。AgentEvent.to_json() 就是线上协议，
改契约 = 三端同步改（AGENTS.md 硬红线）。

为什么网关自己开线程跑图而不是 async 里直接 invoke：run_research 是同步
重活（检索+LLM，分钟级），写进 async 端点会冻住整个事件循环。线程 +
队列与 Streamlit 端同款模式（frontend/app.py 的 QueueSink），两侧代码对称。

终止纪律（契约红线）：done 必须是最后一个事件。研究线程崩溃时网关负责
补一条 error 事件收尾——客户端永远能靠"收到 done/error"判断流结束，
绝不静默断流。

已知边界（对抗性审查登记，单用户演示场景下接受）：
- 并发 run 无上限：多个 POST 各起一个研究线程，共享 provider 单例；
  MCP ClientSession 按请求 id 复用可并发，但重负载下应加队列/限流；
- 客户端中途断开：SSE 生成器收到 GeneratorExit 即停，研究线程作为
  daemon 跑完自然退出（结果丢弃）——短演示无碍，长驻服务需要取消机制；
- 每研究一次申请一个事件队列，随线程结束被 GC，无跨 run 状态。
"""
from __future__ import annotations

import json
import logging
import queue
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.contracts import (
    EVENT_DONE,
    EVENT_ERROR,
    AgentEvent,
)
from app.graph.build import run_research

logger = logging.getLogger(__name__)

app = FastAPI(title="研知 · Deep Research Agent 网关", version="0.1.0")

_QUEUE_POLL_SECONDS = 0.5  # 轮询间隔：SSE 心跳感 vs 空转开销的折中


class _ThreadSafeSink:
    """把事件推进线程安全队列（网关版的 QueueSink，与 Streamlit 端同构）。"""

    def __init__(self, q: queue.Queue[AgentEvent]) -> None:
        self.q = q

    def emit(self, event: AgentEvent) -> None:
        self.q.put(event)


class ResearchRequest(BaseModel):
    # 长度上限：question 直接进 LLM prompt，无上限的输入=无上限的 token 账单
    # （对抗性审查补；超出即 422，客户端可见）
    question: str = Field(max_length=2000)
    focus: str = Field(default="", max_length=500)


@app.get("/api/health")
def health() -> dict:
    """探活：Streamlit 启动时判断网关是否在线。"""
    return {"status": "ok"}


def _run_in_thread(question: str, focus: str, q: queue.Queue[AgentEvent]) -> None:
    """研究线程主体。

    这里刻意【不】再补 error 事件：run_research 的兜底（build.py）在图失败时
    已经把契约 error 事件（{"code": "graph_failed", "msg"}）放进队列再 raise，
    网关再发一条客户端就会收到两个失败信号。这里只补日志；
    极端情况（异常发生在任何契约事件之前）由下面 stream() 的
    "线程死了且队列为空"兜底帧接住。"""
    try:
        run_research(question, focus, sink=_ThreadSafeSink(q))
    except Exception:  # noqa: BLE001 —— 网关兜底边界
        logger.exception("研究线程异常（契约 error 事件已由 build 层入队）")


def _sse_frame(event: AgentEvent) -> str:
    """契约 JSON → SSE 帧；ensure_ascii=False 保中文可读（SSE 无 charset 问题）。"""
    return f"data: {json.dumps(event.to_json(), ensure_ascii=False)}\n\n"


@app.post("/api/research")
def research(req: ResearchRequest) -> StreamingResponse:
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="缺少研究问题")

    q: queue.Queue[AgentEvent] = queue.Queue()
    thread = threading.Thread(
        target=_run_in_thread, args=(question, req.focus.strip(), q), daemon=True
    )
    thread.start()

    def stream():
        while True:
            try:
                event = q.get(timeout=_QUEUE_POLL_SECONDS)
            except queue.Empty:
                if not thread.is_alive() and q.empty():
                    # 线程死了却没留下终止事件（理论上 _run_in_thread 兜不住的
                    # 极端情况）：补 error 收尾，客户端不能挂在无限等待上。
                    # payload 键与 build.py 的契约口径一致（msg，UI 按 msg 取）
                    yield _sse_frame(
                        AgentEvent(
                            EVENT_ERROR,
                            {"code": "gateway_thread_died", "msg": "研究线程异常退出且未上报"},
                        )
                    )
                    return
                continue
            yield _sse_frame(event)
            if event.type in (EVENT_DONE, EVENT_ERROR):
                return

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
