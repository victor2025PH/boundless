# -*- coding: utf-8 -*-
"""额度状态单一事实源（quotawall v2，2026-08-21）门禁。

三层：
1. ``resolve_quota_state`` 纯函数——裁决优先级（chars/trial > agent > tok >
   daily > low > ok）/ tok 的 enforce+funded 双前提 / daily 不进墙不抢 pill /
   体验档单列 trial 变体；
2. ``/api/workspace/quota`` 端点集成——state.feat 握手随轮询下发、legacy 字段
   一个不动（老前端零影响）、chars 用尽在体验档布局下裁决为 trial 墙；
3. 额度拦截抛点的机器可读头 ``X-AITR-Quota``——静态契约（挪走/漏带即红）+
   translate 守卫的行为级断言（403 + header 同时成立）。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from starlette.testclient import TestClient

from src.licensing.quota_state import (
    QUOTA_STATE_FEAT,
    WALL_AGENT,
    WALL_CHARS,
    WALL_NONE,
    WALL_TOK,
    WALL_TRIAL,
    collect_quota_state,
    forecast_exhaustion,
    resolve_quota_state,
)

_REPO = Path(__file__).resolve().parents[1]


def _q(**kw):
    base = {
        "included_chars": 1000, "used_chars": 0, "remaining_chars": 1000,
        "exceeded": False, "source": "license",
        "trial_hours_left": None, "trial_expired": False,
    }
    base.update(kw)
    return base


def _wallet(**kw):
    base = {"enabled": True, "enforce": True, "balance": 0,
            "active_granted": 100, "expired_lost": 0, "monthly": 0}
    base.update(kw)
    return base


# ── 1. 纯函数：裁决优先级与各表前提 ─────────────────────────────────────────

def test_ok_default():
    st = resolve_quota_state(quota=_q(), level="ok")
    assert st["verdict"] == "ok" and st["wall"] == WALL_NONE
    assert st["level"] == "ok" and st["feat"] == QUOTA_STATE_FEAT


def test_low_passthrough():
    st = resolve_quota_state(quota=_q(remaining_chars=100), level="low")
    assert st["verdict"] == "low" and st["wall"] == WALL_NONE and st["level"] == "low"


def test_chars_out():
    st = resolve_quota_state(quota=_q(exceeded=True), level="out")
    assert st["verdict"] == "chars_out" and st["wall"] == WALL_CHARS
    assert st["level"] == "out"


def test_trial_variant_on_exceeded():
    st = resolve_quota_state(
        quota=_q(source="local_trial", exceeded=True), level="out")
    assert st["verdict"] == "trial_out" and st["wall"] == WALL_TRIAL


def test_trial_variant_on_window_expiry_alone():
    """体验档「窗口到期但字符没用完」也必须拦——出路照样是注册领取。"""
    st = resolve_quota_state(
        quota=_q(source="local_trial", trial_expired=True), level="ok")
    assert st["wall"] == WALL_TRIAL and st["level"] == "out"


def test_chars_out_outranks_agent_and_tok():
    st = resolve_quota_state(
        quota=_q(exceeded=True), level="out",
        wallet=_wallet(), agent={"allowed": False, "enabled": True,
                                 "used": 9, "quota": 9})
    assert st["wall"] == WALL_CHARS  # 一次只讲一件事，讲最重的


def test_agent_out_alone():
    st = resolve_quota_state(
        quota=_q(), level="ok",
        agent={"allowed": False, "enabled": True, "enforce": True,
               "used": 500, "quota": 500})
    assert st["verdict"] == "agent_out" and st["wall"] == WALL_AGENT
    assert st["level"] == "out"           # 字符池充足也要红：对该坐席就是全挡
    assert st["agent"]["blocked"] is True
    assert st["agent"]["used"] == 500 and st["agent"]["quota"] == 500


def test_agent_outranks_tok():
    st = resolve_quota_state(
        quota=_q(), level="ok", wallet=_wallet(),
        agent={"allowed": False, "enabled": True, "used": 1, "quota": 1})
    assert st["wall"] == WALL_AGENT


def test_tok_out_requires_enforce_and_funded():
    # enforce 关 = observe-only：绝不弹墙（与 should_degrade_action 同源语义）
    st = resolve_quota_state(quota=_q(), level="ok",
                             wallet=_wallet(enforce=False))
    assert st["wall"] == WALL_NONE and st["verdict"] == "ok"
    # 从未注资：不适用 Token 语义（防 enforce 一开存量部署全体被标用尽）
    st = resolve_quota_state(quota=_q(), level="ok",
                             wallet=_wallet(active_granted=0, expired_lost=0))
    assert st["wall"] == WALL_NONE
    # 余额还有：不弹
    st = resolve_quota_state(quota=_q(), level="ok", wallet=_wallet(balance=5))
    assert st["wall"] == WALL_NONE
    # 三前提齐 → tok 墙
    st = resolve_quota_state(quota=_q(), level="ok", wallet=_wallet())
    assert st["verdict"] == "tok_out" and st["wall"] == WALL_TOK
    assert st["level"] == "out" and st["tok"]["out"] is True


def test_tok_funded_via_expired_lost():
    """曾注资但批次全过期作废（expired_lost>0）同样算 funded——语义同源。"""
    st = resolve_quota_state(
        quota=_q(), level="ok",
        wallet=_wallet(active_granted=0, expired_lost=50))
    assert st["wall"] == WALL_TOK


def test_daily_out_reports_but_never_walls_nor_pills():
    """托管日额度：verdict 如实报告（供互斥/观测），但不进额度墙、不抢 pill——
    它的弹层与横幅归 #ws-aitrial 体系（每日一次治理）。"""
    st = resolve_quota_state(
        quota=_q(), level="ok",
        hosted={"enabled": True, "exhausted": True, "busy": False,
                "remaining": 0, "budget": 60000})
    assert st["verdict"] == "daily_out" and st["wall"] == WALL_NONE
    assert st["level"] == "ok"


