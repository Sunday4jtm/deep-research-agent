"""Spike① 服务端：最小 MCP stdio Server（FastMCP），只暴露一个 echo 工具。

目的：验证 mcp SDK + langchain-mcp-adapters 的版本兼容性（P0 的两个
hello-world 之一）。跑通后此文件保留作为 MCPProvider（D6）的写法参照。
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("spike")


@mcp.tool()
def echo(text: str) -> str:
    """原样返回输入文本。Spike 验证用。"""
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run()  # 默认 stdio 传输——与未来 MCP Server A/B 同款形态
