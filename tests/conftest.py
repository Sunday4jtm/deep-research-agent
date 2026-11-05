"""测试全局隔离：与老项目同一纪律——测试不烧钱、不需要密钥、不碰网络。

PROVIDER_MODE 强制 fake：任何测试都不应该走到真实 provider；
将来如果误加了真实调用，这里会让它立刻暴露而不是悄悄联网。
"""
from __future__ import annotations

import os

import pytest

os.environ["PROVIDER_MODE"] = "fake"
# planner 同理：测试永远走确定性模板，LLM 版规划器由 slow 测试单独实调验证
os.environ["PLANNER_MODE"] = "template"
os.environ["SYNTH_MODE"] = "template"
# v2.3：反思（LLM）与跨 run 记忆在测试里全关——确定性 + 不污染 data/memory/
os.environ["REFLECT_MODE"] = "off"
os.environ["MEMORY_ENABLED"] = ""


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    """离线红线的硬守卫：测试进程内任何网络连接尝试当场报错。

    为什么不靠自觉：D4 实测——"清空 Key"的测试漏清了新加的
    SENSENOVA_API_KEY，导致离线测试真的发起了网络调用（被 403
    暴露才发现）。守卫比纪律可靠：宁可测试当场失败，不可静默联网。

    盲区收口（评审 P1）：补 connect_ex（返回码式连接可绕过 raise 型 connect）
    与 getaddrinfo（DNS 解析本身就是真实出网）。
    slow 豁免（评审 P1）：slow 标记的测试按设计就是实调验证（本文件头部
    自述），守卫若覆盖它，唯一的真模型测试永远走模板兜底假绿——CI 只跑
    `-m "not slow"`，离线红线不受影响。
    """
    if request.node.get_closest_marker("slow"):
        return
    import socket

    def _deny(*args, **kwargs):
        raise RuntimeError("测试尝试建立网络连接——离线红线被触碰！")

    monkeypatch.setattr(socket, "create_connection", _deny)
    monkeypatch.setattr(socket.socket, "connect", _deny)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny)
    monkeypatch.setattr(socket, "getaddrinfo", _deny)
