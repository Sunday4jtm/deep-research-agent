"""实调测试（slow）：验证三级降级 ① 结构化输出真实走通。

默认被 `-m "not slow"` 排除；手动运行：
    /opt/anaconda3/envs/agent-research/bin/python -m pytest -m slow -q
消耗：1~2 次 glm-4-flash 免费调用。
"""
from __future__ import annotations

import pytest

from app.planner import plan_subquestions


@pytest.mark.slow
def test_live_planner_structured_output() -> None:
    subs, usage, _level = plan_subquestions(
        "bge 和 m3e 在中文检索场景怎么选？", focus="本地 CPU 部署"
    )
    assert 1 <= len(subs) <= 4
    assert all(isinstance(s, str) and s.strip() for s in subs)
    assert usage["planner"] >= 1
