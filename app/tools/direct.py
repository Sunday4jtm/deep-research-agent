"""DirectProvider：不经 MCP 的直连实现（D4/D6 分两步填）。

为什么先留桩：PLAN D-4 的顺序是"先验证业务逻辑，再验证协议形态"。
D4 实现 web 直连（走 DeepSeek /v1/tools/search+fetch，与老仓库
web_research.py 口径一致）；D6 实现 kb 直连（走老仓库新增的
POST /api/kb/search）。在实现前就明确"不可用"，而不是静默返回空。
"""
from __future__ import annotations

from app.tools.base import Evidence


class DirectProvider:
    def web_search(self, query: str, k: int = 3) -> list[Evidence]:
        raise NotImplementedError(
            "D4 实现：DeepSeek /v1/tools/search 直连（口径对齐老仓库 web_research.py）"
        )

    def web_fetch(self, url: str) -> str:
        raise NotImplementedError("D4 实现：DeepSeek /v1/tools/fetch 直连（保留出站 URL 字面校验）")

    def kb_search(self, query: str, k: int = 3) -> list[Evidence]:
        raise NotImplementedError("D6 实现：老仓库 POST /api/kb/search（带 X-API-Token）")

    def kb_status(self) -> dict:
        raise NotImplementedError("D6 实现：老仓库 GET /api/kb/status")
