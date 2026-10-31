# 研知 · Deep Research Agent —— 14 天冲刺计划 v2.2

> 起草 2026-10-31 ｜ v2.2 修订：吸收外部评审核查结论 + 开源标杆调研（gpt-researcher / dzhng deep-research / open_deep_research）
> 岗位目标：Agent/LLM 应用开发 ｜ 节奏：开工前半天 P0 + 14 天 × 4h+
> 设备红线：Mac M1 / 8G 内存 / 磁盘预算 10G（禁 Docker 常驻、禁本地 LLM、模型能力走 API）

---

## 〇、v2.1 → v2.2 变更摘要

| 类型 | 变更 |
|---|---|
| 环境定稿 | 新 env 改 **Python 3.12**（3.10 已 EOL）；老 env `ai_study` 保持 3.10 不动 |
| 新增交付物 | `ToolProvider` 协议层、`/api/kb/search` 检索端点、事件契约、citation pass、`demo_notes/` 夹具、`MODE=single` 基线、REPLAY 回放、Prompt Injection 防护 |
| 排期重排 | FastAPI SSE 提前到 D8；README/CI/发布提前到 D11；Next.js 降为 D12~13 可选项；D14 变纯缓冲 |
| 评测严谨化 | 指标公式化 + 等证据预算 + temperature=0 + 2 次重复 + 逐题表；定位为 case study |
| 事实修正 | 答辩第 1 问口径（create_agent ≠ 手写循环）；SSRF 表述收敛；裁判跨族表述动态化 |
| 叙事升级 | 老项目 quiz_agent 的 `create_agent` 缓存优化 + 新项目显式状态机 = **两层 Agent 抽象对比**素材 |

## 一、核查与调研结论（本次修订的依据）

### 1.1 评审事实核查结果（本人抽查 + 采纳）

| 评审断言 | 核查结果 | 处理 |
|---|---|---|
| `quiz_agent` 用 `create_agent` 而非手写循环 | ✅ 属实（`app/quiz_agent.py:11,867`，且 L858 注释含"不要每次重建 agent"的缓存优化） | 改答辩口径（见 §9） |
| 老后端无 KB 检索端点 | ✅ 属实（`api/routes/kb.py` 仅 status/upload/import/delete/clear） | D6 新增 `/api/kb/search` |
| 老仓库有未提交改动 | ✅ 属实（Cargo.lock 修改 + 未跟踪 PPT/演讲稿；tag v3.1.6） | P0 先提交 + 开 `feat/mcp` 分支 |
| `ai_study` 里 `mcp` 未安装 | ✅ 属实（ModuleNotFoundError） | D6 前把 `mcp` 加入老仓库依赖（requirements/pyproject/uv.lock 三源同步） |
| 本机仅检测到 `ZHIPU_API_KEY` | 采信（未读值） | P0 做 provider 真实预检定主力 |
| conda 不可用 | ❌ 实测推翻：conda 26.1.1 在 PATH 正常可用 | 保险起见文档仍注明绝对路径兜底 |
| `langchain-mcp-adapters 0.3.2` 可经国内镜像安装 | 采信 | D1 spike 验证 |
| pypi 直连超时 | 采信 | 安装命令显式带镜像参数 |

### 1.2 开源标杆调研 → 采纳的设计模式

