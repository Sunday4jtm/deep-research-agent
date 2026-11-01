#!/bin/bash
# 演示一键启动：先清场（防旧 MCP Server 孤儿进程叠加爆内存），再起网关
# 用法：bash scripts/demo_start.sh
cd "$(dirname "$0")/.."
pkill -f "uvicorn api.main" 2>/dev/null
pkill -f mcp_kb_server 2>/dev/null
pkill -f mcp_web_server 2>/dev/null
sleep 2
echo "[启动] 网关（含 UI）：http://127.0.0.1:8000"
/opt/anaconda3/envs/agent-research/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
