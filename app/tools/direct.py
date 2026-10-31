"""DirectProvider：不经 MCP 的直连实现（D4 落地 web 侧；D6 补 kb 侧 = HTTP 直连老仓库）。

口径对齐老仓库 app/web_research.py：DeepSeek 官方 /v1/tools/search 与
/v1/tools/fetch。SSRF 校验同样只做字面检查——答辩话术要如实：
"出站前字面校验 + provider 侧抓取"，不含 DNS 解析级防护、不校验重定向。

失败语义（PLAN §3.4）：
- 每次请求最多 2 次退避重试（1s/2s）；
- 同一 provider 连续失败 3 次 → 熔断：直接抛 CircuitOpen，
  由 retriever 兜住并记入 errors，本轮跳过该源；
- DeepSeek 失败自动降智谱（web_search），反之不降（智谱没有 fetch 工具）。
"""
from __future__ import annotations

import ipaddress
import logging
import threading
import time
from urllib.parse import urlparse

import requests

from app.config import get_settings
from app.tools.base import Evidence
from app.tools.sanitize import sanitize_web_text

logger = logging.getLogger(__name__)

DEEPSEEK_API = "https://api.deepseek.com"
ZHIPU_API = "https://open.bigmodel.cn/api/paas/v4"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 2
BREAKER_THRESHOLD = 3


class WebSearchError(RuntimeError):
    """搜索/抓取失败（网络、鉴权、返回结构异常、熔断）。"""


class CircuitOpen(WebSearchError):
    """连续失败熔断中：调用方应跳过该源并记录。"""


def validate_public_url(url: str) -> str:
    """拒绝本机/内网 URL（字面检查，不做 DNS 解析——保持与老仓库同口径）。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise WebSearchError("只允许 http/https 公网 URL")
    if parsed.username or parsed.password:
        raise WebSearchError("URL 不能包含账号密码")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise WebSearchError("拒绝抓取本机/局域网地址")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise WebSearchError("拒绝抓取本机/内网地址")
    return url


def _post_with_retry(url: str, payload: dict, headers: dict) -> dict:
    """POST + 退避重试，带"环境配置 → 裸直连"两段式自愈。

    为什么需要两段式：本机实测 requests 默认路径（trust_env=True）对
    open.bigmodel.cn 握手报"证书链无法校验"，而 trust_env=False 直连
    正常；根因未完全定位（无代理/CA 环境变量，疑似系统钥匙串或加速
    工具的 DNS 层）。先按环境配置发（企业 MITM 网络需要它），一旦
    失败立即用裸直连兜底——两种网络环境都能自愈，不赌根因。

    为什么 4xx 不重试：401/403/429 是确定性失败（Key 错/欠费/限流），
    原样重发只会烧配额、放大限流（评审 P2-12）；5xx 与网络错误才值得退避重试。
    """
    last: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        for trust_env in (True, False):
            try:
                with requests.Session() as session:
                    session.trust_env = trust_env
                    resp = session.post(
                        url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
                    )
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as exc:
                status = (
                    exc.response.status_code
                    if getattr(exc, "response", None) is not None
                    else 0
                )
                if status < 500:
                    raise WebSearchError(f"请求被拒绝（HTTP {status}），不重试") from exc
                last = exc
                logger.warning(
                    "请求失败(HTTP %s, trust_env=%s, 第%d次) %s：%s",
                    status,
                    trust_env,
                    attempt,
                    url,
                    exc,
                )
            except Exception as exc:  # 网络/解析错误统一进下一级
                last = exc
                logger.warning(
                    "请求失败(trust_env=%s, 第%d次) %s：%s", trust_env, attempt, url, exc
                )
        if attempt < MAX_RETRIES:
            time.sleep(attempt)  # 1s → 2s 退避
    raise WebSearchError(f"请求重试后仍失败：{last}") from last


class _Breaker:
    """连续失败计数器：成功清零，达到阈值熔断。

    D9 起检索并行化（nodes.py 线程池并发调 web/kb），record 会被多线程
    同时调——计数读写必须加锁，否则熔断阈值可能被并发竞态吞掉。
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._fails = 0
        self._lock = threading.Lock()

    def record(self, ok: bool) -> None:
        with self._lock:
            self._fails = 0 if ok else self._fails + 1

    @property
    def open(self) -> bool:
        with self._lock:
            return self._fails >= BREAKER_THRESHOLD

    @property
    def fails(self) -> int:
        with self._lock:
            return self._fails


