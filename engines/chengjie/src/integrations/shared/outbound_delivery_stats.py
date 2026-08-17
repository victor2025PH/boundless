"""出站投递结果观测（进程级单例，跨平台通用）。

背景：worker 的发送失败此前**只落日志**。`{"delivered": False, "error": "forbidden"}`
这种结构化失败被调用方各自处理掉，没有任何地方回答「这个号这小时发出去几条、被拒几条、
为什么被拒」。Discord 接入把这个洞照亮了——Bot 不能主动私信陌生人，403 是**日常业务
信号**（社群覆盖不够）而不是故障，可当时除了翻 app.log 没有第二种看法。但这不是
Discord 的问题：WhatsApp 会话过期、Telegram PEER_ID_INVALID、Messenger 24h 窗口关闭，
每个平台都有自己的「正常的失败」，全都同样不可见。

**覆盖面**：本仓的出站**不是一条栈，是四条并行的栈**，四条都已接：

1. ``AccountOrchestrator.send``/``send_media``——编排器托管的协议号与官方 API 号；
2. ``channel_adapters.send_via_adapters`` 的适配器回落分支——编排器不拥有该账号时；
3. **Telegram A 线** Pyrogram 直发（``client/sender.py``、``client/voice_sender.py``）；
4. **各 RPA runner 自有的 adb 发送循环**（LINE / WhatsApp / Messenger）与
   **官方 webhook 的 legacy 自答分支**（``official_pipeline.enabled=false``）。

**去重＝最外层占坑**（``claim_send()``，本模块最关键的一处设计）。上面四条栈**互相嵌套**，
不是并列的：编排器 → 官方 API worker → ``fb_send_message`` 是同一条调用栈上的三层，
A 线的 ``_send_text_guarded`` 既被纯 A 线调用、也被编排器托管的 companion worker 调用。
逐层插桩必然重复计数。故路由层（编排器 / 适配器）进入真正的 worker 调用前用
``with claim_send():`` 占坑，**内层记录点带 ``only_if_outermost=True`` 自动让位**。
选「外层赢」而非「内层赢」有两个决定性理由：外层是**一条逻辑消息一次记录**（内层有重试
——Messenger 的 adb 注入最多重试 4 次、LINE/WA 每段重试 1 次，按内层计会把失败率算成
重试次数的函数）；且外层看到的正是**调用方看到的结果**，看板与调用方不会各说各话。

占坑用 ``contextvars``，作用域**刻意收得很紧**（只裹住那一次 ``await worker.send(...)``）：
contextvar 会随 ``create_task`` 复制进子任务，裹太宽会让占坑期间 spawn 的后台发送任务
永久带着「已被占坑」而从此不计数。

⚠ 仍不计的：**Node 侧微服务自身重试**（messenger-web / whatsapp-baileys 是独立进程，
进程内计数器写不到；它们的失败经 HTTP 返回给编排器时已被第 1 条记下）。

**入队 ≠ 已送达**（``queued`` 单列）：RPA 适配器返回 ``{"delivered": True, "queued": True}``
——那只是**交给了队列**，手机可能关机数小时。把它算成 sent 会让看板在链路彻底不通时
显示 100% 成功。故 ``queued`` 是独立计数，**不进 attempts/sent**；那条消息真正的成败由
RPA 物理出口（``_pace_and_send`` / ``_send_reply_with_retry``）稍后记账。于是
「queued 一直涨、对应平台 attempts 不动」＝**队列卡住了**，这本身就是一个想要的信号。

计数器与 Stage M 护栏（Kill-Switch / 反封号闸门）同址是刻意的：护栏拦下的消息客户
同样没收到，它必须和平台拒收出现在同一张表里。

**成功也计**：只报失败数的看板是骗人的——40 次失败，分母是 40 还是 40000，是「链路
断了」和「正常摩擦」两种完全不同的处境。所以这里记的是三态 sent/failed/blocked，
看板给的是**失败率**。

风格对齐 ``src/web/frontend_error_stats.py``：无新增依赖、线程安全、进程级单例、
distinct key 有上限（超限归 ``__other__``）。
"""

from __future__ import annotations

import functools
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, Optional, Tuple

