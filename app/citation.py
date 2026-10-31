"""citation pass：报告生成后的独立引用校验（借鉴 Anthropic 多 Agent 研究系统）。

为什么独立成 pass 而不是只在提示词里"要求带引用"：提示词是请求，
校验是检查——模型完全可能输出证据列表里不存在的编号。这里的产出
"引用存在率"（可映射引用/总引用）同时是 D10 评测的核心指标，
运行时顺手产出，评测时不用二次解析。
"""
from __future__ import annotations

import re

CITATION_RE = re.compile(r"\[E(\d+)\]")


def check_citations(report: str, evidence_ids: set[str] | list[str]) -> dict:
    """统计报告中的 [E#] 引用并与证据列表对账。

    返回字段即 D10 评测口径：
    - refs_total：引用出现总次数（重复引用照算）
    - refs_unique：去重后引用数
    - mapped：其中能映射到真实证据的数量
    - unmapped：映射不上的编号（模型幻觉引用的直接证据）
    """
    ids = set(evidence_ids)
    refs = [f"E{n}" for n in CITATION_RE.findall(report or "")]
    unique = sorted(set(refs), key=lambda x: int(x[1:]))
    unmapped = sorted(set(refs) - ids, key=lambda x: int(x[1:]))
    return {
        "refs_total": len(refs),
        "refs_unique": len(unique),
        "mapped": len(unique) - len(unmapped),
        "unmapped": unmapped[:10],
        "evidence_total": len(ids),
    }
