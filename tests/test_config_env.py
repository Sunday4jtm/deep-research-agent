"""env 数值解析容错（评审 P3-16）：配置手滑不该让全站起不来。"""
from __future__ import annotations

import pytest

from app.config import get_settings


def test_非数字env回退默认值不崩(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_ROUNDS", "3a")
    monkeypatch.setenv("BREADTH", "")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.max_rounds == 3
        assert settings.breadth == 3, "空串也按默认值处理"
    finally:
        get_settings.cache_clear()  # 坏值绝不能留在缓存里毒害后续测试


def test_agent_max_steps_手滑不崩(monkeypatch: pytest.MonkeyPatch) -> None:
    """裸 int() 曾是 P3-16 修复的漏网点：AGENT_MAX_STEPS=1a0 全站起不来。"""
    monkeypatch.setenv("AGENT_MAX_STEPS", "1a0")
    get_settings.cache_clear()
    try:
        assert get_settings().agent_max_steps == 10
    finally:
        get_settings.cache_clear()


def test_布尔开关大写也生效(monkeypatch: pytest.MonkeyPatch) -> None:
    """大写 True/TRUE 此前静默视为关闭——「配置了但没生效」经典事故。"""
    monkeypatch.setenv("LANGFUSE_ENABLED", "TRUE")
    get_settings.cache_clear()
    try:
        assert get_settings().tracing_enabled is True
    finally:
        get_settings.cache_clear()
