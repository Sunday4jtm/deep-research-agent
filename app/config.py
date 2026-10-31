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
    provider_mode: str       # mcp | direct | fake（PLAN D-5：降级全做成开关）
    web_provider: str        # deepseek | zhipu | tavily | none
    max_rounds: int          # 检索回环熔断（depth）
    breadth: int             # 每轮子问题检索上限（借鉴 dzhng/deep-research）
    evidence_char_budget: int  # 证据总字符预算（双熔断之二）


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        deepseek_api_key=_env("DEEPSEEK_API_KEY"),
        zhipu_api_key=_env("ZHIPU_API_KEY"),
        model_main=_env("MODEL_MAIN", "deepseek-chat"),
        model_fallback=_env("MODEL_FALLBACK", "glm-4-flash"),
        provider_mode=_env("PROVIDER_MODE", "fake"),
        web_provider=_env("WEB_PROVIDER", "deepseek"),
        max_rounds=int(_env("MAX_ROUNDS", "3")),
        breadth=int(_env("BREADTH", "3")),
        evidence_char_budget=int(_env("EVIDENCE_CHAR_BUDGET", "12000")),
    )
