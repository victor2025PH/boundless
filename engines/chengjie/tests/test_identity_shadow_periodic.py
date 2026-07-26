# -*- coding: utf-8 -*-
"""P3.2 身份影子周期化门禁。

钉住的语义：配置解析（默认关/6h/2000 + 夹逼与脏值兜底）、state 文件写读
（缺失/损坏回空）、报告→轻量 state（样本截断、失败也落 ts+error）、
watchdog 巡检（未启用零动作、首跑扫、间隔内节流、到期重扫、重启采纳
state ts 不重扫、失败吞掉不抛且不计成功数）、metrics 段三态
（disabled / 启用未扫 / 有数据）、/api/workspace/metrics 路由并入 + 鉴权。
"""

import json
import types

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.health_watchdog import HealthWatchdog
from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore
from src.utils.cross_platform_identity import CrossPlatformIdentity
from src.utils.identity_shadow import run_shadow_scan
from src.utils.identity_shadow_periodic import (
    STATE_FILENAME,
    build_state,
    metrics_snapshot,
    read_state,
    resolve_identity_db,
    resolve_inbox_db,
    run_periodic_scan,
    shadow_periodic_config,
    state_path,
    write_state,
)

T0 = 1_800_000_000.0


# ── 造数（镜像 test_identity_shadow 的端到端语料：1 对 high + 1 对 medium）────

def _seed_conv(store, platform, chat_key, *, name="", phone="", username="",
               account="acct", chat_type="private", ts=1000.0):
    store.upsert_conversation(InboxConversation(
        conversation_id=f"{platform}:{account}:{chat_key}",
        platform=platform, account_id=account, chat_key=chat_key,
        display_name=name, phone=phone, username=username,
        chat_type=chat_type, last_ts=ts))


def _seed_dbs(tmp_path):
    inbox_db = tmp_path / "inbox.db"
    store = InboxStore(inbox_db)
    _seed_conv(store, "telegram", "8921664288", name="张伟",
               phone="+63 927 013 5480", ts=4000)
    _seed_conv(store, "whatsapp", "639270135480", name="Zhang Wei", ts=3000)
    _seed_conv(store, "telegram", "555000111", username="nightwolf88", ts=2000)
    _seed_conv(store, "line", "U-deadbeef", username="@NightWolf88", ts=1500)

    identity_db = tmp_path / "bot.db"
    cpi = CrossPlatformIdentity(identity_db)
    cpi.link("telegram", "8921664288", "whatsapp", "639270135480")
    cpi.close()
    return inbox_db, identity_db


def _cfg(tmp_path, enabled=True, **over):
    node = {"enabled": enabled}
    node.update(over)
    return {
        "contacts": {"identity_shadow": node},
        "inbox": {"db_path": str(tmp_path / "inbox.db")},
        "memory": {"db_path": str(tmp_path / "bot.db")},
    }


class _CM:
    def __init__(self, config, config_path):
        self.config = config
        self.config_path = str(config_path)


def _watchdog(cfg, cfg_dir):
    app = types.SimpleNamespace(state=types.SimpleNamespace())
    return HealthWatchdog(app=app, config_manager=_CM(cfg, cfg_dir / "config.yaml"),
                          interval_sec=60)


# ── 配置解析 ────────────────────────────────────────────────────────────────

def test_periodic_config_defaults():
    pcfg = shadow_periodic_config({})
    assert pcfg == {"enabled": False, "scan_interval_hours": 6.0,
                    "max_rows": 2000, "sample_size": 8}
    # 节点是脏类型（列表）也兜底为默认
    assert shadow_periodic_config(
        {"contacts": {"identity_shadow": ["junk"]}})["enabled"] is False
    assert shadow_periodic_config(None)["max_rows"] == 2000


def test_periodic_config_clamps_and_junk():
    cfg = {"contacts": {"identity_shadow": {
        "enabled": True, "scan_interval_hours": 0, "max_rows": 5,
        "sample_size": 999,
    }}}
    pcfg = shadow_periodic_config(cfg)
    assert pcfg["enabled"] is True
    assert pcfg["scan_interval_hours"] == 0.25   # 下限一刻钟
    assert pcfg["max_rows"] == 50                # 下限 50
    assert pcfg["sample_size"] == 50             # 上限 50
    # 脏字符串 → 回默认
    cfg = {"contacts": {"identity_shadow": {"scan_interval_hours": "abc"}}}
    assert shadow_periodic_config(cfg)["scan_interval_hours"] == 6.0


