"""通道离线横幅「生命周期」门禁（2026-08-27 Calixa 僵尸横幅事故沉淀）。

事故链：账号在别机重新登录 → 本机会话 needs_login → 同一登录档案（login_id）被
新账号人工重登接替 → 旧账号健康记录无人再更新，红条挂 65 小时、重登按钮永远
404、催办每 4h 轰一次。当日修复落了四层机制但零测试覆盖，本文件把行为契约钉死：

① 身份接替清账：``record(authorized, login_id=L)`` 自动把「同 L 的其他不健康
   账号行」翻 ``logged_out``（横幅灭、发送闸不变），``mark_superseded_accounts``
   把注册表 ``worker:*`` 催办标记改写为 ``superseded:*``（看门狗停催）；
② 运营登出/删除 → 内存健康表联动（``_sync_health_logged_out`` 接线，修「做了
   正确操作红条还要挂到下次重启」）；
③ relogin 失败分诊：worker 404（档案没了，重试无意义）与 worker 宕机（可重试）
   必须是两个不同的人话文案键，绝不透传 httpx 英文原文；
④ 催办衰减：掉线超 ``stale_after`` 的死号降为低频提醒（防告警疲劳），置 0 恢复
   旧行为；
⑤ 成对不变量：``logged_out`` 刻意**不进横幅**（``_channel_health_snapshot`` 过滤）
   但**仍在不健康集合**（发送前快速失败语义不变）——两半各自被改动都会破坏设计。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch):
    """每例独立健康单例 + EventBus + 跳过注册表种子（与 test_platform_session_health 同款）。"""
    import src.integrations.platform_session_health as psh
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(psh, "_SINGLETON", None, raising=False)
    monkeypatch.setattr(psh, "_SEEDED", True, raising=False)
    monkeypatch.setattr(eb, "_bus", None, raising=False)
    yield


def _store():
    from src.integrations.platform_session_health import (
        get_platform_session_health,
    )
    return get_platform_session_health()


def _client():
    from src.web.routes.unified_inbox_account_routes import (
        register_account_routes,
    )
    app = FastAPI()
    register_account_routes(app, api_auth=lambda request: None,
                            config_manager=None)
    return TestClient(app)


# ── ① 身份接替清账（store 语义）────────────────────────────────────────────

def test_supersede_flips_old_account_rows_and_pops_inbox_health():
    s = _store()
    s.record("messenger", "111", "needs_login", login_id="msgA")
    s.record_inbox_health("messenger", "111", unread=3,
                          read_attempts=4, read_fails=4)
    assert "messenger:111" in s.unhealthy_sessions()
    assert "messenger:111" in s.inbox_health()

    trans = s.record("messenger", "222", "authorized", login_id="msgA")
    assert trans["superseded"] == ["111"]
    old = s.dump()["sessions"]["messenger:111"]
    assert old["status"] == "logged_out"
    assert "superseded by messenger:222" in old["detail"]
    # 入站健康行（旧号的陈迹）整行弹出，不给看门狗半死巡检留幽灵
    assert "messenger:111" not in s.inbox_health()


def test_supersede_ignores_other_login_slots_and_healthy_rows():
    s = _store()
    s.record("messenger", "111", "needs_login", login_id="msgB")   # 别的档案
    s.record("messenger", "333", "authorized", login_id="msgC")    # 健康行
    trans = s.record("messenger", "222", "authorized", login_id="msgA")
    assert trans["superseded"] == []
    assert s.dump()["sessions"]["messenger:111"]["status"] == "needs_login"
    assert s.dump()["sessions"]["messenger:333"]["status"] == "authorized"


def test_superseded_row_still_blocks_sends():
    """接替清账只灭横幅/催办，绝不放开发送闸（logged_out 仍属不健康）。"""
    s = _store()
    s.record("messenger", "111", "expired", login_id="msgA")
    s.record("messenger", "222", "authorized", login_id="msgA")
    assert s.is_unhealthy("messenger", "111") is True


def test_session_status_route_supersede_rewrites_registry_marker():
    """端点级全链：session-status authorized 推送 → 清账 + 注册表停催标记。"""
    from src.integrations.account_registry import get_account_registry
    reg = get_account_registry()
    reg.upsert("messenger", "111", status="offline",
               meta={"offline_reason": "worker:needs_login"})
    s = _store()
    s.record("messenger", "111", "needs_login", login_id="msgA")

    c = _client()
    r = c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "account_id": "222",
        "login_id": "msgA", "status": "authorized",
    })
    assert r.status_code == 200
    row = reg.get("messenger", "111") or {}
    assert (row.get("meta") or {}).get("offline_reason") == "superseded:222"
    assert s.dump()["sessions"]["messenger:111"]["status"] == "logged_out"


def test_mark_superseded_leaves_operator_and_online_rows_alone():
    """operator（运营主动登出）与 online 行不许被改写——审计语义/运营决策优先。"""
    from src.integrations.account_registry import get_account_registry
    from src.integrations.platform_session_health import (
        mark_superseded_accounts,
    )
    reg = get_account_registry()
    reg.upsert("messenger", "A", status="offline",
               meta={"offline_reason": "worker:expired"})
    reg.upsert("messenger", "B", status="offline",
               meta={"offline_reason": "operator"})
    reg.upsert("messenger", "C", status="online",
               meta={"offline_reason": "worker:needs_login"})
    mark_superseded_accounts("messenger", ["A", "B", "C"], "NEW")
    assert (reg.get("messenger", "A")["meta"]).get(
        "offline_reason") == "superseded:NEW"
    assert (reg.get("messenger", "B")["meta"]).get(
        "offline_reason") == "operator"
    assert (reg.get("messenger", "C")["meta"]).get(
        "offline_reason") == "worker:needs_login"


# ── ②⑤ 登出联动 + 横幅过滤/发送闸成对不变量 ────────────────────────────────

def test_snapshot_shows_needs_login_hides_logged_out():
    from src.integrations.account_registry import get_account_registry
    from src.web.routes.unified_inbox_setup_routes import (
        _channel_health_snapshot,
    )
    # 状态中心 v2 起快照带「期望在线」策略过滤：无注册表行=幽灵不亮，
    # 故先立一行 online（真实账号的正常形态）
    get_account_registry().upsert("messenger", "111", status="online")
    s = _store()
    s.record("messenger", "111", "needs_login")
    snap = _channel_health_snapshot()
    assert [it["account_id"] for it in snap["unhealthy"]] == ["111"]

    # 运营登出联动的核心语义：翻 logged_out → 横幅立即灭、发送闸仍拦
    s.record("messenger", "111", "logged_out", detail="operator logout")
    snap2 = _channel_health_snapshot()
    assert snap2["unhealthy"] == []
    assert s.is_unhealthy("messenger", "111") is True


def test_logout_and_remove_routes_wire_health_sync():
    """静态接线 ratchet：登出/删除路由必须调用 _sync_health_logged_out。

    此前红条要挂到下一次进程重启才消失——谁把这两处调用删了，同病复发。
    """
    src = (_ENGINE_ROOT / "src" / "web" / "routes"
           / "unified_inbox_account_routes.py").read_text(encoding="utf-8")
    calls = [ln for ln in src.splitlines()
             if "_sync_health_logged_out(" in ln and "def " not in ln]
    assert len(calls) >= 2, (
        "登出/删除路由的健康表联动被移除？红条会退回「挂到下次重启」的旧病：%r"
        % calls)


# ── ③ relogin 分诊 ──────────────────────────────────────────────────────────

def test_relogin_route_maps_404_and_worker_down_to_distinct_keys():
    src = (_ENGINE_ROOT / "src" / "web" / "routes"
           / "ops_overview_routes.py").read_text(encoding="utf-8")
    assert "err.psess.relogin_no_profile" in src, "404（档案没了）专用文案键被移除"
    assert "err.psess.relogin_worker_down" in src, "worker 宕机专用文案键被移除"
    # 404 必须映射到 no_profile（而不是笼统 op_failed 透传 httpx 原文）
    seg = src.split("err.psess.relogin_no_profile")[0][-400:]
    assert "404" in seg, "404 分支与 no_profile 文案的绑定被改动"


def test_relogin_i18n_keys_bilingual_and_no_raw_url_guidance():
    from src.web.web_i18n import get_translations
    zh = get_translations("zh")
    en = get_translations("en")
    for key in ("err.psess.relogin_no_profile", "err.psess.relogin_worker_down"):
        assert zh.get(key), f"{key} 缺中文文案"
        assert en.get(key), f"{key} 缺英文文案"
        # 人话文案不得携带内部 URL/端口（曾把 127.0.0.1:8791 直糊给坐席）
        assert "127.0.0.1" not in zh[key] and "http" not in zh[key].lower()


# ── ④ 催办衰减 ─────────────────────────────────────────────────────────────

def _aged_store(t0: float):
    """置一条 needs_login 且把 unhealthy_since 拨回 t0（record 用墙钟，测试拨表）。"""
    s = _store()
    s.record("messenger", "111", "needs_login")
    s._sessions["messenger:111"]["unhealthy_since"] = t0
    s._sessions["messenger:111"]["last_remind_ts"] = 0.0
    return s

_H = 3600.0


def test_due_reminders_stale_decay_slows_down_old_corpses():
    t0 = 1_000_000.0
    s = _aged_store(t0)
    kw = dict(min_age_sec=30 * 60, interval_sec=4 * _H,
              stale_after_sec=48 * _H, stale_interval_sec=24 * _H)
    # 30min 后首提
    assert "messenger:111" in s.due_reminders(now=t0 + 31 * 60, **kw)
    # 常规窗内（4h 未满）不重提
    assert s.due_reminders(now=t0 + 3 * _H, **kw) == {}
    # 进入陈旧带（>48h）：距上次已 >24h → 提（日更保底）
    due = s.due_reminders(now=t0 + 49 * _H, **kw)
    assert "messenger:111" in due
    assert due["messenger:111"]["down_sec"] == pytest.approx(49 * _H)
    # 衰减核心断言：再过 6h（旧 4h 间隔早该再轰）陈旧带内保持静默
    assert s.due_reminders(now=t0 + 55 * _H, **kw) == {}
    # 满 24h 才再提
    assert "messenger:111" in s.due_reminders(now=t0 + 73.1 * _H, **kw)


def test_due_reminders_stale_zero_keeps_legacy_cadence():
    t0 = 1_000_000.0
    s = _aged_store(t0)
    kw = dict(min_age_sec=30 * 60, interval_sec=4 * _H,
              stale_after_sec=0.0, stale_interval_sec=0.0)
    assert "messenger:111" in s.due_reminders(now=t0 + 49 * _H, **kw)
    # 关衰减＝旧行为：4h 后照常再提
    assert "messenger:111" in s.due_reminders(now=t0 + 53.1 * _H, **kw)


def test_recovery_clears_remind_state():
    t0 = 1_000_000.0
    s = _aged_store(t0)
    s.due_reminders(now=t0 + 31 * 60, min_age_sec=1800, interval_sec=4 * _H)
    s.record("messenger", "111", "authorized")
    assert s.due_reminders(now=t0 + 100 * _H, min_age_sec=1800,
                           interval_sec=4 * _H) == {}


def test_watchdog_passes_stale_params():
    """静态接线 ratchet：看门狗必须把衰减参数递给 due_reminders（缺省 48h/日更）。"""
    src = (_ENGINE_ROOT / "src" / "inbox"
           / "health_watchdog.py").read_text(encoding="utf-8")
    assert "stale_after_hours" in src and "stale_interval_min" in src
    assert "stale_after_sec=stale_after_sec" in src


# ── 横幅前端契约（模板热更新直上生产，静态钉住关键接线）────────────────────

def test_banner_template_stale_class_and_dismiss_wired():
    """实施75 batch3：顶栏红条退役，生命周期语义换渲染面重新钉住——
    右下胶囊（error）+ >48h 陈旧降档（tone→warn，原 .stale 语义）+
    签名暂缓（ws_chandown_snooze 键不变）+ 三出路卡片（重登/登出/暂缓）。"""
    tpl = (_ENGINE_ROOT / "src" / "web" / "templates"
           / "workspace_base.html").read_text(encoding="utf-8")
    assert 'id="ws-chandown"' not in tpl, "通道离线横幅不得回归顶部（实施75 batch3）"
    assert "AITRNotify.ongoing.set('chandown'" in tpl, "右下胶囊接线丢失"
    assert "downMin >= 2880" in tpl, "48h 分界常量丢失（与看门狗 stale_after_hours=48 同刻度）"
    assert "stale ? 'warn' : 'error'" in tpl, "陈旧降档（红→琥珀）语义被移除"
    assert "ws_chandown_snooze" in tpl, "24h 本地暂缓的存储键被改名/移除"
    assert "_chanSnoozed(sig)" in tpl, "签名暂缓判定被移除（异常集合变化须重新提示）"
    assert "ws.chandown.dismiss_t" in tpl, "暂缓出路（原 ×）被移除"
    assert "_chanRelogin(first)" in tpl and "_chanLogout(first)" in tpl, (
        "重登/登出出路从卡片上消失——坐席只能干看着掉线")
    assert "ws.chandown.profile_gone" in tpl, "「档案已不存在」分诊文案被移除"


def test_banner_i18n_keys_bilingual():
    from src.web.web_i18n import get_translations
    zh = get_translations("zh")
    en = get_translations("en")
    keys = (
        "ws.chandown.text2", "ws.chandown.dur_dh", "ws.chandown.dur_hm",
        "ws.chandown.dur_m", "ws.chandown.dismiss_t", "ws.chandown.logout_btn",
        "ws.chandown.logout_confirm", "ws.chandown.logout_ok",
        "ws.chandown.logout_fail", "ws.chandown.logout_denied",
        "ws.chandown.profile_gone", "ws.chandown.relogin_ok2",
        "ws.chandown.waiting",
    )
    for key in keys:
        assert zh.get(key), f"{key} 缺中文"
        assert en.get(key), f"{key} 缺英文"


# ── 横幅仲裁器（单槽 + 折叠角标）契约 ────────────────────────────────────────

def test_banner_arbiter_contract_pinned():
    """仲裁器＝「同屏最多一条横幅」的结构保证（_api_fetch.html）。

    钉三件事：① 全站横幅预登记且优先级序不塌（连接>通道>拦截>AI 降级>冷却）；
    ② 单槽仲裁循环在场；③ 折叠角标（被压掉的横幅要「讲出来」）在场。
    """
    tpl = (_ENGINE_ROOT / "src" / "web" / "templates"
           / "_api_fetch.html").read_text(encoding="utf-8")
    import re
    prios = dict(re.findall(
        r"wsBanner\.register\('([\w-]+)',\s*(\d+)", tpl))
    for bid in ("conn-banner", "ws-chandown", "ws-delivblock",
                "ws-aidegrade", "ws-restartcool"):
        assert bid in prios, f"横幅 {bid} 未预登记进仲裁器"
    assert (int(prios["conn-banner"]) > int(prios["ws-chandown"])
            > int(prios["ws-delivblock"]) > int(prios["ws-aidegrade"])
            > int(prios["ws-restartcool"])), "横幅优先级序被改动"
    assert "_renderStackChip" in tpl, "折叠角标（＋N）被移除"
    from src.web.web_i18n import get_translations
    zh = get_translations("zh")
    en = get_translations("en")
    for key in ("ws.banner.more_title", "ws.banner.name.chandown",
                "ws.banner.name.aidegrade"):
        assert zh.get(key) and en.get(key), f"{key} 双语缺失"


# ── 账号终局 → 待处理草稿连带作废（2026-08-27 十日孤儿稿沉淀）────────────────

def test_cancel_drafts_for_account_sweeps_pending_only(tmp_path):
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")

    def _mk(did, acct, status):
        store.upsert_draft({
            "source_kind": "inbox", "source_id": did.split(":", 1)[1],
            "draft_id": did, "platform": "messenger",
            "account_id": acct, "chat_key": "c1",
            "conversation_id": f"messenger:{acct}:c1",
            "draft_text": "x", "status": status,
            "autopilot_level": "L1", "risk_level": "low",
        })

    _mk("inbox:a1", "111", "pending")
    _mk("inbox:a2", "111", "enriching")
    _mk("inbox:a3", "111", "approved")   # 终态不动（审计事实）
    _mk("inbox:b1", "222", "pending")    # 别的账号不动
    n = store.cancel_drafts_for_account("messenger", "111",
                                        decided_by="account_removed")
    assert n == 2
    rows = {d["draft_id"]: d["status"]
            for d in store.list_drafts(limit=50, status=None)}
    assert rows["inbox:a1"] == "cancelled"
    assert rows["inbox:a2"] == "cancelled"
    assert rows["inbox:a3"] == "approved"
    assert rows["inbox:b1"] == "pending"
    assert store.cancel_drafts_for_account("", "") == 0
    store.close()


def test_remove_route_wires_draft_sweep():
    src = (_ENGINE_ROOT / "src" / "web" / "routes"
           / "unified_inbox_account_routes.py").read_text(encoding="utf-8")
    assert "cancel_drafts_for_account(" in src, (
        "账号删除不再连带作废待处理草稿？十日孤儿稿之病复发")


# ── 状态中心 v2（2026-08-27）────────────────────────────────────────────────

def test_banner_policy_matrix_expected_online_single_source():
    """横幅只亮「期望在线」的号——与看门狗催办同一判据（口径分裂即回归）。

    矩阵：online→亮；offline+worker:*（自灭未处理）→亮；offline+operator
    （运营已登出）/ offline+superseded:*（登录位已接替）/ removed / 无注册表行
    （登录尝试幽灵 msg_*）→ 一律不亮。worker 重启用陈旧 cookie 重推 needs_login
    也点不红已处理的号（Calixa 僵尸最后一条上游通路在此闭合）。
    """
    from src.integrations.account_registry import get_account_registry
    from src.web.routes.unified_inbox_setup_routes import (
        _channel_health_snapshot,
    )
    reg = get_account_registry()
    reg.upsert("messenger", "A", status="online")
    reg.upsert("messenger", "B", status="offline",
               meta={"offline_reason": "worker:needs_login"})
    reg.upsert("messenger", "C", status="offline",
               meta={"offline_reason": "operator"})
    reg.upsert("messenger", "D", status="offline",
               meta={"offline_reason": "superseded:NEW"})
    reg.upsert("messenger", "E", status="removed")
    s = _store()
    for acct in ("A", "B", "C", "D", "E", "msg_ghost1"):
        s.record("messenger", acct, "needs_login")
    shown = {it["account_id"]
             for it in _channel_health_snapshot()["unhealthy"]}
    assert shown == {"A", "B"}


def test_expected_online_policy_is_single_sourced_for_watchdog():
    """看门狗保留的 _session_expected_online 必须委托健康表模块的单一策略源。"""
    src = (_ENGINE_ROOT / "src" / "inbox"
           / "health_watchdog.py").read_text(encoding="utf-8")
    seg = src.split("def _session_expected_online")[1][:1600]
    assert "session_expected_online(key)" in seg, (
        "看门狗自带一套期望在线判据＝与横幅口径再度分裂")


def test_relogin_trace_lands_in_session_row_and_snapshot():
    """重登痕迹（谁/何时）落会话行并随横幅快照可见——多坐席防重复处理。"""
    from src.integrations.account_registry import get_account_registry
    from src.web.routes.unified_inbox_setup_routes import (
        _channel_health_snapshot,
    )
    get_account_registry().upsert("messenger", "111", status="online")
    s = _store()
    s.record("messenger", "111", "needs_login")
    s.record_relogin("messenger", "111", by="老板")
    row = s.dump()["sessions"]["messenger:111"]
    assert row["last_relogin_ts"] > 0
    assert row["last_relogin_by"] == "老板"
    item = _channel_health_snapshot()["unhealthy"][0]
    assert item["relogin_ts"] > 0 and item["relogin_by"] == "老板"
    # 旧签名兼容：不带 by 不抛、不覆盖已有操作者
    s.record_relogin("messenger", "111")
    assert s.dump()["sessions"]["messenger:111"]["last_relogin_by"] == "老板"


def test_relogin_route_passes_actor_to_trace():
    src = (_ENGINE_ROOT / "src" / "web" / "routes"
           / "ops_overview_routes.py").read_text(encoding="utf-8")
    assert "record_relogin(platform, acct,\n" in src.replace("\r\n", "\n"), (
        "relogin 路由的痕迹调用被改动")
    assert "by=actor" in src, "relogin 路由不再传操作者＝痕迹匿名化"


def test_banner_stack_v2_expand_contract():
    """＋N 角标可点开展开（真实元素原地叠出）+ 无障碍属性 + 双语键。"""
    tpl = (_ENGINE_ROOT / "src" / "web" / "templates"
           / "_api_fetch.html").read_text(encoding="utf-8")
    assert "_stackExpanded" in tpl, "展开态被移除——角标退回只能悬停看名字"
    assert "aria-expanded" in tpl and "role', 'button" in tpl.replace('"', "'")
    assert "_wireStackDismiss" in tpl, "外点/Esc 收起接线被移除"
    base = (_ENGINE_ROOT / "src" / "web" / "templates"
            / "workspace_base.html").read_text(encoding="utf-8")
    assert "relogin_trace" in base, "横幅重登痕迹渲染被移除"
    from src.web.web_i18n import get_translations
    zh = get_translations("zh")
    en = get_translations("en")
    for key in ("ws.banner.expand_t", "ws.banner.collapse_t",
                "ws.chandown.relogin_trace", "ws.chandown.relogin_trace_sb"):
        assert zh.get(key) and en.get(key), f"{key} 双语缺失"
