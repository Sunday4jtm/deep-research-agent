# 研知 · Deep Research Agent —— 常用命令收敛（评估 P3 项：陌生人 clone 后 `make help` 即上手）
# 解释器钉绝对路径：conda run 在本机报 __conda_exe 权限拒绝（PLAN/记忆结论），
# 换机器改这一行即可；所有安装命令必须带镜像参数（AGENTS.md 红线）
PY := /opt/anaconda3/envs/agent-research/bin/python

.PHONY: help install demo stop test lint check preflight eval-fast

help: ## 列出可用命令
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-12s %s\n", $$1, $$2}'

install: ## 安装依赖（阿里云镜像；本机 pypi 直连超时）
	$(PY) -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

demo: ## 起演示（清场 → 网关+静态 UI 同端口 127.0.0.1:8000）
	bash scripts/demo_start.sh

stop: ## 停演示（连 MCP Server 孤儿进程一起杀，防内存堆积）
	bash scripts/demo_stop.sh

test: ## 离线测试全量（CI 同款口径）
	$(PY) -m pytest -m "not slow" -q

lint: ## ruff 静态检查
	$(PY) -m ruff check .

check: lint test ## 提交前三关：ruff + 离线测试 + 全仓密钥扫描
	bash scripts/check_secrets.sh --all

preflight: ## Provider 真机预检（chat/search/fetch，不打印密钥）
	$(PY) scripts/preflight.py

eval-fast: ## 快速评测基线（single×1 跳过裁判，夹具免费通道）
	$(PY) -m eval.run_eval --modes single --repeat 1 --skip-judge
