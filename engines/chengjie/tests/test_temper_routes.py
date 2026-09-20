# -*- coding: utf-8 -*-
"""被骂回怼治理 API 门禁（temper_routes，2026-08-12）。

覆盖：路由注册 / status 三段结构 / config 白名单写+校验+即时生效 /
dry-run 诊断器判定链（inject / clear_window / inject_if_sticky /
suppressed_off / disabled / none）+ 零副作用（不写观测计数）。
"""
from __future__ import annotations

import pytest

from src.companion.temper import temper_stats_reset, temper_stats_snapshot


def _noop_auth(request: "Request") -> None:  # noqa: F821 - 注解给 FastAPI 看
    """测试桩：必须带 Request 类型注解，否则 FastAPI 把 request 当 query 参数。"""
    return None


def _build_app(config_manager):
    from fastapi import FastAPI, Request

    from src.web.routes.temper_routes import register_temper_routes
    _noop_auth.__annotations__["request"] = Request
    app = FastAPI()
    register_temper_routes(app, api_auth=_noop_auth,
                           config_manager=config_manager)
    return app


def _client(tmp_path, temper_overlay=None):
    from fastapi.testclient import TestClient

    from src.utils.config_manager import ConfigManager
    cfg = tmp_path / "config.yaml"
    cfg.write_text("companion: {}\n", encoding="utf-8")
    m = ConfigManager(str(cfg))
    m.config = {"companion": {}}
    if temper_overlay is not None:
        m.config["companion"]["temper"] = dict(temper_overlay)
    return TestClient(_build_app(m)), m


def test_routes_registered(tmp_path):
    client, _ = _client(tmp_path)
    live = set()
    for r in client.app.routes:
        for meth in (getattr(r, "methods", None) or set()):
            if meth in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), meth))
    assert ("/api/companion/temper/status", "GET") in live
    assert ("/api/companion/temper/config", "POST") in live
    assert ("/api/companion/temper/dry-run", "POST") in live


def test_status_shape(tmp_path):
    client, _ = _client(tmp_path)
    r = client.get("/api/companion/temper/status").json()
    assert r["ok"] is True
    assert r["config"]["enabled"] is True
    assert r["config"]["force_level"] == ""
    assert isinstance(r["personas"], list)
    assert isinstance(r["stats"], dict) and "hints" in r["stats"]


class TestConfigWrite:
    def test_write_force_level_takes_effect_immediately(self, tmp_path):
        client, m = _client(tmp_path)
        r = client.post("/api/companion/temper/config",
                        json={"force_level": "feisty"})
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] == ["force_level"]
        assert body["config"]["force_level"] == "feisty"
        # 内存 config 已合并（skill_manager 下一条消息立即用新值）
        assert (m.config["companion"]["temper"]["force_level"] == "feisty")
        # overlay 文件真的落盘
        overlay = (tmp_path / "config.local.yaml").read_text(encoding="utf-8")
        assert "force_level" in overlay

    def test_clear_force_back_to_per_persona(self, tmp_path):
        client, _ = _client(tmp_path, {"force_level": "feisty"})
        r = client.post("/api/companion/temper/config",
                        json={"force_level": ""})
        assert r.json()["config"]["force_level"] == ""

    def test_bad_level_rejected(self, tmp_path):
        client, _ = _client(tmp_path)
        r = client.post("/api/companion/temper/config",
                        json={"force_level": "rage"})
        assert r.status_code == 400

    def test_no_keys_rejected(self, tmp_path):
        client, _ = _client(tmp_path)
        assert client.post("/api/companion/temper/config",
                           json={}).status_code == 400

    def test_bool_keys_and_default_level(self, tmp_path):
        client, m = _client(tmp_path)
        r = client.post("/api/companion/temper/config", json={
            "enabled": False, "profanity": False,
            "default_level": "sharp"}).json()
        assert set(r["applied"]) == {"enabled", "profanity", "default_level"}
        assert r["config"]["enabled"] is False
        assert r["config"]["profanity"] is False
        assert r["config"]["default_level"] == "sharp"

    def test_p2_keys_write_and_validation(self, tmp_path):
        client, _ = _client(tmp_path)
        r = client.post("/api/companion/temper/config", json={
            "max_rounds": 6, "taunt_response": False}).json()
        assert set(r["applied"]) == {"max_rounds", "taunt_response"}
        assert r["config"]["max_rounds"] == 6
        assert r["config"]["taunt_response"] is False
        # 夹界 + 非法值
        r2 = client.post("/api/companion/temper/config",
                         json={"max_rounds": 999}).json()
        assert r2["config"]["max_rounds"] == 20
        assert client.post("/api/companion/temper/config",
                           json={"max_rounds": "abc"}).status_code == 400


