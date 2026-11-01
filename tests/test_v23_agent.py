"""v2.3 Agent 化升级的离线测试：反思回路 / 跨 run 记忆 / 上下文压缩 / 思考链事件。

全部不碰网络：反思用假 invoke（JSON 剧本），压缩用假 chat，记忆写 tmp 路径。
"""
from __future__ import annotations

import json

import pytest

from app import memory as app_memory
from app.contracts import EVENT_THOUGHT, ListSink
from app.graph.nodes import make_verify_node


# ============================================================
# 1. 跨 run 记忆
# ============================================================
@pytest.fixture()
def mem_file(tmp_path: pytest.Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_memory, "RUNS_FILE", tmp_path / "runs.jsonl")
    monkeypatch.setenv("MEMORY_ENABLED", "1")
    return tmp_path / "runs.jsonl"


def test_记忆沉淀后可召回(mem_file):
    app_memory.append_run("混合检索的原理", ["要点一", "要点二"], ["a.md#chunk0"], 800)

    hits = app_memory.recall("混合检索是什么")

    assert len(hits) == 1
    assert hits[0]["learnings"] == ["要点一", "要点二"]


def test_无关问题召回为空(mem_file):
    app_memory.append_run("混合检索的原理", ["要点"], [], 100)

    assert app_memory.recall("完全不相干的量子计算") == []


def test_记忆关闭时零读写(mem_file, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMORY_ENABLED", "")
    get_settings_clear()

    assert app_memory.append_run("q", ["l"], [], 1) is False
    assert app_memory.recall("q") == []
    assert not mem_file.exists()


def get_settings_clear():
    from app.config import get_settings

    get_settings.cache_clear()
    return None


# ============================================================
# 2. LLM 反思回路
# ============================================================
def _state_with_gap(learnings=None, rewritten=None) -> dict:
    return {
        "question": "混合检索解决了什么问题？",
        "subq_evidence": {
            "已覆盖子问题": ["E1"],
            "零证据子问题": [],
        },
        "evidence": [
            __import__("app.tools.base", fromlist=["Evidence"]).Evidence(
                eid="E1", source_type="kb", title="k.md",
                locator="k.md#chunk0", snippet="混合检索用 BM25 补词面匹配。",
            )
        ],
        "learnings": learnings or [],
        "rewritten": rewritten or {},
        "round": 1,
    }


def test_反思产出learnings与改写(monkeypatch: pytest.MonkeyPatch):

    fake_json = json.dumps(
        {
            "learnings": ["BM25 补足词面匹配"],
            "rewritten": [{"q": "零证据子问题", "new_query": "混合检索 词面匹配 失准"}],
        },
        ensure_ascii=False,
    )

    class _FakeResp:
        content = fake_json

    def fake_invoke(messages, **kwargs):
        return "fake", _FakeResp()

    monkeypatch.setattr("app.llm.invoke_with_fallback", fake_invoke)
    monkeypatch.setenv("REFLECT_MODE", "llm")
    from app.config import get_settings

    get_settings.cache_clear()
    sink = ListSink()
    verify = make_verify_node(sink)

    result = verify(_state_with_gap())

    get_settings.cache_clear()
    assert result["learnings"] == ["BM25 补足词面匹配"]
    assert result["rewritten"] == {"零证据子问题": "混合检索 词面匹配 失准"}
    thoughts = [e for e in sink.events if e.type == EVENT_THOUGHT]
    assert len(thoughts) == 1 and thoughts[0].payload["stage"] == "reflect"


def test_反思关闭时不调用LLM(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REFLECT_MODE", "off")
    from app.config import get_settings

    get_settings.cache_clear()

    def _boom(*a, **k):
        raise AssertionError("反思关闭时不得调用 LLM")

    monkeypatch.setattr("app.llm.invoke_with_fallback", _boom)
    sink = ListSink()
    verify = make_verify_node(sink)

    result = verify(_state_with_gap())

    assert result["learnings"] == [] and result["rewritten"] == {}
    get_settings.cache_clear()


# ============================================================
# 3. 上下文压缩
# ============================================================
def test_压缩把超预算证据变摘要(monkeypatch: pytest.MonkeyPatch):
    from app.synthesizer import synthesize_report
    from app.tools.base import Evidence

    # 预算压到 500：5 条各 ~320 字的证据必然超预算 → 走压缩路径
    monkeypatch.setenv("EVIDENCE_CHAR_BUDGET", "500")
    from app.config import get_settings as gs

    gs.cache_clear()
    big_evidence = [
        Evidence(f"E{i}", "web", f"来源{i}", f"https://e/{i}", "很长的证据内容 " * 40)
        for i in range(1, 6)
    ]
    state = {"question": "q", "evidence": big_evidence, "gaps": [], "errors": []}

    from langchain_core.messages import AIMessage

    sink = ListSink()
    calls = {"n": 0}

    class _FakeChat:
        # 有状态假模型：第 1 次被压缩调用拿到"要点A；要点B"，之后综合拿到正文
        def _reply(self):
            calls["n"] += 1
            return AIMessage(
                content="要点A；要点B" if calls["n"] == 1 else "报告正文 [E1]"
            )

        def invoke(self, messages, **kw):
            captured["prompts"] = captured.get("prompts", []) + [messages[-1][1]]
            return self._reply()

        def stream(self, messages, **kw):
            captured["prompts"] = captured.get("prompts", []) + [messages[-1][1]]
            return iter([self._reply()])

    captured: dict = {}
    monkeypatch.setattr("app.synthesizer._model_chain", lambda: [("fake", "u", "m", "k")])
    monkeypatch.setattr("app.synthesizer._build_chat", lambda *a, **k: _FakeChat())

    report, usage, citation = synthesize_report(state, sink=sink)

    all_prompts = "\n".join(captured["prompts"])
    assert "要点A" in all_prompts  # 压缩摘要进了综合 prompt
    assert "[被预算裁掉证据的压缩摘要" in all_prompts  # 明确禁止引用的标注在
    assert "报告正文 [E1]" in report  # 流式路径正常产出
    thoughts = [e for e in sink.events if e.type == EVENT_THOUGHT]
    assert any(th.payload["stage"] == "compress" for th in thoughts)  # 思考链可见
    gs.cache_clear()


def test_压缩关闭回退纯截断(monkeypatch: pytest.MonkeyPatch):
    from app.synthesizer import synthesize_report
    from app.tools.base import Evidence

    big_evidence = [
        Evidence(f"E{i}", "web", f"来源{i}", f"https://e/{i}", "内容 " * 300)
        for i in range(1, 6)
    ]
    state = {"question": "q", "evidence": big_evidence, "gaps": [], "errors": []}
    monkeypatch.setenv("EVIDENCE_CHAR_BUDGET", "500")
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("app.synthesizer._model_chain", lambda: [])

    report, usage, citation = synthesize_report(state, sink=ListSink())

    assert report  # 模板兜底/截断路径不挂
    get_settings.cache_clear()