def test_db_path_resolution(tmp_path):
    cfg_dir = tmp_path / "config"
    # 缺省：cfg_dir 下 inbox.db / bot.db
    assert resolve_inbox_db({}, cfg_dir) == cfg_dir / "inbox.db"
    assert resolve_identity_db({}, cfg_dir) == cfg_dir / "bot.db"
    # 相对路径挂 cfg_dir；绝对路径原样
    assert resolve_inbox_db({"inbox": {"db_path": "sub/i.db"}}, cfg_dir) \
        == cfg_dir / "sub" / "i.db"
    abs_p = tmp_path / "elsewhere" / "bot.db"
    assert resolve_identity_db({"memory": {"db_path": str(abs_p)}}, cfg_dir) == abs_p


# ── 报告 → state ────────────────────────────────────────────────────────────

def test_build_state_compacts_report_and_caps_sample(tmp_path):
    inbox_db, identity_db = _seed_dbs(tmp_path)
    report = run_shadow_scan(inbox_db, identity_db, limit=100)
    state = build_state(report, sample_size=1, now=T0)
    assert state["ok"] is True
    assert state["last_scan_ts"] == T0
    assert state["scanned_conversations"] == 4
    assert state["candidates"] == 4
    assert state["pairs"] == 2
    assert state["counts"] == {"high": 1, "medium": 1, "low": 0,
                               "already_linked": 1}
    # 样本截 1（pairs 已按 tier 排序 → 保 high），且是压缩形态（不落全字段画像）
    assert len(state["sample"]) == 1
    s = state["sample"][0]
    assert s["tier"] == "high" and s["already_linked"] is True
    assert s["a"].count(":") == 1 and s["b"].count(":") == 1
    # P9：结构化字段供 ops 确认/否定按钮；a/b 字符串保留兼容
    assert set(s) == {
        "tier", "already_linked", "evidence", "a", "a_name", "b", "b_name",
        "a_platform", "a_chat", "b_platform", "b_chat", "pair_key",
    }
    assert s["a"] == f"{s['a_platform']}:{s['a_chat']}"
    assert s["pair_key"]
    assert state["dismissed_total"] == 0
    # P7 信号覆盖透传（看板据此区分「覆盖不足 vs 匹配问题」）
    cov = state["coverage"]
    assert set(cov) == {"telegram", "whatsapp", "line"}
    assert cov["telegram"]["total"] == 2
    assert cov["whatsapp"] == {"total": 1, "phone": 1, "username": 0}
    # 深拷贝：污染 state 不得反噬报告
    cov["whatsapp"]["phone"] = 99
    assert report["coverage"]["whatsapp"]["phone"] == 1


def test_build_state_failure_keeps_ts_and_error():
    state = build_state({"ok": False, "error": "read_inbox_failed: boom"}, now=T0)
    assert state["ok"] is False
    assert state["last_scan_ts"] == T0
    assert "boom" in state["error"]
    assert "counts" not in state


# ── state 文件 IO ───────────────────────────────────────────────────────────

def test_state_write_read_roundtrip(tmp_path):
    p = state_path(tmp_path)
    assert p.name == STATE_FILENAME
    st = {"ok": True, "last_scan_ts": T0, "pairs": 2}
    assert write_state(p, st) is True
    assert read_state(p) == st
    # UTF-8 缩进 JSON（人肉可读，风格对齐仓库其他 state 文件）
    raw = p.read_text(encoding="utf-8")
    assert json.loads(raw)["pairs"] == 2 and "\n" in raw


