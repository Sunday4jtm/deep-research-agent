"""图节点工厂：每个节点是"闭包住 sink 的函数"。

为什么用工厂而不是裸函数：事件出口（EventSink）由装配层注入，
节点本身保持可测试——离线测试注入 ListSink 断言事件序列。

D2 阶段全部是**确定性骨架实现**（PLAN D-4：先验证业务逻辑，再上 LLM）；
D3~D5 逐个替换为 LLM 版本，骨架版保留为离线测试的基准线。
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from app import memory
from app.config import get_settings
from app.contracts import (
    EVENT_DONE,
    EVENT_EVIDENCE_ADDED,
    EVENT_NODE_FINISHED,
    EVENT_NODE_STARTED,
    EVENT_REPORT_DELTA,
    EVENT_ROUND_STARTED,
    EVENT_THOUGHT,
    EVENT_WARNING,
    AgentEvent,
    EventSink,
)
from app.tools import get_provider
from app.tools.base import Evidence, canonicalize_locator
from app.tools.sanitize import detect_injection

logger = logging.getLogger(__name__)

# 检索并行度：每轮最多同时发起 4 路（子问题×源），覆盖常见 4 子问题场景且
# 不会把 provider 的限流窗口打爆；再大收益递减、风险上升
_FETCH_WORKERS = 4

# FakeProvider 的测试钩子：查询带此前缀时返回空结果，
# 用于离线验证"证据缺失 → 追问回环 → 熔断"路径（保持确定性）。
EMPTY_QUERY_PREFIX = "EMPTY::"


def make_plan_node(sink: EventSink):
    """拆解研究问题。D2 骨架：单子问题直通；D3 换 LLM 拆解 + 三级降级。

    v2.3：规划前召回跨 run 记忆注入 prompt（过往研究的 learnings 作参考
    素材，明确标注"勿照抄"防旧结论污染），并发出第一条思考链事件。"""

    def plan(state):
        sink.emit(AgentEvent(EVENT_NODE_STARTED, {"node": "planner"}))
        settings = get_settings()
        memories = memory.recall(state["question"])
        if memories:
            text = "；".join(
                f"「{m['question'][:30]}」→ {'；'.join(m['learnings'][:2])}"
                for m in memories
            )
            sink.emit(
                AgentEvent(
                    EVENT_THOUGHT,
                    {
                        "stage": "memory",
                        "text": f"召回 {len(memories)} 条过往研究记忆：{text[:150]}",
                    },
                )
            )
        if settings.planner_mode == "llm":
            # 延迟 import：template 模式（离线测试）不应该碰 LLM 模块
            from app.planner import plan_subquestions

            sub_questions, usage = plan_subquestions(
                state["question"], state.get("focus", ""), memories
            )
        else:
            from app.planner import template_plan

            sub_questions = template_plan(state["question"])
            usage = {"planner": 0}
        sink.emit(
            AgentEvent(
                EVENT_THOUGHT,
                {
                    "stage": "plan",
                    "text": "拆解为 "
                    + str(len(sub_questions))
                    + " 个子问题："
                    + "；".join(q[:28] for q in sub_questions[:4]),
                },
            )
        )
        sink.emit(
            AgentEvent(
                EVENT_NODE_FINISHED,
                {
                    "node": "planner",
                    "subq": len(sub_questions),
                    "mode": settings.planner_mode,
                },
            )
        )
        # 初始化字段：让后续节点永远面对"完整状态"，不处理缺键
        return {
            "question": state["question"],
            "focus": state.get("focus", ""),
            "sub_questions": sub_questions,
            "attempted_queries": [],
            "learnings": [],
            "evidence": [],
            "subq_evidence": {},
            "conflicts": [],
            "gaps": [],
            "round": 0,
            "token_usage": usage,
            "errors": [],
        }

    return plan


def make_retrieve_node(sink: EventSink):
    """取证节点：对每个子问题查公网 + 知识库，统一编号入册。

    去重依据 attempted_queries：同一查询绝不重复提交（借鉴 dzhng/deep-research）。
    """

    def retrieve(state):
        sink.emit(AgentEvent(EVENT_NODE_STARTED, {"node": "retriever"}))
        provider = get_provider()
        settings = get_settings()
        evidence: list[Evidence] = list(state.get("evidence", []))
        # 去重键用规范化后的 locator（utUtm/http(s)/尾斜杠差异不该骗过去重）；
        # Evidence.locator 本身保持原样——展示与引用核验要的是原始 URL（评审 P2-11）
        seen_locators = {canonicalize_locator(ev.locator) for ev in evidence}
        attempted: list[str] = list(state.get("attempted_queries", []))
        subq_evidence: dict[str, list[str]] = dict(state.get("subq_evidence", {}))
        round_no = int(state.get("round", 0)) + 1

        sink.emit(AgentEvent(EVENT_ROUND_STARTED, {"round": round_no}))
        k = max(1, settings.breadth)
        errors: list[str] = list(state.get("errors", []))

        # 先定本轮要取证的子问题（gap 允许跨轮重试），再把全部
        # (子问题 × 源) 一次性提交线程池并行跑——trace 数据（D9，
        # assets/_check/d9_timing.py）显示检索证据间隔 avg 114ms、
        # 串行等待随 子问题数×breadth 线性恶化；并行化后墙钟=最慢一路。
        # 消费仍按原顺序：事件流与 eid 编号保持确定性，变的只是"等"的方式。
        eligible: list[str] = []
        query_for: dict[str, str] = {}
        rewritten = dict(state.get("rewritten", {}))
        for subq in state.get("sub_questions", []):
            if subq in attempted:
                # 跨轮去重：查过【且有证据】的不再重复花钱；零证据的 gap 例外——
                # 反思器（v2.3）已为它生成改写查询，用新查询重试=循环真转；
                # 无改写时退回原查询（瞬时失败重试语义，D8）。
                if subq_evidence.get(subq):
                    continue
                query_for[subq] = rewritten.get(subq, subq)
            else:
                attempted.append(subq)
                query_for[subq] = subq
            eligible.append(subq)
        for subq in eligible:
            # gap 重试的改写查询也入册（attempted 只按原子问题名去重，
            # 查询文本照记——排障时能看到"实际发出去的是什么"）
            if query_for[subq] != subq:
                attempted.append(query_for[subq])

        future_by_key: dict[tuple[str, str], Any] = {}
        with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
            for subq in eligible:
                # 默认参数绑定 subq/query：避免闭包晚绑定陷阱（ruff B023 同款关注点）
                future_by_key[(subq, "web")] = pool.submit(
                    lambda q=query_for[subq]: provider.web_search(q, k=k)
                )
                future_by_key[(subq, "kb")] = pool.submit(
                    lambda q=query_for[subq]: provider.kb_search(q, k=k)
                )

        for subq in eligible:
            eids = subq_evidence.get(subq, [])
            # 分源容错（PLAN §3.4）：单源失败只记 warning + errors，
            # 不拖垮整轮——kb 未接入（D6 前）也不影响 web 取证
            for source in ("web", "kb"):
                try:
                    hits = future_by_key[(subq, source)].result()
                except Exception as exc:  # NotImplementedError/网络/熔断一律兜住
                    errors.append(f"{source}/{subq[:40]}: {type(exc).__name__}")
                    sink.emit(
                        AgentEvent(
                            EVENT_WARNING,
                            {
                                "where": source,
                                "query": subq[:60],
                                "error": f"{type(exc).__name__}: {exc}"[:120],
                            },
                        )
                    )
                    continue
                for hit in hits:
                    canonical = canonicalize_locator(hit.locator)
                    if canonical in seen_locators:
                        # 跨子问题去重：规范化后相同视为同一来源——
                        # 不同子问题常搜到同一篇热门文章（还常带不同 utm
                        # 参数），重复证据会稀释报告并烧 token
                        # （D4 实调中 E8/E12 撞车后修复；P2-11 升级为规范化比对）
                        continue
                    seen_locators.add(canonical)
                    flags = detect_injection(hit.snippet)
                    if flags:
                        # 只报告不拦截：内容仍作为"数据"进入证据，
                        # 真正的隔离靠 D5 报告提示词的数据/指令分离
                        sink.emit(
                            AgentEvent(
                                EVENT_WARNING,
                                {
                                    "where": "injection-scan",
                                    "patterns": flags[:5],
                                    "locator": hit.locator[:80],
                                },
                            )
                        )
                    eid = f"E{len(evidence) + 1}"
                    ev = replace(hit, eid=eid)  # Evidence 是 frozen dataclass
                    evidence.append(ev)
                    eids.append(eid)
                    sink.emit(
                        AgentEvent(
                            EVENT_EVIDENCE_ADDED,
                            {
                                "eid": ev.eid,
                                "source_type": ev.source_type,
                                "title": ev.title,
                                "locator": ev.locator,
                            },
                        )
                    )
            subq_evidence[subq] = eids

        sink.emit(
            AgentEvent(
                EVENT_NODE_FINISHED,
                {"node": "retriever", "round": round_no, "evidence": len(evidence)},
            )
        )
        return {
            "evidence": evidence,
            "attempted_queries": attempted,
            "subq_evidence": subq_evidence,
            "round": round_no,
            "errors": errors,
        }

    return retrieve


_CLAIM_RE = re.compile(r"(?P<ctx>[\u4e00-\u9fffA-Za-z]{1,8})[\s:：]*(?P<num>\d+(?:\.\d+)?%?)")


def _claims(text: str) -> dict[str, dict[str, str]]:
    """抽"指标上下文 → {数值: 展示窗口}"：冲突比较的是同名指标的取值，
    不是裸数字——"BM25" 里的 25、"GPT-4" 里的 4 这类模型名数字
    必须挂在字母上下文下，才不会被当成两边的"口径"互相污染。

    展示窗口（数值前 10 字）是给"冲突披露"指标用的：ctx 可能是 "k取"
    这种字母+中文混排碎片，窗口里才有 "RRF 常数 k 取 60" 的完整指标词，
    gold 对照（包含匹配）才命得住——c2 真跑实测的教训。
    """
    claims: dict[str, dict[str, str]] = {}
    for m in _CLAIM_RE.finditer(text):
        window = text[max(0, m.start() - 10): m.end()].strip()
        claims.setdefault(m.group("ctx"), {})[m.group("num")] = window
    return claims


def _find_conflict(kb_text: str, web_text: str) -> tuple[str, str, str] | None:
    """返回 (展示窗口, kb 值, web 值)：同名指标在两源取值集合不同即冲突。"""
    kb = _claims(kb_text)
    web = _claims(web_text)
    for ctx, kb_vals in kb.items():
        web_vals = web.get(ctx)
        if not web_vals:
            continue
        kb_nums = set(kb_vals)
        if kb_nums == set(web_vals):
            continue  # 同名指标取值一致，不算分歧
        # 确定性取一个两源不同的数值对：kb 侧优先取"web 没有的值"
        num = sorted(kb_nums - set(web_vals) or kb_nums)[0]
        return kb_vals[num], num, sorted(web_vals)[0]
    return None


def make_verify_node(sink: EventSink):
    """验证节点：gaps（零证据子问题 → 追问回环）+ 跨源指标分歧标记。

    冲突检测是【启发式】而不是语义裁决（如实定位）：同一子问题下 kb 与
    公网证据出现"同名指标、不同数值"（如两边都说"准确率"但一个 90% 一
    个 95%）时才标记——多半是统计口径/时间点不同，报告需要向读者披露
    而不是默默采信一边。与注入扫描同哲学：只报告不拦截（PLAN §7.1）。
    D10 的 gold conflict 夹具会检验这个启发式的召回。
    """

    def verify(state):
        sink.emit(AgentEvent(EVENT_NODE_STARTED, {"node": "verifier"}))
        subq_evidence = state.get("subq_evidence", {})
        evidence = state.get("evidence", [])
        by_eid = {ev.eid: ev for ev in evidence}
        gaps = [q for q, eids in subq_evidence.items() if not eids]

        conflicts: list[dict[str, str]] = []
        for subq, eids in subq_evidence.items():
            hits = [by_eid[eid] for eid in eids if eid in by_eid]
            kb_hits = [ev for ev in hits if ev.source_type == "kb"]
            web_hits = [ev for ev in hits if ev.source_type == "web"]
            if not kb_hits or not web_hits:
                continue  # 单源无对照，谈不上"跨源分歧"
            # 跨源合并指标口径后两两比对（一个子问题一条冲突，避免刷屏）
            kb_text = " ".join(ev.snippet for ev in kb_hits)
            web_text = " ".join(ev.snippet for ev in web_hits)
            found = _find_conflict(kb_text, web_text)
            if found is None:
                continue
            metric, kb_val, web_val = found
            conflicts.append(
                {"query": subq, "metric": metric, "kb": kb_val, "web": web_val}
            )
            sink.emit(
                AgentEvent(
                    EVENT_WARNING,
                    {
                        "where": "cross-source",
                        "query": subq[:60],
                        "metric": metric,
                        "kb": kb_val,
                        "web": web_val,
                    },
                )
            )

        # ---- LLM 反思（v2.3 思考链核心）：有 gap 且还有回环预算时，
        # 让模型"看一眼已有证据"产出两类思考——learnings（本轮沉淀）与
        # gap 子问题的改写查询（下一轮用新查询而不是原句重试）。
        # 失败/关闭 → 静默回退为"原查询重试"（D8 语义），循环不断。
        settings = get_settings()
        learnings = list(state.get("learnings", []))
        rewritten = dict(state.get("rewritten", {}))
        if (
            settings.reflect_mode == "llm"
            and gaps
            and int(state.get("round", 0)) < settings.max_rounds
        ):
            try:
                learned, rewritten = _reflect(state, gaps, evidence)
                learnings.extend(learned)
                if learned or rewritten:
                    sink.emit(
                        AgentEvent(
                            EVENT_THOUGHT,
                            {
                                "stage": "reflect",
                                "text": f"沉淀 {len(learned)} 条；改写 {len(rewritten)} 个查询："
                                + "；".join(
                                    f"「{q[:24]}→{nq[:24]}」" for q, nq in rewritten.items()
                                )[:160],
                            },
                        )
                    )
            except Exception as exc:  # noqa: BLE001 —— 反思失败回退 D8 语义
                logger.warning("LLM 反思失败，回退原查询重试：%s", exc)

        sink.emit(
            AgentEvent(
                EVENT_NODE_FINISHED,
                {"node": "verifier", "gaps": len(gaps), "conflicts": len(conflicts)},
            )
        )
        return {
            "gaps": gaps,
            "conflicts": conflicts,
            "learnings": learnings,
            "rewritten": rewritten,
        }

    return verify


_REFLECT_PROMPT = """你是研究 Agent 的反思器。研究问题：{question}

