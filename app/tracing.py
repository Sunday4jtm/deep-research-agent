"""Langfuse 追踪接入（D9）：全链路 LLM 调用的"行车记录仪"。

为什么收敛在这一个模块：callback 的注入点在 llm.invoke_with_fallback 与
synthesizer 的 stream/invoke（chat 调用仅有的三个出口），开关/mask/flush
的策略只写一份，三端（Streamlit/网关/未来 Next.js 经网关）天然共享。

mask 的实现与局限（如实记录）：SDK v5 的 get_client 在"进程首个实例"
上做注册克隆，且克隆是否保留 mask 未经网络路径实证——所以这里在进程
内【最先】构造带 mask 的实例（把密钥样式的字符串打码），同时真正的
第一道防线在应用层：State/prompt 里从设计上就不含任何密钥。
demo 语料本身已脱敏（mcp/load_demo_notes.py 的存在理由）。

开关：LANGFUSE_ENABLED=1 时开启；测试环境默认关（conftest 不设该变量），
离线红线零风险——不开关就没有任何 langfuse 导入。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

# 密钥样式：OpenAI 风格 sk-、Langfuse pk/sk-lf-、智谱 xxx.yyy 形态的兜底
_SECRET_RE = re.compile(r"(?:sk|pk)-[A-Za-z0-9_-]{8,}")


def _mask_secrets(data: Any) -> Any:
    """递归把密钥样式的字符串替换为 ***，其余原样透传。

    只做保守打码（宁可漏mask不可误伤语义）：prompt 里出现密钥本身就是
    事故，这里保证事故不上云。
    """
    if isinstance(data, str):
        return _SECRET_RE.sub("***", data)
    if isinstance(data, dict):
        return {k: _mask_secrets(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_mask_secrets(v) for v in data]
    return data


def _ensure_masked_client() -> None:
    """进程内【首个】Langfuse 实例必须带 mask：get_client 的单例注册
    只认第一个实例，谁先构造谁定规矩。LangfuseResourceManager 是 SDK 私有
    路径（v5.0.4 = langfuse._client.resource_manager），路径变了就跳过
    mask 注册、追踪照常跑——打码是纵深防御的一层，不是唯一防线
    （应用层 State/prompt 本就不含密钥）。失败静默降级，追踪绝不拖垮主流程。"""
    try:
        from langfuse import Langfuse
        from langfuse._client.resource_manager import LangfuseResourceManager

        if not LangfuseResourceManager._instances:
            Langfuse(mask=_mask_secrets)
    except Exception as exc:  # noqa: BLE001 —— 追踪层绝不影响主流程
        logger.warning("Langfuse 带掩码客户端注册失败，退化为默认客户端：%s", exc)


def callbacks() -> list:
    """返回本次调用应挂的 callback 列表；未开启/初始化失败 → 空列表。"""
    if not get_settings().tracing_enabled:
        return []
    _ensure_masked_client()
    try:
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Langfuse CallbackHandler 构建失败，本次不追踪：%s", exc)
        return []


def flush() -> None:
    """同步冲刷上报缓冲：研究结束时调用，保证 trace 完整落云。"""
    if not get_settings().tracing_enabled:
        return
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Langfuse flush 失败（trace 可能不完整）：%s", exc)
