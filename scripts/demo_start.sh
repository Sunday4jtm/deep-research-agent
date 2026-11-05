#!/bin/bash
# 演示一键启动：先用 pidfile 清本项目的旧网关，再起网关。
# 为什么不用 pkill -f "uvicorn api.main"：那是极常见的模块布局，会误杀同机
# 其他项目的服务（评审 P2）；"streamlit run" 更是全机所有 Streamlit 无差别
# 击杀。本项目独有的 mcp_kb_server/mcp_web_server 两个名字保留 pkill 安全。
# 用法：bash scripts/demo_start.sh
cd "$(dirname "$0")/.."
PIDFILE=/tmp/deep-research-agent-demo.pid
if [[ -f "$PIDFILE" ]]; then
  old=$(cat "$PIDFILE")
  if kill "$old" 2>/dev/null; then
    echo "[清场] 旧网关 (pid $old)"
  fi
  rm -f "$PIDFILE"
fi
pkill -f mcp_kb_server 2>/dev/null
pkill -f mcp_web_server 2>/dev/null
sleep 2
echo "[启动] 网关（含 UI）：http://127.0.0.1:8000"
/opt/anaconda3/envs/agent-research/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000 &
echo $! > "$PIDFILE"
echo "[pid] 网关 pid=$(cat "$PIDFILE")（demo_stop 用）"
wait
