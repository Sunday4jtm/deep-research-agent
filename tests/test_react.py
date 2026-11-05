"""v2.4 ReAct 自主循环离线测试：假模型剧本工具调用，全程不碰网络。

钉死四件事：工具真的被执行（provider 收到查询）、证据自动编号入册、
思考链 act 事件上屏、步数熔断兜底。
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from app.agent_loop import run_react_research
from app.contracts import EVENT_THOUGHT, EVENT_WARNING, ListSink
from app.tools.base import Evidence


class _ScriptedProvider:
    """记录查询、返回固定证据；可注入失败剧本。"""

    def __init__(self, fail: bool = False) -> None:
        self.queries: list[str] = []
        self.fail = fail

    def web_search(self, query: str, k: int = 4):
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("搜索挂了")
        return [
            Evidence(
                eid="", source_type="web", title="混合检索综述",
                locator="https://s.example/hybrid",
                snippet="混合检索 = BM25 词面匹配 + 向量语义召回，互补提升召回。",
            )
        ]

    def kb_search(self, query: str, k: int = 4):
        self.queries.append("kb:" + query)
        return []

    def web_fetch(self, url: str):
        return "正文内容"


class _ScriptedChat:
    """第一轮返回工具调用，第二轮返回最终报告（bind_tools 返回自身）。"""

    def __init__(self, steps: list) -> None:
        self._steps = list(steps)
        self.bound = False

    def bind_tools(self, tools):
        self.bound = True
        return self

    def invoke(self, messages, **kw):
        return self._steps.pop(0)


@pytest.fixture()
def react_env(monkeypatch: pytest.MonkeyPatch):
    """patch provider / model chain / chat 构建；返回 (provider, install)。"""
    import app.agent_loop as loop_mod

    provider = _ScriptedProvider()

    def install(steps):
        chat = _ScriptedChat(steps)
        monkeypatch.setattr(loop_mod, "get_provider", lambda: provider)
        monkeypatch.setattr(loop_mod, "_model_chain", lambda: [("fake", "u", "m", "k")])
        monkeypatch.setattr(loop_mod, "_build_chat", lambda *a, **k: chat)
        return chat

    return provider, install


def _tool_call_msg(name: str, args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id}]
    )


def test_agent真的调用了工具并引用证据(react_env, monkeypatch: pytest.MonkeyPatch):
    provider, install = react_env
    monkeypatch.setenv("AGENT_MAX_STEPS", "6")
    from app.config import get_settings

    get_settings.cache_clear()
    install(
        [
            _tool_call_msg("web_search", {"query": "混合检索"}),
            AIMessage(content="## 核心结论\n混合检索互补提升召回 [E1]。"),
        ]
    )
    sink = ListSink()
    final = run_react_research("混合检索解决了什么问题？", "", sink=sink)

    assert provider.queries == ["混合检索"]  # 工具真的被执行
    assert len(final["evidence"]) == 1 and final["evidence"][0].eid == "E1"
    assert "[E1]" in final["report"]
    assert final["citation_report"]["mapped"] == 1  # 引用对账与流水线同口径
    acts = [e for e in sink.events if e.type == EVENT_THOUGHT and e.payload.get("stage") == "act"]
    assert acts and "web_search" in acts[0].payload["text"]  # 决策上屏
    get_settings.cache_clear()


def test_工具失败被观察并继续(react_env, monkeypatch: pytest.MonkeyPatch):
    provider, install = react_env
    provider.fail = True
    monkeypatch.setenv("AGENT_MAX_STEPS", "6")
    from app.config import get_settings

    get_settings.cache_clear()
    install(
        [
            _tool_call_msg("web_search", {"query": "q1"}, "c1"),
            AIMessage(content="## 未覆盖与局限\n工具全挂，无法得出结论。"),
        ]
    )
    sink = ListSink()
    final = run_react_research("任意问题", "", sink=sink)

    assert final["errors"], "工具失败应进 errors"
    assert "工具 web_search 失败" not in final["report"]
    get_settings.cache_clear()


def test_步数熔断兜底(react_env, monkeypatch: pytest.MonkeyPatch):
    """模型永远只要工具 → 到上限强制收尾，绝不无限循环。"""
    provider, install = react_env
    monkeypatch.setenv("AGENT_MAX_STEPS", "3")
    from app.config import get_settings

    get_settings.cache_clear()
    install(_tool_call_msg("web_search", {"query": "q"}, f"c{i}") for i in range(1, 10))
    sink = ListSink()
    final = run_react_research("永远要工具的问题", "", sink=sink)

    assert final["round"] == 3  # 熔断
    assert "未能产出报告" in final["report"]
    get_settings.cache_clear()


def test_主模型挂了自动降级备用(react_env, monkeypatch: pytest.MonkeyPatch):
    """此前只取 chain[0]：主模型限流时 ReAct 即死（评估 P1 项）——现在单步内逐个降级。"""
    provider, _install = react_env
    monkeypatch.setenv("AGENT_MAX_STEPS", "6")
    from app.config import get_settings

    get_settings.cache_clear()

    good = _ScriptedChat(
        [
            _tool_call_msg("web_search", {"query": "混合检索"}, "c1"),
            AIMessage(content="## 核心结论\n互补提升召回 [E1]。"),
        ]
    )

    class _Dead:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages, **kw):
            raise RuntimeError("限流")

    import app.agent_loop as loop_mod

    monkeypatch.setattr(loop_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(
        loop_mod, "_model_chain", lambda: [("bad", "u", "m", "k"), ("good", "u", "m", "k")]
    )
    monkeypatch.setattr(
        loop_mod, "_build_chat", lambda p, b, m, k, t: _Dead() if p == "bad" else good
    )

    sink = ListSink()
    final = run_react_research("混合检索解决了什么问题？", "", sink=sink)

    assert "[E1]" in final["report"]
    assert final["citation_report"]["mapped"] == 1
    failovers = [
        e
        for e in sink.events
        if e.type == EVENT_WARNING and e.payload.get("where") == "react/failover"
    ]
    assert failovers, "降级必须发 warning 事件让用户看见"
    get_settings.cache_clear()


def test_主备全挂抛错不假装成功(react_env, monkeypatch: pytest.MonkeyPatch):
    """全败才抛，与 invoke_with_fallback 同语义：绝不拿空报告假装成功。"""
    provider, _install = react_env
    monkeypatch.setenv("AGENT_MAX_STEPS", "6")
    from app.config import get_settings

    get_settings.cache_clear()

    import app.agent_loop as loop_mod

    class _Dead:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages, **kw):
            raise RuntimeError("限流")

    monkeypatch.setattr(loop_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(
        loop_mod, "_model_chain", lambda: [("bad", "u", "m", "k"), ("good", "u", "m", "k")]
    )
    monkeypatch.setattr(loop_mod, "_build_chat", lambda *a, **k: _Dead())

    with pytest.raises(RuntimeError, match="主备模型均失败"):
        run_react_research("任意问题", "", sink=ListSink())
    get_settings.cache_clear()
