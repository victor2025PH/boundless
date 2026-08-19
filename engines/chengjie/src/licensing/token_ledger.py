"""Token 账本（2026-08-19 定价改版 P2 地基）：统一 AI 用量货币的计量 + 强制。

定位
====
2026-08-19 定价决议把「字符额度」升级为跨产品统一的 **Token**（AI 回复 / 专业翻译 /
克隆语音 / AI 配图 / ASR 共用一个钱包；标准翻译永久免费不计量）。本模块是引擎侧
单一账本，风格与语义**完全对齐** ``quota_store``（那套字符额度已在生产验证）：

- **check → do → record**：动作前 ``check_token_balance`` 闸门、成功后
  ``record_token_spend`` 记账——刻意不做 reserve/commit 两段式（字符管线同款取舍：
  额度窗口内最多多放行一笔，代价有界；两段式的复杂度/悬挂持仓风险不值）。
- **fail-open**：账本自身任何异常一律放行/吞掉，绝不把聊天主链打挂。
- **默认关**：``licensing.token_ledger.enabled``（config 缺省 False）——本模块当前
  **未接线**（P2 地基先落库和门禁，P3 才挂翻译/TTS/LLM 消费点），生产零行为变化。
- **本地路由零计费**是调用方契约：标准翻译（ollama_mt/内置引擎）、预渲染语音命中、
  缓存命中、本地 LLM 兜底 → 不调用 record（对齐官网「用尽自动降级永不断线」承诺，
  降级路径本来就该是免费路径）。

与字符额度的关系（迁移期并存）
==============================
旧 ``included_chars`` 字符池继续由 quota_store 强制（存量授权语义不变）；Token 钱包
是新增正交账本。官网口径「注册送 10,000 体验 Token = 现行 1M 字符」的并账（P3）：
按 ``CHARS_PER_TOKEN_LEGACY``（100 字符/Token，即专业翻译费率 10 Token/千字符）换算
一次性入包，此后新授权只发 Token。

计价表（对客公示口径）
======================
``TOKEN_RATES`` 与官网 ``website/lib/chatx-pricing.ts::TOKEN_RATES`` **必须逐项一致**
（门禁 ``tests/test_token_ledger.py::test_rates_match_website_source`` 在两仓同机时
交叉钉住；单独 CI 环境自动跳过）。改费率 = 产品决策，两处同批改。

批次与扣减顺序
==============
- 订阅月度含量：``grant_monthly``（ref 幂等键含 YYYY-MM），**当月月底过期**；
- Token 包：``grant_pack``（ref=订单号幂等），**12 个月过期**；
- 扣减 = 纯函数 ``allocate_spend``：累计支出按「先到期先扣」分摊到未过期批次，
  过期批次未用部分自然作废（不做逐笔 lot 落库——聚合支出 + 确定性分摊，账本
  行数与字符库同量级，审计可由 grants + spend 全量重放）。
"""
from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 稳定错误码：动作被 Token 余额阻断时透传上层（路由映射 i18n 文案）。
TOKEN_EXHAUSTED_ERROR = "token_balance_exhausted"

# ── 计价表（单位动作 → Token）。与官网 chatx-pricing.ts 同批改，勿单边动。────────
# unit_size 语义：per=每 unit_size 个计量单位收 tokens 个 Token（不足一档向上取整）。
TOKEN_RATES: Dict[str, Dict[str, Any]] = {
    # 标准翻译：永久免费（列进表=对外口径完整；record 对 0 费率天然零 IO）
    "std_translate":   {"tokens": 0,  "unit": "chars",   "unit_size": 1000},
    "ai_reply":        {"tokens": 10, "unit": "message", "unit_size": 1},
    "pro_translate":   {"tokens": 10, "unit": "chars",   "unit_size": 1000},
    "deepl_translate": {"tokens": 40, "unit": "chars",   "unit_size": 1000},
    "voice_clone":     {"tokens": 10, "unit": "chars",   "unit_size": 100},
    "ai_image":        {"tokens": 50, "unit": "image",   "unit_size": 1},
    "asr":             {"tokens": 5,  "unit": "minutes", "unit_size": 1},
}

# 旧字符池 → Token 并账换算（1 Token = 100 字符，即专业翻译 10 Token/千字符口径）。
CHARS_PER_TOKEN_LEGACY = 100

# Token 包有效期（月）与订阅月含量的自然过期语义。
PACK_VALID_MONTHS = 12


