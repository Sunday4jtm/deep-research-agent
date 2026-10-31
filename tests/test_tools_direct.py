"""DirectProvider 离线测试：mock requests.post，不碰网络。

覆盖三件事：响应解析（DeepSeek/智谱两种形态）、SSRF 字面校验、
连续失败熔断。清洗效果在 test_sanitize 已覆盖，这里只验管道衔接。
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.tools import direct as direct_mod
from app.tools.direct import CircuitOpen, DirectProvider, WebSearchError, validate_public_url


class _FakeResp:
    def __init__(self, data: dict) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._data


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch):
    """重试退避在测试里零等待——否则熔断测试要熬 9 秒。"""
    monkeypatch.setattr(direct_mod.time, "sleep", lambda _s: None)


def _install_http(monkeypatch: pytest.MonkeyPatch, responder) -> None:
    """把 requests.Session 换成响应器驱动的假会话。

    _post_with_retry 用 Session + trust_env 两段式自愈（D4 实测本机
    证书链问题），所以测试必须 patch Session 而不是 requests.post。
    """

    class _FakeSession:
        def __init__(self) -> None:
            self.trust_env = True

        def __enter__(self) -> _FakeSession:
            return self

        def __exit__(self, *args) -> bool:
            return False

        def post(self, url, json=None, headers=None, timeout=None):
            return responder(url, json, headers, timeout)

    monkeypatch.setattr(direct_mod.requests, "Session", _FakeSession)


def _with_keys(monkeypatch: pytest.MonkeyPatch, deepseek: str = "", zhipu: str = "") -> None:
    if deepseek:
        monkeypatch.setenv("DEEPSEEK_API_KEY", deepseek)
    else:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    if zhipu:
        monkeypatch.setenv("ZHIPU_API_KEY", zhipu)
    else:
        monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    get_settings.cache_clear()


def test_deepseek_search_parse_and_sanitize(
    monkeypatch: pytest.MonkeyPatch, no_sleep
) -> None:
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        return _FakeResp(
            {
                "results": [
                    {"title": "结果A", "url": "https://x.com/a", "snippet": "<b>正文</b>"},
                    {"title": "无URL", "url": "", "snippet": "只有摘要"},
                ]
            }
        )

    _install_http(monkeypatch, fake_post)
    _with_keys(monkeypatch, deepseek="sk-test")
    provider = DirectProvider()
    evs = provider.web_search("查询", 3)
    assert len(evs) == 1, "无 URL 的条目应被跳过"
    assert evs[0].locator == "https://x.com/a"
    assert "<b>" not in evs[0].snippet and "正文" in evs[0].snippet
    assert captured["url"].endswith("/v1/tools/search")
    get_settings.cache_clear()


def test_zhipu_search_parse_standalone_api(
    monkeypatch: pytest.MonkeyPatch, no_sleep
) -> None:
    """智谱独立 web_search API 的响应形态（D4 实测定端点，mock 验解析）。"""
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return _FakeResp(
            {
                "search_result": [
                    {"title": "智谱结果", "link": "https://z.cn/1", "content": "内容"}
                ]
            }
        )

    _install_http(monkeypatch, fake_post)
    _with_keys(monkeypatch, zhipu="zk-test")
    evs = DirectProvider().web_search("查询", 3)
    assert len(evs) == 1
    assert evs[0].locator == "https://z.cn/1"
    assert evs[0].title == "智谱结果"
    get_settings.cache_clear()


def test_web_search_without_any_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_keys(monkeypatch)
    with pytest.raises(WebSearchError, match="没有可用的"):
        DirectProvider().web_search("查询")
    get_settings.cache_clear()


def test_deepseek_breaker_opens_after_three_failures(
    monkeypatch: pytest.MonkeyPatch, no_sleep
) -> None:
    def broken_post(url, json=None, headers=None, timeout=None):
        raise ConnectionError("断网")

    _install_http(monkeypatch, broken_post)
    _with_keys(monkeypatch, deepseek="sk-test")
    provider = DirectProvider()
    for _ in range(3):
        with pytest.raises(WebSearchError):
            provider._deepseek_search("q", 3)
    # 第 4 次必须直接熔断，不再碰网络（否则又重试 2 轮）
    with pytest.raises(CircuitOpen):
        provider._deepseek_search("q", 3)
    get_settings.cache_clear()


def test_validate_public_url_rejects_internal() -> None:
    for bad in (
        "http://localhost/x",
        "http://127.0.0.1/x",
        "http://192.168.1.1/x",
        "http://10.0.0.1/x",
        "ftp://example.com/x",
        "http://user:pass@example.com/x",
        "http://host.local/x",
    ):
        with pytest.raises(WebSearchError):
            validate_public_url(bad)
    assert validate_public_url("https://example.com/a") == "https://example.com/a"


# ============================================================
# kb 侧（D6）：HTTP 直连老仓库 /api/kb/search（Q4 direct 兜底）
# ============================================================
class _FakeGetResp:
    def __init__(self, status_code: int = 200, data: dict | None = None) -> None:
        self.status_code = status_code
        self._data = data or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import http

            raise direct_mod.requests.HTTPError(
                f"{self.status_code} {http.HTTPStatus(self.status_code).phrase}"
            )

    def json(self) -> dict:
        return self._data


def test_kb_search映射为kb证据(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, headers=headers)
        return _FakeGetResp(
            data={
                "query": "q",
                "results": [
                    {"text": "知识库片段", "source": "笔记A.md", "chunk_index": 0},
                    {"text": "", "source": "空块.md"},  # 无正文 → 跳过
                ],
            }
        )

    monkeypatch.setattr(direct_mod.requests, "get", fake_get)
    monkeypatch.setenv("KB_API_BASE", "http://127.0.0.1:8000")
    monkeypatch.setenv("KB_API_TOKEN", "tok-1")
    get_settings.cache_clear()
    provider = DirectProvider()

    hits = provider.kb_search("什么是 RAG", k=3)

    assert len(hits) == 1
    assert hits[0].source_type == "kb"
    assert hits[0].locator == "笔记A.md"
    assert hits[0].title == "笔记A.md"
    assert seen["url"] == "http://127.0.0.1:8000/api/kb/search"
    assert seen["params"]["q"] == "什么是 RAG"
    assert seen["headers"]["X-API-Token"] == "tok-1"
    get_settings.cache_clear()


def test_kb_search令牌错翻译成人话(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        direct_mod.requests, "get", lambda *a, **k: _FakeGetResp(status_code=401)
    )
    get_settings.cache_clear()
    provider = DirectProvider()

    with pytest.raises(WebSearchError) as excinfo:
        provider.kb_search("q", k=3)

    assert "APP_AUTH_TOKEN" in str(excinfo.value)
    get_settings.cache_clear()


def test_kb_search后端不可达(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_get(*args, **kwargs):
        raise direct_mod.requests.ConnectionError("refused")

    monkeypatch.setattr(direct_mod.requests, "get", broken_get)
    get_settings.cache_clear()
    provider = DirectProvider()

    with pytest.raises(WebSearchError) as excinfo:
        provider.kb_search("q", k=3)

    assert "老仓库后端不可达" in str(excinfo.value)
    get_settings.cache_clear()


def test_kb_search连续失败熔断(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_get(*args, **kwargs):
        raise direct_mod.requests.ConnectionError("refused")

    monkeypatch.setattr(direct_mod.requests, "get", broken_get)
    get_settings.cache_clear()
    provider = DirectProvider()
    for _ in range(3):
        with pytest.raises(WebSearchError):
            provider.kb_search("q", k=3)

    with pytest.raises(CircuitOpen):
        provider.kb_search("q", k=3)
    get_settings.cache_clear()
