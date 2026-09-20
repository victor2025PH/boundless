"""坐席顶栏额度徽章门禁（P2 可见性）。

为什么坐席也要看得见：额度用尽会让**翻译与 AI 草稿直接停摆**，而坐席是第一个撞上的人。
只让主管在会员中心看得到，等于让一线在毫无预警下发现「AI 忽然不干活了」。

两条硬不变量：
1. **分级阈值在服务端**（`quota_level`）——顶栏、会员中心、首启向导三处共用，
   前端各写一份必然漂移；
2. **不泄露敏感字段**——这是任意登录坐席可读的端点，不能带授权码/客户名/lic_id。
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.licensing import local_trial as lt
from src.licensing import quota_store as qs
from src.web.routes.license_routes import quota_level, register_license_routes


# ── 分级纯函数 ────────────────────────────────────────────────────────────

def test_level_out_when_exceeded():
    assert quota_level({"exceeded": True, "included_chars": 100}) == "out"


def test_level_low_on_remaining_ratio():
    assert quota_level({"included_chars": 1000, "remaining_chars": 200}) == "low"
    assert quota_level({"included_chars": 1000, "remaining_chars": 201}) == "ok"


def test_level_low_on_trial_hours():
    """时间维度也算紧张——体验档可能字符还多但窗口快关。"""
    assert quota_level({"included_chars": 1000, "remaining_chars": 900,
                        "trial_hours_left": 6.0}) == "low"
    assert quota_level({"included_chars": 1000, "remaining_chars": 900,
                        "trial_hours_left": 30.0}) == "ok"


def test_level_ok_when_no_quota():
    """不限量授权 / 没额度概念 → 不该被判成紧张。"""
    assert quota_level({}) == "ok"
    assert quota_level({"included_chars": 0, "remaining_chars": None}) == "ok"


# ── 端点 ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate():
    lt.reset_local_trial()
    qs.reset_license_quota_store()
    yield
    lt.reset_local_trial()
    qs.reset_license_quota_store()


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


def test_quota_endpoint_reports_trial(tmp_path):
    _wire_trial(tmp_path, used=1500)
    d = _client().get("/api/workspace/quota").json()
    assert d["ok"] is True and d["visible"] is True
    assert d["source"] == "local_trial"
    assert d["included"] == 10_000 and d["used"] == 1500 and d["remaining"] == 8500
    assert d["level"] == "ok"
    assert d["hours_left"] == pytest.approx(48.0, abs=0.2)


def test_quota_endpoint_marks_low(tmp_path):
    _wire_trial(tmp_path, chars=1000, used=850)
    assert _client().get("/api/workspace/quota").json()["level"] == "low"


def test_quota_endpoint_marks_out(tmp_path):
    _wire_trial(tmp_path, chars=1000, used=1000)
    d = _client().get("/api/workspace/quota").json()
    assert d["level"] == "out" and d["exceeded"] is True


def test_quota_endpoint_hidden_without_any_quota():
    """社区模式且体验档未启用 → 徽章整个隐藏，别在顶栏挂一个「0」误导人。"""
    lt.configure_local_trial({"licensing": {"trial": {"enabled": False}}})
    d = _client().get("/api/workspace/quota").json()
    assert d["visible"] is False


def test_quota_endpoint_leaks_no_secrets(tmp_path):
    """任意登录坐席可读 —— 字段白名单，别顺手把 lic_id / 客户名带出去。

    quotawall v2（2026-08-21）：新增 ``state``（四表合议裁决，见
    src/licensing/quota_state.py）。裁决段刻意用 ``tok``/``tok_out`` 命名而非
    token，正是为了让下面的防泄漏子串扫描继续全量成立。
    """
    _wire_trial(tmp_path)
    d = _client().get("/api/workspace/quota").json()
    assert set(d) == {"ok", "visible", "source", "included", "used", "remaining",
                      "exceeded", "hours_left", "expired", "level", "state"}
    blob = str(d).lower()
    for leak in ("lic_id", "local:ab12cd34", "customer", "token", "sub"):
        assert leak not in blob


def test_quota_endpoint_survives_backend_failure(monkeypatch):
    """观测端点绝不能把工作台顶栏弄崩。"""
    import src.web.routes.license_routes as lr

    def _boom():
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(lr, "_quota_snapshot", _boom)
    d = _client().get("/api/workspace/quota").json()
    assert d["ok"] is False and d["visible"] is False and d["level"] == "ok"


def test_pill_and_keys_are_wired():
    """顶栏元素 + 轮询 + i18n 键三者必须同时在位，缺一个徽章就是死的。"""
    from pathlib import Path
    tpl = (Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
           / "workspace_base.html").read_text(encoding="utf-8")
    assert 'id="ws-quota"' in tpl
    assert "/api/workspace/quota" in tpl
    assert "refreshQuota" in tpl

    from src.web.web_i18n import get_translations
    for lang in ("zh", "en"):
        t = get_translations(lang)
        for key in ("base.pill.quota", "base.pill.quota_trial", "base.pill.quota_t",
                    "base.pill.quota_out", "base.pill.quota_tip",
                    "base.pill.quota_tip_hours", "base.pill.quota_tip_out",
                    "base.pill.quota_tip_expired"):
            assert str(t.get(key) or "").strip(), f"[{lang}] 缺 {key}"