#: by_reason / by_platform 各自最多保留的 distinct key 数（超限归 __other__）
_MAX_KEYS = 60
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: 同义 error token 归并——同一个失败在不同 worker 里叫不同名字（历史遗留），
#: 不归并的话看板上会出现「too_large 3 / media_too_large 5」这种要人脑加法的行。
_ALIASES = {
    "too_large": "media_too_large",
    "file_too_large": "media_too_large",
    "chat_not_found": "peer_invalid",
    "peer_id_invalid": "peer_invalid",
    "user_deactivated": "peer_invalid",
    "not_found": "peer_invalid",
    "empty": "empty_text",
    "empty_after_split": "empty_text",
    "nothing_to_split": "empty_text",
    "worker_unavailable": "no_worker",
    "unavailable": "no_worker",
}

#: 异常消息 → 归类。顺序敏感：先专后泛（"connection timeout" 该算 timeout 不算 network）。
_EXC_PATTERNS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("timeout", ("timeout", "timed out")),
    ("forbidden", ("forbidden", "403", "50007", "50013")),
    ("rate_limited", ("rate limit", "429", "too many requests", "flood")),
    ("peer_invalid", ("peer_id_invalid", "user_deactivated", "chat not found",
                      "unknown user", "unknown channel", "404")),
    ("media_too_large", ("payload too large", "413", "40005", "file too large")),
    ("network", ("connection", "unreachable", "dns", "ssl", "socket",
                 "getaddrinfo", "econnreset")),
)


def _san(token: str, fallback: str = "other") -> str:
    """归一化成安全的小写标识符；非法/空 → fallback。"""
    t = str(token or "").strip().lower().replace("-", "_").replace(" ", "_")
    t = re.sub(r"[^a-z0-9_]", "", t)[:40]
    if not t or not _IDENT_RE.match(t):
        return fallback
    return _ALIASES.get(t, t)


def _reason_from_text(msg: str) -> str:
    """自由文本 → 小枚举里的原因；都不中返回 ""。"""
    low = str(msg or "").lower()
    for reason, needles in _EXC_PATTERNS:
        if any(n in low for n in needles):
            return reason
    return ""


def classify_exception(exc: BaseException) -> str:
    """异常 → 归类原因。

    异常字符串是**真正无界的**（含 peer id、文件路径、平台原文），绝不能直接当维度值。
    先按关键词归进小枚举；都不中就退到 ``exc:<类名>``——类名由代码决定、天然有界，
    比一律折叠成 "other" 保住了归因能力（"other 涨了 5000" 等于没说）。
    """
    return (_reason_from_text(f"{type(exc).__name__} {exc}")
            or _san(f"exc_{type(exc).__name__}", fallback="exc_unknown"))


#: 官方 API 的 HTTP 状态码 → 有界原因。状态码是平台契约的一部分，比错误文案稳定。
_HTTP_REASONS = {
    400: "bad_request", 401: "auth_failed", 403: "forbidden", 404: "peer_invalid",
    408: "timeout", 413: "media_too_large", 429: "rate_limited",
    500: "platform_error", 502: "platform_error", 503: "platform_error",
    504: "timeout",
}
_HTTP_RE = re.compile(r"\bHTTP\s+(\d{3})\b", re.I)


def classify_ok_result(out: Any) -> Tuple[str, str]:
    """官方 API 辅助函数的 ``{"ok": bool, "error": str}`` → ``(outcome, reason)``。

    与 :func:`classify_result` 分开是因为形状与语义都不同，硬塞进一个函数会让两边
    都难读。返回 ``("", "")`` 表示**不该记账**——空文本跳过（``skipped: empty``）
    根本没有消息上过线，记成 sent 会用非事件稀释分母。

    原因取值优先级：``error_kind``（``official_send_error`` 已按平台错误码归好类，
    如 ``window_expired``）→ HTTP 状态码 → 文本关键词 → ``platform_error``。
    **刻意不用错误原文**：那里面是 Meta/Zalo 的整段 JSON，无界。
    """
    if not isinstance(out, dict):
        return "sent", ""
    err = str(out.get("error") or "")
    if out.get("ok"):
        data = out.get("data")
        if isinstance(data, dict) and data.get("skipped"):
            return "", ""
        return "sent", ""
    if not err:
        # RPA 的 `_pace_and_send` 把每段结果放在 parts 里，顶层只有 ok——而分段的
        # error（input_field_not_found / text_inject_fail）**正是** RPA 最该看的东西：
        # 它告诉你是选择器坏了还是设备掉了，丢掉就只剩一个没有归因的 platform_error。
        for part in (out.get("parts") or []):
            if isinstance(part, dict) and part.get("error"):
                err = str(part["error"])
                break
    if err.startswith("kill_switch") or err.startswith("send_gate"):
        return "blocked", _san(err.split(":", 1)[0])
    kind = out.get("error_kind")
    if kind:
        return "failed", _san(str(kind), fallback="platform_error")
    m = _HTTP_RE.search(err)
    if m:
        return "failed", _HTTP_REASONS.get(int(m.group(1)), "platform_error")
    if _reason_from_text(err):
        return "failed", _reason_from_text(err)
    # RPA 的 error 是代码里的字面量（有界、可读），直接当原因用比 platform_error 有信息。
    tok = _san(err, fallback="")
    return "failed", (tok or "platform_error")


