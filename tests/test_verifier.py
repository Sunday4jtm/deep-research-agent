"""D8 verifier 测试：冲突启发式单测 + 追问回环真转（gap 第二轮重试成功）。

冲突检测是确定性启发式（数字口径比对），单测钉死四种形态：
单源无对照不报、数字相容不报、一方无数字不报、数字分歧必报。
回环测试证明 D8 前"attempted 去重把 gap 也跳过 → 回环空转"的问题已修。
"""
from __future__ import annotations

import pytest

from app.contracts import (
    EVENT_NODE_FINISHED,
    EVENT_NODE_STARTED,
    EVENT_WARNING,
    ListSink,
)
from app.graph.build import run_research
from app.graph.nodes import make_verify_node
from app.tools.base import Evidence


# ============================================================
# 冲突启发式（直接调 verify 节点，纯离线）
# ============================================================
def _state(kb_snippets: list[str], web_snippets: list[str], zero_evidence: str = "") -> dict:
    """构造 verifier 的输入 state：kb/web 证据各若干 + 可选零证据子问题。"""
    evidence: list[Evidence] = []
    subq_evidence: dict[str, list[str]] = {}
    for text in kb_snippets:
        eid = f"E{len(evidence) + 1}"
        evidence.append(
            Evidence(eid=eid, source_type="kb", title="k.md", locator="k.md#chunk0", snippet=text)
        )
        subq_evidence.setdefault("检索方案对比", []).append(eid)
    for text in web_snippets:
        eid = f"E{len(evidence) + 1}"
        evidence.append(
            Evidence(
                eid=eid, source_type="web", title="网页", locator="https://a.com", snippet=text
            )
        )
        subq_evidence.setdefault("检索方案对比", []).append(eid)
    if zero_evidence:
        subq_evidence[zero_evidence] = []
    return {"subq_evidence": subq_evidence, "evidence": evidence, "round": 1}


def test_数字分歧必须标记冲突():
    sink = ListSink()
    verify = make_verify_node(sink)

    result = verify(_state(kb_snippets=["BM25 准确率 90%"], web_snippets=["BM25 准确率 95%"]))

    assert len(result["conflicts"]) == 1
    conflict = result["conflicts"][0]
    assert conflict["query"] == "检索方案对比"
    # metric = 数值前 10 字窗口：含完整指标词，gold 对照（包含匹配）才命得住
    assert "准确率" in conflict["metric"]
    assert conflict["kb"] == "90%"
    assert conflict["web"] == "95%"
    # 每条冲突都有 warning 事件（UI 过程面板可见）
    warnings = [e for e in sink.events if e.type == EVENT_WARNING]
    assert len(warnings) == 1


def test_数字相容不报冲突():
    verify = make_verify_node(ListSink())

    result = verify(_state(kb_snippets=["准确率 95%"], web_snippets=["准确率 95%，延迟 20ms"]))

    assert result["conflicts"] == []


def test_一方无数字不报冲突():
    verify = make_verify_node(ListSink())

    result = verify(
        _state(kb_snippets=["BM25 是关键词检索"], web_snippets=["向量检索用余弦相似度"])
    )

    assert result["conflicts"] == []


def test_单源无对照不报冲突():
    verify = make_verify_node(ListSink())

    result = verify(_state(kb_snippets=["准确率 90%"], web_snippets=[]))

    assert result["conflicts"] == []


def test_验证事件序列与gaps计数():
    sink = ListSink()
    verify = make_verify_node(sink)

    result = verify(
        _state(
            kb_snippets=["准确率 90%"],
            web_snippets=["准确率 95%"],
            zero_evidence="零证据子问题",
        )
    )

    assert result["gaps"] == ["零证据子问题"]
    types = [e.type for e in sink.events]
    assert types[0] == EVENT_NODE_STARTED
    assert types[-1] == EVENT_NODE_FINISHED
    assert sink.events[-1].payload["gaps"] == 1
    assert sink.events[-1].payload["conflicts"] == 1


# ============================================================
# 追问回环真转：gap 第二轮重试成功（D8 前回环空转）
# ============================================================
class _RetryProvider:
    """第一轮对 gap 子问题返回空（模拟瞬时失败），第二轮起返回真实证据。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def web_search(self, query: str, k: int = 3) -> list[Evidence]:
        self.calls.append(query)
        if self.calls.count(query) == 1:
            return []
        return [
            Evidence(
                eid="",
                source_type="web",
                title="补检索命中",
                locator="https://retry.example.com/a",
                snippet="第二轮拿到了证据",
            )
        ]

    def web_fetch(self, url: str) -> str:
        raise NotImplementedError

    def kb_search(self, query: str, k: int = 3) -> list[Evidence]:
        return []

    def kb_status(self) -> dict:
        return {}


@pytest.fixture()
def retry_provider(monkeypatch: pytest.MonkeyPatch) -> _RetryProvider:
    import app.graph.nodes as nodes_mod

    provider = _RetryProvider()
    monkeypatch.setattr(nodes_mod, "get_provider", lambda: provider)
    return provider


def test_gap第二轮重试成功_回环真转(retry_provider: _RetryProvider):
    final = run_research("瞬时失败后会重试的问题", sink=ListSink())

    assert final["round"] == 2, "第一轮 gap → 第二轮重试"
    assert final["evidence"], "重试应当拿到证据"
    assert not final["gaps"], "重试成功后 gap 清零"
    # 同一子问题确实被查了两次（回环重试），且 attempted 去重没有误吞
    assert retry_provider.calls.count("瞬时失败后会重试的问题") == 2


def test_已有证据的子问题不重复取证(retry_provider: _RetryProvider):
    """首轮成功的子问题跨轮跳过：dedup 防的是重复成功的花费。
    RetryProvider 对首次查询必返空——若它第二轮还查成功证据之外的
    问题，round 会涨到熔断；这里断言 round==2 且证据来自重试，
    反证"有证据的子问题"没有被再查（否则第三轮还会跑）。"""
    final = run_research("瞬时失败后会重试的问题", sink=ListSink())

    assert final["round"] == 2
    assert final["evidence"]
