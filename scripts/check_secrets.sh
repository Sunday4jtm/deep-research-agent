#!/usr/bin/env bash
# 提交前密钥扫描（继承老项目 pre-commit 的"密钥检测"纪律）。
# 只查 staged 文件；.env 本身在 .gitignore 里，不会出现在 staged 列表。
# 命中即退出 1，阻止提交——误报时可用 GIT_ALLOW_SECRET=1 临时放行并人工确认。
set -euo pipefail

if [[ "${GIT_ALLOW_SECRET:-0}" == "1" ]]; then
  echo "（GIT_ALLOW_SECRET=1：本次跳过密钥扫描——请确认你真的知道自己在做什么）"
  exit 0
fi

files=$(git diff --cached --name-only --diff-filter=ACM)
[[ -z "$files" ]] && exit 0

pattern='sk-[A-Za-z0-9]{16,}|[a-f0-9]{32,}|[A-Za-z0-9]{28}\.[A-Za-z0-9]{8}'
hits=$(echo "$files" | xargs grep -InE "$pattern" 2>/dev/null || true)
if [[ -n "$hits" ]]; then
  echo "❌ 疑似密钥泄漏，提交被阻止："
  echo "$hits"
  exit 1
fi
echo "✓ 密钥扫描通过"
