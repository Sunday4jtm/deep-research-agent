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
