# -*- coding: utf-8 -*-
"""Q-30 A（#309 #310，2026-09-12）：``resolve_seat_mode`` 缺省 single，multi 只认显式配置或
presence 近 30 分钟 ≥2 个不同坐席在线；设置页「多坐席协作」开关 + 升级一次性清空认领租约。

指令要求的四例：① 1 账号 → single；② 2 账号 1 在线 → single；③ 2 在线 → multi；
④ 显式 multi → multi。其余：窗口边界 / 坏行 / 取不到数据 / 显式 single 压倒 presence /
白名单枚举 / 迁移 SQL / admin 接线 / 模板接线（静态）。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.web.ui_visibility import (
    SEAT_MULTI,
    SEAT_PRESENCE_WINDOW_SEC,
    SEAT_SINGLE,
    count_recent_agents,
    is_multi_seat,
    resolve_seat_mode,
    seat_mode_verdict,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0


def _users(*roles):
    return [{"username": f"u{i}", "role": r, "enabled": 1} for i, r in enumerate(roles)]


def _presence(*agent_ids, ago_sec=60.0):
    return [{"agent_id": a, "last_seen_at": NOW - ago_sec, "status": "online"} for a in agent_ids]


# ── 指令四例 ─────────────────────────────────────────────────────────

def test_case1_one_account_is_single():
    assert resolve_seat_mode({}, _users("master"), _presence("u0"), now=NOW) == SEAT_SINGLE


def test_case2_two_accounts_one_online_is_single():
    """客户机常态：admin + 坐席两个账号、只有一个人在线 → single（旧判据在这里判成 multi，#309 根因）。"""
    assert resolve_seat_mode({}, _users("master", "agent"), _presence("u1"), now=NOW) == SEAT_SINGLE
    assert is_multi_seat({}, _users("master", "agent"), _presence("u1"), now=NOW) is False


def test_case3_two_online_is_multi():
    assert resolve_seat_mode({}, _users("master", "agent"), _presence("u0", "u1"), now=NOW) == SEAT_MULTI


def test_case4_explicit_multi_wins_with_single_presence():
    """显式 multi 行为一字不改：1 个人在线 / 无 presence / 用户表为空都 multi。"""
    cfg = {"ui_visibility": {"seat_mode": "multi"}}
    assert resolve_seat_mode(cfg, _users("master"), _presence("u0"), now=NOW) == SEAT_MULTI
    assert resolve_seat_mode(cfg, None, None, now=NOW) == SEAT_MULTI
    assert resolve_seat_mode(cfg, [], [], now=NOW) == SEAT_MULTI


# ── 边界 ─────────────────────────────────────────────────────────────

def test_no_data_or_broken_presence_is_single():
    assert resolve_seat_mode({}, None, None, now=NOW) == SEAT_SINGLE
    assert resolve_seat_mode(None, None, None, now=NOW) == SEAT_SINGLE
    assert resolve_seat_mode({}, _users("master", "agent", "agent"), None, now=NOW) == SEAT_SINGLE
    assert resolve_seat_mode({}, None, ["garbage", 42, None], now=NOW) == SEAT_SINGLE
    assert resolve_seat_mode({}, None, [{"agent_id": "a", "last_seen_at": "x"},
                                        {"agent_id": "b"}], now=NOW) == SEAT_SINGLE
    # presence 迭代抛异常 → single 而不是崩
    class _Boom:
        def __iter__(self):
            raise RuntimeError("db gone")
    assert resolve_seat_mode({}, None, _Boom(), now=NOW) == SEAT_SINGLE


def test_presence_window_is_30_minutes_and_counts_distinct_agents():
    assert SEAT_PRESENCE_WINDOW_SEC == 1800.0
    inside = _presence("a", ago_sec=SEAT_PRESENCE_WINDOW_SEC - 1)
    outside = _presence("b", ago_sec=SEAT_PRESENCE_WINDOW_SEC + 1)
    assert count_recent_agents(inside + outside, now=NOW) == 1
    assert resolve_seat_mode({}, None, inside + outside, now=NOW) == SEAT_SINGLE
    # 同一 agent 两行（多窗口 / 重复上报）只算一个人
    dup = [{"agent_id": "a", "last_seen_at": NOW - 10}, {"agent_id": "a", "last_seen_at": NOW - 20}]
    assert count_recent_agents(dup, now=NOW) == 1
    # 手选「离开」（status=offline）但刚刚还在 → 仍算「有几个人」
    off = [{"agent_id": "a", "last_seen_at": NOW - 5, "status": "offline"},
           {"agent_id": "b", "last_seen_at": NOW - 5, "status": "online"}]
    assert resolve_seat_mode({}, None, off, now=NOW) == SEAT_MULTI
    # 空 agent_id 行不算
    assert count_recent_agents([{"agent_id": "", "last_seen_at": NOW}], now=NOW) == 0


