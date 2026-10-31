"""ToolProvider 协议：检索工具的统一接口（PLAN v2.2 改进 #4）。

为什么要有这一层：MCP / 直连 / 假实现三套形态必须能互换，
否则 HYBRID_KB、PROVIDER_MODE 这些开关就是假的（评审原话：
"开关才是真的可切换，而不是两套代码"）。

Evidence 是贯穿全项目的证据单元：eid 编号就是最终报告里的引用编号来源。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

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


@runtime_checkable
class ToolProvider(Protocol):
    """三个检索动作 + 一个状态查询。全部只读（最小权限原则，见 PLAN §7.1）。"""

    def web_search(self, query: str, k: int = 3) -> list[Evidence]: ...

    def web_fetch(self, url: str) -> str: ...

    def kb_search(self, query: str, k: int = 3) -> list[Evidence]: ...

    def kb_status(self) -> dict: ...
