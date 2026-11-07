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


def run_one(q, mode: str, allow_billed: bool = False) -> dict:
    """单题单模式单次：返回该 run 的全部指标与现场（report 全文留档）。

    模式切换经进程环境 + settings 缓存清理实现；【必须还原】——评测
    runner 若在 pytest 进程里跑（夹具端到端测试），泄漏的 MAX_ROUNDS
    会毒化后续所有测试的回环行为（本测试套件真实踩过）。
    """
    # 原值快照用 None 表示「原本未设置」：MEMORY_ENABLED 的空串是 conftest 的
    # 禁用哨兵，必须与「变量不存在」区分开——旧实现按 falsy 判断，空串哨兵被
    # 当成未设置再 pop 一次，整个 pytest 进程的隔离就被第一次 run_one 拆掉
    # （评审 P0-2，磁盘实证：runs.jsonl 曾积累 122 条夹具条目）
    touched = (
        "MAX_ROUNDS",
        "PLANNER_MODE",
        "DEEPSEEK_API_KEY",
        "MEMORY_ENABLED",
        "AGENT_MODE",
    )
    original = {name: os.environ.get(name) for name in touched}
    os.environ["MAX_ROUNDS"] = "1" if mode == "single" else "3"
    if q.kind in FIXTURE_KINDS:
        # 夹具题整条链路钉死：planner 也走 template（单子问题直通）——
        # LLM 拆解出的子问题措辞不可控，剧本证据匹配不上，指标就不是同一把尺子
        os.environ["PLANNER_MODE"] = "template"
    # 评测协议只定义在流水线范式上：AGENT_MODE 显式钉死——夹具 patch 的是
    # nodes.get_provider，AGENT_MODE=react 时 provider 解析在 agent_loop
    # 命名空间，ScriptedProvider 会静默失效、夹具题真联网（评审 P1）
    os.environ["AGENT_MODE"] = "research"
    # 免费保障（用户指示 2026-11-02）：评测全程禁用 DeepSeek（计费），
    # chat 自动落到商汤公测免费档；恢复后仅影响本进程，.env 原样。
    # --allow-deepseek 显式豁免（2026-11-07 用户拍板：web/hybrid 4 题跑公网侧，
    # DeepSeek search/fetch ¥5/1000 次成功请求、失败不计费，全程约 ¥1 量级）。
    # 夹具跑分也不得写跨 run 记忆：必须显式置 "0"。为什么不能 pop——config
    # 读 MEMORY_ENABLED 的默认值是 "1"（开启），pop 掉等于把评测期间的记忆
    # 反向打开（评审 P0-2）
    if not allow_billed:
        os.environ.pop("DEEPSEEK_API_KEY", None)
    os.environ["MEMORY_ENABLED"] = "0"
    get_settings.cache_clear()
    try:
        return _run_one_inner(q, mode)
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
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
    parser.add_argument(
        "--allow-deepseek",
        action="store_true",
        help="允许 DeepSeek 计费调用（web/hybrid 题跑公网侧需要；默认 pop Key 免费保障）",
    )
    parser.add_argument("--skip-judge", action="store_true", help="跳过 LLM 裁判（省时）")
    args = parser.parse_args()

    modes = [m.strip() for m in args.modes.split(",")]
    bad_modes = [m for m in modes if m not in {"single", "multi"}]
    if bad_modes:
        # 旧实现对任何非 single 值静默按 multi 跑（评审 P3）：模式语义必须精确
        parser.error(f"--modes 非法值 {bad_modes}（可选 single/multi）")
    known_kinds = {q.kind for q in QUESTIONS}
    wanted = {k.strip() for k in args.kinds.split(",") if k.strip()} if args.kinds else set()
    if wanted:
        unknown = wanted - known_kinds
        if unknown:
            # 旧实现拼错 kind 静默产 0 题空表（评审 P3）
            parser.error(f"--kinds 非法值 {sorted(unknown)}（合法：{sorted(known_kinds)}）")
    questions = QUESTIONS[: args.limit or None]
    if wanted:
        questions = [q for q in questions if q.kind in wanted]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / f"runs_{stamp}.jsonl"
    md_path = out_dir / f"summary_{stamp}.md"

    all_runs: list[dict] = []
    if args.allow_deepseek:
        print(
            "⚠ --allow-deepseek：本次评测允许 DeepSeek 计费调用（仅 web/hybrid 题会真联网）",
            flush=True,
        )
    for q in questions:
        for mode in modes:
            for repeat in range(args.repeat):
                try:
                    run = run_one(q, mode, allow_billed=args.allow_deepseek)
                except Exception as exc:  # noqa: BLE001 —— 单题崩溃不让整批报废
                    # 一次 provider 抖动此前会让后续题目全部不跑、summary 不
                    # 生成（评审 P2）；jsonl 增量写已保住已跑部分，这里补错误行
                    print(
                        f"  {q.qid}({q.kind}) {mode}#{repeat + 1}: 崩溃 "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    all_runs.append(
                        {
                            "qid": q.qid,
                            "kind": q.kind,
                            "mode": mode,
                            "repeat": repeat,
                            "error": f"{type(exc).__name__}: {exc}"[:200],
                        }
                    )
                    continue
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
            if "error" in run:
                run["support_rate"] = None
                continue
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
        if "error" in r:
            lines.append(
                f"| {r['qid']} | {r['kind']} | {r['mode']}#{r['repeat'] + 1} "
                f"| ERROR {r['error'][:60]} |" + " |" * 7
            )
            continue
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
