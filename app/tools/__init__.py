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
from collections.abc import Callable

from app.config import get_settings
from app.tools.base import Evidence, ToolProvider
from app.tools.fake import FakeProvider


def _build_direct() -> ToolProvider:
    from app.tools.direct import DirectProvider

    return DirectProvider()


def _build_mcp() -> ToolProvider:
    from app.tools.mcp_provider import MCPProvider

    return MCPProvider()


# 注册制工厂（评估 P3 项）：新增 provider（如未来 Tavily）落一行登记而不是改
# if/elif 链——"插件化"从口号变成一行注册。依赖重的实现（mcp/direct）用惰性
# 包装：SDK 缺失只在真用到该模式时才暴露，fake 路径（CI/离线）永不 import 它们
_PROVIDER_FACTORIES: dict[str, Callable[[], ToolProvider]] = {
    "fake": FakeProvider,
    "direct": _build_direct,
    "mcp": _build_mcp,
}

_providers: dict[str, ToolProvider] = {}
_provider_lock = threading.Lock()


def get_provider() -> ToolProvider:
    mode = get_settings().provider_mode
    factory = _PROVIDER_FACTORIES.get(mode)
    if factory is None:
        # 未知模式必须在缓存之前就报错：错的配置不能被单例"焊死"
        raise ValueError(
            f"未知 PROVIDER_MODE：{mode}（可选 {' | '.join(_PROVIDER_FACTORIES)}）"
        )
    with _provider_lock:  # 加锁单飞：与 llm.py 同款，防并发首调重复建实例
        provider = _providers.get(mode)
        if provider is None:
            provider = factory()
            _providers[mode] = provider
        return provider


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
