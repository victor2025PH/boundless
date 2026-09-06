"""AccountLimiter — 控制每账号 / 全域的每日 handoff 发送配额。

为什么需要：Meta 风控会关注"这个账号一天把多少人往外 App 引"。
daily_cap=15 表示每个 Messenger 账号每天最多发 15 次引流话术，
超过后 `check_and_reserve` 会拒绝——让 runner 走普通回复。

global_cap 是全域当日总数软上限（所有账号合计），防止一整个话术池被 Meta
聚合识别。超出后所有账号都会被拒绝。

存储：account_handoff_counters 表，按 UTC 日期分桶，PK 原子递增。
跨进程安全，因为 SQLite 文件锁保护了 UPDATE。

注意：**不是 rate-limiter（秒/分钟），是 quota（日）**。频率类反封号
（"一分钟连发 5 条"）由 rpa runner 自己做 pacing，这里不管。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

# 「不限」哨兵：daily_cap<=0 或 outbound.unlimited_mode 时 effective_cap 返回它
# （与 global_remaining 同刻度）。单一事实源在 outbound_policy，此处 re-export。
try:
    from src.ops.outbound_policy import UNLIMITED_CAP
except Exception:  # pragma: no cover - 极端装配态兜底
    UNLIMITED_CAP = 10 ** 9


@dataclass
class LimitDecision:
    ok: bool
    reason: str = ""
    remaining_today: int = 0
    account_count_today: int = 0
    global_count_today: int = 0


class AccountLimiter:
    """每账号 / 全域的日配额；按 UTC 日期切桶。"""

    def __init__(
        self,
        store,
        *,
        daily_cap: int = 15,
        global_cap: int = 0,   # 0 = 不启用全域限额
        alert_thresholds_pct: Optional[list] = None,   # 如 [80, 100]；None=不告警
        on_threshold_crossed: Optional[Callable[[str, int, int, int], None]] = None,
        warmup_enabled: bool = False,   # M7：新号预热爬坡（默认关→行为不变）
        warmup_start_cap: int = 2,
        warmup_ramp_days: int = 14,
        age_days_fn: Optional[Callable[[str], Optional[float]]] = None,
    ) -> None:
        self._store = store
        # 「0=不限」全仓单一语义（2026-09-04）：旧写法 max(1, cap) 把 0 悄悄改成
        # 1/天——运营配 0 想放开，结果比默认还严。0 → 账号级不限额（仍计数，
        # 便于看板与告警阈值观测）。
        self._daily_cap = max(0, int(daily_cap))
        self._global_cap = max(0, int(global_cap))
        # M7：预热爬坡——effective_cap = warmup_cap(age) ≤ daily_cap。
        # 仅当 warmup_enabled 且 age_days_fn 给出有效天龄时生效；否则恒回退 daily_cap。
        self._warmup_enabled = bool(warmup_enabled)
        self._warmup_start_cap = max(0, int(warmup_start_cap))
        self._warmup_ramp_days = max(1, int(warmup_ramp_days))
        self._age_days_fn = age_days_fn
        # W4-Cap-Alert：跨过任意 pct 阈值时触发回调（stateless：基于 old→new 区间）
        self._thresholds = sorted(set(
            int(p) for p in (alert_thresholds_pct or []) if 0 < int(p) <= 100
        ))
        self._on_threshold = on_threshold_crossed

    # ── W4-Cap-Alert：late-binding 设置回调（main.py 在 webhook 就绪后调） ──
    def set_on_threshold_crossed(
        self,
        callback: Optional[Callable[[str, int, int, int], None]],
    ) -> None:
        self._on_threshold = callback

    # ── 查 ────────────────────────────────────────────
    @staticmethod
    def _utc_day(ts: Optional[int] = None) -> str:
        ts = ts if ts is not None else int(time.time())
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    def effective_cap(self, account_id: str) -> int:
        """该账号当前生效的每日上限。

        预热关闭/无天龄信息 → 恒为配置 ``daily_cap``（行为不变）。
        预热开启且能取到天龄 → ``min(daily_cap, warmup_cap(age))``，新号更低、随天龄爬坡。

        「0=不限」/ ``outbound.unlimited_mode``：账号级业务日上限视为
        ``UNLIMITED_CAP``（10**9）。**预热爬坡是账号安全层，unlimited 下仍生效**
        ——新号仍按 start_cap→ramp 爬坡（目标值取 UNLIMITED，即预热期满后不限）。
        """
        base = self._daily_cap
        try:
            from src.ops.outbound_policy import business_cap
            base = business_cap(base)
        except Exception:
            pass
        if base <= 0:
            base = UNLIMITED_CAP
        if not self._warmup_enabled or self._age_days_fn is None:
            return base
        try:
            age = self._age_days_fn(account_id)
        except Exception:
            logger.debug("age_days_fn 取天龄失败，回退 daily_cap (acc=%s)", account_id,
                         exc_info=True)
            return base
        if age is None:
            return base
        from src.skills.account_health import warmup_cap
        ramped = warmup_cap(
            age, base,
            start_cap=self._warmup_start_cap, ramp_days=self._warmup_ramp_days,
        )
        return min(base, max(0, ramped))

    def is_unlimited(self, account_id: str) -> bool:
        """账号级日上限当前是否为「不限」（0 配置或 unlimited_mode，且不在预热爬坡中）。"""
        return self.effective_cap(account_id) >= UNLIMITED_CAP

    def remaining_for(self, account_id: str, *, now: Optional[int] = None) -> int:
        day = self._utc_day(now)
        used = self._store.get_account_handoff_counter(account_id, day)
        return max(0, self.effective_cap(account_id) - used)

    def _eff_global_cap(self) -> int:
        """全域日上限（0=不限；unlimited_mode 下恒 0）。"""
        try:
            from src.ops.outbound_policy import business_cap
            return business_cap(self._global_cap)
        except Exception:
            return max(0, int(self._global_cap))

    def global_remaining(self, *, now: Optional[int] = None) -> int:
        gcap = self._eff_global_cap()
        if gcap <= 0:
            return UNLIMITED_CAP    # 相当于无限
        day = self._utc_day(now)
        used = self._store.sum_account_handoff_counters(day)
        return max(0, gcap - used)

    def get_counts(self, account_id: str, *, now: Optional[int] = None) -> dict:
        day = self._utc_day(now)
        return {
            "day": day,
            "account_count": self._store.get_account_handoff_counter(account_id, day),
            "account_remaining": self.remaining_for(account_id, now=now),
            "global_count": self._store.sum_account_handoff_counters(day),
            "global_cap": self._global_cap,
            "daily_cap": self._daily_cap,
            "effective_cap": self.effective_cap(account_id),
        }

    # ── 预判 + 扣减（原子一对）──────────────────────────
    def check_and_reserve(
        self,
        account_id: str,
        *,
        now: Optional[int] = None,
    ) -> LimitDecision:
        """只有返回 ok=True 时才扣成功。失败不扣。

        调用方约定：拿到 ok=True 就当这次引流已占坑，后续无论发送成功与否
        都不退还；发送失败应走 runner 的 retry/降级，不通过还额度。
        """
        day = self._utc_day(now)
        # 全域优先判
        gcap = self._eff_global_cap()
        if gcap > 0:
            global_used = self._store.sum_account_handoff_counters(day)
            if global_used >= gcap:
                return LimitDecision(
                    ok=False, reason="global_cap_exceeded",
                    remaining_today=0,
                    global_count_today=global_used,
                )
        # 账号级（M7：用 effective_cap——预热关时即 daily_cap，行为不变）
        eff_cap = self.effective_cap(account_id)
        acct_used = self._store.get_account_handoff_counter(account_id, day)
        if acct_used >= eff_cap:
            _configured = self._daily_cap if self._daily_cap > 0 else UNLIMITED_CAP
            return LimitDecision(
                ok=False,
                reason="warmup_cap_exceeded" if eff_cap < _configured
                else "account_cap_exceeded",
                remaining_today=0,
                account_count_today=acct_used,
            )
        # 扣
        new_count = self._store.incr_account_handoff_counter(account_id, day)
        global_used_after = self._store.sum_account_handoff_counters(day) \
            if gcap > 0 else 0
        logger.info("AccountLimiter reserved: acc=%s day=%s count=%d/%d",
                    account_id, day, new_count, eff_cap)
        # W4-Cap-Alert：阈值跨越检测（stateless——仅看 old→new 区间，按 effective cap）
        self._emit_threshold_crossings(account_id, new_count, cap=eff_cap)
        return LimitDecision(
            ok=True, reason="reserved",
            remaining_today=max(0, eff_cap - new_count),
            account_count_today=new_count,
            global_count_today=global_used_after,
        )

    # ── W4-Cap-Alert ──────────────────────────────────────
    def _emit_threshold_crossings(
        self, account_id: str, new_count: int, *, cap: Optional[int] = None,
    ) -> None:
        """new_count 刚 +1。若上一步的 pct < 某阈值 <= 新 pct，触发回调。

        stateless：基于 (old_count, new_count) 计算——每次跨越只触发一次，
        当天后续扣减不会重复触发同一阈值（因为 pct 单调递增）。
        ``cap`` 缺省用 daily_cap；预热开启时调用方传 effective cap。
        """
        if not self._thresholds or self._on_threshold is None:
            return
        old_count = new_count - 1
        cap = self._daily_cap if cap is None else max(1, int(cap))
        old_pct = old_count * 100.0 / cap
        new_pct = new_count * 100.0 / cap
        for pct in self._thresholds:
            if old_pct < pct <= new_pct:
                try:
                    self._on_threshold(account_id, pct, new_count, cap)
                except Exception:
                    logger.warning(
                        "AccountLimiter threshold callback failed "
                        "(acc=%s pct=%s)", account_id, pct, exc_info=True)

    # ── 退款（业务层拒绝后释放配额） ─────────────────
    def refund(self, account_id: str, *, now: Optional[int] = None) -> bool:
        """预扣后的业务层失败（合规拒/渲染失败）时调用，释放一个配额。

        保守：计数不会低于 0；返回是否确实减了 1。
        """
        day = self._utc_day(now)
        current = self._store.get_account_handoff_counter(account_id, day)
        if current <= 0:
            return False
        # sqlite 没直接的条件减，借一层：写一个"手动 -1"的便利方法
        return self._store.decr_account_handoff_counter(account_id, day) > 0

    # ── 手动工具 ──────────────────────────────────────
    def reset(self, account_id: str, *, now: Optional[int] = None) -> None:
        """运营/测试手动清零今日计数。"""
        day = self._utc_day(now)
        self._store.reset_account_handoff_counter(account_id, day)
