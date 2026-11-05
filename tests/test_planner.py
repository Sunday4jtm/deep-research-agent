"""planner 离线测试：降级链 ②③ 与输出清洗逻辑，全程不碰网络。"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.planner import estimate_tokens, extract_json, plan_subquestions, template_plan


def test_extract_json_object() -> None:
    assert extract_json('{"sub_questions": ["什么是 bge", "m3e 的短板"]}') == [
        "什么是 bge",
        "m3e 的短板",
    ]


def test_extract_json_with_prose_and_numbering() -> None:
    """模型最爱干的事：包一层客套话 + 给条目加编号——都必须能抢救。"""
    text = '好的，拆解如下：{"sub_questions": ["1. bge 的定位", "2. m3e 对比"]} 以上。'
    assert extract_json(text) == ["bge 的定位", "m3e 对比"]


def test_extract_json_bare_list() -> None:
    assert extract_json('["x", "y"]') == ["x", "y"]


def test_extract_json_garbage_returns_none() -> None:
    assert extract_json("模型开始胡说，没有任何结构。") is None


def test_extract_json_dedupes() -> None:
    assert extract_json('{"sub_questions": ["a", "a", "b"]}') == ["a", "b"]


def test_template_plan_strips() -> None:
    assert template_plan("  问题  ") == ["问题"]


def test_estimate_tokens_always_positive() -> None:
    assert estimate_tokens("") >= 1


def test_plan_falls_back_to_template_without_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一个 Key 都没有时也必须能出结果——③级兜底是硬保证。"""
    from app.llm import _chat_cache_clear

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        subs, usage = plan_subquestions("研究问题", "")
        assert subs == ["研究问题"]
        assert usage["planner"] >= 1
    finally:
        get_settings.cache_clear()
        _chat_cache_clear()
