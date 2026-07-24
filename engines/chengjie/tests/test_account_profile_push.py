"""账号官方资料修改（accounts.profile_push）单测。

覆盖纯函数（capabilities / validate_fields / cooldown / merge_push_meta /
prepare_avatar）+ 跨 loop helper + push_whatsapp / push_telegram 执行器（注入假
HTTP / 假 client，零真号）+ 路由守卫链与成功路径（假 registry / 假编排器）+
``_merge_orchestrator_status`` 的 state/state_detail 透传。

增量（人设一键填充 / 新号风控 / 掉线时长 / 可观测化）：
``resolve_persona_fill``（假 PersonaManager + tmp face_ref）、GET 的
persona/account_age_days/fresh 字段、POST ``use_persona`` 全链（face_ref 文件 →
WA payload 真带 avatar_b64 + persona_used + persona_fill 计数）与无人设 400、
推送漏斗计数/dump_prom 行格式、``unhealthy_since`` 经会话健康单例透传。
"""
from __future__ import annotations

import asyncio
import base64
import io
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.integrations import account_profile_push as pp


# ── 纯函数：capabilities ─────────────────────────────────────────────────────

def test_capabilities_known_platforms():
    assert pp.capabilities("telegram") == {
        "mode": "direct", "name": True, "status": True, "avatar": True}
    assert pp.capabilities("whatsapp") == {
        "mode": "direct", "name": True, "status": True, "avatar": True}
    assert pp.capabilities("line")["mode"] == "manual"
    assert pp.capabilities("messenger") == {
        "mode": "manual", "name": False, "status": False, "avatar": False}


def test_capabilities_unknown_platform_manual_all_false():
    caps = pp.capabilities("wechat")
    assert caps == {"mode": "manual", "name": False,
                    "status": False, "avatar": False}
    assert pp.capabilities("")["mode"] == "manual"
    # 大小写归一
    assert pp.capabilities("Telegram")["mode"] == "direct"


def test_capabilities_returns_copy():
    pp.capabilities("telegram")["name"] = False
    assert pp.PLATFORM_CAPS["telegram"]["name"] is True   # 不污染模块表


# ── 纯函数：validate_fields ──────────────────────────────────────────────────

def test_validate_fields_telegram_limits():
    assert pp.validate_fields("telegram", "x" * 64, "y" * 70) is None
    key, fmt = pp.validate_fields("telegram", "x" * 65, "")
    assert key == "err.acct.profile_name_too_long" and fmt == {"max": 64}
    key, fmt = pp.validate_fields("telegram", "", "y" * 71)
    assert key == "err.acct.profile_status_too_long" and fmt == {"max": 70}


def test_validate_fields_whatsapp_limits():
    assert pp.validate_fields("whatsapp", "x" * 25, "y" * 139) is None
    key, fmt = pp.validate_fields("whatsapp", "x" * 26, "")
    assert key == "err.acct.profile_name_too_long" and fmt == {"max": 25}
    key, fmt = pp.validate_fields("whatsapp", "", "y" * 140)
    assert key == "err.acct.profile_status_too_long" and fmt == {"max": 139}


def test_validate_fields_empty_and_unknown_platform():
    assert pp.validate_fields("telegram", "", "") is None
    assert pp.validate_fields("line", "x" * 999, "y" * 999) is None  # 无限制表


# ── 纯函数：cooldown_remaining ───────────────────────────────────────────────

def test_cooldown_remaining_no_ts_ready():
    assert pp.cooldown_remaining({}, time.time(), 24) == 0.0
    assert pp.cooldown_remaining(None, time.time(), 24) == 0.0
    assert pp.cooldown_remaining({"profile_push_last_ts": "junk"},
                                 time.time(), 24) == 0.0


def test_cooldown_remaining_active_and_expired():
    now = 1_000_000.0
    meta = {"profile_push_last_ts": now - 3600}
    remain = pp.cooldown_remaining(meta, now, 24)
    assert remain == pytest.approx(23 * 3600)
    assert pp.cooldown_remaining(meta, now + 24 * 3600, 24) <= 0
    # hours=0 → 冷却禁用
    assert pp.cooldown_remaining(meta, now, 0) == 0.0


def test_format_remaining_human_readable():
    assert pp.format_remaining(3.5 * 3600) == "3.5h"
    assert pp.format_remaining(60) == "0.1h"       # 下限
    assert pp.format_remaining(-5) == "0.1h"


# ── 纯函数：merge_push_meta ──────────────────────────────────────────────────

def test_merge_push_meta_preserves_and_records():
    existing = {"session_string": "SECRET", "self_name": "Lin"}
    merged = pp.merge_push_meta(existing, ["name", "status"], 1000.0, True)
    assert merged["session_string"] == "SECRET"    # 敏感键保住
    assert merged["self_name"] == "Lin"
    assert merged["profile_push_last_ts"] == 1000.0
    assert merged["profile_push_log"] == [
        {"ts": 1000.0, "fields": ["name", "status"], "ok": True}]
    assert "profile_push_last_ts" not in existing  # 不改入参
    assert "profile_push_log" not in existing


def test_merge_push_meta_failure_does_not_touch_last_ts():
    existing = {"profile_push_last_ts": 500.0}
    merged = pp.merge_push_meta(existing, ["avatar"], 1000.0, False)
    assert merged["profile_push_last_ts"] == 500.0  # 失败不烧冷却窗
    assert merged["profile_push_log"][-1] == {
        "ts": 1000.0, "fields": ["avatar"], "ok": False}


def test_merge_push_meta_log_truncates_to_10():
    meta: dict = {}
    for i in range(15):
        meta = pp.merge_push_meta(meta, ["name"], float(i), True)
    log = meta["profile_push_log"]
    assert len(log) == 10
    assert log[0]["ts"] == 5.0 and log[-1]["ts"] == 14.0  # 保最近 10 条


# ── 纯函数：prepare_avatar ───────────────────────────────────────────────────

