"""LINE 渠道页语音配置面 — 路由白名单 + 模板/i18n 静态门禁。

消费方证据（本文件守护的契约链）：
- src/integrations/line_rpa/runner.py:335-336
    vo = self._cfg.get("voice_output"); vo.get("enabled") 闸门
    （_maybe_generate_tts_for_pending，approval 队列 TTS 预览）
- src/integrations/line_rpa/runner.py:343
    voice_cfg=dict(vo) 整段传 shared.tts_preview.generate_approval_tts
- src/integrations/shared/tts_preview.py:103
    get_tts_pipeline(voice_cfg) → src/ai/tts_pipeline.py:515/520 读 backend/voice
- 投递侧 runner.py:2080 只发 final_reply 文本 → TTS 仅审核试听，不随审批投递语音

路由侧：PUT /api/line-rpa/config 白名单放行 voice_output 并 merge 进
config + svc.reconfigure 热更新；voice_output 变更触发共享 TTS 单例重建
（get_tts_pipeline 首建方 cfg 生效，messenger_rpa_routes 同款模式）。
测试全程 hermetic：MagicMock config_manager / service，不起 ADB、不写真实配置。
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE = _ROOT / "src" / "web" / "templates" / "_channel_body_line.html"
_RUNNER = _ROOT / "src" / "integrations" / "line_rpa" / "runner.py"


# ── 测试 app（模式对齐 tests/test_line_rpa_send_queue.py） ─────────────────────

def _build_app(svc, cm):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.routes.line_rpa_routes import register_line_rpa_routes

    app = FastAPI()
    app.state.line_rpa_service = svc

    def api_auth(request):
        pass

    def page_auth(request):
        pass

    register_line_rpa_routes(
        app,
        page_auth=page_auth,
        api_auth=api_auth,
        templates=MagicMock(),
        config_manager=cm,
        audit_store=None,
    )
    return TestClient(app)


def _mock_cm(initial: dict | None = None):
    cm = MagicMock()
    # 非空种子：路由取 `config_manager.config or {}`，空 dict（falsy）会被换成
    # 新字面量导致突变不可见——生产配置永不为空，这里对齐生产形态。
    cm.config = dict(initial) if initial else {"line_rpa": {}}
    return cm


# ── PUT /api/line-rpa/config：白名单 + merge + 热更新 ─────────────────────────

def test_put_voice_output_enabled_persists_and_reconfigures():
    svc = MagicMock()
    cm = _mock_cm()
    client = _build_app(svc, cm)

    r = client.put("/api/line-rpa/config", json={"voice_output": {"enabled": True}})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert "voice_output" in r.json()["updated_keys"]

    # 落进配置
    assert cm.config["line_rpa"]["voice_output"]["enabled"] is True
    cm.save.assert_called_once()

    # 热更新进 service（service.reconfigure → runner._cfg → runner.py:335 消费）
    svc.reconfigure.assert_called_once()
    passed_cfg = svc.reconfigure.call_args.args[0]
    assert passed_cfg["voice_output"]["enabled"] is True


def test_put_voice_output_merges_with_existing_subkeys():
    """dict-merge 语义：只发 enabled 不得抹掉手工配置的 backend/voice。"""
    svc = MagicMock()
    cm = _mock_cm({
        "line_rpa": {
            "voice_output": {"enabled": False, "backend": "avatar_clone", "voice": "x1"},
        },
    })
    client = _build_app(svc, cm)

    r = client.put("/api/line-rpa/config", json={"voice_output": {"enabled": True}})
    assert r.status_code == 200, r.text
    vo = cm.config["line_rpa"]["voice_output"]
    assert vo["enabled"] is True
    assert vo["backend"] == "avatar_clone"
    assert vo["voice"] == "x1"


def test_put_voice_output_backend_voice_pass_whitelist():
    """backend/voice 有真实消费方（tts_pipeline.py:515/520），须能通过白名单。"""
    svc = MagicMock()
    cm = _mock_cm()
    client = _build_app(svc, cm)

    r = client.put("/api/line-rpa/config", json={
        "voice_output": {"enabled": True, "backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural"},
    })
    assert r.status_code == 200, r.text
    vo = cm.config["line_rpa"]["voice_output"]
    assert vo["backend"] == "edge_tts"
    assert vo["voice"] == "zh-CN-XiaoxiaoNeural"


def test_put_voice_output_triggers_tts_singleton_reset(monkeypatch):
    """voice_output 变更 → reset_tts_pipeline（共享单例首建方 cfg 生效，不重建改动不生效）。"""
    calls = []
    monkeypatch.setattr(
        "src.ai.tts_pipeline.reset_tts_pipeline", lambda: calls.append(1))
    svc = MagicMock()
    client = _build_app(svc, _mock_cm())

    r = client.put("/api/line-rpa/config", json={"voice_output": {"enabled": True}})
    assert r.status_code == 200
    assert calls, "voice_output 变更须触发 reset_tts_pipeline"


def test_put_without_voice_output_does_not_reset_tts(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "src.ai.tts_pipeline.reset_tts_pipeline", lambda: calls.append(1))
    svc = MagicMock()
    client = _build_app(svc, _mock_cm())

    r = client.put("/api/line-rpa/config", json={"daily_cap": 5})
    assert r.status_code == 200
    assert not calls, "无 voice_output 的保存不应重建 TTS 单例"


def test_put_unknown_key_still_rejected():
    """白名单闸门未被放宽：未知键仍 400。"""
    svc = MagicMock()
    client = _build_app(svc, _mock_cm())

    r = client.put("/api/line-rpa/config", json={
        "voice_output": {"enabled": True},
        "totally_unknown_key": 1,
    })
    assert r.status_code == 400


# ── GET /api/line-rpa/config：voice_output 回读 ───────────────────────────────

def test_get_config_returns_voice_output_section():
    svc = MagicMock()
    svc.effective_config.return_value = {
        "enabled": True,
        "voice_output": {"enabled": True, "backend": "edge_tts", "voice": ""},
    }
    cm = _mock_cm({"line_rpa": {"voice_output": {"enabled": True}}})
    client = _build_app(svc, cm)

    r = client.get("/api/line-rpa/config")
    assert r.status_code == 200
    d = r.json()
    assert d["effective"]["voice_output"]["enabled"] is True
    assert d["effective"]["voice_output"]["backend"] == "edge_tts"
    assert d["raw"]["voice_output"]["enabled"] is True


# ── 消费方契约钉（防「控件在、消费方没了」的哑控件回归） ────────────────────────

def test_runner_still_consumes_voice_output():
    src = _RUNNER.read_text(encoding="utf-8")
    assert 'self._cfg.get("voice_output")' in src, \
        "runner 不再读 voice_output —— 设置页语音块成哑控件，需同步下架或改接线"
    assert "generate_approval_tts" in src


# ── 模板静态门禁 ──────────────────────────────────────────────────────────────

def _tpl() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


def test_template_has_voice_controls_with_unique_ids():
    tpl = _tpl()
    for el_id in ("cfg-voice-on", "cfg-voice-backend", "cfg-voice-voice",
                  "lr-voice-test-text", "lr-voice-test-btn", "lr-voice-test-out"):
        n = len(re.findall(rf'id="{re.escape(el_id)}"', tpl))
        assert n == 1, f"id={el_id} 应恰好出现 1 次（实际 {n} 次）"


def test_template_save_payload_includes_voice_output():
    tpl = _tpl()
    assert re.search(r"voice_output:\s*\{", tpl), "saveLineRpaCfg payload 缺 voice_output 段"
    # 加载回填 + 保存读取，两处都要引用开关
    assert tpl.count("getElementById('cfg-voice-on')") >= 2


def test_template_tts_test_button_wiring():
    tpl = _tpl()
    assert 'onclick="lrVoiceTtsTest()"' in tpl
    # 顶层全局函数（非 IIFE 内），行首声明
    assert re.search(r"^async function lrVoiceTtsTest\(", tpl, re.M), \
        "lrVoiceTtsTest 须为顶层全局函数供 inline onclick 调用"
    # 走 lrPost（内含 apiFetch + CSRF + timeoutMs:60000），端点为全站 TTS 预览
    m = re.search(r"async function lrVoiceTtsTest\(.*?\n\}", tpl, re.S)
    assert m and "lrPost('/api/voice/tts-test'" in m.group(0)


def test_template_no_bare_fetch():
    """模板禁裸 fetch：请求一律走 apiFetch 包装（apiFetch 为大写 F，不会误伤）。"""
    tpl = _tpl()
    assert re.search(r"(?<![\w$])fetch\(", tpl) is None


def test_template_no_new_inline_hex_in_voice_block():
    """语音块内不引入内联 hex 色（结构色一律 var(--*) 令牌）。"""
    tpl = _tpl()
    start = tpl.index("lr_voice_sec")
    end = tpl.index("限制与安全", start)
    block = tpl[start:end]
    assert re.search(r"#[0-9a-fA-F]{3,8}\b", block) is None, \
        "语音配置块出现内联 hex 色，应使用 --tk-*/--t3 等令牌"


# ── i18n：lr_voice_* 键 ZH+EN 双语齐备 ────────────────────────────────────────

def test_i18n_voice_keys_bilingual_and_referenced():
    from src.web.i18n_packs.line_page import EN, ZH

    tpl = _tpl()
    referenced = set(re.findall(r"lr_voice_[a-z_]+", tpl))
    assert referenced, "模板应引用 lr_voice_* 键"

    zh_keys = {k for k in ZH if k.startswith("lr_voice_")}
    en_keys = {k for k in EN if k.startswith("lr_voice_")}
    assert zh_keys == en_keys, f"ZH/EN 键集不一致: {zh_keys ^ en_keys}"
    missing = referenced - zh_keys
    assert not missing, f"模板引用了未登记的键: {missing}"

    for k in zh_keys:
        assert str(ZH[k]).strip(), f"ZH[{k}] 为空"
        assert str(EN[k]).strip(), f"EN[{k}] 为空"


def test_i18n_voice_keys_not_colliding_with_legacy_lr_namespace():
    """learner 页占用 lr_sNNN/lr_jsNNN 数字命名空间；line_page 的 lr_ 键须归
    显式语义子命名空间（lr_voice_* / lr_reply_mode_*）防撞。新增功能块请在
    _ALLOWED_SUBNS 登记新子命名空间，勿直接用裸 lr_ 前缀。"""
    from src.web.i18n_packs.line_page import ZH

    _ALLOWED_SUBNS = ("lr_voice_", "lr_reply_mode_")
    for k in ZH:
        if k.startswith("lr_"):
            assert k.startswith(_ALLOWED_SUBNS), (
                f"line_page 内 lr_ 前缀键须归 {_ALLOWED_SUBNS} 之一: {k}")
