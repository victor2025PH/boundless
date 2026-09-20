# -*- coding: utf-8 -*-
"""M-5 C（#226 / D-M7，2026-09-06）：AI 生成图片空壳 → 用户版「开发中」态。

X6MDC5 实录：面板报「出图服务器当前不可达…可先联系运维确认服务状态」、今日额度
0/20；backend.log 3h 零探测/零生成记录（探测结果只在内存里）；本机从未成功出图；
「联系运维」是内部话术。钉住：

- ``/api/image/config`` client 形态（未开开发者模式）→ ``dev_state=True``、不下发
  引擎 / 不探出图服务（LAN 不被用户版敲）、只留人设 + 相册兜底；partner / internal /
  开发者模式原面板（引擎 + 探测）；
- 探测结果落 backend.log：``[image_gen] probe host=… ok=… ckpts=… unets=… took=…ms``
  （首次 / 翻转 INFO，同态续探 DEBUG）；用户版首次请求落 ``probe skipped`` 一行；
- cp-image 双树同步、dev_state 分支不渲染生成表单、结果区不出「重新生成」；
- cp-i18n ``cp.image.*`` 全部词条不再出现「联系运维」/ ops，dev_state 双语键齐。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import src.web.routes.image_gen_routes as igr

REPO = Path(__file__).resolve().parents[1]
JS_A = REPO / "shared" / "copilot" / "components" / "cp-image.js"
JS_B = REPO / "desktop" / "renderer" / "shared" / "copilot" / "components" / "cp-image.js"
I18N_A = REPO / "shared" / "copilot" / "i18n" / "cp-i18n.js"
I18N_B = REPO / "desktop" / "renderer" / "shared" / "copilot" / "i18n" / "cp-i18n.js"


class _FakeCM:
    def __init__(self, cfg):
        self.config = cfg


def _mk(tmp_path, monkeypatch, *, flavor="client", developer_mode=False, models=None):
    cfg = {
        "ui_visibility": {"flavor": flavor},
        "companion": {"selfie": {
            "enabled": True,
            "provider": {
                "enabled": True, "backend": "album",
                "album_dir": str(tmp_path / "albums"),
                "out_dir": str(tmp_path / "out"),
                "default_album_key": "mizuki",
                "command_args": ["python", "tools/comfy_infer.py", "--url",
                                 "http://192.168.0.176:8188", "--prompt", "{prompt}",
                                 "--out", "{out}"],
            },
        }},
    }
    probes = {"n": 0}

    def _probe(url):
        probes["n"] += 1
        return models

    monkeypatch.setattr(igr, "_cached_probe", _probe)
    monkeypatch.setattr(igr, "_cached_vram", lambda url: None)
    monkeypatch.setattr(igr, "_cached_queue", lambda url: None)
    sess = {"username": "tester"}
    if developer_mode:
        sess.update({"developer_mode": True, "dev_unlocked": True})

    def auth_dep(request: Request):
        request.scope["session"] = dict(sess)
        return True

    app = FastAPI()
    igr.register_image_gen_routes(app, auth_dep=auth_dep, config_manager=_FakeCM(cfg))
    return TestClient(app), probes


def test_client_config_is_dev_state_and_never_probes(tmp_path, monkeypatch):
    client, probes = _mk(tmp_path, monkeypatch, flavor="client")
    d = client.get("/api/image/config").json()
    assert d["ok"] and d["enabled"] and d["dev_state"] is True
    assert d["engines"] == [] and d["engines_info"] == []
    assert d["comfy_ok"] is None and d["daily_quota"] == 0
    assert d["album_pick"] is True and d["jobs_api"] is False
    assert d["default_persona"] == "mizuki"
    assert probes["n"] == 0, "用户版不得去敲出图服务器"


def test_partner_internal_and_dev_mode_keep_full_panel(tmp_path, monkeypatch):
    for flavor, dev in (("partner", False), ("internal", False), ("client", True)):
        client, probes = _mk(tmp_path, monkeypatch, flavor=flavor, developer_mode=dev,
                             models={"ckpts": ["flux1-dev.safetensors"], "unets": []})
        d = client.get("/api/image/config").json()
        assert "dev_state" not in d, flavor
        assert d["engines"] == ["flux_pulid", "qwen_edit", "z_image"]
        assert d["comfy_ok"] is True
        assert probes["n"] == 1


def test_dev_state_logged_once(tmp_path, monkeypatch, caplog):
    client, _ = _mk(tmp_path, monkeypatch, flavor="client")
    with caplog.at_level(logging.INFO, logger="ai_chat_assistant.image_gen_routes"):
        client.get("/api/image/config")
        client.get("/api/image/config")
    lines = [r.getMessage() for r in caplog.records if "probe skipped" in r.getMessage()]
    assert len(lines) == 1 and "[image_gen]" in lines[0]


def test_probe_log_line_shapes():
    ok = igr.probe_log_line("http://192.168.0.176:8188",
                            {"ckpts": ["a", "b"], "unets": ["c"]}, 123)
    assert ok == "[image_gen] probe host=192.168.0.176:8188 ok=True ckpts=2 unets=1 took=123ms"
    down = igr.probe_log_line("http://192.168.0.176:8188", None, 4001)
    assert down.startswith("[image_gen] probe host=192.168.0.176:8188 ok=False ckpts=0 unets=0")
    assert igr.probe_log_line("", None) == "[image_gen] probe host=- ok=None reason=unconfigured"


def test_cached_probe_logs_first_and_flip_at_info(monkeypatch, caplog):
    # 干净缓存；探针替身按序返回：可达 → 可达 → 不可达
    monkeypatch.setattr(igr, "_probe_cache", {"ts": 0.0, "url": "", "models": None})
    monkeypatch.setattr(igr, "_PROBE_TTL_SEC", 0.0)
    seq = [{"ckpts": ["x"], "unets": []}, {"ckpts": ["x"], "unets": []}, None]
    monkeypatch.setattr(igr, "probe_comfy_models", lambda url, timeout=4.0: seq.pop(0))
    with caplog.at_level(logging.DEBUG, logger="ai_chat_assistant.image_gen_routes"):
        igr._cached_probe("http://h:1")
        igr._cached_probe("http://h:1")
        igr._cached_probe("http://h:1")
    recs = [r for r in caplog.records if "[image_gen] probe host=h:1" in r.getMessage()]
    assert [r.levelno for r in recs] == [logging.INFO, logging.DEBUG, logging.INFO]
    assert "ok=True" in recs[0].getMessage() and "ok=False" in recs[2].getMessage()


def test_cp_image_dev_state_branch_and_mirror_sync():
    a = JS_A.read_bytes()
    assert a == JS_B.read_bytes(), "cp-image.js 双树不同步（谁改组件谁同步两份）"
    js = a.decode("utf-8")
    assert "if (d.dev_state) {" in js and "_renderDevState()" in js
    assert 'this.t("cp.image.dev_state")' in js and 'this.t("cp.image.dev_state_hint")' in js
    # 开发中态：不出生成按钮 / 引擎 / 提示词；结果区不出「重新生成」；不续探
    dev = js.split("_renderDevState() {", 1)[1].split("_isDevState()", 1)[0]
    assert 'data-act="gen"' not in dev and 'data-role="engine"' not in dev
    assert 'data-role="prompt"' not in dev and 'data-role="stock"' in dev
    assert 'this._isDevState() ? "" : \'<button type="button" data-act="redo">' in js
    assert "if (this._busy || this._isDevState()) return;" in js
    assert "if (this._isDevState()) return;         // 开发中态" in js


def test_cp_i18n_no_ops_wording_and_dev_keys():
    a = I18N_A.read_text(encoding="utf-8")
    assert a == I18N_B.read_text(encoding="utf-8"), "cp-i18n.js 双树不同步"
    import re
    img_lines = [l for l in a.splitlines() if '"cp.image.' in l]
    assert img_lines
    for l in img_lines:
        assert "联系运维" not in l and "運維" not in l, l
        assert not re.search(r"\b(?:ask|contact|to|with|for) ops\b", l), l
    for key in ("cp.image.dev_state", "cp.image.dev_state_hint"):
        assert a.count(f'"{key}"') == 2, f"{key} 需 zh/en 双语各一条"
    assert "开发中，暂不可用" in a
