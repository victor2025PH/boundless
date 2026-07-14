"""每日灵签门禁：十神→能量日映射 / 确定性轮转 / 个性化 vs 通用 / 文案构建。

固定 now_ts=2026-07-12（丁亥日、小暑后）做金标——lunar 干支是历法事实，永不漂移。
"""

from __future__ import annotations

import calendar

import pytest

from src.companion.bazi_engine import bazi_available
from src.companion.bazi_daily import (
    build_daily_card_block,
    daily_card,
    format_daily_card,
    ritual_card_line,
)

pytestmark = pytest.mark.skipif(
    not bazi_available(), reason="lunar_python 未安装")

# 2026-07-12 10:00 本地时区 → 日柱丁亥（已用 lunar 实测）
_TS = calendar.timegm((2026, 7, 12, 2, 0, 0, 0, 0, 0))  # UTC 02:00 = 东八区 10:00


def test_known_day_ganzhi_and_jieqi():
    c = daily_card(seed_key="u1", now_ts=_TS)
    assert c is not None
    assert c["day_ganzhi"] == "丁亥"
    assert c["day_wuxing"] == "火"
    assert c["jieqi"] == "小暑"


def test_personalized_energy_mapping():
    """日主乙（阴木）遇丁日（阴火）：我生同性 → 食神 → 表达日。"""
    c = daily_card(day_master_gan="乙", seed_key="u1", now_ts=_TS)
    assert c["personalized"] is True
    assert c["shishen"] == "食神"
    assert c["energy"] == "express"
    assert c["energy_label"] == "表达日"


def test_generic_when_no_day_master():
    c = daily_card(seed_key="u1", now_ts=_TS)
    assert c["personalized"] is False
    assert c["energy"] == "generic"


def test_deterministic_same_day_same_user():
    a = daily_card(day_master_gan="乙", seed_key="u1", now_ts=_TS)
    b = daily_card(day_master_gan="乙", seed_key="u1", now_ts=_TS + 3600)  # 同日不同时刻
    assert a == b


def test_rotation_differs_across_users_or_days():
    base = daily_card(day_master_gan="乙", seed_key="u1", now_ts=_TS)
    other_user = daily_card(day_master_gan="乙", seed_key="u2", now_ts=_TS)
    next_day = daily_card(day_master_gan="乙", seed_key="u1", now_ts=_TS + 86400)
    # 宜/忌/幸运数字三元组：不同用户或不同日至少有一处轮转（确定性但非千篇一律）
    def sig(c):
        return (c["do"], c["avoid"], c["lucky_number"])
    assert sig(base) != sig(other_user) or sig(base) != sig(next_day)


def test_lucky_color_follows_day_wuxing():
    c = daily_card(seed_key="u1", now_ts=_TS)
    assert c["lucky_color"] in ("红色", "橘色", "粉色")  # 丁=火 → 火色池


def test_no_doom_wording_in_pools():
    """签面吉凶零断言：所有池子不含恐吓性字眼（安全红线的内容层保证）。"""
    from src.companion.bazi_daily import _ENERGY_META
    banned = ("大凶", "凶", "灾", "死", "破财", "血光")
    for meta in _ENERGY_META.values():
        for pool in (meta["do"], meta["avoid"]):
            for item in pool:
                assert not any(b in item for b in banned), item


def test_format_and_block_and_ritual_line():
    c = daily_card(day_master_gan="乙", seed_key="u1", now_ts=_TS)
    txt = format_daily_card(c)
    assert "丁亥" in txt and "表达日" in txt and "宜：" in txt
    blk = build_daily_card_block(c)
    assert "今日灵签" in blk and "不要报「大凶大吉」" in blk
    line = ritual_card_line(c)
    assert "顺手" in line and "表达日" in line and "一句带过" in line


def test_ritual_line_generic_card():
    c = daily_card(seed_key="u1", now_ts=_TS)
    line = ritual_card_line(c)
    assert "火气当值" in line


def test_empty_inputs():
    assert format_daily_card({}) == ""
    assert build_daily_card_block({}) == ""
    assert ritual_card_line({}) == ""
