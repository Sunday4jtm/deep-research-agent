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
from app.tools import get_provider
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
    """切到 mcp 模式并清 get_settings 缓存；teardown 再清一次防泄漏到别的测试。"""
    monkeypatch.setenv("PROVIDER_MODE", "mcp")
    get_settings.cache_clear()
    yield
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
    command = provider._get_session()._command
    assert command == ["/opt/envs/ai_study/bin/python", "/tmp/a b/server.py"]


def test_未配置MCP_WEB_CMD时报错指路(mcp_mode, monkeypatch):
    monkeypatch.delenv("MCP_WEB_CMD", raising=False)
    get_settings.cache_clear()
    provider = MCPProvider()

    with pytest.raises(WebSearchError) as excinfo:
        provider._get_session().call("web_search", {"query": "x"})

    assert "MCP_WEB_CMD" in str(excinfo.value)


# ============================================================
# 2. web_search：回包 → Evidence
# ============================================================
def _provider_with_stub(monkeypatch: pytest.MonkeyPatch, payloads: dict) -> MCPProvider:
    provider = MCPProvider()
    stub = _StubSession(payloads)
    monkeypatch.setattr(provider, "_session", stub)
    monkeypatch.setattr(
        MCPProvider, "_get_session", lambda self: stub
    )
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
# 4. kb 侧：D7 前显式不可用
# ============================================================
def test_kb_search保持显式NotImplementedError():
    """kb 走 MCP Server B（D7）；提前半实现会制造"能跑但没数据"的假象。"""
    provider = MCPProvider()
    with pytest.raises(NotImplementedError):
        provider.kb_search("q", k=3)
    with pytest.raises(NotImplementedError):
        provider.kb_status()


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
