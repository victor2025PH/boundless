"""托管代理订阅台账——一键代理的**幂等扣费凭据**与续期状态机。

## 这张表存在的唯一理由：防重复扣费

``token_ledger.record_spend`` 是**按天聚合、无幂等键**的（同 ``(钱包, 日, 动作)``
累加），这对「翻译了 1500 字符」这类计量动作完全正确，但对「买了一条代理」这种
**一次性购买**意味着：网络重试、坐席连点两下、前端超时后重发，每一次都是一笔真实
扣费，而用户只拿到一条代理。故一键代理的幂等**必须**长在这里——
``UNIQUE(charge_ref)`` 是那道唯一的闸。

## 两阶段提交（顺序本身就是设计）

```
reserve(charge_ref)            # 先占坑：并发/重放在这一步就被挡住，零副作用
   ├─ 已存在 → 返回原单       # 幂等重放：连点第二下拿到的是**同一条代理**，不是报错
   └─ 占坑成功
        ↓
   provision()                 # 向供给方要货（外部调用，可能失败）
        ├─ 失败 → release()    # 删除占坑：**一分钱没扣**，同一个键可以原样重试
        └─ 成功
             ↓
        activate(charged=)     # 先落库再记账，最后才扣钱
```

**先开通后扣费**是刻意的方向选择：中途崩溃时，「我们少收了一笔」远好过「用户被扣了
钱却没拿到代理」。滥用风险由 ``reserve`` 之前的余额预检挡住（余额不够根本不会向
上游要货），敞口有界。

``activate(charged=False)`` 是「货已交付但记账失败」的诚实状态：用户照常拿到代理，
行上留着 ``charged=0`` 等运维对账补记——**绝不**为了账面好看而重试扣费（重试扣费
才是真正会双扣的那条路）。
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STATUS_RESERVED = "reserved"
STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"

_DDL = """
CREATE TABLE IF NOT EXISTS proxy_subscriptions (
    sub_id       TEXT PRIMARY KEY,
    charge_ref   TEXT NOT NULL UNIQUE,
    wallet       TEXT NOT NULL DEFAULT '',
    account_key  TEXT NOT NULL DEFAULT '',
    proxy_id     TEXT NOT NULL DEFAULT '',
    provider     TEXT NOT NULL DEFAULT '',
    order_ref    TEXT NOT NULL DEFAULT '',
    country      TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT '',
    tokens       INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'reserved',
    charged      INTEGER NOT NULL DEFAULT 0,
    auto_renew   INTEGER NOT NULL DEFAULT 0,
    renew_count  INTEGER NOT NULL DEFAULT 0,
    period_start REAL NOT NULL DEFAULT 0,
    period_end   REAL NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL DEFAULT 0,
    updated_at   REAL NOT NULL DEFAULT 0,
    note         TEXT NOT NULL DEFAULT '',
    renew_state  TEXT NOT NULL DEFAULT '',
    expiry_warned_at REAL NOT NULL DEFAULT 0,
    swap_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pxsub_wallet ON proxy_subscriptions(wallet);
CREATE INDEX IF NOT EXISTS idx_pxsub_status ON proxy_subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_pxsub_proxy ON proxy_subscriptions(proxy_id);
"""

# 旧库幂等补列（与 translation_trend_store 同模式：ALTER 失败=列已在，静默跳过）。
# P1 首发即带 renew_state/expiry_warned_at/swap_count 的部署走 DDL；比 P2 早建库的
# 部署（含 P1 已装载的生产实例）由这里补齐。
_MIGRATIONS = (
    "ALTER TABLE proxy_subscriptions ADD COLUMN renew_state TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE proxy_subscriptions ADD COLUMN expiry_warned_at REAL NOT NULL DEFAULT 0",
    "ALTER TABLE proxy_subscriptions ADD COLUMN swap_count INTEGER NOT NULL DEFAULT 0",
)

# renew_state 的三个值（空串=本期无续期动作）
RENEW_PENDING = "pending"      # CAS 已延期、扣费还没落账（崩溃恢复点，对账 CLI 点名）
RENEW_UNBILLED = "unbilled"    # 已延期但扣费失败（余额账本异常）——服务照续，待对账补记


def build_charge_ref(
    *, wallet: str, account_key: str, request_id: str = "",
    now: Optional[float] = None, bucket_sec: int = 30,
) -> str:
    """构造幂等键。

    前端每次点击生成一个 ``request_id``（与发送幂等 ``client_msg_id`` 同范式）→
    重放/重试天然同键。**没带**时回落到 ``(钱包, 账号, 30 秒时间桶)``：这挡不住
    「隔一分钟又点一次」，但挡得住真正高发的那一类——连点与网络层重发。
    """
    rid = str(request_id or "").strip()
    if rid:
        return f"pxsub:{wallet or 'default'}:{rid}"
    ts = time.time() if now is None else float(now)
    bucket = int(ts // max(1, int(bucket_sec)))
    return f"pxauto:{wallet or 'default'}:{account_key or '-'}:{bucket}"


class ProxySubscriptionStore:
    """托管代理订阅台账（线程安全 SQLite；风格对齐 ProxyPool / TokenLedgerStore）。"""

    def __init__(self, db_path: Any = ":memory:") -> None:
        self._is_mem = str(db_path) == ":memory:"
        if not self._is_mem:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if not self._is_mem:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            for mig in _MIGRATIONS:
                try:
                    self._conn.execute(mig)
                except sqlite3.OperationalError:
                    pass  # 列已存在（新库走 DDL 直建）
            self._conn.commit()

    # ── 读 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _row(row: sqlite3.Row, *, now: Optional[float] = None) -> Dict[str, Any]:
        d = dict(row)
        ts = time.time() if now is None else now
        d["auto_renew"] = bool(d.get("auto_renew"))
        d["charged"] = bool(d.get("charged"))
        end = float(d.get("period_end") or 0)
        d["expired"] = bool(end and end <= ts)
        d["remaining_sec"] = max(0, int(end - ts)) if end else 0
        d["remaining_days"] = int(d["remaining_sec"] // 86400)
        return d

    def get(self, sub_id: str, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM proxy_subscriptions WHERE sub_id=?", (str(sub_id),),
            ).fetchone()
        return self._row(row, now=now) if row else None

    def by_charge_ref(
        self, charge_ref: str, *, now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM proxy_subscriptions WHERE charge_ref=?",
                (str(charge_ref),),
            ).fetchone()
        return self._row(row, now=now) if row else None

    def by_proxy_id(
        self, proxy_id: str, *, now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """代理 → 订阅（UI 给托管代理打「托管中 · 剩 N 天」徽章用）。"""
        if not proxy_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM proxy_subscriptions WHERE proxy_id=? AND status=? "
                "ORDER BY period_end DESC LIMIT 1",
                (str(proxy_id), STATUS_ACTIVE),
            ).fetchone()
        return self._row(row, now=now) if row else None

    def list_active(
        self, *, wallet: str = "", now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM proxy_subscriptions WHERE status=?"
        args: List[Any] = [STATUS_ACTIVE]
        if wallet:
            sql += " AND wallet=?"
            args.append(wallet)
        sql += " ORDER BY period_end ASC"
        with self._lock:
            rows = self._conn.execute(sql, tuple(args)).fetchall()
        return [self._row(r, now=now) for r in rows]

    def expiring(
        self, *, within_sec: float, now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """即将到期（含已过期）的活跃订阅——续期/提醒巡检的输入。"""
        ts = time.time() if now is None else float(now)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM proxy_subscriptions WHERE status=? AND period_end>0 "
                "AND period_end<=? ORDER BY period_end ASC",
                (STATUS_ACTIVE, ts + float(within_sec)),
            ).fetchall()
        return [self._row(r, now=ts) for r in rows]

    # ── 写：两阶段提交 ────────────────────────────────────────────────────

    def reserve(
        self, *, charge_ref: str, wallet: str, account_key: str = "",
        provider: str = "", country: str = "", kind: str = "",
        tokens: int = 0, auto_renew: bool = False,
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """占坑（幂等闸）。返回新建行；``charge_ref`` 已存在 → ``None``。

        调用方拿到 ``None`` **必须**走 ``by_charge_ref()`` 取原单原样返回，
        而不是报错——重放拿到同一条代理才是正确的幂等语义。
        """
        ref = str(charge_ref or "").strip()
        if not ref:
            return None
        ts = time.time() if now is None else float(now)
        sub_id = "pxs_" + secrets.token_hex(6)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO proxy_subscriptions "
                    "(sub_id, charge_ref, wallet, account_key, provider, country, kind,"
                    " tokens, status, auto_renew, period_start, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sub_id, ref, str(wallet or "default"), str(account_key or ""),
                     str(provider or ""), str(country or "").upper(), str(kind or ""),
                     int(tokens or 0), STATUS_RESERVED, 1 if auto_renew else 0,
                     ts, ts, ts),
                )
                self._conn.commit()
        except sqlite3.IntegrityError:
            return None  # 同 charge_ref 已占坑（幂等拒绝）
        return self.get(sub_id, now=ts)

    def release(self, sub_id: str) -> None:
        """开通失败 → 删除占坑，让**同一个** charge_ref 能原样重试。

        刻意不留 ``failed`` 行：一分钱没扣、一条代理没交付，这笔交易从未发生，
        没有审计义务；留着反而会把用户的重试永久挡在幂等闸外。失败本身走日志与
        计数器观测。仅删 ``reserved`` 状态，绝不误删已激活的订阅。
        """
        with self._lock:
            self._conn.execute(
                "DELETE FROM proxy_subscriptions WHERE sub_id=? AND status=?",
                (str(sub_id), STATUS_RESERVED),
            )
            self._conn.commit()

    def activate(
        self, sub_id: str, *, proxy_id: str, order_ref: str = "",
        period_end: float = 0.0, charged: bool = True,
        country: str = "", kind: str = "", note: str = "",
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """货已交付 → 转 active。``charged=False`` = 交付成功但记账失败（待对账）。"""
        ts = time.time() if now is None else float(now)
        with self._lock:
            self._conn.execute(
                "UPDATE proxy_subscriptions SET proxy_id=?, order_ref=?, status=?,"
                " charged=?, period_end=?, updated_at=?,"
                " country=COALESCE(NULLIF(?,''), country),"
                " kind=COALESCE(NULLIF(?,''), kind),"
                " note=? WHERE sub_id=?",
                (str(proxy_id or ""), str(order_ref or ""), STATUS_ACTIVE,
                 1 if charged else 0, float(period_end or 0), ts,
                 str(country or "").upper(), str(kind or ""), str(note or ""),
                 str(sub_id)),
            )
            self._conn.commit()
        return self.get(sub_id, now=ts)

    def mark_expired(self, sub_id: str, *, now: Optional[float] = None) -> None:
        ts = time.time() if now is None else float(now)
        with self._lock:
            self._conn.execute(
                "UPDATE proxy_subscriptions SET status=?, updated_at=? "
                "WHERE sub_id=? AND status=?",
                (STATUS_EXPIRED, ts, str(sub_id), STATUS_ACTIVE),
            )
            self._conn.commit()

    def cancel(self, sub_id: str, *, now: Optional[float] = None) -> None:
        """退订（停止续期；本期不退款，用到期末——与主流订阅口径一致）。"""
        ts = time.time() if now is None else float(now)
        with self._lock:
            self._conn.execute(
                "UPDATE proxy_subscriptions SET auto_renew=0, status=?, updated_at=? "
                "WHERE sub_id=?",
                (STATUS_CANCELLED, ts, str(sub_id)),
            )
            self._conn.commit()

    def renew(
        self, sub_id: str, *, period_end: float, tokens: int = 0,
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """续期成功：推到期时间 + 累加已扣 Token + 计次。"""
        ts = time.time() if now is None else float(now)
        with self._lock:
            self._conn.execute(
                "UPDATE proxy_subscriptions SET period_end=?, tokens=tokens+?,"
                " renew_count=renew_count+1, updated_at=? WHERE sub_id=?",
                (float(period_end), int(tokens or 0), ts, str(sub_id)),
            )
            self._conn.commit()
        return self.get(sub_id, now=ts)

    # ── 生命周期（P2：自动续期 / 到期回收 / 免费换货）────────────────────────

    def renew_cas(
        self, sub_id: str, *, expected_end: float, new_end: float,
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """续期第一步：**乐观锁延期**（每期至多成功一次的幂等闸）。

        WHERE 钉住 ``period_end==expected_end``（±1s 容浮点）——巡检重入/进程重启后
        重跑，同一期只有一次 CAS 能赢，输家拿 ``None`` 静默跳过。**先延期后扣费**与
        首购同一方向（宁可少收一笔，绝不双扣）：赢家把 ``renew_state`` 置
        ``pending``，扣费落账后由 :meth:`finalize_renewal` 清态；中途崩溃时
        ``pending`` 行就是对账 CLI 的点名对象。顺带清 ``expiry_warned_at``
        （新的一期要重新计警）。
        """
        ts = time.time() if now is None else float(now)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE proxy_subscriptions SET period_end=?, renew_count=renew_count+1,"
                " renew_state=?, expiry_warned_at=0, updated_at=?"
                " WHERE sub_id=? AND status=? AND ABS(period_end-?)<1.0",
                (float(new_end), RENEW_PENDING, ts, str(sub_id), STATUS_ACTIVE,
                 float(expected_end)),
            )
            self._conn.commit()
            if cur.rowcount <= 0:
                return None
        return self.get(sub_id, now=ts)

    def finalize_renewal(
        self, sub_id: str, *, tokens: int, unbilled: bool = False,
        now: Optional[float] = None,
    ) -> None:
        """续期第二步：记账结果落台账（成功清态 / 失败标 unbilled 待对账）。"""
        ts = time.time() if now is None else float(now)
        with self._lock:
            self._conn.execute(
                "UPDATE proxy_subscriptions SET tokens=tokens+?, renew_state=?,"
                " updated_at=? WHERE sub_id=?",
                (int(tokens or 0), RENEW_UNBILLED if unbilled else "", ts,
                 str(sub_id)),
            )
            self._conn.commit()

    def pending_renewals(self, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """续期中途崩溃的行（renew_state 非空）——对账 CLI 的输入。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM proxy_subscriptions WHERE renew_state<>''"
                " ORDER BY updated_at ASC",
            ).fetchall()
        return [self._row(r, now=now) for r in rows]

    def mark_expiry_warned(self, sub_id: str, *, now: Optional[float] = None) -> None:
        """记「已发过到期预警」——同一期只轰一次，续期成功后自动复位。"""
        ts = time.time() if now is None else float(now)
        with self._lock:
            self._conn.execute(
                "UPDATE proxy_subscriptions SET expiry_warned_at=?, updated_at=?"
                " WHERE sub_id=?", (ts, ts, str(sub_id)),
            )
            self._conn.commit()

    def record_swap(
        self, sub_id: str, *, new_proxy_id: str, now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """免费换货：换绑代理 + 计次（不动 period_end/tokens——换货不是新购买）。"""
        ts = time.time() if now is None else float(now)
        with self._lock:
            self._conn.execute(
                "UPDATE proxy_subscriptions SET proxy_id=?, swap_count=swap_count+1,"
                " updated_at=? WHERE sub_id=? AND status=?",
                (str(new_proxy_id), ts, str(sub_id), STATUS_ACTIVE),
            )
            self._conn.commit()
        return self.get(sub_id, now=ts)

    def stats(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        """观测快照：活跃数 / 待对账数 / 累计扣费 / 最近到期 / 生命周期计数。"""
        out = {"active": 0, "unbilled": 0, "tokens_total": 0, "expiring_7d": 0,
               "renew_pending": 0, "expired_total": 0, "swaps_total": 0}
        ts = time.time() if now is None else float(now)
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(tokens),0) AS t,"
                    " COALESCE(SUM(CASE WHEN charged=0 THEN 1 ELSE 0 END),0) AS u,"
                    " COALESCE(SUM(swap_count),0) AS s"
                    " FROM proxy_subscriptions WHERE status=?", (STATUS_ACTIVE,),
                ).fetchone()
                out["active"] = int(row["n"] or 0)
                out["tokens_total"] = int(row["t"] or 0)
                out["unbilled"] = int(row["u"] or 0)
                out["swaps_total"] = int(row["s"] or 0)
                row2 = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM proxy_subscriptions "
                    "WHERE status=? AND period_end>0 AND period_end<=?",
                    (STATUS_ACTIVE, ts + 7 * 86400),
                ).fetchone()
                out["expiring_7d"] = int(row2["n"] or 0)
                row3 = self._conn.execute(
                    "SELECT COALESCE(SUM(CASE WHEN renew_state<>'' THEN 1 ELSE 0 END),0)"
                    " AS p,"
                    " COALESCE(SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END),0) AS e"
                    " FROM proxy_subscriptions",
                ).fetchone()
                out["renew_pending"] = int(row3["p"] or 0)
                out["expired_total"] = int(row3["e"] or 0)
        except Exception:
            logger.debug("[proxy_sub] stats 失败（返回零值）", exc_info=True)
        return out


_store: Optional[ProxySubscriptionStore] = None
_store_lock = threading.Lock()


def get_proxy_subscriptions(db_path: Optional[Path] = None) -> ProxySubscriptionStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                path = Path(db_path) if db_path else Path("config/proxy_subscriptions.db")
                _store = ProxySubscriptionStore(path)
    return _store


def reset_proxy_subscriptions() -> None:
    """测试钩子：丢弃单例（下次 get 重建）。"""
    global _store
    with _store_lock:
        _store = None


__all__ = [
    "RENEW_PENDING",
    "RENEW_UNBILLED",
    "STATUS_ACTIVE",
    "STATUS_CANCELLED",
    "STATUS_EXPIRED",
    "STATUS_RESERVED",
    "ProxySubscriptionStore",
    "build_charge_ref",
    "get_proxy_subscriptions",
    "reset_proxy_subscriptions",
]
