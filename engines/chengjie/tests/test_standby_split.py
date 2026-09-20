# -*- coding: utf-8 -*-
"""#12 拆开控制（2026-08-30）：POST /api/companion/standby {"set": …} 单键写入口。

守三件事（钧 0830 01:54/01:57 审计行实锤的反面）：

1. **单键语义**：改「新会话默认档」只写 ``inbox.auto_draft.automation_mode``，
   绝不顺手翻 ``l2_autosend.deliver``；关「真发总闸」只走 deliver 能力意图，
   绝不改默认档——三档预设的捆绑翻转正是「开着全自动却不工作」的根因。
2. **护栏不被绕过**：deliver 走 ``_apply_plan``（check_toggle 同一护栏 + 审计），
   开真发在无 auto_ai 依据时照旧被拦，响应 blocked 如实回。
3. **读侧回显**：GET/POST 都带 ``split`` 段（default_mode/worker/deliver/
   auto_ai_rows），设置页据此渲染面板与影响面披露；旧前端不识此键零影响。
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.web.routes.companion_capability_routes import (
    register_companion_capability_routes,
)


def _noop_auth(request: Request):
    return None


class _CM:
    """极简 ConfigManager 假件：set_overlay_flag 按点路径就地改 config。"""

    def __init__(self, tmp_path, config):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("telegram: {enabled: false}\n", encoding="utf-8")
        self.config_path = str(cfg)
        self.config = config
        self.writes = []

    def set_overlay_flag(self, path, value):
        self.writes.append((path, value))
        node = self.config
        parts = str(path).split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
        return True, ""


class _Store:
    def __init__(self, modes):
        self._modes = dict(modes)

    def all_automation_modes(self):
        return dict(self._modes)


def _client(tmp_path, *, config=None, modes=None):
    cfg = config if config is not None else {
        "inbox": {
            "l2_autosend": {"enabled": True, "deliver": True},
            "auto_draft": {"automation_mode": "auto_ai"},
        },
        "companion_send_gate": {"enabled": True},
    }
    cm = _CM(tmp_path, cfg)
    app = FastAPI()
    from starlette.middleware.sessions import SessionMiddleware

    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.config_manager = cm
    app.state.inbox_store = _Store(
        modes if modes is not None
        else {"line:a:u1": "auto_ai", "line:a:u2": "review"})
    register_companion_capability_routes(app, api_auth=_noop_auth)

    @app.get("/_login")
    async def _login(request: Request):  # 测试专用：种主管 session
        request.session["user_id"] = "boss"
        request.session["role"] = "master"
        return {"ok": True}

    client = TestClient(app)
    client.get("/_login")
    return client, cm


def test_get_standby_carries_split_section(tmp_path):
    client, _cm = _client(tmp_path)
    d = client.get("/api/companion/standby").json()
    assert d["ok"] is True
    sp = d["split"]
    assert sp["default_mode"] == "auto_ai"
    assert sp["worker"] is True and sp["deliver"] is True
    assert sp["auto_ai_rows"] == 1


def test_set_default_mode_touches_only_that_key(tmp_path):
    client, cm = _client(tmp_path)
    d = client.post("/api/companion/standby",
                    json={"set": {"default_mode": "review"}}).json()
    assert d["ok"] is True
    assert d["split_applied"] == {"default_mode": "review"}
    assert d["split"]["default_mode"] == "review"
    # 单键语义：只写默认档，deliver/worker 一个都不许碰
    assert [w[0] for w in cm.writes] == ["inbox.auto_draft.automation_mode"]
    assert d["split"]["deliver"] is True and d["split"]["worker"] is True
    # 三档反推不受影响（worker+deliver 仍双开=watching）
    assert d["mode"] == "watching"


def test_set_default_mode_rejects_unknown(tmp_path):
    client, cm = _client(tmp_path)
    d = client.post("/api/companion/standby",
                    json={"set": {"default_mode": "bogus"}}).json()
    assert d["ok"] is False
    assert cm.writes == []


def test_set_deliver_off_keeps_default_mode_and_worker(tmp_path):
    client, cm = _client(tmp_path)
    d = client.post("/api/companion/standby",
                    json={"set": {"deliver": False}}).json()
    assert d["ok"] is True
    assert d["split_applied"] == {"deliver": False}
    # 只走 deliver 能力路径（check_toggle 解析出的 flag_path）
    assert [w[0] for w in cm.writes] == ["inbox.l2_autosend.deliver"]
    assert d["split"]["deliver"] is False
    assert d["split"]["default_mode"] == "auto_ai"   # 默认档纹丝不动
    assert d["split"]["worker"] is True              # worker 纹丝不动
    assert d["mode"] == "suggest"                    # 反推档随真值走
    # 热接线字段在场（测试 app 无闭包 → 如实 not_wired，绝不谎报生效）
    assert d["rewire"]["rewired"] is False


def test_set_deliver_on_guardrail_still_applies(tmp_path):
    """开真发照旧过 check_toggle：全局默认档 review 且零 auto_ai 会话 → 拦下，
    blocked 如实回，overlay 零写入——拆分入口绝不是护栏旁路。"""
    cfg = {
        "inbox": {
            "l2_autosend": {"enabled": True, "deliver": False},
            "auto_draft": {"automation_mode": "review"},
        },
    }
    client, cm = _client(tmp_path, config=cfg, modes={"line:a:u2": "review"})
    d = client.post("/api/companion/standby",
                    json={"set": {"deliver": True}}).json()
    assert d["ok"] is True                    # 请求受理，但意图被护栏拦
    assert d["split_applied"] == {"deliver": True}
    assert [b["key"] for b in d["blocked"]] == ["l2_autosend_deliver"]
    assert cm.writes == []
    assert d["split"]["deliver"] is False     # 真值未变，回显如实


def test_preset_path_unaffected_and_echoes_split(tmp_path):
    """带 mode 的三档预设路径行为保持（捆绑语义不变），响应新带 split 回显。"""
    client, cm = _client(tmp_path)
    d = client.post("/api/companion/standby", json={"mode": "suggest"}).json()
    assert d["ok"] is True and d["requested"] == "suggest"
    paths = [w[0] for w in cm.writes]
    assert "inbox.auto_draft.automation_mode" in paths   # 捆绑：默认档
    assert "inbox.l2_autosend.deliver" in paths          # 捆绑：真发
    assert d["split"]["deliver"] is False
    assert d["split"]["default_mode"] == "review"
