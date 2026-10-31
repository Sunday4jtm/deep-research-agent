"""双模型客户端：主力优先、失败自动回退，调用方不感知主备。

沿用老项目踩坑结论（为什么这么写）：
1. ``lru_cache`` 只在函数返回后才写入缓存 → 并发首次调用会各自完整初始化
   （老项目里模型被加载两遍），所以必须加锁单飞；
2. 国产模型高峰期限流/偶发不按格式回复是常态，调用层必须有兜底，
   不能指望用户手点重试（老项目 grader/quiz_agent 三级降级的同款思路）。
"""
from __future__ import annotations

import logging
import threading
from functools import lru_cache

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from app.config import DEEPSEEK_BASE_URL, ZHIPU_BASE_URL, get_settings
from app.tracing import callbacks as tracing_callbacks

logger = logging.getLogger(__name__)
_build_lock = threading.Lock()


def estimate_tokens(text: str) -> int:
    """粗估 token（中文≈1.5字/token、英文≈4字符/token，取中间值 3）。

    为什么放 llm.py：planner 与 synthesizer 都要用，且"没有
    usage_metadata 时也不缺数"是模型层的公共策略。P3 接入 Langfuse
    后以 provider 返回的真实 usage 为准。
    """
    return max(1, len(text or "") // 3)


def _model_chain() -> list[tuple[str, str, str, str]]:
    """按优先级返回 (provider, base_url, model, api_key) 列表。Key 缺失的跳过。

    顺序即优先级：DeepSeek（主力）→ SenseNova（过渡期公测免费）→ 智谱（兜底）。
    谁配了 Key 谁上岗，全部缺席才报错——不靠配置排列组合，靠 Key 存在性。
    """
    s = get_settings()
    chain: list[tuple[str, str, str, str]] = []
    if s.deepseek_api_key:
        chain.append(("deepseek", DEEPSEEK_BASE_URL, s.model_main, s.deepseek_api_key))
    if s.sensenova_api_key:
        chain.append(
            ("sensenova", s.sensenova_base_url, s.sensenova_model, s.sensenova_api_key)
        )
    if s.zhipu_api_key:
        chain.append(("zhipu", ZHIPU_BASE_URL, s.model_fallback, s.zhipu_api_key))
    return chain


@lru_cache(maxsize=8)
def _build_chat(
    provider: str, base_url: str, model: str, api_key: str, temperature: float
) -> ChatOpenAI:
    with _build_lock:  # 加锁单飞：防止并发首调重复初始化（老项目修过的 bug）
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            timeout=60,
            max_retries=1,
        )


def invoke_with_fallback(
    messages: list[BaseMessage], temperature: float = 0.0
) -> tuple[str, BaseMessage]:
    """依次尝试主备模型，返回 (provider, 响应)。全部失败才抛错。"""
    chain = _model_chain()
    if not chain:
        raise RuntimeError(
            "未配置任何模型 Key：请在 .env 填 DEEPSEEK_API_KEY 或 ZHIPU_API_KEY"
        )
    errors: list[str] = []
    for provider, base_url, model, api_key in chain:
        try:
            resp = _build_chat(provider, base_url, model, api_key, temperature).invoke(
                messages, config={"callbacks": tracing_callbacks()}
            )
            return provider, resp
        except Exception as exc:  # 限流/网络/Key 问题都降级，不中断业务
            errors.append(f"{provider}/{model}: {type(exc).__name__}")
            logger.warning("模型调用失败，切换下一个：%s - %s", provider, exc)
    raise RuntimeError("主备模型均失败：" + " | ".join(errors))
