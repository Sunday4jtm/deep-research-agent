#!/bin/bash
# 演示一键停止：必须连 MCP Server 一起杀——它们单个 1.5~2GB（torch+chroma+bge），
# 只杀网关的话子进程靠 stdin EOF 兜底退出，慢且不可靠；8G 机器上孤儿进程
# 堆积就是"内存爆了"的根因（2026-11-01 实测）。
pkill -f "uvicorn api.main" 2>/dev/null
pkill -f mcp_kb_server 2>/dev/null
pkill -f mcp_web_server 2>/dev/null
pkill -f "streamlit run" 2>/dev/null
echo "[已停止] 网关 / MCP Server A+B / Streamlit"
