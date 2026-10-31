"""工具层统一出口：按 PROVIDER_MODE 返回对应实现（PLAN D-5）。

为什么按模式缓存单例（评审 P0-1）：图每轮 retrieve 都会调 get_provider()，
如果每次新建实例——MCP 模式下每轮拉起一个 MCP Server 子进程且无人回收
（_McpSession 的后台 loop 线程常驻，旧实例被 GC 也带不走它，一次 3 轮研究
漏 3 个子进程，D7 的 Server B 带 torch/chroma 会直接把 8G 内存打爆）；
direct 模式下熔断计数随实例清零，"连续失败熔断"跨轮永远不生效。
单例让子进程与熔断器都活到进程级生命周期。
"""
from __future__ import annotations

import contextlib
import threading

from app.config import get_settings
from app.tools.base import Evidence, ToolProvider
from app.tools.fake import FakeProvider

_KNOWN_MODES = ("fake", "direct", "mcp")
_providers: dict[str, ToolProvider] = {}
_provider_lock = threading.Lock()


def get_provider() -> ToolProvider:
    mode = get_settings().provider_mode
    if mode not in _KNOWN_MODES:
        # 未知模式必须在缓存之前就报错：错的配置不能被单例"焊死"
        raise ValueError(f"未知 PROVIDER_MODE：{mode}（可选 fake | direct | mcp）")
    with _provider_lock:  # 加锁单飞：与 llm.py 同款，防并发首调重复建实例
        provider = _providers.get(mode)
        if provider is None:
            provider = _build_provider(mode)
            _providers[mode] = provider
        return provider


def _build_provider(mode: str) -> ToolProvider:
    if mode == "fake":
        return FakeProvider()
    if mode == "direct":
        from app.tools.direct import DirectProvider

        return DirectProvider()
    from app.tools.mcp_provider import MCPProvider

    return MCPProvider()


def reset_provider_cache() -> None:
    """清空单例缓存（有 close 的先收尾）。

    单例的代价：改了 mode 相关配置（如 MCP_WEB_CMD）不重建就看不到。
    测试里 monkeypatch 环境变量后必须手动调它——与 get_settings.cache_clear
    同一条纪律，做不到自动失效就把它写进约定。
    """
    with _provider_lock:
        while _providers:
            _, provider = _providers.popitem()
            closer = getattr(provider, "close", None)
            if closer is not None:
                # 收尾失败不阻断清理循环，剩余实例照常关
                with contextlib.suppress(Exception):
                    closer()


__all__ = [
    "Evidence",
    "ToolProvider",
    "FakeProvider",
    "get_provider",
    "reset_provider_cache",
]
