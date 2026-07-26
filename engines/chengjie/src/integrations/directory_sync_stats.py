"""目录同步（好友名单 → 通讯录）观测（进程级单例，平台通用）。

背景：``src/integrations/telegram_directory_sync.py::sync_directory_once`` 是个
**6 小时一轮、无人值守**的后台任务，此前只打日志——日志会滚、也没人盯。于是
「同步跑了几轮、拉了多少好友、有没有一直在失败、上次成功是什么时候」在 ops 看板上
一律不可见；名单同步悄悄坏掉（FloodWait 连环撞、会话被顶掉、store 未就绪）只有等
坐席反馈「这个人明明是好友却搜不到」才会暴露。

本模块把每轮同步变成**可观测计数**，按 ``platform:account_id`` 分账号聚合，经
dump()→``/api/workspace/metrics.directory_sync``、dump_prom()→Prometheus
（``directory_sync_*``）读出。判读方式：

- ``last_sync_ts`` 长时间不前进 → 该账号的同步 loop 死了/账号掉线（**主告警项**，
  Prometheus 侧 ``time() - directory_sync_last_ts > 阈值`` 即可）。
- ``failures`` 涨而 ``runs`` 不涨 → RPC 段一直挂（看 ``last_failure_stage``
  区分是通讯录段还是会话段）。
- ``last_contacts`` 骤降 → 名单被清/账号被限，比「坐席说搜不到人」早得多。

**为什么按账号分而不是只看全局**：本机是多账号部署（主账号 + 若干协议号），一个号
挂了而其他号正常时，全局计数照样在涨，只有分账号才看得见。

刻意保留 ``platform`` 形参而非写死 "telegram"：WhatsApp（Baileys HTTP 桥）与 LINE
已有同形状的目录生产者，后续接同一口径时不必再造一张表。

隐私：**只存计数 + 已消毒的 platform/account_id 标识**，绝不存好友昵称、jid、会话
内容。distinct 账号 key 有上限（防脏 account_id 撑爆内存），超限归入 ``__other__``
并计 overflow。

风格对齐 src/web/ui_event_stats.py：无新增依赖，线程安全，进程级单例。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, Optional

# 账号数量级很小（本机个位数），50 足够容纳全部真实账号，超出的必是脏数据
_MAX_KEYS = 50
_MAX_STAGES = 16  # stage 是小枚举（contacts/dialogs），设上限只为挡脏值
# platform 是小写标识符（telegram/whatsapp/line），不合法归 unknown
_PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
# account_id 形态各异（数字号、tg1、+8613800138000…），只做字符白名单 + 截断
_ACCT_SAFE = re.compile(r"[^A-Za-z0-9_\-.+]")
# stage 是失败发生的段名（contacts/dialogs），同 platform 口径
_STAGE_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


def _san_platform(platform: str) -> str:
    p = str(platform or "").strip().lower()
    return p if _PLATFORM_RE.match(p) else "unknown"


def _san_account(account_id: str) -> str:
    a = _ACCT_SAFE.sub("", str(account_id or "").strip())
    if not a:
        return "unknown"
    return a[:48]


def _san_stage(stage: str) -> str:
    s = str(stage or "").strip().lower()
    return s if _STAGE_RE.match(s) else "unknown"


def _nn_int(value: Any) -> int:
    """条数可能被上游传成 None/字符串 → 一律归一为非负 int（观测不该因脏值抛）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


