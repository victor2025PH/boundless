# -*- coding: utf-8 -*-
"""P2-② 失败任务自动留证据.

任务失败时自动捕获 (screencap + logcat 200 行 + 元数据 meta.json) 写到
`data/forensics/{task_id}/{ts}/`, 让运营点开任务详情就能看现场, 不必去服务器
拉日志/截图.

设计要点:
- **fail-safe**: 设备离线/磁盘满/adb 超时 → 写一份"capture_failed"meta, 不抛异常
- **异步线程**: 非阻塞 task lifecycle. daemon=False 但显式 join + 6s timeout, 避免
  主线程退出导致取证丢失
- **同 task_id 重试链多次失败**: 用 ts 子目录保留每次现场
- **自动清理**: startup_cleanup 清 > FORENSICS_RETENTION_DAYS 的目录, 防磁盘累积

API:
    capture_forensics(task_id, device_id, error_text, params_snapshot)
        → 异步触发取证 (立即返回)

    list_forensics(task_id) → list[dict]
        → 返回该 task_id 下所有时间戳目录的清单 (供 endpoint 消费)

    forensics_path(task_id, ts, filename) → Path
        → 解析单文件路径 (供静态文件服务用; 含路径穿越防护)

    startup_cleanup() → int
        → 清掉 > FORENSICS_RETENTION_DAYS 的目录, 返回清理数

存储 layout:
    data/forensics/
      {task_id}/
        {ts}/                    # ISO UTC 时间戳: 20260428T143012Z
          screencap.png          # adb exec-out screencap -p
          logcat.txt             # adb shell logcat -d -t 200
          meta.json              # 元数据 + 各步状态
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 配置 ──
FORENSICS_ROOT = Path("data/forensics")
FORENSICS_RETENTION_DAYS = 7
FORENSICS_LOGCAT_LINES = 200
FORENSICS_TIMEOUT_S = 6.0
FORENSICS_JOIN_TIMEOUT_S = 8.0  # 主线程退出前等待取证线程的最长时间
FORENSICS_AUTO_CLEANUP_INTERVAL_S = 86400  # 24h piggyback 清理一次

# 路径穿越防护: ts/filename 必须仅含安全字符
_SAFE_NAME_PAT = re.compile(r"^[a-zA-Z0-9_.\-]+$")

# 每日 piggyback 清理用 (避免 module-import 副作用 + server.py 0 改动)
_LAST_CLEANUP_TS = 0.0
_CLEANUP_LOCK = threading.Lock()


def capture_forensics(
    task_id: str,
    device_id: str,
    error_text: str = "",
    params_snapshot: Optional[Dict[str, Any]] = None,
) -> Optional[threading.Thread]:
    """异步触发取证. 立即返回 thread 对象 (供测试 join), 主流程不阻塞.

    传 device_id 为空时跳过 (无法定位设备), 传 task_id 为空时跳过.
    """
    if not task_id or not device_id:
        return None
    th = threading.Thread(
        target=_do_capture,
        args=(task_id, device_id, error_text, params_snapshot or {}),
        daemon=False,  # 显式 join 给主流程 retain
        name=f"forensics-{str(task_id)[:8]}",
    )
    th.start()
    _maybe_trigger_cleanup()
    return th


def _maybe_trigger_cleanup() -> None:
    """每 24h piggyback 一次自动清理. 0 改 server.py 让 retention 生效."""
    global _LAST_CLEANUP_TS
    now = time.time()
    with _CLEANUP_LOCK:
        if now - _LAST_CLEANUP_TS < FORENSICS_AUTO_CLEANUP_INTERVAL_S:
            return
        _LAST_CLEANUP_TS = now
    threading.Thread(
        target=startup_cleanup, daemon=True, name="forensics-auto-cleanup"
    ).start()


def _do_capture(task_id: str, device_id: str, error_text: str,
                params_snapshot: Dict[str, Any]) -> None:
    """实际执行取证. 任何异常都吞掉只 log warn."""
    try:
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        out_dir = FORENSICS_ROOT / _safe_seg(task_id) / ts
        out_dir.mkdir(parents=True, exist_ok=True)

        # params_snapshot 为空时 lazy fetch (避免 hook 处多查一次 SQL).
        # lazy import 防 task_store ↔ task_forensics 循环导入.
        if not params_snapshot:
            try:
                from src.host.task_store import get_task as _get_task
                t = _get_task(task_id, include_deleted=True)
                if t:
                    params_snapshot = t.get("params") or {}
                    if isinstance(params_snapshot, str):
                        try:
                            params_snapshot = json.loads(params_snapshot)
                        except Exception:
                            params_snapshot = {}
            except Exception as e:
                logger.debug("[forensics] lazy fetch params failed: %s", e)
                params_snapshot = {}

        meta: Dict[str, Any] = {
            "task_id": task_id,
            "device_id": device_id,
            "captured_at_utc": ts,
            "error": (error_text or "")[:500],
            "params": _redact_params(params_snapshot or {}),
        }

        # ── 0. P2.0: 优先 promote 该设备的 pending 快照（失败那一刻同步抓的） ──
        # 这些快照比 fallback screencap 更"真"——屏幕可能已经过去了 1-2 分钟。
        try:
            promoted = promote_pending_to_task(task_id, device_id, out_dir)
            meta["promoted_pending_count"] = promoted
        except Exception as e:
            logger.debug("[forensics] promote_pending in _do_capture failed: %s", e)
            meta["promoted_pending_count"] = 0

        # ── 1. screencap (兜底: 即便 pending 已 promote, 此处再补一张"取证那一刻"的画面,
        #     方便对比"失败那一刻 vs 取证那一刻", 看屏幕是否已飘走) ──
        png_path = out_dir / "screencap.png"
        try:
            proc = subprocess.run(
                ["adb", "-s", device_id, "exec-out", "screencap", "-p"],
                capture_output=True,
                timeout=FORENSICS_TIMEOUT_S,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0 and proc.stdout and len(proc.stdout) > 100:
                png_path.write_bytes(proc.stdout)
                meta["screencap"] = {
                    "ok": True, "size_bytes": len(proc.stdout),
                    "note": "fallback_realtime",
                }
            else:
                meta["screencap"] = {
                    "ok": False,
                    "reason": f"rc={proc.returncode}, bytes={len(proc.stdout or b'')}",
                }
        except subprocess.TimeoutExpired:
            meta["screencap"] = {"ok": False, "reason": "timeout"}
        except Exception as e:
            meta["screencap"] = {"ok": False, "reason": f"exception: {e}"}

        # ── 2. logcat 最近 N 行 ──
        log_path = out_dir / "logcat.txt"
        try:
            proc2 = subprocess.run(
                ["adb", "-s", device_id, "shell", "logcat", "-d", "-t",
                 str(FORENSICS_LOGCAT_LINES)],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=FORENSICS_TIMEOUT_S,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            text = (proc2.stdout or "")
            if text:
                log_path.write_text(text, encoding="utf-8")
                meta["logcat"] = {"ok": True, "lines": text.count("\n")}
            else:
                meta["logcat"] = {"ok": False,
                                   "reason": f"empty (rc={proc2.returncode})"}
        except subprocess.TimeoutExpired:
            meta["logcat"] = {"ok": False, "reason": "timeout"}
        except Exception as e:
            meta["logcat"] = {"ok": False, "reason": f"exception: {e}"}

        # ── 3. 写 meta.json ──
        try:
            (out_dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("[forensics] write meta.json failed: %s", e)
        logger.info("[forensics] captured task=%s device=%s dir=%s",
                    str(task_id)[:8], str(device_id)[:8], out_dir)
    except Exception as e:
        # 整体兜底, 取证不阻塞 task 状态写入
        logger.warning("[forensics] _do_capture top-level failed: %s", e)


# ── P2.0 (2026-04-30): automation 层即时取证 ──────────────────────────────
#
# 动机：原 capture_forensics 在 task_store.update_task_status('failed') 时触发,
# 但 FB 多步任务可能已跑过 1-2 分钟, 屏幕已经回首页 → 截到的不是失败那一刻。
#
# 解决方案：automation 函数(如 extract_group_members)失败时立即调
# capture_immediate(device_id, step), 把当前 PNG + hierarchy XML 暂存到
# data/forensics/_pending/<device_id>/. 后续 task_store 触发 forensics 时,
# promote_pending_to_task 优先把 pending 快照 mv 到正式目录 (按设备维度收割)。
#
# 设计要点：
# - automation 层不持有 task_id, 故按 device_id 索引 pending
# - hierarchy dump 用 ADB 命令而非 u2.dump_hierarchy() — 更原子, 无 u2 连接依赖
# - 每设备保留最多 N 个 pending (FIFO 滚动), 防磁盘累积
# - 失败完全静默, 不影响 automation 主流程

PENDING_ROOT = FORENSICS_ROOT / "_pending"
PENDING_PER_DEVICE_MAX = 10  # 每设备最多保留多少 pending 快照
PENDING_RETENTION_S = 1800   # pending 快照超过 30 分钟未 promote → 视为孤儿, 清掉


def capture_immediate(device_id: str, step_name: str,
                      hint: str = "", reason: str = "") -> Optional[str]:
    """automation 层即时取证 — 在失败那一刻同步抓 PNG + XML 到 _pending 区。

    返回快照目录名 (形如 'extract_zero_after_enter_20260430T071230Z') 或 None。
    全程吞异常, 不影响调用方主流程。

    与 capture_forensics 区别：
    - 同步执行 (调用方在失败 return 前调, 确保截到失败那一刻)
    - 不需要 task_id (automation 层通常拿不到)
    - 不写 logcat (logcat 由后续 promote 阶段补)
    """
    if not device_id:
        return None
    try:
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        snap_name = f"{_safe_seg(step_name) or 'unknown'}_{ts}"
        out_dir = PENDING_ROOT / _safe_seg(device_id) / snap_name
        out_dir.mkdir(parents=True, exist_ok=True)

        meta: Dict[str, Any] = {
            "device_id": device_id,
            "step": step_name,
            "captured_at_utc": ts,
            "hint": (hint or "")[:300],
            "reason": (reason or "")[:300],
            "pending": True,
        }

        # 1. screencap (同步, 失败时也继续 dump XML)
        try:
            proc = subprocess.run(
                ["adb", "-s", device_id, "exec-out", "screencap", "-p"],
                capture_output=True,
                timeout=FORENSICS_TIMEOUT_S,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0 and proc.stdout and len(proc.stdout) > 100:
                (out_dir / "screencap.png").write_bytes(proc.stdout)
                meta["screencap"] = {"ok": True, "size_bytes": len(proc.stdout)}
            else:
                meta["screencap"] = {
                    "ok": False,
                    "reason": f"rc={proc.returncode}, bytes={len(proc.stdout or b'')}",
                }
        except Exception as e:
            meta["screencap"] = {"ok": False, "reason": f"exception: {type(e).__name__}"}

        # 2. hierarchy dump (uiautomator dump → /sdcard, 再 cat 出来)
        # 比 u2.dump_hierarchy() 更原子: 不依赖 u2 连接, 网络中断也能跑
        #
        # P2.X-4 (2026-04-30) retry: uiautomator dump 在动画中 / 输入法弹起时
        # 偶发返回空文件 (历史: caefd0e0 hierarchy.ok=false reason=empty_or_too_small).
        # 最多重试 _HIERARCHY_DUMP_MAX_RETRIES 次, 每次间隔 0.5s; 同时校验
        # XML 是否以 '<' 开头 (排除 adb 输出错误信息) 且长度 > 200 字节。
        _HIERARCHY_DUMP_MAX_RETRIES = 2
        _HIERARCHY_DUMP_RETRY_DELAY_S = 0.5
        _HIERARCHY_DUMP_MIN_BYTES = 200   # 小于此视为损坏/空文件 (50→200 更严格)
        # 用 device_id 后缀防多设备并发写同一文件
        _dev_tag = re.sub(r"[^a-zA-Z0-9]", "_", (device_id or "dev"))[:12]
        _dump_path = f"/sdcard/_p1_{_dev_tag}.xml"
        try:
            xml = ""
            last_fail_reason = "never_attempted"
            for _attempt in range(_HIERARCHY_DUMP_MAX_RETRIES + 1):
                if _attempt > 0:
                    time.sleep(_HIERARCHY_DUMP_RETRY_DELAY_S)
                try:
                    subprocess.run(
                        ["adb", "-s", device_id, "shell",
                         "uiautomator", "dump", _dump_path],
                        capture_output=True,
                        timeout=min(FORENSICS_TIMEOUT_S, 8),   # 单次最长 8s
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    proc2 = subprocess.run(
                        ["adb", "-s", device_id, "shell", "cat", _dump_path],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace",
                        timeout=FORENSICS_TIMEOUT_S,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    candidate = (proc2.stdout or "").strip()
                    # 有效 XML 必须以 '<' 开头 (排除 adb 错误文字) 且足够大
                    if candidate and candidate.startswith("<") and len(candidate) >= _HIERARCHY_DUMP_MIN_BYTES:
                        xml = candidate
                        break
                    last_fail_reason = (
                        f"empty_or_too_small (attempt={_attempt}, "
                        f"size={len(candidate)}, "
                        f"starts_lt={'<' if candidate else 'empty'})"
                    )
                except Exception as _e:
                    last_fail_reason = f"exception: {type(_e).__name__} (attempt={_attempt})"

            if xml:
                (out_dir / "hierarchy.xml").write_text(xml, encoding="utf-8")
                meta["hierarchy"] = {
                    "ok": True,
                    "size_bytes": len(xml),
                    "attempts": _attempt + 1,
                }
            else:
                meta["hierarchy"] = {
                    "ok": False,
                    "reason": last_fail_reason,
                    "attempts": _HIERARCHY_DUMP_MAX_RETRIES + 1,
                }
        except Exception as e:
            meta["hierarchy"] = {"ok": False, "reason": f"exception: {type(e).__name__}"}

        # 3. meta.json
        try:
            (out_dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

        # 4. FIFO 滚动 — 每设备保留 N 个最新, 超出删最老
        try:
            _trim_pending_per_device(device_id)
        except Exception:
            pass

        logger.info("[forensics-immediate] device=%s step=%s dir=%s",
                    str(device_id)[:8], step_name, snap_name)
        return snap_name
    except Exception as e:
        logger.debug("[forensics-immediate] failed: %s", e)
        return None


def _trim_pending_per_device(device_id: str) -> None:
    """FIFO 滚动: 每设备最多保留 PENDING_PER_DEVICE_MAX 个最新快照。"""
    dev_dir = PENDING_ROOT / _safe_seg(device_id)
    if not dev_dir.is_dir():
        return
    snaps = []
    for d in dev_dir.iterdir():
        if d.is_dir():
            try:
                snaps.append((d.stat().st_mtime, d))
            except Exception:
                continue
    if len(snaps) <= PENDING_PER_DEVICE_MAX:
        return
    snaps.sort(key=lambda x: x[0])  # 升序: 最老在前
    for _, old in snaps[:len(snaps) - PENDING_PER_DEVICE_MAX]:
        shutil.rmtree(old, ignore_errors=True)


def promote_pending_to_task(task_id: str, device_id: str,
                             dst_dir: Path) -> int:
    """task_store 触发 forensics 时, 把该设备 _pending 区所有快照 mv 到正式目录。

    返回 promote 的快照数。已存在同名子目录则跳过。pending 区为空时返回 0。
    """
    if not device_id:
        return 0
    src_dev = PENDING_ROOT / _safe_seg(device_id)
    if not src_dev.is_dir():
        return 0
    moved = 0
    try:
        for snap in list(src_dev.iterdir()):
            if not snap.is_dir():
                continue
            target = dst_dir / snap.name
            if target.exists():
                continue
            try:
                shutil.move(str(snap), str(target))
                moved += 1
            except Exception as e:
                logger.debug("[forensics] promote %s failed: %s", snap.name, e)
    except Exception as e:
        logger.debug("[forensics] promote_pending walk failed: %s", e)
    if moved:
        logger.info("[forensics] promoted %d pending snapshot(s) for task=%s device=%s",
                    moved, str(task_id)[:8], str(device_id)[:8])
    return moved


def cleanup_orphan_pending() -> int:
    """清理 PENDING_RETENTION_S 内未 promote 的孤儿快照。
    server.py startup_cleanup piggyback 调用即可。"""
    if not PENDING_ROOT.is_dir():
        return 0
    cutoff = time.time() - PENDING_RETENTION_S
    cleaned = 0
    try:
        for dev_dir in PENDING_ROOT.iterdir():
            if not dev_dir.is_dir():
                continue
            for snap in list(dev_dir.iterdir()):
                if not snap.is_dir():
                    continue
                try:
                    if snap.stat().st_mtime < cutoff:
                        shutil.rmtree(snap, ignore_errors=True)
                        cleaned += 1
                except Exception:
                    continue
    except Exception as e:
        logger.debug("[forensics] cleanup_orphan_pending failed: %s", e)
    if cleaned:
        logger.info("[forensics] cleanup_orphan_pending removed %d orphans", cleaned)
    return cleaned


def _redact_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """删除 params 中可能含敏感信息的字段, 防 forensics 泄露 token/cookie."""
    if not isinstance(params, dict):
        return {}
    blacklist = {"token", "password", "cookie", "session", "api_key", "secret"}
    out = {}
    for k, v in params.items():
        kl = str(k).lower()
        if any(b in kl for b in blacklist):
            out[k] = "<redacted>"
        else:
            out[k] = v
    return out


def _safe_seg(s: str) -> str:
    """sanitize 路径片段: 仅留字母/数字/`-_.`, 防路径穿越."""
    safe = re.sub(r"[^a-zA-Z0-9_.\-]", "_", str(s))
    return safe[:128] or "_"


def list_forensics(task_id: str) -> List[Dict[str, Any]]:
    """返回该 task_id 下所有时间戳目录的清单 (按时间倒序).

    每项含: ts, files (含 path/size), meta (从 meta.json 解析). 缺 meta 则忽略.
    """
    if not task_id:
        return []
    base = FORENSICS_ROOT / _safe_seg(task_id)
    if not base.is_dir():
        return []
    rows = []
    for ts_dir in sorted(base.iterdir(), reverse=True):
        if not ts_dir.is_dir():
            continue
        # ts 目录名必须能被 _SAFE_NAME_PAT 匹配, 防穿越
        if not _SAFE_NAME_PAT.match(ts_dir.name):
            continue
        files = []
        for f in ts_dir.iterdir():
            if f.is_file() and _SAFE_NAME_PAT.match(f.name):
                try:
                    files.append({"name": f.name, "size": f.stat().st_size})
                except Exception:
                    pass
        meta: Dict[str, Any] = {}
        meta_file = ts_dir / "meta.json"
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        rows.append({"ts": ts_dir.name, "files": files, "meta": meta})
    return rows


def forensics_path(task_id: str, ts: str, filename: str) -> Optional[Path]:
    """安全解析 forensics 单文件路径. 任何路径穿越尝试返 None."""
    for seg in (task_id, ts, filename):
        if not seg or not _SAFE_NAME_PAT.match(_safe_seg(seg)):
            return None
    p = FORENSICS_ROOT / _safe_seg(task_id) / _safe_seg(ts) / _safe_seg(filename)
    try:
        # 解析为绝对路径, 验证仍在 FORENSICS_ROOT 之下
        rp = p.resolve()
        if FORENSICS_ROOT.resolve() not in rp.parents:
            return None
        if not rp.is_file():
            return None
        return rp
    except Exception:
        return None


def startup_cleanup(retention_days: int = FORENSICS_RETENTION_DAYS) -> int:
    """清理 > N 天的 task_id 目录. 返回清理数. server.py 启动时调一次即可."""
    if not FORENSICS_ROOT.is_dir():
        return 0
    cutoff = time.time() - retention_days * 86400
    cleaned = 0
    try:
        for task_dir in FORENSICS_ROOT.iterdir():
            if not task_dir.is_dir():
                continue
            # P2.0: _pending 由 cleanup_orphan_pending 单独管理（30min 而非 7 days）
            if task_dir.name == "_pending":
                continue
            try:
                # 用目录最后修改时间判断 (子目录新增也会刷新)
                if task_dir.stat().st_mtime < cutoff:
                    shutil.rmtree(task_dir, ignore_errors=True)
                    cleaned += 1
            except Exception:
                continue
    except Exception as e:
        logger.warning("[forensics] startup_cleanup error: %s", e)
    if cleaned:
        logger.info("[forensics] startup_cleanup removed %d old dirs (> %d days)",
                    cleaned, retention_days)
    # P2.0: piggyback 清理 _pending 区孤儿快照（超过 30 分钟未 promote）
    try:
        cleanup_orphan_pending()
    except Exception:
        pass
    return cleaned
