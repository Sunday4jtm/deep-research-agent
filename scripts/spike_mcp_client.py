"""Spike① 客户端：langchain-mcp-adapters 连接上面的 stdio Server。

验证三件事：① 能握手 ② 能发现工具 ③ 能直接调用工具（不经 LLM——
Spike 只验协议层，业务编排是 D2 的事）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


async def main() -> int:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(
        {
            "spike": {
                "command": sys.executable,
                "args": [str(ROOT / "scripts" / "spike_mcp_server.py")],
                "transport": "stdio",
            }
        }
    )
    tools = await client.get_tools()
    print(f"发现工具：{[t.name for t in tools]}")
    if not tools:
        print("FAIL：没有发现任何工具")
        return 1
    result = await tools[0].ainvoke({"text": "hello mcp"})
    print(f"调用结果：{result!r}")
    ok = "echo: hello mcp" in str(result)
    print("SPIKE_MCP_PASS" if ok else "SPIKE_MCP_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
