"""FakeProvider：全离线确定性实现（DoD #4 的基石）。

为什么必须确定性：评测与 CI 要可复现，"假"的数据绝不能带随机性——
这直接继承老项目 conftest 的 FakeEmbeddings/StubLLM 设计纪律。
"""
from __future__ import annotations

from app.tools.base import Evidence

# 关键词触发的固定语料；未命中走 _fallback，保证任何查询都有可预期的返回
_FAKE_WEB: dict[str, list[Evidence]] = {
    "bge": [
        Evidence(
            eid="",
            source_type="web",
            title="中文 Embedding 选型：BGE 与 M3E 对比",
            locator="https://example.com/bge-vs-m3e",
            snippet="BAAI/bge-small-zh-v1.5 在 C-MTEB 中文检索任务上优于同规模 M3E，"
            "且社区维护活跃；M3E 在短文本相似度场景表现接近。选型建议以检索召回为主指标。",
        ),
    ],
    "langgraph": [
        Evidence(
            eid="",
            source_type="web",
            title="LangGraph 官方文档：StateGraph 与条件边",
            locator="https://example.com/langgraph",
            snippet="LangGraph 用 StateGraph 组织节点，通过条件边实现循环与分支；"
            "状态是唯一的共享数据通道，节点只读写声明的字段。",
        ),
    ],
}

_FAKE_KB: dict[str, list[Evidence]] = {
    "过拟合": [
        Evidence(
            eid="",
            source_type="kb",
            title="机器学习迷你笔记",
            locator="demo_notes/ml-basics.md",
            snippet="过拟合指模型在训练集表现好、测试集表现差。缓解手段：正则化、"
            "Dropout、早停、数据增强、交叉验证选择超参数。",
        ),
    ],
    "embedding": [
        Evidence(
            eid="",
            source_type="kb",
            title="机器学习迷你笔记",
            locator="demo_notes/ml-basics.md",
            snippet="Embedding 把离散符号映射为稠密向量，语义相近的样本在向量空间距离更近；"
            "检索场景用余弦相似度衡量相关性。",
        ),
    ],
}

_WEB_FALLBACK = Evidence(
    eid="",
    source_type="web",
    title="（固定示例）通用网页结果",
    locator="https://example.com/fallback",
    snippet="这是 FakeProvider 的固定回退片段，用于离线演示与测试。",
)

_KB_FALLBACK = Evidence(
    eid="",
    source_type="kb",
    title="demo_notes/ml-basics.md",
    locator="demo_notes/ml-basics.md",
    snippet="（固定示例）知识库中没有与该查询直接相关的片段。",
)


def _pick(catalog: dict[str, list[Evidence]], fallback: Evidence, query: str) -> list[Evidence]:
    q = query.lower()
    hits = [ev for keyword, evs in catalog.items() if keyword in q for ev in evs]
    return hits or [fallback]


class FakeProvider:
    """确定性假工具：同查询永远同结果，测试可断言、演示可离线。"""

    def web_search(self, query: str, k: int = 3) -> list[Evidence]:
        if query.startswith("EMPTY::"):
            return []  # 测试钩子：验证"证据缺失 → 追问回环"路径
        return _pick(_FAKE_WEB, _WEB_FALLBACK, query)[:k]

    def web_fetch(self, url: str) -> str:
        return f"（FakeProvider 固定正文）页面 {url} 的离线示例内容。"

    def kb_search(self, query: str, k: int = 3) -> list[Evidence]:
        if query.startswith("EMPTY::"):
            return []
        return _pick(_FAKE_KB, _KB_FALLBACK, query)[:k]

    def kb_status(self) -> dict:
        return {"collection": "fake", "chunks": 2, "provider": "fake"}