def test_explicit_single_beats_presence_and_unknown_value_falls_back_to_presence():
    two = _presence("a", "b")
    assert resolve_seat_mode({"ui_visibility": {"seat_mode": "single"}}, None, two, now=NOW) == SEAT_SINGLE
    assert resolve_seat_mode({"ui_visibility": {"seat_mode": "SINGLE "}}, None, two, now=NOW) == SEAT_SINGLE
    assert resolve_seat_mode({"ui_visibility": {"seat_mode": "solo"}}, None, two, now=NOW) == SEAT_MULTI
    assert resolve_seat_mode({"ui_visibility": {"seat_mode": ""}}, None, _presence("a"), now=NOW) == SEAT_SINGLE


def test_default_now_uses_wall_clock():
    fresh = [{"agent_id": "a", "last_seen_at": time.time() - 5},
             {"agent_id": "b", "last_seen_at": time.time() - 5}]
    assert resolve_seat_mode({}, None, fresh) == SEAT_MULTI
    stale = [{"agent_id": "a", "last_seen_at": 1_700_000_000.0},
             {"agent_id": "b", "last_seen_at": 1_700_000_000.0}]
    assert resolve_seat_mode({}, None, stale) == SEAT_SINGLE


def test_verdict_reports_source_for_settings_card():
    v = seat_mode_verdict({}, _presence("a"), now=NOW)
    assert v == {"mode": "single", "source": "default", "agents_recent": 1, "window_min": 30}
    v = seat_mode_verdict({}, _presence("a", "b"), now=NOW)
    assert v["mode"] == "multi" and v["source"] == "presence" and v["agents_recent"] == 2
    v = seat_mode_verdict({"ui_visibility": {"seat_mode": "multi"}}, _presence("a"), now=NOW)
    assert v["mode"] == "multi" and v["source"] == "explicit" and v["agents_recent"] == 1
    v = seat_mode_verdict({"ui_visibility": {"seat_mode": "single"}}, _presence("a", "b"), now=NOW)
    assert v["mode"] == "single" and v["source"] == "explicit"
    assert seat_mode_verdict(None, None)["mode"] == "single"


# ── 设置页白名单 ─────────────────────────────────────────────────────

def test_settings_whitelist_has_seat_mode_enum_synced_with_ui_visibility():
    from src.inbox.reply_pacing_settings import FIELDS, SEAT_MODE_CHOICES, sanitize_patch

    spec = FIELDS["ui_visibility.seat_mode"]
    assert spec["type"] == "enum" and spec["default"] == "" and spec["hot"] is True
    assert tuple(spec["choices"]) == SEAT_MODE_CHOICES == ("", SEAT_SINGLE, SEAT_MULTI)
    clean, errors = sanitize_patch({"ui_visibility.seat_mode": "multi"})
    assert not errors and clean == {"ui_visibility.seat_mode": "multi"}
    clean, errors = sanitize_patch({"ui_visibility.seat_mode": ""})
    assert not errors and clean == {"ui_visibility.seat_mode": ""}
    _, errors = sanitize_patch({"ui_visibility.seat_mode": "team"})
    assert errors and errors[0]["code"] == "bad_enum"


def test_settings_snapshot_reads_seat_mode_effective_value():
    from src.inbox.reply_pacing_settings import build_snapshot, nested_patch

    snap = build_snapshot({"ui_visibility": {"seat_mode": "multi"}})
    assert snap["values"]["ui_visibility.seat_mode"] == "multi"
    assert "ui_visibility.seat_mode" in snap["meta"]
    assert build_snapshot({})["values"]["ui_visibility.seat_mode"] == ""
    assert nested_patch({"ui_visibility.seat_mode": "multi"}) == {"ui_visibility": {"seat_mode": "multi"}}


