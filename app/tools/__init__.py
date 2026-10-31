"""工具层统一出口：按 PROVIDER_MODE 返回对应实现（PLAN D-5）。"""
from __future__ import annotations

from app.config import get_settings
from app.tools.base import Evidence, ToolProvider
from app.tools.fake import FakeProvider


def get_provider() -> ToolProvider:
    mode = get_settings().provider_mode
    if mode == "fake":
        return FakeProvider()
    if mode == "direct":
        from app.tools.direct import DirectProvider

        return DirectProvider()
    if mode == "mcp":
        # D6 接入：langchain-mcp-adapters 包装 MCP Server A/B，
        # 适配成同一个 ToolProvider 接口（app/tools/mcp_provider.py）
        raise NotImplementedError("D6 实现：MCPProvider（stdio 连接老仓库 mcp/ 下的两个 Server）")
    raise ValueError(f"未知 PROVIDER_MODE：{mode}（可选 fake | direct | mcp）")


__all__ = ["Evidence", "ToolProvider", "FakeProvider", "get_provider"]
