# 评测基线（D10，免费通道 24 run）

> 为什么入库：`eval/results/` 被 gitignore（随跑随生成），README 里的核心数字
> 此前在仓库里零证据可查——clone 者既看不到也验不了。本文件是免费通道最终
> 基线的**脱敏快照**，数字来自本机 `eval/results/runs_20261102_084409.jsonl`
> （逐行可验算）。

## 协议与口径

- 6 题（3 private + 2 conflict + 1 injection）× single/multi × 2 次 = **24 run**；
  temperature=0，等证据预算，夹具题整条链路钉死（ScriptedProvider + template
  planner——防御类指标不取决于当天公网上有什么）；
- **web/hybrid 4 题暂缓**：DeepSeek 全线停用（计费，用户指示 2026-11-02），
  恢复后补跑，本表不覆盖公网题；
- **注入 0/4 存活是历史口径**：本基线生成于 `INJECTION_FILTER=strict` 上线前
  （当时只告警不拦截），商汤 6.8-flash-lite 在 4/4 次注入题中执行了金丝雀
  ——这个崩塌发现正是 strict 强过滤上线的动因；strict 落地后注入防御由架构
  层保证（门禁测试钉死），重跑预期存活 4/4；
- N/A 不进均值；引用存在率对 refs_unique=0 的 run 记 N/A。

## 汇总（按 kind × mode）

| kind | mode | n | wall 均值 | 引用存在率 | 覆盖率 | 冲突披露 | 注入存活 | 支撑率（裁判） |
|---|---|---|---|---|---|---|---|---|
| conflict | single | 4 | 17.3s | 1.000 | 1.000 | 1.00 | N/A | 1.000 |
| conflict | multi | 4 | 15.7s | 1.000 | 1.000 | 1.00 | N/A | 1.000 |
| injection | single | 2 | 20.7s | 1.000 | 1.000 | N/A | **0/2** | 0.600 |
| injection | multi | 2 | 19.0s | 1.000 | 1.000 | N/A | **0/2** | 0.700 |
| private | single | 6 | 27.0s | 1.000 | 1.000 | N/A | N/A | 0.900 |
| private | multi | 6 | 17.4s | 1.000 | 1.000 | N/A | N/A | 1.000 |
| **全局** | — | 24 | p50 16.9s / p95 34.9s | 1.000 | 1.000 | 1.00 | **0/4** | **0.917** |

## 延迟口径（如实）

- wall p50/p95 为 24 run 墙钟分位数；p95=34.9s 被最长 run（55.8s）主导——
  小样本下分位数对尾部敏感；
- 节点级 p50 均值 0.01s（检索/验证均亚秒级），p95 均值 19.5s ≈ synthesizer
  的模型本质延迟（D9 用 trace 定位过并据此做了检索并行化）。

## 复现

```bash
# 夹具题（private/conflict/injection）无 Key 可跑（judge 变 N/A）；
# 完整数字需智谱 Key（裁判）+ 老仓库 demo 库连接
python -m eval.run_eval --modes single,multi --repeat 2 --kinds private,conflict,injection
```
