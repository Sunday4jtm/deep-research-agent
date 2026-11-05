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
# 标签要求首字符是字母/斜杠/感叹号：裸 <[^>]+> 会把 "x < 10 and y > 5"、
# List<String> 这类正常技术文本当成标签剥掉——证据被不可逆损坏且引用核验
# 看不出异常（评审实测：'x < 10 and y > 5' 被洗成 'x   5'）
_TAGS = re.compile(r"<[a-zA-Z/!][^>]{0,199}>")

# 注入特征模式（中英都收）：命中即发 warning 事件，不静默丢弃内容——
# 记录下来既是安全信号，也是 D10 评测"注入防御"指标的观测来源。
# 刻意不收 "system prompt"/"act as"：LLM 技术文的高频正常词，strict 模式下
# 误报会把正常证据隔离出综合 prompt（评审推演的误杀面）；角色扮演类攻击
# 由 "you are now"/"pretend to be" 覆盖
INJECTION_PATTERNS = (
    "ignore previous",
    "ignore above",
    "ignore all previous",
    "ignore prior",
    "disregard",
    "forget previous",
    "forget all",
    "new instructions",
    "you are now",
    "pretend to be",
    "reveal your",
    "忽略以上",
    "忽略之前",
    "忽略上面",
    "忽略先前",
    "无视以上",
    "无视之前",
    "你现在是",
    "你现在是一个",
    "假装你是",
    "必须服从",
    "新的任务",
    "新的指令",
)

# 匹配前双方先删空白：'忽 略 以上'、'ignore all previous' 插空格是最便宜的
# 绕过，先归一化再比（评审 P2-9）。代价是极端情况下英文跨词误命中——
# 本检测只发 warning 不拦截内容，误报代价可接受
_COMPACT_PATTERNS = tuple("".join(p.split()) for p in INJECTION_PATTERNS)

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
    compact = "".join((text or "").lower().split())
    return [
        pattern
        for pattern, compacted in zip(INJECTION_PATTERNS, _COMPACT_PATTERNS, strict=False)
        if compacted in compact
    ]


def strip_injected_lines(text: str) -> tuple[str, int]:
    """strict 模式的报告侧拦截：逐行扫描，命中注入特征的整行剥除。

    为什么整行删而不是只删模式词：注入指令通常占一整句/行，只删特征词
    会留下语义残肢（模型可能已在按该指令的形态输出）。局限（如实）：
    跨行拆写的特征（"忽略"+"以上"分两行）逐行扫描看不到——报告侧选
    "误杀少、可解释"的行粒度，输入侧仍走全文归一化扫描。拦截动作由
    调用方执行（synthesizer._finalize / agent_loop 交付前），本函数保持
    纯函数便于单测。返回 (净化后文本, 剥除行数)。
    """
    if not text:
        return text, 0
    lines = text.splitlines()
    kept = [line for line in lines if not detect_injection(line)]
    removed = len(lines) - len(kept)
    if not removed:
        return text, 0
    return "\n".join(kept), removed
