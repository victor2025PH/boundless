"""坐席「AI 生成图片」——后台 API（工具箱 cp-image 卡，2026-08-21）。

挂 ``/api/image/*``。把已有出图链（``SelfieProvider`` + ``comfy_infer.py`` +
``image_gate`` VLM 后验）接到坐席**手动出图**入口：坐席选人设/场景/引擎 → 生成 →
体检 → 预览 → 复用现有 ``/api/unified-inbox/send-media`` 发送 / 存入相册。

设计（复用为主，零新造链）：
- 生成走 ``generate_with_gate``（与 autosend 自动链、Stage A 同一后验入口），
  **强制 backend=command**（现场生图，非相册挑图）、**关相册兜底**（坐席要的是生成图，
  兜底一张随机相册照会误导）。
- 引擎按局域网算力分配：flux_pulid（现役 176 FLUX+PuLID 锁脸）/ qwen_edit
  （Qwen-Image-Edit-2511 参考图一致性）/ z_image（Z-Image-Turbo 速度档）。
  每引擎 command_args 可在 ``companion.selfie.provider.manual_ui.engines`` 显式配；
  未配则从既有 ``provider.command_args`` 派生（注入 ``--engine`` / ``--face-ref {base}``）
  ——生产已配 command_args，故本功能免改 overlay 即可用 flux_pulid。
- 发送**不新造**：前端取生成预览 blob 走既有 send-media（幂等/未送达回执/接管全复用）。
- 存册：copy 到 persona 相册目录 + ``persona_media_store.add``（与自动入册同表）。

安全：受 ``companion.selfie.enabled`` 总闸（生产已开）；写操作 viewer 403；
路径消毒防穿越；生成产物只落 out_dir/预览静态目录。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger("ai_chat_assistant.image_gen_routes")

_ROLE_VIEWER = "viewer"
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}

# 引擎默认标签（i18n 由前端 cp-i18n 兜；后端只回 id + 可选 label 覆写）。
_DEFAULT_ENGINES = ("flux_pulid", "qwen_edit", "z_image")

# ── 错误码契约（2026-08-22 错误面收口）────────────────────────────────────
# 生成失败响应带 ``code``，前端按 ``cp.image.errc_<code>``/``errs_<code>`` 出
# 人话标题+建议（契约由 tests/test_cp_app_i18n_keys.py 双向钉住）。来源优先级：
# comfy_infer stderr 的显式 ``ERR_CODE=`` 行（确定性）> 旧格式/非 command 后端
# 的特征回落。新增码必须同步：本表 + comfy_infer 发射点 + cp-i18n 两语词条。
KNOWN_ERROR_CODES = (
    "model_missing",        # 服务端模型清单缺文件/为空（2026-08-22 事故形态）
    "vram_insufficient",    # 腾挪后显存仍不足，主动放弃
    "gen_timeout",          # 等待出图超时（常见=冷启动加载模型中）
    "lock_busy",            # 本机出图互斥锁等待超时（有别的图在渲）
    "gate_reject",          # VLM 视觉体检不合格（重试次数用尽）
    "submit_rejected",      # /prompt 被拒（非缺模型的校验失败）
    "server_unreachable",   # ComfyUI 端点不可达
    "face_ref_missing",     # 锁脸但人设无基准脸（路由快失败，不烧 GPU）
    "face_ref_upload_failed",
    "template_error",       # 工作流模板缺失/解析失败
    "no_output",            # 执行完成但无产物（执行期错误，常见=显存被挤）
    "exec_error",           # 其它执行异常
    "preview_failed",       # 生成成功但预览落盘失败（磁盘/静态目录问题）
    "quota_exceeded",       # 手动出图日配额用尽（manual_ui.daily_quota，P2）
    "identity_mismatch",    # 同脸校验判「不同人」（vision_gate.identity_check，P2）
    "unknown",
)

_ERR_CODE_RE = re.compile(r"ERR_CODE=([a-z_]+)")


def classify_gen_error(reason: str) -> str:
    """失败 reason → 机器码。显式 ``ERR_CODE=`` 优先；否则按特征保守回落。"""
    s = str(reason or "")
    m = _ERR_CODE_RE.search(s)
    if m and m.group(1) in KNOWN_ERROR_CODES:
        return m.group(1)
    low = s.lower()
    if "vision_gate:" in low:
        return "gate_reject"
    if "selfie_timeout" in low or "timeoutexpired" in low or "等待出图超时" in s:
        return "gen_timeout"
    if "not in list" in low or "not in []" in low:
        return "model_missing"
    if "提交被拒" in s:
        return "submit_rejected"
    if "出图锁" in s:
        return "lock_busy"
    if "urlopen error" in low or "connection refused" in low or "10061" in low:
        return "server_unreachable"
    if "显存" in s or "vram" in low:
        return "vram_insufficient"
    return "unknown"


def _comfy_url_from_args(args: Any) -> str:
    """从 command_args 抽 ``--url`` 的 ComfyUI 基址（探测部署状态用）；无则空串。"""
    if not isinstance(args, list):
        return ""
    for i, a in enumerate(args):
        if str(a) == "--url" and i + 1 < len(args):
            return str(args[i + 1]).rstrip("/")
    return ""


def probe_comfy_models(url: str, timeout: float = 4.0) -> Optional[Dict[str, List[str]]]:
    """查 ComfyUI 服务端可用模型清单 ``{ckpts, unets}``；探测失败返回 None（未知）。

    这是「引擎列表诚实化」的数据源：未装模型的引擎在前端置灰而不是让坐席
    选中后吃 400（2026-08-22 事故里 flux 模型被整树清空，探测会得到空清单
    → 三引擎全灰 + 面板顶部醒目提示，坐席第一眼就知道该找运维而不是自己重试）。
    """
    if not url:
        return None
    out: Dict[str, List[str]] = {}
    try:
        for key, node, field in (("ckpts", "CheckpointLoaderSimple", "ckpt_name"),
                                 ("unets", "UNETLoader", "unet_name")):
            with urllib.request.urlopen(f"{url}/object_info/{node}",
                                        timeout=timeout) as r:
                info = json.loads(r.read())
            lst = ((info.get(node) or {}).get("input", {})
                   .get("required", {}).get(field) or [[]])[0]
            out[key] = [str(x) for x in lst] if isinstance(lst, list) else []
    except Exception:
        return None
    return out


def engine_deploy_status(models: Optional[Dict[str, List[str]]],
                         engines: List[str]) -> Dict[str, Optional[bool]]:
    """按服务端模型清单判各引擎部署态：True/False/None(未知——探测失败或自定义引擎)。"""
    status: Dict[str, Optional[bool]] = {e: None for e in engines}
    if models is None:
        return status
    ck = [s.lower() for s in models.get("ckpts", [])]
    un = [s.lower() for s in models.get("unets", [])]
    if "flux_pulid" in status:
        status["flux_pulid"] = any("flux" in s for s in ck)
    if "qwen_edit" in status:
        status["qwen_edit"] = any("qwen_image_edit" in s for s in un)
    if "z_image" in status:
        status["z_image"] = any("z_image" in s or "z-image" in s for s in un)
    return status


# 探测结果 TTL 缓存（模块级；60s——模型恢复后一分钟内面板自动解锁，够快且不打爆探测）
_PROBE_TTL_SEC = 60.0
_probe_cache: Dict[str, Any] = {"ts": 0.0, "url": "", "models": None}


def _cached_probe(url: str) -> Optional[Dict[str, List[str]]]:
    now = time.time()
    if (_probe_cache["url"] == url
            and now - float(_probe_cache["ts"]) < _PROBE_TTL_SEC):
        return _probe_cache["models"]
    models = probe_comfy_models(url)
    _probe_cache.update({"ts": now, "url": url, "models": models})
    return models


def probe_comfy_vram(url: str, timeout: float = 3.0) -> Optional[float]:
    """查出图卡当前空闲显存(GB)；失败 None。给「生成前算力预警」用（知情不拦）。"""
    if not url:
        return None
    try:
        with urllib.request.urlopen(url + "/system_stats", timeout=timeout) as r:
            j = json.loads(r.read())
        dev = (j.get("devices") or [{}])[0]
        return round(float(dev.get("vram_free", 0)) / (1024 ** 3), 1)
    except Exception:
        return None


_VRAM_TTL_SEC = 30.0
_vram_cache: Dict[str, Any] = {"ts": 0.0, "url": "", "free": None}


def _cached_vram(url: str) -> Optional[float]:
    now = time.time()
    if (_vram_cache["url"] == url
            and now - float(_vram_cache["ts"]) < _VRAM_TTL_SEC):
        return _vram_cache["free"]
    free = probe_comfy_vram(url)
    _vram_cache.update({"ts": now, "url": url, "free": free})
    return free


# ── 手动出图日配额（P2；进程级计数——重启清零=宽松语义，成本护栏而非硬闸）──
_QUOTA_STATE: Dict[str, Any] = {"day": "", "by_actor": {}}


def quota_check_and_count(actor: str, limit: int,
                          now: Optional[float] = None) -> Optional[int]:
    """配额闸门：未超限 → 计数 +1 并返回 None；超限 → 返回已用数（不计数）。

    ``limit<=0``＝不限额（与 daily_reply_budget=0 同语义，勿改成全拦）。
    按 (日, actor) 计数——「谁在烧算力」与限额判定同一口径。
    """
    if limit <= 0:
        return None
    day = time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))
    if _QUOTA_STATE["day"] != day:
        _QUOTA_STATE["day"] = day
        _QUOTA_STATE["by_actor"] = {}
    used = int(_QUOTA_STATE["by_actor"].get(actor or "-", 0))
    if used >= limit:
        return used
    _QUOTA_STATE["by_actor"][actor or "-"] = used + 1
    return None


def quota_used(actor: str) -> int:
    day = time.strftime("%Y-%m-%d")
    if _QUOTA_STATE["day"] != day:
        return 0
    return int(_QUOTA_STATE["by_actor"].get(actor or "-", 0))


def with_timeout_arg(cmd_args: List[str], budget_sec: float) -> List[str]:
    """给 comfy_infer 注入 ``--timeout=预算-15s``（已带 --timeout / 非 comfy_infer
    命令不动）：让子进程在被 ``command_timeout_sec`` 掐死**之前**优雅退出，留下
    ``ERR_CODE=gen_timeout`` 终局真相——掐死只剩 TimeoutExpired，什么都查不到。"""
    args = [str(x) for x in (cmd_args or [])]
    if "--timeout" in args or not any("comfy_infer" in a for a in args):
        return args
    return args + ["--timeout", str(int(max(60.0, budget_sec - 15)))]


# ── 异步任务注册表（P1 2026-08-22：生成挂长连接 2-5 分钟不可取消 → 任务化）───
# 进程内即可（单实例部署 + 出图本机互斥锁天然串行）；完成态保留 30min 供轮询
# 迟到者取结果，超量从最老清（防内存缓慢泄漏）。
_JOBS: Dict[str, Dict[str, Any]] = {}
_JOB_TTL_SEC = 1800.0
_JOBS_CAP = 60


def _prune_jobs(now: Optional[float] = None) -> None:
    ts = now if now is not None else time.time()
    stale = [k for k, j in _JOBS.items()
             if j.get("status") not in ("queued", "running")
             and ts - float(j.get("ts", 0)) > _JOB_TTL_SEC]
    for k in stale:
        _JOBS.pop(k, None)
    if len(_JOBS) > _JOBS_CAP:
        for k in sorted(_JOBS, key=lambda x: float(_JOBS[x].get("ts", 0)))[
                : len(_JOBS) - _JOBS_CAP]:
            _JOBS.pop(k, None)


def _comfy_interrupt(url: str, timeout: float = 5.0) -> bool:
    """请求 ComfyUI 中断当前渲染（取消任务的 GPU 侧配合，best-effort 绝不抛）。

    本机出图互斥锁保证「当前渲染」就是我们这单——不会误伤别人的任务。"""
    if not url:
        return False
    try:
        req = urllib.request.Request(url + "/interrupt", data=b"", method="POST")
        urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except Exception:
        return False


def register_image_gen_routes(app, auth_dep, audit_store=None, config_manager=None,
                              page_auth=None):
    """挂载坐席手动出图 API。``auth_dep``=登录校验；``audit_store``=操作审计（可选）。"""

    def _root_cfg() -> Dict[str, Any]:
        try:
            return (config_manager.config if config_manager is not None else {}) or {}
        except Exception:
            return {}

    def _selfie_cfg() -> Dict[str, Any]:
        return ((_root_cfg().get("companion") or {}).get("selfie") or {})

    def _provider_cfg() -> Dict[str, Any]:
        return (_selfie_cfg().get("provider") or {})

    def _manual_cfg() -> Dict[str, Any]:
        return (_provider_cfg().get("manual_ui") or {})

    def _enabled() -> bool:
        # 手动出图是既有 selfie 能力的延伸（非新子系统）：随 companion.selfie.enabled 开关；
        # manual_ui.enabled 显式 false 可单独关掉手动入口（保留自动链出图）。
        if not bool(_selfie_cfg().get("enabled", False)):
            return False
        return bool(_manual_cfg().get("enabled", True))

    def _actor(request: Request) -> str:
        try:
            return str(request.session.get("username") or "web_admin")
        except Exception:
            return "web_admin"

    def _is_viewer(request: Request) -> bool:
        try:
            return str(request.session.get("role") or "") == _ROLE_VIEWER
        except Exception:
            return False

    def _audit(request: Request, action: str, target: str = "", detail: str = "") -> None:
        if audit_store is None:
            return
        try:
            audit_store.log(actor=_actor(request), action=action,
                            target=target, detail=detail)
        except Exception:
            logger.debug("image_gen audit 失败", exc_info=True)

    def _persona_manager():
        from src.utils.persona_manager import PersonaManager
        return PersonaManager.get_instance()

    def _face_ref_path(persona_id: str) -> str:
        """人设锁脸基准图：``album_dir/<pid>/face_ref.*``（无则空串）。"""
        pcfg = _provider_cfg()
        album_dir = Path(str(pcfg.get("album_dir") or "config/persona_albums"))
        key = "".join(c for c in str(persona_id or "")
                      if c.isalnum() or c in ("-", "_"))
        if not key:
            return ""
        base = album_dir / key
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            p = base / ("face_ref" + ext)
            if p.is_file():
                try:
                    return str(p.resolve())
                except OSError:
                    return str(p)
        return ""

    def _engine_command_args(engine: str, base_args: Any, lock_face: bool) -> Optional[List[str]]:
        """解析某引擎的 command_args：显式配 > 从 provider.command_args 派生。

        派生规则（生产已配 base command_args=[python comfy_infer.py --url .. --prompt {prompt}
        --out {out}]，据此免改 overlay 即用）：
          flux_pulid: base + [--face-ref {base}]（lock_face 时；否则不加）
          qwen_edit : base + [--engine qwen_edit, --face-ref {base}(lock), --min-free-gb 18]
          z_image   : base + [--engine z_image]
        """
        engines = _manual_cfg().get("engines") or {}
        explicit = (engines.get(engine) or {}).get("command_args")
        if isinstance(explicit, list) and explicit:
            return [str(x) for x in explicit]
        if not isinstance(base_args, list) or not base_args:
            return None
        args = [str(x) for x in base_args]
        if engine == "z_image":
            return args + ["--engine", "z_image"]
        extra: List[str] = []
        if engine == "qwen_edit":
            extra += ["--engine", "qwen_edit", "--min-free-gb", "18"]
        if lock_face:
            extra += ["--face-ref", "{base}"]
        return args + extra

    def _engine_list() -> List[str]:
        cfg = _manual_cfg().get("engines")
        if isinstance(cfg, dict) and cfg:
            return [str(k) for k in cfg.keys()]
        return list(_DEFAULT_ENGINES)

    def _out_dir() -> Path:
        return Path(str(_provider_cfg().get("out_dir") or "tmp_selfies"))

    # ── GET /api/image/config：卡片初始化（人设列表 + 引擎 + 开关）────────────
    @app.get("/api/image/config")
    async def api_image_config(request: Request, _=Depends(auth_dep)):
        if not _enabled():
            return {"ok": True, "enabled": False}
        personas: List[Dict[str, Any]] = []
        try:
            pm = _persona_manager()
            for s in pm.list_profiles_summary():
                pid = str(s.get("id") or "")
                if not pid:
                    continue
                personas.append({
                    "id": pid,
                    "name": str(s.get("name") or s.get("display_name") or pid),
                    "has_face_ref": bool(_face_ref_path(pid)),
                })
        except Exception:
            logger.debug("image config 人设列表失败", exc_info=True)
        mcfg = _manual_cfg()
        engines = _engine_list()
        # 部署态探测（TTL 60s）：未装模型的引擎前端置灰；ComfyUI 不可达时
        # deployed=None（未知，不拦）+ comfy_ok=false（前端出「服务器不可达」预警）。
        # 必须进线程池——同步 urlopen 最坏阻塞 ~4s，async 路由里裸调会卡整个事件循环。
        comfy_url = _comfy_url_from_args(_provider_cfg().get("command_args"))
        loop = asyncio.get_running_loop()
        models = await loop.run_in_executor(None, _cached_probe, comfy_url)
        vram_free = await loop.run_in_executor(None, _cached_vram, comfy_url)
        deploy = engine_deploy_status(models, engines)
        quota = int(mcfg.get("daily_quota") or 0)
        return {
            "ok": True,
            "enabled": True,
            "engines": engines,
            "engines_info": [{"id": e, "deployed": deploy.get(e)} for e in engines],
            "comfy_ok": models is not None,
            "default_engine": str(mcfg.get("default_engine") or "flux_pulid"),
            "personas": personas,
            "default_persona": str(_provider_cfg().get("default_album_key") or ""),
            # P1 能力旗标（前端特性探测：旧后端无此键 → 组件自动退回同步生成路径）
            "jobs_api": True,
            "album_pick": True,
            # P2：场景热度提示 + 算力预警 + 日配额（0=不限）
            "scene_hints": True,
            "vram_free_gb": vram_free,
            "daily_quota": quota,
            "quota_used": quota_used(_actor(request)) if quota > 0 else 0,
        }

    # ── 生成核心（sync /generate 与异步 jobs 共用；单一实现防口径分叉）──────
    def _parse_gen_body(body: Dict[str, Any]) -> Dict[str, Any]:
        p: Dict[str, Any] = {}
        p["persona_id"] = str(body.get("persona_id") or "").strip()
        p["prompt"] = str(body.get("prompt") or "").strip()
        p["mode"] = str(body.get("mode") or "selfie").strip().lower()
        p["engine"] = str(body.get("engine") or "").strip() or "flux_pulid"
        p["scene"] = str(body.get("scene") or "").strip()
        p["lock_face"] = bool(body.get("lock_face", p["mode"] == "selfie"))
        try:
            w, h = int(body.get("width") or 1024), int(body.get("height") or 1024)
        except Exception:
            w, h = 1024, 1024
        p["width"], p["height"] = max(512, min(1536, w)), max(512, min(1536, h))
        return p

    def _gen_guards(request: Request, p: Dict[str, Any]) -> None:
        """请求级校验（HTTPException 带请求语言文案）；sync 与 jobs 入口共用。"""
        if _is_viewer(request):
            raise HTTPException(403, tr(request, "err.image.viewer_denied"))
        if not _enabled():
            raise HTTPException(403, tr(request, "err.image.disabled"))
        if not p["prompt"]:
            raise HTTPException(400, tr(request, "err.image.prompt_empty"))
        if p["engine"] not in _engine_list():
            raise HTTPException(
                400, tr(request, "err.image.engine_unknown", name=p["engine"]))
        if _engine_command_args(p["engine"], _provider_cfg().get("command_args"),
                                p["lock_face"]) is None:
            raise HTTPException(
                501, tr(request, "err.image.engine_unconfigured", name=p["engine"]))

    async def _do_generate(p: Dict[str, Any], actor: str = "") -> Dict[str, Any]:
        """出图 + VLM 后验 + 预览落盘。**绝不抛**——统一返回响应 dict
        （ok/code/reason...），sync 端点原样透传、jobs 任务体存结果。"""
        from src.web.image_gen_stats import get_image_gen_stats
        stats = get_image_gen_stats()
        engine, persona_id = p["engine"], p["persona_id"]
        stats.record_attempt(engine)
        t0 = time.time()

        def _fail(code: str, reason: str) -> Dict[str, Any]:
            stats.record_fail(code)
            logger.info("[image_gen] 生成失败 engine=%s code=%s reason=%s",
                        engine, code, str(reason)[:600])
            return {"ok": False, "code": code, "reason": str(reason)[:800],
                    "engine": engine,
                    "latency_ms": int((time.time() - t0) * 1000)}

        # 日配额闸门（P2；limit<=0=不限）：放在一切重操作之前，超限零 GPU 消耗。
        _q_limit = int(_manual_cfg().get("daily_quota") or 0)
        _q_used = quota_check_and_count(actor, _q_limit)
        if _q_used is not None:
            return _fail("quota_exceeded",
                         f"daily quota reached: {_q_used}/{_q_limit} (actor={actor or '-'})")

        pcfg = dict(_provider_cfg())
        cmd_args = _engine_command_args(engine, pcfg.get("command_args"),
                                        p["lock_face"])
        if not cmd_args:
            return _fail("submit_rejected", f"engine '{engine}' unconfigured")

        # 手动链超时预算：人在等，值得给足——默认 max(300s, 生产 command_timeout_sec)；
        # manual_ui.timeout_sec 可显式覆写。并注入 --timeout=预算-15s 让 comfy_infer
        # 在被掐死之前优雅退出留下 ERR_CODE 终局真相。
        manual_timeout = float(_manual_cfg().get("timeout_sec") or 0) \
            or max(300.0, float(pcfg.get("command_timeout_sec", 180) or 180))
        cmd_args = with_timeout_arg(cmd_args, manual_timeout)

        # 强制 command 后端（现场生图）+ 关相册兜底（坐席要生成图，非随机相册照）。
        prov_cfg = {
            "enabled": True,
            "backend": "command",
            "command_args": cmd_args,
            "command_timeout_sec": manual_timeout,
            "out_dir": str(_out_dir()),
            "album_dir": str(pcfg.get("album_dir") or "config/persona_albums"),
            "default_album_key": str(pcfg.get("default_album_key") or ""),
            "album_fallback": False,
        }
        base_image = _face_ref_path(persona_id) if p["lock_face"] else ""
        if p["lock_face"] and p["mode"] != "object" and not base_image:
            # 锁脸但人设无基准脸：继续生成＝随机人脸（「换人」级穿帮）。快失败零 GPU。
            return _fail("face_ref_missing",
                         f"persona '{persona_id or '-'}' has no face_ref")
        persona = None
        try:
            if persona_id:
                persona = _persona_manager().get_persona_by_id(persona_id)
        except Exception:
            persona = None

        from src.ai.companion_selfie import get_selfie_provider, reset_selfie_provider
        from src.ai.image_gate import generate_with_gate, resolve_gate_cfg
        # 引擎间 command_args 不同 → 强制按本次 cfg 重建 provider（显式 reset 防串味）。
        reset_selfie_provider()
        provider = get_selfie_provider(prov_cfg)
        gate_cfg = resolve_gate_cfg(_selfie_cfg())
        kind = "object" if p["mode"] == "object" else "selfie"
        subject = p["prompt"] if kind == "object" else ""
        try:
            res = await generate_with_gate(
                provider, p["prompt"], persona=persona, root_config=_root_cfg(),
                gate_cfg=gate_cfg, seed=-1, kind=kind, subject=subject,
                expect_scene=p["scene"], album_key=persona_id,
                allow_album_fallback=False, base_image=base_image)
        except Exception as ex:  # noqa: BLE001
            reason = f"{type(ex).__name__}: {ex}"
            return _fail(classify_gen_error(reason), reason)
        finally:
            reset_selfie_provider()  # 复位，autosend 链下次按自身配置重建

        if not getattr(res, "ok", False) or not getattr(res, "image_path", ""):
            reason = str(getattr(res, "error", "") or "unknown")
            return _fail(classify_gen_error(reason), reason)

        # 同脸校验（P2，vision_gate.identity_check 默认关）：锁脸生成后验
        # 「真是同一个人吗」——PuLID 是生成机制不是验收机制，锁脸失败出的
        # 陌生人脸对客户是「换人」级穿帮。软失败=skipped 绝不拦；明确判
        # 「不同人」才拒（与语音 clone_score 地板同哲学：只抓灾难级）。
        identity = "skipped"
        if (bool(gate_cfg.get("identity_check", False)) and base_image
                and p["lock_face"] and kind == "selfie"):
            from src.ai.image_gate import check_face_identity
            identity, _id_note = await check_face_identity(
                res.image_path, base_image, _root_cfg())
            if identity == "mismatch":
                return _fail("identity_mismatch",
                             f"identity check: not the same person ({_id_note})")

        # 生成图落静态预览目录（复用出站媒体静态服务），前端取 URL 预览/发送。
        try:
            from src.integrations.protocol_bridge import save_outbound_media
            data = Path(res.image_path).read_bytes()
            filename = os.path.basename(res.image_path) or f"gen_{uuid.uuid4().hex[:8]}.png"
            _local, preview_url, _mt = save_outbound_media(
                "_genpreview", persona_id or "default", filename, data)
        except Exception as ex:  # noqa: BLE001
            return _fail("preview_failed", f"{type(ex).__name__}: {ex}")

        lat = int((time.time() - t0) * 1000)
        stats.record_ok(lat)
        if audit_store is not None:
            try:
                audit_store.log(actor=actor or "web_admin", action="image.generate",
                                target=f"{engine}:{persona_id}",
                                detail=f"mode={p['mode']} scene={p['scene']}")
            except Exception:
                logger.debug("image_gen audit 失败", exc_info=True)
        return {
            "ok": True,
            "engine": engine,
            "preview_url": preview_url,
            "filename": os.path.basename(res.image_path),
            "path": res.image_path,
            "provider": str(getattr(res, "provider", "") or ""),
            "gate": str((getattr(res, "extra", {}) or {}).get("gate_reason", "")),
            "identity": identity,   # ok|mismatch(已拦)|skipped——前端徽章据实渲染
            "latency_ms": lat,
        }

    # ── POST /api/image/generate：同步出图（旧前端兼容面；新前端走 jobs）──────
    @app.post("/api/image/generate")
    async def api_image_generate(request: Request, _=Depends(auth_dep)):
        try:
            body = await request.json()
        except Exception:
            body = {}
        p = _parse_gen_body(body)
        _gen_guards(request, p)
        return await _do_generate(p, actor=_actor(request))

    # ── 异步任务三件套：创建 / 轮询 / 取消（P1 2026-08-22）────────────────────
    # 同步端点挂 2-5 分钟长连接：不可取消、壳/网络层可能先断（前端报「网络错误」
    # 而 GPU 白烧、坐席重试=双倍负载）。任务化后前端 1.5s 轮询 + 可取消
    # （ComfyUI /interrupt best-effort，出图互斥锁保证中断的就是本单）。
    @app.post("/api/image/jobs")
    async def api_image_job_create(request: Request, _=Depends(auth_dep)):
        try:
            body = await request.json()
        except Exception:
            body = {}
        p = _parse_gen_body(body)
        _gen_guards(request, p)
        _prune_jobs()
        job_id = uuid.uuid4().hex[:12]
        job: Dict[str, Any] = {"status": "running", "stage": "rendering",
                               "ts": time.time(), "cancelled": False,
                               "engine": p["engine"], "res": None}
        _JOBS[job_id] = job
        actor = _actor(request)

        async def _run() -> None:
            try:
                res = await _do_generate(p, actor=actor)
            except Exception as ex:  # noqa: BLE001（_do_generate 不抛；此为最后防线）
                res = {"ok": False, "code": "exec_error",
                       "reason": f"{type(ex).__name__}: {ex}", "engine": p["engine"]}
            job["res"] = res
            if job.get("cancelled"):
                job["status"] = "cancelled"   # 结果作废：坐席已明示不要
            else:
                job["status"] = "done" if res.get("ok") else "failed"

        asyncio.get_running_loop().create_task(_run())
        return {"ok": True, "job_id": job_id}

    @app.get("/api/image/jobs/{job_id}")
    async def api_image_job_status(job_id: str, request: Request, _=Depends(auth_dep)):
        job = _JOBS.get(str(job_id))
        if job is None:
            raise HTTPException(404, tr(request, "err.image.job_not_found"))
        out = {"ok": True, "status": job["status"], "stage": job.get("stage", ""),
               "elapsed_ms": int((time.time() - float(job["ts"])) * 1000)}
        if job["status"] in ("done", "failed") and isinstance(job.get("res"), dict):
            out["result"] = job["res"]
        return out

    @app.post("/api/image/jobs/{job_id}/cancel")
    async def api_image_job_cancel(job_id: str, request: Request, _=Depends(auth_dep)):
        if _is_viewer(request):
            raise HTTPException(403, tr(request, "err.image.viewer_denied"))
        job = _JOBS.get(str(job_id))
        if job is None:
            raise HTTPException(404, tr(request, "err.image.job_not_found"))
        if job["status"] in ("queued", "running") and not job.get("cancelled"):
            job["cancelled"] = True
            job["status"] = "cancelled"
            try:
                from src.web.image_gen_stats import get_image_gen_stats
                get_image_gen_stats().record_cancel()
            except Exception:
                pass
            # GPU 侧配合中断（best-effort；互斥锁保证当前渲染就是本单）
            url = _comfy_url_from_args(_provider_cfg().get("command_args"))
            await asyncio.get_running_loop().run_in_executor(
                None, _comfy_interrupt, url)
            _audit(request, "image.job_cancel", target=str(job_id))
        return {"ok": True, "status": job["status"]}

    # ── 相册优先层（P1）：生成前先看存货——秒回、零 GPU、与自动链「相册优先于
    #    现场出图」同哲学。scene 归一走 persona_media.scene_class_of 单一词表。──
    @app.get("/api/image/album-stock")
    async def api_image_album_stock(request: Request, _=Depends(auth_dep)):
        if not _enabled():
            return {"ok": True, "items": []}
        persona_id = str(request.query_params.get("persona_id") or "").strip()
        scene = str(request.query_params.get("scene") or "").strip()
        if not persona_id:
            return {"ok": True, "items": []}
        items: List[Dict[str, Any]] = []
        try:
            from src.companion.persona_media import row_scene_class, scene_class_of
            from src.companion.persona_media_store import get_persona_media_store
            st = get_persona_media_store()
            if st is not None:
                want = scene_class_of(scene) if scene else ""
                rows = st.list(persona_id, enabled_only=True, media_type="photo")
                for row in reversed(rows):  # 新入册优先展示
                    fp = str(row.get("file_path") or "")
                    if not fp or not Path(fp).is_file():
                        continue
                    cls = row_scene_class(dict(row))
                    if want and cls != want:
                        continue
                    items.append({
                        "media_id": str(row.get("id") or ""),
                        "path": fp,
                        "scene_class": cls,
                        "caption": str(row.get("caption") or ""),
                        "url": "/api/image/album-file?path=" + urllib.parse.quote(fp),
                    })
                    if len(items) >= 6:
                        break
        except Exception:
            logger.debug("[image_gen] album-stock 查询失败", exc_info=True)
        try:
            from src.web.image_gen_stats import get_image_gen_stats
            get_image_gen_stats().record_stock_query(hit=bool(items))
        except Exception:
            pass
        return {"ok": True, "items": items}

    @app.get("/api/image/album-file")
    async def api_image_album_file(request: Request, _=Depends(auth_dep)):
        """相册文件只读服务（viewer 可看）：路径消毒钉死在 album_dir 子树。"""
        if not _enabled():
            raise HTTPException(403, tr(request, "err.image.disabled"))
        raw = str(request.query_params.get("path") or "").strip()
        if not raw:
            raise HTTPException(400, tr(request, "err.image.path_denied"))
        album_root = Path(str(_provider_cfg().get("album_dir")
                              or "config/persona_albums"))
        try:
            root_res = album_root.resolve()
            p_res = Path(raw).resolve()
            if os.path.commonpath([str(root_res), str(p_res)]) != str(root_res):
                raise HTTPException(400, tr(request, "err.image.path_denied"))
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(400, tr(request, "err.image.path_denied"))
        if not p_res.is_file() or p_res.suffix.lower() not in _IMAGE_EXT:
            raise HTTPException(404, tr(request, "err.image.src_missing"))
        from fastapi.responses import FileResponse
        return FileResponse(str(p_res))

    # ── GET /api/image/scene-hints：场景热度提示（P2）——客户点名需求 × 相册
    #    供给，chips 按「未兑现↓→需求↓」排序 + 缺货标记：手动出图从「凭感觉挑
    #    场景」变成「照客户要的单补货」（与 media_gap 看板同源数据，零新计数器）。──
    @app.get("/api/image/scene-hints")
    async def api_image_scene_hints(request: Request, _=Depends(auth_dep)):
        if not _enabled():
            return {"ok": True, "scenes": []}
        persona_id = str(request.query_params.get("persona_id") or "").strip()
        try:
            from src.companion.media_gap import collect_scene_supply
            from src.inbox.image_autosend import metrics_snapshot
            supply = await asyncio.get_running_loop().run_in_executor(
                None, collect_scene_supply, _selfie_cfg())
            snap = metrics_snapshot()
            demand = dict(snap.get("scene_demand") or {})
            unmet = dict(snap.get("scene_unmet") or {})
            # P3：并入日账本 14 天窗（跨重启记忆）。进程计数与账本同源同事件
            # （record_scene_request 双写）→ 相加会双计，按 per-scene max 合并：
            # 账本为主（长窗），进程为兜底（账本写失败/重启后单例未建的间隙）。
            try:
                from src.companion.persona_media_store import peek_persona_media_store
                st = peek_persona_media_store()
                if st is not None:
                    win = await asyncio.get_running_loop().run_in_executor(
                        None, st.scene_demand_window, 14)
                    for sc, row in (win or {}).items():
                        if sc == "other":
                            continue
                        demand[sc] = max(int(demand.get(sc, 0) or 0),
                                         int(row.get("demand", 0) or 0))
                        unmet[sc] = max(int(unmet.get(sc, 0) or 0),
                                        int(row.get("unmet", 0) or 0))
            except Exception:
                logger.debug("[image_gen] scene-hints 账本窗读取失败", exc_info=True)
        except Exception:
            logger.debug("[image_gen] scene-hints 汇总失败", exc_info=True)
            return {"ok": True, "scenes": []}
        mine = (supply.get(persona_id) or {}) if persona_id else {}
        shared = supply.get("") or {}
        out: List[Dict[str, Any]] = []
        for sc in sorted(set(demand) | set(unmet)):
            if not sc or sc == "other":
                continue
            out.append({
                "scene": sc,
                "demand": int(demand.get(sc, 0) or 0),
                "unmet": int(unmet.get(sc, 0) or 0),
                "stock": int(mine.get(sc, 0) or 0) + int(shared.get(sc, 0) or 0),
            })
        out.sort(key=lambda r: (-r["unmet"], -r["demand"], r["scene"]))
        return {"ok": True, "scenes": out[:10]}

    # ── POST /api/image/mark-sent：发送成功后回写媒体账本（P1 一致性旁路收口）──
    # 手动图此前发出后自动链全然不知：重发冷却认不出「这张刚发过」、90min 服装
    # 连续窗丢失锚点。conv_key 口径与 image_autosend 完全一致（platform:acct:chat）。
    @app.post("/api/image/mark-sent")
    async def api_image_mark_sent(request: Request, _=Depends(auth_dep)):
        if _is_viewer(request):
            raise HTTPException(403, tr(request, "err.image.viewer_denied"))
        if not _enabled():
            raise HTTPException(403, tr(request, "err.image.disabled"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        persona_id = str(body.get("persona_id") or "").strip()
        path = str(body.get("path") or "").strip()
        media_id = str(body.get("media_id") or "").strip()
        scene = str(body.get("scene") or "").strip()
        is_album = bool(body.get("album"))
        platform = str(body.get("platform") or "").strip()
        account_id = str(body.get("account_id") or "").strip()
        chat_key = str(body.get("chat_key") or "").strip()
        recorded = False
        if platform and chat_key:
            conv_key = f"{platform}:{account_id or 'default'}:{chat_key}"
            file_key = os.path.basename(path) if path else ""
            try:
                # 系列取策展文件名约定 <scene>_<series>_<nn>（与 A/B 线同一函数）；
                # 生成图的随机文件名解析不出系列 → 空串（如实）。
                from src.ai.companion_selfie import album_series_of_path
                from src.companion.persona_media_store import get_persona_media_store
                st = get_persona_media_store()
                if st is not None and (media_id or file_key):
                    st.record_send(
                        conv_key, media_id or f"manual:{file_key}",
                        persona_id=persona_id,
                        series=(album_series_of_path(path) if path else ""),
                        file_key=file_key)
                    recorded = True
            except Exception:
                logger.debug("[image_gen] mark-sent 落账失败", exc_info=True)
        try:
            from src.web.image_gen_stats import get_image_gen_stats
            get_image_gen_stats().record_sent(album=is_album)
        except Exception:
            pass
        _audit(request, "image.mark_sent",
               target=f"{persona_id}:{media_id or os.path.basename(path)}",
               detail=f"album={is_album} scene={scene}")
        return {"ok": True, "recorded": recorded}

    # ── POST /api/image/save-album：把生成图存入人设相册 ─────────────────────
    @app.post("/api/image/save-album")
    async def api_image_save_album(request: Request, _=Depends(auth_dep)):
        if _is_viewer(request):
            raise HTTPException(403, tr(request, "err.image.viewer_denied"))
        if not _enabled():
            raise HTTPException(403, tr(request, "err.image.disabled"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        persona_id = str(body.get("persona_id") or "").strip()
        src_path = str(body.get("path") or "").strip()
        scene = str(body.get("scene") or "").strip()
        if not persona_id or not src_path:
            raise HTTPException(400, tr(request, "err.image.save_missing"))
        # 路径消毒：只允许 out_dir 内的生成产物（防任意文件读写/穿越）。
        try:
            out_root = _out_dir().resolve()
            src_res = Path(src_path).resolve()
            if os.path.commonpath([str(out_root), str(src_res)]) != str(out_root):
                raise HTTPException(400, tr(request, "err.image.path_denied"))
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(400, tr(request, "err.image.path_denied"))
        if not src_res.is_file() or src_res.suffix.lower() not in _IMAGE_EXT:
            raise HTTPException(404, tr(request, "err.image.src_missing"))

        pcfg = _provider_cfg()
        album_dir = Path(str(pcfg.get("album_dir") or "config/persona_albums"))
        key = "".join(c for c in persona_id if c.isalnum() or c in ("-", "_"))
        if not key:
            raise HTTPException(400, tr(request, "err.image.persona_bad"))
        dest_dir = album_dir / key
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            ext = src_res.suffix.lower()
            dst = dest_dir / f"manual_{time.strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}{ext}"
            shutil.copy2(str(src_res), str(dst))
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(502, tr(request, "err.image.save_failed", err=str(ex)[:200]))

        registered = False
        try:
            from src.companion.persona_media_store import get_persona_media_store
            st = get_persona_media_store()
            tags = ["manual_generated"]
            if scene:
                tags.append(f"scene:{scene}")
            st.add(persona_id, "photo", str(dst), "", tags=tags,
                   created_by="manual_ui")
            registered = True
        except Exception:
            logger.debug("save-album 入册失败（文件已落盘）", exc_info=True)

        try:
            from src.web.image_gen_stats import get_image_gen_stats
            get_image_gen_stats().record_saved()
        except Exception:
            pass
        _audit(request, "image.save_album", target=persona_id, detail=f"scene={scene}")
        return {"ok": True, "registered": registered, "path": str(dst)}

    logger.info("[image_gen] 手动出图路由已挂载 /api/image/*")
