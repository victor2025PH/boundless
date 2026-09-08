"""O-2 D（2026-09-08，#201 WYNN22）：记忆页「自安装以来新增 N 条」+ 停滞红字。

WYNN22 用户可验口径：「AI 记忆页 13:14 后无新增」——页面此前只有「AI 记得的事（库内 active
条数）」，看不出「抽取链到底有没有在产出」。现在：
- store ``growth_counts``：since_install（历史写过的每一行，含后来删除 / 过时）/ today_new
  （本机 0 点起）/ promoted_total（stable）/ first_created_at；随 ``admin_summary`` 出；
- 路由 ``/api/episodic-memory/summary`` 合成 ``growth.inbound_total``（inbox.db 入站累计）与
  ``growth.stalled``（入站 ≥50 且 since_install==0）；
- 模板顶部一行计数 + 红字（链到 /logs 看 [episodic] 行）；i18n zh/en（zh_hant 随发版 regen）。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from src.utils.episodic_memory_store import EpisodicMemoryStore
from src.web.routes.episodic_identity_routes import (
    MEMORY_STALL_MIN_INBOUND,
    inbound_message_total,
    memory_growth_status,
)

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    s = EpisodicMemoryStore(tmp_path / "bot.db")
    yield s
    try:
        s.close()
    except Exception:
        pass


# ── store.growth_counts ─────────────────────────────────────────────────────

def test_growth_counts_empty_store_is_all_zero(store):
    g = store.growth_counts()
    assert g == {"since_install": 0, "today_new": 0, "promoted_total": 0, "first_created_at": None}
    assert store.admin_summary(days=7)["growth"]["since_install"] == 0


def test_growth_counts_track_all_time_writes_not_current_rows(store):
    a = store.add_fact("u1", "客户住在曼谷")
    b = store.add_fact("u1", "客户喜欢喝美式咖啡")
    store.add_fact("u2", "客户下周来见我")
    g = store.growth_counts()
    assert g["since_install"] == 3 and g["today_new"] == 3 and g["promoted_total"] == 0
    assert g["first_created_at"] and abs(g["first_created_at"] - time.time()) < 60
    # 一条转正、一条被坐席忽略（软删）：库内 active 变 2，但「自安装以来写过」仍是 3
    store._conn.execute("UPDATE episodic_memory SET tier='stable' WHERE id=?", (a,))
    store._conn.commit()
    store.ignore_fact(b)
    g2 = store.growth_counts()
    assert g2["since_install"] == 3, "历史写过的行不因删除 / 过时而消失——它回答的是「抽取链有没有产出」"
    assert g2["promoted_total"] == 1
    assert store.admin_summary(days=7)["total_count"] == 2


def test_growth_today_starts_at_local_midnight(store):
    store.add_fact("u1", "客户今天说的")
    yesterday = time.time() - 36 * 3600
    old = store.add_fact("u1", "客户昨天说的")
    store._conn.execute("UPDATE episodic_memory SET created_at=? WHERE id=?", (yesterday, old))
    store._conn.commit()
    g = store.growth_counts()
    assert g["since_install"] == 2 and g["today_new"] == 1
    assert abs(g["first_created_at"] - yesterday) < 1


# ── 路由纯函数：停滞判定 / 入站累计 ────────────────────────────────────────

def test_memory_growth_status_stall_rule():
    assert MEMORY_STALL_MIN_INBOUND == 50
    zero = {"since_install": 0, "today_new": 0, "promoted_total": 0, "first_created_at": None}
    assert memory_growth_status(zero, 50)["stalled"] is True, "WYNN22 形态：入站够多、记忆 0 条"
    assert memory_growth_status(zero, 49)["stalled"] is False, "入站不足不判（装完第一小时不误报）"
    assert memory_growth_status(zero, None)["stalled"] is False, "入站数拿不到不判（宁不报勿误报）"
    assert memory_growth_status({**zero, "since_install": 1}, 5000)["stalled"] is False, "修复后从 0 开始涨即消失"
    out = memory_growth_status(zero, 120)
    assert out["inbound_total"] == 120 and out["stall_min_inbound"] == 50
    assert memory_growth_status(None, 100)["stalled"] is True   # store 读数缺失按 0 处理
    assert memory_growth_status(zero, 10, min_inbound=5)["stalled"] is True


def test_inbound_message_total_reads_inbox_db_read_only(tmp_path):
    db = tmp_path / "inbox.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE messages (message_id TEXT, direction TEXT, text TEXT)")
    con.executemany("INSERT INTO messages VALUES (?,?,?)",
                    [(f"m{i}", "in" if i % 3 else "out", "x") for i in range(60)])
    con.commit()
    con.close()
    assert inbound_message_total(db) == 40
    assert inbound_message_total(None) is None
    assert inbound_message_total(tmp_path / "missing.db") is None
    bad = tmp_path / "bad.db"
    sqlite3.connect(bad).close()
    assert inbound_message_total(bad) is None, "无 messages 表 → None 不抛"


# ── 端到端：/api/episodic-memory/summary 带 growth 块 ─────────────────────────

def test_summary_endpoint_carries_growth(tmp_path):
    import asyncio
    from unittest.mock import MagicMock

    from starlette.testclient import TestClient

    from src.utils.audit_store import AuditStore
    from src.web.admin import create_app
    from tests.test_web_episodic_memory_api import _load_cm

    loop = asyncio.new_event_loop()
    try:
        cm = loop.run_until_complete(_load_cm(tmp_path))
    finally:
        loop.close()
    estore = EpisodicMemoryStore(tmp_path / "bot.db")
    estore.add_fact("u1", "客户住在曼谷")
    sm = MagicMock()
    sm._episodic_store = estore
    tc = MagicMock()
    tc.skill_manager = sm
    app = create_app(cm, audit_store=AuditStore(db_path=tmp_path / "audit.db"), boot_ts=0,
                     telegram_client=tc)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.headers.update({"Authorization": "Bearer test-token-123"})
        r = client.get("/api/episodic-memory/summary?days=7&top=3")
    assert r.status_code == 200
    g = r.json()["growth"]
    assert g["since_install"] == 1 and g["today_new"] == 1 and g["promoted_total"] == 0
    assert g["stalled"] is False and "inbound_total" in g and g["stall_min_inbound"] == 50
    estore.close()


# ── 模板 / i18n ───────────────────────────────────────────────────────────────

def test_template_has_growth_line_and_stall_alert():
    tpl = (REPO / "src" / "web" / "templates" / "episodic_memory.html").read_text(encoding="utf-8")
    assert 'id="cs-growth"' in tpl and 'id="cs-stall"' in tpl
    assert "_emGrowthRender(d.growth)" in tpl
    assert 'href="/logs"' in tpl, "红字必须链到日志页看 [episodic] 行"
    for k in ("em_growth_line", "em_growth_stalled", "em_growth_stalled_link", "em_growth_tip"):
        assert k in tpl, k


def test_i18n_growth_keys_bilingual():
    from src.web.i18n_packs.episodic_page import EN, ZH
    for k in ("em_growth_line", "em_growth_since", "em_growth_inbound", "em_growth_stalled",
              "em_growth_stalled_link", "em_growth_tip"):
        assert ZH.get(k) and EN.get(k), f"{k} 缺双语"
    for k in ("em_growth_line",):
        assert "{n}" in ZH[k] and "{t}" in ZH[k] and "{p}" in ZH[k]
        assert "{n}" in EN[k] and "{t}" in EN[k] and "{p}" in EN[k]
    assert "{i}" in ZH["em_growth_stalled"] and "{i}" in EN["em_growth_stalled"]
