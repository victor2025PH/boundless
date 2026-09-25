# -*- coding: utf-8 -*-
"""实施53 验收工具 time_consistency_obs 的纯函数门禁。

钉两件事：① 判钟解析（会话绑定 > 平台默认 > 服务器钟，来源如实标注）；
② 错位聚合与出站守卫同一口径（daypart/venue 检出即计数，媒体占位剔除）。
读数口径与拦截口径同源（都走 world_clock_guard 纯函数）——报表与守卫
永远说同一套话。
"""
import os
import time

import pytest

from src.companion.persona_location import resolve_persona_place
from tools.time_consistency_obs import judge_rows, local_hour_for

VAN = resolve_persona_place({"location": "vancouver"})
TPE = resolve_persona_place({"location": "taipei"})


@pytest.fixture(autouse=True)
def _server_clock_is_utc8():
    """生产服务器钟是 UTC+8。CI 容器是 UTC 时 mktime/localtime 会把
    「03:31」读成 UTC，温哥华夏令时落到 20 点而不是正午 12 点。"""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def _ts(y, mo, d, h, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


def test_local_hour_persona_clock_and_server_fallback():
    # 服务器（UTC+8）2026-08-22 03:31 → 温哥华 2026-08-21 12:31（夏令时 -15h）
    ts = _ts(2026, 8, 22, 3, 31)
    h_van, src_van = local_hour_for(ts, VAN)
    assert src_van == "persona:vancouver"
    assert h_van == 12
    h_srv, src_srv = local_hour_for(ts, None)
    assert src_srv == "server"
    assert h_srv == 3


def test_judge_rows_binding_priority_and_detectors():
    ts_night = _ts(2026, 8, 22, 3, 31)   # 服务器/台北深夜；温哥华正午
    rows = [
        # 台北人设（会话绑定）凌晨说在书店 → venue 错位
        {"conversation_id": "telegram:acc:5433982810",
         "text": "我在二手书店看书呢", "ts": ts_night},
        # 温哥华人设（会话绑定）同一时刻说在书店 → 当地正午，合法
        {"conversation_id": "telegram:acc:8762705170",
         "text": "我在二手书店看书呢", "ts": ts_night},
        # 无绑定会话走平台默认（无居住地）→ 服务器钟：深夜说「下午好」→ daypart
        {"conversation_id": "whatsapp:acc:wa1",
         "text": "下午好呀！", "ts": ts_night},
        # 媒体占位剔除
        {"conversation_id": "whatsapp:acc:wa1",
         "text": "[图片] 自拍", "ts": ts_night},
    ]
    out = judge_rows(
        rows,
        conv_bindings={
            "telegram:acc:5433982810": "lin_xiaoyu",
            "telegram:acc:8762705170": "lin_jiaxin",
        },
        platform_defaults={"whatsapp": "gu_jia"},
        persona_places={"lin_xiaoyu": TPE, "lin_jiaxin": VAN, "gu_jia": None},
    )
    assert out["judged"] == 3
    assert out["skipped_placeholder"] == 1
    assert out["violations"] == {"daypart": 1, "venue": 1}
    assert out["by_clock"]["persona:taipei"]["bad"] == 1
    assert out["by_clock"]["persona:vancouver"]["bad"] == 0   # 当地正午书店合法
    assert out["by_clock"]["server"]["bad"] == 1
    tags = {t for s in out["samples"] for t in s["tags"]}
    assert "venue:bookstore" in tags and "daypart:afternoon" in tags


def test_judge_rows_cutoff_windows():
    before = _ts(2026, 8, 20, 3, 0)
    after = _ts(2026, 8, 23, 3, 0)
    cutoff = _ts(2026, 8, 22, 12, 30)
    rows = [
        {"conversation_id": "telegram:acc:x", "text": "我在图书馆自习",
         "ts": before},
        {"conversation_id": "telegram:acc:x", "text": "我在图书馆自习",
         "ts": after},
    ]
    out = judge_rows(
        rows, conv_bindings={}, platform_defaults={}, persona_places={},
        cutoff_ts=cutoff)
    assert out["by_window"]["before"] == {"n": 1, "bad": 1}
    assert out["by_window"]["after"] == {"n": 1, "bad": 1}
