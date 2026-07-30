"""托管识图供给的**生命周期**门禁（2026-07-31，「一键开齐入站识别点了没用」事故）。

事故链（四环，缺一环症状都不成立）：
  ① 识图后端只在 ``main.py`` 启动时注入一次，且只在**内存**（令牌不入库）；
  ② ``check_and_hot_reload`` 会把 config 整体从磁盘重建，``_apply_env_overrides``
     此前只回放 AI —— 于是任何一次写 overlay 都会把识图后端抹掉；
  ③ 而写 overlay 正是「一键开齐入站识别」自己干的事（写 ``vision.enabled: true``），
     30s 内热重载 → 按钮把自己刚要用的后端弄没了 → 自检黄灯「开了但后端未就绪」；
  ④ 令牌后到（首启没网 / 未领试用）时也没有补供给的路径：守护线程只续 AI、
     领试用钩子只补 AI+Telegram → 要等下次重启。

单函数语义已由 ``test_hosted_gateway.py`` 钉住；本文件钉的是**跨模块的生命周期不变量**
（注入 → 热重载 → 仍可用 / 令牌后到 → 自愈 / 按钮先供给再开关），那四环任何一环回归，
这里先红。
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import src.ai.hosted_gateway as hg
from src.companion.media_capability import collect_media_status, vision_backend_ready
from src.utils.config_manager import ConfigManager
from src.web.routes.companion_capability_routes import (
    register_companion_capability_routes,
)

_MIN_CONFIG = {
    # 热重载校验要求 telegram 节点存在（_validate_hot_reload_config）
    "telegram": {"enabled": False, "api_id": "1", "api_hash": "h", "phone_number": "+1"},
    "ai": {"api_key": "cx.tok"},
    "skills": {"enabled": []},
    "licensing": {"hosted_ai": {"enabled": True, "site_url": "https://bd2026.cc"}},
}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """本文件一律不打官网。

    ``_provision_media_backends`` 会先跑 ``ensure_hosted_ai``（没令牌时真发 HTTP 领）。
    首版漏了这道闸，两条「无令牌」用例在本机**真领到了令牌**从而误判为通过——既慢又看
    运气，还会往厂商试用台账写记录。领令牌的语义由 test_hosted_gateway 覆盖，这里只关心
    「领不到时识图供给如实失败」。
    """
    monkeypatch.setattr(hg, "_http", lambda *a, **k: {"ok": False, "error": "network"})


def _cm(tmp_path, extra=None) -> ConfigManager:
    cfg = dict(_MIN_CONFIG)
    if extra:
        cfg.update(extra)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    cm = ConfigManager(str(p))
    asyncio.run(cm.load())
    return cm


def _hot_reload(cm) -> bool:
    """强制走一次真的热重载（绕过 30s 节流 + 造 mtime 变化）。"""
    ov = cm._overlay_path()
    target = ov if ov.exists() else cm.config_path
    os.utime(target, (time.time() + 10, time.time() + 10))
    cm._last_hot_reload_check = 0
    return cm.check_and_hot_reload()


# ── ① 注入落 env（热重载回放的前提）───────────────────────────────────────

def test_vision_injection_writes_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _cm(tmp_path)
    assert hg.ensure_hosted_vision(cm) is True
    assert os.environ.get(hg.VISION_ENV_BASE) == "https://bd2026.cc/api/ai/v1"
    assert os.environ.get(hg.VISION_ENV_MODEL)


# ── ② 核心回归钉：写 overlay 触发热重载后，识图后端必须还在 ────────────────

def test_vision_survives_overlay_write_and_hot_reload(tmp_path, monkeypatch):
    """事故复现路径：点「一键开齐入站识别」→ 写 overlay → 热重载 → 后端不能消失。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    monkeypatch.setenv("AITR_HOSTED_AI_KEY", "cx.tok")  # 令牌回放（识图复用同一枚）
    cm = _cm(tmp_path)
    assert hg.ensure_hosted_vision(cm) is True
    assert vision_backend_ready(cm.config) is True

    # 按钮做的事：写 overlay 开关（这一步会改 overlay mtime → 触发热重载）
    ok, _msg = cm.set_overlay_flag("vision.enabled", True)
    assert ok
    assert _hot_reload(cm) is True

    assert vision_backend_ready(cm.config) is True, "热重载把托管识图后端抹掉了（事故回归）"
    rep = collect_media_status(cm.config)
    vis = next(c for c in rep["capabilities"] if c["key"] == "vision_inbound")
    assert vis["stage"] == "active"


def test_hot_reload_replay_respects_explicit_off(tmp_path, monkeypatch):
    """运营点「全部关闭」写下的 false 是**意图**，回放只补供给、不许把它扳回来。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    monkeypatch.setenv("AITR_HOSTED_AI_KEY", "cx.tok")
    cm = _cm(tmp_path)
    assert hg.ensure_hosted_vision(cm) is True

    ok, _ = cm.set_overlay_flag("vision.enabled", False)
    assert ok
    _hot_reload(cm)

    assert cm.config["vision"]["enabled"] is False
    # 供给仍在（后端没被删），只是开关按运营意图关着
    assert vision_backend_ready(cm.config) is True


def test_hot_reload_replay_never_overrides_user_backend(tmp_path, monkeypatch):
    """自建/自配后端（无托管标记）→ 回放必须绕开，绝不改人家的地址。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    monkeypatch.setenv("AITR_HOSTED_AI_KEY", "cx.tok")
    monkeypatch.setenv(hg.VISION_ENV_BASE, "https://bd2026.cc/api/ai/v1")
    cm = _cm(tmp_path, {"vision": {"enabled": True, "provider": "openai_compatible",
                                   "base_url": "http://192.168.1.9:11434"}})
    _hot_reload(cm)
    assert cm.config["vision"]["base_url"] == "http://192.168.1.9:11434"
    assert cm.config["vision"].get("_hosted_vision") is not True


