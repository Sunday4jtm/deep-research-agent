"""synthesizer 离线测试：假模型驱动流式与降级链，全程不碰网络。"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from app.contracts import EVENT_REPORT_DELTA, EVENT_WARNING, ListSink
from app.synthesizer import synthesize_report, template_report
from app.tools.base import Evidence


def _state() -> dict:
    return {
        "question": "测试问题",
        "evidence": [
            Evidence("E1", "web", "标题一", "https://a.example/1", "片段一"),
            Evidence("E3", "web", "标题三", "https://a.example/3", "片段三"),
        ],
        "gaps": [],
        "errors": [],
    }


def test_template_report_cites_all() -> None:
    report = template_report(_state())
    assert "[E1]" in report and "[E3]" in report
    assert "证据清单" in report


def test_llm_synth_streams_and_checks_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """假模型流式输出：report_delta 逐块发出，citation pass 抓住幻觉引用。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    model = GenericFakeChatModel(
        messages=iter([AIMessage(content="结论一 [E1]。结论二 [E3][E9]。")])
    )
    monkeypatch.setattr("app.synthesizer._model_chain", lambda: [("fake", "u", "m", "k")])
    monkeypatch.setattr("app.synthesizer._build_chat", lambda *a, **k: model)

    sink = ListSink()
    report, usage, citation = synthesize_report(_state(), sink)

    assert "[E1]" in report
    assert citation["mapped"] == 2
    assert citation["unmapped"] == ["E9"], "假模型的幻觉引用必须被对账出来"
    assert citation["evidence_total"] == 2
    deltas = [e for e in sink.events if e.type == EVENT_REPORT_DELTA]
    assert deltas, "流式模式应发出 report_delta"
    assert usage["synthesizer"] >= 1


def test_llm_synth_falls_back_to_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """流式与非流式全失败 → 模板兜底 + warning 事件，绝不向上抛。"""

    class _Broken:
        def stream(self, *args, **kwargs):
            raise RuntimeError("断流")

        def invoke(self, *args, **kwargs):
            raise RuntimeError("断流")

    monkeypatch.setattr("app.synthesizer._model_chain", lambda: [("fake", "u", "m", "k")])
    monkeypatch.setattr("app.synthesizer._build_chat", lambda *a, **k: _Broken())

    sink = ListSink()
    report, _usage, citation = synthesize_report(_state(), sink)

    assert "证据清单" in report, "模板兜底必须产出结构化报告"
    warnings = [e for e in sink.events if e.type == EVENT_WARNING]
    assert warnings and warnings[0].payload["where"] == "synthesizer"
    assert citation["mapped"] == 2, "模板引用全部可映射"
