"""Streamlit 事件流 XSS 回归（评审 P0-1）：公网可控字段必须转义后才进 HTML。

frontend/app.py 可裸 import（无 Streamlit 运行时只有无害 warning），直接对
_event_line 做纯函数断言；攻击载荷原型 = 恶意页面 <title> 里藏的 HTML。
"""
from __future__ import annotations

from app.contracts import AgentEvent
from frontend.app import _event_line


def test_evidence_title必须转义():
    """公网搜索结果的 title 原样拼进 unsafe_allow_html 的攻击链已被评审走通。"""
    ev = AgentEvent(
        "evidence_added",
        {
            "eid": "E1",
            "source_type": "web",
            "locator": "https://attacker.example/x",
            "title": "<img src=x onerror=alert(1)>",
        },
    )
    line = _event_line(ev)
    assert "<img" not in line, "原始 HTML 不得进入事件行"
    assert "&lt;img" in line, "必须以实体形式上屏"


def test_通用事件payload必须转义():
    """warning/error 事件的 payload 含异常文本与 URL，同样是外部可控内容。"""
    ev = AgentEvent("warning", {"error": "<script>alert(1)</script>"})
    line = _event_line(ev)
    assert "<script>" not in line
    assert "&lt;script&gt;" in line


def test_eid字段也转义():
    """eid 今天是服务端生成的 E#；转义是为了契约演进后不漏防任何一个字段。"""
    ev = AgentEvent(
        "evidence_added",
        {"eid": 'E1"><b', "source_type": "kb", "locator": "x", "title": "正常标题"},
    )
    # 断言转义形态存在：裸串 "><b" 会撞上模板自身的 class="ev"><b> 标记
    assert "E1&quot;&gt;&lt;b" in _event_line(ev)
