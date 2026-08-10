"""Unified voice / TTS API routes.

Endpoints
---------
POST /api/voice/tts-test
    Generate a TTS preview for a given persona + text.
    Body: {text, persona_id?, format?, background?}
    Returns: {ok, url, duration_sec, provider, voice, error?}
    background=true → validation/quota still run synchronously, the synthesis
    itself moves to a background job; returns {ok, job_id} immediately.

GET  /api/voice/tts-test-jobs/{job_id}
    Poll a background preview job: running/done/error ("done" carries the same
    payload as the sync success response; unknown or expired job → not_found).

GET  /api/voice/tts-test/{filename}
    Serve the generated preview audio file (short-lived).

GET  /api/voice/effective-config
    Read-only snapshot of the effective voice config for a platform/persona
    (channel-center preview panel).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

_TTS_PREVIEW_DIR = Path("tmp_tts_preview")
_TTS_PREVIEW_TTL_SEC = 600  # files older than 10 min are cleaned up


_TTS_PREVIEW_PREFIXES = ("ttspreview-", "line-tts-")
# P9-D: per-prefix TTL — approval queue files survive longer than quick UI tests
_TTS_PREVIEW_TTL_BY_PREFIX: Dict[str, float] = {
    "ttspreview-": _TTS_PREVIEW_TTL_SEC,   # 10 min: UI test previews
    "line-tts-":   7200.0,                  # 2 h: LINE approval queue
}


def _cleanup_old_previews() -> None:
    """Remove preview files older than per-prefix TTL (best-effort)."""
    try:
        now = time.time()
        for f in _TTS_PREVIEW_DIR.iterdir():
            if not f.is_file():
                continue
            ttl = next(
                (v for k, v in _TTS_PREVIEW_TTL_BY_PREFIX.items() if f.name.startswith(k)),
                None,
            )
            if ttl is None:
                continue
            try:
                st = f.stat()
                # P11-B: 任何 < 512B 的孤儿文件（截断/静默失败）直接清除，不受 TTL 约束
                if st.st_size < 512 or st.st_mtime < now - ttl:
                    f.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


# ── tts-test 后台 job 模式（channel-center「后台试听」）──────────────────────
# 模块级注册表 {job_id: {"status","result","error","ts","task"}}：
# - status: running|done|error（英文枚举，前端直接消费）；
# - result: 与同步成功响应完全同构的 dict（status=done 时非空）；
# - error: 同步失败路径会返回的 error 文案（status=error 时使用）；
# - ts: 提交时刻；条目超过 _TTS_JOB_TTL_SEC 由提交/轮询路径顺手清理（表有界，
#   无后台清扫线程）；
# - task: asyncio.Task 强引用（create_task 对任务仅弱引用，防 GC 吞任务）。
_TTS_JOBS: Dict[str, Dict[str, Any]] = {}
_TTS_JOB_TTL_SEC = 900.0   # 结果保留 15 min，过期轮询按 not_found 处理
_TTS_JOB_MAX_RUNNING = 3   # 并发在跑上限：第 4 个提交立即拒绝（busy）


def _cleanup_tts_jobs() -> None:
    """清掉 ts 超过 ``_TTS_JOB_TTL_SEC`` 的 job 条目（best-effort）。

    单事件循环内的纯 dict 操作，无需加锁；条目被清后仍在跑的后台任务
    写的是自己持有的 entry 引用，不会 KeyError。
    """
    try:
        now = time.time()
        for jid in list(_TTS_JOBS):
            entry = _TTS_JOBS.get(jid) or {}
            if now - float(entry.get("ts") or 0.0) > _TTS_JOB_TTL_SEC:
                _TTS_JOBS.pop(jid, None)
    except Exception:
        pass


# 「立即渲染」后台进程句柄（防重复拉起；渲染幂等，丢句柄无害）
_PRERENDER_PROC: Dict[str, Any] = {"proc": None}

# enroll 后一次性音色体检子进程句柄（同上模式；体检幂等=只追加 jsonl 一行）
_VQ_PROBE_PROC: Dict[str, Any] = {"proc": None}

# 音色体检最新结果缓存（30s TTL；jsonl 尾读虽轻，profiles 是高频接口）
_VQ_CACHE: Dict[str, Any] = {"until": 0.0, "map": {}}


def _voice_quality_latest() -> Dict[str, Dict[str, Any]]:
    """每人设最新音色体检行 {persona: {label, score, date}}（best-effort）。

    数据源＝夜间探针 + enroll 即时体检共同追加的
    ``<数据根>/logs/voice_similarity.jsonl``（与 avatar-status 卡同一文件；
    ``AITR_DATA_DIR`` 优先=测试隔离，服务进程 CWD=实例数据根两者等价）。
    A/B 对照的固定噪声行（prosody=off）不算——那是基线数据不代表生产路径。
    读不到/坏行 → 空表（下拉不出徽标，绝不因体检面阻塞选音色）。
    """
    now = time.monotonic()
    if now < float(_VQ_CACHE.get("until") or 0.0):
        return dict(_VQ_CACHE.get("map") or {})
    latest: Dict[str, Dict[str, Any]] = {}
    try:
        base = str(os.environ.get("AITR_DATA_DIR") or "").strip()
        p = (Path(base) if base else Path(".")) / "logs" / "voice_similarity.jsonl"
        if p.is_file():
            for line in p.read_text(
                    encoding="utf-8", errors="replace").splitlines()[-120:]:
                try:
                    row = json.loads(line)
                    if row.get("prosody") == "off":
                        continue
                    pid = str(row.get("persona") or "").strip()
                    if not pid:
                        continue
                    latest[pid] = {
                        "label": str(row.get("label") or ""),
                        "score": row.get("score"),
                        "date": str(row.get("date") or ""),
                    }
                except Exception:
                    continue
    except Exception:
        latest = {}
    _VQ_CACHE["map"] = latest
    _VQ_CACHE["until"] = now + 30.0
    return dict(latest)


def _spawn_voice_quality_probe(persona_id: str) -> bool:
    """enroll 成功后拉起一次性音色体检（fire-and-forget 子进程，防重复）。

    修「录了但不像要等夜间探针（最长 24h）才可见」：登记即对该人设合成
    探针句 → campplus 声纹比对 → 追加 voice_similarity.jsonl → profiles 的
    quality 徽标几分钟内点亮。``--data-root`` 钉当前实例（服务进程 CWD=
    实例数据根的启动契约）；非 TTS 节点机/评分器缺失时子进程自行 SKIP
    exit 0 零副作用；``--prosody-ab off``＝交互场景只要生产路径一行，省一半
    GPU。已有一轮在跑（如夜间任务窗口）→ 不重复拉。
    """
    import subprocess
    import sys as _sys

    pid = str(persona_id or "").strip()
    if not pid:
        return False
    proc = _VQ_PROBE_PROC.get("proc")
    if proc is not None and proc.poll() is None:
        return False
    try:
        _VQ_PROBE_PROC["proc"] = subprocess.Popen(
            [_sys.executable, "-m", "scripts.voice_similarity_probe",
             "--persona", pid, "--prosody-ab", "off",
             "--data-root", str(Path.cwd())],
            cwd=str(Path(__file__).resolve().parents[3]),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as ex:  # noqa: BLE001
        logger.warning("[voice/enroll] 体检子进程拉起失败: %s", ex)
        return False


def _spawn_prerender_render() -> bool:
    """后台拉起一轮 --all-personas 增量预渲染（fire-and-forget，防重复）。

    子进程方式复用 CLI 全套自愈（7858 冷载 ensure_ready/批级重试），
    避免 web 层直接依赖 scripts 渲染逻辑。已有一轮在跑 → 不重复拉。
    """
    import subprocess
    import sys as _sys

    proc = _PRERENDER_PROC.get("proc")
    if proc is not None and proc.poll() is None:
        return False
    try:
        _PRERENDER_PROC["proc"] = subprocess.Popen(
            [_sys.executable, "-m", "scripts.avatar_prerender", "--all-personas"],
            cwd=str(Path(__file__).resolve().parents[3]),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as ex:  # noqa: BLE001
        logger.warning("[prerender-lines] 后台渲染拉起失败: %s", ex)
        return False


def register_voice_routes(app, api_auth, config_manager=None):
    """Register /api/voice/* endpoints on *app*."""

    def _classify_err(err: Any) -> str:
        """管线错误 → 坐席可行动分类（薄封装；词表单源在 tts_pipeline）。"""
        try:
            from src.ai.tts_pipeline import classify_voice_error
            return classify_voice_error(str(err or ""))
        except Exception:
            return ""

    def _voice_fail_message(request: Request, reason: str) -> str:
        """分类 → 本地化人话（告诉坐席下一步做什么，而非只给错误码）。

        与 ``_run_tts_preview`` 刻意分离：preview 核心段不得引用 request
        （后台 job 在请求生命周期外跑），i18n 只能在持有 request 的路由层做。
        """
        if reason == "hub_source_down":
            return tr(request, "err.voice.hub_source_down")
        if reason == "profile_not_ready":
            return tr(request, "err.voice.profile_not_ready")
        return ""

    async def _run_tts_preview(body: Dict[str, Any], text: str) -> Dict[str, Any]:
        """tts-test 核心段：resolve voice_cfg → override → fast 档 → 合成 → 整形响应。

        同步路径 ``return await`` 本函数（响应与历史行为逐字节一致）；background
        路径由 ``asyncio.create_task`` 包住、把结果/异常写进模块级 ``_TTS_JOBS``。
        入参在提交时已从 request 取好（body 为已解析 dict、text 已校验），本函数
        不得引用 request——后台任务在请求生命周期结束后仍在跑。
        返回值＝POST /api/voice/tts-test 的响应 dict（成功/失败两种形状都在此组装）。
        """
        persona_id: Optional[str] = body.get("persona_id") or None
        fmt_override: Optional[str] = body.get("format") or None
        # Optional: caller passes current UI settings to override saved config
        cfg_override: Optional[Dict[str, Any]] = body.get("voice_cfg_override") or None
        # 试听=发送 契约（2026-08-05 P0）：前端把当前会话上下文（chat_key/
        # platform/account_id）一并传来，试听与 send-voice 走**同一组解析入参**
        # ——修「试听走全局回落、发送走会话绑定，两条链解析出不同音色」的分叉
        # （坐席试听过了、点发送却失败/换声，是本次事故的放大器）。
        # 不传（渠道中心/旧标签页）＝旧行为，零破坏。
        chat_key: str = str(body.get("chat_key") or "").strip()
        platform: str = str(body.get("platform") or "telegram").strip().lower()
        account_id: str = str(body.get("account_id") or "").strip()

        # Resolve voice config
        raw_cfg: Dict[str, Any] = {}
        if config_manager and hasattr(config_manager, "config"):
            raw_cfg = config_manager.config or {}

        # 账号默认人设（与 A 线 sender / send-voice 同口径）：chat 未绑定人设时
        # 回落到该账号的人设而非域默认——「默认音色」试听出来的才是真发出去的声音。
        account_persona_id = ""
        if account_id:
            try:
                from src.ai.persona_voice import resolve_account_persona_id
                account_persona_id = resolve_account_persona_id(
                    raw_cfg, platform, account_id) or ""
            except Exception:
                account_persona_id = ""

        try:
            from src.ai.persona_voice import resolve_effective_voice_context
            voice_ctx = resolve_effective_voice_context(
                raw_cfg, persona_id=persona_id,
                chat_key=chat_key or None,
                account_persona_id=account_persona_id or None,
                contact_key=chat_key or None,
                platform=platform, account_id=account_id or None,
                text=text)
            voice_cfg = voice_ctx.get("voice_cfg") or {}
        except Exception as ex:
            logger.warning("[voice/tts-test] resolve_voice_cfg failed: %s", ex)
            voice_cfg = {}
            voice_ctx = {"persona_id": persona_id or "", "persona_source": "error", "emotion": None}

        # Apply caller override (allows previewing unsaved UI settings)
        if isinstance(cfg_override, dict):
            voice_cfg.update({k: v for k, v in cfg_override.items() if v not in (None, "")})
            logger.debug("[voice/tts-test] override keys: %s", list(cfg_override))

        voice_cfg["enabled"] = True
        if fmt_override:
            voice_cfg["format"] = fmt_override.strip().lower()

        # fast=true → force the always-hot edge_tts backend so the preview answers
        # within seconds even when the production clone chain (avatar_clone → hub
        # → 7852) is cold or queued. ``requested_backend`` keeps the pre-swap
        # backend name so the UI can still show what a real send would use.
        fast = bool(body.get("fast"))
        requested_backend = str(voice_cfg.get("backend") or "")
        if fast:
            voice_cfg["backend"] = "edge_tts"
            voice_cfg.pop("voice_profile", None)
            _fast_voice = str(voice_cfg.get("voice") or "")
            if not _fast_voice or "Neural" not in _fast_voice:
                # current voice is not an edge voice id → safe zh default
                voice_cfg["voice"] = "zh-CN-XiaoxiaoNeural"

        # Redirect output to preview dir
        _TTS_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        uid = uuid.uuid4().hex[:10]
        suffix = voice_cfg.get("format", "mp3")
        preview_path = _TTS_PREVIEW_DIR / f"ttspreview-{uid}.{suffix}"
        voice_cfg["out_dir"] = str(_TTS_PREVIEW_DIR)

        _t0 = time.monotonic()
        try:
            from src.ai.tts_pipeline import TTSPipeline
            tts = TTSPipeline(voice_cfg)
            # Override out path so we control filename
            import asyncio as _aio
            # total_budget_sec：链内各级（LLM 口语化/hub/本机克隆）按剩余预算收口，
            # 在 45s 内要么出货要么带着具体失败级返回——外层 wait_for 只兜极端。
            # （2026-07-28 复盘：旧链各级预算之和 ≫ 50s 外闸 → 被掐死时 TimeoutError
            # str() 为空，日志只剩一行「TTS error: 」，无从排查。）
            # fast mode gets a tighter budget: edge_tts is cheap and the whole
            # point is answering before the frontend gives up waiting.
            # 试听=发送契约（P0 2026-08-10）：send-voice 已改「原文直念」（手打
            # 文字不进口语化改写链，实录：LLM 档把问句改写成对它的回答后念出），
            # 试听与发送**全同参**（pre_colloquialized + interactive）——坐席听到
            # 的字与发出的字永远一致，试听产物经「所听即所发」复用时零分叉；
            # interactive 同时把 hub 候选数封顶（人在等）+ 豁免开场词去重剥词。
            result = await _aio.wait_for(
                tts.synthesize(
                    text, timeout_sec=(15.0 if fast else 45.0),
                    emotion=voice_ctx.get("emotion"),
                    pre_colloquialized=True, interactive=True,
                    total_budget_sec=(15.0 if fast else 45.0)),
                timeout=(20.0 if fast else 50.0),
            )
        except Exception as ex:
            _exs = f"{type(ex).__name__}: {ex}".rstrip(": ")
            logger.error(
                "[voice/tts-test] TTS error (persona=%s text_len=%d elapsed=%.1fs): %s",
                voice_ctx.get("persona_id") or persona_id or "-", len(text),
                time.monotonic() - _t0, _exs)
            return {"ok": False, "error": _exs[:200], "fast": fast,
                    "reason": _classify_err(_exs)}

        if not result.ok:
            logger.warning(
                "[voice/tts-test] synth not ok (persona=%s provider=%s elapsed=%.1fs): %s",
                voice_ctx.get("persona_id") or persona_id or "-",
                result.provider, time.monotonic() - _t0, result.error)
            return {"ok": False, "error": result.error, "fast": fast,
                    "reason": _classify_err(result.error)}

        # Rename to our deterministic preview path
        try:
            Path(result.audio_path).rename(preview_path)
        except Exception:
            preview_path = Path(result.audio_path)

        # Async cleanup of old files (non-blocking)
        try:
            _cleanup_old_previews()
        except Exception:
            pass

        # 「所听即所发」复用登记（P1 2026-08-05）：sidecar 记文本指纹+音色键+元数据，
        # send-voice 带回 filename 且校验通过时直接复用本音频（省一次合成/额度，
        # 且客户听到的与坐席试听的逐字节一致）。best-effort：失败只丢复用资格。
        try:
            from src.integrations.shared.tts_preview import record_preview_meta
            record_preview_meta(
                preview_path.name, text=text,
                persona_key=str(persona_id or ""),
                meta={
                    "resolved_persona_id": voice_ctx.get("persona_id") or "",
                    "persona_source": voice_ctx.get("persona_source") or "",
                    "provider": result.provider,
                    "voice": result.voice,
                    "emotion": (
                        getattr(voice_ctx.get("emotion"), "emotion", "")
                        if voice_ctx.get("emotion") else ""
                    ),
                    "fallback_from": (result.extra or {}).get("fallback_from", ""),
                    "duration_sec": result.duration_sec,
                    "format": result.format,
                    "fast": bool(fast),
                })
        except Exception:
            logger.debug("[voice/tts-test] 复用 sidecar 登记失败（忽略）", exc_info=True)

        file_url = f"/api/voice/tts-test/{preview_path.name}"
        return {
            "ok": True,
            "url": file_url,
            "audio_url": file_url,
            "filename": preview_path.name,
            "duration_sec": result.duration_sec,
            "provider": result.provider,
            "voice": result.voice,
            "format": result.format,
            "fast": fast,
            "requested_backend": requested_backend,
            "bytes": preview_path.stat().st_size if preview_path.is_file() else 0,
            "voice_meta": {
                "persona_id": voice_ctx.get("persona_id") or "",
                "persona_source": voice_ctx.get("persona_source") or "",
                "provider": result.provider,
                "voice": result.voice,
                "emotion": (
                    getattr(voice_ctx.get("emotion"), "emotion", "")
                    if voice_ctx.get("emotion") else ""
                ),
                "fallback_from": (result.extra or {}).get("fallback_from", ""),
            },
        }

    @app.post("/api/voice/tts-test")
    async def api_voice_tts_test(request: Request, _=Depends(api_auth)):
        """Generate a TTS audio preview (sync by default; background job opt-in).

        Request body (JSON):
            text        — text to synthesise (required)
            persona_id  — persona ID for voice config lookup (optional)
            format      — output format override: mp3|ogg (optional)
            background  — true → validate + quota-check now, run the synthesis
                          in a background job and return {ok, job_id} at once;
                          poll GET /api/voice/tts-test-jobs/{job_id} (optional)
        """
        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body")

        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text is required")
        if len(text) > 400:
            raise HTTPException(400, "text too long (max 400 chars)")

        # P0-4/C3：字符额度用尽且 licensing.enforce 开 → 402 + i18n（试听同样耗额度）
        # 后台模式下配额检查同样在**提交时**同步执行（提交即检查，任务里不再重复）。
        from src.licensing.quota_store import check_license_quota
        if not check_license_quota()["allowed"]:
            raise HTTPException(402, tr(request, "err.lic.chars_exhausted"))

        # 提交路径顺手清理过期 job（与轮询路径同口径；纯内存操作，不影响响应）
        _cleanup_tts_jobs()

        if bool(body.get("background")):
            running = sum(1 for e in _TTS_JOBS.values()
                          if e.get("status") == "running")
            if running >= _TTS_JOB_MAX_RUNNING:
                return {"ok": False, "error": "busy"}

            job_id = uuid.uuid4().hex
            entry: Dict[str, Any] = {"status": "running", "result": None,
                                     "error": None, "ts": time.time()}
            _TTS_JOBS[job_id] = entry

            async def _job_runner() -> None:
                # 任务内部整体兜住：后台没有外层错误处理，任何异常都写进 job 条目
                try:
                    rv = await _run_tts_preview(body, text)
                except Exception as ex:  # noqa: BLE001
                    entry["status"] = "error"
                    entry["error"] = (f"{type(ex).__name__}: {ex}".rstrip(": "))[:200]
                    return
                if rv.get("ok"):
                    entry["status"] = "done"
                    entry["result"] = rv
                else:
                    # 失败文案与同步路径完全一致（rv 的 error 字段原样透出）；
                    # reason=可行动分类，轮询路由据此补本地化 message
                    entry["status"] = "error"
                    entry["error"] = str(rv.get("error") or "")
                    entry["reason"] = str(rv.get("reason") or "")

            # create_task 对任务仅弱引用 → Task 存进条目防 GC（任务亦持 entry 引用）
            entry["task"] = asyncio.create_task(
                _job_runner(), name=f"tts-preview-job-{job_id}")
            return {"ok": True, "job_id": job_id}

        rv = await _run_tts_preview(body, text)
        # 失败分类 → 本地化人话（additive：error 原样保留，老前端零感知）
        if not rv.get("ok") and rv.get("reason"):
            _msg = _voice_fail_message(request, str(rv.get("reason")))
            if _msg:
                rv["message"] = _msg
        return rv

    @app.get("/api/voice/tts-test-jobs/{job_id}")
    async def api_voice_tts_test_job(job_id: str, request: Request,
                                     _=Depends(api_auth)):
        """Poll a background TTS preview job (see POST /api/voice/tts-test).

        running → {ok, status: "running", age_sec}
        done    → {ok, status: "done", result: <same dict as the sync success>}
        error   → {ok, status: "error", error: <same text as the sync failure>}
        unknown or expired job_id → {ok: false, error: "not_found"}
        """
        _cleanup_tts_jobs()
        entry = _TTS_JOBS.get(job_id)
        if entry is None:
            return {"ok": False, "error": "not_found"}
        status = str(entry.get("status") or "")
        if status == "running":
            age = max(0.0, time.time() - float(entry.get("ts") or 0.0))
            return {"ok": True, "status": "running", "age_sec": round(age, 1)}
        if status == "done":
            return {"ok": True, "status": "done", "result": entry.get("result")}
        out = {"ok": True, "status": "error",
               "error": str(entry.get("error") or "")}
        # 可行动分类 → 本地化 message（additive；老前端只读 error 不受影响）
        _reason = str(entry.get("reason") or "")
        if _reason:
            out["reason"] = _reason
            _msg = _voice_fail_message(request, _reason)
            if _msg:
                out["message"] = _msg
        return out

    @app.get("/api/voice/effective-config")
    async def api_voice_effective_config(request: Request, platform: str = "telegram",
                                         persona_id: str = "", chat_key: str = "",
                                         account_id: str = "", _=Depends(api_auth)):
        """Read-only effective voice config snapshot (channel-center preview panel
        + 坐席音色状态条).

        Resolves the same voice context as a real send (persona layers merged)
        and reports the backend/voice that would actually be used, plus the raw
        per-channel backend straight from config. Never raises: any resolver
        error returns ``{ok: false}`` so the frontend can silently hide the panel.

        2026-08-05 P1：可选 ``chat_key``/``account_id``（不传=旧行为）——传入后
        与 send-voice 同一组解析入参（含账号人设回落），「状态条上显示的」与
        「点发送后实际用的」同源。响应增 ``is_clone/ready/hub_strict/hub_risk``
        四键：hub_risk 仅在该解析结果处于 hub 严格辖区时探测（TCP 60s 负缓存 +
        outage 台账事后证据，单边确定性——报了必真，不报不代表健康）。
        """
        try:
            raw_cfg: Dict[str, Any] = {}
            if config_manager and hasattr(config_manager, "config"):
                raw_cfg = config_manager.config or {}

            from src.ai.persona_voice import (
                resolve_account_persona_id, resolve_effective_voice_context)
            _acc_pid = ""
            if account_id:
                try:
                    _acc_pid = resolve_account_persona_id(
                        raw_cfg, platform, account_id) or ""
                except Exception:
                    _acc_pid = ""
            ctx = resolve_effective_voice_context(
                raw_cfg, persona_id=persona_id or None,
                chat_key=chat_key or None,
                account_persona_id=_acc_pid or None,
                contact_key=chat_key or None,
                account_id=account_id or None,
                platform=platform, text="")
            voice_cfg = ctx.get("voice_cfg") or {}

            # Raw per-channel backend (config as-written, before persona layering).
            _chan_path = {
                "telegram": ("telegram", "voice_reply"),
                "whatsapp": ("whatsapp_rpa", "voice_output"),
                "messenger": ("messenger_rpa", "voice_output"),
                "line": ("line_rpa", "voice_output"),
            }.get(str(platform or "").strip().lower())
            channel_backend = ""
            if _chan_path:
                _sect = raw_cfg.get(_chan_path[0])
                _sub = _sect.get(_chan_path[1]) if isinstance(_sect, dict) else None
                if isinstance(_sub, dict):
                    channel_backend = str(_sub.get("backend") or "")

            # basename only — never leak server directory layout to the UI
            vp = voice_cfg.get("voice_profile")
            ref = str(vp.get("reference_audio_path") or "").strip() if isinstance(vp, dict) else ""

            # 就绪度（与 /api/voice/profiles 的 _describe 同语义：克隆类后端须
            # 授权+参考音齐备；非克隆恒就绪）
            _clone_backends = {"voice_clone_command", "coqui_http",
                               "voice_clone_lan", "avatar_clone", "minicpm_clone"}
            _vp_on = bool(isinstance(vp, dict) and vp.get("enabled"))
            _eff_backend = str(
                ((vp.get("backend") if _vp_on else None) if isinstance(vp, dict) else None)
                or voice_cfg.get("backend") or "edge_tts").lower()
            is_clone = _vp_on and _eff_backend in _clone_backends
            ready = True
            if is_clone:
                ready = bool(vp.get("owner_consent")) and bool(ref)

            # hub 严格辖区风险预告（仅命中辖区才探测，健康路径零开销）：
            # unreachable=TCP 确定不可达（必拒发）；recent_failures=台账里 hub 失败
            # 比最近一次成功更新（hub 活着但音色档 404 类的事后证据）。
            from src.ai.tts_pipeline import hub_strict_scope, probe_hub_reachable
            _av_cfg = raw_cfg.get("avatar_voice") or {}
            hub_strict = hub_strict_scope(_av_cfg, ctx.get("persona_id"))
            hub_risk = ""
            if hub_strict:
                if not probe_hub_reachable(_av_cfg):
                    hub_risk = "unreachable"
                else:
                    try:
                        from src.ai.voice_outage import get_voice_outage
                        _snap = get_voice_outage().outage_snapshot()
                        _hub_fail = any(
                            "hub_voice_source_unavailable" in str(k)
                            for k in (_snap.get("fail_reasons") or {}))
                        if _hub_fail and float(_snap.get("last_fail_ts") or 0.0) > \
                                float(_snap.get("last_ok_ts") or 0.0):
                            hub_risk = "recent_failures"
                    except Exception:
                        hub_risk = ""
            return {
                "ok": True,
                "platform": platform,
                "persona_id": ctx.get("persona_id") or "",
                "persona_source": ctx.get("persona_source") or "",
                "backend": str(voice_cfg.get("backend") or ""),
                "voice": str(voice_cfg.get("voice") or ""),
                "is_clone": is_clone,
                "ready": ready,
                "hub_strict": hub_strict,
                "hub_risk": hub_risk,
                "channel_backend": channel_backend,
                "reference_audio": os.path.basename(ref) if ref else "",
            }
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": str(ex)[:200]}

    @app.get("/api/voice/profiles")
    async def api_voice_profiles(request: Request, _=Depends(api_auth)):
        """列出可用「语音音色」供收件箱按人设选择：全局默认 + 各人设的克隆/音色。

        返回：{ok, default:{backend,voice,is_clone,ready}, profiles:[{persona_id,name,backend,voice,is_clone,ready}]}
        - is_clone：voice_profile 启用且后端为克隆类（见 clone_backends 集合）
        - ready：克隆音色但缺 owner_consent/参考音频时为 False（前端可标灰/提示）
        """
        raw_cfg: Dict[str, Any] = {}
        if config_manager and hasattr(config_manager, "config"):
            raw_cfg = config_manager.config or {}
        from src.ai.persona_voice import resolve_voice_cfg

        # 2026-08-05 修：avatar_clone（AvatarHub 7852，enroll 主路产物）与
        # minicpm_clone 此前不在集合里 → 生产主力克隆音色被标成 is_clone=False
        # → ready 恒 True、⚠/置灰从不出现——缺授权/缺参考音的音色在下拉里看着
        # 正常，点发送才撞 voice_profile_requires_owner_consent 硬失败。
        # 合成链对这两个后端**确实强制** owner_consent+参考音（tts_pipeline
        # _try_avatar_clone / _try_minicpm），此处只是让下拉的就绪灯与合成行为
        # 一致（voice_enroll 各 build_*_voice_profile 的 docstring 本就承诺此契约）。
        clone_backends = {"voice_clone_command", "coqui_http", "voice_clone_lan",
                          "avatar_clone", "minicpm_clone"}

        def _describe(cfg: Dict[str, Any]) -> Dict[str, Any]:
            vp = cfg.get("voice_profile") if isinstance(cfg.get("voice_profile"), dict) else {}
            vp_on = bool(vp.get("enabled"))
            backend = str((vp.get("backend") if vp_on else None) or cfg.get("backend") or "edge_tts").lower()
            voice = str((vp.get("speaker_id") if vp_on else None) or cfg.get("voice") or "")
            is_clone = vp_on and backend in clone_backends
            ready = True
            if is_clone:
                ref = str(vp.get("reference_audio_path") or "").strip()
                ready = bool(vp.get("owner_consent")) and bool(ref)
            return {"backend": backend, "voice": voice, "is_clone": is_clone, "ready": ready}

        default_desc = _describe(resolve_voice_cfg(None, raw_cfg))
        profiles = []
        # 音色体检徽标（P3 2026-08-05）：夜间探针 + enroll 即时体检的最新声纹
        # 相似度分级进下拉（warn=正常带下方 / critical=疑似换错参考音/文件坏）。
        # 刻意**不置灰**——体检差的音色仍能出声，是否弃用由人决定（与 P1 状态条
        # 同哲学：显性化而非代决定）。
        vq = _voice_quality_latest()
        try:
            from src.utils.persona_manager import PersonaManager
            pm = PersonaManager.get_instance()
            for s in pm.list_profiles_summary():
                if not s.get("has_voice"):
                    continue
                pid = s.get("id")
                desc = _describe(resolve_voice_cfg(pid, raw_cfg))
                q = vq.get(str(pid)) or {}
                profiles.append({
                    "persona_id": pid, "name": s.get("name") or pid, **desc,
                    "quality": str(q.get("label") or ""),
                    "quality_score": q.get("score"),
                })
        except Exception as ex:  # noqa: BLE001
            logger.warning("[voice/profiles] persona enumerate failed: %s", ex)
        return {"ok": True, "default": default_desc, "profiles": profiles}

    @app.get("/api/voice/persona-audit")
    async def api_voice_persona_audit(request: Request, _=Depends(api_auth)):
        """语音「体检」：对每个人设跑**与真实发送同一决策函数**的 voice context 解析，
        暴露实际会用到的 后端/音色/情绪基调/克隆就绪度，并标记是否误继承了全局克隆
        参考音（clone_bleed）。ops 面板用它一眼看清「谁有独立声音、谁还在裸默认」。

        返回：{ok, default, personas:[{persona_id,name,backend,voice,format,emotion,
               is_clone,ready,clone_bleed,source}], summary:{total,voiced,clone_ready,bleed}}
        """
        raw_cfg: Dict[str, Any] = {}
        if config_manager and hasattr(config_manager, "config"):
            raw_cfg = config_manager.config or {}
        from src.ai.persona_voice import resolve_effective_voice_context

        # 与 /api/voice/profiles 同一集合语义（avatar_clone/minicpm_clone 为生产
        # 主力克隆后端；漏掉它们审计的 clone_ready/bleed 就是对着空气数数）。
        clone_backends = {"voice_clone_command", "coqui_http", "voice_clone_lan",
                          "avatar_clone", "minicpm_clone"}
        global_ref = ""
        try:
            _gvp = (((raw_cfg.get("telegram") or {}).get("voice_reply") or {})
                    .get("voice_profile") or {})
            global_ref = str(_gvp.get("reference_audio_path") or "").strip()
        except Exception:
            global_ref = ""

        def _row(persona_id, name, ctx):
            vc = ctx.get("voice_cfg") or {}
            vp = vc.get("voice_profile") if isinstance(vc.get("voice_profile"), dict) else {}
            backend = str(vc.get("backend") or "edge_tts").lower()
            voice = str(vc.get("voice") or vp.get("speaker_id") or "")
            emo = ctx.get("emotion")
            is_clone = backend in clone_backends
            ready = True
            if is_clone:
                ref = str(vp.get("reference_audio_path") or "").strip()
                ready = bool(vp.get("owner_consent")) and bool(ref)
            ref_used = str(vp.get("reference_audio_path") or "").strip()
            # A persona that is *not* the global default but reuses the global
            # clone's reference audio = misconfiguration (every persona sounds the same).
            clone_bleed = bool(global_ref) and ref_used == global_ref and bool(persona_id)
            return {
                "persona_id": persona_id or "",
                "name": name or persona_id or "",
                "backend": backend,
                "voice": voice,
                "format": str(vc.get("format") or ""),
                "emotion": getattr(emo, "emotion", "") if emo else "",
                "is_clone": is_clone,
                "ready": ready,
                "clone_bleed": clone_bleed,
                "source": ctx.get("persona_source") or "",
            }

        default_row = _row(
            None, "（全局默认）", resolve_effective_voice_context(raw_cfg))
        personas = []
        try:
            from src.utils.persona_manager import PersonaManager
            pm = PersonaManager.get_instance()
            for s in pm.list_profiles_summary():
                pid = s.get("id")
                ctx = resolve_effective_voice_context(raw_cfg, persona_id=pid)
                personas.append(_row(pid, s.get("name"), ctx))
        except Exception as ex:  # noqa: BLE001
            logger.warning("[voice/persona-audit] enumerate failed: %s", ex)
        return {
            "ok": True,
            "default": default_row,
            "personas": personas,
            "summary": {
                "total": len(personas),
                "voiced": sum(1 for p in personas if p["voice"]),
                "clone_ready": sum(1 for p in personas if p["is_clone"] and p["ready"]),
                "bleed": sum(1 for p in personas if p["clone_bleed"]),
            },
        }

    def _dashscope_creds() -> tuple:
        """从 config 取 DashScope 凭据（messenger_rpa.voice_output 优先）；空则由 enroll 走 env/secret。"""
        raw_cfg = (getattr(config_manager, "config", None) or {}) if config_manager else {}
        vo = (raw_cfg.get("messenger_rpa") or {}).get("voice_output") or {}
        return str(vo.get("dashscope_api_key") or "").strip(), str(vo.get("dashscope_region") or "").strip()

    def _audit(request: Request, action: str, detail: str) -> None:
        """声纹运营留痕（登记/解绑/改绑/删除）。app.state.audit_store 缺失时静默跳过。"""
        try:
            st = getattr(request.app.state, "audit_store", None)
            if not st:
                return
            try:
                actor = request.session.get("username", "api")
            except Exception:
                actor = "api"
            st.log(actor, action, detail)
        except Exception:
            logger.debug("[voice] audit log failed", exc_info=True)

    @app.get("/api/voice/cloned")
    async def api_voice_cloned(request: Request, _=Depends(api_auth)):
        """列出 DashScope 已登记的克隆声纹（无 key 时优雅返回 reason，不报错）。"""
        api_key, region = _dashscope_creds()
        try:
            from src.ai.voice_enroll import list_cloned_voices
            d = await asyncio.to_thread(
                list_cloned_voices, api_key=api_key, region=region or "intl")
        except RuntimeError as ex:
            if "DASHSCOPE_API_KEY" in str(ex):
                return {"ok": False, "reason": "no_api_key",
                        "message": "未配置 DASHSCOPE_API_KEY（messenger_rpa.voice_output.dashscope_api_key 或环境变量）"}
            return {"ok": False, "reason": "list_failed", "message": str(ex)[:300]}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "reason": "list_failed", "message": str(ex)[:300]}
        voices = (d.get("output") or {}).get("voice_list") or (d.get("output") or {}).get("voices") or []
        return {"ok": True, "voices": voices, "raw": d}

    @app.post("/api/voice/enroll")
    async def api_voice_enroll(request: Request, _=Depends(api_auth)):
        """声纹自助登记闭环：参考音频 → 策展/质检 → 登记克隆声纹 →
        写回指定人设的 voice_profile → 收件箱音色下拉即可选。

        multipart 两种来源（互斥，file 优先）：
          - file       — 坐席上传参考音频（旧口径，向后兼容）
          - media_ref  — 会话语音消息的 /static/protocol_media/... 引用
                         （P0「从消息一键导入」：服务端直读归档原音，零下载往返）
        其余字段：persona_id + preferred_name + region? + language_type? +
          reference_text?（逐字稿；消息导入时前端预填 ASR 转写）+
          owner_consent?（授权确认：media_ref 必须显式为真；上传缺省视为已确认）+
          force?（跳过质检门的主管逃生门）+ platform/conversation_id/message_id?（溯源）。
        登记前统一过「策展 + 质检门」（转单声道 WAV / 自动裁最佳片段 / 过短拒绝），
        质检回显随响应 quality 字段返回。无 DASHSCOPE_API_KEY 时优雅返回。
        """
        form = await request.form()
        upload = form.get("file")
        media_ref = str(form.get("media_ref") or "").strip()
        persona_id = str(form.get("persona_id") or "").strip()
        preferred_name = str(form.get("preferred_name") or "").strip()
        region_in = str(form.get("region") or "").strip()
        language_type = str(form.get("language_type") or "Japanese").strip() or "Japanese"
        reference_text = str(form.get("reference_text") or "").strip()
        force_quality = str(form.get("force") or "").strip().lower() in ("1", "true", "yes", "on")
        _consent_raw = form.get("owner_consent")
        consent: Optional[bool] = None
        if _consent_raw is not None:
            consent = str(_consent_raw).strip().lower() in ("1", "true", "yes", "on")

        has_upload = upload is not None and bool(getattr(upload, "filename", ""))
        use_media = bool(media_ref) and not has_upload
        if not has_upload and not media_ref:
            raise HTTPException(400, tr(request, "err.voice.ref_file_required"))
        if not persona_id:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="persona_id"))
        if not preferred_name:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="preferred_name"))
        # 授权闸门：消息导入必须显式确认（客户语音≠自动授权）；上传路径显式否认
        # 同拒；上传且未携带字段＝旧客户端，按既有语义视为已确认（向后兼容）。
        if use_media and consent is not True:
            raise HTTPException(400, tr(request, "err.voice.consent_required"))
        if consent is False:
            raise HTTPException(400, tr(request, "err.voice.consent_required"))
        owner_consent = True if consent is None else consent

        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        persona = pm.get_persona_by_id(persona_id)
        if persona is None:
            raise HTTPException(404, tr(request, "err.voice.persona_not_found", persona_id=persona_id))

        if has_upload:
            data = await upload.read()
            src_suffix = (os.path.splitext(upload.filename)[1] or ".wav").lower()
        else:
            # media_ref → 本地归档文件：只认 protocol_media 白名单目录 + 容纳检查防穿越
            from src.integrations.protocol_bridge import (
                protocol_media_root, static_media_ref_to_path)
            _mp = static_media_ref_to_path(media_ref)
            if not _mp:
                raise HTTPException(400, tr(request, "err.voice.media_ref_invalid"))
            _rp = Path(_mp).resolve()
            try:
                _contained = _rp.is_relative_to(protocol_media_root().resolve())
            except Exception:
                _contained = False
            if not _contained:
                raise HTTPException(400, tr(request, "err.voice.media_ref_invalid"))
            if not _rp.is_file():
                raise HTTPException(404, tr(request, "err.voice.media_ref_not_found"))
            data = _rp.read_bytes()
            src_suffix = (_rp.suffix or ".ogg").lower()
        if not data:
            raise HTTPException(400, tr(request, "err.inbox.empty_file"))
        if len(data) > 15 * 1024 * 1024:
            raise HTTPException(413, tr(request, "err.voice.ref_too_large"))

        api_key, cfg_region = _dashscope_creds()
        region = region_in or cfg_region or "intl"

        # 策展 + 质检门：统一转单声道 WAV、自动裁最佳片段，落 voice_samples/<safe>.wav
        #（独立于 protocol_media 生命周期——媒体目录将来清理不影响已登记音色）
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", preferred_name)[:40] or "voice"
        samples = Path("voice_samples")
        from src.ai.voice_enroll import build_source_ref, prepare_reference_audio
        try:
            prep = await asyncio.to_thread(
                prepare_reference_audio, data, src_suffix, str(samples), safe,
                force=force_quality)
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(500, tr(request, "err.voice.ref_save_failed", err=ex))
        quality = {k: prep.get(k) for k in (
            "level", "issues", "tips", "curated", "degraded", "duration_sec")}
        if not prep.get("ok"):
            code = str(prep.get("reject_code") or "rejected")
            msg = (tr(request, "err.voice.ref_too_short",
                      sec=prep.get("duration_sec") or 0)
                   if code == "too_short"
                   else tr(request, "err.voice.ref_not_audio"))
            return {"ok": False, "reason": f"ref_{code}", "message": msg,
                    "quality": quality}
        audio_path = Path(prep["audio_path"]).resolve()

        # 音色溯源（voice_profile.source_ref + 审计行）：从哪条消息/谁导入的
        try:
            actor = request.session.get("username", "api")
        except Exception:
            actor = "api"
        source_ref = build_source_ref(
            kind=("inbox_message" if use_media else "upload"),
            media_ref=(media_ref if use_media else ""),
            platform=str(form.get("platform") or ""),
            conversation_id=str(form.get("conversation_id") or ""),
            message_id=str(form.get("message_id") or ""),
            imported_by=str(actor or "api"))

        # 逐字稿 sidecar 前置统一写（后端无关；avatar 分支缺稿时仍会 STT 自动补）
        if reference_text:
            try:
                audio_path.with_suffix(".txt").write_text(
                    reference_text, encoding="utf-8")
            except Exception:
                pass

        raw_full_cfg: Dict[str, Any] = {}
        if config_manager and hasattr(config_manager, "config"):
            raw_full_cfg = config_manager.config or {}

        # ── AvatarHub 最优先：本机 7852 在线则零样本登记（音质最好且零云端配额）──
        # 附加两个自动化：① 缺逐字稿时用 AvatarHub STT 转写参考音生成（提升相似度）；
        # ② 登记即 register_spk 预热（后台线程，首句延迟显著下降）。
        av_cfg = raw_full_cfg.get("avatar_voice") or {}
        if isinstance(av_cfg, dict) and av_cfg.get("enabled"):
            try:
                from src.ai.avatar_voice import AvatarVoiceClient
                av_client = AvatarVoiceClient(av_cfg)
                av_up = await asyncio.to_thread(av_client.health_ok)
            except Exception as ex:  # noqa: BLE001
                logger.debug("[voice/enroll] AvatarHub health probe error: %s", ex)
                av_up = False
            if av_up:
                av_ref_text = reference_text
                if not av_ref_text:
                    try:
                        from src.ai.avatar_voice import convert_to_wav_16k_mono
                        wav16 = await asyncio.to_thread(
                            convert_to_wav_16k_mono, str(audio_path))
                        stt_src = Path(wav16) if wav16 else audio_path
                        av_ref_text = await asyncio.to_thread(
                            av_client.stt, stt_src.read_bytes()) or ""
                        if wav16:
                            Path(wav16).unlink(missing_ok=True)
                    except Exception:  # noqa: BLE001
                        av_ref_text = ""
                if av_ref_text:
                    try:  # sidecar：find_reference_text 自动发现，换后端也不丢
                        audio_path.with_suffix(".txt").write_text(
                            av_ref_text, encoding="utf-8")
                    except Exception:
                        pass
                from src.ai.voice_enroll import build_avatar_voice_profile
                vp_av = build_avatar_voice_profile(
                    reference_audio_path=str(audio_path), speaker_id=safe,
                    reference_text=av_ref_text,
                    owner_consent=owner_consent, source_ref=source_ref)
                new_persona = dict(persona)
                new_persona["voice_profile"] = vp_av
                try:
                    pm.upsert_profile(persona_id, new_persona)
                    cm = getattr(request.app.state, "config_manager", None) or config_manager
                    pm.persist_profiles(cm)
                except Exception as ex:  # noqa: BLE001
                    logger.warning("[voice/enroll] AvatarHub persist failed: %s", ex)
                    return {"ok": False, "reason": "persist_failed", "message": str(ex)[:300]}
                # 预热（fire-and-forget：失败只影响首句延迟）
                try:
                    import threading as _th
                    from src.ai.avatar_voice import load_reference_b64

                    def _warm() -> None:
                        try:
                            av_client.register_spk(load_reference_b64(str(audio_path)))
                        except Exception:
                            pass

                    _th.Thread(target=_warm, name="avatar-enroll-warm", daemon=True).start()
                except Exception:
                    pass
                _audit(request, "voice_enroll",
                       f"persona={persona_id} mode=avatar_clone name={preferred_name}"
                       f" src={source_ref.get('kind')} consent={owner_consent}"
                       f" level={quality.get('level')}")
                # 录入即体检（P3）：合成探针句 → 声纹比对 → jsonl → 下拉徽标
                # 几分钟内点亮；体检结果缓存失效让 profiles 尽快看到新行
                if _spawn_voice_quality_probe(persona_id):
                    _VQ_CACHE["until"] = 0.0
                return {"ok": True, "mode": "avatar_clone", "persona_id": persona_id,
                        "reference_audio_path": str(audio_path),
                        "reference_text": av_ref_text,
                        "avatar_base_url": av_client.base_url,
                        "quality": quality}
            logger.info("[voice/enroll] avatar_voice(7852) 不可用 → 回落 LAN/云端登记")

        # ── 局域网次优先：LAN 克隆主机在线则零样本登记（不烧云端配额）──
        lan_cfg = raw_full_cfg.get("voice_clone_lan") or {}
        if isinstance(lan_cfg, dict) and lan_cfg.get("enabled"):
            try:
                from src.ai.voice_clone_client import VoiceCloneClient
                lan = VoiceCloneClient(lan_cfg)
                lan_up = await asyncio.to_thread(lan.health_ok)
            except Exception as ex:  # noqa: BLE001
                logger.debug("[voice/enroll] LAN health probe error: %s", ex)
                lan_up = False
            if lan_up:
                from src.ai.voice_enroll import build_lan_voice_profile
                from src.utils.persona_manager import PersonaManager as _PM
                vp_lan = build_lan_voice_profile(
                    reference_audio_path=str(audio_path), speaker_id=safe,
                    base_url=lan.base_url, language=str(lan_cfg.get("language") or "zh"),
                    reference_text=reference_text,
                    clone_path=str(lan_cfg.get("clone_path") or "/v1/tts/clone"),
                    owner_consent=owner_consent, source_ref=source_ref)
                new_persona = dict(persona)
                new_persona["voice_profile"] = vp_lan
                try:
                    _pm = _PM.get_instance()
                    _pm.upsert_profile(persona_id, new_persona)
                    cm = getattr(request.app.state, "config_manager", None) or config_manager
                    _pm.persist_profiles(cm)
                except Exception as ex:  # noqa: BLE001
                    logger.warning("[voice/enroll] LAN persist failed: %s", ex)
                    return {"ok": False, "reason": "persist_failed", "message": str(ex)[:300]}
                _audit(request, "voice_enroll",
                       f"persona={persona_id} mode=lan_zeroshot name={preferred_name}"
                       f" src={source_ref.get('kind')} consent={owner_consent}"
                       f" level={quality.get('level')}")
                return {"ok": True, "mode": "lan_zeroshot", "persona_id": persona_id,
                        "reference_audio_path": str(audio_path), "lan_base_url": lan.base_url,
                        "quality": quality}
            logger.info("[voice/enroll] voice_clone_lan 不可用 → 回落云端 Qwen 登记")

        from src.ai.voice_enroll import (
            build_qwen_voice_profile, enroll_voice, qwen_profile_json_dict)
        try:
            res = await asyncio.to_thread(
                enroll_voice, audio_path=str(audio_path),
                preferred_name=preferred_name, api_key=api_key, region=region)
        except RuntimeError as ex:
            if "DASHSCOPE_API_KEY" in str(ex):
                return {"ok": False, "reason": "no_api_key",
                        "message": "未配置 DASHSCOPE_API_KEY（messenger_rpa.voice_output.dashscope_api_key 或环境变量）"}
            return {"ok": False, "reason": "enroll_failed", "message": str(ex)[:300]}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "reason": "enroll_failed", "message": str(ex)[:300]}

        voice = res["voice"]
        target_model = res.get("target_model")
        # 写 qwen_tts_wrapper 消费的 voice-profile JSON
        json_path = (samples / f"qwen_{safe}.json").resolve()
        try:
            json_path.write_text(
                json.dumps(qwen_profile_json_dict(
                    voice=voice, target_model=target_model,
                    reference_audio_path=str(audio_path), region=region,
                    preferred_name=preferred_name), ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            logger.debug("[voice/enroll] 写 voice-profile JSON 失败", exc_info=True)

        # 写回人设 voice_profile 并持久化（重启后续用）
        vp = build_qwen_voice_profile(
            voice=voice, reference_audio_path=str(audio_path),
            voice_profile_json_path=str(json_path), speaker_id=safe,
            region=region, target_model=target_model, language_type=language_type,
            owner_consent=owner_consent, source_ref=source_ref)
        new_persona = dict(persona)
        new_persona["voice_profile"] = vp
        try:
            pm.upsert_profile(persona_id, new_persona)
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_profiles(cm)
        except Exception as ex:  # noqa: BLE001
            logger.warning("[voice/enroll] persist persona failed: %s", ex)
            return {"ok": False, "reason": "persist_failed", "message": str(ex)[:300],
                    "voice": voice}
        _audit(request, "voice_enroll",
               f"persona={persona_id} voice={voice} name={preferred_name}"
               f" src={source_ref.get('kind')} consent={owner_consent}"
               f" level={quality.get('level')}")
        return {"ok": True, "voice": voice, "persona_id": persona_id,
                "reference_audio_path": str(audio_path), "quality": quality}

    @app.delete("/api/voice/profiles/{persona_id}")
    async def api_voice_unbind(persona_id: str, request: Request,
                               purge_cloud: bool = False, _=Depends(api_auth)):
        """解绑：移除某人设的 voice_profile（音色回落默认）。

        默认仅断开绑定、保留云端声纹（可随时改绑复用）。
        purge_cloud=1 时额外永久删除云端 Qwen 声纹（不可恢复，best-effort）。
        """
        from src.ai.voice_enroll import without_voice_profile
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        persona = pm.get_persona_by_id(persona_id)
        if persona is None:
            raise HTTPException(404, tr(request, "err.voice.persona_not_found", persona_id=persona_id))
        vp = persona.get("voice_profile")
        if not isinstance(vp, dict):
            return {"ok": True, "persona_id": persona_id, "changed": False}
        voice = str(vp.get("voice") or "").strip()
        purged = None
        if purge_cloud and voice:
            api_key, region = _dashscope_creds()
            try:
                from src.ai.voice_enroll import delete_cloned_voice
                await asyncio.to_thread(
                    delete_cloned_voice, voice=voice, api_key=api_key, region=region or "intl")
                purged = True
            except RuntimeError as ex:
                purged = False
                if "DASHSCOPE_API_KEY" in str(ex):
                    logger.info("[voice/unbind] purge skipped: no api key")
            except Exception as ex:  # noqa: BLE001
                purged = False
                logger.warning("[voice/unbind] cloud purge failed: %s", ex)
        try:
            pm.upsert_profile(persona_id, without_voice_profile(persona))
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_profiles(cm)
        except Exception as ex:  # noqa: BLE001
            logger.warning("[voice/unbind] persist failed: %s", ex)
            return {"ok": False, "reason": "persist_failed", "message": str(ex)[:300]}
        _audit(request, "voice_unbind",
               f"persona={persona_id} voice={voice or '-'} purge_cloud={bool(purge_cloud)} purged={purged}")
        return {"ok": True, "persona_id": persona_id, "changed": True, "purged": purged}

    @app.post("/api/voice/rebind")
    async def api_voice_rebind(request: Request, _=Depends(api_auth)):
        """改绑/复用：把已登记音色从 from_persona_id 复制到 to_persona_id（免重复上传/登记）。"""
        body = await request.json()
        src_id = str((body or {}).get("from_persona_id") or "").strip()
        dst_id = str((body or {}).get("to_persona_id") or "").strip()
        if not src_id or not dst_id:
            raise HTTPException(400, tr(request, "err.voice.from_to_persona_required"))
        if src_id == dst_id:
            raise HTTPException(400, tr(request, "err.voice.src_dst_same"))
        from src.ai.voice_enroll import copy_voice_profile
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        src = pm.get_persona_by_id(src_id)
        dst = pm.get_persona_by_id(dst_id)
        if src is None:
            raise HTTPException(404, tr(request, "err.voice.src_persona_not_found", src_id=src_id))
        if dst is None:
            raise HTTPException(404, tr(request, "err.voice.dst_persona_not_found", dst_id=dst_id))
        if not isinstance(src.get("voice_profile"), dict):
            return {"ok": False, "reason": "source_has_no_voice",
                    "message": f"源人设 {src_id} 未绑定音色"}
        try:
            pm.upsert_profile(dst_id, copy_voice_profile(src, dst))
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_profiles(cm)
        except Exception as ex:  # noqa: BLE001
            logger.warning("[voice/rebind] persist failed: %s", ex)
            return {"ok": False, "reason": "persist_failed", "message": str(ex)[:300]}
        _audit(request, "voice_rebind", f"from={src_id} to={dst_id}")
        return {"ok": True, "from_persona_id": src_id, "to_persona_id": dst_id}

    def _local_persona_voice_rows():
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        rows = []
        for s in pm.list_profiles_summary():
            pid = s.get("id")
            if not pid:
                continue
            p = pm.get_persona_by_id(pid)
            if not isinstance(p, dict):
                continue
            rows.append({"persona_id": pid, "name": s.get("name") or pid, "persona": p})
        return rows

    @app.get("/api/voice/reconcile")
    async def api_voice_reconcile(request: Request, _=Depends(api_auth)):
        """声纹资产对账：云端 list × 本地人设 voice_profile 交叉比对。

        返回 orphans（可回收）/ shared（多人设共用）/ linked / dangling（本地引用但云端未见）。
        """
        from src.ai.voice_enroll import (
            collect_local_voice_refs, list_all_cloned_voices, reconcile_voice_assets)
        local_refs = collect_local_voice_refs(_local_persona_voice_rows())
        api_key, region = _dashscope_creds()
        try:
            cloud_entries = await asyncio.to_thread(
                list_all_cloned_voices, api_key=api_key, region=region or "intl")
        except RuntimeError as ex:
            if "DASHSCOPE_API_KEY" in str(ex):
                report = reconcile_voice_assets([], local_refs)
                return {
                    "ok": False, "reason": "no_api_key",
                    "message": "未配置 DASHSCOPE_API_KEY，仅返回本地引用侧对账",
                    "local_refs": local_refs, **report,
                }
            return {"ok": False, "reason": "list_failed", "message": str(ex)[:300],
                    "local_refs": local_refs}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "reason": "list_failed", "message": str(ex)[:300],
                    "local_refs": local_refs}
        report = reconcile_voice_assets(cloud_entries, local_refs)
        return {"ok": True, "cloud_entries": cloud_entries, "local_refs": local_refs, **report}

    @app.post("/api/voice/purge")
    async def api_voice_purge(request: Request, _=Depends(api_auth)):
        """永久删除单个云端声纹。有本地引用时需 force=true（前端二次确认）。"""
        body = await request.json()
        voice = str((body or {}).get("voice") or "").strip()
        force = bool((body or {}).get("force"))
        if not voice:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="voice"))
        from src.ai.voice_enroll import collect_local_voice_refs, delete_cloned_voice, purge_guard
        local_refs = collect_local_voice_refs(_local_persona_voice_rows())
        guard = purge_guard(voice, local_refs, force=force)
        if not guard.get("allowed"):
            return {"ok": False, **guard}
        api_key, region = _dashscope_creds()
        try:
            await asyncio.to_thread(
                delete_cloned_voice, voice=voice, api_key=api_key, region=region or "intl")
        except RuntimeError as ex:
            if "DASHSCOPE_API_KEY" in str(ex):
                return {"ok": False, "reason": "no_api_key",
                        "message": "未配置 DASHSCOPE_API_KEY"}
            return {"ok": False, "reason": "purge_failed", "message": str(ex)[:300]}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "reason": "purge_failed", "message": str(ex)[:300]}
        _audit(request, "voice_purge",
               f"voice={voice} force={force} ref_count={guard.get('ref_count', 0)}")
        return {"ok": True, "voice": voice, "purged": True, "ref_count": guard.get("ref_count", 0)}

    @app.post("/api/voice/purge-orphans")
    async def api_voice_purge_orphans(request: Request, _=Depends(api_auth)):
        """一键回收孤儿声纹：仅删除 ref_count=0 的云端声纹（安全，不碰仍被引用者）。"""
        from src.ai.voice_enroll import (
            collect_local_voice_refs, delete_cloned_voice,
            list_all_cloned_voices, reconcile_voice_assets)
        api_key, region = _dashscope_creds()
        local_refs = collect_local_voice_refs(_local_persona_voice_rows())
        try:
            cloud_entries = await asyncio.to_thread(
                list_all_cloned_voices, api_key=api_key, region=region or "intl")
        except RuntimeError as ex:
            if "DASHSCOPE_API_KEY" in str(ex):
                return {"ok": False, "reason": "no_api_key",
                        "message": "未配置 DASHSCOPE_API_KEY"}
            return {"ok": False, "reason": "list_failed", "message": str(ex)[:300]}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "reason": "list_failed", "message": str(ex)[:300]}
        report = reconcile_voice_assets(cloud_entries, local_refs)
        orphans = report.get("orphans") or []
        deleted, failed = [], []
        for item in orphans:
            vid = item.get("voice")
            if not vid:
                continue
            try:
                await asyncio.to_thread(
                    delete_cloned_voice, voice=vid, api_key=api_key, region=region or "intl")
                deleted.append(vid)
            except Exception as ex:  # noqa: BLE001
                failed.append({"voice": vid, "message": str(ex)[:200]})
        _audit(request, "voice_purge_orphans",
               f"deleted={len(deleted)} failed={len(failed)} voices={','.join(deleted[:20])}")
        return {
            "ok": True, "deleted": deleted, "failed": failed,
            "orphan_count": len(orphans), "summary": report.get("summary"),
        }

    def _serve_tts_file(filename: str):
        """Shared helper: validate + serve any TTS preview file."""
        if "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(400, "invalid filename")
        if not any(filename.startswith(p) for p in _TTS_PREVIEW_PREFIXES):
            raise HTTPException(404, "not found")
        fpath = _TTS_PREVIEW_DIR / filename
        if not fpath.is_file():
            raise HTTPException(404, "preview expired or not found")
        ext = fpath.suffix.lower().lstrip(".")
        mime_map = {"mp3": "audio/mpeg", "ogg": "audio/ogg",
                   "wav": "audio/wav", "opus": "audio/ogg"}
        return FileResponse(str(fpath), media_type=mime_map.get(ext, "application/octet-stream"))

    @app.get("/api/voice/tts-test/{filename}")
    async def api_voice_tts_file(filename: str, request: Request, _=Depends(api_auth)):
        """Serve a previously generated TTS preview file."""
        return _serve_tts_file(filename)

    @app.get("/api/voice/tts-file/{filename}")
    async def api_voice_tts_file_alt(filename: str, request: Request, _=Depends(api_auth)):
        """P8-D: Unified TTS file serving endpoint (line-tts-* + ttspreview-*)."""
        return _serve_tts_file(filename)

    @app.post("/api/admin/tts-cleanup")
    async def api_admin_tts_cleanup(request: Request, max_age_sec: int = 86400, _=Depends(api_auth)):
        """P16-C: Admin endpoint to trigger manual cleanup of old TTS preview files.

        Query params:
            max_age_sec — files older than this are deleted (default 24h)
        Returns:
            {ok: true, removed: N, max_age_sec: N}
        """
        from src.integrations.shared.tts_preview import cleanup_tts_previews
        removed = cleanup_tts_previews(max_age_sec=float(max_age_sec))
        return {"ok": True, "removed": removed, "max_age_sec": max_age_sec}

    @app.get("/api/admin/tts-stats")
    async def api_admin_tts_stats(request: Request, _=Depends(api_auth)):
        """P17-C: Return statistics about tmp_tts_preview directory.

        Returns:
            {ok: true, files: N, total_bytes: N, oldest_sec: N|null, newest_sec: N|null, by_prefix: {...}}
        """
        from src.integrations.shared.tts_preview import _TTS_DIR as tts_dir
        import time
        stats = {"files": 0, "total_bytes": 0, "by_prefix": {}, "oldest_sec": None, "newest_sec": None}
        now = time.time()
        prefixes = ("tts-", "line-tts-", "wa-tts-")
        try:
            if tts_dir.exists():
                for f in tts_dir.iterdir():
                    if not f.is_file():
                        continue
                    st = f.stat()
                    age = now - st.st_mtime
                    stats["files"] += 1
                    stats["total_bytes"] += st.st_size
                    if stats["oldest_sec"] is None or age > stats["oldest_sec"]:
                        stats["oldest_sec"] = age
                    if stats["newest_sec"] is None or age < stats["newest_sec"]:
                        stats["newest_sec"] = age
                    pref = next((p for p in prefixes if f.name.startswith(p)), "other")
                    stats["by_prefix"][pref] = stats["by_prefix"].get(pref, 0) + 1
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}
        return {"ok": True, **stats}

    # ── 本机 IndexTTS2 随主程序启停（coupled 模式）状态 + 开关 ─────────────────
    @app.get("/api/voice/local-tts/status")
    async def api_local_tts_status(request: Request, _=Depends(api_auth)):
        """Return local IndexTTS2 supervisor health + config snapshot."""
        sup = getattr(request.app.state, "local_tts_supervisor", None)
        cfg = (config_manager.config or {}) if config_manager else {}
        mcc = cfg.get("minicpm_clone") or {}
        la = mcc.get("local_autostart") or {}
        snap = sup.status_snapshot() if sup is not None else {}
        return {
            "ok": True,
            "configured_enabled": bool(la.get("enabled")),
            "stop_with_app": bool(la.get("stop_with_app", True)),
            "minicpm_base_url": str(mcc.get("base_url") or ""),
            **snap,
        }

    # ── 预渲染台词库（缺口→备货「一键入库」闭环）──────────────────────────────
    @app.get("/api/voice/prerender-lines")
    async def api_prerender_lines_list(request: Request, _=Depends(api_auth)):
        """台词库清单（_common + 各人设文件与台词数）。"""
        from src.ai.voice_prerender import DEFAULT_LINES_DIR, list_lines_files
        return {"ok": True, "lines_dir": DEFAULT_LINES_DIR,
                "files": list_lines_files()}

    @app.post("/api/voice/prerender-lines/add")
    async def api_prerender_lines_add(request: Request, _=Depends(api_auth)):
        """把一条缺口台词写进台词库（默认 _common=全人设），今晚计划任务自动渲染。

        Body: {text, target?="_common", render_now?=false}
        render_now=true 时后台立即拉起一轮 --all-personas 增量渲染（幂等跳过已有；
        走同一把 GPU 串行锁，与在线合成互不打架；7858 冷载由 CLI 自愈兜住）。
        """
        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body")
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="text"))
        target = str(body.get("target") or "_common").strip()

        from src.ai.voice_prerender import append_prerender_line
        rv = append_prerender_line(text, target=target)
        if not rv.get("ok"):
            raise HTTPException(400, f"append failed: {rv.get('reason')}")
        if rv.get("added"):
            _audit(request, "prerender_line_add",
                   f"target={rv['target']} text={rv['text'][:40]}")

        rendered_now = False
        if rv.get("added") and bool(body.get("render_now")):
            rendered_now = _spawn_prerender_render()
        return {**rv, "render_now": rendered_now,
                "note": ("rendering in background" if rendered_now
                         else "will render at nightly task (04:30)")}

    # ── AvatarHub 语音节点状态（7852/7858/STT 健康 + 用量观测）─────────────────
    @app.get("/api/voice/avatar-status")
    async def api_avatar_voice_status(request: Request, _=Depends(api_auth)):
        """AvatarHub 三端点健康（并行短超时探测）+ 合成/预渲染/STT 用量快照。"""
        cfg = (config_manager.config or {}) if config_manager else {}
        av_cfg = cfg.get("avatar_voice") or {}
        out: Dict[str, Any] = {
            "ok": True,
            "enabled": bool(av_cfg.get("enabled")),
            "base_url": str(av_cfg.get("base_url") or "http://127.0.0.1:7852"),
            "qwen_base_url": str(av_cfg.get("qwen_base_url") or "http://127.0.0.1:7858"),
            "dynamic_instruct": bool(av_cfg.get("dynamic_instruct", False)),
            "prerender_enabled": bool(
                (av_cfg.get("prerender") or {}).get("enabled", True)
                if isinstance(av_cfg.get("prerender"), dict) else True),
        }
        if av_cfg.get("enabled"):
            try:
                from src.ai.avatar_voice import AvatarVoiceClient
                probe_cfg = dict(av_cfg)
                probe_cfg["health_timeout_sec"] = 1.5
                client = AvatarVoiceClient(probe_cfg)
                h1, h2, h3 = await asyncio.gather(
                    asyncio.to_thread(client.health),
                    asyncio.to_thread(client.qwen_health),
                    asyncio.to_thread(client.stt_health),
                )
                out["health"] = {"cosyvoice": h1, "qwen3": h2, "stt": h3}
            except Exception as ex:  # noqa: BLE001
                out["health_error"] = str(ex)[:200]
        try:
            from src.ai.avatar_voice_stats import get_avatar_voice_stats
            out["stats"] = get_avatar_voice_stats().dump()
        except Exception:
            out["stats"] = {}
        # 语音出站断档台账（P2 2026-08-05）：A 线/B 线/坐席手动三链「已决定发语音」
        # 的滚动窗成败——ops 卡据此出「出站健康」行（attempts>0 且 ok=0 ＝断档红）。
        # 刻意不随 avatar_voice.enabled 闸门：台账覆盖全部语音后端，不只 AvatarHub。
        try:
            from src.ai.voice_outage import get_voice_outage
            out["outage"] = get_voice_outage().outage_snapshot()
        except Exception:
            out["outage"] = {}
        # 口语化 LLM 档健康：provider 分布（lan 端点/cloud/fallback 成败）+ 端点
        # 冷却 + 落盘缓存命中——「176 的逻辑有没有真的在被调用」看板可见（2026-08-01）
        try:
            from src.ai.voice_colloquial_llm import health_signal
            out["colloquial"] = health_signal()
        except Exception:
            out["colloquial"] = {}
        # 所听即所发（P1 2026-08-05）：试听产物复用观测——hit_rate 低时看 misses
        # 分布定位（expired 多→放宽 TTL；text_mismatch 多→坐席改稿没重生成）
        try:
            from src.integrations.shared.tts_preview import reuse_stats_snapshot
            out["preview_reuse"] = reuse_stats_snapshot()
        except Exception:
            out["preview_reuse"] = {}
        # 音色质量最新抽检（夜间探针 jsonl 尾部；声纹+韵律自然度按人设取最新一行）
        try:
            latest: Dict[str, Any] = {}
            vq = Path("logs/voice_similarity.jsonl")
            if vq.is_file():
                import json as _json
                for line in vq.read_text(
                        encoding="utf-8", errors="replace").splitlines()[-60:]:
                    try:
                        row = _json.loads(line)
                        # A/B 对照的固定噪声行（prosody=off）不进看板——那是
                        # 基线数据，不代表生产路径质量
                        if row.get("prosody") == "off":
                            continue
                        latest[str(row.get("persona"))] = {
                            "score": row.get("score"),
                            "naturalness": row.get("naturalness"),
                            "date": row.get("date"),
                        }
                    except Exception:
                        continue
            out["voice_quality"] = latest
        except Exception:
            out["voice_quality"] = {}
        # 参考音质量审计（scripts.reference_audio_audit 产物；Phase E 换声决策依据）
        try:
            ra = Path("logs/reference_audio_audit.json")
            if ra.is_file():
                import json as _json2
                data = _json2.loads(ra.read_text(encoding="utf-8", errors="replace"))
                out["reference_quality"] = {
                    "date": data.get("date"),
                    "worst": data.get("worst"),
                    "results": [
                        {
                            "personas": r.get("personas"),
                            "level": r.get("level"),
                            "issues": (r.get("issues") or [])[:4],
                            "has_sidecar": r.get("has_sidecar"),
                        }
                        for r in (data.get("results") or [])[:12]
                    ],
                }
            else:
                out["reference_quality"] = None
        except Exception:
            out["reference_quality"] = None
        # 预渲染备货量（人设数 / 台词条数 / 过期人设）——运营一眼看出备货是否生效
        try:
            from src.ai.voice_prerender import (
                DEFAULT_BASE_DIR,
                PRERENDER_DIRNAME,
                stock_is_stale,
            )
            base = Path(
                (av_cfg.get("prerender") or {}).get("base_dir")
                if isinstance(av_cfg.get("prerender"), dict)
                and (av_cfg.get("prerender") or {}).get("base_dir")
                else DEFAULT_BASE_DIR)
            personas = 0
            clips = 0
            stale: list = []
            # 人设当前参考音（比对备货指纹用）：PersonaManager 权威
            refs: Dict[str, str] = {}
            try:
                from src.utils.persona_manager import PersonaManager
                for p in PersonaManager.get_instance().get_profiles_by_tag("") or []:
                    if isinstance(p, dict):
                        vp = p.get("voice_profile") or {}
                        pid = str(p.get("id") or "").strip()
                        r = str(vp.get("reference_audio_path") or "").strip()
                        if pid and r:
                            refs[pid] = r
            except Exception:
                pass
            if base.is_dir():
                for pdir in base.iterdir():
                    d = pdir / PRERENDER_DIRNAME
                    if d.is_dir():
                        n = len(list(d.glob("*.ogg")))
                        if n:
                            personas += 1
                            clips += n
                            r = refs.get(pdir.name)
                            if r and stock_is_stale(
                                    pdir.name, r, base_dir=str(base)):
                                stale.append(pdir.name)
            out["prerender_inventory"] = {
                "personas": personas, "clips": clips,
                "stale": len(stale), "stale_personas": stale}
        except Exception:
            out["prerender_inventory"] = {"personas": 0, "clips": 0, "stale": 0}
        return out

    @app.post("/api/voice/local-tts/toggle")
    async def api_local_tts_toggle(request: Request, _=Depends(api_auth)):
        """Toggle ``minicpm_clone.local_autostart.enabled`` (overlay) + runtime start/stop.

        Body: ``{enabled: bool}``. Writes ``config.local.yaml`` and applies immediately
        when ``local_tts_supervisor`` is mounted on ``app.state``.
        """
        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body")
        if "enabled" not in body:
            raise HTTPException(400, "enabled is required")
        enabled = bool(body.get("enabled"))
        if config_manager is None or not hasattr(config_manager, "set_overlay_flag"):
            return {"ok": False, "error": "config overlay unavailable"}
        ok, msg = config_manager.set_overlay_flag(
            "minicpm_clone.local_autostart.enabled", enabled)
        sup = getattr(request.app.state, "local_tts_supervisor", None)
        runtime: Dict[str, Any] = {}
        snap: Dict[str, Any] = {}
        if sup is not None:
            sup.reload_from_config(config_manager.config.get("minicpm_clone") or {})
            runtime = await sup.apply_enabled(enabled)
            snap = sup.status_snapshot()
        return {
            "ok": ok,
            "message": msg,
            **runtime,
            **snap,
            "enabled": enabled,  # authoritative after toggle (snap may still reflect pre-stop state)
        }

    @app.get("/admin/tts-dashboard")
    async def admin_tts_dashboard(request: Request):
        """P20-B: TTS stats dashboard page."""
        try:
            api_auth(request)
        except HTTPException as exc:
            if exc.status_code == 401:
                return HTMLResponse(
                    '<!doctype html><meta http-equiv="refresh" content="0; url=/login">'
                    '<a href="/login">请先登录</a>',
                    status_code=200,
                )
            raise
        from fastapi.templating import Jinja2Templates
        templates = Jinja2Templates(directory="src/web/templates")
        return templates.TemplateResponse("admin_tts_dashboard.html", {"request": request})
