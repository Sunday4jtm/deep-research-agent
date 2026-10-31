"""测试全局隔离：与老项目同一纪律——测试不烧钱、不需要密钥、不碰网络。

PROVIDER_MODE 强制 fake：任何测试都不应该走到真实 provider；
将来如果误加了真实调用，这里会让它立刻暴露而不是悄悄联网。
"""
from __future__ import annotations

import os

os.environ["PROVIDER_MODE"] = "fake"
# 防止本机 .env 里真实 Key 让某个测试"意外走通网络"（宁可测试失败也不静默联网）
os.environ.setdefault("PYTEST_OFFLINE", "1")