def classify_result(res: Any) -> Tuple[str, str]:
    """worker 返回值 → ``(outcome, reason)``，outcome ∈ sent|failed|blocked|queued。

    与编排器判「要不要回写收件箱」**同口径**：只有显式 ``delivered=False`` 才算失败，
    非 dict 返回值按成功（老 worker 契约）。两处口径若分叉，会出现「看板说失败、
    线程里却有气泡」的自相矛盾。

    唯一的例外是 ``queued``：RPA 适配器为了让收件箱立刻出气泡而返回 ``delivered=True``，
    但那条消息此刻只在队列里。按 sent 记会让「手机关机、队列积压」显示成 100% 成功。
    """
    if not isinstance(res, dict):
        return "sent", ""
    if res.get("queued"):
        return "queued", ""
    if res.get("delivered", True) is not False:
        return "sent", ""
    blocked = res.get("blocked")
    if blocked:
        # send_blocked 的 reason 形如 kill_switch:<scope> / send_gate:<reason>——
        # 只取族名，冒号后的细节（scope/账号）会把维度基数炸开。
        return "blocked", _san(str(blocked).split(":", 1)[0])
    return "failed", _san(str(res.get("error") or ""), fallback="unspecified")


class OutboundDeliveryStats:
    """出站投递三态计数（线程安全，进程级）。"""

    #: 记账三态 + 一个「只是交给了队列」的中间态（不进 attempts，见模块 docstring）
    OUTCOMES = ("sent", "failed", "blocked", "queued")

    __slots__ = ("_lock", "_started_at", "_last_fail_ts", "_last_fail",
                 "sent", "failed", "blocked", "queued", "overflow",
                 "_by_platform", "_by_reason", "_by_kind")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_fail_ts = 0.0
        self._last_fail: Dict[str, Any] = {}
        self.sent = 0
        self.failed = 0
        self.blocked = 0
        self.queued = 0
        self.overflow = 0
        # platform → {sent, failed, blocked}
        self._by_platform: Dict[str, Dict[str, int]] = {}
        # (platform, reason) → n；失败与拦截各自的原因分布，看板按平台展开
        self._by_reason: Dict[Tuple[str, str], int] = {}
        self._by_kind: Dict[str, Dict[str, int]] = {}

    def record(self, platform: str, *, kind: str = "text",
               outcome: str = "sent", reason: str = "") -> None:
        p = _san(platform, fallback="unknown")
        k = "media" if str(kind).lower() == "media" else "text"
        oc = outcome if outcome in self.OUTCOMES else "failed"
        with self._lock:
            setattr(self, oc, getattr(self, oc) + 1)
            of = False
            for bucket, key in ((self._by_platform, p), (self._by_kind, k)):
                row = bucket.get(key)
                if row is None:
                    if len(bucket) >= _MAX_KEYS:
                        key, row = "__other__", bucket.get("__other__")
                        of = True
                    if row is None:
                        row = dict.fromkeys(self.OUTCOMES, 0)
                        bucket[key] = row
                row[oc] += 1
            if oc in ("failed", "blocked"):
                r = _san(reason, fallback="unspecified")
                rk = (p, r)
                if rk not in self._by_reason and len(self._by_reason) >= _MAX_KEYS:
                    # 溢出桶必须是**全局唯一**的一个。曾写成 (p, "__other__")：平台名
                    # 本身失控时（脏数据/新平台刷进来）每个平台各造一个溢出桶，上限
                    # 形同虚设——正是这个上限要防的那种失控。
                    rk, of = ("__other__", "__other__"), True
                self._by_reason[rk] = self._by_reason.get(rk, 0) + 1
                if oc == "failed":
                    self._last_fail_ts = time.time()
                    self._last_fail = {"platform": p, "reason": r, "kind": k,
                                       "ts": self._last_fail_ts}
            if of:
                self.overflow += 1

    def _totals(self) -> Dict[str, Any]:
        attempts = self.sent + self.failed + self.blocked
        return {
            "attempts": attempts,
            "sent": self.sent,
            "failed": self.failed,
            "blocked": self.blocked,
            # 刻意不进 attempts：入队只是交接，成败由 RPA 物理出口稍后记。
            # 「queued 涨而该平台 attempts 不动」＝队列卡住，是个想要的信号。
            "queued": self.queued,
            # 失败率的分母刻意含 blocked：护栏拦下的那条**客户同样没收到**，
            # 从「有多少该说的话没说出去」的角度看，它和平台拒收是一回事。
            "failure_rate": round((self.failed + self.blocked) / attempts, 4)
            if attempts else 0.0,
        }

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            by_reason: Dict[str, Dict[str, int]] = {}
            for (p, r), n in self._by_reason.items():
                by_reason.setdefault(p, {})[r] = n
            return {
                "started_at": self._started_at,
                "overflow": self.overflow,
                **self._totals(),
                "by_platform": {k: dict(v) for k, v in sorted(self._by_platform.items())},
                "by_kind": {k: dict(v) for k, v in sorted(self._by_kind.items())},
                "by_reason": {p: dict(sorted(rs.items(), key=lambda kv: (-kv[1], kv[0])))
                              for p, rs in sorted(by_reason.items())},
                "last_failure": dict(self._last_fail),
            }

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP outbound_delivery_total Outbound send attempts by platform and outcome",
                "# TYPE outbound_delivery_total counter",
            ]
            for p, row in sorted(self._by_platform.items()):
                for oc in self.OUTCOMES:
                    lines.append(
                        f'outbound_delivery_total{{platform="{_esc(p)}",'
                        f'outcome="{oc}"}} {int(row.get(oc, 0))}')
            lines += [
                "# HELP outbound_delivery_failure_total Outbound failures by platform and reason",
                "# TYPE outbound_delivery_failure_total counter",
            ]
            for (p, r), n in sorted(self._by_reason.items()):
                lines.append(
                    f'outbound_delivery_failure_total{{platform="{_esc(p)}",'
                    f'reason="{_esc(r)}"}} {int(n)}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self.sent = self.failed = self.blocked = self.queued = 0
            self.overflow = 0
            self._by_platform.clear()
            self._by_reason.clear()
            self._by_kind.clear()
            self._last_fail = {}
            self._last_fail_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[OutboundDeliveryStats] = None
_LOCK = threading.Lock()


def get_outbound_delivery_stats() -> OutboundDeliveryStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = OutboundDeliveryStats()
    return _SINGLETON


#: 「本次发送已被外层路由层认领」——见模块 docstring 的去重段。
_CLAIMED: ContextVar[bool] = ContextVar("outbound_send_claimed", default=False)


@contextmanager
def claim_send() -> Iterator[None]:
    """路由层占坑：作用域内的所有内层记录点让位给调用方。

    只裹住**那一次真正的 worker/adapter 调用**，别裹整个函数——contextvar 会随
    ``create_task`` 复制进子任务，裹太宽会让占坑期间 spawn 的后台发送任务永久
    带着「已被占坑」，从此在看板上消失。
    """
    tok = _CLAIMED.set(True)
    try:
        yield
    finally:
        _CLAIMED.reset(tok)


def send_claimed() -> bool:
    """当前调用栈上是否已有外层路由层认领了这次发送。"""
    return _CLAIMED.get()


def record_send_result(platform: str, *, kind: str = "text",
                       res: Any = None, exc: Optional[BaseException] = None,
                       outcome: str = "", reason: str = "",
                       only_if_outermost: bool = False) -> None:
    """一行式记账：**永不抛**（观测绝不能弄坏发送）。

    三种喂法（优先级从高到低）：``exc=`` 异常、``outcome=`` 直接给结论（调用点已经
    知道自己在哪个分支，如 A 线的「客户端未初始化」）、``res=`` 交给
    :func:`classify_result` 推断。给了 ``outcome`` 就不再看 ``res``。

    ``only_if_outermost=True`` 供**内层**发送出口使用（A 线 / RPA adb / 官方 API 的
    HTTP 辅助函数）——它们既可能被编排器调用、也可能被各自的旁路直接调用，带上这个
    参数就只在「没有外层认领」时记账，两条路径各记一次且不重复。
    """
    try:
        if only_if_outermost and _CLAIMED.get():
            return
        if exc is not None:
            get_outbound_delivery_stats().record(
                platform, kind=kind, outcome="failed",
                reason=classify_exception(exc))
            return
        if not outcome:
            outcome, reason = classify_result(res)
        get_outbound_delivery_stats().record(
            platform, kind=kind, outcome=outcome, reason=reason)
    except Exception:  # noqa: BLE001 - 观测失败必须静默
        pass


def record_bool_send(platform: str, ok: Any, *, kind: str = "text",
                     reason: str = "", only_if_outermost: bool = True) -> None:
    """给「只返回 True/False」的老出口用（A 线 send_photo、LINE push…）。

    默认 ``only_if_outermost=True``：这些函数**全都是**内层出口，写成默认值可以让
    调用点少一个参数、也少一次「忘了加就重复计数」的机会。
    """
    record_send_result(
        platform, kind=kind, only_if_outermost=only_if_outermost,
        res={"delivered": True} if ok else {"delivered": False,
                                            "error": reason or "send_failed"})


def record_ok_send(platform: str, out: Any, *, kind: str = "text",
                   only_if_outermost: bool = True) -> None:
    """给官方 API 辅助函数的 ``{"ok": ...}`` 形状用；**永不抛**。"""
    try:
        if only_if_outermost and _CLAIMED.get():
            return
        outcome, reason = classify_ok_result(out)
        if not outcome:
            return
        get_outbound_delivery_stats().record(
            platform, kind=kind, outcome=outcome, reason=reason)
    except Exception:  # noqa: BLE001 - 观测失败必须静默
        pass


#: 叶子出口的返回形状 → 取「成败」的方式。全部知识集中在这里，调用点只写一行装饰器。
_SHAPES = {
    #: {"ok": bool, "error": str[, "error_kind"]}——官方 API 辅助 + LINE/WA RPA
    "ok": lambda r: classify_ok_result(r),
    #: True/False——A 线 send_photo/send_voice、LINE push、Messenger _send_reply
    "bool": lambda r: (("sent", "") if r else ("failed", "send_failed")),
    #: (ok, Message|None)——A 线 _send_text_guarded
    "tuple_ok": lambda r: (("sent", "") if (r or (None,))[0]
                           else ("failed", "send_failed")),
    #: {"delivered": bool, ...}——worker 契约
    "delivered": lambda r: classify_result(r),
}


def meter_send(platform: str, *, kind: str = "text", shape: str = "ok"):
    """把一个**叶子发送出口**接进出站看板。

    用装饰器而非在每个 ``return`` 前插一行，是因为这些函数动辄七八个返回点（各种
    早退守卫），逐点插桩既啰嗦又必然有人漏改；装饰器让「这个函数的出站要计数」变成
    函数签名上方一行可读的声明。

    自带 ``only_if_outermost``：叶子出口既可能被编排器调用、也可能被旁路直接调用，
    占坑机制保证一次发送只记一条（见模块 docstring）。
    """
    mapper = _SHAPES.get(shape) or _SHAPES["ok"]

    def _deco(fn):
        @functools.wraps(fn)
        async def _wrapped(*args, **kwargs):
            try:
                out = await fn(*args, **kwargs)
            except BaseException as ex:  # noqa: BLE001 - 只记账，异常照常上抛
                record_send_result(platform, kind=kind, exc=ex,
                                   only_if_outermost=True)
                raise
            try:
                if not _CLAIMED.get():
                    outcome, reason = mapper(out)
                    if outcome:
                        get_outbound_delivery_stats().record(
                            platform, kind=kind, outcome=outcome, reason=reason)
            except Exception:  # noqa: BLE001 - 观测失败必须静默
                pass
            return out

        return _wrapped

    return _deco


__all__ = [
    "OutboundDeliveryStats",
    "claim_send",
    "classify_exception",
    "classify_ok_result",
    "classify_result",
    "get_outbound_delivery_stats",
    "meter_send",
    "record_bool_send",
    "record_ok_send",
    "record_send_result",
    "send_claimed",
]
