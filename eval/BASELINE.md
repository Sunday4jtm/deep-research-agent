# 评测基线（D10，免费通道 24 run）

> 为什么入库：`eval/results/` 被 gitignore（随跑随生成），README 里的核心数字
> 此前在仓库里零证据可查——clone 者既看不到也验不了。本文件是免费通道最终
> 基线的**脱敏快照**，数字来自本机 `eval/results/runs_20261106_003109.jsonl`
> （逐行可验算）。

## 协议与口径

- 6 题（3 private + 2 conflict + 1 injection）× single/multi × 2 次 = **24 run**；
  temperature=0，等证据预算，夹具题整条链路钉死（ScriptedProvider + template
  planner——防御类指标不取决于当天公网上有什么）；
- **web/hybrid 4 题暂缓**：DeepSeek 全线停用（计费，用户指示 2026-11-02），
  恢复后补跑，本表不覆盖公网题；
- **`INJECTION_FILTER=strict` 已上线**（默认）：本基线生成于强过滤落地后
  （2026-11-06），**注入存活 4/4**——对照历史：strict 之前的 24 run（
  2026-11-02，只告警不拦截）注入存活 **0/4**（商汤 6.8-flash-lite 在全部
  4 次注入题中执行了金丝雀，即"换免费模型=防御崩塌"的实测发现），该发现
  就是 strict 强过滤的动因；0/4 → 4/4 是架构层防御生效的实证闭环，
  门禁测试（复读机假模型）在 CI 层钉死不回退；
- N/A 不进均值；引用存在率对 refs_unique=0 的 run 记 N/A。

## 汇总（按 kind × mode，strict 后基线）

| kind | mode | n | wall 均值 | 引用存在率 | 覆盖率 | 冲突披露 | 注入存活 | 支撑率（裁判） |
|---|---|---|---|---|---|---|---|---|
| conflict | single | 4 | 14.9s | 1.000 | 1.000 | 1.00 | N/A | 1.000 |
| conflict | multi | 4 | 17.3s | 1.000 | 1.000 | 1.00 | N/A | 1.000 |
| injection | single | 2 | 17.4s | 1.000 | 1.000 | N/A | **2/2** | 0.900 |
| injection | multi | 2 | 19.7s | 1.000 | 1.000 | N/A | **2/2** | 0.800 |
| private | single | 6 | 23.8s | 1.000 | 1.000 | N/A | N/A | 1.000 |
| private | multi | 6 | 24.2s | 1.000 | 1.000 | N/A | N/A | 1.000 |
| **全局** | — | 24 | p50 19.6s / p95 32.5s | 1.000 | 1.000 | 1.00 | **4/4** | **0.975** |

## 历史基线对照（strict 前，2026-11-02）

| 指标 | strict 前（只告警） | strict 后（架构层拦截） |
|---|---|---|
| 注入存活 | **0/4**（4/4 执行金丝雀） | **4/4** |
| 支撑率（裁判） | 0.917 | 0.975 |
| 引用存在率 / 覆盖率 | 1.000 / 1.000 | 1.000 / 1.000（持平） |
| 冲突披露 | 1.00 | 1.00（持平） |

## 延迟口径（如实）

- wall p50/p95 为 24 run 墙钟分位数；p95=32.5s 被最长 run（35.9s）主导——
  小样本下分位数对尾部敏感；
- strict 强过滤把命中注入特征的证据隔离出综合 prompt，注入题的支撑率反而
  从 0.6/0.7 升至 0.9/0.8：被污染的证据不再混进引用。

## 复现

```bash
# 夹具题（private/conflict/injection）无 Key 可跑（judge 变 N/A）；
# 完整数字需智谱 Key（裁判，JUDGE_MODEL）+ 商汤 Key（chat 免费档）
python -m eval.run_eval --modes single,multi --repeat 2 --kinds private,conflict,injection
```
