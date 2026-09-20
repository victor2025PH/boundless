# -*- coding: utf-8 -*-
"""按钮回调拉取器（tools/duty_callback_poll.py）纯函数门禁（实施82 P2）。"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.duty_callback_poll import decide, derive_pull_key, pick_new  # noqa: E402


def test_pull_key_matches_site_derivation():
    # 与官网 bug-callback-store.bugPullKey 同式：sha256 hex 前 32
    tok = "123456:ABC-example"
    assert derive_pull_key(tok) == hashlib.sha256(
        tok.encode()).hexdigest()[:32]
    assert len(derive_pull_key(tok)) == 32
    assert derive_pull_key("") == ""


def test_decide_semantics():
    st, note, alert = decide({"verdict": "y", "username": "skuio"})
    assert st == "verified" and "@skuio" in note and alert is False
    st2, note2, alert2 = decide({"verdict": "n", "first_name": "张三",
                                 "from_id": 42})
    assert st2 == "confirmed" and "张三" in note2
    assert alert2 is True, "「还是不行」必须告警值守——点了没人接比没按钮更伤"
    # 无名用户回落 id
    _, note3, _ = decide({"verdict": "n", "from_id": 42})
    assert "42" in note3


def test_pick_new_watermark_and_order():
    evts = [
        {"ts": 10, "ticket": 1, "verdict": "y"},
        {"ts": 30, "ticket": 3, "verdict": "n"},
        {"ts": 20, "ticket": 2, "verdict": "y"},
        {"ts": 40, "ticket": 0, "verdict": "y"},   # 坏 ticket 剔除
        "junk",
    ]
    got = pick_new(evts, watermark=10)
    assert [e["ticket"] for e in got] == [2, 3], "水位过滤 + 按 ts 升序"
    assert pick_new(evts, watermark=999) == []
