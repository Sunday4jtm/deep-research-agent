"""Langfuse 追踪接入（D9）：全链路 LLM 调用的"行车记录仪"。

为什么收敛在这一个模块：callback 的注入点覆盖 chat 调用的全部出口——
llm.invoke_with_fallback（verifier 反思链）、synthesizer（流式/非流式/
压缩）与 agent_loop 的 ReAct 每步，开关/mask/flush 的策略只写一份，
各端（静态研究台/Streamlit/未来 Next.js 经网关）天然共享。

mask 的实现与局限（如实记录）：SDK v5 的 get_client 在"进程首个实例"
上做注册克隆，且克隆是否保留 mask 未经网络路径实证——所以这里在进程
内【最先】构造带 mask 的实例（把密钥样式的字符串打码），同时真正的
第一道防线在应用层：State/prompt 里从设计上就不含任何密钥。
demo 语料本身已脱敏（mcp/load_demo_notes.py 的存在理由）。

开关：LANGFUSE_ENABLED=1 时开启；测试环境默认关（conftest 不设该变量），
离线红线零风险——不开关就没有任何 langfuse 导入。
"""
from __future__ import annotations

import contextvars
import logging
import re
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

# 一次研究的 run 级 trace 上下文（评审 P1"trace 不贯穿"）：run_research 开场
# 生成父 trace_id 放进 ContextVar，callbacks() 据此给每次 LLM 调用挂
# trace_context——同线程内所有调用落到同一个 trace，面板才能还原一次研究的
# 完整链路。研究跑在专用线程里（Streamlit/网关/评测同一模式），ContextVar
# 的线程隔离正好保证跨 run 不串。
_run_trace: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "lf_run_trace", default=None
)

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


def start_run_trace() -> bool:
    """一次研究开一个父 trace。返回 True=已开启；未开启追踪或失败返回 False。

    trace_context 的 {"trace_id": ...} 形态是 SDK 文档用法（CallbackHandler
    docstring 示例）；create_trace_id 产出 32 位小写十六进制，符合 SDK 校验。
    """
    if not get_settings().tracing_enabled:
        return False
    try:
        from langfuse import get_client

        _run_trace.set({"trace_id": get_client().create_trace_id()})
        return True
    except Exception as exc:  # noqa: BLE001 —— 追踪层绝不影响主流程
        logger.warning("Langfuse run trace 创建失败，本次退化为孤立 trace：%s", exc)
        return False


def end_run_trace() -> None:
    """run 结束必须复位：同线程的下一次研究不清理就会串进旧 trace。"""
    _run_trace.set(None)


def callbacks() -> list:
    """返回本次调用应挂的 callback 列表；未开启/初始化失败 → 空列表。

    run trace 上下文存在时，handler 以 trace_context 接入父 trace——
    这是"全链路可追踪"能按一次研究聚合的关键。
    """
    if not get_settings().tracing_enabled:
        return []
    _ensure_masked_client()
    try:
        from langfuse.langchain import CallbackHandler

        ctx = _run_trace.get()
        if ctx:
            return [CallbackHandler(trace_context=ctx)]
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
