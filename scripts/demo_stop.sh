#!/bin/bash
# 演示一键停止：pidfile 精确杀本项目网关（pkill -f "uvicorn api.main" 会误杀
# 其他项目；"streamlit run" 会全机无差别杀，已按本项目路径收窄）。
# MCP Server 必须一起杀——单个 1.5~2GB（torch+chroma+bge），只杀网关的话
# 子进程靠 stdin EOF 兜底退出，慢且不可靠；8G 机器上孤儿进程堆积就是
# "内存爆了"的根因（2026-11-01 实测）。名字本项目独有，pkill 安全。
PIDFILE=/tmp/deep-research-agent-demo.pid
if [[ -f "$PIDFILE" ]]; then
  old=$(cat "$PIDFILE")
  if kill "$old" 2>/dev/null; then
    echo "[停止] 网关 (pid $old)"
  fi
  rm -f "$PIDFILE"
fi
pkill -f mcp_kb_server 2>/dev/null
pkill -f mcp_web_server 2>/dev/null
pkill -f "streamlit run frontend/app.py" 2>/dev/null
echo "[已停止] 网关 / MCP Server A+B / Streamlit"
