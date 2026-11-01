"""跨 run 记忆（v2.3）：把每次研究的沉淀写入本地，下次研究召回注入。

为什么是 JSONL 而不是向量库：记忆条目很小（问题+learnings+来源清单），
关键词重叠打分足够做"相关过往"的粗召回；引向量库是给"未来版本"留的
升级位（eval/ 夹具与隐私边界先跑通）。

隐私边界：data/memory/ 已 gitignore，只落本机；learnings 里含私有库
发现，因此该目录与 demo 语料一样属于"不出本机"的数据。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from app.config import get_settings

MEMORY_DIR = Path(__file__).resolve().parents[1] / "data" / "memory"
RUNS_FILE = MEMORY_DIR / "runs.jsonl"


def _ngrams(text: str, n: int = 2) -> set[str]:
    """字符 bigram 集合：无分词依赖的轻量相似度口径（中英混排够用）。"""
    cleaned = re.sub(r"\s+", "", text.lower())
    return {cleaned[i : i + n] for i in range(max(len(cleaned) - n + 1, 0))}


def append_run(
    question: str, learnings: list[str], top_locators: list[str], report_chars: int
) -> bool:
    """研究结束后沉淀一条记忆；失败静默（记忆绝不影响主流程）。"""
    if not get_settings().memory_enabled:
        return False
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "question": question,
            "learnings": learnings[:8],
            "top_locators": top_locators[:8],
            "report_chars": report_chars,
        }
        with RUNS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception:  # noqa: BLE001 —— 记忆层兜底边界
        return False


def recall(question: str, k: int = 3) -> list[dict]:
    """召回与本次问题最相关的 k 条过往记忆（bigram 重叠打分）。

    返回 [{question, learnings}]，按相关度降序；无记忆/未开启 → []。
    召回结果只是"参考素材"，注入 prompt 时明确标注，避免旧结论污染新研究。
    """
    if not get_settings().memory_enabled or not RUNS_FILE.exists():
        return []
    try:
        q_grams = _ngrams(question)
        scored: list[tuple[float, dict]] = []
        for line in RUNS_FILE.read_text(encoding="utf-8").splitlines()[-200:]:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            score = len(q_grams & _ngrams(entry.get("question", "")))
            if score:
                scored.append((score, entry))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            {"question": e.get("question", ""), "learnings": e.get("learnings", [])}
            for _, e in scored[:k]
        ]
    except Exception:  # noqa: BLE001
        return []
