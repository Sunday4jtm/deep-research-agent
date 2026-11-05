"""citation pass：报告生成后的独立引用校验（借鉴 Anthropic 多 Agent 研究系统）。

为什么独立成 pass 而不是只在提示词里"要求带引用"：提示词是请求，
校验是检查——模型完全可能输出证据列表里不存在的编号。这里的产出
"引用存在率"（可映射引用/总引用）同时是 D10 评测的核心指标，
运行时顺手产出，评测时不用二次解析。
"""
from __future__ import annotations

import re

# 全角括号/逗号归一 + 忽略大小写：弱模型不按格式回复是常态（PLAN 记录），
# 漏检形态 = 幻觉引用漏判 + 分母失真（评审 P2：全角［E9］/小写 [e8] 之前全部不可见）
_FULLWIDTH = str.maketrans({"［": "[", "］": "]", "，": ","})
CITATION_RE = re.compile(r"\[E(\d+)\]", re.IGNORECASE)
# 并列形态 "[E1, E2]"：单条正则只认「] 紧跟数字」，并列串会整段漏检
_CITE_GROUP_RE = re.compile(r"\[(\s*E\s*\d+(?:\s*,\s*E\s*\d+)+)\]", re.IGNORECASE)
_REF_IN_GROUP_RE = re.compile(r"E\s*(\d+)", re.IGNORECASE)


def check_citations(report: str, evidence_ids: set[str] | list[str]) -> dict:
    """统计报告中的 [E#] 引用并与证据列表对账。

    返回字段即 D10 评测口径：
    - refs_total：引用出现总次数（重复引用照算；并列 [E1, E2] 按两条计）
    - refs_unique：去重后引用数
    - mapped：其中能映射到真实证据的数量
    - unmapped：映射不上的编号（模型幻觉引用的直接证据），展示截断到 10 条
    - unmapped_total：unmapped 的完整计数（契约字段）——旧实现只有截断列表，
      两端 UI 拿 len(截断列表) 当计数，幻觉 15 个编号时显示 10，核心卖点在最
      严重的失败场景系统性谎报（评审 P1）
    - evidence_total：证据总数
    """
    ids = set(evidence_ids)
    text = (report or "").translate(_FULLWIDTH)
    refs = [f"E{n}" for n in CITATION_RE.findall(text)]
    refs += [
        f"E{n}"
        for group in _CITE_GROUP_RE.findall(text)
        for n in _REF_IN_GROUP_RE.findall(group)
    ]
    unique = sorted(set(refs), key=lambda x: int(x[1:]))
    unmapped = sorted(set(refs) - ids, key=lambda x: int(x[1:]))
    return {
        "refs_total": len(refs),
        "refs_unique": len(unique),
        "mapped": len(unique) - len(unmapped),
        "unmapped": unmapped[:10],
        "unmapped_total": len(unmapped),
        "evidence_total": len(ids),
    }
