"""Provider 真实预检（PLAN v2.2 P0-2）。

为什么要有这个脚本：v2.1 计划的 DeepSeek 链路只有 mock 证据，评审指出
"D4 就可能停摆"。本脚本用一次真实调用验证每条链路，P0 结束时主力模型必须定案。

原则：只输出 PASS/FAIL 与耗时，绝不打印任何密钥。
用法（在任意有依赖的环境里跑，不需要新 env）：
    python scripts/preflight.py            # 全部检查
    python scripts/preflight.py zhipu      # 只查智谱
    python scripts/preflight.py deepseek   # 只查 DeepSeek 三件套
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ZHIPU_BASE = "https://open.bigmodel.cn/api/paas/v4/"
DEEPSEEK_BASE = "https://api.deepseek.com/v1"
SENSENOVA_BASE = "https://api.sensenova.cn/compatible-mode/v1"
# 工具接口不走 /v1 前缀，与老仓库 web_research.py 口径对齐
DEEPSEEK_TOOLS_BASE = "https://api.deepseek.com"


def load_env() -> dict[str, str]:
    """轻量 .env 解析：预检阶段不引入 python-dotenv，任何环境都能跑。"""
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    override = {k: v for k, v in os.environ.items() if k.endswith(("API_KEY", "API_BASE")) and v}
    env.update(override)
    return env


def _report(name: str, ok: bool, start: float, detail: str = "") -> bool:
    elapsed = time.monotonic() - start
    flag = "PASS" if ok else "FAIL"
    print(f"{flag}  {name:<26} {elapsed:5.1f}s  {detail[:110]}")
    return ok


def check_chat(name: str, api_key: str, base_url: str, model: str) -> bool:
    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI

    start = time.monotonic()
    try:
        llm = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=0,
            timeout=45,
            max_retries=1,
        )
        resp = llm.invoke([HumanMessage(content="只回复两个字母：ok")])
        ok = bool(resp.content and str(resp.content).strip())
        return _report(name, ok, start, f"回复：{str(resp.content)[:40]!r}")
    except Exception as exc:  # 限流/网络/Key 错误统一汇报，不中断其余检查
        return _report(name, False, start, f"{type(exc).__name__}: {str(exc)[:100]}")


def check_tools(env: dict[str, str]) -> bool:
    import requests

    key = env.get("DEEPSEEK_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    start = time.monotonic()
    try:
        resp = requests.post(
            f"{DEEPSEEK_TOOLS_BASE}/v1/tools/search",
            json={"query": "LangGraph 官方文档 StateGraph", "count": 3},
            headers=headers,
            timeout=45,
        )
        resp.raise_for_status()
        items = resp.json().get("results", [])
        first = items[0].get("url", "")[:55] if items else "—"
        ok = _report("deepseek search", True, start, f"命中 {len(items)} 条，首条：{first}")
    except Exception as exc:
        ok = _report("deepseek search", False, start, f"{type(exc).__name__}: {str(exc)[:100]}")

    start = time.monotonic()
    try:
        resp = requests.post(
            f"{DEEPSEEK_TOOLS_BASE}/v1/tools/fetch",
            json={"url": "https://example.com"},
            headers=headers,
            timeout=45,
        )
        resp.raise_for_status()
        content = str(resp.json().get("content") or "")
        ok2 = _report("deepseek fetch", True, start, f"正文 {len(content)} 字")
    except Exception as exc:
        ok2 = _report("deepseek fetch", False, start, f"{type(exc).__name__}: {str(exc)[:100]}")
    return ok and ok2


def main() -> int:
    env = load_env()
    targets = sys.argv[1:] or ["zhipu", "deepseek", "sensenova"]
    all_ok = True

    if "zhipu" in targets:
        key = env.get("ZHIPU_API_KEY", "")
        if not key:
            print("SKIP  zhipu chat               未配置 ZHIPU_API_KEY")
            all_ok = False
        else:
            all_ok &= check_chat("zhipu chat (glm-4-flash)", key, ZHIPU_BASE, "glm-4-flash")

    if "sensenova" in targets:
        key = env.get("SENSENOVA_API_KEY", "")
        if not key:
            print("SKIP  sensenova chat           未配置 SENSENOVA_API_KEY")
            all_ok = False
        else:
            base = env.get("SENSENOVA_BASE_URL", SENSENOVA_BASE)
            model = env.get("SENSENOVA_MODEL", "SenseChat-5")
            all_ok &= check_chat(f"sensenova chat ({model})", key, base, model)

    if "deepseek" in targets:
        key = env.get("DEEPSEEK_API_KEY", "")
        if not key:
            print("SKIP  deepseek chat/search/fetch  未配置 DEEPSEEK_API_KEY")
            print("（作业：注册 platform.deepseek.com 并充值，填进 .env 后重跑本脚本）")
            all_ok = False
        else:
            all_ok &= check_chat("deepseek chat", key, DEEPSEEK_BASE, "deepseek-chat")
            all_ok &= check_tools(env)

    verdict = "全部通过，可按计划推进" if all_ok else "存在未验证链路——作业见 PLAN.md §五 P0-2"
    print(f"\n结论：{verdict}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
