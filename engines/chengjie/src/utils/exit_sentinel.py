"""进程退出可观测 — 哨兵文件 + 退出原因记录（2026-07-12 无痕死亡排障配套）。

背景：当天 22:40 生产 main.py 无痕消失——err.log 干净、app.log 无停止日志，
死因（OOM/外力 taskkill /F/崩溃）无从判断。Windows 上 ``TerminateProcess``
类死法（taskkill /F、OOM）**任何进程内钩子都捕获不到**，故用两层互补：

- **哨兵文件**（兜底一切死法）：启动时若发现残留哨兵 = 上次非正常死亡，
  把上次的 pid/启动时间/存活时长记 WARNING 进日志（「死过、死前活了多久」
  从此有据）；随后写入本次哨兵；正常退出（atexit）删除。
- **可捕获路径记原因**：atexit 记「clean exit」；SIGINT/SIGBREAK/SIGTERM
  信号记「收到信号 N」后链回原 handler（不改变既有退出行为）；
  ``faulthandler`` 把段错误/致命异常的 traceback 落到独立文件
  （logs/fatal_traceback.log，append 模式，事后可翻）。

零依赖、幂等、任何失败绝不影响启动/退出主流程。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_installed = False


def check_previous_exit(sentinel: Path) -> Optional[dict]:
    """启动时检查上次会话是否干净退出。

    返回 None=上次干净（或首启）；返回 dict=上次非正常死亡的现场
    （pid/started_at/存活时长），并已把 WARNING 记入日志。

    存活时长口径（2026-07-29 修正）：优先用哨兵内容里的心跳时间 ``beat_at``
    （install 的心跳线程周期回写，量的是**真实存活**），无心跳字段（legacy）
    回落文件 mtime。历史缺陷：旧版只在启动写一次哨兵 → mtime≈started_at →
    「存活约 0 分钟」恒成立且毫无信息量；时间戳被快照/恢复类工具回拨时还会
    算出**负数**分钟。现负值一律判「时间戳异常」不再报负数。
    """
    try:
        if not sentinel.exists():
            return None
        raw = sentinel.read_text(encoding="utf-8")
        info = json.loads(raw) if raw.strip() else {}
    except Exception:
        info = {}
    started = float(info.get("started_at") or 0)
    beat = float(info.get("beat_at") or 0)
    mtime = 0.0
    try:
        mtime = sentinel.stat().st_mtime
    except OSError:
        pass
    last_seen = beat or mtime
    raw_lived = (last_seen - started) if (started and last_seen) else 0.0
    anomaly = raw_lived < 0
    lived = max(0.0, raw_lived)
    lived_txt = ("无法判定（时间戳异常，疑被快照/恢复类工具改写）" if anomaly
                 else f"约 {lived / 60:.0f} 分钟")
    logger.warning(
        "检测到上次会话非正常死亡（哨兵残留）：pid=%s 启动于 %s 存活%s。"
        "无痕死法多为 taskkill /F / OOM / 断电——如反复出现请查系统事件日志与内存水位。",
        info.get("pid"),
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)) if started else "?",
        lived_txt,
    )
    return {"pid": info.get("pid"), "started_at": started,
            "lived_sec": lived, "ts_anomaly": anomaly}


def _write_sentinel(sentinel: Path, started_at: float) -> None:
    """写/回写哨兵（含心跳时间）。任何失败绝不外抛。"""
    try:
        sentinel.write_text(json.dumps({
            "pid": os.getpid(),
            "started_at": started_at,
            "beat_at": time.time(),
        }), encoding="utf-8")
    except Exception:
        logger.debug("哨兵写入失败（已忽略）", exc_info=True)


def install(sentinel_path: str = "logs/run_sentinel.json",
            fatal_log_path: str = "logs/fatal_traceback.log",
            heartbeat_sec: float = 60.0) -> Optional[dict]:
    """安装退出可观测（返回上次非正常死亡现场 dict / None）。幂等。

    调用时机：日志配置完成后（WARNING 能落 app.log）。
    ``heartbeat_sec``>0 时启动守护心跳线程周期回写 ``beat_at``——被硬杀后
    下次启动能报出**真实存活时长**（旧版只写一次，恒报 ~0 分钟）。0=关闭。
    """
    global _installed
    if _installed:
        return None
    _installed = True
    sentinel = Path(sentinel_path)
    started_at = time.time()
    prev = None
    try:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        prev = check_previous_exit(sentinel)
        _write_sentinel(sentinel, started_at)
    except Exception:
        logger.debug("哨兵写入失败（已忽略）", exc_info=True)

    # 心跳停止旗标：正常退出路径先置旗再删文件，防「删除后又被心跳线程复活
    # → 下次启动误报非正常死亡」的竞态。
    import threading
    _stop_beat = threading.Event()

    def _beat_loop():
        while not _stop_beat.wait(max(5.0, float(heartbeat_sec))):
            if _stop_beat.is_set():
                break
            _write_sentinel(sentinel, started_at)

    if heartbeat_sec and heartbeat_sec > 0:
        try:
            threading.Thread(
                target=_beat_loop, name="exit-sentinel-beat", daemon=True).start()
        except Exception:
            logger.debug("哨兵心跳线程启动失败（已忽略）", exc_info=True)

    def _clean_exit():
        _stop_beat.set()
        try:
            logger.info("进程正常退出（atexit），哨兵已清理")
        except Exception:
            pass
        try:
            sentinel.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(_clean_exit)

    # faulthandler：段错误/致命错误的 traceback 落独立文件（append，跨次追溯）
    try:
        import faulthandler
        f = open(fatal_log_path, "a", encoding="utf-8")   # noqa: SIM115 常驻句柄
        f.write(f"\n=== session pid={os.getpid()} started "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        f.flush()
        faulthandler.enable(file=f, all_threads=True)
    except Exception:
        logger.debug("faulthandler 启用失败（已忽略）", exc_info=True)

    # 信号路径：记录原因后链回原 handler（不改变既有退出行为）。
    # Windows 语义诚实声明：SIGINT=Ctrl+C、SIGBREAK=Ctrl+Break/窗口关闭；
    # SIGTERM 名义注册（taskkill 非 /F 走 WM_CLOSE 实际到不了）；/F 无解靠哨兵。
    def _chain(sig_name, prev_handler):
        def _h(signum, frame):
            try:
                logger.warning("收到 %s（signum=%s），进程即将退出", sig_name, signum)
            except Exception:
                pass
            if callable(prev_handler):
                prev_handler(signum, frame)
            elif prev_handler == signal.SIG_DFL:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum) if hasattr(os, "kill") else sys.exit(1)
        return _h

    for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
        try:
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            prev_h = signal.getsignal(sig)
            signal.signal(sig, _chain(name, prev_h))
        except Exception:
            logger.debug("信号 %s 挂钩失败（已忽略）", name, exc_info=True)

    return prev
