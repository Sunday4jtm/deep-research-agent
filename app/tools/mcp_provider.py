"""MCPProvider：经 MCP stdio 连老仓库 mcp/ 下的两个 Server（A=web，B=kb）。

为什么用 mcp SDK 原生 ClientSession 而不是 langchain-mcp-adapters：
spike①（P0）已用 adapters 验证过生态兼容性，但产品路径要的是
"同步 ToolProvider 协议 + 结构化错误分流"——adapters 产出的是 LangChain
Tool（ainvoke 后还得从文本解析回 Evidence），"异步桥接同步"这段必写的
代码一点没省，反而多一层抽象。直接用 SDK 的 ClientSession + 后台事件
循环，链路最短、调试时栈最浅。答辩口径：两种都验过，选原生因为适配层薄。

为什么只开一个后台事件循环线程：stdio_client 与 ClientSession 都是
async context manager，必须常驻同一个 loop 才能保持子进程与会话存活；
sync 方法经 run_coroutine_threadsafe 提交。若"每次调用 asyncio.run
重连子进程"，一轮取证 2×breadth 次工具调用就要拉起/杀掉子进程同样多次。

失败语义（对齐 DirectProvider，PLAN §3.4）：
- 工具回 {"ok": False, "unsupported": True}（配置没开）与 {"ok": False}
  （真故障）都转成 WebSearchError，由 retriever 分源容错记入 errors；
- 连续失败 3 次熔断（复用 direct 的 _Breaker，同一口径）；
- MCP_WEB_CMD 未配置 = mcp 模式不可用，报错文案直接指路 .env。
"""
from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import logging
import os
import shlex
import threading

from app.config import get_settings
from app.tools.base import Evidence
from app.tools.direct import CircuitOpen, WebSearchError, _Breaker
from app.tools.sanitize import sanitize_web_text

logger = logging.getLogger(__name__)

INIT_TIMEOUT = 30.0  # 子进程拉起 + MCP initialize 的上限
CALL_TIMEOUT = 60.0  # 单次工具调用上限（搜索/抓取远端本身有自己的超时）