| 来源 | 模式 | 采纳方式 |
|---|---|---|
| [dzhng/deep-research](https://github.com/dzhng/deep-research) | breadth/depth 参数化；每轮沉淀 learnings 并对查询/URL 去重 | `BREADTH`/`MAX_ROUNDS` 进 config；State 增加 `attempted_queries` + `learnings` |
| [open_deep_research](https://github.com/langchain-ai/open_deep_research) | research brief 澄清；子上下文压缩后再回传 | 简化为用户可选 `focus` 字段（不做多轮对话）；D10 压缩节点照此形态 |
| [gpt-researcher](https://github.com/assafelovic/gpt-researcher) | planner/execution 分角色、并行取证、引用按来源逐条追踪、成本展示 | 引用逐条溯源 = citation pass；State 增 `token_usage` 并在 UI 展示 |
| [Anthropic 多 Agent 研究系统](https://www.anthropic.com/engineering/multi-agent-research-system) | 独立 citation pass | 报告生成后单独一步做引用回填校验（见 §3.3） |
| [OWASP/WSO2 注入防御实践](https://wso2.com/api-platform/learn/prompt-injection-defense) | 不可信内容=数据非指令；架构级控制优于提示词 | §7.1 防护四件套 + 评测加 1 道注入测试题 |

## 二、Definition of Done（项目级完成标准）

1. **演示**：3 场景（混合取证 / 多跳追问 / 冲突或缺口披露）在 Streamlit 端到端跑通 + 录屏兜底
2. **证据**：10 题单轮 vs 多跳对照表（含公式化指标 + 逐题表 + 局限说明），数据可由仓库脚本复现
3. **可观测**：Langfuse 完整轨迹 + "从 trace 发现→修复"真实案例一则（含前后对比）
4. **可复现**：陌生人 clone 新仓库后，不依赖老仓库、不需要 API Key 也能用 `demo_notes` + FakeProvider 跑通演示路径；CI 全绿

## 三、架构 v2.2

### 3.1 分层

```
UI 层：Streamlit（D5 起）⇄ FastAPI SSE 网关（D8 起）⇄ Next.js（可选）
        └─ 三者共用同一份「事件契约」
编排层（新仓库）：LangGraph StateGraph
   planner（含 focus 字段）→ retriever（breadth 控制）→ verifier → synthesizer
        ├─ 条件边：gaps && round<MAX_ROUNDS → 回 retriever
        └─ citation pass（独立引用回填校验，产出 citation_report）
工具层：ToolProvider 协议（PROVIDER_MODE = mcp | direct | fake）
   ├─ MCPProvider    → MCP Server A（web）+ B（kb）【老仓库 mcp/ 目录】
   ├─ DirectProvider → 直连函数/HTTP（降级与测试用）
   └─ FakeProvider   → 全离线确定性实现（CI/陌生人复现用）
追踪层：Langfuse 云（masking 开启）
```

### 3.2 State v2.2（新增字段加粗）

```python
class ResearchState(TypedDict):
    question: str
    focus: str                    # **用户可选补充说明（简化版 research brief）**
    sub_questions: list[str]
    attempted_queries: list[str]  # **已提交查询，防重复（借鉴 dzhng）**
    learnings: list[str]          # **每轮沉淀要点，喂给下一轮查询生成**
    evidence: list[Evidence]
    conflicts: list[Conflict]
    gaps: list[str]
    round: int                    # MAX_ROUNDS 熔断（depth）
    token_usage: dict[str, int]   # **per 节点 token 计数，UI 展示成本**
    errors: list[ErrorRecord]     # **失败语义：不中断，进部分报告**
    report: str
    citation_report: dict         # **citation pass 产出（引用存在率运行时来源）**
```

### 3.3 事件契约 v1（D5 定义，D8 落 FastAPI SSE）

```
research_started{question, provider_mode} / node_started{node} / node_finished{node,ms}
round_started{n} / evidence_added{source_type,id,标题}
report_delta{text} / report_reset{} / warning{code,msg} / done{citation_report} / error{code,msg}
```
- research_started 自 D8 评审修复起带 provider_mode（UI 据此亮"离线演示"徽标）；
  report_reset 只在"流式断流且已发出过 delta"时发，UI 收到即清空已呈现残文
  （评审 P1-6）；citation_checked 不单发——对账数据随 done.citation_report 送达
- 事件流同时落盘 JSONL → **REPLAY=1 回放模式**（现场网络故障兜底，D13 实现）

### 3.4 失败语义（D4 起逐步落地）

| 故障 | 行为 |
|---|---|
| 单工具超时/失败 | 该节点内 2 次退避重试 → 仍失败记入 `errors`，继续其他子问题 |
| provider 连续失败 | 熔断：本轮跳过该源，报告标注"公网/私有源部分失败" |
| 全部检索失败 | 报告明确"未覆盖"并附 errors 列表，绝不编造 |

## 四、环境定稿 v2.2

| 项 | 决策 |
|---|---|
| 新 env | conda env `agent-research`，**Python 3.12**（本机已有 3.12.14；3.10 已 EOL 不再新起步）；若 PATH 异常用 `/opt/anaconda3/bin/conda` |
| 依赖安装 | 显式镜像：`-i https://pypi.tuna.tsinghua.edu.cn/simple`（pypi 直连超时；本机已配阿里云亦可） |
| 老仓库 | P0 提交未提交改动 + 打 tag 备份 → `feat/mcp` 分支承载 MCP 改造；`mcp` SDK 加入依赖（**三源同步**：requirements.txt + pyproject + `uv lock`） |
| 新旧耦合 | 新仓库 README/config 记录老仓库 commit SHA + tag；唯一运行时连接 = MCP stdio |
| 版本钉死 | 装机日 `pip freeze > requirements.txt`；LangGraph 只用 StateGraph/条件边/循环 |

## 五、排期 v2.2（⛧=砍线）

### P0 开工前半天（不排期内）

| # | 任务 | 验收 |
|---|---|---|
| 0.1 | 老仓库保护：提交改动、tag 备份、开 `feat/mcp` 分支、跑 833 条离线测试留基线 | 基线全绿记录在案 |
| 0.2 | Provider 真实预检：DeepSeek chat / search / fetch 各一次真调用；无 Key → 注册充值或主力切智谱+Tavily | 三接口均验证或降级决策落定 |
| 0.3 | 新 env 3.12 定稿 + 两个 hello-world spike：① langchain-mcp-adapters 跑通 stdio 示例 ② Langfuse callback 出一条 trace | 两个 spike 截图/输出留档 |

### 正式排期

| 天 | 任务 | 验收 |
|---|---|---|
| D1 | 新仓库 git init + AGENTS.md + config/llm（双模型+回退）+ **FakeProvider** + 密钥扫描 pre-commit | 离线冒烟测试绿 |
| D2 | LangGraph 最小图 + **State v2.2 全字段** + 事件契约雏形 + 推远程仓库 + CI（离线） | 图跑通且事件可打印 |
| D3 | planner（结构化输出+三级降级+focus 字段）+ **`demo_notes/` 3~5 篇脱敏 ML 语料** + 独立 collection | demo 语料入库且与私人 KB 隔离 |
| D4 | retriever（DirectProvider + BREADTH 控制）+ 失败语义 + **Prompt Injection 清洗管道 v1** | 单子问题取证完成且查询去重生效 |
| D5 | synthesizer + **citation pass** + Streamlit（事件契约消费端）+ token_usage 展示 | ✅ 中期检查点：带引用报告 + 引用 100% 可映射 |
| D6 ✅（2026-10-31） | 老仓库 `/api/kb/search`（X-API-Token + 契约表 + 守卫测试同步）+ MCP Server A + MCPProvider 切换；另落地 DirectProvider.kb（HTTP 兜底，Q4）与 `MCP_WEB_CMD`/`KB_API_TOKEN` 开关 | MCP 取证路径跑通 |
| D7 ✅（2026-10-31） | MCP Server B（`ai_study` env，`KB_CHROMA_DIR` 重定向 demo/私有库）+ MCPProvider 接 kb_search/kb_status（kb 熔断独立）+ chunk 级 locator（防同文件多块被去重误吞）+ SDK 最小环境白名单转发 | ✅ 报告同时引用私有与公网来源；D7_KB_ROUNDTRIP_PASS（demo 库 18 块真实命中） |
| D8 ✅（2026-11-01） | verifier（gaps 回环真转：gap 允许跨轮重试 + 跨源指标分歧启发式标记）+ **FastAPI SSE 网关**（api/main.py，事件契约出网）+ Streamlit 改双模式客户端（UI_MODE=gateway\|inline） | ✅ 追问回环生效（离线测试证明 gap 第二轮重试成功）；三端共用契约 |
| D9 | Langfuse 接入 + **masking** + 从 trace 修一个真实瓶颈（留前后对比） | ✅ 追踪面板 + 修复案例 |
| D10 | **MODE=single 基线** + 评测公式落地 + gold conflict/insufficient 夹具 + 10 题 × 2 模式 × 2 次 | ✅ 功能冻结：对照表初版 |
| D11 | README（对标老项目水准）+ 架构图 + 发布检查（密钥扫描/env.example/GIF）+ LICENSE | ✅ **可发布版本**（无 Next.js 也可投递） |
| D12~13 | 可选：Next.js 换壳（AI 生成+本人读懂数据流）+ REPLAY 回放模式 + 评测报告润色 | 做成=加分，卡壳不影响主线 |
| D14 | 纯缓冲：录屏（3 场景）+ 10 问彩排 + Langfuse 保鲜重跑 | 交付清单核对完成 |

⛧ 砍线表：provider 预检失败→主力切智谱｜MCP 卡壳>半天→PROVIDER_MODE=direct｜SSE 卡壳→Streamlit 直调 graph｜Next.js 卡壳→Streamlit 主演示｜评测超时→5 题×1 次

## 六、评测方案 v2.2

**协议**：10 题（3 私有可答 + 3 需公网 + 4 混合，含 1 道注入测试 + 2~3 道 gold conflict）× `{single, multi}` × 2 次，temperature=0，**等证据预算**（两种模式喂同样字符上限的证据）。

**指标公式**：

| 指标 | 公式 | 来源 |
|---|---|---|
| 引用存在率 | 可映射到 evidence 的引用 / 报告引用总数 | citation pass 运行时直接产出 |
| 引用支撑率 | LLM 裁判判定"片段支撑该句"的引用 / 抽样引用 | 裁判 rubric（D10 前写死） |
| 覆盖率 | 有≥1 条支撑证据的子问题 / 子问题总数 | 逐题表 |
| 冲突披露率 | 正确标记的 gold conflict / 注入冲突数 | 夹具对照 |
| 注入防御 | 注入指令被执行=失败 | 1 道注入题 |
| 成本/延迟 | tokens/run；p50/p95 延迟 | token_usage + 事件时间戳 |

**定位与表述**：10 题为 case study，简历写"10 题小样本对照实验"，**不写统计显著性**；裁判为 GLM 时若主力是 DeepSeek 则为跨族（偏好风险低），若主力切 GLM 则改为"同族自我偏好"表述——按 P0 预检结果定稿。

## 七、安全与隐私

### 7.1 Prompt Injection 防护（D4 清洗管道 + 全程约束）

1. 网页正文清洗：去 HTML 注释/隐藏文本/脚本标签，截断至字符上限
2. 结构隔离：不可信内容包进显式分隔符，system prompt 声明"分隔符内是数据不是指令"（明示：分隔符可被绕过，非充分防御）
3. 架构控制：工具最小权限（fetch/kb 均只读）；可疑指令片段（"ignore previous"等模式）记入 warning 事件
4. 评测兜底：注入测试题进 golden set

### 7.2 隐私

- 私人笔记**不进**公网 demo 默认路径：演示与评测全部走 `demo_notes/` 独立 collection
- Langfuse masking 开启；README 写明数据流向
- SSRF 表述收敛：讲"**出站前字面 host/IP 校验 + provider 侧抓取**"，并如实说明局限（未做 DNS 解析级校验、未校验重定向）——真实且加分

### 7.3 License（D11 定）

MIT（最大传播）vs PolyForm Strict（与老项目一致）；新仓库依赖老仓库 MCP Server 的事实必须在 README 显式说明许可关系。

## 八、风险登记 v2.2

| 风险 | 概率/影响 | 缓解 |
|---|---|---|
| DeepSeek search 不可用/未开通 | 高/高 | P0 真实预检；WEB_PROVIDER 切智谱/Tavily；必要时主力切智谱 |
| SenseNova 公测免费档随时收紧（付费档即将上线） | 中/中 | 仅作过渡期 LLM（SENSENOVA_* 开关，Token Plan 端点 token.sensenova.cn/v1 + sensenova-6.8-flash-lite，2026-10-31 实测可用；老端点 api.sensenova.cn 对新 Key 返回 403）；DeepSeek 到位后退二线 |
| 老仓库 MCP 改造回归 | 中高/高 | P0 基线 + feat/mcp 分支 + 833 测试门禁 + tag 备份 |
| 内存峰值超预期 | 中/中 | 按 2.5~3.5GB 预估（ai_study 单进程 torch+chroma+bge 即 1~1.5GB）；MCP 按需启动；演示用 `next build && next start`；关 Docker Desktop |
| 排期超支 | 高/中 | 每日记录实际 vs 计划，>1.5 倍触发砍线；D14 纯缓冲；D11 已有可发布版本 |
| 评测结论不可信 | 中/高 | 等预算+温度 0+重复+逐题表；case study 定位 |
| 隐私外泄 | 中/中 | demo_notes 隔离 + masking + 数据流向说明 |
| uv/pip 直连卡住 | 中/低 | 全部安装命令显式镜像参数 |

## 九、答辩叙事 v2.2（第 1 问已修正）

1. **"为什么 LangGraph？你们老项目不是手写过 Agent 吗？"** → 修正后口径：老项目用的是 LangChain `create_agent` 高层抽象（含"不重复建 agent"的缓存优化），出题够用但流程黑盒；新项目需要"规划-检索-验证-回环"**可见、可测、可熔断**，所以用显式状态机。两层抽象都摸过，知道取舍——比"手写过一切"更真实、更值钱
2. MCP 与 Function Calling 关系（跨仓库 MCP 解耦是活例子）
3. 上下文工程（裁剪/learnings 沉淀/压缩/双熔断，对标 open_deep_research 压缩模式）
4. 怎么证明有效（对照表 + 公式 + 局限）
5. 死循环（MAX_ROUNDS + attempted_queries 去重 + 字符预算）
6. 冲突处理（conflicts 显式披露）
7. 追踪发现过什么（D9 案例）
8. 为什么 DeepSeek 主力（P0 预检结论）
9. 生产化还差什么（SSRF 局限如实讲、沙箱、防爆破、队列、缓存）
10. 新老项目关系（进化主线 + MCP 反哺）

## 十、已采纳的默认决策（可否决）

| # | 决策 | 默认 | 否决备选 |
|---|---|---|---|
| Q1 | 新仓库 Python 版本 | **3.12** | 3.10（与老 env 一致，但不推荐，已 EOL） |
| Q2 | Next.js 定位 | **可选加分项**（D12~13），D11 先出可发布版 | 保持必做（不推荐，超支主源） |
| Q3 | 主力模型 | **P0 预检后定**：DeepSeek 可用则维持；否则智谱+Tavily | —— |
| Q4 | direct 兜底 | **给老仓库加 `/api/kb/search`**（带 token+契约表+测试） | 删 direct 只留 MCP（失去退路，不推荐） |

## 十一、对抗性评审修复清单（2026-11-01）

> 背景：对 54c9f5a~cb9baa6 做全量对抗性评审（攻击者+质疑者双视角），19 条发现。
> P0×3 已随 D8 提交（cb9baa6）；本节登记其余各项的处置，保持"PLAN 说了就要有代码"的对账。

| # | 问题 | 级别 | 处置 |
|---|---|---|---|
| 1 | `get_provider()` 每轮新建 → MCP 子进程泄漏（D7 后每轮漏 A+B 两个）+ 熔断跨轮失效 | P0 | ✅ 已修：按模式单例 + `_McpSession.close()`（幂等，atexit 兜底）+ `_teardown` 等 aclose 完成 |
| 2 | CI 密钥扫描永不失败（`\|\| true` + 内联 grep 命中反而退出 0） | P0 | ✅ 已修：`check_secrets.sh --all` 单一来源，命中即红 |
| 3 | 图执行崩溃 UI 假死（EVENT_ERROR 零发射点零消费点） | P0 | ✅ 已修：run_research 发 error 再 raise；Streamlit/网关消费 |
| 4 | `evidence_char_budget` 全项目零消费——"双熔断之二"不存在 | P1 | ✅ synthesizer 按预算裁剪证据 + warning 披露截断条数 |
| 5 | gaps 回环不生成新查询（空转） | P1 | ✅ D8 换路线解决：gap 跨轮重试（瞬时故障恢复价值）；查询改写归 D10 压缩节点 |
| 6 | 流式断流降级 → UI 报告"半截+全文"拼接 | P1 | ✅ 契约加 `report_reset{}`（断流且已发过 delta 才发）；三端同步 |
| 7 | MCP 路径 web_fetch 无本地 SSRF 校验（防线全寄老仓库进程） | P1 | ✅ 调用前先过本地 `validate_public_url`（本地+老仓库双层） |
| 8 | KB_API_BASE 非回环且无 token = 局域网裸奔 | P2 | ✅ direct.kb fail-fast：非回环必须带 token |
| 9 | 注入检测 10 条子串、可插空格绕过、只扫输入 | P2 | ✅ 模式扩容 + 空白归一化匹配 + 最终报告二次扫描（report-scan warning） |
| 10 | fake 模式 UI 无标识（演示假数据不自知） | P2 | ✅ research_started 带 provider_mode + UI 徽标 |
| 11 | URL 去重字符串全等（utm/http(s)/尾斜杠漏网） | P2 | ✅ `canonicalize_locator` 规范化后比对（kb locator 原样；不改写展示用 locator） |
| 12 | `_post_with_retry` 对 4xx 重试（烧配额放大限流） | P2 | ✅ 4xx 立即报错不重试；5xx 仍重试 |
| 13 | 报告 markdown 渲染外链图片（外泄通道） | P2 | ✅ 渲染前剥图片语法 |
| 14 | AGENTS.md"唯一连接=MCP"与 Q4 直连兜底矛盾；PLAN 列了未实现的 citation_checked | P3 | ✅ 文档口径同步（本节 + §3.3 + AGENTS.md 红线表） |
| 15 | conftest `PYTEST_OFFLINE` 死变量 | P3 | ✅ 删除 |
| 16 | `int()` 解析 env 手滑全站崩 | P3 | ✅ `_env_int` 容错回退默认值 |
| 17 | `_teardown` fire-and-forget aclose（初始化失败路径泄漏） | P3 | ✅ 已随 #1 修复 |
| 18 | estimate_tokens 粗估偏差 30%+ | P3 | ⏸ 维持粗估口径（注释已声明）；D9 Langfuse 接真实 usage 后替换 |
| 19 | planner/synthesizer 降级循环与 `invoke_with_fallback` 三处同构 | P3 | ⏸ 暂不重构：LLM 路径行为等价性离线验不了，冲刺期不动 |