# ── ③ 令牌后到 → 无需重启自愈 ─────────────────────────────────────────────

def test_refresh_once_provisions_vision_when_token_arrives_late(tmp_path, monkeypatch):
    """首启没令牌 → 识图供给失败；令牌后到时守护线程那一轮必须把识图补上。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _cm(tmp_path, {"ai": {"api_key": ""}})
    assert hg.ensure_hosted_vision(cm) is False
    assert vision_backend_ready(cm.config) is False

    def _fake_ai(config_manager):  # 模拟这一轮真的领到了令牌
        config_manager.config["ai"]["api_key"] = "cx.late"
        return True

    monkeypatch.setattr(hg, "ensure_hosted_ai", _fake_ai)
    hg.refresh_once(cm)
    assert vision_backend_ready(cm.config) is True
    assert cm.config["vision"]["api_key"] == "cx.late"


def test_apply_syncs_vision_token(tmp_path, monkeypatch):
    """令牌换新只走 AI 那条路 → 已注入的识图必须同步，否则一路 401 且静默。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _cm(tmp_path)
    assert hg.ensure_hosted_vision(cm) is True
    hg._apply(cm, {"token": "cx.rotated", "base_url": "https://bd2026.cc/api/ai/v1"})
    assert cm.config["vision"]["api_key"] == "cx.rotated"


# ── 供给原因码（UI 据此给「点得动的下一步」）──────────────────────────────

def test_vision_provision_reason_codes(tmp_path, monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    monkeypatch.delenv("AITR_MANAGED_EDITION", raising=False)
    assert hg.vision_provision_reason({"licensing": {"hosted_ai": {"enabled": False}}}) == "self_hosted"

    hosted = {"licensing": {"hosted_ai": {"enabled": True}}}
    assert hg.vision_provision_reason({**hosted, "ai": {"api_key": ""}}) == "no_token"
    assert hg.vision_provision_reason({**hosted, "ai": {"api_key": "cx.t"}}) == "not_provisioned"
    assert hg.vision_provision_reason({
        **hosted, "ai": {"api_key": "cx.t"},
        "vision": {"base_url": "http://lan:11434"}}) == "user_backend"
    assert hg.vision_provision_reason({
        **hosted, "ai": {"api_key": "cx.t"},
        "vision": {"_hosted_vision": True, "base_url": "https://bd2026.cc/api/ai/v1"}}) == ""


# ── ④ 按钮对结果负责：预设先供给再写开关 ──────────────────────────────────

def _auth(request: Request):  # Request 注解不能省：否则 FastAPI 当查询参数解析 → 422
    return True


def _client(cm):
    app = FastAPI()
    app.state.config_manager = cm
    register_companion_capability_routes(app, api_auth=_auth)
    return TestClient(app)


def test_preset_provisions_backend_before_enabling(tmp_path, monkeypatch):
    """点一次就该真的能用：开启类预设必须先补供给，warnings 也得是修完之后的账。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _cm(tmp_path)
    client = _client(cm)

    r = client.post("/api/companion/media-capabilities/preset",
                    json={"name": "understand_all"})
    d = r.json()
    assert d["ok"] is True
    assert d["provisioned"]["vision"] is True
    assert vision_backend_ready(cm.config) is True
    # 识图那条告警不该再出现（供给已补上）
    assert not any("识图" in w for w in d.get("warnings") or [])
    vis = next(c for c in d["status"]["capabilities"] if c["key"] == "vision_inbound")
    assert vis["stage"] == "active"


def test_preset_still_writes_flags_when_provision_fails(tmp_path, monkeypatch):
    """供给失败不该拦住开关（保留「先开开关后补后端」的既有语义），但要如实回报。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _cm(tmp_path, {"ai": {"api_key": ""}})  # 没令牌 → 供给必失败
    client = _client(cm)

    d = client.post("/api/companion/media-capabilities/preset",
                    json={"name": "understand_all"}).json()
    assert d["ok"] is True
    assert d["provisioned"]["vision"] is False
    assert d["provisioned"]["vision_reason"] == "no_token"
    assert cm.config["vision"]["enabled"] is True  # 开关照写


def test_provision_endpoint_retries_and_reports_reason(tmp_path, monkeypatch):
    """「重试接入」：能修就修好，修不了给确定性原因码（替代「请联系客服」）。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _cm(tmp_path)
    client = _client(cm)

    d = client.post("/api/companion/media-capabilities/provision", json={}).json()
    assert d["ok"] is True and d["provisioned"]["vision"] is True and d["reason"] == ""

    cm2 = _cm(tmp_path, {"ai": {"api_key": ""}})
    d2 = _client(cm2).post("/api/companion/media-capabilities/provision", json={}).json()
    assert d2["provisioned"]["vision"] is False and d2["reason"] == "no_token"
