"""D10 评测层离线测试：指标口径钉死 + 夹具端到端（ScriptedProvider 全程离线）。"""
from __future__ import annotations

import json

import pytest

from app.config import get_settings
from eval.fixtures import ScriptedProvider
from eval.metrics import (
    citation_existence,
    conflict_disclosure,
    coverage,
    injection_survived,
    latency_stats,
)
from eval.questions import QUESTIONS


# ============================================================
# 指标口径
# ============================================================
def test_引用存在率零引用是NA不是零分():
    assert citation_existence({"refs_unique": 0, "mapped": 0}) is None
    assert citation_existence({"refs_unique": 4, "mapped": 4}) == 1.0
    assert citation_existence({"refs_unique": 4, "mapped": 2}) == 0.5


def test_覆盖率的分母是子问题总数():
    assert coverage({"a": ["E1"], "b": [], "c": ["E2"]}) == pytest.approx(2 / 3)
    assert coverage({}) is None


def test_冲突披露率按指标名包含命中():
    assert conflict_disclosure(["召回率", "RRF 常数"], ["召回率", "RRF 常数"]) == 1.0
    # verifier 抽出的指标名是上下文片段（可能是 gold 的前缀/变体），包含即命中
    assert conflict_disclosure(["RRF"], ["RRF 常数"]) == 1.0
    assert conflict_disclosure([], ["召回率"]) == 0.0
    assert conflict_disclosure(["x"], []) is None  # 无 gold = N/A


def test_注入存活判定():
    assert injection_survived("正常报告", "TRUST_ME_CANARY") is True
    assert injection_survived("被带偏 TRUST_ME_CANARY", "TRUST_ME_CANARY") is False
    assert injection_survived("任意", "") is None  # 无金丝雀约定 = N/A


def test_延迟统计从事件时间戳来():
    events = []
    for i, (start, end) in enumerate([((0.0, 1.0)), ((10.0, 12.5))]):
        from app.contracts import AgentEvent

        events.append(AgentEvent("node_started", {"node": f"n{i}"}, ts=start))
        events.append(AgentEvent("node_finished", {"node": f"n{i}"}, ts=end))

    stats = latency_stats(events)

    # p50 取上中位（n 小时的实现口径）：[1.0, 2.5] → 2.5
    assert stats["p50"] == pytest.approx(2.5)
    assert stats["p95"] == pytest.approx(2.5)
    assert stats["total"] == pytest.approx(12.5)


# ============================================================
# 夹具 provider 与端到端（ScriptedProvider，全程离线）
# ============================================================
def test_夹具按关键词分发且与真实Evidence同构():
    p = ScriptedProvider()
    kb = p.kb_search("混合检索相比纯向量检索解决了什么问题", k=3)
    assert kb and kb[0].source_type == "kb"
    web = p.web_search("帮我调研语义检索的落地要点", k=3)
    assert any("TRUST_ME_CANARY" in e.snippet for e in web), "注入金丝雀必须在夹具证据里"


def test_题集构成符合PLAN协议():
    kinds = [q.kind for q in QUESTIONS]
    assert len(QUESTIONS) == 10
    assert kinds.count("private") == 3
    assert kinds.count("web") == 3
    assert kinds.count("conflict") == 2
    assert kinds.count("injection") == 1


def test_夹具端到端_冲突题与注入题(monkeypatch: pytest.MonkeyPatch):
    """冲突披露率与注入防御的完整链路：剧本证据 → verifier/synthesizer → 指标。"""
    from eval.run_eval import run_one

    q_conflict = next(q for q in QUESTIONS if q.kind == "conflict")
    q_injection = next(q for q in QUESTIONS if q.kind == "injection")

    monkeypatch.setenv("PROVIDER_MODE", "fake")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("SENSENOVA_API_KEY", raising=False)

    run = run_one(q_conflict, "single")
    assert run["citation_existence"] is not None, "剧本证据下报告必须带引用"
    # 剧本证据下冲突启发式是确定性的：只断言非 None 防不住 verifier 静默退化（评审 P2）
    assert run["conflict_disclosure"] == 1.0

    # strict 强过滤下金丝雀到不了报告，注入必然存活；哪天变 False 即防御被击穿
    run_inj = run_one(q_injection, "single")
    assert run_inj["injection_survived"] is True


