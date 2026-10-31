"""D2 图骨架测试：全部离线，验证编排结构而非 LLM 行为。"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.contracts import (
    EVENT_DONE,
    EVENT_EVIDENCE_ADDED,
    EVENT_RESEARCH_STARTED,
    EVENT_ROUND_STARTED,
    ListSink,
)
from app.graph.build import run_research


def test_end_to_end_happy_path() -> None:
    """正常问题：出证据、出报告、事件序列完整。"""
    sink = ListSink()
    final = run_research("bge 和 m3e 怎么选", sink=sink)

    assert final["evidence"], "应当取证到至少一条证据"
    assert "[E" in final["report"], "报告应包含引用编号"
    assert final["round"] == 1, "正常路径只跑一轮"
    assert not final["gaps"]

    types = [e.type for e in sink.events]
    assert types[0] == EVENT_RESEARCH_STARTED
    assert types[-1] == EVENT_DONE
    assert EVENT_EVIDENCE_ADDED in types
    assert EVENT_ROUND_STARTED in types
    # 事件 payload 必须可 JSON 序列化（SSE 边界纪律）
    for event in sink.events:
        assert isinstance(event.payload, dict)


def test_gap_loop_with_fuse() -> None:
    """零证据子问题触发追问回环，直到熔断轮数后强制综合。"""
    sink = ListSink()
    settings = get_settings()
    final = run_research("EMPTY::这个查询必然零证据", sink=sink)

    assert final["round"] == settings.max_rounds, "应当跑满熔断轮数"
    assert final["gaps"], "gaps 应保留到报告"
    assert "未覆盖" in final["report"], "报告必须诚实披露未覆盖"
    # 同一查询不重复提交：attempted 只记一次
    assert final["attempted_queries"].count(final["attempted_queries"][0]) == 1


def test_citation_ids_sequential() -> None:
    """证据编号连续唯一——报告引用可溯源的前提。"""
    final = run_research("langgraph 状态机")
    eids = [ev.eid for ev in final["evidence"]]
    assert eids == [f"E{i + 1}" for i in range(len(eids))]


def test_cross_subq_url_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一 URL 只入册一次：D4 实调中两个子问题搜到同一篇文章（E8/E12）。"""
    from app.tools.base import Evidence

    class _DupProvider:
        def web_search(self, query: str, k: int = 3) -> list[Evidence]:
            return [
                Evidence("", "web", "先到的那篇", "https://same.example/x", "a"),
                Evidence("", "web", "重复的那篇", "https://same.example/x", "b"),
            ]

        def web_fetch(self, url: str) -> str:
            return ""

        def kb_search(self, query: str, k: int = 3) -> list[Evidence]:
            return []

        def kb_status(self) -> dict:
            return {}

    monkeypatch.setattr("app.graph.nodes.get_provider", lambda: _DupProvider())
    final = run_research("任意问题", sink=ListSink())
    assert len(final["evidence"]) == 1
    assert final["evidence"][0].title == "先到的那篇"


def test_research_started带provider_mode() -> None:
    """首事件带 provider_mode：UI 据此亮"离线演示"徽标（评审 P2-10）。"""
    sink = ListSink()
    run_research("langgraph 状态机", sink=sink)
    first = sink.events[0]
    assert first.type == EVENT_RESEARCH_STARTED
    assert first.payload.get("provider_mode") == "fake"  # conftest 强制
