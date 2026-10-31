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