def tokens_for(action: str, units: float) -> int:
    """动作用量 → Token 数（不足一档向上取整；未知动作按 0=不计费并 debug 提示）。

    例：pro_translate 1500 chars → ceil(1500/1000)×10 = 20 Token；
        voice_clone 40 chars → ceil(40/100)×10 = 10 Token；
        ai_reply 1 message → 10 Token。
    """
    rate = TOKEN_RATES.get(str(action or ""))
    if rate is None:
        logger.debug("[token_ledger] 未知动作 %r（按 0 计费）", action)
        return 0
    if rate["tokens"] <= 0:
        return 0
    u = float(units or 0)
    if u <= 0:
        return 0
    blocks = math.ceil(u / float(rate["unit_size"]))
    return int(blocks * rate["tokens"])


# ── 纯函数核算：先到期先扣，过期未用作废 ───────────────────────────────────────

def allocate_spend(
    grants: List[Tuple[int, Optional[float]]], total_spend: int, now: float,
) -> Dict[str, Any]:
    """把累计支出分摊到批次，返回 {balance, active_granted, expired_lost, spend_unmet}。

    grants: [(tokens, expires_at_epoch|None), ...]（None=永不过期）。
    规则：按**到期时间升序**（先到期先扣，None 最后）逐批吸收支出；
    - 已过期批次只吸收「其过期前理应已发生」的支出？——刻意不做时间序重放
      （需要逐笔 spend 时间戳与批次配对，复杂且对账收益低）；简化为**保守口径**：
      已过期批次直接作废、不吸收任何支出，支出全部压在未过期批次上。
      该口径对客户**永远不多扣**（过期作废的是我们送出的额度，支出压在活批次上
      只会让余额显示更低=更保守），与「订阅含量当月有效」的对外承诺一致。
    """
    active: List[Tuple[int, Optional[float]]] = []
    expired_lost = 0
    for tokens, exp in grants:
        t = int(tokens or 0)
        if t <= 0:
            continue
        if exp is not None and exp <= now:
            expired_lost += t
        else:
            active.append((t, exp))
    # 先到期先扣（None=最晚）
    active.sort(key=lambda g: (g[1] is None, g[1] if g[1] is not None else 0))
    remaining_spend = max(0, int(total_spend or 0))
    balance = 0
    for t, _exp in active:
        absorbed = min(t, remaining_spend)
        remaining_spend -= absorbed
        balance += t - absorbed
    return {
        "balance": balance,
        "active_granted": sum(t for t, _ in active),
        "expired_lost": expired_lost,
        "spend_unmet": remaining_spend,  # >0 = 历史支出超过现存批次（正常：老批次过期后的痕迹）
    }


def _month_end_epoch(now: Optional[float] = None) -> float:
    """当月最后一秒（UTC）——订阅月度含量的过期时刻。"""
    t = time.gmtime(now if now is not None else time.time())
    if t.tm_mon == 12:
        nxt = time.struct_time((t.tm_year + 1, 1, 1, 0, 0, 0, 0, 0, 0))
    else:
        nxt = time.struct_time((t.tm_year, t.tm_mon + 1, 1, 0, 0, 0, 0, 0, 0))
    import calendar

    return calendar.timegm(nxt) - 1


def _pack_expiry_epoch(now: Optional[float] = None) -> float:
    """Token 包过期时刻 = 入账起 PACK_VALID_MONTHS 个自然月（近似 30.44 天/月）。"""
    base = now if now is not None else time.time()
    return base + PACK_VALID_MONTHS * 30.44 * 86400


_DDL = """
CREATE TABLE IF NOT EXISTS token_grants (
    ref        TEXT PRIMARY KEY,
    lic_id     TEXT NOT NULL,
    tokens     INTEGER NOT NULL,
    kind       TEXT NOT NULL,             -- pack | monthly | bonus | migration
    expires_at REAL,                      -- epoch 秒；NULL=永不过期
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_grants_lic ON token_grants (lic_id);
CREATE TABLE IF NOT EXISTS token_spend (
    lic_id  TEXT NOT NULL,
    day     TEXT NOT NULL,
    action  TEXT NOT NULL,
    tokens  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (lic_id, day, action)
);
"""


