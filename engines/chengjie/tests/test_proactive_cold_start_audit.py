"""冷启动审计工具门禁（`tools/proactive_cold_start_audit.py` 的纯函数层）。

为什么审计工具本身也要门禁
==========================
这份报告是**用来给人下判断的证据**（「隔离期还剩多久」「放开后会发几条」），运维会照着
它决定要不要放量。一份会算错的证据比没有证据更糟——没有证据时人会自己去查，有错误证据
时人就直接信了。所以工具的两个纯函数（静态护栏复刻 / 汇总算式）必须钉住：

- ``passes_static_guards``：错在**松**的一侧 → 群聊被算进 eligible → 报告虚报战功；
  错在**紧**的一侧 → 真实私聊被漏掉 → 报告漏报风险。两个方向都要有用例。
- ``audit_rows``：账号维度聚合、沉默阈值、reason 分桶、``would_send`` 与 ``suppressed``
  互补——这几个数字之间的一致性就是报告可信度本身。

另有一条**只读性**门禁：审计必须不写线上的接入时刻登记（写脏了会让真闸把老账号误判成
新号 → 无差别静默）。它靠 ``AccountConnectionLog(None)`` 纯内存保证，此处显式钉住。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent


def _load_tool():
    """按路径加载 tools/ 下的脚本（tools 不是包，没有 __init__）。"""
    path = ENGINE_ROOT / "tools" / "proactive_cold_start_audit.py"
    spec = importlib.util.spec_from_file_location("_cs_audit", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_cs_audit"] = mod
    spec.loader.exec_module(mod)
    return mod


AUDIT = _load_tool()

HOUR = 3600.0
DAY = 24 * HOUR
NOW = 1_700_000_000.0

_CFG = {"enabled": True, "warmup_hours": 72.0,
        "require_inbound_since_connect": True, "quota_gate": True}


def _row(cid, account_id="A", chat_key="1001", chat_type="private",
         created_at=NOW - 300 * DAY, last_ts=NOW - 30 * DAY,
         last_in_ts=NOW - 30 * DAY, platform="telegram"):
    return {"conversation_id": cid, "account_id": account_id,
            "chat_key": chat_key, "chat_type": chat_type, "platform": platform,
            "created_at": created_at, "last_ts": last_ts, "last_in_ts": last_in_ts}


# ── 静态护栏复刻 ─────────────────────────────────────────────────────────────
def test_static_guards_pass_private():
    assert AUDIT.passes_static_guards(_row("c1")) is True


def test_static_guards_reject_group_by_chat_type():
    for ct in ("group", "supergroup", "channel"):
        assert AUDIT.passes_static_guards(_row("c1", chat_type=ct)) is False


def test_static_guards_reject_negative_telegram_chat_key():
    # 群/频道兜底：chat_type 缺失时靠负 ID 判（对齐 _conversations 623-625 行）
    assert AUDIT.passes_static_guards(
        _row("c1", chat_key="-1002461207899", chat_type="")) is False


def test_static_guards_reject_saved_messages():
    # chat_key == account_id → Saved Messages（is_system_peer 同一函数判定）
    assert AUDIT.passes_static_guards(
        _row("c1", account_id="8942577244", chat_key="8942577244")) is False


def test_static_guards_allow_bot_and_user_types():
    # 刻意放行 bot：是否联系 bot 属产品决策，不由本工具替用户删名单
    for ct in ("bot", "user", ""):
        assert AUDIT.passes_static_guards(_row("c1", chat_type=ct)) is True


def test_static_guards_negative_key_only_applies_to_telegram():
    # 别的平台 chat_key 可能天然带负号/非数字，不该套 Telegram 的群 ID 规则
    assert AUDIT.passes_static_guards(
        _row("c1", platform="whatsapp", chat_key="-abc", chat_type="")) is True


# ── 汇总算式 ─────────────────────────────────────────────────────────────────
def test_audit_new_account_all_suppressed():
    """事故形态：新号刚接入，导入的老会话全部到点 → 全压，会发 0。"""
    rows = [_row("c1", created_at=NOW - HOUR, last_ts=NOW - 90 * DAY,
                 last_in_ts=NOW - 90 * DAY, chat_key="1001"),
            _row("c2", created_at=NOW - 2 * HOUR, last_ts=NOW - 30 * DAY,
                 last_in_ts=NOW - 30 * DAY, chat_key="1002")]
    rep = AUDIT.audit_rows(rows, cfg=_CFG, now=NOW, min_silent_hours=24)
    assert rep["totals"]["eligible"] == 2
    assert rep["totals"]["would_send"] == 0
    assert rep["totals"]["suppressed"] == {"cold_start_warming": 2}


def test_audit_old_account_real_relationship_sends():
    """老号 + 接入后真聊过 → 放行（只减发送 ≠ 全停，反向也要钉）。"""
    rows = [_row("c1", created_at=NOW - 300 * DAY, last_ts=NOW - 30 * DAY,
                 last_in_ts=NOW - 30 * DAY)]
    rep = AUDIT.audit_rows(rows, cfg=_CFG, now=NOW, min_silent_hours=24)
    assert rep["totals"]["would_send"] == 1
    assert rep["totals"]["suppressed"] == {}


def test_audit_not_yet_silent_is_not_counted_as_win():
    """没到沉默阈值的会话不进 eligible——本来就不会发，不算本闸的功劳。"""
    rows = [_row("c1", created_at=NOW - HOUR, last_ts=NOW - 1 * HOUR,
                 last_in_ts=NOW - 1 * HOUR)]
    rep = AUDIT.audit_rows(rows, cfg=_CFG, now=NOW, min_silent_hours=24)
    assert rep["totals"]["eligible"] == 0
    assert rep["totals"]["suppressed"] == {}


def test_audit_groups_excluded_from_eligible():
    """群聊只计入 conversations，不进 private/eligible（防报告虚报战功）。"""
    rows = [_row("c1", chat_key="-100200", chat_type="supergroup",
                 created_at=NOW - HOUR, last_ts=NOW - 90 * DAY)]
    rep = AUDIT.audit_rows(rows, cfg=_CFG, now=NOW, min_silent_hours=24)
    acc = rep["accounts"][0]
    assert acc["conversations"] == 1 and acc["private"] == 0
    assert rep["totals"]["eligible"] == 0


def test_audit_quota_reason_overrides():
    """额度耗尽 → 连老号真实关系也停，reason 归 quota_exhausted。"""
    rows = [_row("c1", created_at=NOW - 300 * DAY, last_ts=NOW - 30 * DAY,
                 last_in_ts=NOW - 30 * DAY)]
    rep = AUDIT.audit_rows(rows, cfg=_CFG, now=NOW, min_silent_hours=24,
                           quota_exhausted=True)
    assert rep["totals"]["suppressed"] == {"quota_exhausted": 1}
    assert rep["totals"]["would_send"] == 0


def test_audit_accounts_isolated_and_age_computed():
    """多账号各算各的接入时刻与年龄（.198 现场就是同机两个 TG 号）。"""
    rows = [_row("c1", account_id="NEW", chat_key="1001",
                 created_at=NOW - 2 * HOUR, last_ts=NOW - 90 * DAY,
                 last_in_ts=NOW - 90 * DAY),
            _row("c2", account_id="OLD", chat_key="1002",
                 created_at=NOW - 300 * DAY, last_ts=NOW - 30 * DAY,
                 last_in_ts=NOW - 30 * DAY)]
    rep = AUDIT.audit_rows(rows, cfg=_CFG, now=NOW, min_silent_hours=24)
    by_id = {a["account_id"]: a for a in rep["accounts"]}
    assert by_id["NEW"]["account_age_hours"] == 2.0
    assert by_id["NEW"]["would_send"] == 0
    assert by_id["OLD"]["account_age_hours"] == round(300 * 24, 1)
    assert by_id["OLD"]["would_send"] == 1


def test_audit_totals_are_internally_consistent():
    """eligible == would_send + sum(suppressed)：报告自洽性（数字互相能对上）。"""
    rows = [_row("c%d" % i, account_id="NEW", chat_key=str(2000 + i),
                 created_at=NOW - HOUR, last_ts=NOW - (i + 30) * DAY,
                 last_in_ts=NOW - (i + 30) * DAY) for i in range(5)]
    rows += [_row("d%d" % i, account_id="OLD", chat_key=str(3000 + i),
                  created_at=NOW - 300 * DAY, last_ts=NOW - (i + 30) * DAY,
                  last_in_ts=NOW - (i + 30) * DAY) for i in range(3)]
    rep = AUDIT.audit_rows(rows, cfg=_CFG, now=NOW, min_silent_hours=24)
    t = rep["totals"]
    assert t["eligible"] == t["would_send"] + sum(t["suppressed"].values())
    assert t["would_send"] == 3


def test_audit_gate_disabled_reports_everything_sending():
    """关闸 → 全放行（工具能如实复现「没有闸会怎样」，这正是复盘要的对照）。"""
    rows = [_row("c1", created_at=NOW - HOUR, last_ts=NOW - 90 * DAY,
                 last_in_ts=NOW - 90 * DAY)]
    rep = AUDIT.audit_rows(rows, cfg=dict(_CFG, enabled=False), now=NOW,
                           min_silent_hours=24)
    assert rep["totals"]["would_send"] == 1


# ── 只读性 ───────────────────────────────────────────────────────────────────
def test_audit_never_writes_connection_log(tmp_path, monkeypatch):
    """审计绝不落盘接入时刻登记。

    写脏那份登记的后果不是「报告不准」，而是**真闸误判**：老账号被记成刚接入 →
    整机主动触达静默 72h，且没有任何报错。故此处直接钉「审计跑完，磁盘上不该多出
    任何 account_connection.json」。
    """
    monkeypatch.chdir(tmp_path)
    rows = [_row("c1", created_at=NOW - HOUR, last_ts=NOW - 90 * DAY)]
    AUDIT.audit_rows(rows, cfg=_CFG, now=NOW, min_silent_hours=24)
    assert list(tmp_path.rglob("account_connection.json")) == []


def test_render_is_ascii_safe_for_gbk_console():
    """报告不得含 emoji：PS5.1 控制台 GBK 编码会让整份报告以异常收场（本仓旧坑）。

    只校验**工具自己生成**的框架文案；会话名来自真实数据（可能带 emoji），那部分
    由调用方的 PYTHONIOENCODING 负责，不在本门禁范围。
    """
    rows = [_row("c1", created_at=NOW - HOUR, last_ts=NOW - 90 * DAY)]
    rep = AUDIT.audit_rows(rows, cfg=_CFG, now=NOW, min_silent_hours=24)
    text = AUDIT.render(rep)
    for line in text.split("\n"):
        line.encode("gbk")  # 抛 UnicodeEncodeError 即门禁失败
