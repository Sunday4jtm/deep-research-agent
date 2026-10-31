"""网页内容清洗（PLAN §7.1）：不可信内容=数据，不是指令。

为什么在 provider 边界清洗：进 Evidence 的任何文本都可能藏恶意指令，
清洗越靠近入口，后续所有节点（verifier/synthesizer/UI）越安全。
分隔符包裹只是纵深防御的一层、可被绕过，不能单独依赖（调研结论）；
真正的防线是"清洗 + 架构级最小权限 + 报告引用回填"的组合。
"""
from __future__ import annotations

import re

# 零宽字符/方向控制符：隐藏指令的常见载体，直接删除
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_TAGS = re.compile(r"<[^>]{1,200}>")

# 注入特征模式（中英都收）：命中即发 warning 事件，不静默丢弃内容——
# 记录下来既是安全信号，也是 D10 评测"注入防御"指标的观测来源
INJECTION_PATTERNS = (
    "ignore previous",
    "ignore above",
    "ignore all previous",
    "disregard",
    "system prompt",
    "忽略以上",
    "忽略之前",
    "忽略上面",
    "你现在是",
    "必须服从",
)

DEFAULT_MAX_CHARS = 4000


def sanitize_web_text(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """删零宽字符 → 删注释/脚本 → 剥标签 → 压缩空行 → 截断。"""
    cleaned = _ZERO_WIDTH.sub("", text or "")
    cleaned = _HTML_COMMENT.sub(" ", cleaned)
    cleaned = _SCRIPT.sub(" ", cleaned)
    cleaned = _TAGS.sub(" ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned[:max_chars]


def detect_injection(text: str) -> list[str]:
    """返回命中的注入特征模式（空列表=未命中）。只报告不拦截。"""
    lowered = (text or "").lower()
    return [pattern for pattern in INJECTION_PATTERNS if pattern in lowered]
