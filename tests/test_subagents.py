"""Subagents 检索模式离线测试（v2.5）：假模型剧本工具调用，全程不碰网络。

钉死五件事：子 Agent 真的执行工具且证据重编为全局 E#、步数熔断仍带回产出、
LLM 全败降为缺口（gap 回环兜底）、无 chat Key 自动回落 parallel（Agent 化
不成为新故障点）、fetch 深读覆盖同源片段（全局去重不丢全文）。
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from app.contracts import EVENT_THOUGHT, EVENT_WARNING, ListSink
from app.graph.nodes import make_retrieve_node
from app.tools.base import Evidence


class _ScriptedProvider:
    """按调用序返回不同来源（locator 互不相同，供跨子问题去重断言）。"""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self._n = 0

    def _loc(self) -> str:
        self._n += 1
        return f"https://s.example/w{self._n}"

    def web_search(self, query: str, k: int = 4):
        locator = self._loc()
        self.queries.append("web:" + query)
        return [
            Evidence("", "web", f"网页·{query[:10]}", locator, f"{query} 的检索摘要。")
        ]

    def kb_search(self, query: str, k: int = 4):
        self._loc()  # 推进序号：kb locator 的 chunk 号与 web 来源互不撞车
        self.queries.append("kb:" + query)
        return [
            Evidence(
                "", "kb", f"笔记·{query[:10]}", f"notes.md#chunk{self._n}",
                f"{query} 的知识库片段。",
            )
        ]

    def web_fetch(self, url: str):
        return f"{url} 的全文正文内容，章节要点与数据表俱全。" * 5

    def kb_status(self) -> dict:
        return {}


class _ScriptedChat:
    """剧本步骤队列（bind_tools 返回自身）；子 Agent 顺序执行时按序消费。"""

    def __init__(self, steps: list) -> None:
        self._steps = list(steps)

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kw):
        return self._steps.pop(0)


def _tool_call_msg(name: str, args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _state(sub_questions: list[str]) -> dict:
    return {
        "question": "研究问题",
        "focus": "",
        "sub_questions": sub_questions,
        "attempted_queries": [],
        "subq_evidence": {},
        "rewritten": {},
        "evidence": [],
        "errors": [],
        "round": 0,
        "token_usage": {},
    }


@pytest.fixture()
def sub_env(monkeypatch: pytest.MonkeyPatch):
    """钉 subagents 模式 + 顺序执行（剧本确定性）；返回 install(步骤)。"""
    import app.graph.nodes as nodes_mod
    import app.subagents as sub_mod
    from app.config import get_settings

    monkeypatch.setenv("RETRIEVAL_MODE", "subagents")
    monkeypatch.setenv("SUBAGENT_MAX_STEPS", "4")
    # 顺序执行让剧本步骤可预测：并行只是"等"的方式，消费顺序本就按 eligible 序
    monkeypatch.setattr(nodes_mod, "_FETCH_WORKERS", 1)
    provider = _ScriptedProvider()

    def install(steps: list):
        chat = _ScriptedChat(steps)
        monkeypatch.setattr(sub_mod, "_model_chain", lambda: [("fake", "u", "m", "k")])
        monkeypatch.setattr(sub_mod, "_build_chat", lambda *a, **k: chat)
        monkeypatch.setattr(nodes_mod, "get_provider", lambda: provider)
        return chat

    get_settings.cache_clear()
    yield install
    get_settings.cache_clear()  # 开关态绝不留在缓存里毒害后续测试


def test_双子问题各自子Agent且证据全局重编(sub_env):
    sub_env(
        [
            _tool_call_msg("web_search", {"query": "A 的查询"}, "c1"),
            AIMessage(content="A 的要点。"),
            _tool_call_msg("kb_search", {"query": "B 的查询"}, "c2"),
            AIMessage(content="B 的要点。"),
        ]
    )
    sink = ListSink()
    final = make_retrieve_node(sink)(_state(["子问题A", "子问题B"]))

    assert [ev.eid for ev in final["evidence"]] == ["E1", "E2"], "局册编号重编为全局 E#"
    assert final["subq_evidence"] == {"子问题A": ["E1"], "子问题B": ["E2"]}
    assert final["round"] == 1
    acts = [
        e for e in sink.events if e.type == EVENT_THOUGHT and e.payload.get("stage") == "subagent"
    ]
    assert acts and "web_search" in acts[0].payload["text"], "检索决策上屏"
    assert final["token_usage"]["retriever"] >= 1, "子 Agent token 入账"


def test_步数熔断仍带回已获证据(sub_env):
    sub_env(_tool_call_msg("web_search", {"query": f"q{i}"}, f"c{i}") for i in range(1, 10))
    sink = ListSink()
    final = make_retrieve_node(sink)(_state(["唯一子问题"]))

    # SUBAGENT_MAX_STEPS=4：熔断但 4 步的工具产出已入册（查询互异→4 条证据）
    assert len(final["evidence"]) == 4
    assert final["round"] == 1
    assert final["errors"] == [], "步数熔断不是错误：证据照常入册"


def test_子Agent全败降为缺口不炸(sub_env, monkeypatch: pytest.MonkeyPatch):
    import app.subagents as sub_mod

    class _Dead:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages, **kw):
            raise RuntimeError("限流")

    sub_env([])
    monkeypatch.setattr(sub_mod, "_build_chat", lambda *a, **k: _Dead())

    sink = ListSink()
    final = make_retrieve_node(sink)(_state(["子问题A"]))

    assert final["subq_evidence"]["子问题A"] == [], "失败子 Agent 留缺口给 gap 回环"
    assert any(e.startswith("subagent/") for e in final["errors"])
    warns = [
        e for e in sink.events if e.type == EVENT_WARNING and e.payload.get("where") == "subagent"
    ]
    assert warns, "子 Agent 失败必须发 warning"


def test_无chatKey自动回落parallel(sub_env, monkeypatch: pytest.MonkeyPatch):
    import app.subagents as sub_mod

    sub_env([])
    monkeypatch.setattr(sub_mod, "_model_chain", lambda: [])

    sink = ListSink()
    final = make_retrieve_node(sink)(_state(["子问题A"]))

    # parallel 两路照常：web 先 kb 后
    assert [ev.eid for ev in final["evidence"]] == ["E1", "E2"]
    assert final["errors"] == []
    fallback = [
        e for e in sink.events if e.type == EVENT_WARNING and e.payload.get("where") == "subagents"
    ]
    assert fallback, "回落必须可见（静默降级=排障盲区）"


def test_fetch深读覆盖同源片段(sub_env):
    sub_env(
        [
            _tool_call_msg("web_search", {"query": "目标查询"}, "c1"),
            _tool_call_msg("web_fetch", {"url": "https://s.example/w1"}, "c2"),
            AIMessage(content="要点。"),
        ]
    )
    sink = ListSink()
    final = make_retrieve_node(sink)(_state(["子问题A"]))

    assert len(final["evidence"]) == 1, "fetch 与检索摘要同源：就地替换不重复入册"
    ev = final["evidence"][0]
    assert "全文正文内容" in ev.snippet and "检索摘要" not in ev.snippet, "深读全文覆盖片段"
