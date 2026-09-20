"""功能总览 API（/api/setup/features*，P1）门禁。

自建 FastAPI app + 假 auth dep + **真 ConfigManager**（config_path 指 tmp——
set_overlay_flag 真写 tmp/config.local.yaml + 真深合并内存，端到端验证
「点开关 → overlay 落盘 → 状态回读」整条链，绝不碰仓库 config/）。

覆盖：清单四态与注册表一致 / A、B 类可开可关 / 开 B 类缺依赖 409 /
C 类（含 show=False 隐藏键）不可自助 404/409 / viewer 只读 403 /
本地化 name 非裸键（动态拼键 tr 接线活着）。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.utils.config_manager import ConfigManager
from src.utils.feature_registry import dig, ui_features
from src.web.routes.feature_center_routes import register_feature_center_routes


def _build(tmp_path: Path, cfg: dict):
    cm = ConfigManager(config_path=str(tmp_path / "config.yaml"))
    cm.config = cfg
    sess = {"role": "", "user": "tester"}
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = dict(sess)

    register_feature_center_routes(app, auth_dep, config_manager=cm)
    return TestClient(app), cm, sess


def _by_key(items, key):
    for it in items:
        if it["key"] == key:
            return it
    raise AssertionError(f"清单缺 {key}")


def test_list_states_match_registry(tmp_path):
    client, _cm, _ = _build(tmp_path, {})
    d = client.get("/api/setup/features").json()
    assert d["ok"] is True
    assert len(d["features"]) == len(ui_features())
    goals = _by_key(d["features"], "companion.goals.enabled")
    assert goals["state"] == "available" and goals["toggleable"] is True
    memvec = _by_key(d["features"], "memory.vector.enabled")
    assert memvec["state"] == "needs_dep"
    assert memvec["extra"], "缺依赖必须带人话说明"
    voice = _by_key(d["features"], "avatar_voice.enabled")
    assert voice["state"] == "locked" and voice["toggleable"] is False
    assert voice["extra"], "locked 必须带原因"
    # 动态拼键的 tr 接线活着：name 是译文不是裸键
    assert goals["name"] and "fc_f_" not in goals["name"]


def test_list_reports_on_state(tmp_path):
    client, _cm, _ = _build(
        tmp_path, {"companion": {"goals": {"enabled": True}},
                   "avatar_voice": {"enabled": True}})
    d = client.get("/api/setup/features").json()
    assert _by_key(d["features"], "companion.goals.enabled")["state"] == "on"
    # C 类被部署 overlay 打开 → 如实报 on（不装「未开放」），但仍不可自助切
    voice = _by_key(d["features"], "avatar_voice.enabled")
    assert voice["state"] == "on" and voice["toggleable"] is False


def test_toggle_writes_overlay_and_memory(tmp_path):
    client, cm, _ = _build(tmp_path, {})
    r = client.post("/api/setup/features/toggle",
                    json={"key": "companion.goals.enabled", "enabled": True})
    assert r.status_code == 200
    assert r.json()["item"]["state"] == "on"
    assert dig(cm.config, "companion.goals.enabled") is True
    ov = yaml.safe_load(
        (tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
    assert ov["companion"]["goals"]["enabled"] is True
    # 再关回去
    r2 = client.post("/api/setup/features/toggle",
                     json={"key": "companion.goals.enabled", "enabled": False})
    assert r2.status_code == 200
    assert r2.json()["item"]["enabled"] is False
    assert dig(cm.config, "companion.goals.enabled") is False


def test_toggle_b_class_blocked_without_deps(tmp_path):
    client, cm, _ = _build(tmp_path, {})
    r = client.post("/api/setup/features/toggle",
                    json={"key": "memory.vector.enabled", "enabled": True})
    assert r.status_code == 409, "缺依赖硬开=「开了但跑不动」，必须拒绝"
    assert dig(cm.config, "memory.vector.enabled") is None
    # 配上嵌入端点后放行
    cm.config = {"ai": {"embedding_base_url": "http://127.0.0.1:11434"}}
    r2 = client.post("/api/setup/features/toggle",
                     json={"key": "memory.vector.enabled", "enabled": True})
    assert r2.status_code == 200
    # 关闭不查依赖（关永远允许）
    r3 = client.post("/api/setup/features/toggle",
                     json={"key": "memory.vector.enabled", "enabled": False})
    assert r3.status_code == 200


def test_toggle_c_class_and_unknown_rejected(tmp_path):
    client, cm, _ = _build(tmp_path, {})
    assert client.post("/api/setup/features/toggle",
                       json={"key": "avatar_voice.enabled",
                             "enabled": True}).status_code == 409
    assert client.post("/api/setup/features/toggle",
                       json={"key": "no.such.flag",
                             "enabled": True}).status_code == 404
    # show=False 的种子门禁键不暴露给自助开关
    assert client.post("/api/setup/features/toggle",
                       json={"key": "realtime_voice.enabled",
                             "enabled": True}).status_code == 404
    assert not (tmp_path / "config.local.yaml").exists(), "被拒的操作零落盘"


def test_viewer_readonly(tmp_path):
    client, _cm, sess = _build(tmp_path, {})
    sess["role"] = "viewer"
    assert client.get("/api/setup/features").status_code == 200
    r = client.post("/api/setup/features/toggle",
                    json={"key": "companion.goals.enabled", "enabled": True})
    assert r.status_code == 403


# ── P2：授权分层（needs_upgrade 第五态 + 档位墙） ────────────────────────────
# plan_override 走 feature_gate 的免单例路径（不读 license 文件），测试密闭。

def _gate_cfg(plan: str, **extra) -> dict:
    cfg = {"licensing": {"feature_gate": {"enabled": True,
                                          "plan_override": plan}},
           "translation": {"engines": {"order": ["ollama_mt", "ai"]}}}
    cfg.update(extra)
    return cfg


def test_needs_upgrade_state_and_toggle_wall(tmp_path):
    client, cm, _ = _build(tmp_path, _gate_cfg("community"))
    d = client.get("/api/setup/features").json()
    xc = _by_key(d["features"], "translation.engines.confidence_switch.enabled")
    assert xc["state"] == "needs_upgrade", "依赖齐但档位不够 → 档位墙优先展示"
    assert xc["toggleable"] is False and xc["extra"]
    # A 类（拍板升 A 后）人人标配，不受档位墙影响
    dp = _by_key(d["features"], "companion.deep_persona.enabled")
    assert dp["state"] == "available"
    # 未归 family 的功能不受 gate 影响
    assert _by_key(d["features"],
                   "companion.goals.enabled")["state"] == "available"
    assert _by_key(d["features"],
                   "inbox.reply_style.bubbles.enabled")["state"] == "available"
    # 档位墙：开被拒且零落盘
    r = client.post("/api/setup/features/toggle",
                    json={"key": xc["key"], "enabled": True})
    assert r.status_code == 409
    assert dig(cm.config, xc["key"]) is None
    # meta：gate 开时带档位
    assert d["gate_enabled"] is True and d["plan"] == "community"
    assert d["version"], "版本指纹必须存在（源码态=dev）"


def test_plan_override_unlocks_and_gate_off_neutral(tmp_path):
    client, _cm, _ = _build(tmp_path, _gate_cfg("flagship"))
    d = client.get("/api/setup/features").json()
    assert _by_key(d["features"],
                   "translation.engines.confidence_switch.enabled"
                   )["state"] == "available"
    r = client.post("/api/setup/features/toggle",
                    json={"key": "translation.engines.confidence_switch.enabled",
                          "enabled": True})
    assert r.status_code == 200
    # gate 关（默认）→ 无 needs_upgrade 且 meta 不带档位
    client2, _cm2, _ = _build(
        tmp_path / "b",
        {"translation": {"engines": {"order": ["ollama_mt", "ai"]}}})
    d2 = client2.get("/api/setup/features").json()
    assert _by_key(d2["features"],
                   "translation.engines.confidence_switch.enabled"
                   )["state"] == "available"
    assert d2["gate_enabled"] is False and d2["plan"] == ""


def test_enabled_feature_stays_on_despite_plan_lock(tmp_path):
    """先开后降档 → 如实报 on（运行时强制是 feature_gate 接线点的事，总览不撒谎）。"""
    cfg = _gate_cfg("community")
    cfg["translation"]["engines"]["confidence_switch"] = {"enabled": True}
    client, _cm, _ = _build(tmp_path, cfg)
    d = client.get("/api/setup/features").json()
    xc = _by_key(d["features"], "translation.engines.confidence_switch.enabled")
    assert xc["state"] == "on"
