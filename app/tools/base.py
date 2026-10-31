"""ToolProvider 协议：检索工具的统一接口（PLAN v2.2 改进 #4）。

为什么要有这一层：MCP / 直连 / 假实现三套形态必须能互换，
否则 HYBRID_KB、PROVIDER_MODE 这些开关就是假的（评审原话：
"开关才是真的可切换，而不是两套代码"）。

Evidence 是贯穿全项目的证据单元：eid 编号就是最终报告里的引用编号来源。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlparse

SNIPPET_MAX_CHARS = 500  # 单条片段上限：上下文工程第一道裁剪（PLAN §3.2）


@dataclass(frozen=True)
class Evidence:
    eid: str          # E1 / E2 …（retriever 统一编号）
    source_type: str  # kb | web
    title: str
    locator: str      # URL（web）或 来源名（kb）
    snippet: str      # 已裁剪正文片段

    def __post_init__(self) -> None:
        if len(self.snippet) > SNIPPET_MAX_CHARS:
            # 用 object.__setattr__ 因为 frozen；截断必须发生在进入 State 之前
            object.__setattr__(self, "snippet", self.snippet[:SNIPPET_MAX_CHARS])


# 常见分享追踪参数：对"是不是同一篇文章"没有信息量，去重比对前剥掉
_TRACKING_KEYS = {"spm", "from", "igshid", "fr", "vd_source", "share_token", "share_source"}


def canonicalize_locator(locator: str) -> str:
    """证据来源的规范化键（仅供去重比对，不改写 Evidence.locator 展示值）。

    web URL：scheme 统一 https、host 小写去 www.、去 fragment、去
    utm_*/分享追踪参数、去尾斜杠——同一篇文章加 utm、换 http(s) 不该
    被当成两份证据（评审 P2-11，D7 双源后撞车面变大）。
    kb 的 locator（"文件名#chunkN"）不是 URL：原样返回。
    局限（如实）：m. 移动站域名不折叠（可能内容确实不同）；端口不参与比对。
    """
    try:
        parts = urlparse(locator)
    except ValueError:
        return locator
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return locator
    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    query = "&".join(
        f"{k}={v}"
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_KEYS and not k.lower().startswith("utm_")
    )
    path = parts.path.rstrip("/") or "/"
    canonical = f"https://{host}{path}"
    if query:
        canonical += f"?{query}"
    return canonical


@runtime_checkable
class ToolProvider(Protocol):
    """三个检索动作 + 一个状态查询。全部只读（最小权限原则，见 PLAN §7.1）。"""

    def web_search(self, query: str, k: int = 3) -> list[Evidence]: ...

    def web_fetch(self, url: str) -> str: ...

    def kb_search(self, query: str, k: int = 3) -> list[Evidence]: ...

    def kb_status(self) -> dict: ...