def _day_str(now: Optional[float] = None) -> str:
    """UTC 日期键 ``YYYY-MM-DD``（与 quota_store/tts_cost_store 同口径）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


class TokenLedgerStore:
    """按授权聚合的 Token 批次 + 支出账本（线程安全 SQLite；接口风格 = LicenseQuotaStore）。"""

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
            self._conn.commit()

    # ── 批次入账（全部幂等 ref）─────────────────────────────────────────────

    def _grant(
        self, ref: str, lic_id: str, tokens: int, kind: str,
        expires_at: Optional[float], note: str = "",
    ) -> bool:
        n = int(tokens or 0)
        r = str(ref or "").strip()
        if n <= 0 or not r:
            return False
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO token_grants (ref, lic_id, tokens, kind, expires_at, note, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (r, str(lic_id or "default"), n, kind, expires_at, str(note or ""),
                     time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())),
                )
                self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # 同 ref 已入账（幂等拒绝）
        except Exception:
            logger.debug("[token_ledger] grant 失败（已忽略）", exc_info=True)
            return False

    def grant_pack(
        self, lic_id: str, tokens: int, ref: str, note: str = "",
        *, now: Optional[float] = None,
    ) -> bool:
        """Token 包入账（ref=订单号；12 个月有效）。重复 ref → False。"""
        return self._grant(ref, lic_id, tokens, "pack", _pack_expiry_epoch(now), note)

    def grant_monthly(
        self, lic_id: str, tokens: int, *, now: Optional[float] = None, note: str = "",
    ) -> bool:
        """订阅月度含量入账（幂等键=授权+自然月；当月月底过期）。

        每月首次任意 check/record 触发即可（调用方无需定时任务），重复调用天然拒绝。
        """
        month = time.strftime("%Y-%m", time.gmtime(now if now is not None else time.time()))
        ref = f"monthly:{lic_id or 'default'}:{month}"
        return self._grant(ref, lic_id, tokens, "monthly", _month_end_epoch(now), note or month)

    def grant_bonus(self, lic_id: str, tokens: int, ref: str, note: str = "") -> bool:
        """注册奖励 / 字符池并账（migration）等一次性批次（永不过期）。"""
        return self._grant(ref, lic_id, tokens, "bonus", None, note)

    # ── 支出与核算 ─────────────────────────────────────────────────────────

    def record_spend(
        self, lic_id: str, action: str, tokens: int, *, now: Optional[float] = None,
    ) -> None:
        """记一笔已消耗 Token 到当日聚合（成功后调用）。绝不抛。"""
        n = int(tokens or 0)
        if n <= 0:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO token_spend (lic_id, day, action, tokens) VALUES (?,?,?,?) "
                    "ON CONFLICT(lic_id, day, action) DO UPDATE SET tokens = tokens + excluded.tokens",
                    (str(lic_id or "default"), _day_str(now), str(action or "other"), n),
                )
                self._conn.commit()
        except Exception:
            logger.debug("[token_ledger] record_spend 失败（已忽略）", exc_info=True)

    def _grants(self, lic_id: str) -> List[Tuple[int, Optional[float]]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tokens, expires_at FROM token_grants WHERE lic_id = ?",
                (str(lic_id or "default"),),
            ).fetchall()
        return [(int(r["tokens"]), r["expires_at"]) for r in rows]

    def total_spend(self, lic_id: str) -> int:
        """历史累计支出。读失败按 0（不误伤放行）。"""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(tokens),0) AS s FROM token_spend WHERE lic_id = ?",
                    (str(lic_id or "default"),),
                ).fetchone()
            return int(row["s"] or 0)
        except Exception:
            logger.debug("[token_ledger] total_spend 读取失败（按 0）", exc_info=True)
            return 0

    def balance(self, lic_id: str, *, now: Optional[float] = None) -> Dict[str, Any]:
        """余额核算快照：{balance, active_granted, expired_lost, spend_unmet, total_spend}。"""
        out = {"balance": 0, "active_granted": 0, "expired_lost": 0,
               "spend_unmet": 0, "total_spend": 0}
        try:
            spend = self.total_spend(lic_id)
            alloc = allocate_spend(
                self._grants(lic_id), spend, now if now is not None else time.time(),
            )
            out.update(alloc)
            out["total_spend"] = spend
        except Exception:
            logger.debug("[token_ledger] balance 核算失败（返回零值）", exc_info=True)
        return out

    def usage(self, lic_id: str) -> Dict[str, Any]:
        """观测快照：{today, by_action, grants: [..]}（会员页 Token 钱包数据源）。"""
        out: Dict[str, Any] = {"today": 0, "by_action": {}, "grants": []}
        try:
            today = _day_str()
            with self._lock:
                for r in self._conn.execute(
                    "SELECT action, SUM(tokens) AS s FROM token_spend "
                    "WHERE lic_id = ? GROUP BY action", (str(lic_id or "default"),),
                ).fetchall():
                    out["by_action"][str(r["action"])] = int(r["s"] or 0)
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(tokens),0) AS s FROM token_spend "
                    "WHERE lic_id = ? AND day = ?", (str(lic_id or "default"), today),
                ).fetchone()
                out["today"] = int(row["s"] or 0)
                out["grants"] = [
                    dict(r) for r in self._conn.execute(
                        "SELECT ref, tokens, kind, expires_at, created_at FROM token_grants "
                        "WHERE lic_id = ? ORDER BY created_at DESC LIMIT 50",
                        (str(lic_id or "default"),),
                    ).fetchall()
                ]
        except Exception:
            logger.debug("[token_ledger] usage 读取失败（返回零值）", exc_info=True)
        return out


# ── 模块级单例 + 配置闸（与 quota_store 同构；当前未接线，默认关）───────────────
_STORE: Optional[TokenLedgerStore] = None
_DB_PATH: Optional[str] = None
_CFG_LOCK = threading.Lock()
_WARNED_LIC_IDS: set = set()


def _default_db_path() -> str:
    from src.licensing.data_paths import data_file

    return data_file("token_ledger.db")


def configure_token_ledger(
    *, db_path: Any = None, store: Optional[TokenLedgerStore] = None,
) -> Optional[TokenLedgerStore]:
    """启动期装配（可选）：覆盖 db 路径或直接注入 store（测试用）。幂等。"""
    global _STORE, _DB_PATH
    with _CFG_LOCK:
        if store is not None:
            _STORE = store
        if db_path is not None:
            _DB_PATH = str(db_path)
        return _STORE


def get_token_ledger() -> Optional[TokenLedgerStore]:
    return _STORE


def reset_token_ledger() -> None:
    """测试钩子：清空单例/路径/告警去重。"""
    global _STORE, _DB_PATH
    with _CFG_LOCK:
        _STORE = None
        _DB_PATH = None
        _WARNED_LIC_IDS.clear()


def _ensure_store() -> Optional[TokenLedgerStore]:
    global _STORE
    with _CFG_LOCK:
        if _STORE is None:
            try:
                _STORE = TokenLedgerStore(_DB_PATH or _default_db_path())
            except Exception:
                logger.warning("[token_ledger] 建库失败，Token 计量禁用", exc_info=True)
                _STORE = None
        return _STORE


def token_ledger_enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """总闸 ``licensing.token_ledger.enabled``（默认 False——P2 地基未接线）。"""
    try:
        if cfg is None:
            from src.utils.config import get_config  # type: ignore

            cfg = get_config() or {}
        lic = (cfg.get("licensing") or {}) if isinstance(cfg, dict) else {}
        tl = lic.get("token_ledger") or {}
        return bool(tl.get("enabled", False))
    except Exception:
        return False


def check_token_balance(
    lic_id: str, *, enforce: bool = False, now: Optional[float] = None,
) -> Dict[str, Any]:
    """Token 闸门：{allowed, exhausted, balance, ...}。

    - enforce=False（默认）→ 余额耗尽仅 warn 一次/授权，恒放行（调用方走降级路径）；
    - enforce=True → 余额 <=0 时 allowed=False（调用方以 TOKEN_EXHAUSTED_ERROR
      切换免费引擎——**永不断线**是对外承诺，阻断的是付费动作不是消息本身）；
    - 自身任何异常 → 放行。
    """
    out: Dict[str, Any] = {"allowed": True, "exhausted": False, "balance": 0,
                           "enforce": bool(enforce), "lic_id": str(lic_id or "default")}
    try:
        store = _ensure_store()
        if store is None:
            return out
        bal = store.balance(out["lic_id"], now=now)
        out.update(bal)
        out["exhausted"] = bal["balance"] <= 0 and bal["active_granted"] > 0
        # 从未有任何批次（active_granted=0 且零支出）视为「未启用 Token 钱包」→ 放行不告警
        if bal["active_granted"] == 0 and bal["total_spend"] == 0:
            return out
        if out["exhausted"]:
            if enforce:
                out["allowed"] = False
            elif out["lic_id"] not in _WARNED_LIC_IDS:
                _WARNED_LIC_IDS.add(out["lic_id"])
                logger.warning(
                    "[token_ledger] 授权 %s Token 余额耗尽（active=%s spend=%s）；"
                    "enforce 未开启，仅提醒不阻断",
                    out["lic_id"], bal["active_granted"], bal["total_spend"],
                )
    except Exception:
        logger.debug("[token_ledger] check 失败（放行）", exc_info=True)
    return out


def record_token_action(
    lic_id: str, action: str, units: float, *, now: Optional[float] = None,
) -> int:
    """成功交付后记账（费率表内算好 Token 再落账）。返回记账 Token 数；绝不抛。"""
    try:
        n = tokens_for(action, units)
        if n <= 0:
            return 0
        store = _ensure_store()
        if store is None:
            return 0
        store.record_spend(lic_id, action, n, now=now)
        return n
    except Exception:
        logger.debug("[token_ledger] record_token_action 失败（已忽略）", exc_info=True)
        return 0


__all__ = [
    "CHARS_PER_TOKEN_LEGACY",
    "PACK_VALID_MONTHS",
    "TOKEN_EXHAUSTED_ERROR",
    "TOKEN_RATES",
    "TokenLedgerStore",
    "allocate_spend",
    "check_token_balance",
    "configure_token_ledger",
    "get_token_ledger",
    "record_token_action",
    "reset_token_ledger",
    "token_ledger_enabled",
    "tokens_for",
]
