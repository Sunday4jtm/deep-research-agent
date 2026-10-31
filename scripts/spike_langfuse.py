"""Spike②：Langfuse callback 链路验证。

前置：.env 里填好 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST
（注册 https://cloud.langfuse.com → Projects → API Keys，2 分钟）。
没有 Key 时会友好退出，不影响其他工作。
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

import os  # noqa: E402


def main() -> int:
    if not os.environ.get("LANGFUSE_PUBLIC_KEY"):
        print("SKIP：尚未配置 LANGFUSE_PUBLIC_KEY")
        print("（作业：注册 cloud.langfuse.com 拿 API Keys 填进 .env）")
        return 2

    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI
    from langfuse import get_client
    from langfuse.langchain import CallbackHandler

    handler = CallbackHandler()
    llm = ChatOpenAI(
        model="glm-4-flash",
        api_key=os.environ["ZHIPU_API_KEY"],
        base_url="https://open.bigmodel.cn/api/paas/v4/",
        timeout=45,
    )
    llm.invoke([HumanMessage(content="只回复：trace ok")], config={"callbacks": [handler]})
    get_client().flush()
    print("SPIKE_LANGFUSE_PASS：trace 已上报（到 Langfuse 控制台确认可见）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
