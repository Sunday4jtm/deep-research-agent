"""locator 规范化与跨子问题去重（评审 P2-11）的离线测试。"""
from __future__ import annotations

import pytest

from app.contracts import ListSink
from app.graph.build import run_research
from app.tools.base import Evidence, canonicalize_locator


def test_utm参数不改变同一性() -> None:
    a = canonicalize_locator("https://example.com/a?utm_source=feed&id=1")
    b = canonicalize_locator("https://example.com/a?utm_source=app&id=1")
    assert a == b, "同一篇文章换 utm 渠道参数，去重必须认得出是同一篇"


def test_http与https及尾斜杠和www视为同源() -> None:
    assert canonicalize_locator("http://Example.com/a/") == canonicalize_locator(
        "https://www.example.com/a"
    )


def test_fragment与分享追踪参数被剥() -> None:
    assert "#" not in canonicalize_locator("https://example.com/a#section-2")
    assert "spm" not in canonicalize_locator("https://example.com/a?spm=xxx&x=1")
    assert "x=1" in canonicalize_locator("https://example.com/a?spm=xxx&x=1"), (
        "非追踪参数必须保留——剥多了会把不同页面误判成同一篇"
    )


def test_kb_locator原样返回() -> None:
    """kb 的 locator（文件名#chunkN）不是 URL：规范化必须原样放行。"""
    assert canonicalize_locator("笔记A.md#chunk3") == "笔记A.md#chunk3"


def test_非http协议原样返回() -> None:
    assert canonicalize_locator("ftp://example.com/x") == "ftp://example.com/x"


def test_图级去重_utm马甲不重复入册(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一次搜索里的 utm 马甲必须被规范化去重吞掉，真不同的 URL 照常入册。"""

    class _VainProvider:
        def web_search(self, query: str, k: int = 3) -> list[Evidence]:
            return [
                Evidence("", "web", "原文", "https://same.example/x", "a"),
                Evidence("", "web", "utm马甲", "https://same.example/x?utm_source=rss", "b"),
                Evidence("", "web", "真不同", "https://other.example/y", "c"),
            ]

        def web_fetch(self, url: str) -> str:
            return ""

        def kb_search(self, query: str, k: int = 3) -> list[Evidence]:
            return []

        def kb_status(self) -> dict:
            return {}

    monkeypatch.setattr("app.graph.nodes.get_provider", lambda: _VainProvider())
    final = run_research("任意问题", sink=ListSink())
    assert [ev.title for ev in final["evidence"]] == ["原文", "真不同"]
