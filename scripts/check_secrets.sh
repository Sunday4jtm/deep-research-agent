#!/usr/bin/env bash
# 提交前密钥扫描（继承老项目 pre-commit 的"密钥检测"纪律）。
# 默认只查 staged 文件；--all 扫全部跟踪文件（CI 用——CI 上没有 staged 概念，
# 且旧的内联 grep 写法命中密钥反而退出 0，扫描永远绿灯形同虚设，评审 P0-2）。
# .env 本身在 .gitignore 里，两种模式都扫不到它。
# 命中即退出 1，阻止提交——误报时可用 GIT_ALLOW_SECRET=1 临时放行并人工确认。
set -euo pipefail

if [[ "${GIT_ALLOW_SECRET:-0}" == "1" ]]; then
  echo "（GIT_ALLOW_SECRET=1：本次跳过密钥扫描——请确认你真的知道自己在做什么）"
  exit 0
fi

if [[ "${1:-}" == "--all" ]]; then
  files=$(git ls-files)
else
  files=$(git diff --cached --name-only --diff-filter=ACM)
fi
[[ -z "$files" ]] && exit 0

# 模式盲区收口（评审 P2）：补 GitHub/AWS/PEM 形态；xargs 改 -0（按空白切
# 文件名会漏扫/误扫含空格路径）
pattern='sk-[A-Za-z0-9]{16,}|[a-f0-9]{32,}|[A-Za-z0-9]{28}\.[A-Za-z0-9]{8}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
hits=$(printf '%s\n' "$files" | tr '\n' '\0' | xargs -0 grep -InE "$pattern" 2>/dev/null || true)
if [[ -n "$hits" ]]; then
  echo "❌ 疑似密钥泄漏，提交被阻止："
  echo "$hits"
  exit 1
fi
echo "✓ 密钥扫描通过"