已查证据要点：
{digest}

以下子问题查了但没有拿到证据：
{gaps}

请输出 JSON（不要多余文字）：
{{"learnings": ["要点，≤40字"],
  "rewritten": [{{"q": "原子问题", "new_query": "改写后的新查询，换角度换关键词"}}]}}
learnings 只写证据里站得住的结论；new_query 必须与原问题措辞明显不同。"""


def _reflect(state, gaps, evidence):
    """LLM 反思：返回 (新增 learnings, 改写查询 dict)。异常向上抛由调用方兜底。"""
    digest = "\n".join(
        f"- [{ev.eid}|{ev.source_type}] {ev.snippet[:110]}" for ev in evidence[:10]
    ) or "（本轮无证据）"
    prompt = _REFLECT_PROMPT.format(
        question=state.get("question", "")[:120],
        digest=digest[:2400],
        gaps="\n".join("- " + g[:60] for g in gaps),
    )
    import json as _json

    from app.llm import invoke_with_fallback

    _, resp = invoke_with_fallback([{"role": "user", "content": prompt}])
    # 不能用 planner.extract_json：它钉死 sub_questions 形状，反思载荷会被
    # 静默丢成 None（实测踩过）——这里做自己的花括号抢救
    m = re.search(r"\{.*\}", str(resp.content), re.DOTALL)
    try:
        data = _json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    learned = [str(x)[:48] for x in (data.get("learnings") or []) if str(x).strip()][:5]
    rewritten: dict[str, str] = {}
    for item in data.get("rewritten") or []:
        if isinstance(item, dict) and item.get("q") and item.get("new_query"):
            rewritten[str(item["q"])] = str(item["new_query"])[:80]
    return learned, rewritten


def make_synth_node(sink: EventSink):
    """综合节点：LLM 版带引用报告 + citation pass（D5）；模板模式作离线基准。"""

    def synthesize(state):
        sink.emit(AgentEvent(EVENT_NODE_STARTED, {"node": "synthesizer"}))
        settings = get_settings()
        if settings.synth_mode == "llm":
            # 延迟 import：模板模式（离线测试）不碰 LLM 模块
            from app.synthesizer import synthesize_report

            report, usage, citation = synthesize_report(state, sink)
        else:
            from app.citation import check_citations
            from app.synthesizer import template_report

            report = template_report(state)
            usage = {"synthesizer": 0}
            citation = check_citations(
                report, {ev.eid for ev in state.get("evidence", [])}
            )
            sink.emit(AgentEvent(EVENT_REPORT_DELTA, {"text": report}))
        sink.emit(AgentEvent(EVENT_NODE_FINISHED, {"node": "synthesizer"}))
        merged_usage = dict(state.get("token_usage", {}))
        merged_usage.update(usage)
        # done 必须是终止事件：三端 UI 以它判定"研究结束"；
        # token_usage 随 done 送达（D5 验收项：UI 展示成本）
        sink.emit(
            AgentEvent(
                EVENT_DONE, {"citation_report": citation, "token_usage": merged_usage}
            )
        )
        return {
            "report": report,
            "citation_report": citation,
            "token_usage": merged_usage,
        }

    return synthesize
