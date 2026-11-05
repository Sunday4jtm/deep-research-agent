"""清理 data/memory/runs.jsonl 的测试污染条目（评审 P0-2 配套工具）。

背景：dfec94a 的 MEMORY_ENABLED 隔离实现反向生效（run_eval pop 变量 →
config 默认 "1" = 评测期间记忆开启），2026-11-02 23:19 起每次 pytest/评测
都往真实跨 run 记忆里写测试假问题，实测累计 122 条、learnings 全空。
隔离修复后跑本脚本清存量，避免真实研究把测试噪声召回进 prompt。

用法（项目根目录）：
  /opt/anaconda3/envs/agent-research/bin/python scripts/clean_memory.py --all

两种模式：
  --all  整档清空（先备份）。2026-11-05 人工核验依据：现存 122 条的 locator
         全部是 example.com/.example 假域或评测夹具、EMPTY:: 测试钩子，
         learnings 全空、时间戳全部落在 pytest 批次时刻——文件里没有一条
         真实研究记忆，整档清空 + 备份是最干净的处置。
  默认   只删命中测试特征签名的条目（保守模式，供以后误写入时增量清理）：
         locator 含 example 假域 / question 以 EMPTY:: 开头 / question
         命中 eval 题集原文。

安全网：任何删除都会先把原文件复制为 runs.jsonl.bak-<时间戳>（同目录，
data/ 已 gitignore），清错可整档回滚。
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

RUNS_FILE = Path(__file__).resolve().parents[1] / "data" / "memory" / "runs.jsonl"

# 保守模式的测试特征：example 系保留假域 + FakeProvider 测试钩子前缀
TEST_LOCATOR_MARKS = (".example", "example.com")
TEST_QUESTION_PREFIX = "EMPTY::"


def _eval_questions() -> set[str]:
    """eval 题集的问题原文：夹具跑分的记忆条目按 question 命中。"""
    try:
        from eval.questions import QUESTIONS

        return {q.question for q in QUESTIONS}
    except Exception:  # noqa: BLE001 —— 工具脚本不因 import 失败而崩
        return set()


def main() -> None:
    parser = argparse.ArgumentParser(description="清理跨 run 记忆里的测试污染条目")
    parser.add_argument("--all", action="store_true", help="整档清空（先备份）")
    args = parser.parse_args()

    if not RUNS_FILE.exists():
        print(f"没有记忆文件：{RUNS_FILE}")
        return

    lines = RUNS_FILE.read_text(encoding="utf-8").splitlines()
    backup = RUNS_FILE.with_name(f"runs.jsonl.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(RUNS_FILE, backup)

    if args.all:
        kept: list[str] = []
        removed = sum(1 for line in lines if line.strip())
    else:
        questions = _eval_questions()
        kept, removed = [], 0
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                kept.append(line)  # 解析不了的原样保留，不做不可逆的猜测
                continue
            locators = " ".join(str(x) for x in (entry.get("top_locators") or []))
            question = str(entry.get("question", ""))
            polluted = (
                any(mark in locators for mark in TEST_LOCATOR_MARKS)
                or question.startswith(TEST_QUESTION_PREFIX)
                or question in questions
            )
            if polluted:
                removed += 1
            else:
                kept.append(line)

    RUNS_FILE.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    print(f"备份：{backup}")
    print(f"清理完成：删除 {removed} 条，保留 {len(kept)} 条（原文件 {len(lines)} 行）")


if __name__ == "__main__":
    main()
