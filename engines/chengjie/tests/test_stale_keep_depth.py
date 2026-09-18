"""断层修剪「删 → 标」：keep_stale 随上下文深度档抬（2026-09-18 「脑子有点空」事故）。

最大档从 inbox 取 200 行，``trim_stale_history`` 默认 keep_stale=3 只留 3 条旧话——
客户隔 5 天回来问「还记得我们第一次聊什么吗」，窗口里没有第一次聊的任何内容。
旧消息已带「[N天前]」时间标，多留不会重演「旧话当刚才」；上限交给 prompt 预算裁剪。
"""
from __future__ import annotations

import inspect
import time

from src.ai import context_depth as cd
from src.inbox.persona_reply import trim_stale_history

DAY = 86400.0


def test_stale_keep_msgs_by_tier():
    assert cd.stale_keep_msgs({"ai": {}}, 3) == 3                              # standard 零变化
    assert cd.stale_keep_msgs({"ai": {"context_depth": "standard"}}, 3) == 3
    assert cd.stale_keep_msgs({"ai": {"context_depth": "deep"}}, 3) == 40
    assert cd.stale_keep_msgs({"ai": {"context_depth": "max"}}, 3) == 160
    assert cd.stale_keep_msgs({"ai": {"context_depth": "ultra"}}, 3) == 1000
    # 手调更大的 legacy 不被压
    assert cd.stale_keep_msgs({"ai": {"context_depth": "deep"}}, 100) == 100


def test_stale_keep_follows_session_override_scope():
    cfg = {"ai": {"context_depth": "standard"}}
    with cd.override_scope("max", skip_economy=True):
        assert cd.stale_keep_msgs(cfg, 3) == 160
    assert cd.stale_keep_msgs(cfg, 3) == 3


def _hist(n_old: int, n_new: int, *, now: float):
    rows = []
    for i in range(n_old):
        rows.append({"direction": "in" if i % 2 == 0 else "out",
                     "text": f"旧{i}", "ts": now - 6 * DAY + i * 60})
    for i in range(n_new):
        rows.append({"direction": "in" if i % 2 == 0 else "out",
                     "text": f"新{i}", "ts": now - 600 + i * 60})
    return rows


def test_trim_keeps_more_stale_when_asked_and_labels_all():
    now = time.time()
    rows = _hist(60, 4, now=now)
    default = trim_stale_history(rows, now_ts=now)
    assert len(default) == 3 + 4                       # 旧行为：只留 3 条旧的
    deep = trim_stale_history(rows, now_ts=now, keep_stale=40)
    assert len(deep) == 40 + 4
    old_part = deep[:40]
    assert all(m["text"].startswith("[") and "天前]" in m["text"] for m in old_part)
    assert old_part[0]["text"].endswith("旧20")        # 保留的是最近的 40 条旧话
    assert [m["text"] for m in deep[40:]] == ["新0", "新1", "新2", "新3"]


def test_trim_keep_stale_larger_than_available_keeps_everything():
    now = time.time()
    rows = _hist(10, 2, now=now)
    out = trim_stale_history(rows, now_ts=now, keep_stale=160)
    assert len(out) == 12
    assert sum(1 for m in out if "天前]" in m["text"]) == 10


def test_autodraft_wires_keep_stale_from_depth():
    """autodraft_helpers 必须把深度档的 keep_stale 传给 trim_stale_history（结构契约）。"""
    from src.inbox import autodraft_helpers
    src = inspect.getsource(autodraft_helpers)
    assert "stale_keep_msgs" in src
    assert "trim_stale_history(msgs, keep_stale=" in src
