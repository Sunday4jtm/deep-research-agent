"""FastAPI SSE 网关的离线测试：桩掉 run_research，验证协议而非模型。

覆盖四件事：
  1. SSE 帧就是契约 JSON（type/payload/ts），顺序与研究事件一致；
  2. done 必须是最后一个事件（契约红线），流随后自然结束；
  3. 研究线程崩溃 → 客户端收到 error 事件收尾，绝不静默断流；
  4. 空问题 400。

TestClient 走 ASGI 传输，不碰真实 socket，不违反离线红线。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.contracts import (
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_REPORT_DELTA,
    EVENT_RESEARCH_STARTED,
    AgentEvent,
)


@pytest.fixture()
def gateway(monkeypatch: pytest.MonkeyPatch):
    """(TestClient, install) 二元组：install 换上可编程的假 run_research，
    往 sink 里吐脚本化事件——测的是 SSE 协议层，不是模型行为。"""
    import api.main as api_main

    def install(script: list[AgentEvent] | Exception) -> None:
        def fake_research(question: str, focus: str = "", sink=None) -> dict:
            sink.emit(AgentEvent(EVENT_RESEARCH_STARTED, {"question": question}))
            if isinstance(script, Exception):
                # 真实 run_research 的契约：崩前自己发 error 事件再 raise（build.py 兜底）
                sink.emit(
                    AgentEvent(
                        EVENT_ERROR,
                        {"code": "graph_failed", "msg": f"{type(script).__name__}: {script}"},
                    )
                )
                raise script
            for event in script:
                sink.emit(event)
            return {}

        monkeypatch.setattr(api_main, "run_research", fake_research)

    return TestClient(api_main.app), install


def _read_sse(client: TestClient, question: str = "测试问题") -> list[dict]:
    with client.stream("POST", "/api/research", json={"question": question}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = []
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))
    return events


def test_sse帧就是契约JSON且done收尾(gateway):
    client, install = gateway
    script = [
        AgentEvent(EVENT_REPORT_DELTA, {"text": "第一段"}),
        AgentEvent(EVENT_REPORT_DELTA, {"text": "第二段"}),
        AgentEvent(EVENT_DONE, {"citation_report": {"mapped": 3}}),
    ]
    install(script)

    events = _read_sse(client, "LangGraph 值得学吗")

    assert events[0]["type"] == EVENT_RESEARCH_STARTED
    assert [e["type"] for e in events[1:]] == [
        EVENT_REPORT_DELTA,
        EVENT_REPORT_DELTA,
        EVENT_DONE,
    ]
    assert events[1]["payload"] == {"text": "第一段"}
    assert isinstance(events[0]["ts"], float)  # 契约字段完整
    assert events[-1]["type"] == EVENT_DONE  # done 必须最后


def test_研究线程崩溃收到error收尾(gateway):
    client, install = gateway
    install(RuntimeError("LLM 炸了"))

    events = _read_sse(client)

    assert events[-1]["type"] == EVENT_ERROR
    assert "RuntimeError" in events[-1]["payload"]["msg"]  # 契约键=msg（build.py 口径）
    assert all(e["type"] != EVENT_DONE for e in events)  # 崩溃时不得伪造 done


def test_空问题返回400(gateway):
    client, _ = gateway
    resp = client.post("/api/research", json={"question": "   "})

    assert resp.status_code == 400


def test_health探活(gateway):
    client, _ = gateway
    resp = client.get("/api/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_静态前端挂在根路径不影响API(gateway):
    """单端口同时服务 UI 与 API：GET / 出研究台页面，/api/* 照常。"""
    client, _ = gateway
    r = client.get("/")
    assert r.status_code == 200
    assert "研知" in r.text
    assert client.get("/api/health").json() == {"status": "ok"}
