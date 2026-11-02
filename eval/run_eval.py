"""评测 runner（D10）：10 题 × {single,multi} × N 次的批量执行与对照表。

用法（项目根目录，env 里的 PROVIDER_MODE 决定取证链路）：
  /opt/anaconda3/envs/agent-research/bin/python -m eval.run_eval \
      --modes single,multi --repeat 2 --out eval/results

模式定义（PLAN §六）：single=MAX_ROUNDS 1（无回环），multi=MAX_ROUNDS 3
（追问回环开）；两种模式用同一个 EVIDENCE_CHAR_BUDGET（等证据预算）。

夹具题（conflict/injection/private）强制走 ScriptedProvider——防御类指标
必须钉在同一把尺子上；web 题走 .env 配置的真实链路。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

from app.config import get_settings
from app.contracts import ListSink
from app.graph import nodes as nodes_mod
from app.graph.build import run_research
from eval.fixtures import ScriptedProvider
from eval.judge import judge_support, sample_citation_pairs
from eval.metrics import (
    citation_existence,
    conflict_disclosure,
    coverage,
    injection_survived,
    latency_stats,
)
from eval.questions import QUESTIONS

FIXTURE_KINDS = {"private", "conflict", "injection"}


def run_one(q, mode: str) -> dict:
    """单题单模式单次：返回该 run 的全部指标与现场（report 全文留档）。

    模式切换经进程环境 + settings 缓存清理实现；【必须还原】——评测
    runner 若在 pytest 进程里跑（夹具端到端测试），泄漏的 MAX_ROUNDS
    会毒化后续所有测试的回环行为（本测试套件真实踩过）。
    """
    original_rounds = os.environ.get("MAX_ROUNDS", "")
    original_planner = os.environ.get("PLANNER_MODE", "")
    original_ds = os.environ.get("DEEPSEEK_API_KEY", "")
    original_mem = os.environ.get("MEMORY_ENABLED", "")
    os.environ["MAX_ROUNDS"] = "1" if mode == "single" else "3"
    if q.kind in FIXTURE_KINDS:
        # 夹具题整条链路钉死：planner 也走 template（单子问题直通）——
        # LLM 拆解出的子问题措辞不可控，剧本证据匹配不上，指标就不是同一把尺子
        os.environ["PLANNER_MODE"] = "template"
    # 免费保障（用户指示 2026-11-02）：评测全程禁用 DeepSeek（计费），
    # chat 自动落到商汤公测免费档；恢复后仅影响本进程，.env 原样。
    # 夹具跑分也不得写跨 run 记忆（MEMORY_ENABLED 隔离），否则污染真实研究的召回
    os.environ.pop("DEEPSEEK_API_KEY", None)
    os.environ.pop("MEMORY_ENABLED", None)
    get_settings.cache_clear()
    try:
        return _run_one_inner(q, mode)
    finally:
        for name, original in (
            ("MAX_ROUNDS", original_rounds),
            ("PLANNER_MODE", original_planner),
            ("DEEPSEEK_API_KEY", original_ds),
            ("MEMORY_ENABLED", original_mem),
        ):
            if original:
                os.environ[name] = original
            else:
                os.environ.pop(name, None)
        get_settings.cache_clear()


def _run_one_inner(q, mode: str) -> dict:
    fixture = q.kind in FIXTURE_KINDS
    if fixture:
        scripted = ScriptedProvider()
        nodes_mod.get_provider, orig = (lambda: scripted), nodes_mod.get_provider
        get_settings.cache_clear()
    started = time.time()
    sink = ListSink()
    try:
        final = run_research(q.question, focus=q.focus, sink=sink)
    finally:
        if fixture:
            nodes_mod.get_provider = orig  # type: ignore[assignment]
            get_settings.cache_clear()

    report = str(final.get("report", ""))
    citation = final.get("citation_report", {})
    events = sink.events
    flagged = [
        e.payload.get("metric", "")
        for e in events
        if e.type == "warning" and e.payload.get("where") == "cross-source"
    ]
    return {
        "qid": q.qid,
        "kind": q.kind,
        "mode": mode,
        "wall_seconds": round(time.time() - started, 2),
        "round": final.get("round"),
        "evidence_count": len(final.get("evidence", [])),
        "errors": final.get("errors", []),
        "citation_existence": citation_existence(citation),
        "coverage": coverage(final.get("subq_evidence", {})),
        "conflict_disclosure": conflict_disclosure(flagged, q.gold.get("conflicts", [])),
        "flagged_metrics": flagged,
        "injection_survived": injection_survived(report, q.gold.get("canary", "")),
        "latency": latency_stats(events),
        "tokens": final.get("token_usage", {}),
        "cited": report.count("[E"),
        "report": report,
        # 裁判的输入：eid → 片段（片段有 500 字上限，整包很小）
        "evidence_pairs": {ev.eid: ev.snippet for ev in final.get("evidence", [])},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="研知 评测 runner（D10）")
    parser.add_argument("--modes", default="single,multi")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0=全部）")
    parser.add_argument(
        "--kinds", default="", help="只跑指定类型（逗号分隔，如 private,conflict,injection）"
    )
    parser.add_argument("--out", default="eval/results")
    parser.add_argument("--skip-judge", action="store_true", help="跳过 LLM 裁判（省时）")
    args = parser.parse_args()

    questions = QUESTIONS[: args.limit or None]
    if args.kinds:
        wanted = {k.strip() for k in args.kinds.split(",")}
        questions = [q for q in questions if q.kind in wanted]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / f"runs_{stamp}.jsonl"
    md_path = out_dir / f"summary_{stamp}.md"

    all_runs: list[dict] = []
    for q in questions:
        for mode in args.modes.split(","):
            for repeat in range(args.repeat):
                run = run_one(q, mode)
                run["repeat"] = repeat
                all_runs.append(run)
                exist = run["citation_existence"]
                exist_s = f"{exist:.2f}" if exist is not None else "N/A"
                print(
                    f"  {q.qid}({q.kind}) {mode}#{repeat + 1}: "
                    f"wall={run['wall_seconds']}s round={run['round']} "
                    f"证据={run['evidence_count']} 引用存在率={exist_s}",
                    flush=True,
                )
                with jsonl_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(run, ensure_ascii=False) + "\n")

    if not args.skip_judge:
        # 引用支撑率：抽样 ≤5 对/题，智谱裁判（Key 缺席返回 None=N/A）
        for run in all_runs:
            pairs = sample_citation_pairs(run["report"], run["evidence_pairs"])
            run["support_rate"] = judge_support(pairs) if pairs else None
        # 裁判结果回写 jsonl：run 循环先写文件、裁判后算——不回写就丢了（实测踩过）
        with jsonl_path.open("w", encoding="utf-8") as f:
            for run in all_runs:
                f.write(json.dumps(run, ensure_ascii=False) + "\n")

    # ---- 对照表（markdown）----
    def _fmt(v) -> str:
        return f"{v:.2f}" if isinstance(v, float) else ("N/A" if v is None else str(v))

    lines = [
        f"# 评测对照表（{stamp}）",
        "",
        f"- 协议：{len(questions)} 题 × {args.modes} × {args.repeat} 次；"
        f"PROVIDER_MODE={get_settings().provider_mode}",
        "- N/A 不进均值；防御类指标（冲突披露/注入存活）为夹具题确定性结果",
        "",
        "| qid | kind | mode#rep | wall | p50/p95 | round | 证据 | 存在率 | 覆盖 | 冲突 | 注入 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in all_runs:
        lat = r.get("latency") or {}
        lat_s = (
            f"{lat.get('p50', 0) * 1000:.0f}/{lat.get('p95', 0) * 1000:.0f}"
            if lat
            else "N/A"
        )
        lines.append(
            f"| {r['qid']} | {r['kind']} | {r['mode']}#{r['repeat'] + 1} "
            f"| {r['wall_seconds']} | {lat_s} | {r['round']} | {r['evidence_count']} "
            f"| {_fmt(r['citation_existence'])} | {_fmt(r['coverage'])} "
            f"| {_fmt(r['conflict_disclosure'])} | {_fmt(r['injection_survived'])} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n完成：{jsonl_path}\n       {md_path}")


if __name__ == "__main__":
    main()
