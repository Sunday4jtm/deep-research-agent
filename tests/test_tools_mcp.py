"""MCPProvider 离线测试：全程桩掉 _McpSession，不拉子进程、不碰 socket。

分两块：
  1. 工厂接线：PROVIDER_MODE=mcp 返回 MCPProvider（get_settings 缓存要清）；
  2. 失败语义与证据映射：工具回包 → Evidence 的同口径转换、
     unsupported/真故障/熔断的分流、kb 侧 D7 前显式不可用。

stdio 握手 + 真子进程的端到端验证不在离线层（属 slow 实调，见
scripts/preflight 后续扩展）；这里守住的是"结构错了当场红"。
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.tools import get_provider, reset_provider_cache
from app.tools.direct import CircuitOpen, WebSearchError
from app.tools.mcp_provider import MCPProvider, _McpSession


class _StubSession:
    """替身 _McpSession：call() 直接吐预置回包，记录调用参数。"""

    def __init__(self, payloads: dict | None = None) -> None:
        self.payloads = payloads or {}
        self.calls: list[tuple[str, dict]] = []

    def call(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        if name not in self.payloads:
            raise WebSearchError(f"桩未配置工具 {name} 的回包")
        payload = self.payloads[name]
        if isinstance(payload, Exception):
            raise payload
        return payload


@pytest.fixture()
def mcp_mode(monkeypatch: pytest.MonkeyPatch):
    """切到 mcp 模式并清两处缓存（settings + provider 单例）；teardown 再清防泄漏。

    get_provider 现在按模式缓存单例（评审 P0-1）：不 reset 的话，
    本 fixture 缓存的 MCPProvider 会带着旧的 MCP_WEB_CMD 活到别的测试。
    """
    monkeypatch.setenv("PROVIDER_MODE", "mcp")
    get_settings.cache_clear()
    reset_provider_cache()
    yield
    reset_provider_cache()
    get_settings.cache_clear()


# ============================================================
# 1. 工厂接线
# ============================================================
def test_mcp模式返回MCPProvider(mcp_mode):
    assert isinstance(get_provider(), MCPProvider)


def test_命令按shlex解析进会话(mcp_mode, monkeypatch):
    monkeypatch.setenv("MCP_WEB_CMD", '/opt/envs/ai_study/bin/python "/tmp/a b/server.py"')
    provider = MCPProvider()
    # 触发懒构建并检查解析结果（不会真的拉子进程——_ensure 只在 call 时执行）
    command = provider._get_web()._command
    assert command == ["/opt/envs/ai_study/bin/python", "/tmp/a b/server.py"]


def test_kb命令同样按shlex解析(mcp_mode, monkeypatch):
    monkeypatch.setenv("MCP_KB_CMD", "/opt/envs/ai_study/bin/python /path/kb_server.py")
    provider = MCPProvider()

    assert provider._get_kb()._command == [
        "/opt/envs/ai_study/bin/python",
        "/path/kb_server.py",
    ]


def test_未配置MCP_WEB_CMD时报错指路(mcp_mode, monkeypatch):
    monkeypatch.delenv("MCP_WEB_CMD", raising=False)
    get_settings.cache_clear()
    provider = MCPProvider()

    with pytest.raises(WebSearchError) as excinfo:
        provider._get_web().call("web_search", {"query": "x"})

    assert "MCP_WEB_CMD" in str(excinfo.value)


# ============================================================
# 2. web_search：回包 → Evidence
# ============================================================
def _provider_with_stub(monkeypatch: pytest.MonkeyPatch, payloads: dict) -> MCPProvider:
    provider = MCPProvider()
    stub = _StubSession(payloads)
    # web/kb 两个入口都指向同一个桩：按 payloads 里配了什么工具就表现成什么
    monkeypatch.setattr(MCPProvider, "_get_web", lambda self: stub)
    monkeypatch.setattr(MCPProvider, "_get_kb", lambda self: stub)
    return provider


def test_搜索回包映射成Evidence且无URL被跳过(monkeypatch):
    provider = _provider_with_stub(
        monkeypatch,
        {
            "web_search": {
                "ok": True,
                "results": [
                    {"title": "A", "url": "https://a.com", "snippet": "片段A"},
                    {"title": "无URL", "url": "", "snippet": "不该出现"},
                ],
            }
        },
    )

    hits = provider.web_search("查询词", k=3)

    assert len(hits) == 1
    assert hits[0].source_type == "web"
    assert hits[0].locator == "https://a.com"
    assert hits[0].snippet == "片段A"


def test_unsupported与真故障都转WebSearchError(monkeypatch):
    provider = _provider_with_stub(
        monkeypatch,
        {
            "web_search": {"ok": False, "unsupported": True, "error": "未启用", "results": []},
        },
    )
    with pytest.raises(WebSearchError):
        provider.web_search("q", k=3)

    provider2 = _provider_with_stub(
        monkeypatch,
        {"web_search": {"ok": False, "unsupported": False, "error": "上游超时", "results": []}},
    )
    with pytest.raises(WebSearchError):
        provider2.web_search("q", k=3)


def test_连续失败3次后熔断(monkeypatch):
    provider = _provider_with_stub(
        monkeypatch,
        {"web_search": {"ok": False, "unsupported": False, "error": "挂了", "results": []}},
    )
    for _ in range(3):
        with pytest.raises(WebSearchError):
            provider.web_search("q", k=3)

    with pytest.raises(CircuitOpen):  # 第 4 次不再真正调用远端
        provider.web_search("q", k=3)


# ============================================================
# 3. web_fetch
# ============================================================
def test_抓取正文被清洗后返回(monkeypatch):
    provider = _provider_with_stub(
        monkeypatch,
        {"web_fetch": {"ok": True, "title": "页", "url": "https://a.com", "content": "正文"}},
    )

    assert provider.web_fetch("https://a.com") == "正文"


def test_空正文与工具失败都报错(monkeypatch):
    provider = _provider_with_stub(
        monkeypatch, {"web_fetch": {"ok": True, "title": "页", "url": "u", "content": ""}}
    )
    with pytest.raises(WebSearchError):
        provider.web_fetch("https://a.com")

    provider2 = _provider_with_stub(
        monkeypatch, {"web_fetch": {"ok": False, "unsupported": False, "error": "拒绝内网"}}
    )
    with pytest.raises(WebSearchError):
        provider2.web_fetch("http://127.0.0.1")


# ============================================================
# 4. kb 侧：MCP Server B（D7）
# ============================================================
def test_kb回包映射成Evidence且locator带chunk号(monkeypatch):
    """同一文件的两个块 = 两份证据：locator 必须带 chunk 号，
    否则 retriever 跨子问题去重会把同文件多块误当重复吞掉。"""
    provider = _provider_with_stub(
        monkeypatch,
        {
            "kb_search": {
                "ok": True,
                "results": [
                    {"text": "第0块", "source": "笔记A.md", "chunk_index": 0},
                    {"text": "第3块", "source": "笔记A.md", "chunk_index": 3},
                    {"text": "", "source": "空块.md", "chunk_index": 0},  # 无正文跳过
                    {"text": "旧块", "source": "旧文档.md"},  # 缺 chunk_index 兼容
                ],
            }
        },
    )

    hits = provider.kb_search("查询词", k=5)

    assert len(hits) == 3
    assert hits[0].source_type == "kb"
    assert hits[0].locator == "笔记A.md#chunk0"
    assert hits[1].locator == "笔记A.md#chunk3"
    assert hits[2].locator == "旧文档.md"
    assert hits[0].title == "笔记A.md"


def test_kb失败转WebSearchError(monkeypatch):
    provider = _provider_with_stub(
        monkeypatch,
        {"kb_search": {"ok": False, "error": "kb_search 失败（详见 Server B stderr 日志）"}},
    )

    with pytest.raises(WebSearchError):
        provider.kb_search("q", k=3)


def test_kb与web熔断互相独立(monkeypatch):
    """公网挂 3 次熔断后，私有源取证必须照常——分源容错的 provider 层保证。"""
    provider = _provider_with_stub(
        monkeypatch,
        {
            "web_search": {"ok": False, "unsupported": False, "error": "挂了", "results": []},
            "kb_search": {
                "ok": True,
                "results": [{"text": "片段", "source": "笔记.md", "chunk_index": 0}],
            },
        },
    )
    for _ in range(3):
        with pytest.raises(WebSearchError):
            provider.web_search("q", k=3)

    with pytest.raises(CircuitOpen):  # web 已熔断
        provider.web_search("q", k=3)

    hits = provider.kb_search("q", k=3)  # kb 完全不受影响
    assert hits[0].locator == "笔记.md#chunk0"


def test_kb_status映射(monkeypatch):
    provider = _provider_with_stub(
        monkeypatch,
        {"kb_status": {"ok": True, "total_chunks": 18, "sources": ["笔记A.md"]}},
    )

    assert provider.kb_status() == {"total_chunks": 18, "sources": ["笔记A.md"]}


# ============================================================
# 6. 子进程环境白名单（D7 实测：SDK 默认最小环境会静默丢掉 KB_CHROMA_DIR）
# ============================================================
def test_环境白名单只转发约定变量(monkeypatch):
    from app.tools.mcp_provider import _McpSession

    monkeypatch.setenv("KB_CHROMA_DIR", "/tmp/chroma_demo")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-不应传播")

    env = _McpSession._server_env()

    assert env["KB_CHROMA_DIR"] == "/tmp/chroma_demo"  # 约定开关必须到达子进程
    assert "DEEPSEEK_API_KEY" not in env  # 密钥不随进程传播（Server 自己读老仓库 .env）


# ============================================================
# 5. _McpSession 契约：初始化失败必须回滚（不能卡死在半初始化态）
# ============================================================
def test_初始化失败后回滚且可重试(monkeypatch):
    """_start 炸掉时 _ensure 必须清场（loop/session 归零）——否则第一次
    子进程拉起失败（比如路径配错）会把 provider 永久卡死在坏状态。"""
    session = _McpSession(["/bin/echo"])

    calls = {"n": 0}

    async def _fail_start(self=None):
        calls["n"] += 1
        raise RuntimeError("子进程拉起失败")

    monkeypatch.setattr(session, "_start", _fail_start)
    with pytest.raises(RuntimeError):
        session._ensure()

    assert session._loop is None and session._session is None  # 已回滚
    # 回滚后允许再次尝试（不抛"已在初始化"之类的卡死错误）
    with pytest.raises(RuntimeError):
        session._ensure()
    assert calls["n"] == 2


# ============================================================
# 7. 单例与收尾（评审 P0-1 修复配套）
# ============================================================
def test_get_provider_mcp模式下返回同一实例(mcp_mode):
    """图每轮 retrieve 都调 get_provider()：不缓存的话每轮拉起 A/B 两个
    子进程且无人回收（D7 后翻倍），direct 侧熔断计数也会随实例清零。"""
    assert get_provider() is get_provider()


def test_close对未初始化会话幂等无害():
    """close 必须能安全走 atexit：未初始化/已关闭都是 no-op，不能抛错。"""
    session = _McpSession(["/bin/echo"])
    session.close()
    session.close()
    assert session._loop is None and session._session is None


def test_provider_close_未连接时无害():
    """工厂 reset 会调 close()：provider 从未真正连接过时不能炸。"""
    MCPProvider().close()
    assert MCPProvider()._web is None and MCPProvider()._kb is None
