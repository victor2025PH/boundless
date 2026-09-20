# -*- coding: utf-8 -*-
"""回怼防线效果周读 CLI 门禁（tools/temper_review.py 纯函数核心）。

钉三件事：① 场次归组（间隙切场）与轮数统计；② 求和形态分级与生产纯函数
同口径（detect_insult / is_*apology 直接复用，零口径分叉）；③ 留存只对
「观察窗走完」的场次计率（窗内场次 pending 不掺水）+ cutoff 前后切段。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.temper_review import analyze_fights  # noqa: E402

H = 3600.0


def _rows(cid: str, spec):
    """spec: (direction, text, ts) 简写构造。"""
    return [(cid, d, t, ts) for d, t, ts in spec]


def test_episode_grouping_and_rounds():
    now = 10 * 86400.0
    base = now - 5 * 86400
    rows = _rows("c1", [
        ("in", "你个傻逼", base),
        ("out", "你才傻逼呢", base + 10),
        ("in", "操你妈", base + 60),          # 同场第 2 轮
        ("out", "行啊你", base + 70),
        # 间隙 > 30min → 新一场（注意要用高置信词表能命中的骂法：
        # 「傻逼玩意」裸写不带「你」刻意不在词表内——宁漏勿错）
        ("in", "你个废物", base + 4 * H),
    ])
    rep = analyze_fights(rows, now=now)
    t = rep["total"]
    assert t["episodes"] == 2
    assert t["rounds_total"] == 3
    assert t["rounds_max"] == 2
    assert t["conversations"] == 1
    assert t["replied"] == 1  # 第二场无 AI 回应


def test_deesc_tiers_and_retention_window():
    now = 10 * 86400.0
    old = now - 3 * 86400          # 窗走完：留存可判
    fresh = now - H                # 窗未走完：pending
    rows = _rows("a", [
        ("in", "你个傻逼", old),
        ("out", "你才傻逼", old + 5),
        ("in", "开玩笑的啦", old + 30),        # 敷衍开脱
        ("in", "在吗聊聊", old + 2 * H),       # 24h 内回来了 → retained
    ]) + _rows("b", [
        ("in", "操你妈", old),
        ("in", "对不起我错了", old + 60),      # 真道歉；之后再无入站 → 未留存
    ]) + _rows("c", [
        ("in", "你个废物", fresh),             # 观察窗未走完 → 不计率
    ])
    rep = analyze_fights(rows, now=now)
    d = rep["deesc"]
    assert d["flippant"] == 1 and d["sincere"] == 1 and d["plain"] == 0
    t = rep["total"]
    assert t["episodes"] == 3
    assert t["retention_eligible"] == 2      # c 场 pending 不计
    assert t["retained"] == 1                # 仅 a 场回来过
    assert t["retention_rate"] == 0.5


def test_cutoff_split_pre_post():
    now = 20 * 86400.0
    cutoff = now - 10 * 86400
    pre_t = cutoff - 5 * 86400
    post_t = cutoff + 5 * 86400
    rows = _rows("x", [("in", "你个傻逼", pre_t)]) + _rows(
        "y", [("in", "操你妈", post_t), ("in", "草泥马", post_t + 60)])
    rep = analyze_fights(rows, now=now, cutoff_ts=cutoff)
    assert rep["pre"]["episodes"] == 1 and rep["pre"]["rounds_total"] == 1
    assert rep["post"]["episodes"] == 1 and rep["post"]["rounds_total"] == 2
    assert rep["total"]["episodes"] == 2


def test_normal_chat_produces_zero_episodes():
    now = 86400.0 * 5
    rows = _rows("n", [
        ("in", "今天天气不错", now - 86400),
        ("out", "是呀，出去走走？", now - 86400 + 30),
        ("in", "对不起我来晚了", now - 86400 + 60),   # 日常道歉≠骂战求和
    ])
    rep = analyze_fights(rows, now=now)
    assert rep["total"]["episodes"] == 0
    assert rep["deesc"] == {"sincere": 0, "flippant": 0, "plain": 0}