class _McpSession:
    """常驻后台事件循环的 stdio MCP 会话（懒启动；进程退出子进程随行）。"""

    def __init__(self, command: list[str]) -> None:
        self._command = command
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._session = None  # ClientSession（避免顶部 import mcp——离线测试不该碰 SDK）

    def _ensure(self) -> None:
        """懒初始化：第一次真正调用时才拉起子进程（fake/未配置路径零开销）。"""
        with self._lock:
            if self._loop is not None:
                return
            if not self._command:
                raise WebSearchError(
                    "MCP 未配置：请在 .env 设置 MCP_WEB_CMD"
                    "（老仓库 mcp/mcp_web_server.py 的启动命令）"
                )
            self._loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=self._loop.run_forever, daemon=True, name="mcp-provider-loop"
            )
            thread.start()
            start = asyncio.run_coroutine_threadsafe(self._start(), self._loop)
            try:
                start.result(timeout=INIT_TIMEOUT)
            except Exception:
                self._teardown()
                raise
            logger.info("MCP Server 已连接（stdio）：%s", self._command)
            # 进程退出时显式收口（close 幂等；单例下整个进程只注册一次）。
            # 不注册的话，daemon loop 线程死在解释器关闭的哪个阶段不可控，
            # 子进程回收全靠 stdin EOF 兜底——多一道确定性的闸。
            atexit.register(self.close)

    async def _start(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stack = contextlib.AsyncExitStack()
        params = StdioServerParameters(
            command=self._command[0], args=self._command[1:], env=self._server_env()
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

    # 转发给 Server 子进程的环境变量白名单。为什么必须显式：mcp SDK 默认只给
    # 子进程一份最小环境（get_default_environment：PATH/HOME 等），父进程的
    # 其他变量一概不传——D7 实测 KB_CHROMA_DIR 就这样被静默丢掉，Server B
    # 回退默认目录检索了真私有库。密钥（DEEPSEEK_API_KEY 等）刻意不在白名单：
    # Server 的上游 Key 由它自己从老仓库 .env 读取（load_dotenv），不随进程传播。
    ENV_PASSTHROUGH = ("KB_CHROMA_DIR",)

    @staticmethod
    def _server_env() -> dict:
        from mcp.client.stdio import get_default_environment

        env = dict(get_default_environment())
        for key in _McpSession.ENV_PASSTHROUGH:
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    def _teardown(self) -> None:
        """回滚半初始化状态：失败后下次调用能重新尝试，而不是卡死在坏会话上。

        为什么必须等 aclose 真正跑完再停 loop：fire-and-forget 提交后紧接着
        stop loop，清理协程可能永远轮不到执行，子进程就泄漏了。等 5s 上限
        是防 aclose 卡死拖住调用方——超时后子进程由 stdin EOF 兜底退出。
        """
        if self._stack is not None and self._loop is not None:
            fut = asyncio.run_coroutine_threadsafe(self._stack.aclose(), self._loop)
            try:
                fut.result(timeout=5)
            except Exception:
                logger.warning("MCP 会话清理未按时完成（子进程将由 stdin EOF 兜底退出）")
        self._stack = None
        self._session = None
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop = None

    def close(self) -> None:
        """显式收口：关会话与子进程、停后台 loop。幂等，atexit 与测试都能调。

        工厂单例生命周期下这是 Streamlit 长驻进程里唯一的确定性回收出口——
        进程活着时 daemon 线程永远不会自己死（评审 P0-1 泄漏的根因）。
        """
        with self._lock:
            if self._loop is None:
                return  # 未初始化或已关闭：无害 no-op
            self._teardown()

    def call(self, name: str, arguments: dict) -> dict:
        """同步调用远端工具；工具约定都回 {"ok": ...} 结构（见 mcp_web_server.py）。"""
        self._ensure()
        fut = asyncio.run_coroutine_threadsafe(self._call_async(name, arguments), self._loop)
        try:
            return fut.result(timeout=CALL_TIMEOUT)
        except TimeoutError as exc:
            raise WebSearchError(f"MCP 工具 {name} 超时（>{CALL_TIMEOUT}s）") from exc

    async def _call_async(self, name: str, arguments: dict) -> dict:
        result = await self._session.call_tool(name, arguments)
        if result.isError:
            raise WebSearchError(f"MCP 工具 {name} 报告错误")
        # FastMCP 对 dict 返回值同时给 structuredContent 和 JSON 文本；
        # 解析文本 JSON 最稳（不赌各 SDK 版本 structuredContent 字段的行为）
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                try:
                    return json.loads(text)
                except ValueError:
                    return {"ok": True, "raw": text}
        raise WebSearchError(f"MCP 工具 {name} 返回空内容")


class MCPProvider:
    """PROVIDER_MODE=mcp 时由 get_provider() 返回。

    D7 起挂两个 Server：A=web（老仓库 web_research）、B=kb（老仓库
    search_hybrid）。两个会话/两个熔断器完全独立——公网挂了不影响
    私有源取证，反之亦然（PLAN §3.4 分源容错在 provider 层的对应物）。
    """

    def __init__(self) -> None:
        self._web: _McpSession | None = None
        self._kb: _McpSession | None = None
        self._web_breaker = _Breaker("mcp-web")
        self._kb_breaker = _Breaker("mcp-kb")

    def _get_web(self) -> _McpSession:
        if self._web is None:
            self._web = _McpSession(shlex.split(get_settings().mcp_web_command))
        return self._web

    def _get_kb(self) -> _McpSession:
        if self._kb is None:
            self._kb = _McpSession(shlex.split(get_settings().mcp_kb_command))
        return self._kb

    def close(self) -> None:
        """关停两个 MCP 会话与子进程（工具工厂单例的收尾出口；未连接时无害）。"""
        for session in (self._web, self._kb):
            if session is not None:
                session.close()
        self._web = None
        self._kb = None

    # ---- web_search / web_fetch：MCP Server A（老仓库 web_research 的包装）----
    def web_search(self, query: str, k: int = 3) -> list[Evidence]:
        if self._web_breaker.open:
            raise CircuitOpen(f"mcp-web 熔断中（连续失败 {self._web_breaker.fails} 次）")
        try:
            payload = self._get_web().call("web_search", {"query": query, "count": k})
            if not payload.get("ok"):
                # unsupported（老仓库没配 DeepSeek Key）与真故障都让
                # retriever 记 errors——文案区分开，排障时不用猜
                raise WebSearchError(f"MCP web_search 失败：{payload.get('error', '')}")
            out = self._to_evidence(payload.get("results") or [], k)
            self._web_breaker.record(True)
            return out
        except Exception:
            self._web_breaker.record(False)
            raise

    @staticmethod
    def _to_evidence(items: list, k: int) -> list[Evidence]:
        """与 DirectProvider 同口径：无 URL 的结果无法被引用核验，跳过。"""
        out: list[Evidence] = []
        for item in items[:k]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            title = str(item.get("title") or "").strip() or url
            out.append(
                Evidence(
                    eid="",
                    source_type="web",
                    title=title[:120],
                    locator=url,
                    snippet=sanitize_web_text(str(item.get("snippet") or "")),
                )
            )
        return out

    def web_fetch(self, url: str) -> str:
        if self._web_breaker.open:
            raise CircuitOpen("mcp-web 熔断中")
        try:
            payload = self._get_web().call("web_fetch", {"url": url})
            if not payload.get("ok"):
                raise WebSearchError(f"MCP web_fetch 失败：{payload.get('error', '')}")
            content = sanitize_web_text(str(payload.get("content") or ""))
            if not content:
                raise WebSearchError("抓取结果没有正文")
            self._web_breaker.record(True)
            return content
        except Exception:
            self._web_breaker.record(False)
            raise

    # ---- kb_search / kb_status：MCP Server B（老仓库 search_hybrid 的包装，D7）----
    def kb_search(self, query: str, k: int = 3) -> list[Evidence]:
        if self._kb_breaker.open:
            raise CircuitOpen(f"mcp-kb 熔断中（连续失败 {self._kb_breaker.fails} 次）")
        try:
            payload = self._get_kb().call("kb_search", {"query": query, "k": k})
            if not payload.get("ok"):
                raise WebSearchError(f"MCP kb_search 失败：{payload.get('error', '')}")
            out = self._kb_to_evidence(payload.get("results") or [], k)
            self._kb_breaker.record(True)
            return out
        except Exception:
            self._kb_breaker.record(False)
            raise

    @staticmethod
    def _kb_to_evidence(items: list, k: int) -> list[Evidence]:
        """与 DirectProvider.kb 同口径；locator 带 chunk 号（见下）。"""
        out: list[Evidence] = []
        for item in items[:k]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "")
            source = str(item.get("source") or "")
            if not text or not source:
                continue  # 无正文/无来源的块没法当证据引用，跳过
            # locator 带 chunk 号：同一文件的第 3 块和第 7 块是两份独立证据，
            # 若用纯 source 名做 locator，retriever 的跨子问题去重会把它们
            # 当成重复证据丢掉（web 侧 URL 天然唯一，kb 侧必须自己造唯一键）
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
        return out

    def kb_status(self) -> dict:
        if self._kb_breaker.open:
            raise CircuitOpen("mcp-kb 熔断中")
        try:
            payload = self._get_kb().call("kb_status", {})
            if not payload.get("ok"):
                raise WebSearchError(f"MCP kb_status 失败：{payload.get('error', '')}")
            self._kb_breaker.record(True)
            return {
                "total_chunks": payload.get("total_chunks"),
                "sources": payload.get("sources") or [],
            }
        except Exception:
            self._kb_breaker.record(False)
            raise