class DirectorySyncStats:
    """目录同步计数（线程安全，进程级）。

    写在 asyncio 任务里（同步 loop）、读在 HTTP 请求线程里（metrics 路由），故用
    ``RLock`` 而非无锁自增。
    """

    __slots__ = (
        "_lock", "_started_at",
        "total_runs", "total_failures", "overflow", "_accounts", "_by_stage",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self.total_runs = 0
        self.total_failures = 0
        self.overflow = 0                     # distinct 账号超限被归入 __other__ 的次数
        self._accounts: Dict[str, Dict[str, Any]] = {}
        self._by_stage: Dict[str, int] = {}   # 失败按段（contacts / dialogs）

    @staticmethod
    def _new_row() -> Dict[str, Any]:
        return {
            "runs": 0, "failures": 0,
            "last_contacts": 0, "last_chats": 0,
            "last_sync_ts": 0.0, "last_failure_ts": 0.0, "last_failure_stage": "",
        }

    def _slot(self, key: str) -> Dict[str, Any]:
        """取账号槽位；超限则归 ``__other__`` 并计 overflow（调用方已持锁）。"""
        row = self._accounts.get(key)
        if row is not None:
            return row
        if len(self._accounts) >= _MAX_KEYS:
            self.overflow += 1
            key = "__other__"
            row = self._accounts.get(key)
            if row is not None:
                return row
        row = self._new_row()
        self._accounts[key] = row
        return row

    def record_sync(self, platform: str, account_id: str, *,
                    contacts: int = 0, chats: int = 0) -> None:
        """记一轮**成功**同步（``contacts``/``chats`` 是 store 实际处理条数）。

        口径：一轮同步只记一次，即便其中某一段失败也算「跑过」——段级失败另由
        ``record_failure`` 计数，两者相加不等于 runs（同一轮可能既有 run 又有
        failure），这是刻意的：runs 回答「loop 还活着吗」，failures 回答「哪一段坏了」。
        """
        key = f"{_san_platform(platform)}:{_san_account(account_id)}"
        now = time.time()
        with self._lock:
            row = self._slot(key)
            row["runs"] += 1
            row["last_contacts"] = _nn_int(contacts)
            row["last_chats"] = _nn_int(chats)
            row["last_sync_ts"] = now
            self.total_runs += 1

    def record_failure(self, platform: str, account_id: str, stage: str = "") -> None:
        """记一次段级失败（``stage``＝"contacts" / "dialogs"）。"""
        key = f"{_san_platform(platform)}:{_san_account(account_id)}"
        st = _san_stage(stage)
        now = time.time()
        with self._lock:
            row = self._slot(key)
            row["failures"] += 1
            row["last_failure_ts"] = now
            row["last_failure_stage"] = st
            self.total_failures += 1
            if st in self._by_stage or len(self._by_stage) < _MAX_STAGES:
                self._by_stage[st] = self._by_stage.get(st, 0) + 1
            else:
                self._by_stage["__other__"] = self._by_stage.get("__other__", 0) + 1

    def dump(self) -> Dict[str, Any]:
        """供 ``/api/workspace/metrics`` 消费。账号按 ``last_sync_ts`` 降序——
        看板第一行就是最近同步的那个号，从没同步过的（ts=0）沉到底部最扎眼。"""
        with self._lock:
            ordered = sorted(self._accounts.items(),
                             key=lambda kv: (-kv[1]["last_sync_ts"], kv[0]))
            return {
                "started_at": self._started_at,
                "last_sync_ts": max((r["last_sync_ts"] for _, r in ordered), default=0.0),
                "total_runs": self.total_runs,
                "total_failures": self.total_failures,
                "overflow": self.overflow,
                "failures_by_stage": dict(sorted(self._by_stage.items())),
                "accounts": {k: dict(r) for k, r in ordered},
            }

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP directory_sync_runs_total Directory sync rounds completed",
                "# TYPE directory_sync_runs_total counter",
                f"directory_sync_runs_total {self.total_runs}",
                "# HELP directory_sync_failures_total Directory sync stage failures",
                "# TYPE directory_sync_failures_total counter",
                f"directory_sync_failures_total {self.total_failures}",
                "# HELP directory_sync_failures_by_stage_total Directory sync failures by stage",
                "# TYPE directory_sync_failures_by_stage_total counter",
            ]
            for s, n in sorted(self._by_stage.items()):
                lines.append(
                    f'directory_sync_failures_by_stage_total{{stage="{_esc(s)}"}} {int(n)}')
            lines += [
                "# HELP directory_sync_contacts Contacts upserted in the last sync round",
                "# TYPE directory_sync_contacts gauge",
            ]
            for k, r in sorted(self._accounts.items()):
                lines.append(
                    f'directory_sync_contacts{{account="{_esc(k)}"}} {int(r["last_contacts"])}')
            lines += [
                "# HELP directory_sync_chats Chat placeholders upserted in the last sync round",
                "# TYPE directory_sync_chats gauge",
            ]
            for k, r in sorted(self._accounts.items()):
                lines.append(
                    f'directory_sync_chats{{account="{_esc(k)}"}} {int(r["last_chats"])}')
            lines += [
                # 陈旧度告警的主指标：time() - directory_sync_last_ts > 阈值 = 该号同步死了
                "# HELP directory_sync_last_ts Epoch seconds of the last successful sync",
                "# TYPE directory_sync_last_ts gauge",
            ]
            for k, r in sorted(self._accounts.items()):
                lines.append(
                    f'directory_sync_last_ts{{account="{_esc(k)}"}} {r["last_sync_ts"]:.0f}')
            lines += [
                "# HELP directory_sync_last_failure_ts Epoch seconds of the last stage failure",
                "# TYPE directory_sync_last_failure_ts gauge",
            ]
            for k, r in sorted(self._accounts.items()):
                lines.append(
                    f'directory_sync_last_failure_ts{{account="{_esc(k)}"}} '
                    f'{r["last_failure_ts"]:.0f}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self.total_runs = 0
            self.total_failures = 0
            self.overflow = 0
            self._accounts.clear()
            self._by_stage.clear()


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[DirectorySyncStats] = None
_LOCK = threading.Lock()


def get_directory_sync_stats() -> DirectorySyncStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = DirectorySyncStats()
    return _SINGLETON


__all__ = ["DirectorySyncStats", "get_directory_sync_stats"]