class DirectProvider:
    """直连实现：PROVIDER_MODE=direct 时由 get_provider() 返回。"""

    def __init__(self) -> None:
        self._deepseek_breaker = _Breaker("deepseek")
        self._zhipu_breaker = _Breaker("zhipu")
        self._kb_breaker = _Breaker("kb")

    # ---- web_search：DeepSeek 主路，智谱兜底 ----
    def web_search(self, query: str, k: int = 3) -> list[Evidence]:
        settings = get_settings()
        attempts: list[str] = []
        if settings.deepseek_api_key and settings.web_provider == "deepseek":
            try:
                return self._deepseek_search(query, k)
            except Exception as exc:
                attempts.append(f"deepseek: {type(exc).__name__}")
                logger.warning("DeepSeek 搜索失败，降级智谱：%s", exc)
        if settings.zhipu_api_key:
            return self._zhipu_search(query, k)
        raise WebSearchError(
            "没有可用的 web_search provider（" + "; ".join(attempts) + "）"
        )

    def _deepseek_search(self, query: str, k: int) -> list[Evidence]:
        if self._deepseek_breaker.open:
            raise CircuitOpen(f"deepseek 熔断中（连续失败 {self._deepseek_breaker.fails} 次）")
        settings = get_settings()
        try:
            data = _post_with_retry(
                f"{DEEPSEEK_API}/v1/tools/search",
                {"query": query, "count": k},
                {"Authorization": f"Bearer {settings.deepseek_api_key}"},
            )
            items = data.get("results")
            if not isinstance(items, list):
                raise WebSearchError("联网搜索缺少 results 数组")
            out = self._items_to_evidence(items, k)
            self._deepseek_breaker.record(True)
            return out
        except Exception:
            self._deepseek_breaker.record(False)
            raise

    def _zhipu_search(self, query: str, k: int) -> list[Evidence]:
        if self._zhipu_breaker.open:
            raise CircuitOpen(f"zhipu 熔断中（连续失败 {self._zhipu_breaker.fails} 次）")
        settings = get_settings()
        # 走智谱【独立】web_search API。实测记录（D4）：
        # 1) OpenAI 兼容 chat 端点挂 tools=[web_search] 不产生真实搜索
        #    （content 全是编造的泛泛而谈，没有一个真实 URL）——废路；
        # 2) 独立端点 /api/paas/v4/web_search 是收费接口，未充值返回
        #    429/1113——由熔断与失败语义兜住，充值后即可用。
        payload = {
            "search_engine": settings.zhipu_search_engine,
            "search_query": query,
            "count": k,
        }
        try:
            data = _post_with_retry(
                f"{ZHIPU_API}/web_search",
                payload,
                {"Authorization": f"Bearer {settings.zhipu_api_key}"},
            )
            items = data.get("search_result")
            if not isinstance(items, list):
                raise WebSearchError("zhipu web_search 缺少 search_result 数组")
            out = self._items_to_evidence(items, k)
            if not out:
                raise WebSearchError("zhipu web_search 未解析到有效结果")
            self._zhipu_breaker.record(True)
            return out
        except Exception:
            self._zhipu_breaker.record(False)
            raise

    @staticmethod
    def _items_to_evidence(items: list, k: int) -> list[Evidence]:
        out: list[Evidence] = []
        for item in items[:k]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("link") or "").strip()
            if not url:
                # 无 URL 的结果无法被引用与核验——跳过（与老仓库 web_research 同口径）
                continue
            title = str(item.get("title") or "").strip() or url
            content = str(
                item.get("snippet") or item.get("description") or item.get("content") or ""
            )
            out.append(
                Evidence(
                    eid="",
                    source_type="web",
                    title=title[:120],
                    locator=url,
                    snippet=sanitize_web_text(content),
                )
            )
        return out

    # ---- web_fetch：仅 DeepSeek 支持 ----
    def web_fetch(self, url: str) -> str:
        settings = get_settings()
        if not settings.deepseek_api_key:
            raise WebSearchError("网页抓取仅 DeepSeek provider 支持（当前未配置其 Key）")
        validate_public_url(url)
        if self._deepseek_breaker.open:
            raise CircuitOpen("deepseek 熔断中")
        try:
            data = _post_with_retry(
                f"{DEEPSEEK_API}/v1/tools/fetch",
                {"url": url},
                {"Authorization": f"Bearer {settings.deepseek_api_key}"},
            )
            content = sanitize_web_text(str(data.get("content") or ""))
            if not content:
                raise WebSearchError("抓取结果没有正文")
            self._deepseek_breaker.record(True)
            return content
        except Exception:
            self._deepseek_breaker.record(False)
            raise

    # ---- kb：HTTP 直连老仓库 /api/kb/search（Q4 的 direct 兜底；主力取证走 MCP）----
    def _kb_get(self, path: str, params: dict) -> dict:
        """带 X-API-Token 的 GET；错误统一翻译成人话（老仓库 401 的 detail 是前端口吻）。"""
        settings = get_settings()
        host = (urlparse(settings.kb_api_base).hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "localhost.localdomain", "::1") and (
            not settings.kb_api_token
        ):
            # fail-fast（评审 P2-8）：老仓库 APP_AUTH_TOKEN 未配置时入站鉴权
            # 整体关闭（其 main.py 的既有决定）——非回环地址 + 零令牌 = 局域网
            # 任何人可全文检索私有库，新仓库侧必须在出门前拒绝
            raise WebSearchError(
                f"KB_API_BASE 指向非本机地址（{host}）但未配置 KB_API_TOKEN——"
                "拒绝无令牌跨网访问私有库；请在 .env 配置与老仓库 APP_AUTH_TOKEN 一致的令牌"
            )
        url = settings.kb_api_base.rstrip("/") + path
        headers = {"X-API-Token": settings.kb_api_token} if settings.kb_api_token else {}
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=10)
        except requests.RequestException as exc:
            raise WebSearchError(
                f"老仓库后端不可达（{type(exc).__name__}）：KB_API_BASE={settings.kb_api_base}"
                "——确认老仓库后端已启动，或改用 PROVIDER_MODE=mcp"
            ) from exc
        if resp.status_code == 401:
            raise WebSearchError(
                "老仓库令牌校验失败：KB_API_TOKEN 必须与老仓库 .env 的 APP_AUTH_TOKEN 一致"
            )
        try:
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise WebSearchError(f"老仓库响应异常（{type(exc).__name__}）") from exc

    def kb_search(self, query: str, k: int = 3) -> list[Evidence]:
        if self._kb_breaker.open:
            raise CircuitOpen(f"kb 熔断中（连续失败 {self._kb_breaker.fails} 次）")
        try:
            data = self._kb_get("/api/kb/search", {"q": query, "k": k})
            out: list[Evidence] = []
            for item in (data.get("results") or [])[:k]:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "")
                source = str(item.get("source") or "")
                if not text or not source:
                    continue  # 无正文/无来源的块没法当证据引用，跳过
                # locator 带 chunk 号（与 MCPProvider.kb 同口径）：同一文件的
                # 两个块是两份证据，纯 source 名会让跨子问题去重误吞
                chunk = item.get("chunk_index")
                locator = source if chunk is None else f"{source}#chunk{chunk}"
                out.append(
                    Evidence(
                        eid="",
                        source_type="kb",
                        title=source[:120],
                        locator=locator,
                        snippet=sanitize_web_text(text),
                    )
                )
            self._kb_breaker.record(True)
            return out
        except Exception:
            self._kb_breaker.record(False)
            raise

    def kb_status(self) -> dict:
        if self._kb_breaker.open:
            raise CircuitOpen("kb 熔断中")
        try:
            data = self._kb_get("/api/kb/status", {})
            self._kb_breaker.record(True)
            return data
        except Exception:
            self._kb_breaker.record(False)
            raise
