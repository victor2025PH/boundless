# -*- coding: utf-8 -*-
"""Token 钱包 P3 工具门禁：影子读数（tools/wallet_shadow_report）+ 并账（scripts/wallet_migrate）。

守的不变量：
- 读数余额与账本核算同源（allocate_spend），跑道/燃烧按含零日的真均值；
- 校准面「付费路径占比」窗口切片正确、零流量如实 n/a；
- 并账换算向上取整（宁多给客户）、负值钳制；
- 并账入账 = migration 批次**永不过期** + ref 幂等（重复跑绝不双倍）+
  钱包键与消费点同源（wallet_id_for_status）——键不同=并了个寂寞。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.wallet_migrate import (  # noqa: E402
    apply_migration,
    migration_ref,
    plan_migration,
)
from src.licensing.token_ledger import (  # noqa: E402
    TokenLedgerStore,
    wallet_id_for_status,
)
from tools.wallet_shadow_report import (  # noqa: E402
    billed_share,
    collect_root,
    recent_days,
    summarize_wallet,
    trend_line,
)

NOW = time.time()


def _day(offset: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(NOW - offset * 86400))


def _st(customer="tg:@boss", lic_id="chatx-personal-T1", licensed=True, included=0):
    return SimpleNamespace(
        licensed=licensed, lic_id=lic_id, customer=customer,
        included_chars=included, included_tokens_monthly=0, enforce=False,
        state="active",
    )


# ── 影子读数：钱包面 ─────────────────────────────────────────────────────────

def test_recent_days_window_ends_today():
    days = recent_days(3, NOW)
    assert len(days) == 3 and days[-1] == _day(0) and days[0] == _day(2)


def test_summarize_wallet_balance_and_burn():
    grants = [(1000, None)]
    rows = [(_day(1), "ai_reply", 140), (_day(0), "ai_reply", 70),
            (_day(30), "ai_reply", 90)]  # 窗口外的历史支出也进余额核算
    w = summarize_wallet(grants, rows, now=NOW, days=14)
    assert w["total_spend"] == 300 and w["balance"] == 700
    assert w["window_spend"] == 210          # 30 天前那笔不进窗口
    assert w["burn7_avg"] == 30.0            # (140+70)/7 —— 零日拉平，均值不虚高
    assert w["runway_days"] == round(700 / 30.0, 1)
    assert w["would_degrade_now"] is False


def test_summarize_wallet_would_degrade_and_zero_traffic():
    # 支出超过授予 → 余额 0 → enforce 开即降级
    w = summarize_wallet([(100, None)], [(_day(0), "ai_reply", 150)], now=NOW)
    assert w["balance"] == 0 and w["would_degrade_now"] is True
    assert w["spend_unmet"] == 50            # 异常痕迹如实报
    # 零支出钱包（只有授予）不算降级，跑道无意义为 None
    w2 = summarize_wallet([(100, None)], [], now=NOW)
    assert w2["would_degrade_now"] is False and w2["runway_days"] is None


# ── 影子读数：校准面 ─────────────────────────────────────────────────────────

def test_billed_share_math_and_window():
    chars = [(_day(1), "tts", 10_000), (_day(40), "tts", 99_999),   # 窗口外不计
             (_day(2), "translation", 5_000)]
    tokens = [(_day(1), "voice_clone", 250),
              (_day(2), "pro_translate", 30), (_day(2), "deepl_translate", 20)]
    cal = billed_share(chars, tokens, days=14, now=NOW)
    # tts: 10000 chars 全计费应为 10000/100*10=1000，实记 250 → 25%
    assert cal["tts"]["tokens_if_all_billed"] == 1000
    assert cal["tts"]["billed_share"] == 0.25
    # translation: 5000 chars → 全计费 50，实记 30+20=50 → 100%
    assert cal["translation"]["billed_share"] == 1.0


def test_billed_share_zero_flow_is_na():
    cal = billed_share([], [(_day(0), "voice_clone", 10)], days=7, now=NOW)
    assert cal["tts"]["billed_share"] is None
    assert cal["tts"]["tokens_actual"] == 10  # 占比 >1 的口径漂移由消费方肉眼判


# ── 影子读数：端到端（真 SQLite，只读打开）─────────────────────────────────────

def test_collect_root_end_to_end(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    store = TokenLedgerStore(cfg / "token_ledger.db")
    store.grant_pack("w1", 2000, ref="o1")
    store.record_spend("w1", "ai_reply", 120)
    store.note_fair_use("w1", 300)

    from src.licensing.quota_store import LicenseQuotaStore

    q = LicenseQuotaStore(cfg / "license_quota.db")
    q.record("L1", "tts", 4_000)

    rep = collect_root(tmp_path, days=14)
    assert rep["ledger_db"] and rep["quota_db"]
    assert rep["wallets"]["w1"]["balance"] == 1880
    assert rep["wallets"]["w1"]["by_action_alltime"] == {"ai_reply": 120}
    assert rep["fair_use"][0]["chars"] == 300
    assert rep["calibration"]["tts"]["chars"] == 4_000


def test_collect_root_missing_dbs_soft(tmp_path):
    rep = collect_root(tmp_path, days=7)
    assert rep["ledger_db"] is False and rep["wallets"] == {}
    assert rep["calibration"]["tts"]["billed_share"] is None


def test_trend_line_compact_shape(tmp_path):
    """周批趋势行只留决策字段（balance/burn/runway/degrade + 校准），带 UTC 时间戳。"""
    cfg = tmp_path / "config"
    cfg.mkdir()
    store = TokenLedgerStore(cfg / "token_ledger.db")
    store.grant_pack("w1", 1000, ref="o1")
    store.record_spend("w1", "ai_reply", 70)
    row = trend_line(collect_root(tmp_path, days=7), now=NOW)
    assert row["ts"].endswith("Z") and row["root"] == str(tmp_path)
    w = row["wallets"]["w1"]
    assert set(w) == {"balance", "window_spend", "burn7_avg", "runway_days", "would_degrade_now"}
    assert w["balance"] == 930 and "by_day" not in w


# ── 并账：纯函数 ─────────────────────────────────────────────────────────────

def test_plan_migration_math():
    p = plan_migration(1_000_000, 0, 0)
    assert p["remaining_chars"] == 1_000_000 and p["tokens"] == 10_000
    # 向上取整：150 剩余 chars → 2 Token（宁多给客户）
    assert plan_migration(150, 0, 0)["tokens"] == 2
    # 用超（负余额）钳 0；负输入钳 0
    assert plan_migration(1000, 0, 5000)["tokens"] == 0
    assert plan_migration(-5, -5, -5)["tokens"] == 0
    # 充值包计入
    assert plan_migration(0, 300_000, 100_000)["remaining_chars"] == 200_000


# ── 并账：入账语义 ───────────────────────────────────────────────────────────

def test_apply_migration_dry_run_writes_nothing():
    store = TokenLedgerStore(":memory:")
    st = _st()
    r = apply_migration(store, st, plan_migration(10_000, 0, 0), apply=False)
    assert r["applied"] is False and r["skipped_reason"] == "dry_run"
    assert store.balance(r["wallet"])["balance"] == 0


def test_apply_migration_grant_never_expires_and_idempotent():
    store = TokenLedgerStore(":memory:")
    st = _st(customer="tg:@alice", lic_id="chatx-personal-A9")
    plan = plan_migration(500_000, 0, 100_000)   # 剩 400k chars = 4000 Token
    r1 = apply_migration(store, st, plan, apply=True)
    assert r1["applied"] is True and r1["tokens"] == 4000
    # 钱包键与消费点同一把（wallet_id_for_status），不是裸 lic_id
    assert r1["wallet"] == wallet_id_for_status(st)
    assert r1["ref"] == migration_ref("chatx-personal-A9")
    # 两年后余额仍在（migration 永不过期——存量价值不设时限）
    far = time.time() + 730 * 86400
    assert store.balance(r1["wallet"], now=far)["balance"] == 4000
    # 幂等：重复跑绝不双倍
    r2 = apply_migration(store, st, plan, apply=True)
    assert r2["applied"] is False and r2["skipped_reason"] == "ref_exists_or_store_error"
    assert store.balance(r1["wallet"])["balance"] == 4000


def test_apply_migration_zero_remaining_skips():
    store = TokenLedgerStore(":memory:")
    r = apply_migration(store, _st(), plan_migration(0, 0, 0), apply=True)
    assert r["applied"] is False and r["skipped_reason"] == "no_remaining_chars"
    assert store.balance(r["wallet"])["balance"] == 0