def test_daily_busy_is_not_out():
    """通道全局繁忙不是用户的额度问题——与升级窗同判据，不推销。"""
    st = resolve_quota_state(
        quota=_q(), level="ok",
        hosted={"enabled": True, "exhausted": True, "busy": True})
    assert st["verdict"] == "ok"


def test_hosted_probe_error_ignored():
    st = resolve_quota_state(
        quota=_q(), level="ok",
        hosted={"enabled": True, "error": "network", "exhausted": True})
    assert st["verdict"] == "ok" and st["hosted"]["enabled"] is False


def test_none_inputs_are_safe():
    st = resolve_quota_state(quota=None, level="ok")
    assert st["verdict"] == "ok" and st["wall"] == WALL_NONE


def test_forecast_exhaustion_math_and_honesty():
    """近 7 天日均外推：算得出给数字，算不出绝不编（None）。"""
    # 1000 剩余 ÷ 日均 100 → 10 天
    fc = forecast_exhaustion(1000, [100, 100, 100, 100, 100, 100, 100])
    assert fc == {"days_left": 10.0, "burn7": 100}
    # 不限量 / 无历史 / 零消耗 → None（诚实边界）
    assert forecast_exhaustion(None, [100]) is None
    assert forecast_exhaustion(1000, []) is None
    assert forecast_exhaustion(1000, [0, 0, 0]) is None
    # 负数脏值按 0 处理；remaining 0 → days_left 0（已经没了）
    assert forecast_exhaustion(0, [50, -3, 50])["days_left"] == 0.0
    # 异常入参不抛
    assert forecast_exhaustion("x", object()) is None


def test_daily_totals_zero_filled_old_to_new(tmp_path):
    """store.daily_totals：旧→新逐日、缺日补零（外推分母=窗口长度不是有数日数）。"""
    qs.configure_license_quota_store(db_path=str(tmp_path / "d.db"))
    store = qs._ensure_store()
    import time as _t
    now = _t.time()
    store.record("lic-a", "translation", 300)             # 今天
    # 手工造一条 3 天前的记录（record 只会写今天，直接补 SQL 行）
    day3 = _t.strftime("%Y-%m-%d", _t.gmtime(now - 3 * 86400))
    with store._lock:
        store._conn.execute(
            "INSERT INTO license_char_usage (lic_id, day, category, chars) "
            "VALUES (?, ?, ?, ?)", ("lic-a", day3, "translation", 700))
        store._conn.commit()
    days = store.daily_totals("lic-a", 7)
    assert len(days) == 7
    assert days[-1] == 300 and days[3] == 700
    assert sum(days) == 1000
    # 其他授权互不串味
    assert qs.current_daily_totals(7, lic_id="lic-b") == [0] * 7


