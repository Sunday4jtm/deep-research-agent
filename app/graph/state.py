"""LangGraph 状态定义（PLAN §3.2 State v2.2）。

字段取舍的"为什么"：
- ``attempted_queries`` / ``learnings`` 来自 dzhng/deep-research 的迭代检索
  模式——每轮带着上轮学到的东西生成更好的查询，绝不重复查同一个缺口；
- ``subq_evidence`` 记录"哪个子问题拿到了哪些证据编号"，D8 的覆盖度检查
  直接用它，不必从 evidence 反推；
- ``token_usage`` / ``errors`` 让失败语义可见：单点失败不中断，进 errors
  出部分报告（PLAN §3.4 失败语义）。
"""
from __future__ import annotations

from typing import TypedDict

from app.tools.base import Evidence


class ResearchState(TypedDict, total=False):
    question: str
    focus: str                    # 用户可选补充说明（简化版 research brief）
    sub_questions: list[str]
    attempted_queries: list[str]  # 已提交过的查询：跨轮去重的依据
    learnings: list[str]          # 每轮反思沉淀（v2.3 反思回路产出，喂下一轮反思与报告）
    rewritten: dict[str, str]     # gap 子问题 → 改写后的新查询（v2.3，循环真转的关键）
    evidence: list[Evidence]
    subq_evidence: dict[str, list[str]]  # 子问题 → 证据编号列表
    conflicts: list[dict[str, str]]      # KB vs 公网冲突（D8 填充）
    gaps: list[str]                      # 零证据子问题 → 追问回环的依据
    round: int                           # 已执行的检索轮次（熔断依据）
    token_usage: dict[str, int]          # 节点 → token 消耗（UI 展示成本）
    errors: list[str]                    # 失败语义：记入部分报告，不中断
    report: str
    citation_report: dict[str, int]
