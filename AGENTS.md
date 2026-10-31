# AGENTS.md —— 研知 · Deep Research Agent 项目入口（给 AI 协作者和人）

> 拿到这个仓库先读这一份再动代码。完整计划见 [`PLAN.md`](PLAN.md)。

## 1. 这是什么

LangGraph 编排的多跳研究 Agent（面试作品）。私有知识库与公网搜索经 **MCP 协议**
接入为工具；Streamlit/FastAPI/Next.js 三端共用一份事件契约；全链路 Langfuse 追踪。
环境：conda env `agent-research`（Python 3.12）；依赖安装一律带清华镜像参数。

## 2. 三条最重要的约定（继承自姊妹项目《AI 学习监督员》）

1. **注释用中文，解释"为什么"而不是"做了什么"**——踩坑结论只写在注释里，禁止"顺手清理"。
2. **改完必须跑验证并如实汇报**：`ruff check .` + `pytest -m "not slow" -q`。
   失败项逐条列出；跑不起来也要说"没验成"，禁止选择性汇报。
3. **不做破坏性操作**：删除先报备；临时产物放 `assets/_check/`（已 gitignore）。

## 3. 硬红线

| 红线 | 为什么 |
|---|---|
| 密钥只进 `.env`（已 gitignore），代码里绝不出现；提交被 `scripts/check_secrets.sh` 拦截 | 面试官会翻 GitHub |
| 安装命令必须带镜像参数（`-i https://pypi.tuna.tsinghua.edu.cn/simple`） | 本机 pypi 直连超时 |
| 测试全离线：一律走 `FakeProvider`/Stub，禁止测试联网 | 不烧钱、CI 无需 secrets |
| **新仓库代码零 import 老仓库**（`../智谱项目`）；唯一连接 = MCP 协议 | 架构边界（PLAN D-2），破了叙事就垮 |
| `PROVIDER_MODE`/`WEB_PROVIDER` 等开关改动必须同步 `.env.example` 与 `PLAN.md` | 开关是评测对照与兜底的共同机制 |
| 事件契约（`app/contracts.py`，D2 起）改动必须三端同步 | 三套 UI 共用一份契约，漏一边=功能凭空消失（老项目同款教训） |
| LangGraph 只用 StateGraph/条件边/循环；不引 checkpoint/子图 | 2 周范围控制（PLAN 风险表） |

## 4. 代码地图

```
app/config.py        全局配置（.env 加载，离线安全）
app/llm.py           三模型链 + 回退（加锁单飞）+ token 粗估
app/contracts.py     事件契约 v1（三端共用，改动=三端同步）
app/planner.py       LLM 规划器（结构化输出三级降级）
app/synthesizer.py   LLM 综合器（流式报告 + 降级链）
app/citation.py      citation pass（引用对账，评测指标来源）
app/tools/base.py    ToolProvider 协议 + Evidence（引用编号来源）
app/tools/fake.py    确定性离线实现（CI/评测/陌生人复现）
app/tools/direct.py  直连实现（web+kb 已落地；kb=HTTP 直连老仓库 /api/kb/search）
app/tools/mcp_provider.py MCPProvider（stdio 连老仓库 MCP Server，D6=web、D7=kb）
app/tools/sanitize.py 清洗管道 + 注入特征检测
app/graph/state.py   ResearchState 全字段（PLAN §3.2）
app/graph/nodes.py   图节点工厂（planner/retriever/verifier/synthesizer）
app/graph/build.py   StateGraph 装配 + run_research 入口
frontend/app.py      Streamlit 界面（事件契约消费端，后台线程+队列轮询）
scripts/preflight.py Provider 真实预检（P0）
scripts/spike_*      P0 两个 hello-world spike（MCP/Langfuse）
tests/               全离线测试（conftest 带断网守卫）
.github/workflows/   GitHub Actions CI（离线）
```
