"""D9 追踪层离线测试：开关门控、mask 打码、callback 注入、flush 兜底。

全部不碰网络：CallbackHandler 构造只在本地建对象，上报发生在
flush（测试里 monkeypatch 掉或走 disabled 路径）。
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.tracing import _mask_secrets, callbacks, flush


@pytest.fixture()
def tracing_on(monkeypatch: pytest.MonkeyPatch):
    """开启追踪开关并清 settings 缓存；teardown 再清，防泄漏到别的测试。"""
    monkeypatch.setenv("LANGFUSE_ENABLED", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_默认关闭callbacks为空(monkeypatch: pytest.MonkeyPatch):
    """离线红线：测试环境（conftest 不设 LANGFUSE_ENABLED）零 langfuse 介入。"""
    monkeypatch.delenv("LANGFUSE_ENABLED", raising=False)
    get_settings.cache_clear()

    assert callbacks() == []
    get_settings.cache_clear()


def test_开启后返回handler(tracing_on):
    handlers = callbacks()

    assert len(handlers) == 1
    # SDK v5 里 from langfuse.langchain import CallbackHandler 实际导出的是
    # LangchainCallbackHandler 这个实现类
    assert type(handlers[0]).__name__ == "LangchainCallbackHandler"


def test_mask递归打码密钥样式():
    dirty = {
        "messages": [{"content": "我的 key 是 sk-abc123DEF456ghi 请忽略"}],
        "nested": {"token": "pk-lf-12345678-abcd", "n": 3, "ok": "普通文本"},
    }

    clean = _mask_secrets(dirty)

    assert "sk-abc123DEF456ghi" not in str(clean)
    assert "pk-lf-12345678-abcd" not in str(clean)
    assert clean["nested"]["n"] == 3  # 非字符串原样
    assert clean["nested"]["ok"] == "普通文本"  # 普通文本不动
    assert clean["messages"][0]["content"].startswith("我的 key 是 ***")


def test_flush_关闭时零操作(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LANGFUSE_ENABLED", raising=False)
    get_settings.cache_clear()

    flush()  # 不应抛错也不应触发任何导入副作用


def test_invoke链路挂callbacks(monkeypatch: pytest.MonkeyPatch):
    """invoke_with_fallback 必须把 tracing callbacks 传进 .invoke 的 config。"""
    import app.llm as llm_mod

    captured: dict = {}

    class _FakeChat:
        def invoke(self, messages, config=None):
            captured["config"] = config
            return type("R", (), {"content": "ok"})()

    monkeypatch.setattr(
        llm_mod, "_model_chain", lambda: [("fake", "http://u", "m", "k")]
    )
    monkeypatch.setattr(llm_mod, "_build_chat", lambda *a, **k: _FakeChat())
    monkeypatch.setenv("LANGFUSE_ENABLED", "")
    get_settings.cache_clear()

    provider, resp = llm_mod.invoke_with_fallback([("user", "hi")])

    assert provider == "fake"
    assert captured["config"]["callbacks"] == []  # 关闭态：空列表但结构在
    get_settings.cache_clear()