def test_routes_expose_seat_mode_effective_in_snapshot_extras():
    src = (ROOT / "src" / "web" / "routes" / "reply_settings_routes.py").read_text(encoding="utf-8")
    assert 'snap["seat_mode_effective"] = seat_mode_verdict(' in src
    assert "list_agent_presence(" in src and "SEAT_PRESENCE_WINDOW_SEC" in src


# ── 升级一次性清空认领 ────────────────────────────────────────────────

def test_migration_clears_conversation_claims_once(tmp_path):
    from src.inbox import store as store_mod
    from src.inbox.store import InboxStore, _MIGRATIONS

    sql = [s for s in _MIGRATIONS if isinstance(s, str) and s.startswith("DELETE FROM conversation_claims")]
    assert len(sql) == 1 and "q30_seat_mode_default_single" in sql[0]
    store = InboxStore(tmp_path / "inbox.db")
    r = store.set_conversation_claim("conv-1", "agent-a", agent_name="A", ttl_sec=900)
    assert r.get("ok") and store.get_conversation_claim("conv-1")
    # 迁移已按内容哈希记账：重开库不再清（可安全重跑 = 语义上幂等，但不会反复删人家新写的租约）
    store2 = InboxStore(tmp_path / "inbox.db")
    assert store2.get_conversation_claim("conv-1") is not None
    # 模拟「升级到本版本」：撤掉该条记账 → 下次开库执行一次 → 租约清空
    sha = store2._migration_sha(sql[0])
    with store2._lock:
        store2._conn.execute("DELETE FROM schema_migrations_sha WHERE sql_sha=?", (sha,))
        store2._conn.commit()
    store3 = InboxStore(tmp_path / "inbox.db")
    assert store3.get_conversation_claim("conv-1") is None
    assert store3.list_conversation_claims() == []
    assert store3.migration_errors == 0
    assert store_mod is not None


# ── admin / 模板接线（静态）──────────────────────────────────────────

def test_admin_multi_seat_snapshot_uses_presence_and_fails_closed():
    src = (ROOT / "src" / "web" / "admin.py").read_text(encoding="utf-8")
    i = src.index("def _multi_seat_now() -> bool:")
    body = src[i:i + 1400]
    assert "list_agent_presence(" in body and "SEAT_PRESENCE_WINDOW_SEC" in body
    assert "is_multi_seat(" in body and "user_store.list_users()" not in body
    assert "return False" in body and "return True" not in body
    assert '_seat_snap = {"ts": 0.0, "multi": False}' in src


def test_reply_settings_template_wires_seat_card():
    html = (ROOT / "src" / "web" / "templates" / "reply_settings.html").read_text(encoding="utf-8")
    assert 'id="rps-sec-seat"' in html and 'id="rps-ms-toggle"' in html
    assert 'data-hot-chip="ui_visibility.seat_mode"' in html
    assert '{path: "ui_visibility.seat_mode", kind: "virtual", feat: "seat"' in html
    # 收集 / 回填都按 feat 探测（旧后端白名单缺键 → 不参与，防假脏 / 保存被拒）
    assert html.count('def.feat === "seat"') >= 2
    assert "rpsSeatDetect(d)" in html and "window.rpsMsChanged = rpsMsChanged;" in html
    # 卡片在额度守卫组之前。Q-26 C 的 rps-grp-auto 未进包时不要求该 id；
    # 工作树若已有该分组，则坐席卡须落在分组内。
    assert html.index('id="rps-sec-seat"') < html.index('id="rps-grp-quota"')
    if 'id="rps-grp-auto"' in html:
        assert html.index('id="rps-grp-auto"') < html.index('id="rps-sec-seat"')


def test_i18n_pack_covers_seat_card_keys():
    from src.web.web_i18n import get_translations

    zh, en, hant = get_translations("zh"), get_translations("en"), get_translations("zh_hant")
    for k in ("rps_ms_title", "rps_ms_hint", "rps_ms_row", "rps_ms_row_hint", "rps_ms_mode_multi",
              "rps_ms_mode_single", "rps_ms_src_explicit", "rps_ms_src_presence",
              "rps_ms_src_default", "rps_ms_eff_line", "rps_ms_pending_save"):
        assert zh.get(k) and en.get(k) and hant.get(k), k
    assert "{n}" in zh["rps_ms_src_presence"] and "{m}" in en["rps_ms_src_presence"]