def _png_bytes(w=100, h=50):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (255, 0, 0, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_prepare_avatar_squares_and_jpegs():
    from PIL import Image
    out = pp.prepare_avatar(_png_bytes(100, 50), 5)
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert img.size == (640, 640)
    assert img.mode == "RGB"


def test_prepare_avatar_too_large():
    data = _png_bytes(200, 200)
    with pytest.raises(ValueError) as ei:
        pp.prepare_avatar(data, max_mb=len(data) / (1024 * 1024) / 2)
    assert ei.value.args[0] == "err.acct.profile_avatar_too_large"


def test_prepare_avatar_garbage_invalid():
    with pytest.raises(ValueError) as ei:
        pp.prepare_avatar(b"not-an-image-at-all", 5)
    assert ei.value.args[0] == "err.acct.profile_avatar_invalid"
    with pytest.raises(ValueError):
        pp.prepare_avatar(b"", 5)


# ── run_on_client_loop（跨 loop 安全） ───────────────────────────────────────

async def test_run_on_client_loop_same_or_no_loop_direct_await():
    async def _job():
        return 41

    # loop=None → 直接 await
    assert await pp.run_on_client_loop(SimpleNamespace(loop=None), _job) == 41
    # loop=当前 loop → 直接 await
    cur = asyncio.get_running_loop()
    assert await pp.run_on_client_loop(SimpleNamespace(loop=cur), _job) == 41


async def test_run_on_client_loop_cross_loop_dispatch():
    other = asyncio.new_event_loop()
    t = threading.Thread(target=other.run_forever, daemon=True)
    t.start()
    try:
        ran_on = []

        async def _job():
            ran_on.append(asyncio.get_running_loop())
            return 42

        got = await pp.run_on_client_loop(SimpleNamespace(loop=other), _job)
        assert got == 42
        assert ran_on[0] is other   # 真在 client 的 loop 上执行
    finally:
        other.call_soon_threadsafe(other.stop)
        t.join(timeout=5)
        other.close()


async def test_run_on_client_loop_unwraps_inner_client():
    other = asyncio.new_event_loop()   # 不运行 → 应回落直 await 而非挂死
    try:
        wrapper = SimpleNamespace(client=SimpleNamespace(loop=other))

        async def _job():
            return "ok"

        assert await pp.run_on_client_loop(wrapper, _job) == "ok"
    finally:
        other.close()


# ── push_whatsapp（注入假 HTTP） ─────────────────────────────────────────────

_WA_CFG = {"platform_login": {"whatsapp": {"baileys_url": "http://svc:1"}}}


async def test_push_whatsapp_posts_nonempty_fields_only():
    calls = {}

    async def _fake(url, payload):
        calls["url"] = url
        calls["payload"] = payload
        return {"ok": True, "applied": {"name": True}, "pushname": "新名"}

    res = await pp.push_whatsapp(
        _WA_CFG, "wa1", name="新名", avatar_bytes=b"JPG", post_json=_fake)
    assert calls["url"] == "http://svc:1/accounts/wa1/profile"
    assert calls["payload"] == {
        "name": "新名", "avatar_b64": base64.b64encode(b"JPG").decode("ascii")}
    assert "status_text" not in calls["payload"]   # 空字段不带
    assert res["pushname"] == "新名"


async def test_push_whatsapp_404_maps_offline():
    class _HTTPErr(Exception):
        def __init__(self, code):
            self.response = SimpleNamespace(status_code=code)

    async def _fake404(url, payload):
        raise _HTTPErr(404)

    with pytest.raises(RuntimeError) as ei:
        await pp.push_whatsapp(_WA_CFG, "wa1", name="n", post_json=_fake404)
    assert ei.value.args[0] == "err.acct.profile_offline"

    async def _fake409(url, payload):
        raise _HTTPErr(409)

    with pytest.raises(RuntimeError) as ei:
        await pp.push_whatsapp(_WA_CFG, "wa1", name="n", post_json=_fake409)
    assert ei.value.args[0] == "err.acct.profile_offline"


async def test_push_whatsapp_network_error_maps_push_failed():
    async def _boom(url, payload):
        raise ConnectionError("refused")

    with pytest.raises(RuntimeError) as ei:
        await pp.push_whatsapp(_WA_CFG, "wa1", name="n", post_json=_boom)
    assert ei.value.args[0] == "err.acct.profile_push_failed"


async def test_push_whatsapp_body_not_connected_maps_offline():
    async def _fake(url, payload):
        return {"ok": False, "error": "not_connected"}

    with pytest.raises(RuntimeError) as ei:
        await pp.push_whatsapp(_WA_CFG, "wa1", name="n", post_json=_fake)
    assert ei.value.args[0] == "err.acct.profile_offline"


# ── push_telegram（假 client） ───────────────────────────────────────────────

class _FakeTg:
    """裸 pyrogram 形态假 client（loop=None → run_on_client_loop 直 await）。"""

    def __init__(self, fail_name=False):
        self.loop = None
        self.calls = []
        self.fail_name = fail_name

    async def update_profile(self, first_name=None, last_name=None, bio=None):
        self.calls.append(("update_profile", first_name, last_name, bio))
        if self.fail_name and first_name is not None:
            raise RuntimeError("FIRSTNAME_INVALID")
        return True

    async def set_profile_photo(self, photo=None):
        self.calls.append(("set_profile_photo", photo))
        return True

    async def get_me(self):
        return SimpleNamespace(first_name="新名", last_name="",
                               username="newme", photo=None)


async def test_push_telegram_all_fields_applied():
    cl = _FakeTg()
    res = await pp.push_telegram(cl, name="新名", bio="签名", avatar_path="a.jpg")
    assert res["applied"] == {"name": True, "status": True, "avatar": True}
    assert res["errors"] == {}
    # 改名清旧姓；bio 单独一次调用；头像走 set_profile_photo
    assert ("update_profile", "新名", "", None) in cl.calls
    assert ("update_profile", None, None, "签名") in cl.calls
    assert ("set_profile_photo", "a.jpg") in cl.calls


async def test_push_telegram_per_field_errors_isolated():
    cl = _FakeTg(fail_name=True)
    res = await pp.push_telegram(cl, name="新名", bio="签名")
    assert "name" in res["errors"] and "FIRSTNAME_INVALID" in res["errors"]["name"]
    assert res["applied"] == {"status": True}   # 昵称失败不拖累签名


async def test_push_telegram_unwraps_wrapper_client():
    inner = _FakeTg()
    wrapper = SimpleNamespace(client=inner)     # A 线 TelegramClient 包装形态
    res = await pp.push_telegram(wrapper, name="n")
    assert res["applied"] == {"name": True}
    assert inner.calls


async def test_push_telegram_no_usable_client_offline():
    with pytest.raises(RuntimeError) as ei:
        await pp.push_telegram(None, name="n")
    assert ei.value.args[0] == "err.acct.profile_offline"
    with pytest.raises(RuntimeError):
        await pp.push_telegram(SimpleNamespace(), name="n")  # 无 update_profile


# ── 路由（假 registry / 假编排器 / 假执行器） ────────────────────────────────

_CFG_ON = {"accounts": {"profile_push": {
    "enabled": True, "cooldown_hours": 24, "max_avatar_mb": 5}}}


class _FakeRegistry:
    def __init__(self, rows=None):
        self.rows = dict(rows or {})   # {(platform, account_id): row}
        self.upserts = []

    def get(self, platform, account_id):
        row = self.rows.get((platform, account_id))
        return dict(row) if row is not None else None

    def list(self, platform=None, *, include_removed=False):
        out = []
        for (plat, aid), row in self.rows.items():
            if platform and plat != str(platform).lower():
                continue
            if (not include_removed
                    and str(row.get("status") or "") == "removed"):
                continue
            out.append(dict(row))
        return out

    def upsert(self, platform, account_id, **kw):
        self.upserts.append((platform, account_id, kw))
        row = self.rows.setdefault(
            (platform, account_id),
            {"platform": platform, "account_id": account_id})
        for k, v in kw.items():
            if v is not None:
                row[k] = v
        return dict(row)


def _mk_client(cfg, registry, monkeypatch):
    from src.web.routes import unified_inbox_account_routes as acct_routes
    monkeypatch.setattr(acct_routes, "get_account_registry", lambda: registry)
    app = FastAPI()
    acct_routes.register_account_routes(
        app, api_auth=lambda request: None,
        config_manager=SimpleNamespace(config=cfg))
    return TestClient(app)


def test_route_post_flag_off_403(monkeypatch):
    c = _mk_client({}, _FakeRegistry(), monkeypatch)
    r = c.post("/api/accounts/telegram/123/profile", json={"name": "x"})
    assert r.status_code == 403


def test_route_post_manual_platform_400(monkeypatch):
    c = _mk_client(_CFG_ON, _FakeRegistry(), monkeypatch)
    for plat in ("line", "messenger"):
        r = c.post(f"/api/accounts/{plat}/a1/profile", json={"name": "x"})
        assert r.status_code == 400, r.text


def test_route_post_no_fields_400(monkeypatch):
    c = _mk_client(_CFG_ON, _FakeRegistry(), monkeypatch)
    r = c.post("/api/accounts/telegram/123/profile", json={})
    assert r.status_code == 400


def test_route_post_field_too_long_400(monkeypatch):
    c = _mk_client(_CFG_ON, _FakeRegistry(), monkeypatch)
    r = c.post("/api/accounts/whatsapp/wa1/profile", json={"name": "x" * 26})
    assert r.status_code == 400
    assert "25" in r.json()["detail"]


def test_route_post_bad_avatar_400(monkeypatch):
    c = _mk_client(_CFG_ON, _FakeRegistry(), monkeypatch)
    bad = base64.b64encode(b"garbage-bytes").decode("ascii")
    r = c.post("/api/accounts/telegram/123/profile", json={"avatar_b64": bad})
    assert r.status_code == 400
    # 非法 base64 也 400
    r2 = c.post("/api/accounts/telegram/123/profile",
                json={"avatar_b64": "%%%not-base64%%%"})
    assert r2.status_code == 400


def test_route_post_cooldown_429(monkeypatch):
    reg = _FakeRegistry({("whatsapp", "wa1"): {
        "platform": "whatsapp", "account_id": "wa1",
        "meta": {"profile_push_last_ts": time.time() - 60}}})
    c = _mk_client(_CFG_ON, reg, monkeypatch)
    r = c.post("/api/accounts/whatsapp/wa1/profile", json={"name": "新名"})
    assert r.status_code == 429
    assert "h" in r.json()["detail"]   # 人类可读剩余（如 "23.9h"）


def test_route_post_whatsapp_success(monkeypatch):
    import src.integrations.account_self_profile as sp
    reg = _FakeRegistry({("whatsapp", "wa1"): {
        "platform": "whatsapp", "account_id": "wa1", "label": "小雨",
        "meta": {"session_string": "KEEP", "baileys_login_id": "L1"}}})

    async def _fake_push(cfg, account_id, *, name="", status_text="",
                         avatar_bytes=None, post_json=None):
        return {"ok": True, "applied": {"name": True, "status": True},
                "errors": {}, "pushname": "新名", "avatar_url": "https://x/p.jpg"}

    enriched = []

    async def _fake_enrich(platform, account_id, **kw):
        enriched.append((platform, account_id, kw.get("name"),
                         kw.get("avatar_url")))
        return {"self_name": kw.get("name") or ""}

    monkeypatch.setattr(pp, "push_whatsapp", _fake_push)
    monkeypatch.setattr(sp, "enrich_from_fields", _fake_enrich)
    c = _mk_client(_CFG_ON, reg, monkeypatch)
    r = c.post("/api/accounts/whatsapp/wa1/profile",
               json={"name": "新名", "status_text": "在忙"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["applied"] == {"name": True, "status": True}
    assert d["errors"] == {}
    assert d["cooldown"]["active"] is True
    # 回读刷新被调用（node 回传的 pushname/avatar_url）
    assert enriched == [("whatsapp", "wa1", "新名", "https://x/p.jpg")]
    # meta 审计 read-merge-write：敏感键保住 + last_ts/log 落上
    assert len(reg.upserts) == 1
    meta = reg.upserts[0][2]["meta"]
    assert meta["session_string"] == "KEEP"
    assert meta["baileys_login_id"] == "L1"
    assert meta["profile_push_last_ts"] > 0
    assert meta["profile_push_log"][-1]["ok"] is True
    assert sorted(meta["profile_push_log"][-1]["fields"]) == ["name", "status"]


def test_route_post_whatsapp_not_connected_409(monkeypatch):
    reg = _FakeRegistry({("whatsapp", "wa1"): {
        "platform": "whatsapp", "account_id": "wa1", "meta": {}}})

    async def _offline(cfg, account_id, **kw):
        raise RuntimeError("err.acct.profile_offline")

    monkeypatch.setattr(pp, "push_whatsapp", _offline)
    c = _mk_client(_CFG_ON, reg, monkeypatch)
    r = c.post("/api/accounts/whatsapp/wa1/profile", json={"name": "n"})
    assert r.status_code == 409
    assert reg.upserts == []   # 没真推送 → 不烧冷却/不写审计


def test_route_post_whatsapp_service_down_502(monkeypatch):
    reg = _FakeRegistry()

    async def _down(cfg, account_id, **kw):
        raise RuntimeError("err.acct.profile_push_failed")

    monkeypatch.setattr(pp, "push_whatsapp", _down)
    c = _mk_client(_CFG_ON, reg, monkeypatch)
    r = c.post("/api/accounts/whatsapp/wa1/profile", json={"name": "n"})
    assert r.status_code == 502


def test_route_post_telegram_no_client_409(monkeypatch):
    from src.web.routes import unified_inbox_account_routes as acct_routes
    monkeypatch.setattr(acct_routes, "_get_tg_pyro_for_account",
                        lambda app, aid: None)
    c = _mk_client(_CFG_ON, _FakeRegistry(), monkeypatch)
    r = c.post("/api/accounts/telegram/123/profile", json={"name": "n"})
    assert r.status_code == 409


def test_route_post_telegram_success_with_avatar(monkeypatch):
    import src.integrations.account_self_profile as sp
    from src.web.routes import unified_inbox_account_routes as acct_routes
    reg = _FakeRegistry({("telegram", "123"): {
        "platform": "telegram", "account_id": "123",
        "meta": {"session_string": "KEEP"}}})
    cl = _FakeTg()
    monkeypatch.setattr(acct_routes, "_get_tg_pyro_for_account",
                        lambda app, aid: cl)
    enriched = []

    async def _fake_enrich_user(platform, account_id, user, **kw):
        enriched.append((platform, account_id,
                         getattr(user, "first_name", "")))
        return {"self_name": "新名"}

    monkeypatch.setattr(sp, "enrich_from_user", _fake_enrich_user)
    c = _mk_client(_CFG_ON, reg, monkeypatch)
    avatar = base64.b64encode(_png_bytes(80, 80)).decode("ascii")
    r = c.post("/api/accounts/telegram/123/profile",
               json={"name": "新名", "status_text": "新签名",
                     "avatar_b64": avatar})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["applied"] == {"name": True, "status": True, "avatar": True}
    # 头像经 tempfile 传给 set_profile_photo，且用后删除
    photo_calls = [x for x in cl.calls if x[0] == "set_profile_photo"]
    assert len(photo_calls) == 1
    import os
    assert not os.path.exists(photo_calls[0][1])
    # 回读刷新走 get_me → enrich_from_user
    assert enriched == [("telegram", "123", "新名")]
    meta = reg.upserts[-1][2]["meta"]
    assert meta["session_string"] == "KEEP"
    assert meta["profile_push_last_ts"] > 0


def test_route_post_telegram_all_fields_failed_502(monkeypatch):
    from src.web.routes import unified_inbox_account_routes as acct_routes
    reg = _FakeRegistry({("telegram", "123"): {
        "platform": "telegram", "account_id": "123", "meta": {}}})
    cl = _FakeTg(fail_name=True)
    monkeypatch.setattr(acct_routes, "_get_tg_pyro_for_account",
                        lambda app, aid: cl)
    c = _mk_client(_CFG_ON, reg, monkeypatch)
    r = c.post("/api/accounts/telegram/123/profile", json={"name": "新名"})
    assert r.status_code == 502
    # 失败也落审计，但不推 last_ts（不烧冷却窗）
    meta = reg.upserts[-1][2]["meta"]
    assert meta["profile_push_log"][-1]["ok"] is False
    assert "profile_push_last_ts" not in meta


def test_route_get_shape_with_registry_row(monkeypatch):
    now = time.time()
    reg = _FakeRegistry({("whatsapp", "wa1"): {
        "platform": "whatsapp", "account_id": "wa1", "label": "小雨",
        "proxy_id": "p1", "fingerprint_id": "",
        "meta": {"self_name": "小雨", "self_avatar": "/x.jpg",
                 "persona_ids": ["lin_jiaxin", "other"],
                 "profile_push_last_ts": now - 60,
                 "profile_push_log": [
                     {"ts": float(i), "fields": ["name"], "ok": True}
                     for i in range(8)]}}})
    c = _mk_client(_CFG_ON, reg, monkeypatch)
    r = c.get("/api/accounts/whatsapp/wa1/profile")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["enabled"] is True
    assert d["capabilities"]["mode"] == "direct"
    assert d["self"] == {"self_name": "小雨", "self_avatar": "/x.jpg"}
    assert d["label"] == "小雨"
    assert d["persona_id"] == "lin_jiaxin"   # persona_ids[0] 回落
    assert d["proxy"] is True and d["fingerprint"] is False
    assert d["cooldown"]["active"] is True
    assert d["cooldown"]["hours"] == 24
    assert d["cooldown"]["remaining_sec"] > 0
    assert d["last_push_ts"] == pytest.approx(now - 60, abs=2)
    assert len(d["push_log"]) == 5           # 只透出最近 5 条


def test_route_get_missing_account_still_ok(monkeypatch):
    c = _mk_client({}, _FakeRegistry(), monkeypatch)
    r = c.get("/api/accounts/telegram/default/profile")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["enabled"] is False
    assert d["self"] == {} and d["label"] == "" and d["persona_id"] == ""
    assert d["cooldown"]["active"] is False
    assert d["push_log"] == [] and d["last_push_ts"] == 0
    # manual 平台能力如实声明
    r2 = c.get("/api/accounts/line/a1/profile")
    assert r2.json()["capabilities"]["mode"] == "manual"


# ── _merge_orchestrator_status：state/state_detail 透传 ──────────────────────

def test_merge_orchestrator_status_passes_state(monkeypatch):
    from src.web.routes import unified_inbox_read_routes as read_routes
    fake_orch = SimpleNamespace(status=lambda: {"accounts": [
        {"platform": "whatsapp", "account_id": "wa1", "state": "error",
         "mode": "protocol", "last_error": "",
         "worker": {"detail": "d" * 300}},
        {"platform": "telegram", "account_id": "888", "state": "running",
         "mode": "protocol", "last_error": "old boom", "worker": {}},
    ]})
    monkeypatch.setattr(
        "src.integrations.account_orchestrator.get_orchestrator",
        lambda cfg=None: fake_orch)
    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry",
        lambda: SimpleNamespace(list=lambda platform=None: []))
    status = {"telegram:888": {"platform": "telegram", "account_id": "888",
                               "running": False, "label": "工作号"}}
    read_routes._merge_orchestrator_status(
        status, SimpleNamespace(config={}))
    # 新建条目：state + detail 截断 120
    v = status["whatsapp:wa1"]
    assert v["state"] == "error"
    assert v["state_detail"] == "d" * 120
    # 既有条目：补 state，不删既有字段；worker.detail 空 → 回落 last_error
    v2 = status["telegram:888"]
    assert v2["state"] == "running"
    assert v2["state_detail"] == "old boom"
    assert v2["label"] == "工作号" and v2["running"] is True


def test_merge_orchestrator_status_adapter_entries_untouched(monkeypatch):
    from src.web.routes import unified_inbox_read_routes as read_routes
    monkeypatch.setattr(
        "src.integrations.account_orchestrator.get_orchestrator",
        lambda cfg=None: SimpleNamespace(status=lambda: {"accounts": []}))
    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry",
        lambda: SimpleNamespace(list=lambda platform=None: []))
    status = {"telegram": {"platform": "telegram", "account_id": "default",
                           "running": True}}
    read_routes._merge_orchestrator_status(status, SimpleNamespace(config={}))
    assert "state" not in status["telegram"]        # 适配器条目不动
    assert "state_detail" not in status["telegram"]


# ── resolve_persona_fill（人设一键填充素材） ─────────────────────────────────

class _FakePersonaManager:
    """假 PersonaManager：只认 p1（name 带空白验证 strip）。"""

    _profiles = {"p1": {"id": "p1", "name": " 林佳欣 "}}

    @classmethod
    def get_instance(cls):
        return cls()

    def get_persona_by_id(self, pid):
        return self._profiles.get(pid)


def test_resolve_persona_fill_with_and_without_face_ref(tmp_path, monkeypatch):
    import src.utils.persona_manager as pm_mod
    monkeypatch.setattr(pm_mod, "PersonaManager", _FakePersonaManager)
    d = tmp_path / "p1"
    d.mkdir()
    (d / "face_ref.jpg").write_bytes(b"IMG")
    cfg = {"companion": {"selfie": {"provider": {"album_dir": str(tmp_path)}}}}
    fill = pp.resolve_persona_fill("p1", cfg)
    assert fill["id"] == "p1"
    assert fill["name"] == "林佳欣"                      # strip 后
    assert fill["face_ref_path"].endswith("face_ref.jpg")
    # 目录存在但无 face_ref 图片（.txt 不算）→ face_ref_path 空串
    d2 = tmp_path / "empty" / "p1"
    d2.mkdir(parents=True)
    (d2 / "face_ref.txt").write_text("not-an-image")
    cfg2 = {"companion": {"selfie": {"provider": {
        "album_dir": str(tmp_path / "empty")}}}}
    fill2 = pp.resolve_persona_fill("p1", cfg2)
    assert fill2["name"] == "林佳欣" and fill2["face_ref_path"] == ""
    # pid 空 / 查无 → {}
    assert pp.resolve_persona_fill("", cfg) == {}
    assert pp.resolve_persona_fill("ghost", cfg) == {}


def test_resolve_persona_fill_pm_broken_returns_empty(monkeypatch):
    import src.utils.persona_manager as pm_mod

    class _Boom:
        @classmethod
        def get_instance(cls):
            raise RuntimeError("PM down")

    monkeypatch.setattr(pm_mod, "PersonaManager", _Boom)
    assert pp.resolve_persona_fill("p1", {}) == {}


def test_fresh_account_days_config():
    assert pp.fresh_account_days({}) == 7
    assert pp.fresh_account_days({"accounts": {"profile_push": {
        "fresh_account_days": 14}}}) == 14
    assert pp.fresh_account_days({"accounts": {"profile_push": {
        "fresh_account_days": "junk"}}}) == 7


# ── 推送漏斗计数 + Prometheus 行格式 ─────────────────────────────────────────

def test_push_stats_counting_and_prom_format():
    pp.reset_profile_push_stats()
    try:
        pp.bump_push_stat("attempts", "whatsapp")
        pp.bump_push_stat("attempts", "telegram")
        pp.bump_push_stat("cooldown_blocked")
        pp.bump_push_stat("offline_blocked")
        pp.record_push_result("whatsapp", True, True)    # success + partial
        pp.record_push_result("telegram", False)         # failed
        pp.bump_push_stat("persona_fill")
        pp.bump_push_stat("bogus_key")                   # 未知 key 忽略
        pp.bump_push_stat("persona_fill", "whatsapp")    # 非 attempts/success 不进 by_platform
        st = pp.get_profile_push_stats()
        assert st["attempts"] == 2 and st["success"] == 1
        assert st["partial"] == 1 and st["failed"] == 1
        assert st["cooldown_blocked"] == 1 and st["offline_blocked"] == 1
        assert st["persona_fill"] == 2
        assert "bogus_key" not in st
        assert st["active"] is True and st["last_ts"] > 0
        assert st["by_platform"]["whatsapp"] == {"attempts": 1, "success": 1}
        assert st["by_platform"]["telegram"] == {"attempts": 1, "success": 0}
        # 深拷贝：改快照不污染内部状态
        st["by_platform"]["whatsapp"]["attempts"] = 999
        assert pp.get_profile_push_stats()[
            "by_platform"]["whatsapp"]["attempts"] == 1
        text = pp.dump_profile_push_prom()
        assert "# TYPE profile_push_total counter" in text
        assert 'profile_push_total{op="attempts"} 2' in text
        assert 'profile_push_total{op="success"} 1' in text
        assert 'profile_push_total{op="cooldown_blocked"} 1' in text
        assert 'profile_push_total{op="persona_fill"} 2' in text
        assert "# TYPE profile_push_platform_total counter" in text
        assert ('profile_push_platform_total{platform="whatsapp",op="attempts"} 1'
                in text)
        assert ('profile_push_platform_total{platform="telegram",op="success"} 0'
                in text)
    finally:
        pp.reset_profile_push_stats()
    st2 = pp.get_profile_push_stats()
    assert st2["attempts"] == 0 and st2["active"] is False
    assert st2["by_platform"] == {} and st2["last_ts"] == 0.0


# ── GET：persona / account_age_days / fresh 字段 ─────────────────────────────

def test_route_get_fresh_and_persona_fields(monkeypatch, tmp_path):
    now = time.time()
    face = tmp_path / "face_ref.jpg"
    face.write_bytes(b"IMG")
    reg = _FakeRegistry({
        ("whatsapp", "new1"): {"platform": "whatsapp", "account_id": "new1",
                               "created_at": now - 2 * 86400,
                               "meta": {"persona_id": "p1"}},
        ("whatsapp", "old1"): {"platform": "whatsapp", "account_id": "old1",
                               "created_at": now - 30 * 86400,
                               "meta": {"persona_id": "noface"}},
    })

    def _fake_fill(pid, cfg):
        if pid == "p1":
            return {"id": "p1", "name": "林佳欣", "face_ref_path": str(face)}
        if pid == "noface":
            return {"id": "noface", "name": "无脸", "face_ref_path": ""}
        return {}

    monkeypatch.setattr(pp, "resolve_persona_fill", _fake_fill)
    c = _mk_client(_CFG_ON, reg, monkeypatch)
    # 新号（age 2d < 7d）+ 绑定人设有 face_ref
    d = c.get("/api/accounts/whatsapp/new1/profile").json()
    assert d["fresh"] is True and d["fresh_days"] == 7
    assert d["account_age_days"] == pytest.approx(2.0, abs=0.1)
    assert d["persona"]["id"] == "p1"
    assert d["persona"]["name"] == "林佳欣"
    assert d["persona"]["face_ref"] is True
    assert d["persona"]["face_ref_url"].startswith(
        "/api/personas/p1/face-ref/image?v=")
    # 老号 + 人设无 face_ref → face_ref_url 不给
    d2 = c.get("/api/accounts/whatsapp/old1/profile").json()
    assert d2["fresh"] is False
    assert d2["account_age_days"] == pytest.approx(30.0, abs=0.1)
    assert d2["persona"]["face_ref"] is False
    assert "face_ref_url" not in d2["persona"]
    # 无 created_at（registry 无此行）→ age null + fresh false + persona null
    d3 = c.get("/api/accounts/whatsapp/nobody/profile").json()
    assert d3["account_age_days"] is None and d3["fresh"] is False
    assert d3["persona"] is None


# ── POST use_persona 全链 ────────────────────────────────────────────────────

def _wa_cfg_on():
    return {**_CFG_ON,
            "platform_login": {"whatsapp": {"baileys_url": "http://svc:1"}}}


def test_route_post_use_persona_full_chain(monkeypatch, tmp_path):
    import src.integrations.account_self_profile as sp
    import src.integrations.whatsapp_baileys_login as wbl
    pp.reset_profile_push_stats()
    try:
        face = tmp_path / "face_ref.jpg"
        face.write_bytes(_png_bytes(80, 80))
        reg = _FakeRegistry({("whatsapp", "wa1"): {
            "platform": "whatsapp", "account_id": "wa1",
            "meta": {"persona_id": "lin_jiaxin", "session_string": "KEEP"}}})
        monkeypatch.setattr(
            pp, "resolve_persona_fill",
            lambda pid, cfg: {"id": pid, "name": "林佳欣",
                              "face_ref_path": str(face)})
        calls = {}

        async def _fake_post(url, payload):
            calls["url"] = url
            calls["payload"] = payload
            return {"ok": True, "applied": {"name": True, "avatar": True},
                    "errors": {}, "pushname": "林佳欣", "avatar_url": ""}

        monkeypatch.setattr(wbl, "_post_json", _fake_post)

        async def _fake_enrich(platform, account_id, **kw):
            return {}

        monkeypatch.setattr(sp, "enrich_from_fields", _fake_enrich)
        c = _mk_client(_wa_cfg_on(), reg, monkeypatch)
        r = c.post("/api/accounts/whatsapp/wa1/profile",
                   json={"use_persona": True})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["persona_used"] == {"name": True, "avatar": True}
        # 人设素材真被送出（经真 push_whatsapp）：name=人设名；
        # avatar_b64=face_ref 文件经 prepare_avatar 规格化后的 JPEG
        assert calls["payload"]["name"] == "林佳欣"
        jpg = base64.b64decode(calls["payload"]["avatar_b64"])
        assert jpg[:3] == b"\xff\xd8\xff"
        # meta 审计 read-merge-write：敏感键仍保住
        assert reg.upserts[-1][2]["meta"]["session_string"] == "KEEP"
        st = pp.get_profile_push_stats()
        assert st["attempts"] == 1 and st["success"] == 1
        assert st["persona_fill"] == 1 and st["failed"] == 0
        assert st["by_platform"]["whatsapp"] == {"attempts": 1, "success": 1}
    finally:
        pp.reset_profile_push_stats()


def test_route_post_use_persona_explicit_fields_win(monkeypatch, tmp_path):
    import src.integrations.account_self_profile as sp
    import src.integrations.whatsapp_baileys_login as wbl
    pp.reset_profile_push_stats()
    try:
        face = tmp_path / "face_ref.jpg"
        face.write_bytes(_png_bytes(60, 60))
        reg = _FakeRegistry({("whatsapp", "wa1"): {
            "platform": "whatsapp", "account_id": "wa1",
            "meta": {"persona_ids": ["lin_jiaxin"]}}})   # persona_ids[0] 回落
        monkeypatch.setattr(
            pp, "resolve_persona_fill",
            lambda pid, cfg: {"id": pid, "name": "林佳欣",
                              "face_ref_path": str(face)})
        calls = {}

        async def _fake_post(url, payload):
            calls["payload"] = payload
            return {"ok": True, "applied": {"name": True, "avatar": True},
                    "errors": {}}

        monkeypatch.setattr(wbl, "_post_json", _fake_post)

        async def _fake_enrich(platform, account_id, **kw):
            return {}

        monkeypatch.setattr(sp, "enrich_from_fields", _fake_enrich)
        c = _mk_client(_wa_cfg_on(), reg, monkeypatch)
        r = c.post("/api/accounts/whatsapp/wa1/profile",
                   json={"use_persona": True, "name": "自定义名"})
        assert r.status_code == 200, r.text
        assert calls["payload"]["name"] == "自定义名"    # 显式 body 优先
        assert r.json()["persona_used"] == {"name": False, "avatar": True}
    finally:
        pp.reset_profile_push_stats()


def test_route_post_use_persona_no_persona_400(monkeypatch):
    pp.reset_profile_push_stats()
    try:
        # 未绑定人设 → 400（即便 body 带了 status_text 也如实拒绝）
        reg = _FakeRegistry({("whatsapp", "wa1"): {
            "platform": "whatsapp", "account_id": "wa1", "meta": {}}})
        c = _mk_client(_CFG_ON, reg, monkeypatch)
        r = c.post("/api/accounts/whatsapp/wa1/profile",
                   json={"use_persona": True, "status_text": "hi"})
        assert r.status_code == 400
        assert r.json()["detail"]
        # 绑定了但素材全空（人设已删/既无名也无脸）→ 同样 400
        reg2 = _FakeRegistry({("whatsapp", "wa2"): {
            "platform": "whatsapp", "account_id": "wa2",
            "meta": {"persona_ids": ["ghost"]}}})
        monkeypatch.setattr(pp, "resolve_persona_fill", lambda pid, cfg: {})
        c2 = _mk_client(_CFG_ON, reg2, monkeypatch)
        r2 = c2.post("/api/accounts/whatsapp/wa2/profile",
                     json={"use_persona": True})
        assert r2.status_code == 400
        # 守卫在 attempts 埋点之前 → 漏斗不计
        assert pp.get_profile_push_stats()["attempts"] == 0
    finally:
        pp.reset_profile_push_stats()


def test_route_post_funnel_counters_wired(monkeypatch):
    pp.reset_profile_push_stats()
    try:
        # 429 冷却 → attempts + cooldown_blocked
        reg = _FakeRegistry({("whatsapp", "wa1"): {
            "platform": "whatsapp", "account_id": "wa1",
            "meta": {"profile_push_last_ts": time.time() - 60}}})
        c = _mk_client(_CFG_ON, reg, monkeypatch)
        assert c.post("/api/accounts/whatsapp/wa1/profile",
                      json={"name": "n"}).status_code == 429

        # 409 offline → offline_blocked
        async def _off(cfg, account_id, **kw):
            raise RuntimeError("err.acct.profile_offline")

        monkeypatch.setattr(pp, "push_whatsapp", _off)
        c2 = _mk_client(_CFG_ON, _FakeRegistry(), monkeypatch)
        assert c2.post("/api/accounts/whatsapp/wa2/profile",
                       json={"name": "n"}).status_code == 409

        # 502 服务失败 → failed
        async def _down(cfg, account_id, **kw):
            raise RuntimeError("err.acct.profile_push_failed")

        monkeypatch.setattr(pp, "push_whatsapp", _down)
        c3 = _mk_client(_CFG_ON, _FakeRegistry(), monkeypatch)
        assert c3.post("/api/accounts/whatsapp/wa3/profile",
                       json={"name": "n"}).status_code == 502

        st = pp.get_profile_push_stats()
        assert st["attempts"] == 3
        assert st["cooldown_blocked"] == 1
        assert st["offline_blocked"] == 1
        assert st["failed"] == 1
        assert st["success"] == 0 and st["persona_fill"] == 0
        assert st["by_platform"]["whatsapp"]["attempts"] == 3
        assert st["by_platform"]["whatsapp"]["success"] == 0
    finally:
        pp.reset_profile_push_stats()


# ── _merge_orchestrator_status：unhealthy_since 透传 ─────────────────────────

def test_merge_orchestrator_status_injects_unhealthy_since(monkeypatch):
    from src.integrations.platform_session_health import (
        get_platform_session_health,
    )
    from src.web.routes import unified_inbox_read_routes as read_routes
    h = get_platform_session_health()
    h.reset()
    try:
        before = time.time()
        h.record("whatsapp", "wa1", "expired", detail="logged out")
        h.record("telegram", "888", "authorized")
        fake_orch = SimpleNamespace(status=lambda: {"accounts": [
            {"platform": "whatsapp", "account_id": "wa1", "state": "error",
             "mode": "protocol", "last_error": "", "worker": {}},
            {"platform": "telegram", "account_id": "888", "state": "running",
             "mode": "protocol", "last_error": "", "worker": {}},
        ]})
        monkeypatch.setattr(
            "src.integrations.account_orchestrator.get_orchestrator",
            lambda cfg=None: fake_orch)
        monkeypatch.setattr(
            "src.integrations.account_registry.get_account_registry",
            lambda: SimpleNamespace(list=lambda platform=None: []))
        status: dict = {}
        read_routes._merge_orchestrator_status(
            status, SimpleNamespace(config={}))
        v = status["whatsapp:wa1"]
        assert before <= v["unhealthy_since"] <= time.time()
        # 健康会话不注入
        assert "unhealthy_since" not in status["telegram:888"]
        # 恢复后（unhealthy_since 清零）不再注入
        h.record("whatsapp", "wa1", "authorized")
        status2: dict = {}
        read_routes._merge_orchestrator_status(
            status2, SimpleNamespace(config={}))
        assert "unhealthy_since" not in status2["whatsapp:wa1"]
    finally:
        h.reset()


# ── 批量对齐 / churn 纯函数 + 路由 ───────────────────────────────────────────

def test_meta_persona_id_and_bound_filter():
    assert pp.meta_persona_id({"persona_id": "a"}) == "a"
    assert pp.meta_persona_id({"persona_ids": ["b", "c"]}) == "b"
    assert pp.meta_persona_id({}) == ""
    rows = [
        {"platform": "whatsapp", "account_id": "1",
         "meta": {"persona_id": "p1"}},
        {"platform": "line", "account_id": "2",
         "meta": {"persona_ids": ["p1"]}},
        {"platform": "telegram", "account_id": "3",
         "meta": {"persona_id": "other"}},
    ]
    bound = pp.accounts_bound_to_persona(rows, "p1")
    assert [r["account_id"] for r in bound] == ["1", "2"]


def test_profile_churn_count_and_predict_align():
    now = time.time()
    meta = {"profile_push_log": [
        {"ts": now - 86400, "ok": True},
        {"ts": now - 2 * 86400, "ok": True},
        {"ts": now - 10 * 86400, "ok": True},   # 超窗
        {"ts": now - 100, "ok": False},         # 失败不计
    ]}
    assert pp.profile_churn_count(meta, now, days=7) == 2
    assert pp.predict_align_status(
        "line", {}, now=now, cooldown_hours=24, fresh_days=7,
        created_at=now - 30 * 86400, online=True) == "skipped_manual"
    assert pp.predict_align_status(
        "whatsapp", {"profile_push_last_ts": now - 60},
        now=now, cooldown_hours=24, fresh_days=7,
        created_at=now - 30 * 86400, online=True) == "skipped_cooldown"
    assert pp.predict_align_status(
        "whatsapp", {}, now=now, cooldown_hours=24, fresh_days=7,
        created_at=now - 2 * 86400, online=True) == "skipped_fresh"
    assert pp.predict_align_status(
        "whatsapp", {}, now=now, cooldown_hours=24, fresh_days=7,
        created_at=now - 30 * 86400, online=False) == "skipped_offline"
    assert pp.predict_align_status(
        "whatsapp", {}, now=now, cooldown_hours=24, fresh_days=7,
        created_at=now - 30 * 86400, online=True) == "would_push"
    # 已对齐：无头像可推且昵称已等于人设名
    assert pp.predict_align_status(
        "whatsapp", {}, now=now, cooldown_hours=24, fresh_days=7,
        created_at=now - 30 * 86400, online=True,
        target_name="林佳欣", has_avatar=False,
        current_name="林佳欣") == "skipped_aligned"
    # 有头像时即使同名也要再推（锁脸照可能更新）
    assert pp.predict_align_status(
        "whatsapp", {}, now=now, cooldown_hours=24, fresh_days=7,
        created_at=now - 30 * 86400, online=True,
        target_name="林佳欣", has_avatar=True,
        current_name="林佳欣") == "would_push"


def test_same_platform_align_risk():
    assert pp.same_platform_align_risk([]) == []
    risk = pp.same_platform_align_risk([
        {"platform": "whatsapp", "status": "would_push"},
        {"platform": "whatsapp", "status": "would_push"},
        {"platform": "telegram", "status": "would_push"},
        {"platform": "line", "status": "skipped_manual"},
    ])
    assert risk == [{"platform": "whatsapp", "count": 2}]


def test_parse_align_include_and_key():
    assert pp.parse_align_include(None) is None
    assert pp.parse_align_include("x") is None
    assert pp.parse_align_include([]) == set()
    assert pp.parse_align_include([
        "whatsapp:wa1", "Telegram:TG2", {"platform": "line", "account_id": "L1"},
        "bad", {"platform": "", "account_id": "x"}, 7,
    ]) == {("whatsapp", "wa1"), ("telegram", "TG2"), ("line", "L1")}
    assert pp.account_align_key("WhatsApp", " wa1 ") == "whatsapp:wa1"


def test_parse_align_fields():
    assert pp.parse_align_fields(None) is None
    assert pp.parse_align_fields([]) is None
    assert pp.parse_align_fields("name") is None       # 非列表 → 不限制
    assert pp.parse_align_fields(["name"]) == {"name"}
    assert pp.parse_align_fields(["Avatar", "x"]) == {"avatar"}
    assert pp.parse_align_fields(["name", "avatar"]) == {"name", "avatar"}
    assert pp.parse_align_fields(["bogus"]) is None    # 交集为空 → 不限制


def test_route_get_exposes_churn_7d(monkeypatch):
    now = time.time()
    reg = _FakeRegistry({("whatsapp", "wa1"): {
        "platform": "whatsapp", "account_id": "wa1",
        "meta": {"profile_push_log": [
            {"ts": now - 100, "ok": True},
            {"ts": now - 200, "ok": True},
            {"ts": now - 300, "ok": True},
        ]}}})
    c = _mk_client(_CFG_ON, reg, monkeypatch)
    d = c.get("/api/accounts/whatsapp/wa1/profile").json()
    assert d["churn_7d"] == 3


def test_route_persona_align_dry_run_and_execute(monkeypatch):
    import src.integrations.account_self_profile as sp
    from src.web.routes import unified_inbox_account_routes as acct_routes
    pp.reset_profile_push_stats()
    try:
        now = time.time()
        reg = _FakeRegistry({
            ("whatsapp", "wa_old"): {
                "platform": "whatsapp", "account_id": "wa_old",
                "label": "老号", "created_at": now - 30 * 86400,
                "meta": {"persona_id": "p1", "self_name": "旧名"}},
            ("whatsapp", "wa_fresh"): {
                "platform": "whatsapp", "account_id": "wa_fresh",
                "created_at": now - 2 * 86400,
                "meta": {"persona_id": "p1"}},
            ("line", "L1"): {
                "platform": "line", "account_id": "L1",
                "created_at": now - 30 * 86400,
                "meta": {"persona_id": "p1"}},
            ("telegram", "tg1"): {
                "platform": "telegram", "account_id": "tg1",
                "created_at": now - 30 * 86400,
                "meta": {"persona_id": "other"}},
        })
        monkeypatch.setattr(
            pp, "resolve_persona_fill",
            lambda pid, cfg: {"id": pid, "name": "林佳欣",
                              "face_ref_path": ""})

        async def _fake_push(cfg, account_id, **kw):
            return {"ok": True, "applied": {"name": True},
                    "errors": {}, "pushname": kw.get("name") or "",
                    "avatar_url": ""}

        async def _noop_sleep(_):
            return None

        async def _fake_enrich(platform, account_id, **kw):
            return {"self_name": kw.get("name") or ""}

        monkeypatch.setattr(pp, "push_whatsapp", _fake_push)
        monkeypatch.setattr(sp, "enrich_from_fields", _fake_enrich)
        monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
        monkeypatch.setattr(acct_routes, "_get_tg_pyro_for_account",
                            lambda app, aid: None)

        c = _mk_client(_CFG_ON, reg, monkeypatch)
        dry = c.post("/api/accounts/persona-align",
                     json={"persona_id": "p1", "dry_run": True})
        assert dry.status_code == 200, dry.text
        dd = dry.json()
        assert dd["dry_run"] is True
        by = {r["account_id"]: r["status"] for r in dd["results"]}
        assert by["wa_old"] == "would_push"
        assert by["wa_fresh"] == "skipped_fresh"
        assert by["L1"] == "skipped_manual"
        assert "tg1" not in by   # 别人设
        assert dd["summary"]["would_push"] == 1
        assert dd.get("same_platform_risk") == []   # 仅 1 个 would_push

        exe = c.post("/api/accounts/persona-align",
                     json={"persona_id": "p1", "dry_run": False})
        assert exe.status_code == 200, exe.text
        ed = exe.json()
        by2 = {r["account_id"]: r["status"] for r in ed["results"]}
        assert by2["wa_old"] == "pushed"
        assert by2["wa_fresh"] == "skipped_fresh"
        assert ed["summary"]["pushed"] == 1
        assert pp.get_profile_push_stats()["align_runs"] == 1
        assert pp.get_profile_push_stats()["persona_fill"] >= 1

        # include 白名单：未勾选的 would_push → skipped_excluded（不真推）
        pp.reset_profile_push_stats()
        # 重置冷却/已对齐态，否则刚 pushed 会进 cooldown 或 skipped_aligned
        meta = reg.rows[("whatsapp", "wa_old")].setdefault("meta", {})
        meta.pop("profile_push_last_ts", None)
        meta["self_name"] = "旧名"
        skip = c.post("/api/accounts/persona-align",
                      json={"persona_id": "p1", "dry_run": False,
                            "include": []})
        assert skip.status_code == 200, skip.text
        sd = skip.json()
        by3 = {r["account_id"]: r["status"] for r in sd["results"]}
        assert by3["wa_old"] == "skipped_excluded"
        assert sd["summary"]["pushed"] == 0
        assert pp.get_profile_push_stats()["align_runs"] == 1
    finally:
        pp.reset_profile_push_stats()


def test_route_persona_align_field_level(monkeypatch, tmp_path):
    """fields=["name"] → 只推昵称，不带头像字节（字段级对齐）。"""
    import src.integrations.account_self_profile as sp
    from src.web.routes import unified_inbox_account_routes as acct_routes
    pp.reset_profile_push_stats()
    try:
        now = time.time()
        reg = _FakeRegistry({
            ("whatsapp", "wa_old"): {
                "platform": "whatsapp", "account_id": "wa_old",
                "created_at": now - 30 * 86400,
                "meta": {"persona_id": "p1", "self_name": "旧名"}},
        })
        face = tmp_path / "face.jpg"
        face.write_bytes(b"RAWFACE")
        monkeypatch.setattr(
            pp, "resolve_persona_fill",
            lambda pid, cfg: {"id": pid, "name": "林佳欣",
                              "face_ref_path": str(face)})
        monkeypatch.setattr(pp, "prepare_avatar",
                            lambda raw, mb: b"AVATARJPEG")

        seen = []

        async def _fake_push(cfg, account_id, **kw):
            seen.append(dict(kw))
            applied = {}
            if kw.get("name"):
                applied["name"] = True
            if kw.get("avatar_bytes"):
                applied["avatar"] = True
            return {"ok": bool(applied), "applied": applied, "errors": {},
                    "pushname": kw.get("name") or "", "avatar_url": ""}

        async def _noop_sleep(_):
            return None

        async def _fake_enrich(platform, account_id, **kw):
            return {"self_name": kw.get("name") or ""}

        monkeypatch.setattr(pp, "push_whatsapp", _fake_push)
        monkeypatch.setattr(sp, "enrich_from_fields", _fake_enrich)
        monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

        c = _mk_client(_CFG_ON, reg, monkeypatch)
        exe = c.post("/api/accounts/persona-align",
                     json={"persona_id": "p1", "dry_run": False,
                           "fields": ["name"]})
        assert exe.status_code == 200, exe.text
        ed = exe.json()
        by = {r["account_id"]: r["status"] for r in ed["results"]}
        assert by["wa_old"] == "pushed"
        assert len(seen) == 1
        assert seen[0].get("name") == "林佳欣"
        assert not seen[0].get("avatar_bytes")   # 只推昵称，头像不带
    finally:
        pp.reset_profile_push_stats()


def test_route_persona_align_writes_ops_event(monkeypatch, tmp_path):
    """批量对齐真推 → 落一条 profile_align ops 事件（可按号回溯）。"""
    import src.integrations.account_self_profile as sp
    from src.ops import ops_events as ope
    pp.reset_profile_push_stats()
    ope.reset_ops_event_store()
    # 单例落到临时库，避免污染实例 config/ops_events.db
    store = ope.OpsEventStore(str(tmp_path / "ops_events.db"))
    monkeypatch.setattr(ope, "get_ops_event_store", lambda *a, **k: store)
    try:
        now = time.time()
        reg = _FakeRegistry({
            ("whatsapp", "wa_old"): {
                "platform": "whatsapp", "account_id": "wa_old",
                "created_at": now - 30 * 86400,
                "meta": {"persona_id": "p1", "self_name": "旧名"}},
        })
        monkeypatch.setattr(
            pp, "resolve_persona_fill",
            lambda pid, cfg: {"id": pid, "name": "林佳欣", "face_ref_path": ""})

        async def _fake_push(cfg, account_id, **kw):
            applied = {"name": True} if kw.get("name") else {}
            return {"ok": bool(applied), "applied": applied, "errors": {},
                    "pushname": kw.get("name") or "", "avatar_url": ""}

        async def _noop_sleep(_):
            return None

        async def _fake_enrich(platform, account_id, **kw):
            return {"self_name": kw.get("name") or ""}

        monkeypatch.setattr(pp, "push_whatsapp", _fake_push)
        monkeypatch.setattr(sp, "enrich_from_fields", _fake_enrich)
        monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

        c = _mk_client(_CFG_ON, reg, monkeypatch)
        exe = c.post("/api/accounts/persona-align",
                     json={"persona_id": "p1", "dry_run": False})
        assert exe.status_code == 200, exe.text
        evs = store.recent(account_id="wa_old", limit=10)
        align = [e for e in evs if e["kind"] == "profile_align"]
        assert len(align) == 1
        assert align[0]["reason"] == "ok"
        assert "persona=p1" in align[0]["detail"]
        assert "name" in align[0]["detail"]
    finally:
        pp.reset_profile_push_stats()
        ope.reset_ops_event_store()


def test_ops_events_daily_and_recent_kinds(tmp_path):
    """ops_events 新增聚合方法：recent_kinds（跨账号按 kind）+ daily_kinds（按天）。"""
    from src.ops.ops_events import OpsEventStore
    st = OpsEventStore(str(tmp_path / "ev.db"))
    now = time.time()
    st.record("profile_align", account_id="a1", reason="ok",
              detail="fields=name;persona=p1;run=RUN1", ts=now)
    st.record("profile_align", account_id="a2", reason="failed",
              detail="fields=name;persona=p1;run=RUN1", ts=now)
    st.record("profile_push", account_id="a1", reason="ok",
              detail="fields=avatar", ts=now)
    st.record("paused", account_id="a3", reason="", ts=now)  # 无关 kind

    rec = st.recent_kinds(["profile_push", "profile_align"], limit=10)
    assert len(rec) == 3  # 不含 paused
    kinds = {r["kind"] for r in rec}
    assert kinds == {"profile_push", "profile_align"}

    daily = st.daily_kinds(["profile_align"], days=7)
    assert len(daily) == 1
    assert daily[0]["total"] == 2
    assert daily[0]["ok"] == 1 and daily[0]["failed"] == 1


def test_route_persona_align_run_id_groups(monkeypatch, tmp_path):
    """一次批量对齐的多条 per-account 事件共享同一 run_id（可折叠归组）。"""
    import src.integrations.account_self_profile as sp
    from src.ops import ops_events as ope
    pp.reset_profile_push_stats()
    ope.reset_ops_event_store()
    store = ope.OpsEventStore(str(tmp_path / "ops_events.db"))
    monkeypatch.setattr(ope, "get_ops_event_store", lambda *a, **k: store)
    try:
        now = time.time()
        reg = _FakeRegistry({
            ("whatsapp", "wa1"): {
                "platform": "whatsapp", "account_id": "wa1",
                "created_at": now - 30 * 86400,
                "meta": {"persona_id": "p1", "self_name": "旧1"}},
            ("whatsapp", "wa2"): {
                "platform": "whatsapp", "account_id": "wa2",
                "created_at": now - 30 * 86400,
                "meta": {"persona_id": "p1", "self_name": "旧2"}},
        })
        monkeypatch.setattr(
            pp, "resolve_persona_fill",
            lambda pid, cfg: {"id": pid, "name": "林佳欣", "face_ref_path": ""})

        async def _fake_push(cfg, account_id, **kw):
            applied = {"name": True} if kw.get("name") else {}
            return {"ok": bool(applied), "applied": applied, "errors": {},
                    "pushname": kw.get("name") or "", "avatar_url": ""}

        async def _noop_sleep(_):
            return None

        async def _fake_enrich(platform, account_id, **kw):
            return {"self_name": kw.get("name") or ""}

        monkeypatch.setattr(pp, "push_whatsapp", _fake_push)
        monkeypatch.setattr(sp, "enrich_from_fields", _fake_enrich)
        monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

        c = _mk_client(_CFG_ON, reg, monkeypatch)
        exe = c.post("/api/accounts/persona-align",
                     json={"persona_id": "p1", "dry_run": False})
        assert exe.status_code == 200, exe.text
        evs = store.recent_kinds(["profile_align"], limit=10)
        assert len(evs) == 2
        runs = set()
        for e in evs:
            for seg in str(e["detail"]).split(";"):
                if seg.startswith("run="):
                    runs.add(seg[4:])
        assert len(runs) == 1 and "" not in runs  # 同一批次同一 run_id
    finally:
        pp.reset_profile_push_stats()
        ope.reset_ops_event_store()


@pytest.mark.asyncio
async def test_execute_profile_push_whatsapp_ok(monkeypatch):
    async def _fake(cfg, account_id, **kw):
        return {"ok": True, "applied": {"name": True}, "errors": {},
                "pushname": "n", "avatar_url": ""}

    monkeypatch.setattr(pp, "push_whatsapp", _fake)
    out = await pp.execute_profile_push(
        "whatsapp", "wa1", name="n", config=_CFG_ON)
    assert out["ok"] is True and out["offline"] is False
    assert out["applied"] == {"name": True}


@pytest.mark.asyncio
async def test_execute_profile_push_offline(monkeypatch):
    async def _off(cfg, account_id, **kw):
        raise RuntimeError("err.acct.profile_offline")

    monkeypatch.setattr(pp, "push_whatsapp", _off)
    out = await pp.execute_profile_push(
        "whatsapp", "wa1", name="n", config=_CFG_ON)
    assert out["ok"] is False and out["offline"] is True
