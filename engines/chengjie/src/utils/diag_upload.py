# -*- coding: utf-8 -*-
"""诊断包「打包 → 转投官网 → 回 6 位短码」的单一实现。

两个 HTTP 入口共用本模块，它们存在的理由不同、**实现不该有两份**：

- ``POST /api/admin/diagnostic-upload``（历史入口，运维/管理员）
- ``POST /api/support/diag-upload``（P1-9 新增，**坐席也能用**——``ROLE_AGENT``
  被 ``admin._agent_api_allowed`` 挡在 ``/api/admin/*`` 之外，报障链对坐席原本是断的）

若各写一份，打码规则/元数据/超时会各自漂移，客服拿到的两个包也就不再等价。

返回契约：``{"ok": True, "code": "123456"}`` 或 ``{"ok": False, "error": <机器码>}``。
**刻意不返回人话文案**——调用方各自 ``tr(request, ...)`` 出用户语言。
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 官网收包端点的超时：包已在本机打好，这里只是一次上行传输。
UPLOAD_TIMEOUT_SEC = 30
DEFAULT_SITE = "https://bd2026.cc"


def resolve_diag_dirs(config_manager) -> Tuple[Optional[Path], Optional[Path]]:
    """按实例配置推 (config_dir, logs_dir)；推不出来一律 (None, None)。

    绝不抛——诊断包本身就是「出事时才用」的东西，不能因为路径推断失败而无包可传。
    """
    try:
        cfg_path = getattr(config_manager, "config_path", "") or ""
        if not cfg_path:
            return None, None
        cfg_dir = Path(cfg_path).parent
        return cfg_dir, cfg_dir.parent / "logs"
    except Exception:
        logger.debug("diag 目录解析失败（已忽略）", exc_info=True)
        return None, None


def app_identity() -> Tuple[Dict[str, Any], str]:
    """返回 (meta_app_payload, version)；取不到 → ({}, "")。"""
    try:
        from src.utils.app_identity import identity_payload
        payload = identity_payload() or {}
        return payload, str(payload.get("version") or "")
    except Exception:
        return {}, ""


def machine_code() -> str:
    """本机机器码（``XXXX-XXXX-XXXX-XXXX``）；licensing 模块缺席 → 空串。

    空串是**合法**状态（源码态/精简包），调用方据此隐藏机器码行而不是显示 "N/A"。
    """
    try:
        from src.licensing.machine_bridge import machine_fingerprint
        return str(machine_fingerprint() or "")
    except Exception:
        return ""


def site_url(config_manager) -> str:
    cfg = (getattr(config_manager, "config", None) if config_manager is not None else None) or {}
    try:
        from src.ai.hosted_gateway import _site_url
        return _site_url(cfg)
    except Exception:
        return DEFAULT_SITE


NOTE_MAX_CHARS = 200


def runtime_log_files() -> Dict[str, Path]:
    """从运行时 logger 树收集真实的文件日志落点（P2-12 B38 批 2026-08-22）。

    3DTSV9 实战暴露：诊断包只有 fatal/sidecar，**主日志 app.log 零痕迹**——桌面
    部署的 file handler 落点不在 logs_dir 一级，按路径猜必然漂。这里直接问
    进程：root / ai_chat_assistant / src 三个 logger 名下全部 file handler 的
    ``baseFilename``（进程正在写谁就收谁），arcname 归 ``logs/app/`` 下。
    绝不抛；收不到＝返回空（源码态 console-only 部署是合法形态）。
    """
    out: Dict[str, Path] = {}
    try:
        import logging as _logging
        seen: set = set()
        for name in ("", "ai_chat_assistant", "src"):
            try:
                lg = _logging.getLogger(name) if name else _logging.getLogger()
                for h in list(getattr(lg, "handlers", []) or []):
                    base = getattr(h, "baseFilename", None)
                    if not base or base in seen:
                        continue
                    seen.add(base)
                    p = Path(base)
                    if p.is_file():
                        out[f"logs/app/{p.name}"] = p
            except Exception:
                continue
    except Exception:
        return {}
    return out


def shell_backend_log_files() -> Dict[str, Path]:
    """B76（实施74 b4alt）：打包态主日志兜底——收壳捕获的 stdout 镜像。

    EC4UPG/KVRX2Y（1.054）实锤：桌面部署 ``logging.file`` 常为空 → 引擎进程
    **没有** FileHandler → ``runtime_log_files()`` 空手而归 → 诊断包只有
    sidecar/fatal，主链日志取证整体被阻塞（B71/B73 类全靠猜）。打包态真正的
    主日志是 backend-launcher 行缓冲落盘的 ``<userData>/logs/backend.log``
    （stdout/stderr 镜像，PYTHONUNBUFFERED=1 保实时）；按
    ``AITR_DATA_DIR=<userData>/data`` 布局契约（sidecar-launcher 同款）上行
    一级定位。只在桌面态（``AITR_DESKTOP_MODE=1``）收集；单文件尾裁由
    ``build_diagnostic_bundle`` 的 ``extra_tail_kb``（2MB）承担——backend.log
    是无轮转追加文件，可能数百 MB，绝不整文件进包。绝不抛。
    """
    out: Dict[str, Path] = {}
    try:
        import os
        if str(os.environ.get("AITR_DESKTOP_MODE") or "") != "1":
            return out
        data_dir = str(os.environ.get("AITR_DATA_DIR") or "").strip()
        if not data_dir:
            return out
        p = Path(data_dir).resolve().parent / "logs" / "backend.log"
        if p.is_file():
            out["logs/app/backend.log"] = p
    except Exception:
        return {}
    return out


def sanitize_note(note: Any) -> str:
    """把前端带来的「用户当时看到什么」压成一行短文本。

    这条 note 的全部价值是让客服**不必先问「你当时在哪个页面、报什么错」**——
    所以它只该装报错文案与页面路径，且必须有硬上限：出问题的那一刻前端可能把
    整段响应体塞进 toast，无上限＝把响应体（可能含业务数据）搬进诊断包。
    """
    return " ".join(str(note or "").split())[:NOTE_MAX_CHARS]


#: 传输失败重试间隔（秒）。一次瞬断重试即过；长断由 mini 包降级兜。
RETRY_DELAY_SEC = 2.0

#: mini 包的日志尾巴预算（KB/文件）——「meta+fatal ~10KB」历史实证可过
#: 掐大 POST 的线路（B53：skuio 机全尺寸包全程 unreachable，同机小 POST 200）。
MINI_TAIL_KB = 24


def error_detail_for(request: Any, err: str) -> str:
    """机器码 → 用户语言人话（三分：本地未就绪 / 官网不可达 / 被拒收）。

    两个 HTTP 入口（support / ops-overview）共用，勿各写一份映射。"""
    from src.web.web_i18n import tr
    e = str(err or "")
    if e == "upstream_unreachable":
        return tr(request, "err.svc.upstream_unreachable")
    if e == "bundle_failed":
        return tr(request, "err.svc.diag_bundle_failed")
    if e.startswith("upload_rejected"):
        status = e.rsplit("_", 1)[-1]
        return tr(request, "err.svc.diag_upload_rejected",
                  status=(status if status.isdigit() else "?"))
    return e[:120]


def classify_upload_error(ex: Exception) -> str:
    """传输异常 → 三分机器码（B53：urllib HTTPError 此前被折叠成 unreachable，
    「服务端拒收 413」与「根本没连上」不可分辨，排查方向直接走偏）。

    - ``upload_rejected_<status>``：连上了、服务端拒收（413 超限/401/5xx…）；
    - ``upstream_unreachable``：连不上/超时/断线。
    纯函数可单测。
    """
    import urllib.error
    if isinstance(ex, urllib.error.HTTPError):
        try:
            return f"upload_rejected_{int(ex.code)}"
        except Exception:
            return "upload_rejected"
    return "upstream_unreachable"


def _fatal_log_files(logs_dir: Optional[Path]) -> Dict[str, Path]:
    """logs 目录里的崩溃第一现场（fatal/哨兵），mini 包的核心价值。"""
    out: Dict[str, Path] = {}
    try:
        if logs_dir is None:
            return out
        d = Path(logs_dir)
        if not d.is_dir():
            return out
        for pat in ("fatal*", "run_sentinel.json", "exit_*.json"):
            for p in sorted(d.glob(pat))[:4]:
                if p.is_file():
                    out[f"logs/{p.name}"] = p
    except Exception:
        return out
    return out


def line_diag_files() -> Dict[str, Path]:
    """B98/B76（实施68 P1-15，实施64:531）：收集 LINE 通道诊断文件进包。

    实测缺口：诊断包只带 msg/wa sidecar 日志，**LINE 通道零日志**——okline 走进程
    内 worker（不是独立 sidecar），协议 transcript 落 ``sessions/_diag/``，主链日志
    混在 app.log 里（已由 runtime_log_files 收）。此前 LINE 类问题（如 B98「只出不
    进」）永远无法远程取证。这里把 LINE 协议 transcript 收进来（每类取最近几份），
    arcname 归 ``logs/line/``。绝不抛：拿不到配置/目录不存在＝返回空。
    """
    out: Dict[str, Path] = {}
    try:
        from src.integrations.line_protocol_login import sessions_dir
    except Exception:
        return out
    try:
        cfg = {}
        try:
            from src.config.config_manager import get_config_manager
            _cm = get_config_manager()
            cfg = getattr(_cm, "config", None) or {}
        except Exception:
            cfg = {}
        diag_dir = Path(sessions_dir(cfg)) / "_diag"
        if not diag_dir.is_dir():
            return out
        # 协议 transcript（login_*.log）：登录/收发失败的第一现场，取最近 6 份
        entries = sorted(
            [p for p in diag_dir.glob("login_*.log") if p.is_file()],
            key=lambda p: p.stat().st_mtime, reverse=True)[:6]
        for p in entries:
            out[f"logs/line/{p.name}"] = p
    except Exception:
        return out
    return out


async def build_and_upload(config_manager, note: Any = "") -> Dict[str, Any]:
    """打诊断包并转投官网，返回 ``{ok, code, mini?}`` / ``{ok:False, error}``。

    走后端 server-to-server 转投而非浏览器直传——工作台是 127.0.0.1 源，
    直 POST 官网必撞 CORS 预检。

    B53（实施64 P1-6）传输链三改：
    ① 传输失败自动重试一次（瞬断即愈）；
    ② 报错三分（本地打包失败 ``bundle_failed`` / 服务端拒收
       ``upload_rejected_<status>`` / 连不上 ``upstream_unreachable``）；
    ③ 全尺寸包两次都没送出去 → 降级 **mini 包**（meta+fatal+主日志短尾，
       ~10KB 级）重传——egress 掐大/长 POST 的线路（skuio 实锤）也能把
       关键现场送回来，响应带 ``mini: true``。
    远程诊断腿（hosted_gateway.check_remote_diag）复用本函数＝同修复。

    ``note``＝报障现场（页面 + 报错文案），随包元数据落盘，见 :func:`sanitize_note`。
    """
    from src.utils.diagnostic_bundle import build_diagnostic_bundle

    cfg_dir, logs_dir = resolve_diag_dirs(config_manager)
    app_meta, ver = app_identity()
    meta: Dict[str, Any] = {}
    if app_meta:
        meta["app"] = app_meta
    meta["config_dir"] = str(cfg_dir or "")
    meta["logs_dir"] = str(logs_dir or "")
    note_s = sanitize_note(note)
    if note_s:
        meta["user_note"] = note_s
    fp = machine_code()

    # B98/B76：主 app.log（runtime_log_files）+ 打包态壳镜像 backend.log
    # （shell_backend_log_files，B76 半边收口）+ LINE 协议 transcript 一并进包——
    # LINE 类问题此前因诊断包缺 LINE 日志而无法远程取证。
    _extra = dict(runtime_log_files())
    _extra.update(shell_backend_log_files())
    _extra.update(line_diag_files())
    try:
        blob = await asyncio.to_thread(
            build_diagnostic_bundle, config_dir=cfg_dir, logs_dir=logs_dir,
            meta=meta, extra_files=_extra)
    except Exception:  # noqa: BLE001 —— 本地打包失败 ≠ 官网不可达，分开报
        logger.info("诊断包本地打包失败", exc_info=True)
        return {"ok": False, "error": "bundle_failed"}

    site = site_url(config_manager)

    def _upload(payload: bytes) -> Dict[str, Any]:
        req = urllib.request.Request(
            f"{site}/api/diag-upload", data=payload, method="POST")
        req.add_header("content-type", "application/zip")
        req.add_header("x-diag-meta", json.dumps(
            {"app": ver, "fp": fp, "note": note_s}, ensure_ascii=False))
        with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    last_err = "upstream_unreachable"
    for attempt in (1, 2):
        try:
            out = await asyncio.to_thread(_upload, blob)
            break
        except Exception as ex:  # noqa: BLE001
            last_err = classify_upload_error(ex)
            logger.info("诊断包转投官网失败（attempt=%d, %s, %d bytes）",
                        attempt, last_err, len(blob), exc_info=True)
            if attempt == 1:
                await asyncio.sleep(RETRY_DELAY_SEC)
    else:
        # 全尺寸两次都没送出去 → mini 包降级（meta + fatal + 主日志短尾）
        try:
            mini_meta = dict(meta)
            mini_meta["mini_bundle"] = True
            mini_meta["full_bundle_error"] = last_err
            mini_meta["full_bundle_bytes"] = len(blob)
            mini_extra = dict(_fatal_log_files(logs_dir))
            mini_extra.update(runtime_log_files())
            mini_extra.update(line_diag_files())
            mini_blob = await asyncio.to_thread(
                build_diagnostic_bundle, config_dir=None, logs_dir=None,
                meta=mini_meta, extra_files=mini_extra,
                extra_tail_kb=MINI_TAIL_KB)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": last_err}
        try:
            out = await asyncio.to_thread(_upload, mini_blob)
        except Exception as ex:  # noqa: BLE001
            logger.info("mini 诊断包重传仍失败（%d bytes）", len(mini_blob),
                        exc_info=True)
            return {"ok": False, "error": classify_upload_error(ex)}
        if not out.get("ok") or not out.get("code"):
            return {"ok": False,
                    "error": str(out.get("error") or "upload_failed")[:120]}
        logger.info("[diag] 全尺寸包传输失败（%s）→ mini 包送达 code=%s",
                    last_err, out.get("code"))
        return {"ok": True, "code": str(out.get("code")), "mini": True}
    if not out.get("ok") or not out.get("code"):
        return {"ok": False, "error": str(out.get("error") or "upload_failed")[:120]}
    return {"ok": True, "code": str(out.get("code"))}
