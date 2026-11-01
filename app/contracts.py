"""事件契约 v1（PLAN §3.3）——三套 UI 共用同一份定义（AGENTS.md 硬红线）。

为什么 payload 一律是 JSON 可序列化的 dict：事件要跨 FastAPI SSE 网关出网，
任何 dataclass 私有结构都会在序列化边界上翻车；字符串常量三端按字面匹配，
改名 = 三端同步改（老项目"前后端契约漂移当天可见"的同款教训）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

# ---- 事件类型常量：三端按字符串字面匹配，勿改名 ----
EVENT_RESEARCH_STARTED = "research_started"
EVENT_NODE_STARTED = "node_started"
EVENT_NODE_FINISHED = "node_finished"
EVENT_ROUND_STARTED = "round_started"
EVENT_EVIDENCE_ADDED = "evidence_added"
EVENT_REPORT_DELTA = "report_delta"
# 流式断流且已发出过 delta 时发：UI 收到即清空已呈现的残文，防止降级路径的
# 全文拼在半截后面（评审 P1-6）。只在 synthesizer 降级边界发，其他节点禁用。
EVENT_REPORT_RESET = "report_reset"
# Agent 化升级（v2.3）：反思/规划/压缩的"思考链"事件——payload {stage, text}，
# UI 据此渲染思考链时间线；三端同步（网关透传、Streamlit 事件流、静态前端时间线）
EVENT_THOUGHT = "thought"
EVENT_WARNING = "warning"
EVENT_DONE = "done"
EVENT_ERROR = "error"


@dataclass(frozen=True)
class AgentEvent:
    """单条事件：type 是上面的常量之一；payload 必须是 JSON 可序列化 dict。"""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type, "payload": self.payload, "ts": self.ts}


class EventSink(Protocol):
    """事件出口协议：UI / 日志 / 回放各自实现。"""

    def emit(self, event: AgentEvent) -> None: ...


class ListSink:
    """收集到列表：测试断言与 REPLAY 回放共用（保证顺序）。"""

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)


class PrintSink:
    """打印到 stdout：D2 阶段 CLI 演示用。"""

    def emit(self, event: AgentEvent) -> None:
        print(f"[{event.type}] {event.payload}")