def test_no_credential_words_in_state():
    """本 state 随坐席可读端点下发——字段名/裁决值不得含 token/customer/lic_id
    等会触发防泄漏门禁的字样（Token 段刻意叫 tok/tok_out 就是为此）。"""
    st = resolve_quota_state(
        quota=_q(exceeded=True), level="out",
        wallet=_wallet(), hosted={"enabled": True, "exhausted": True},
        agent={"allowed": False, "enabled": True, "used": 1, "quota": 1})
    blob = str(st).lower()
    for leak in ("token", "customer", "lic_id"):
        assert leak not in blob


# ── 2. 端点集成（复用 pill 门禁同款脚手架） ─────────────────────────────────

from src.licensing import local_trial as lt  # noqa: E402
from src.licensing import quota_store as qs  # noqa: E402
from src.licensing import token_ledger as tl  # noqa: E402
from src.web.routes.license_routes import register_license_routes  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate():
    lt.reset_local_trial()
    qs.reset_license_quota_store()
    tl.reset_token_ledger()
    yield
    lt.reset_local_trial()
    qs.reset_license_quota_store()
    tl.reset_token_ledger()


def _client():
    app = FastAPI()
    register_license_routes(app, api_auth=lambda request: None)
    return TestClient(app)


def _wire_trial(tmp_path, *, chars=10_000, window=48.0, used=0):
    qs.configure_license_quota_store(db_path=str(tmp_path / "q.db"))
    t = lt.LocalTrial(str(tmp_path / "local_trial.json"), chars=chars,
                      window_hours=window, machine_short="ab12cd34")
    lt.configure_local_trial({"licensing": {"trial": {"enabled": True}}}, trial=t)
    t.begin(now=time.time())
    if used:
        store = qs._ensure_store()
        store.record(t.lic_id(), "translation", used)
    return t


def test_endpoint_carries_state_feat(tmp_path):
    _wire_trial(tmp_path, used=1500)
    d = _client().get("/api/workspace/quota").json()
    st = d.get("state")
    assert isinstance(st, dict) and st["feat"] == QUOTA_STATE_FEAT
    assert st["verdict"] == "ok" and st["wall"] == WALL_NONE
    # legacy 字段一个不动（老前端零影响）
    assert d["level"] == "ok" and d["visible"] is True and d["remaining"] == 8500
    # 未配置 shop_url → 空串=前端不渲染充值按钮
    assert st["shop_url"] == ""
    # 预计耗尽（P2）：今天烧了 1500、剩 8500 → 日均 1500/7、约 39.7 天
    fc = st.get("forecast")
    assert fc and fc["burn7"] == 1500 // 7
    assert 39.0 <= float(fc["days_left"]) <= 40.5
    # lic_id 绝不入响应（防泄漏门禁同口径）
    assert "lic_id" not in str(d)


def test_endpoint_state_trial_wall_when_out(tmp_path):
    _wire_trial(tmp_path, chars=1000, used=1000)
    d = _client().get("/api/workspace/quota").json()
    assert d["level"] == "out"                      # legacy
    st = d["state"]
    assert st["wall"] == WALL_TRIAL and st["verdict"] == "trial_out"
    assert st["level"] == "out"


