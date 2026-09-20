"""Q-22 门禁（#287 #288 #239追加 #304，2026-09-12）：克隆声不可用**不再静默换系统音**。

事故（V9YAAX / YNH6ZW，1.0.81–1.0.82）：STEVEN（克隆男声）× 日文客户 → 语种闸命中 →
`tts_pipeline` 静默回落 edge `ja-JP-KeitaNeural`，客户听到的是与人设无关的系统声，
坐席界面一切「成功」。本文件钉住六条契约：

1. 克隆 × 语种超能力 → **阻断**：ok=False、error=clone_unavailable:…、零音频、edge 零调用，
   响应带可二次确认的同语种同性别 system_voice；
2. 坐席显式 confirm_system_voice=True → 才出 edge（Keita），并打 system_voice_confirmed 标；
3. 自动链（A 线 sender / B 线 voice_autosend）遇阻断 → 改发文字 + `[tts] skip_voice` 一行，
   永不出系统音；冷启动等一拍只重试一次；
4. 文本语种 zh × 计划 tts_lang=yue → skip_voice lang_mismatch（#304 十翼「突然说广东话」的
   第二道保险）；
5. 登记响应带 preview + supported_langs（+health）——#239 结果面板的数据源；profiles 行带
   voice_langs（人设卡「克隆声支持语种」）；
6. 路由级拒收 + 文案键 ZH/EN 齐全：send-voice 对「克隆回落的系统音」无 confirm 一律拒收；
   四类不可用原因各有 i18n 键。既有 voice_selection_gold / voice_placeholder_guard 由套件
   同跑不回归（本文件不复述其断言）。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from src.ai.tts_pipeline import (
    TTSPipeline, classify_voice_error, log_skip_voice, skip_voice_reason)

_ROOT = Path(__file__).resolve().parents[1]
_JA = "どこにいるの。ご飯は食べた？一緒に遊びに行こうよ。"
_ZH = "你好，今天天气不错，我们晚上一起吃饭吧。"


def _clone_cfg(tmp_path, **over):
    """STEVEN：克隆男声（avatar_clone 主链 · hub index_tts 钉住 → 能力表只含 zh/en），兜底 edge。"""
    cfg = {
        "enabled": True,
        "backend": "avatar_clone",
        "fallback_on_error": True,
        "fallback_backend": "edge_tts",
        "fallback_voice": "zh-CN-XiaoxiaoNeural",
        "out_dir": str(tmp_path / "out"),
        "tts_cache": {"enabled": False},
        "persona_id": "steven",
        "persona_gender": "male",
        "avatar_voice": {"enabled": True,
                         "hub_fish": {"enabled": True, "tts_engine": "index_tts",
                                      "persona_allowlist": ["steven"]}},
        "voice_profile": {
            "enabled": True, "owner_consent": True, "backend": "avatar_clone",
            "speaker_id": "steven",
        },
    }
    cfg.update(over)
    return cfg


def _deny_clone(monkeypatch, calls: list | None = None):
    async def fake_avatar(self, rv, out, t0, **kw):
        if calls is not None:
            calls.append("avatar")
        return None          # = avatar_clone_unreachable

    monkeypatch.setattr(TTSPipeline, "_try_avatar_clone", fake_avatar)


def _spy_run_backend(monkeypatch, seen):
    async def fake_run_backend(self, rv, text, out, voice, backend, fmt,
                               timeout_sec, *, spec=None):
        seen.append({"voice": voice, "backend": backend})
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"ID3" + b"\x00" * 600)
        rv.ok = True
        rv.audio_path = str(out)
        return None

    monkeypatch.setattr(TTSPipeline, "_run_backend", fake_run_backend)


# ── 1 阻断：ja × STEVEN → 无音频、无 edge、带 system_voice ──────────────────────

async def test_case1_ja_x_steven_blocked_no_audio_no_edge(tmp_path, monkeypatch):
    _deny_clone(monkeypatch)
    seen: list = []
    _spy_run_backend(monkeypatch, seen)
    rv = await TTSPipeline(_clone_cfg(tmp_path)).synthesize(_JA)
    assert rv.ok is False
    assert rv.error == "clone_unavailable:clone_lang_unsupported:ja"
    assert rv.extra.get("clone_unavailable") == "clone_lang_unsupported:ja"
    assert rv.extra.get("degrade_to_text") is True
    assert rv.extra.get("fallback_blocked") == "clone_unavailable"
    assert not rv.audio_path, "阻断不得留下任何音频产物"
    assert seen == [], f"未二次确认不得调用 edge：{seen}"
    # 可二次确认的系统音：同语种（ja）同性别（male）→ Keita，而非 Nanami/晓晓
    assert rv.extra.get("system_voice") == "ja-JP-KeitaNeural"
    assert classify_voice_error(rv.error) == "clone_unavailable"
    assert skip_voice_reason(rv) == "clone_unavailable"


# ── 2 二次确认：confirm_system_voice=True → edge Keita + confirmed 标 ────────────

async def test_case2_confirm_system_voice_yields_edge_keita(tmp_path, monkeypatch):
    _deny_clone(monkeypatch)
    seen: list = []
    _spy_run_backend(monkeypatch, seen)
    rv = await TTSPipeline(_clone_cfg(tmp_path)).synthesize(_JA, confirm_system_voice=True)
    assert rv.ok is True
    assert rv.provider == "edge_tts"
    assert rv.voice == "ja-JP-KeitaNeural"
    assert seen == [{"voice": "ja-JP-KeitaNeural", "backend": "edge_tts"}]
    assert rv.extra.get("fallback_from") == "avatar_clone"      # 如实标「克隆回落」，路由据此核 confirm
    assert rv.extra.get("system_voice_confirmed") is True
    assert "clone_unavailable" not in rv.extra


# ── 3 自动链：阻断 → 改发文字 + skip_voice 日志；冷启动重试恰一次 ─────────────────

async def test_case3_auto_chain_degrades_to_text_and_logs_skip_voice(tmp_path, monkeypatch, caplog):
    _deny_clone(monkeypatch)
    seen: list = []
    _spy_run_backend(monkeypatch, seen)
    rv = await TTSPipeline(_clone_cfg(tmp_path)).synthesize(_JA)   # 自动链不带 confirm
    with caplog.at_level(logging.INFO):
        reason = log_skip_voice(rv, conv="telegram:acc1:chat9")
    assert reason == "clone_unavailable"
    assert seen == []
    line = next((r.getMessage() for r in caplog.records if "skip_voice" in r.getMessage()), "")
    assert "[tts] skip_voice reason=clone_unavailable:clone_lang_unsupported:ja" in line
    assert "conv=telegram:acc1:chat9" in line and "改发文字" in line
    # 正常结果不是阻断：不打日志、返回空
    class _Ok:  # noqa: D401
        ok = True
        error = ""
        extra: dict = {}
    assert log_skip_voice(_Ok()) == ""

    # 两条自动链都接了：传 tts_lang=plan_tts_lang(...) 且 not ok 时走 log_skip_voice（改发文字）
    for rel in ("src/client/sender.py", "src/inbox/voice_autosend.py"):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert "log_skip_voice(" in src, rel
        assert "plan_tts_lang(" in src, rel
        assert "confirm_system_voice=True" not in src, f"{rel}：自动链永不带系统音确认位"


async def test_case3b_cold_start_retries_primary_exactly_once(tmp_path, monkeypatch):
    """minicpm 探到「模型载入中」→ 自动链等一拍（受 cold_start_retry_sec）重试主链**一次**。"""
    calls: list = []

    async def fake_minicpm(self, rv, out, t0, **kw):
        calls.append("minicpm")
        rv.extra["clone_cold_start"] = "minicpm_clone_loading"
        return None

    monkeypatch.setattr(TTSPipeline, "_try_minicpm_clone", fake_minicpm)
    seen: list = []
    _spy_run_backend(monkeypatch, seen)
    cfg = _clone_cfg(
        tmp_path, backend="minicpm_clone",
        avatar_voice={"enabled": True, "cold_start_retry_sec": 5},
        voice_profile={"enabled": True, "owner_consent": True, "backend": "minicpm_clone",
                       "speaker_id": "steven", "lang_route_cleared": "zh"},
    )
    tts = TTSPipeline(cfg)
    # 等待秒数照 cfg 算（受总预算裁剪，≥3s 才等）；为免测试真睡，把管线里的 sleep 钉成瞬回并记录
    slept: list = []

    async def fake_sleep(sec, *_a, **_k):
        slept.append(sec)

    import src.ai.tts_pipeline as _mod
    monkeypatch.setattr(_mod.asyncio, "sleep", fake_sleep)
    rv = await tts.synthesize(_ZH, interactive=False)
    assert calls.count("minicpm") == 2, f"冷启动只重试一次（主链应恰调 2 次）：{calls}"
    assert slept and 3.0 <= slept[0] <= 5.0, slept
    assert rv.extra.get("clone_cold_start_waited")
    assert rv.ok is False and rv.extra.get("degrade_to_text") is True
    assert seen == [], "冷启动仍不可用 → 改发文字，不出系统音"


# ── 4 语种一致性：文本 zh × 计划 tts_lang=yue → lang_mismatch ────────────────────

async def test_case4_text_zh_x_plan_yue_skips_voice_lang_mismatch(tmp_path, monkeypatch, caplog):
    calls: list = []
    _deny_clone(monkeypatch, calls)
    seen: list = []
    _spy_run_backend(monkeypatch, seen)
    with caplog.at_level(logging.INFO):
        rv = await TTSPipeline(_clone_cfg(tmp_path)).synthesize(_ZH, tts_lang="yue")
    assert rv.ok is False
    assert rv.error == "lang_mismatch"
    assert rv.extra.get("skip_voice") == "lang_mismatch"
    assert rv.extra.get("degrade_to_text") is True
    assert rv.extra.get("text_lang") == "zh"
    assert rv.extra.get("tts_lang") == "yue"
    assert rv.extra.get("lang_source") == "conv_lang_plan"
    assert calls == [] and seen == [], "不一致 → 根本不进合成"
    assert classify_voice_error(rv.error) == "lang_mismatch"
    assert skip_voice_reason(rv) == "lang_mismatch"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("[tts] skip_voice reason=lang_mismatch" in m and "text_lang=zh" in m
               and "tts_lang=yue" in m for m in msgs), msgs
    # 一致（计划 zh × 文本 zh）→ 正常进合成，合成生效行带三段语种字段
    caplog.clear()
    with caplog.at_level(logging.INFO):
        rv2 = await TTSPipeline(_clone_cfg(tmp_path)).synthesize(_ZH, tts_lang="zh", confirm_system_voice=True)
    assert rv2.ok is True
    eff = next((m for m in (r.getMessage() for r in caplog.records) if "合成生效" in m), "")
    assert re.search(r"text_lang=\S+ tts_lang=\S+ source=\S+", eff), eff


# ── 5 登记响应：preview + supported_langs（+health）；profiles 行带 voice_langs ───────

def test_case5_enroll_response_carries_preview_and_supported_langs(tmp_path, monkeypatch):
    from src.web.routes.voice_routes import clone_supported_langs, enroll_result_extras
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    cfg = {"avatar_voice": {"enabled": True,
                            "hub_fish": {"enabled": True, "tts_engine": "index_tts",
                                         "persona_allowlist": ["steven"]}}}
    ref = tmp_path / "voice_samples" / "steven.wav"
    ref.parent.mkdir(parents=True)
    ref.write_bytes(b"RIFF" + b"\x00" * 64)
    vp = {"backend": "avatar_clone", "reference_audio_path": str(ref), "reference_text": "你好"}
    ex = enroll_result_extras(cfg, "steven", vp, "avatar_clone")
    assert isinstance(ex["supported_langs"], list) and "zh" in ex["supported_langs"]
    assert "ja" not in ex["supported_langs"]
    assert ex["preview"]["kind"] == "tts_test"
    assert ex["preview"]["tts_test"]["persona_id"] == "steven"
    assert ex["preview"]["reference"] == {"file": "steven.wav", "has_text": True}
    assert set(ex["health"]) == {"probe_spawned", "last"}
    assert clone_supported_langs(cfg, "avatar_clone", "steven") == ex["supported_langs"]
    # 能力未知 → 空列表（前端不判不冤枉），绝不抛
    assert enroll_result_extras({}, "x", None, "")["supported_langs"] == []

    src = (_ROOT / "src/web/routes/voice_routes.py").read_text(encoding="utf-8")
    body = src[src.index('@app.post("/api/voice/enroll")'):]
    body = body[:body.index("\n    @app.", 10)]
    oks = [m.start() for m in re.finditer(r'return \{"ok": True', body)]
    assert len(oks) == 3, "enroll 三条成功出口（avatar / lan / qwen）"
    for i in oks:
        assert "enroll_result_extras(" in body[i:i + 700] or "**_extras" in body[i:i + 400], body[i:i + 300]
    prof = src[src.index('@app.get("/api/voice/profiles")'):]
    prof = prof[:prof.index("\n    @app.", 10)]
    assert '"voice_langs": (clone_supported_langs(' in prof

    # 前端：cp-voice 把 supported_langs/preview/health 随 cp-voice-enrolled 广播；失败也广播；
    # 人设页有可停留结果面板（× 才关）+ 支持语种行
    for tree in ("shared/copilot/components/cp-voice.js",
                 "desktop/renderer/shared/copilot/components/cp-voice.js"):
        js = (_ROOT / tree).read_text(encoding="utf-8")
        assert "supported_langs: Array.isArray(d.supported_langs)" in js, tree
        assert 'new CustomEvent("cp-voice-enroll-failed"' in js, tree
    html = (_ROOT / "src/web/templates/personas.html").read_text(encoding="utf-8")
    assert 'id="pe-vc-result"' in html and 'id="pe-vc-langs"' in html
    assert "function peVcRenderResult(" in html and "peVcResultHide()" in html
    assert "addEventListener('cp-voice-enroll-failed'" in html
    assert "_peVcToast(" in html.split("function peVcAfterEnrolled")[1], "无面板元素时才回落旧 toast"


# ── 6 路由级拒收 + 文案键齐全；前端红条两键 ──────────────────────────────────────

def test_case6_send_voice_rejects_unconfirmed_fallback_and_i18n_complete():
    route = (_ROOT / "src/web/routes/unified_inbox_send_routes.py").read_text(encoding="utf-8")
    assert "confirm_system_voice" in route
    assert "clone_unavailable:system_voice_unconfirmed" in route, "复用/补标旁路也要拒收"
    assert "system_voice_available" in route
    # 缺省（未确认）决不带 confirm 去合成：只有 body 显式 1/true 才置位
    assert re.search(r"_confirm_sys\s*=.*confirm_system_voice", route)

    vr = (_ROOT / "src/web/routes/voice_routes.py").read_text(encoding="utf-8")
    assert "def clone_unavailable_message(" in vr
    from src.web.i18n_packs import errors_stock as es
    zh = getattr(es, "ZH", None) or getattr(es, "ZH_CN", None) or {}
    en = getattr(es, "EN", None) or {}
    if not zh or not en:   # 包的容器名不同：退化为源码扫描
        txt = (_ROOT / "src/web/i18n_packs/errors_stock.py").read_text(encoding="utf-8")
        for k in ("err.voice.clone_unavailable_lang", "err.voice.clone_unavailable_quota",
                  "err.voice.clone_unavailable_offline", "err.voice.clone_unavailable_garbled",
                  "err.voice.clone_unavailable", "err.voice.lang_mismatch_skip"):
            assert txt.count(f'"{k}"') >= 2, f"{k} 需 ZH+EN 双份"
    else:
        for k in ("err.voice.clone_unavailable_lang", "err.voice.clone_unavailable_quota",
                  "err.voice.clone_unavailable_offline", "err.voice.clone_unavailable_garbled",
                  "err.voice.clone_unavailable", "err.voice.lang_mismatch_skip"):
            assert k in zh and k in en, k

    # 右栏红条：两键都在，且系统音键只在 system_voice_available 时渲染；缺省请求不带 confirm
    for tree in ("shared/copilot/components/cp-voice.js",
                 "desktop/renderer/shared/copilot/components/cp-voice.js"):
        js = (_ROOT / tree).read_text(encoding="utf-8")
        assert 'data-act="send-as-text"' in js and 'data-act="sys-voice"' in js, tree
        assert "blk.system_voice_available" in js, tree
        assert "confirm_system_voice: confirmSys ? 1 : undefined" in js, tree
        assert "confirm_system_voice: sysConfirm ? 1 : undefined" in js, tree
        assert "_sysVoiceSend()" in js and "confirm(this._t(\"cp.voice.sys_voice_confirm\"))" in js, tree
        # Q-22 B：克隆声念不了目标语 → 生成按钮置灰带原因
        assert "_syncGenGate(" in js and "data-lang-blocked" in js, tree
    i18n = (_ROOT / "shared/copilot/i18n/cp-i18n.js").read_text(encoding="utf-8")
    for k in ("cp.voice.send_as_text_btn", "cp.voice.sys_voice_btn", "cp.voice.sys_voice_confirm",
              "cp.voice.blocked_generic", "cp.voice.blocked_lang_mismatch",
              "cp.voice.eff_langs", "cp.voice.gen_lang_blocked_t"):
        assert i18n.count(f'"{k}"') >= 2, f"{k} 需 ZH+EN"
    # 红线：管线里不新增任何缺省回落音（fallback_voice 缺省仍只在既有一处读取）
    pipe = (_ROOT / "src/ai/tts_pipeline.py").read_text(encoding="utf-8")
    assert pipe.count("[tts] fallback") == 4
    assert "confirm_system_voice: bool = False" in pipe
