"""失败语义测试（PLAN §3.4）：单源失败不拖垮整轮，部分报告照常产出。"""
from __future__ import annotations

import pytest

from app.contracts import (
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_RESEARCH_STARTED,
    EVENT_WARNING,
    ListSink,
)
from app.graph.build import run_research
from app.tools.base import Evidence


class _HalfBrokenProvider:
    """web 全断、kb 正常——模拟公网不可用但私有库在线的场景。"""

    def web_search(self, query: str, k: int = 3) -> list[Evidence]:
        raise RuntimeError("公网不可用")

    def web_fetch(self, url: str) -> str:
        raise RuntimeError("公网不可用")

    def kb_search(self, query: str, k: int = 3) -> list[Evidence]:
        return [
            Evidence(
                eid="",
                source_type="kb",
                title="本地知识库",
                locator="demo_notes/ml-basics.md",
                snippet="过拟合的缓解手段包括正则化、Dropout 与早停。",
            )
        ]

    def kb_status(self) -> dict:
        return {"provider": "stub"}


def test_web_failure_degrades_to_kb_and_records_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.graph.nodes.get_provider", lambda: _HalfBrokenProvider())
    sink = ListSink()
    final = run_research("什么是过拟合", sink=sink)

    assert final["errors"], "web 失败必须进 errors"
    assert any("web/" in e for e in final["errors"])
    assert final["evidence"], "kb 证据必须仍然可用"
    assert final["report"], "部分报告照常产出"
    assert not final["gaps"], "kb 证据覆盖了该子问题，不应产生缺口"
    types = [e.type for e in sink.events]
    assert EVENT_WARNING in types
    assert types[-1] == EVENT_DONE, "失败场景也必须以 done 收尾"


def test_total_failure_still_completes_with_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全源失败：零证据 → 确定性 verifier 按"未覆盖"处理。

    已知语义边界（D8 换 LLM verifier 时再精细化）：源失败与证据缺失
    目前都表现为零证据，回环轮次因 attempted 去重而零网络开销，
    最终报告以"未覆盖"披露并由 errors 解释原因——诚实优先。
    """

    class _AllBroken(_HalfBrokenProvider):
        def kb_search(self, query: str, k: int = 3) -> list[Evidence]:
            raise RuntimeError("私有库也不可用")

    from app.config import get_settings

    monkeypatch.setattr("app.graph.nodes.get_provider", lambda: _AllBroken())
    final = run_research("任何问题", sink=ListSink())
    assert not final["evidence"]
    assert final["gaps"], "零证据在确定性 verifier 下按未覆盖处理"
    assert final["round"] == get_settings().max_rounds
    assert len(final["errors"]) >= 2, "web 与 kb 的失败都要记录"
    assert "未覆盖" in final["report"], "报告必须诚实披露"


# ============================================================
# 图执行崩溃的失败语义（评审 P0-3）：error 事件是后台线程里 UI
# 唯一能收到的失败信号，没有它界面就会"线程悄悄死亡、停在研究中"
# ============================================================
def test_节点内异常以error事件收尾并向上抛(monkeypatch: pytest.MonkeyPatch) -> None:
    """节点炸掉不能只靠 raise：事件流必须以 error 收尾，供 UI 判定终态。"""

    def _boom():
        raise ValueError("未知 PROVIDER_MODE：typo（可选 fake | direct | mcp）")

    monkeypatch.setattr("app.graph.nodes.get_provider", _boom)
    sink = ListSink()
    with pytest.raises(ValueError):
        run_research("任意问题", sink=sink)

    types = [e.type for e in sink.events]
    assert types[0] == EVENT_RESEARCH_STARTED
    assert types[-1] == EVENT_ERROR, "崩溃必须以 error 事件收尾，不能无声终止"
    assert "PROVIDER_MODE" in sink.events[-1].payload["msg"]


def test_装配阶段异常同样发error事件(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_research_app 本身炸（如图装配/import 问题）也在 error 语义覆盖内。"""

    def _boom(_sink):
        raise RuntimeError("装配爆炸")

    monkeypatch.setattr("app.graph.build.build_research_app", _boom)
    sink = ListSink()
    with pytest.raises(RuntimeError):
        run_research("任意问题", sink=sink)

    assert [e.type for e in sink.events] == [EVENT_RESEARCH_STARTED, EVENT_ERROR]
    assert "装配爆炸" in sink.events[-1].payload["msg"]
