"""前端「哑按钮」运行时错误观测（进程级单例）。

背景：内联 `on*="fn()"` 引用了未挂 window 的函数（IIFE 内漏挂 / 拼写错 / 动态点属性拼接
被当减法），点击抛 `ReferenceError` 静默失效。静态门禁（test_*_inline_handlers_*、
test_template_dynamic_dot_access）挡「入库前」，运行时兜底守卫（unified_inbox +
_rpa_shared_scripts）弹红条给用户看——但**后台此前无感知**：哪页哪函数点崩、多频，全靠用户上报。

本模块把这些前端错误变成**可观测计数**：dead-click 守卫捕获后 beacon 到
`POST /api/telemetry/frontend-error`，此处按 (page, fn, type) 累计，经 dump()→
`/api/workspace/metrics.frontend_errors`、dump_prom()→Prometheus，闭合「测不到→线上也能被发现」。

风格对齐 src/ai/outbound_translation_stats.py：无新增依赖，线程安全，进程级单例。
**只存计数 + 已消毒的 page 路径 + 标识符名**，绝不存完整报文/URL 查询串/堆栈（防 PII/敏感串泄漏）。
distinct key 有上限（防脏数据/刷量把内存撑爆），超限归入 `__other__` 并计 overflow。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, Optional

_MAX_KEYS = 100  # by_page / by_fn 各自最多保留的 distinct key 数
_IDENT_RE = re.compile(r"^[A-Za-z_$][\w$]*$")
# page 路径只保留合法 URL path 字符（丢查询串/hash/异常内容），截断防超长
_PATH_SAFE = re.compile(r"[^A-Za-z0-9/_\-.:]")
_KNOWN_TYPES = {
    "ReferenceError", "TypeError", "SyntaxError", "RangeError", "Error",
    # apiFetch 网络层失败的语义类型（非 JS Error.name）：超时 vs 硬网络错分开看
    # ——之前全被折叠成 "Error"，「坐席在超时还是在断网」这层信号被丢掉了。
    "timeout", "neterr",
    # 账号视角/深链「意图落空」语义类型（2026-07-29 P9 补录）：此前不在白名单，
    # 全被折叠成 "Error"——by_type 里根本看不见这几类，只能靠 fn 猜。
    # scoped_fail=账号 scoped 取数失败；dead_intent=查看会话目标账号不在册；
    # conv_not_found=?conv= 深链经窗口刷新+scoped 救援后仍找不到目标会话。
    "scoped_fail", "dead_intent", "conv_not_found",
    # 共享组件写请求失败分型（2026-07-31 人设切换事故补录）：copilot 组件把
    # 换绑等写操作的 HTTP 失败按状态码上报——http_403_csrf 一旦出现即「某个
    # 宿主环境的写通道又断了」，比等用户截图早两周。
    "http_401", "http_403", "http_403_csrf", "http_404", "http_409", "http_5xx",
    # B54 输入框焦点自愈（实施68 P1-16，2026-08-26）：_focus_selfheal.html 的
    # 自愈触发计数——fn 带 focus_selfheal_{refocus|shell_fix|window_focus|fail}
    # 分级，type 归本类；累计分布定位「还有哪页在丢焦点」。
    "focus_selfheal",
}


def _san_page(page: str) -> str:
    p = str(page or "").split("?", 1)[0].split("#", 1)[0].strip()
    p = _PATH_SAFE.sub("", p)
    if not p:
        return "unknown"
    return p[:80]


def _san_fn(fn: str) -> str:
    f = str(fn or "").strip()
    return f[:64] if _IDENT_RE.match(f) else "unknown"


_DIGITS_RE = re.compile(r"\d{2,}")


def _san_endpoint(ep: str) -> str:
    """请求端点消毒：只留 path（丢查询串/hash），数字串掩码 ``<n>``（控基数：
    /api/inbox/threads/12345 与 /54321 归并为一键），非法/空 → ""（可选维度）。"""
    p = str(ep or "").split("?", 1)[0].split("#", 1)[0].strip()
    # 绝对 URL → 摘 path（beacon 侧已尽量只送 path，此处兜底）
    if "://" in p:
        p = "/" + p.split("://", 1)[1].split("/", 1)[-1] if "/" in p.split("://", 1)[1] else "/"
    p = _PATH_SAFE.sub("", p)
    if not p or not p.startswith("/"):
        return ""
    return _DIGITS_RE.sub("<n>", p)[:100]


def _san_type(etype: str) -> str:
    t = str(etype or "").strip()
    return t if t in _KNOWN_TYPES else "Error"


#: 「脚本 bug」类型（与 _boot_error_guard 的判定同口径）：ReferenceError＝死代码/作用域
#: 错位、SyntaxError＝模板半保存热更上生产。网络型/HTTP 型不算——那是环境不是代码。
SCRIPT_BUG_TYPES = frozenset({"ReferenceError", "SyntaxError"})


class FrontendErrorStats:
    """前端运行时错误计数（线程安全，进程级）。"""

    __slots__ = (
        "_lock", "_started_at", "_last_ts",
        "total", "overflow", "_by_fn", "_by_page", "_by_type", "_by_endpoint",
        "_script_bugs", "_script_last_ts",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_ts = 0.0
        self.total = 0
        self.overflow = 0                     # distinct key 超限被归入 __other__ 的次数
        self._by_fn: Dict[str, int] = {}
        self._by_page: Dict[str, int] = {}
        self._by_type: Dict[str, int] = {}
        # 网络层失败（apiFetch beacon）附带的**已消毒**请求端点——2026-07-29 事故
        # 教训：单次会话 136 次 apiFetch 错误只有 page/fn/type 三维，坏的是哪个
        # 接口完全无从归因。可选维度：dead-click 守卫（无端点语义）不送即不计。
        self._by_endpoint: Dict[str, int] = {}
        # 脚本 bug 三元组 "page type fn"（2026-09-15 `_psnArRender` 事故沉淀）：by_page /
        # by_fn / by_type 三张独立表拼不回「哪页哪函数抛 ReferenceError」——看门狗要按
        # **三元组**判「新出现的坏符号」并直接把可点开的定位送进告警。只收 SCRIPT_BUG_TYPES。
        self._script_bugs: Dict[str, int] = {}
        self._script_last_ts = 0.0

    @staticmethod
    def _bump(d: Dict[str, int], key: str) -> bool:
        """key 已存在或未超限 → +1 返回 False；超限 → 归 __other__ 返回 True（overflow）。"""
        if key in d or len(d) < _MAX_KEYS:
            d[key] = d.get(key, 0) + 1
            return False
        d["__other__"] = d.get("__other__", 0) + 1
        return True

    def record(self, *, page: str = "", fn: str = "", etype: str = "",
               endpoint: str = "") -> None:
        p, f, t = _san_page(page), _san_fn(fn), _san_type(etype)
        ep = _san_endpoint(endpoint)
        with self._lock:
            self.total += 1
            self._last_ts = time.time()
            of = self._bump(self._by_page, p)
            of = self._bump(self._by_fn, f) or of
            if ep:
                of = self._bump(self._by_endpoint, ep) or of
            self._by_type[t] = self._by_type.get(t, 0) + 1  # type 是小枚举，不设上限
            if t in SCRIPT_BUG_TYPES:
                self._script_last_ts = self._last_ts
                of = self._bump(self._script_bugs, f"{p} {t} {f}") or of
            if of:
                self.overflow += 1

    def script_bugs_snapshot(self) -> Dict[str, Any]:
        """看门狗口径：``{"items": {"page type fn": n}, "total": N, "last_ts": ts}``。"""
        with self._lock:
            return {
                "items": dict(sorted(self._script_bugs.items(), key=lambda kv: (-kv[1], kv[0]))),
                "total": sum(self._script_bugs.values()),
                "last_ts": self._script_last_ts,
            }

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_ts,
                "total": self.total,
                "overflow": self.overflow,
                "script_bugs": dict(sorted(
                    self._script_bugs.items(), key=lambda kv: (-kv[1], kv[0]))),
                "script_last_ts": self._script_last_ts,
                "by_type": dict(sorted(self._by_type.items())),
                "by_fn": dict(sorted(self._by_fn.items(), key=lambda kv: (-kv[1], kv[0]))),
                "by_page": dict(sorted(self._by_page.items(), key=lambda kv: (-kv[1], kv[0]))),
                "by_endpoint": dict(sorted(
                    self._by_endpoint.items(), key=lambda kv: (-kv[1], kv[0]))),
            }

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP frontend_errors_total Frontend dead-click runtime errors (denominator)",
                "# TYPE frontend_errors_total counter",
                f"frontend_errors_total {self.total}",
                "# HELP frontend_errors_by_type_total Frontend runtime errors by JS error type",
                "# TYPE frontend_errors_by_type_total counter",
            ]
            for t, n in sorted(self._by_type.items()):
                lines.append(f'frontend_errors_by_type_total{{type="{_esc(t)}"}} {int(n)}')
            lines += [
                "# HELP frontend_errors_by_page_total Frontend runtime errors by page path",
                "# TYPE frontend_errors_by_page_total counter",
            ]
            for p, n in sorted(self._by_page.items()):
                lines.append(f'frontend_errors_by_page_total{{page="{_esc(p)}"}} {int(n)}')
            lines += [
                "# HELP frontend_errors_by_fn_total Frontend runtime errors by referenced symbol",
                "# TYPE frontend_errors_by_fn_total counter",
            ]
            for f, n in sorted(self._by_fn.items()):
                lines.append(f'frontend_errors_by_fn_total{{fn="{_esc(f)}"}} {int(n)}')
            lines += [
                "# HELP frontend_errors_by_endpoint_total Frontend network errors by sanitized endpoint",
                "# TYPE frontend_errors_by_endpoint_total counter",
            ]
            for e, n in sorted(self._by_endpoint.items()):
                lines.append(
                    f'frontend_errors_by_endpoint_total{{endpoint="{_esc(e)}"}} {int(n)}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self.total = 0
            self.overflow = 0
            self._by_fn.clear()
            self._by_page.clear()
            self._by_type.clear()
            self._by_endpoint.clear()
            self._script_bugs.clear()
            self._script_last_ts = 0.0
            self._last_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[FrontendErrorStats] = None
_LOCK = threading.Lock()


def get_frontend_error_stats() -> FrontendErrorStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = FrontendErrorStats()
    return _SINGLETON


__all__ = ["FrontendErrorStats", "get_frontend_error_stats"]
