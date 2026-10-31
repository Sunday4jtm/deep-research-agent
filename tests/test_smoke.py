"""D1 冒烟测试：全部离线，验证脚手架的地基假设。"""
from __future__ import annotations

import pytest

from app import llm
from app.config import get_settings
from app.tools import get_provider
from app.tools.base import SNIPPET_MAX_CHARS


def test_fake_provider_deterministic() -> None:
    """同查询两次调用结果完全一致——评测可复现的底线。"""
    p = get_provider()
    first = p.web_search("bge 和 m3e 怎么选")
    second = p.web_search("bge 和 m3e 怎么选")
    assert first == second
    assert first and first[0].source_type == "web"
    assert "bge" in first[0].title.lower() or "bge" in first[0].snippet.lower()


def test_fake_kb_hit_and_fallback() -> None:
    p = get_provider()
    hit = p.kb_search("什么是过拟合")
    assert hit[0].source_type == "kb"
    miss = p.kb_search("完全不相关的查询词")
    assert miss and miss[0].source_type == "kb"  # 回退也是 kb 类型，不破坏下游假设


def test_evidence_snippet_always_bounded() -> None:
    """超长片段必须在 Evidence 构造时就被截断——上下文工程第一道闸。"""
    p = get_provider()
    for ev in p.web_search("bge") + p.kb_search("embedding"):
        assert len(ev.snippet) <= SNIPPET_MAX_CHARS


def test_config_offline_safe() -> None:
    """没有网络、只有部分 Key 时配置层也不能崩。"""
    s = get_settings()
    assert s.provider_mode == "fake"  # conftest 强制
    assert s.max_rounds >= 1 and s.breadth >= 1


def test_model_chain_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """三模型齐备时的优先级：DeepSeek 主力 → SenseNova 过渡 → 智谱兜底。"""
    from app.llm import _model_chain

    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")
    monkeypatch.setenv("SENSENOVA_API_KEY", "s")
    monkeypatch.setenv("ZHIPU_API_KEY", "z")
    get_settings.cache_clear()
    try:
        names = [provider for provider, *_ in _model_chain()]
        assert names == ["deepseek", "sensenova", "zhipu"]
    finally:
        get_settings.cache_clear()


def test_llm_chain_empty_raises_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """一个 Key 都没有时，必须在发请求之前就报人话错误。

    .env 里的 Key 在 import 时就被 load_dotenv 灌进 os.environ 了，
    所以这里要连环境变量一起清掉再清 lru_cache，才能模拟"全新空环境"。
    ⚠️ 新增 provider 时必须同步这个列表——D4 曾因漏了 SENSENOVA_API_KEY
    让"离线"测试真发起了网络调用（被 403 暴露），教训见 conftest 守卫。
    """
    for key_var in ("DEEPSEEK_API_KEY", "SENSENOVA_API_KEY", "ZHIPU_API_KEY"):
        monkeypatch.delenv(key_var, raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="未配置任何模型 Key"):
            llm.invoke_with_fallback([])
    finally:
        get_settings.cache_clear()  # 恢复缓存，别污染后续测试
