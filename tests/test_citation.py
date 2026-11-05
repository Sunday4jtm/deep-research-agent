"""citation pass 离线测试。"""
from __future__ import annotations

from app.citation import check_citations


def test_all_mapped_with_repeats() -> None:
    result = check_citations("x [E1] y [E2] 再引一次 [E1]", {"E1", "E2"})
    assert result["refs_total"] == 3
    assert result["refs_unique"] == 2
    assert result["mapped"] == 2
    assert result["unmapped"] == []


def test_unmapped_detected() -> None:
    """模型幻觉出来的引用编号必须被抓住——引用存在率的分母来源。"""
    result = check_citations("幻觉引用 [E9]", {"E1"})
    assert result["unmapped"] == ["E9"]
    assert result["mapped"] == 0


def test_empty_report() -> None:
    result = check_citations("", {"E1"})
    assert result["refs_total"] == 0
    assert result["evidence_total"] == 1


def test_unmapped_total_完整计数不被截断() -> None:
    """unmapped 列表展示截断到 10，但 unmapped_total 必须是真实数量——
    旧实现 UI 拿 len(截断列表) 当计数：幻觉 17 个编号显示 10（评审 P1）。"""
    ids = {f"E{i}" for i in range(1, 4)}
    report = " ".join(f"[E{n}]" for n in range(10, 27))  # 17 个幻觉编号
    result = check_citations(report, ids)
    assert result["unmapped_total"] == 17
    assert len(result["unmapped"]) == 10  # 展示仍截断


def test_漏检形态_全角小写并列() -> None:
    """弱模型不按格式回复是常态：全角括号/小写 e/并列 [E1, E2] 都算引用。"""
    result = check_citations("真实 [E1] 全角幻觉［E9］小写 [e8] 并列 [E1, E7]", {"E1"})
    assert result["refs_total"] == 5
    assert result["refs_unique"] == 4
    assert result["mapped"] == 1
    assert result["unmapped_total"] == 3
    assert result["unmapped"] == ["E7", "E8", "E9"]