def test_read_state_missing_or_corrupt_returns_empty(tmp_path):
    assert read_state(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert read_state(bad) == {}
    # 顶层不是 dict 也回空
    bad.write_text("[1,2]", encoding="utf-8")
    assert read_state(bad) == {}


# ── 周期扫描入口（端到端）───────────────────────────────────────────────────

def test_run_periodic_scan_end_to_end(tmp_path):
    _seed_dbs(tmp_path)
    cfg = _cfg(tmp_path)
    state = run_periodic_scan(cfg, tmp_path, now=T0)
    assert state["ok"] is True
    assert state["counts"]["high"] == 1 and state["counts"]["medium"] == 1
    # 落盘内容与返回一致
    assert read_state(state_path(tmp_path)) == state


def test_run_periodic_scan_read_only(tmp_path):
    """扫描前后业务库字节不变（mode=ro 铁律延续到周期链路）。"""
    inbox_db, identity_db = _seed_dbs(tmp_path)
    before = (inbox_db.read_bytes(), identity_db.read_bytes())
    run_periodic_scan(_cfg(tmp_path), tmp_path, now=T0)
    assert (inbox_db.read_bytes(), identity_db.read_bytes()) == before


# ── watchdog 巡检语义 ───────────────────────────────────────────────────────

def test_watchdog_disabled_is_noop(tmp_path):
    _seed_dbs(tmp_path)
    wd = _watchdog(_cfg(tmp_path, enabled=False), tmp_path)
    wd._check_identity_shadow(now=T0)
    assert not state_path(tmp_path).exists()          # 没扫没写
    assert wd.total_identity_shadow_scans == 0
    assert wd._ishadow_last_ts == 0.0


def test_watchdog_scans_then_throttles_then_rescans(tmp_path):
    _seed_dbs(tmp_path)
    wd = _watchdog(_cfg(tmp_path), tmp_path)
    # 首跑：扫 + 落盘
    wd._check_identity_shadow(now=T0)
    assert wd.total_identity_shadow_scans == 1
    assert read_state(state_path(tmp_path))["last_scan_ts"] == T0
    # 间隔内（默认 6h）：节流不重扫
    wd._check_identity_shadow(now=T0 + 3600)
    assert wd.total_identity_shadow_scans == 1
    assert read_state(state_path(tmp_path))["last_scan_ts"] == T0
    # 到期：重扫，ts 前移
    t2 = T0 + 6 * 3600 + 1
    wd._check_identity_shadow(now=t2)
    assert wd.total_identity_shadow_scans == 2
    assert read_state(state_path(tmp_path))["last_scan_ts"] == t2


def test_watchdog_restart_adopts_state_ts(tmp_path):
    """重启不重扫：新实例冷启动采纳 state 文件的 last_scan_ts 做节流基准。"""
    _seed_dbs(tmp_path)
    cfg = _cfg(tmp_path)
    _watchdog(cfg, tmp_path)._check_identity_shadow(now=T0)
    # 「重启」＝全新 watchdog 实例（内存 ts 归零）
    wd2 = _watchdog(cfg, tmp_path)
    wd2._check_identity_shadow(now=T0 + 60)
    assert wd2.total_identity_shadow_scans == 0       # 采纳文件 ts → 未到期不扫
    assert read_state(state_path(tmp_path))["last_scan_ts"] == T0
    # 文件 ts 到期后照常扫
    wd2._check_identity_shadow(now=T0 + 6 * 3600 + 1)
    assert wd2.total_identity_shadow_scans == 1


def test_watchdog_custom_interval(tmp_path):
    _seed_dbs(tmp_path)
    wd = _watchdog(_cfg(tmp_path, scan_interval_hours=1), tmp_path)
    wd._check_identity_shadow(now=T0)
    wd._check_identity_shadow(now=T0 + 1800)
    assert wd.total_identity_shadow_scans == 1        # 半小时内不重扫
    wd._check_identity_shadow(now=T0 + 3601)
    assert wd.total_identity_shadow_scans == 2        # 1h 到期重扫


def test_watchdog_scan_failure_swallowed_and_throttled(tmp_path):
    """库缺失 → 扫描失败：不抛、不计成功数、state 落 error、失败也按周期节流。"""
    cfg = _cfg(tmp_path)                              # 不 seed → inbox.db 不存在
    wd = _watchdog(cfg, tmp_path)
    wd._check_identity_shadow(now=T0)                 # 不应 raise
    assert wd.total_identity_shadow_scans == 0
    st = read_state(state_path(tmp_path))
    assert st["ok"] is False and "read_inbox_failed" in st["error"]
    assert st["last_scan_ts"] == T0
    # 失败后间隔内不重试（ts 已推进，防每 tick 刷 warning）
    wd._check_identity_shadow(now=T0 + 60)
    assert read_state(state_path(tmp_path))["last_scan_ts"] == T0


def test_watchdog_corrupt_state_file_does_not_break_check(tmp_path):
    """state 文件被外部写坏 → 冷启动采纳基准回 0 → 照常扫描并覆写为好文件。"""
    _seed_dbs(tmp_path)
    state_path(tmp_path).write_text("{corrupt", encoding="utf-8")
    wd = _watchdog(_cfg(tmp_path), tmp_path)
    wd._check_identity_shadow(now=T0)
    assert wd.total_identity_shadow_scans == 1
    assert read_state(state_path(tmp_path))["ok"] is True


def test_watchdog_status_snapshot_has_counter(tmp_path):
    wd = _watchdog(_cfg(tmp_path, enabled=False), tmp_path)
    assert wd.status_snapshot()["total_identity_shadow_scans"] == 0


# ── metrics 段 ──────────────────────────────────────────────────────────────

def test_metrics_snapshot_disabled(tmp_path):
    assert metrics_snapshot(_cfg(tmp_path, enabled=False), tmp_path) \
        == {"enabled": False}
    assert metrics_snapshot({}, tmp_path) == {"enabled": False}


def test_metrics_snapshot_enabled_no_state_yet(tmp_path):
    snap = metrics_snapshot(_cfg(tmp_path), tmp_path)
    assert snap["enabled"] is True
    assert snap["state_available"] is False
    assert snap["scan_interval_hours"] == 6.0
    assert snap["max_rows"] == 2000


def test_metrics_snapshot_merges_state(tmp_path):
    _seed_dbs(tmp_path)
    run_periodic_scan(_cfg(tmp_path), tmp_path, now=T0)
    snap = metrics_snapshot(_cfg(tmp_path), tmp_path)
    assert snap["enabled"] is True and snap["state_available"] is True
    assert snap["last_scan_ts"] == T0
    assert snap["pairs"] == 2
    assert snap["counts"]["high"] == 1
    assert len(snap["sample"]) == 2
    # P11：零动作时不出 totals 段（ops 卡不渲染空读数行）
    assert "totals" not in snap


def test_metrics_snapshot_includes_merge_totals(tmp_path):
    """P11：人工确认/手动链的合流累计随快照并入；扫描重写 state 不冲累计。"""
    from src.utils.identity_shadow_actions import record_merge_event

    _seed_dbs(tmp_path)
    record_merge_event(
        tmp_path, "confirm", merged_rows=4, cluster_relinked=1,
        canonical="telegram:111", now=T0)
    # totals 先写、扫描后跑——独立文件语义：state 重写不得抹掉累计
    run_periodic_scan(_cfg(tmp_path), tmp_path, now=T0)
    snap = metrics_snapshot(_cfg(tmp_path), tmp_path)
    tot = snap.get("totals") or {}
    assert tot.get("confirm_pairs") == 1
    assert tot.get("merged_rows") == 4
    assert tot.get("cluster_relinked") == 1
    assert (tot.get("last") or {}).get("kind") == "confirm"


# ── /api/workspace/metrics 路由并入 + 鉴权 ──────────────────────────────────

def _metrics_app(tmp_path, cfg, role="admin"):
    from src.web.routes.drafts_routes import register_metrics_route

    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": role, "user_id": "u1"}
        return await call_next(req)

    def _api_auth(r: Request):
        return True

    register_metrics_route(app, api_auth=_api_auth)
    app.state.config_manager = _CM(cfg, tmp_path / "config.yaml")
    return TestClient(app, raise_server_exceptions=True)


def test_metrics_route_exposes_identity_shadow(tmp_path):
    _seed_dbs(tmp_path)
    cfg = _cfg(tmp_path)
    run_periodic_scan(cfg, tmp_path, now=T0)
    c = _metrics_app(tmp_path, cfg)
    isd = c.get("/api/workspace/metrics").json().get("identity_shadow")
    assert isd is not None
    assert isd["enabled"] is True and isd["state_available"] is True
    assert isd["counts"] == {"high": 1, "medium": 1, "low": 0,
                             "already_linked": 1}
    assert isd["sample"][0]["tier"] == "high"


def test_metrics_route_disabled_flag_only(tmp_path):
    c = _metrics_app(tmp_path, _cfg(tmp_path, enabled=False))
    isd = c.get("/api/workspace/metrics").json().get("identity_shadow")
    assert isd == {"enabled": False}


def test_metrics_route_requires_supervisor(tmp_path):
    """非主管角色读 metrics（含 identity_shadow 段）→ 403（既有鉴权闸对新段同样生效）。"""
    c = _metrics_app(tmp_path, _cfg(tmp_path), role="agent")
    assert c.get("/api/workspace/metrics").status_code == 403
