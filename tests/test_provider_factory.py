"""工具工厂单例纪律（评审 P0-1 修复的回归网）。

get_provider 的旧实现每次调用 new 一个实例：MCP 模式每轮 retrieve 拉起
新子进程且无人回收；direct 模式熔断计数随实例清零。这里守住"同模式
必须复用同一实例"和"坏配置不能进缓存"两条底线。
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.tools import get_provider, reset_provider_cache


def test_同模式返回同一实例():
    # conftest 已强制 PROVIDER_MODE=fake；两次调用必须拿到同一个对象
    assert get_provider() is get_provider()


def test_reset后重建新实例():
    p1 = get_provider()
    reset_provider_cache()
    assert get_provider() is not p1, "reset 的意义就是让改配置后的重建生效"


def test_未知模式报错且不污染缓存(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PROVIDER_MODE", "typo")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError):
            get_provider()
    finally:
        # typo 模式绝不能留在 settings 缓存里毒害后续测试
        get_settings.cache_clear()