class TestDryRun:
    def test_insult_inject_with_default_level(self, tmp_path):
        temper_stats_reset()
        client, _ = _client(tmp_path)
        r = client.post("/api/companion/temper/dry-run",
                        json={"text": "傻逼"}).json()
        assert r["verdict"] == "inject"
        assert r["checks"]["insult"] is True
        assert r["level"] == "gentle" and r["source"] == "default"
        assert r["hint_preview"]
        # 诊断器零副作用：不写观测计数
        assert temper_stats_snapshot()["insults"] == 0

    def test_force_level_reflected(self, tmp_path):
        client, _ = _client(tmp_path, {"force_level": "feisty"})
        r = client.post("/api/companion/temper/dry-run",
                        json={"text": "操你妈"}).json()
        assert r["level"] == "feisty" and r["source"] == "force"
        assert "骂回去" in r["hint_preview"]

    def test_profanity_cap_visible(self, tmp_path):
        client, _ = _client(
            tmp_path, {"force_level": "feisty", "profanity": False})
        r = client.post("/api/companion/temper/dry-run",
                        json={"text": "傻逼"}).json()
        assert r["level"] == "sharp" and r["profanity_capped"] is True

    def test_deescalation_clears_window(self, tmp_path):
        client, _ = _client(tmp_path)
        r = client.post("/api/companion/temper/dry-run",
                        json={"text": "对不起嘛别生气"}).json()
        assert r["verdict"] == "clear_window"

    def test_hostile_variant_sticky_conditional(self, tmp_path):
        client, _ = _client(tmp_path)
        r = client.post("/api/companion/temper/dry-run",
                        json={"text": "傻逼玩意"}).json()
        assert r["verdict"] == "inject_if_sticky"
        assert r["checks"]["insult"] is False
        assert r["checks"]["hostile"] is True

    def test_taunt_banter_verdict(self, tmp_path):
        """阿龙实录缺口（P2 收编）：「你太没脾气了」→ 接梗而非 none。"""
        client, _ = _client(tmp_path)
        r = client.post("/api/companion/temper/dry-run",
                        json={"text": "你太没脾气了"}).json()
        assert r["verdict"] == "taunt_banter"
        assert r["checks"]["taunt"] is True
        assert r["checks"]["insult"] is False
        assert "上钩" in r["hint_preview"]

    def test_taunt_disabled_falls_to_none(self, tmp_path):
        client, _ = _client(tmp_path, {"taunt_response": False})
        r = client.post("/api/companion/temper/dry-run",
                        json={"text": "你太没脾气了"}).json()
        assert r["verdict"] == "none"

    def test_taunt_off_persona_suppressed(self, tmp_path):
        client, _ = _client(tmp_path, {"force_level": "off"})
        r = client.post("/api/companion/temper/dry-run",
                        json={"text": "你太没脾气了"}).json()
        assert r["verdict"] == "suppressed_off"

    def test_normal_chat_none(self, tmp_path):
        client, _ = _client(tmp_path)
        r = client.post("/api/companion/temper/dry-run",
                        json={"text": "你平时都这样吗"}).json()
        assert r["verdict"] == "none"

    def test_platform_cap_via_conversation_id(self, tmp_path):
        """封顶压过 force：messenger 会话 force=feisty 也只到 sharp。"""
        client, _ = _client(tmp_path, {
            "force_level": "feisty",
            "platform_caps": {"messenger": "sharp"}})
        r = client.post("/api/companion/temper/dry-run", json={
            "text": "傻逼", "conversation_id": "messenger:acct1:peer9"}).json()
        assert r["level"] == "sharp"
        assert r["platform_capped"] is True
        # 非封顶平台不受影响
        r2 = client.post("/api/companion/temper/dry-run", json={
            "text": "傻逼", "conversation_id": "telegram:acct1:peer9"}).json()
        assert r2["level"] == "feisty"
        assert r2["platform_capped"] is False

    def test_off_suppressed(self, tmp_path):
        client, _ = _client(tmp_path, {"force_level": "off"})
        r = client.post("/api/companion/temper/dry-run",
                        json={"text": "傻逼"}).json()
        assert r["verdict"] == "suppressed_off"
        assert r["hint_preview"] == ""

    def test_disabled_verdict(self, tmp_path):
        client, _ = _client(tmp_path, {"enabled": False})
        r = client.post("/api/companion/temper/dry-run",
                        json={"text": "傻逼"}).json()
        assert r["verdict"] == "disabled"

    def test_missing_text_rejected(self, tmp_path):
        client, _ = _client(tmp_path)
        assert client.post("/api/companion/temper/dry-run",
                           json={}).status_code == 400

    def test_bad_conversation_id_rejected(self, tmp_path):
        client, _ = _client(tmp_path)
        r = client.post("/api/companion/temper/dry-run",
                        json={"text": "傻逼", "conversation_id": "notaconv"})
        assert r.status_code == 400

    def test_unknown_persona_reported_not_found(self, tmp_path):
        client, _ = _client(tmp_path)
        r = client.post("/api/companion/temper/dry-run", json={
            "text": "傻逼", "persona_id": "no_such_persona_xyz"}).json()
        assert r["persona"]["found"] is False
        assert r["persona"]["tier"] == "explicit"
        # 查无人设 → 按 default_level 判档，不崩
        assert r["level"] == "gentle"


@pytest.mark.parametrize("payload", [
    {"text": "you idiot", "lang": "en"},
])
def test_dry_run_en_hint(tmp_path, payload):
    client, _ = _client(tmp_path)
    r = client.post("/api/companion/temper/dry-run", json=payload).json()
    assert r["verdict"] == "inject"
    assert "customer service" in r["hint_preview"].lower()
