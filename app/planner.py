"""LLM 版规划器：研究问题 → 2~4 个互补子问题（结构化输出 + 三级降级）。

三级降级直接继承老项目 grader/quiz_agent 的实战结论（glm-4-flash 高峰期
会偶发"不调工具、直接回一段自然语言"导致解析失败）：
  ① with_structured_output(function_calling)，按主备模型依次尝试；
  ② ①全败 → 普通调用 + 正则提取 JSON；
  ③ ②也败 → 确定性模板兜底——任何优化都不能成为新的故障点。
"""
from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, Field

from app.llm import _build_chat, _model_chain, estimate_tokens

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是研究规划器。把用户的研究问题拆解为 2~4 个互补的检索查询，"
    "覆盖不同角度（定义与原理、方法与对比、最新进展等）。"
    "只输出 JSON，格式：{\"sub_questions\": [\"...\"]}。"
)

_NUM_PREFIX = re.compile(r"^\s*(?:[-*•]|\d+[.、)])\s*")  # 模型爱给子问题加编号前缀


class SubQuestions(BaseModel):
    """结构化输出 schema：数量限制写进 schema，模型端先过滤一轮。"""

    sub_questions: list[str] = Field(
        description="2~4 个互补的检索查询，覆盖不同角度",
        min_length=1,
        max_length=4,
    )


def _usage_from(resp) -> int:
    meta = getattr(resp, "usage_metadata", None)
    if isinstance(meta, dict) and meta.get("total_tokens"):
        return int(meta["total_tokens"])
    return estimate_tokens(str(getattr(resp, "content", "")))


def _clean(items: list) -> list[str]:
    """去空、去编号前缀、去重、封顶 4 条——模型输出永远不能直接信。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        q = _NUM_PREFIX.sub("", item.strip())
        if not q or q in seen:
            continue
        seen.add(q)
        out.append(q)
    return out[:4]


def extract_json(text: str) -> list[str] | None:
    """从自由文本里抢救 JSON 数组：先找对象、再找裸数组，都不行返回 None。"""
    for pattern in (r"\{.*\}", r"\[.*\]"):
        match = re.search(pattern, text, re.DOTALL)
        if not match:
            continue
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            data = data.get("sub_questions")
        if isinstance(data, list):
            cleaned = _clean(data)
            if cleaned:
                return cleaned
    return None


def template_plan(question: str) -> list[str]:
    """③ 确定性兜底：单子问题直通。简单问题本来也不需要拆。"""
    return [question.strip()]


def plan_subquestions(question: str, focus: str = "") -> tuple[list[str], dict[str, int]]:
    """入口：永不向上抛异常（③兜底保证）。返回 (子问题, token_usage)。"""
    chain = _model_chain()
    user_text = f"研究问题：{question}"
    if focus:
        user_text += f"\n用户补充的关注点：{focus}（拆解时覆盖这个角度）"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]

    # ① 结构化输出，主备模型依次试
    for provider, base_url, model, api_key in chain:
        try:
            llm = _build_chat(provider, base_url, model, api_key, 0.0)
            structured = llm.with_structured_output(SubQuestions, method="function_calling")
            result = structured.invoke(messages)
            cleaned = _clean(result.sub_questions)
            if cleaned:
                logger.info("planner ① 成功：%s/%s → %d 条", provider, model, len(cleaned))
                return cleaned, {"planner": estimate_tokens(str(cleaned))}
        except Exception as exc:  # 限流/格式飘了都降下一级，不中断
            logger.warning("planner ① 失败（%s/%s）：%s", provider, model, exc)

    # ② 普通调用 + JSON 抢救
    for provider, base_url, model, api_key in chain:
        try:
            llm = _build_chat(provider, base_url, model, api_key, 0.0)
            resp = llm.invoke(messages)
            cleaned = extract_json(str(resp.content))
            if cleaned:
                logger.info("planner ② 成功：%s/%s → %d 条", provider, model, len(cleaned))
                return cleaned, {"planner": _usage_from(resp)}
        except Exception as exc:
            logger.warning("planner ② 失败（%s/%s）：%s", provider, model, exc)

    # ③ 模板兜底：链路空（无 Key）或 ①②全败都到这里
    if not chain:
        logger.warning("planner：未配置任何模型 Key，走模板兜底")
    else:
        logger.warning("planner ①② 全败，走模板兜底")
    return template_plan(question), {"planner": estimate_tokens(question)}
