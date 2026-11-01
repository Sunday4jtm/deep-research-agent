"""全局配置：一切来自环境变量 / .env，绝不硬编码密钥。

为什么不用 pydantic-settings：冲刺期依赖越少越好；且离线测试
要求"没有 Key 也能 import"——缺配置只应在真正调用 provider 时报错。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"


@dataclass(frozen=True)
class Settings:
    deepseek_api_key: str
    zhipu_api_key: str
    model_main: str          # 主力 chat 模型，P0 预检后定稿
    model_fallback: str      # 兜底 chat 模型（国产免费模型高峰限流是常态，见老项目结论）
    sensenova_api_key: str   # 过渡期 LLM（公测免费，5h/1500次）；DeepSeek 到位后退二线
    sensenova_base_url: str
    sensenova_model: str     # 模型名以 platform.sensenova.cn 控制台为准
    provider_mode: str       # mcp | direct | fake（PLAN D-5：降级全做成开关）
    planner_mode: str        # llm | template（离线测试强制 template，见 conftest）
    synth_mode: str          # llm | template（同上；LLM 版带引用报告+流式）
    zhipu_search_engine: str  # 智谱独立 web_search API 的引擎档位（search_std 起步）
    web_provider: str        # deepseek | zhipu | tavily | none
    mcp_web_command: str     # MCP Server A 启动命令（stdio，空 = mcp 模式不可用）
    mcp_kb_command: str      # MCP Server B 启动命令（D7；空 = mcp 模式 kb 侧不可用）
    kb_api_base: str         # 老仓库后端地址（DirectProvider.kb 兜底走 HTTP）
    kb_api_token: str        # 老仓库 X-API-Token（须与老仓库 .env 的 APP_AUTH_TOKEN 一致）
    max_rounds: int          # 检索回环熔断（depth）
    breadth: int             # 每轮子问题检索上限（借鉴 dzhng/deep-research）
    evidence_char_budget: int  # 证据总字符预算（双熔断之二）
    agent_mode: str         # research（流水线）| react（ReAct 自主循环，v2.4）
    agent_max_steps: int    # ReAct 步数熔断
    reflect_mode: str       # llm（验证后 LLM 反思：learnings+查询改写）| off（纯确定性 gap 重试）
    memory_enabled: bool    # 跨 run 记忆（data/memory/runs.jsonl，已 gitignore）
    tracing_enabled: bool    # Langfuse 追踪开关（D9；Keys 在 .env，开关独立——离线测试默认关）


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    """int 容错解析：.env 手滑（'3a'/误删数字）不该让全站 import 即崩（评审 P3-16）。

    get_settings 被 lru_cache 后，一次解析崩溃 = 之后所有调用都拿到异常，
    连 fake 模式的离线路径都起不来——所以宁可用默认值也不要炸。
    """
    raw = _env(key)
    try:
        return int(raw)
    except ValueError:
        return default


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        deepseek_api_key=_env("DEEPSEEK_API_KEY"),
        zhipu_api_key=_env("ZHIPU_API_KEY"),
        model_main=_env("MODEL_MAIN", "deepseek-chat"),
        model_fallback=_env("MODEL_FALLBACK", "glm-4-flash"),
        sensenova_api_key=_env("SENSENOVA_API_KEY"),
        sensenova_base_url=_env("SENSENOVA_BASE_URL", "https://api.sensenova.cn/compatible-mode/v1"),
        sensenova_model=_env("SENSENOVA_MODEL", "SenseChat-5"),
        provider_mode=_env("PROVIDER_MODE", "fake"),
        planner_mode=_env("PLANNER_MODE", "llm"),
        synth_mode=_env("SYNTH_MODE", "llm"),
        zhipu_search_engine=_env("ZHIPU_SEARCH_ENGINE", "search_std"),
        web_provider=_env("WEB_PROVIDER", "deepseek"),
        mcp_web_command=_env("MCP_WEB_CMD"),
        mcp_kb_command=_env("MCP_KB_CMD"),
        kb_api_base=_env("KB_API_BASE", "http://127.0.0.1:8000"),
        kb_api_token=_env("KB_API_TOKEN"),
        max_rounds=_env_int("MAX_ROUNDS", 3),
        breadth=_env_int("BREADTH", 3),
        evidence_char_budget=_env_int("EVIDENCE_CHAR_BUDGET", 12000),
        agent_mode=_env("AGENT_MODE", "research"),
        agent_max_steps=int(_env("AGENT_MAX_STEPS", "10")),
        reflect_mode=_env("REFLECT_MODE", "llm"),
        memory_enabled=_env("MEMORY_ENABLED", "1") in {"1", "true", "yes", "on"},
        tracing_enabled=_env("LANGFUSE_ENABLED") in {"1", "true", "yes", "on"},
    )