def test_endpoint_state_failure_keeps_legacy(monkeypatch, tmp_path):
    """合议失败只丢 state，legacy 徽章字段绝不陪葬。"""
    _wire_trial(tmp_path, used=10)
    import src.licensing.quota_state as qstate

    def _boom(**kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(qstate, "collect_quota_state", _boom)
    d = _client().get("/api/workspace/quota").json()
    assert d["ok"] is True and "state" not in d
    assert d["visible"] is True and d["level"] == "ok"


# ── 3. 机器可读头 X-AITR-Quota ──────────────────────────────────────────────

_HEADER = "X-AITR-Quota"
#: (路由文件, 抛点特征串)——每个特征串的每次出现，其后 420 字符窗口内必须带头。
_SITES = [
    ("src/web/routes/unified_inbox_translate_routes.py", "err.quota.agent_chars_exhausted"),
    ("src/web/routes/unified_inbox_translate_routes.py", "err.lic.chars_exhausted"),
    ("src/web/routes/voice_routes.py", "err.quota.agent_chars_exhausted"),
    ("src/web/routes/voice_routes.py", "err.lic.chars_exhausted"),
    ("src/web/routes/unified_inbox_send_routes.py", "err.quota.agent_chars_exhausted"),
    ("src/web/routes/unified_inbox_send_routes.py", "err.lic.chars_exhausted"),
]


def test_quota_raise_sites_carry_machine_header():
    for rel, needle in _SITES:
        src = (_REPO / rel).read_text(encoding="utf-8")
        hits = [m.start() for m in re.finditer(re.escape(needle), src)]
        assert hits, f"{rel}: 找不到抛点特征 {needle}（路由改名请同步本契约）"
        for pos in hits:
            window = src[pos:pos + 420]
            assert _HEADER in window, (
                f"{rel}: {needle} 抛点缺 {_HEADER} 机器头——前端额度墙的"
                f"动作点触发全靠它（quotawall v2）")


def test_enforce_agent_quota_403_header_behavioral(monkeypatch):
    from src.web.routes import unified_inbox_translate_routes as m

    monkeypatch.setattr(
        m, "check_request_quota",
        lambda req: {"allowed": False, "used": 9, "quota": 9})

    class _St:
        ui_lang = "zh"

    class _Req:
        state = _St()

    with pytest.raises(HTTPException) as ei:
        m._enforce_agent_quota(_Req())
    assert ei.value.status_code == 403
    assert (ei.value.headers or {}).get(_HEADER) == "agent_chars"


def _translate_client(translate_result):
    """手动翻译主入口最小装配：真 TranslationService + 桩 translate。

    api_auth 必须是**带 Request 标注**的函数——该路由族用 ``Depends(api_auth)``
    消费，裸 lambda 的无标注参数会被 FastAPI 当成必填 query 参数 → 全端点 422。
    且标注必须用**模块级**导入的 Request：本模块开着 ``from __future__ import
    annotations``（PEP 563 注解字符串化），FastAPI 按函数 __globals__ 解析注解，
    函数内局部导入的别名解析不到 → 同样退化成必填 query 参数（已实锤）。
    """
    from src.ai.translation_service import TranslationService
    from src.web.routes.unified_inbox_translate_routes import (
        register_translate_routes,
    )

    def _auth(request: Request):
        return None

    app = FastAPI()
    register_translate_routes(app, api_auth=_auth)
    svc = TranslationService(ai_client=None)

    async def _stub(text, **kw):
        return translate_result

    svc.translate = _stub  # type: ignore[method-assign]
    app.state.translation_service = svc
    return TestClient(app, raise_server_exceptions=False)


def test_manual_translate_converts_quota_softfail_to_402():
    """服务层软失败（license_quota_exceeded）在手动翻译主入口必须转 402+机器头。

    没有这层转换：额度用尽点「翻译」= HTTP 200 回原文，额度墙动作点永不触发
    （voice/send 会弹、translate 静默，坐席看到的是「翻译坏了」而不是「该充值了」）。
    """
    from src.ai.translation_service import TranslationResult
    from src.licensing.quota_store import QUOTA_EXCEEDED_ERROR

    r = _translate_client(TranslationResult(
        "hi", "hi", "en", "zh", False,
        provider="license", error=QUOTA_EXCEEDED_ERROR,
    )).post("/api/unified-inbox/translate",
            json={"text": "hi", "target_lang": "zh"})
    assert r.status_code == 402
    assert r.headers.get(_HEADER) == "license_chars"


def test_manual_translate_other_failures_stay_softfail():
    """只有额度错误升 402；引擎不可用等其他失败保持 200 软失败（不误伤）。"""
    from src.ai.translation_service import TranslationResult

    r = _translate_client(TranslationResult(
        "hi", "hi", "en", "zh", False,
        provider="none", error="no_translation_engine",
    )).post("/api/unified-inbox/translate",
            json={"text": "hi", "target_lang": "zh"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False
    assert d["translation"]["error"] == "no_translation_engine"


# ── 4. 前端接线静态契约（模板热更新直上生产，接线断=静默缺陷） ────────────────

def test_frontend_wall_wiring():
    tpl = (_REPO / "src/web/templates/workspace_base.html").read_text(encoding="utf-8")
    # 调度器 + 动作点事件 + 互斥 + 充值闭环四件套
    assert "__wsQuotaWall" in tpl
    assert "aitr:quota-blocked" in tpl
    assert "aitr:quota-state" in tpl
    assert "shownRecently" in tpl
    assert "topup-voucher" in tpl          # 弹层内嵌凭证兑换
    assert "qw_cta_buy" in tpl             # 充值 CTA 埋点
    fetch_tpl = (_REPO / "src/web/templates/_api_fetch.html").read_text(encoding="utf-8")
    assert _HEADER in fetch_tpl and "aitr:quota-blocked" in fetch_tpl
    # P2 → 实施75 batch2：预警从顶部条迁右下胶囊+卡片（同步函数/贪睡日键/埋点保留）
    assert 'id="ws-quotalow"' not in tpl, "预警横幅不得回归顶部（实施75）"
    assert "AITRNotify.ongoing.set('quotalow'" in tpl
    assert "_qwLowSync" in tpl and "qw.low.snooze." in tpl
    assert "qw_low_shown" in tpl and "qw_low_buy" in tpl
    assert "window.__qwWatchMs" in tpl
    assert "ws.quotalow.tip" in tpl


def test_wall_i18n_keys_bilingual():
    from src.web.web_i18n import get_translations
    keys = [
        "ws.quotawall.title_trial", "ws.quotawall.body_trial", "ws.quotawall.claim",
        "ws.quotawall.title_token", "ws.quotawall.body_token",
        "ws.quotawall.title_agent", "ws.quotawall.body_agent",
        "ws.quotawall.recharge", "ws.quotawall.watching",
        "ws.quotawall.credited", "ws.quotawall.recovered",
        "ws.quotawall.voucher_err",
        "base.pill.quota_tokens", "base.pill.quota_tip_token",
        "base.pill.quota_tip_agent",
        "ws.quotalow.text", "ws.quotalow.text_agent",
        "ws.quotalow.later", "ws.quotalow.tip",
    ]
    for lang in ("zh", "en"):
        t = get_translations(lang)
        for k in keys:
            assert str(t.get(k) or "").strip(), f"[{lang}] 缺 {k}"


# ── 5. 服务端到账真值（P2.5 对账线）────────────────────────────────────────
#
# qw_credited 埋点只统计「页面开着等到 watch 命中」的场景（关页即漏计），
# 漏斗卡的对账分母读 license_char_topup 台账（凭证兑换/手工直充/自动履约
# 同一张表）。这里钉：店级按日聚合语义 + 端点总数口径 + 前端接线。

def test_topup_daily_counts_aggregates_and_zero_fills():
    store = qs.LicenseQuotaStore(":memory:")
    assert store.add_topup("L1", 100, "ORD-1")
    assert store.add_topup("L1", 250, "ORD-2")
    assert not store.add_topup("L1", 999, "ORD-1")   # ref 幂等：不重复入账
    store.add_topup("L2", 700, "ORD-OTHER")          # 别的授权不串味
    rows = store.topup_daily_counts("L1", days=7)
    assert len(rows) == 7
    assert all(r["n"] == 0 and r["chars"] == 0 for r in rows[:-1])  # 缺日补零
    today = rows[-1]
    assert today["n"] == 2 and today["chars"] == 350


def test_topup_trend_endpoint_totals_and_no_leak(tmp_path):
    t = _wire_trial(tmp_path)
    store = qs._ensure_store()
    store.add_topup(t.lic_id(), 50_000, "ORD-T1")
    store.add_topup(t.lic_id(), 30_000, "ORD-T2")
    d = _client().get("/api/admin/license/topup-trend").json()
    assert d["ok"] is True and len(d["days"]) == 14
    assert d["total_n"] == 2 and d["total_chars"] == 80_000
    # 只出聚合数字：lic_id / 订单号绝不入响应
    s = str(d)
    assert "lic_id" not in s and "ORD-T1" not in s


def test_topup_trend_endpoint_empty_without_license():
    d = _client().get("/api/admin/license/topup-trend").json()
    assert d["ok"] is True and d["total_n"] == 0 and d["total_chars"] == 0


def test_funnel_card_consumes_server_truth():
    tpl = (_REPO / "src/web/templates/ops_overview.html").read_text(encoding="utf-8")
    assert "/api/admin/license/topup-trend" in tpl
    assert "ov2_qwf_srv" in tpl
    from src.web.web_i18n import get_translations
    for lang in ("zh", "en"):
        assert str(get_translations(lang).get("ov2_qwf_srv") or "").strip(), \
            f"[{lang}] 缺 ov2_qwf_srv"
