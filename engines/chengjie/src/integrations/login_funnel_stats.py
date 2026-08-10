"""扫码登录漏斗观测（进程级单例，平台通用）。

背景（2026-07-25 事故）：LINE 协议扫码 100% 登录失败，持续多日无人知晓。后端把 PIN 送到了
浏览器、前端把它丢了，两端各自的单测都全绿——**没有任何一层在看「发起了多少次、成功了几次」**。
一个「pin_issued=N, authorized=0」的计数会在第一天就把它喊出来。

本模块把登录流程拆成漏斗段计数，按 (platform, mode) 归并：

    started → qr_shown → pin_issued → authorized
                                    ↘ failed[reason_code]

判读方式：
- ``qr_shown`` 远小于 ``started`` → 二维码生成链路坏了（网关不可达 / 渲染失败）。
- ``pin_issued`` 有量而 ``authorized`` 近零 → **PIN 没能抵达用户**，即本次事故的形态。
- ``failed`` 按 reason_code 分布 → 直接指出是超时、限流还是网络。

刻意做成平台通用而非 LINE 专用：同一个漏斗形状适用于 Telegram/WhatsApp/Messenger，
下次任一平台的登录链路悄悄坏掉，同一张表就能看见。

隐私：**只存计数**。绝不记录 PIN、token、account_id、二维码内容或任何用户可识别信息。
distinct key 有上限（防脏 platform/mode 值撑爆内存），超限归入 ``__other__``。

风格对齐 src/web/frontend_error_stats.py：无新增依赖，线程安全，进程级单例。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Optional

_MAX_KEYS = 40
_IDENT_RE = re.compile(r"^[a-z0-9_]{1,24}$")

# 与 line_protocol_login.classify_login_error 的枚举一致；越界值归 login_failed，
# 防脏码把 by_reason 撑成高基数维度。
_REASON_CODES = frozenset({
    "qr_expired", "pin_timeout", "network", "rate_limited",
    "login_failed", "okline_missing", "client_init", "cancelled",
    # sidecar（WA Baileys / Messenger web 的 Node 服务）没起来。与 network 分开计：
    # 前者是运维照单启动即可修，后者要查网络，混在一起看不出「又是服务没开」这个高频项。
    "service_down",
    # Telegram 协议登录缺 api_id/api_hash（含 credpool 取不到）——配置问题而非链路故障，
    # 与 login_failed 分开计才看得出「一直是没配凭据」这个可一次性修好的根因。
    "creds_missing",
    # Telegram 报 API_ID_INVALID 族（2026-08-10 实锤事故）：凭据**本体**无效——托管池
    # 组废了或用户自填错值，重试/刷新二维码永远无解，处置是换凭据+重启应用。与
    # creds_missing（没配）和 login_failed（不明）都不同：多台机器同报此码＝池组事故。
    "cred_invalid",
    # 连不上 Telegram DC（原生 TCP 直连被墙/被拦）——大陆用户系统代理只救 HTTP 救不了
    # MTProto 的典型形态。与 network（连不上**我们的**服务）分开：处置是配代理，不是查网。
    "tg_unreachable",
    # 托管登录（Messenger web）的页面级归因，由 Node 侧 login_classify.js 判出并透传。
    # 三者各对应完全不同的处置：换号/申诉、去窗口输验证码、核对密码。压成 login_failed
    # 就等于告诉运维「你自己去猜」。
    "two_factor", "checkpoint", "password_error",
    # 会话在 TTL 内没走到任何终态（人没在窗口期内完成）。与 login_failed 分开：前者是
    # 耐心/时长预算问题，后者是链路真的坏了——混在一起会把「TTL 太短」永久藏起来。
    "session_timeout",
    # Telegram 手机号登录专属（sendCode/signIn）：号码非法 / 被封 / 未注册 / 验证码错或过期。
    # 处置各不相同（改号 / 换号 / 去 App 注册 / 重输 / 重发），绝不能压成 login_failed。
    "phone_invalid", "phone_banned", "phone_unoccupied",
    "code_invalid", "code_expired",
})

_STAGES = ("started", "qr_shown", "pin_issued", "authorized", "failed")


def _san_key(platform: str, mode: str) -> str:
    p = str(platform or "").strip().lower()
    m = str(mode or "").strip().lower()
    p = p if _IDENT_RE.match(p) else "unknown"
    m = m if _IDENT_RE.match(m) else "unknown"
    return f"{p}:{m}"


def _san_reason(code: str) -> str:
    c = str(code or "").strip().lower()
    return c if c in _REASON_CODES else "login_failed"


class LoginFunnelStats:
    """扫码登录漏斗计数（线程安全，进程级）。"""

    __slots__ = ("_lock", "_started_at", "_last_ts", "_funnel", "_since", "_reasons",
                 "_auth_ms_sum", "_auth_ms_n", "overflow")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_ts = 0.0
        self.overflow = 0
        self._funnel: Dict[str, Dict[str, int]] = {}
        # 「距上次成功以来」的计数：累计口径判不出**回归**——上周成功过的链路今天彻底
        # 坏掉，``authorized > 0`` 永远成立，看板不标 stalled、告警也不该闭嘴。回归比
        # 「从没通过的新链路」更隐蔽（多数人默认它还好着），必须能判。成功即清零。
        self._since: Dict[str, Dict[str, int]] = {}
        self._reasons: Dict[str, Dict[str, int]] = {}
        # 只累计成功登录耗时：失败样本的耗时由超时预算主导，混进去会把均值钉死在超时值上
        self._auth_ms_sum: Dict[str, float] = {}
        self._auth_ms_n: Dict[str, int] = {}

    def _slot(self, key: str) -> tuple[str, Dict[str, int]]:
        """返回 (生效 key, 行)。

        必须把**生效** key 交还调用方：超限折叠成 ``__other__`` 时，若旁挂的
        ``_since`` / ``_reasons`` / ``_auth_ms_*`` 仍按原始脏 key 落键，就绕过了
        ``_MAX_KEYS`` 想防的那件事——主表封顶、旁表无限长，且这些条目在 ``dump``
        里永远读不到（只按 ``_funnel`` 的 key 查），是纯泄漏。
        """
        row = self._funnel.get(key)
        if row is not None:
            return key, row
        if len(self._funnel) >= _MAX_KEYS:
            self.overflow += 1
            key = "__other__"
            row = self._funnel.get(key)
            if row is not None:
                return key, row
        row = {s: 0 for s in _STAGES}
        self._funnel[key] = row
        return key, row

    def record(self, platform: str, mode: str, stage: str, *,
               reason_code: str = "", elapsed_ms: float = 0.0) -> None:
        """记一次漏斗事件。调用方全部 best-effort——观测绝不能影响登录本身。"""
        if stage not in _STAGES:
            return
        key = _san_key(platform, mode)
        with self._lock:
            key, row = self._slot(key)
            row[stage] += 1
            since = self._since.setdefault(key, {s: 0 for s in _STAGES})
            if stage == "authorized":
                for s in _STAGES:
                    since[s] = 0
            else:
                since[stage] += 1
            self._last_ts = time.time()
            if stage == "failed":
                bucket = self._reasons.setdefault(key, {})
                r = _san_reason(reason_code)
                bucket[r] = bucket.get(r, 0) + 1
            elif stage == "authorized" and elapsed_ms > 0:
                self._auth_ms_sum[key] = self._auth_ms_sum.get(key, 0.0) + float(elapsed_ms)
                self._auth_ms_n[key] = self._auth_ms_n.get(key, 0) + 1

    def dump(self) -> Dict[str, Any]:
        """供 /api/workspace/metrics 消费。``stalled`` 是给看板的一眼判读。"""
        with self._lock:
            rows: List[Dict[str, Any]] = []
            for key, f in sorted(self._funnel.items()):
                n = self._auth_ms_n.get(key, 0)
                since = self._since.get(key) or {s: 0 for s in _STAGES}
                rows.append({
                    "key": key,
                    "started": f["started"],
                    "qr_shown": f["qr_shown"],
                    "pin_issued": f["pin_issued"],
                    "authorized": f["authorized"],
                    "failed": f["failed"],
                    # 距上次成功以来（从未成功＝等于累计）。看板与 watchdog 共用同一判据，
                    # 免得「收到告警去看板却显示健康」。
                    "started_since_success": since["started"],
                    "qr_shown_since_success": since["qr_shown"],
                    "failed_since_success": since["failed"],
                    "reasons": dict(self._reasons.get(key, {})),
                    "avg_authorized_ms": round(self._auth_ms_sum.get(key, 0.0) / n) if n else 0,
                    # 连撞 3 次没成一次 = 该平台该方式的登录链路事实上不可用（含回归）
                    "stalled": bool(since["started"] >= 3),
                })
            return {"rows": rows, "overflow": self.overflow,
                    "last_ts": self._last_ts, "since": self._started_at}

    def dump_prom(self) -> str:
        lines = [
            "# HELP login_funnel_total 扫码登录漏斗分段计数",
            "# TYPE login_funnel_total counter",
        ]
        with self._lock:
            for key, f in sorted(self._funnel.items()):
                plat, _, mode = key.partition(":")
                for stage in _STAGES:
                    lines.append(
                        f'login_funnel_total{{platform="{plat}",mode="{mode}",'
                        f'stage="{stage}"}} {f[stage]}')
            # 判据本身也要出到 Prometheus：累计 counter 判不出「先能用后坏掉」的回归，
            # 而 rate()/increase() 又只能近似。看板、watchdog、外部告警读同一个数，
            # 才不会出现「Prom 没响但看板标红」这种自相矛盾。
            # 外部告警规则可直接写：login_funnel_since_success{stage="started"} >= 3
            lines.append("# HELP login_funnel_since_success 距上次成功登录以来的分段计数（成功即清零）")
            lines.append("# TYPE login_funnel_since_success gauge")
            for key in sorted(self._funnel):
                plat, _, mode = key.partition(":")
                since = self._since.get(key) or {}
                for stage in _STAGES:
                    lines.append(
                        f'login_funnel_since_success{{platform="{plat}",mode="{mode}",'
                        f'stage="{stage}"}} {since.get(stage, 0)}')
            lines.append("# HELP login_funnel_failed_total 登录失败按原因码")
            lines.append("# TYPE login_funnel_failed_total counter")
            for key, bucket in sorted(self._reasons.items()):
                plat, _, mode = key.partition(":")
                for reason, n in sorted(bucket.items()):
                    lines.append(
                        f'login_funnel_failed_total{{platform="{plat}",mode="{mode}",'
                        f'reason="{reason}"}} {n}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._funnel.clear()
            self._since.clear()
            self._reasons.clear()
            self._auth_ms_sum.clear()
            self._auth_ms_n.clear()
            self.overflow = 0


_stats: Optional[LoginFunnelStats] = None
_stats_lock = threading.Lock()


def get_login_funnel_stats() -> LoginFunnelStats:
    global _stats
    if _stats is None:
        with _stats_lock:
            if _stats is None:
                _stats = LoginFunnelStats()
    return _stats


def record_login_stage(platform: str, mode: str, stage: str, *,
                       reason_code: str = "", elapsed_ms: float = 0.0) -> None:
    """模块级便捷入口（调用方一律 try/except 包裹，观测失败绝不影响登录）。

    顺带旁路写入按日趋势（``login_funnel_trend``，默认关 → record 恒 no-op）。
    写口只此一处，调用方零改动即获跨重启时间线。
    """
    try:
        get_login_funnel_stats().record(
            platform, mode, stage, reason_code=reason_code, elapsed_ms=elapsed_ms)
    except Exception:  # noqa: BLE001
        pass
    try:
        from src.integrations.login_funnel_trend import record_login_funnel_trend
        record_login_funnel_trend(stage, reason_code=reason_code)
    except Exception:  # noqa: BLE001
        pass