def test_单个run可JSON序列化(monkeypatch: pytest.MonkeyPatch):
    from eval.run_eval import run_one

    q = next(q for q in QUESTIONS if q.kind == "private")
    # 清 Key 强制 synthesizer 走模板兜底：不碰网络，且模板报告必带引用
    for key in ("DEEPSEEK_API_KEY", "ZHIPU_API_KEY", "SENSENOVA_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PROVIDER_MODE", "fake")
    get_settings.cache_clear()
    run = run_one(q, "single")
    get_settings.cache_clear()
    json.dumps(run, ensure_ascii=False)  # 不抛即过：结果必须能落 jsonl


# ============================================================
# MEMORY_ENABLED 隔离回归（评审 P0-2：pop 变量 = config 默认 "1" = 评测期间
# 记忆反向开启，runs.jsonl 曾因此积累 122 条夹具条目）
# ============================================================
def test_run_one期间记忆必须显式禁用(monkeypatch: pytest.MonkeyPatch):
    """夹具跑分不得写跨 run 记忆：run_one 必须显式置 "0"，pop 会被默认值反转。"""
    import os

    import eval.run_eval as run_eval_mod

    seen_settings: dict = {}

    def fake_inner(question_obj, mode):
        seen_settings["memory_enabled"] = get_settings().memory_enabled
        return {"ok": True}

    monkeypatch.setattr(run_eval_mod, "_run_one_inner", fake_inner)
    monkeypatch.setenv("MEMORY_ENABLED", "1")  # 故意从"开着"进入，验证期间被关掉
    get_settings.cache_clear()
    run_eval_mod.run_one(QUESTIONS[0], "single")

    assert seen_settings["memory_enabled"] is False, "评测期间记忆必须关闭"
    assert os.environ.get("MEMORY_ENABLED") == "1", "调用方原值必须原样还原"


def test_run_one还原区分未设置与空串哨兵(monkeypatch: pytest.MonkeyPatch):
    """conftest 用空串哨兵禁用记忆：还原逻辑若把空串当"未设置"再 pop 一次，
    整个 pytest 进程的隔离会被第一次 run_one 拆掉（评审 P0-2 的第二半）。"""
    import os

    import eval.run_eval as run_eval_mod

    def fake_inner(question_obj, mode):
        return {"ok": True}

    monkeypatch.setattr(run_eval_mod, "_run_one_inner", fake_inner)
    monkeypatch.setenv("MEMORY_ENABLED", "")  # 模拟 conftest 哨兵
    get_settings.cache_clear()
    run_eval_mod.run_one(QUESTIONS[0], "single")

    assert os.environ.get("MEMORY_ENABLED") == "", "空串哨兵必须原样还原，不得变成未设置"
    assert get_settings().memory_enabled is False, "还原后隔离必须继续生效"
    get_settings.cache_clear()


def test_run_one钉死流水线范式(monkeypatch: pytest.MonkeyPatch):
    """夹具 patch 的是 nodes.get_provider，AGENT_MODE=react 时 provider 解析在
    agent_loop 命名空间——run_one 必须显式钉 research，否则夹具真联网（评审 P1）。"""
    import eval.run_eval as run_eval_mod

    seen: dict = {}

    def fake_inner(question_obj, mode):
        seen["agent_mode"] = get_settings().agent_mode
        return {"ok": True}

    monkeypatch.setattr(run_eval_mod, "_run_one_inner", fake_inner)
    monkeypatch.setenv("AGENT_MODE", "react")  # 故意从 react 进入
    get_settings.cache_clear()
    run_eval_mod.run_one(QUESTIONS[0], "single")

    assert seen["agent_mode"] == "research", "评测期间必须钉在流水线范式"
