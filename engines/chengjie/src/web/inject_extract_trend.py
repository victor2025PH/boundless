"""注入「抽取率」按日落库（选择器失效遥测的阈值校准数据面）。

背景与定位
----------
``desktop_inject_health`` 的提取器判据当前是**归零判**（分母 > 0 而分子 == 0 才算失配）——
它抓得住「全坏」，抓不住「官方改版只坏了一半选择器」的部分失效（装饰率 60% 照样绿灯）。
要把判据升级成比率阈值（如「装饰率 < 30% 预警」），必须先知道**健康时的真实分布**：
不同平台的装饰率天然多少（WA 系统消息不装饰、TG service 气泡不计），拍脑袋定阈值只会
在某个平台上天天误报。本模块把每条健康上报的抽取计数按 (日, 平台, 账号) 增量 upsert
落地，攒 1-2 周后据分布定阈值（p10 下浮法，与 ``calibrate_naturalness_floor`` 同哲学）。

存什么（全部是计数，绝不落消息内容）：
- 正文侧：样本数 / decorated·unresolved 求和 + **装饰率直方图**（0 / (0,.3] / (.3,.7] /
  (.7,1) / 1 五桶）——日聚合求和会抹掉分布形状，而校准要的恰是分位数，桶是最省的保形法。
- 回流侧：ingest_tried·ingest_keyed 求和 + 归零样本数（tried>0 且 keyed==0）。

设计（严格对齐 ``identity_trend_store`` / ``translation_trend_store``）：
- **纯增量 upsert**：``INSERT ... ON CONFLICT DO UPDATE``，无周期快照线程，写在
  ``POST /api/desktop/inject-health`` 旁路（心跳 30s/账号，量级极小）。
- **默认关**：未 ``configure_inject_extract_trend(enabled=True, ...)`` → record 恒 no-op，零 IO。
- **模块级单例**；键含 account_id（单个坏座席不该污染整平台的分布，校准工具可按需聚合）。
- 无信号样本（正文没试过 && 回流没试过，如空会话）**不落行**——校准数据集保持干净，
  等桌面壳带 extract 计数的版本铺开后自然开始积累。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS inject_extract_daily (
    day        TEXT NOT NULL,
    platform   TEXT NOT NULL,
    account_id TEXT NOT NULL,
    reports          INTEGER NOT NULL DEFAULT 0,
    attempted_sum    INTEGER NOT NULL DEFAULT 0,
    decorated_sum    INTEGER NOT NULL DEFAULT 0,
    unresolved_sum   INTEGER NOT NULL DEFAULT 0,
    ratio_b0 INTEGER NOT NULL DEFAULT 0,
    ratio_b1 INTEGER NOT NULL DEFAULT 0,
    ratio_b2 INTEGER NOT NULL DEFAULT 0,
    ratio_b3 INTEGER NOT NULL DEFAULT 0,
    ratio_b4 INTEGER NOT NULL DEFAULT 0,
    ingest_reports   INTEGER NOT NULL DEFAULT 0,
    ingest_tried_sum INTEGER NOT NULL DEFAULT 0,
    ingest_keyed_sum INTEGER NOT NULL DEFAULT 0,
    ingest_zero      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, platform, account_id)
);
"""


def _day_str(now: Optional[float] = None) -> str:
    """UTC 日期键 ``YYYY-MM-DD``（与其余 trend store 同口径，跨时区部署一致）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


# 直方图桶的「上边界」＝可升级到的候选阈值（桶只有 5 档，分辨不出更细的分位）。
# b0=[0]、b1=(0,.3]、b2=(.3,.7]、b3=(.7,1)、b4=[1]；候选阈值取 b1/b2 的上沿。
_THRESHOLD_CANDIDATES = (0.7, 0.3)  # 从激进到保守，取第一个「误伤 ≤ 预算」的


def suggest_extract_threshold(
    agg: Dict[str, Any], *, min_samples: int = 300,
    max_false_positive: float = 0.05,
) -> Dict[str, Any]:
    """从抽取率直方图给出「归零判 → 比率阈值」的**建议**阈值（纯函数，不改告警行为）。

    与 ``calibrate_naturalness_floor`` 同哲学：机制先上线，样本门槛就是等待期——数据没攒够
    返回 ``insufficient`` + 阈值 0.0（维持现有归零判），攒够后**自动**给出可安全升级到的阈值，
    无需人回来定数字。**只出建议不改行为**：真正把归零判换成比率阈值仍是运营看数后的显式
    决定（阈值形状在真实分布上可能踩坑，不该由本函数直接改生产告警）。

    判据＝**误伤预算**而非拍脑袋的分位数：候选阈值 B（桶上沿 0.3/0.7）只有在「健康分布里
    装饰率 ≤ B 的占比 ≤ max_false_positive」时才可采纳，取满足预算的最高 B。都不满足 → 0.0
    （该平台天然低装饰、或当前正在坏——两种都不该升级：前者会天天误报，后者归零判已在告警）。
    这天然保守：坏着的平台 b0 占比高 → 拒绝升级 → 归零判继续兜底，不会因为「坏得多」反而放松。

    agg：``daily()`` 的一行或跨天聚合（含 ``reports`` / ``ratio_b0..b4``）。
    返回 ``{status, samples, threshold, frac_le_03, frac_le_07, zero_rate}``。
    """
    reports = max(0, int(agg.get("reports") or 0))
    b = [max(0, int(agg.get(f"ratio_b{i}") or 0)) for i in range(5)]
    zero_rate = round(b[0] / reports, 4) if reports else 0.0
    if reports < max(1, int(min_samples)):
        return {"status": "insufficient", "samples": reports,
                "threshold": 0.0, "frac_le_03": 0.0, "frac_le_07": 0.0,
                "zero_rate": zero_rate}
    fp = max(0.0, float(max_false_positive))
    frac_le_03 = (b[0] + b[1]) / reports
    frac_le_07 = (b[0] + b[1] + b[2]) / reports
    fracs = {0.3: frac_le_03, 0.7: frac_le_07}
    threshold = 0.0
    for cand in _THRESHOLD_CANDIDATES:  # 高→低，取第一个不超预算的
        if fracs[cand] <= fp:
            threshold = cand
            break
    return {"status": "ready", "samples": reports, "threshold": threshold,
            "frac_le_03": round(frac_le_03, 4),
            "frac_le_07": round(frac_le_07, 4), "zero_rate": zero_rate}


def suggest_extract_thresholds(
    rows: List[Dict[str, Any]], *, min_samples: int = 300,
    max_false_positive: float = 0.05,
) -> List[Dict[str, Any]]:
    """把 ``daily()`` 的 (日,平台) 多行**按平台跨天聚合**后逐平台出建议（纯函数）。

    校准要的是「这个平台整体健康时装饰率长什么样」，故跨天求和（单日样本少、噪声大）。
    返回按平台名排序的 ``[{platform, status, threshold, samples, ...}]``。
    """
    by_plat: Dict[str, Dict[str, int]] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        plat = str(r.get("platform") or "")
        if not plat:
            continue
        acc = by_plat.setdefault(
            plat, {"reports": 0, "ratio_b0": 0, "ratio_b1": 0,
                   "ratio_b2": 0, "ratio_b3": 0, "ratio_b4": 0})
        acc["reports"] += max(0, int(r.get("reports") or 0))
        for i in range(5):
            acc[f"ratio_b{i}"] += max(0, int(r.get(f"ratio_b{i}") or 0))
    out: List[Dict[str, Any]] = []
    for plat in sorted(by_plat):
        s = suggest_extract_threshold(
            by_plat[plat], min_samples=min_samples,
            max_false_positive=max_false_positive)
        s["platform"] = plat
        out.append(s)
    return out


def ratio_bucket(decorated: int, unresolved: int) -> Optional[int]:
    """装饰率 → 直方图桶号（0..4）；本次没尝试（分母 0）→ None（不计样本）。

    分母用 decorated+unresolved（本轮真正尝试过的气泡）而非 bubbles：已推送过的
    气泡会跳过 ingest 但仍计 decorated，bubbles 与尝试数在长会话里天然不相等。
    桶边界与「归零判 / 未来比率阈值」对齐：0 = 现在的 mismatch_text 判据；
    (0,.3] = 未来预警候选区；1 = 完全健康。
    """
    dec = max(0, int(decorated or 0))
    unr = max(0, int(unresolved or 0))
    attempted = dec + unr
    if attempted <= 0:
        return None
    r = dec / attempted
    if r <= 0:
        return 0
    if r <= 0.3:
        return 1
    if r <= 0.7:
        return 2
    if r < 1.0:
        return 3
    return 4


class InjectExtractTrendStore:
    """抽取率按日聚合（线程安全 SQLite）。"""

    def __init__(self, db_path: Any = ":memory:",
                 retention_days: float = 90.0) -> None:
        self._is_mem = str(db_path) == ":memory:"
        self.retention_days = max(1.0, float(retention_days or 90.0))
        if not self._is_mem:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, timeout=10,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if not self._is_mem:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._conn.commit()

    def add_sample(
        self, *, platform: str, account_id: str,
        decorated: int = 0, unresolved: int = 0,
        ingest_tried: int = 0, ingest_keyed: int = 0,
        now: Optional[float] = None,
    ) -> bool:
        """把一条上报的抽取计数计入当日聚合。无信号（两侧都没尝试）→ False 不落行。绝不抛。"""
        plat = str(platform or "").lower()
        acct = str(account_id or "")
        if not plat or not acct:
            return False
        dec = max(0, int(decorated or 0))
        unr = max(0, int(unresolved or 0))
        tried = max(0, int(ingest_tried or 0))
        keyed = max(0, int(ingest_keyed or 0))
        bucket = ratio_bucket(dec, unr)
        if bucket is None and tried <= 0:
            return False  # 空会话/无信号：不污染校准数据集
        text_n = 1 if bucket is not None else 0
        buckets = [0, 0, 0, 0, 0]
        if bucket is not None:
            buckets[bucket] = 1
        ing_n = 1 if tried > 0 else 0
        ing_zero = 1 if (tried > 0 and keyed == 0) else 0
        day = _day_str(now)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO inject_extract_daily (day, platform, account_id,"
                    " reports, attempted_sum, decorated_sum, unresolved_sum,"
                    " ratio_b0, ratio_b1, ratio_b2, ratio_b3, ratio_b4,"
                    " ingest_reports, ingest_tried_sum, ingest_keyed_sum, ingest_zero)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(day, platform, account_id) DO UPDATE SET"
                    "  reports = reports + excluded.reports,"
                    "  attempted_sum = attempted_sum + excluded.attempted_sum,"
                    "  decorated_sum = decorated_sum + excluded.decorated_sum,"
                    "  unresolved_sum = unresolved_sum + excluded.unresolved_sum,"
                    "  ratio_b0 = ratio_b0 + excluded.ratio_b0,"
                    "  ratio_b1 = ratio_b1 + excluded.ratio_b1,"
                    "  ratio_b2 = ratio_b2 + excluded.ratio_b2,"
                    "  ratio_b3 = ratio_b3 + excluded.ratio_b3,"
                    "  ratio_b4 = ratio_b4 + excluded.ratio_b4,"
                    "  ingest_reports = ingest_reports + excluded.ingest_reports,"
                    "  ingest_tried_sum = ingest_tried_sum + excluded.ingest_tried_sum,"
                    "  ingest_keyed_sum = ingest_keyed_sum + excluded.ingest_keyed_sum,"
                    "  ingest_zero = ingest_zero + excluded.ingest_zero",
                    (day, plat, acct, text_n, dec + unr, dec, unr,
                     buckets[0], buckets[1], buckets[2], buckets[3], buckets[4],
                     ing_n, tried, keyed, ing_zero),
                )
                self._conn.commit()
            return True
        except Exception:
            logger.debug("[inject_extract_trend] add_sample 失败（已忽略）", exc_info=True)
            return False

    def daily(self, *, days: int = 14,
              now: Optional[float] = None) -> List[Dict[str, Any]]:
        """近 N 天按 (日, 平台) 聚合（升序；跨账号求和 + 独立账号数）。

        只回有数据的行（消费方是校准工具/诊断读数，不是 sparkline，无需补零连线）；
        账号级明细校准工具直接读库文件（键里有 account_id，SQL 一句可分组）。
        """
        n = max(1, min(int(days or 14), 120))
        base = now if now is not None else time.time()
        since = _day_str(base - (n - 1) * 86400)
        rows: List[Dict[str, Any]] = []
        try:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT day, platform,"
                    " COUNT(DISTINCT account_id) AS accounts,"
                    " SUM(reports) AS reports,"
                    " SUM(attempted_sum) AS attempted_sum,"
                    " SUM(decorated_sum) AS decorated_sum,"
                    " SUM(unresolved_sum) AS unresolved_sum,"
                    " SUM(ratio_b0) AS ratio_b0, SUM(ratio_b1) AS ratio_b1,"
                    " SUM(ratio_b2) AS ratio_b2, SUM(ratio_b3) AS ratio_b3,"
                    " SUM(ratio_b4) AS ratio_b4,"
                    " SUM(ingest_reports) AS ingest_reports,"
                    " SUM(ingest_tried_sum) AS ingest_tried_sum,"
                    " SUM(ingest_keyed_sum) AS ingest_keyed_sum,"
                    " SUM(ingest_zero) AS ingest_zero"
                    " FROM inject_extract_daily WHERE day >= ?"
                    " GROUP BY day, platform ORDER BY day, platform",
                    (since,),
                ).fetchall()
                for r in cur:
                    d = {k: r[k] for k in r.keys()}
                    att = int(d.get("attempted_sum") or 0)
                    rep = int(d.get("reports") or 0)
                    ing = int(d.get("ingest_reports") or 0)
                    d["decorated_rate"] = (
                        round(int(d.get("decorated_sum") or 0) / att, 4) if att else 0.0)
                    d["zero_rate"] = (
                        round(int(d.get("ratio_b0") or 0) / rep, 4) if rep else 0.0)
                    d["ingest_zero_rate"] = (
                        round(int(d.get("ingest_zero") or 0) / ing, 4) if ing else 0.0)
                    rows.append(d)
        except Exception:
            logger.debug("[inject_extract_trend] daily 读取失败（已忽略）", exc_info=True)
            return []
        return rows

    def prune(self, *, retention_days: Optional[float] = None,
              now: Optional[float] = None) -> int:
        """删除超过保留期的旧日聚合（缺省用构造时配置的保留期）。返回删除条数。"""
        keep = self.retention_days if retention_days is None else max(
            0.0, float(retention_days))
        base = now if now is not None else time.time()
        cut = _day_str(base - keep * 86400)
        try:
            with self._lock:
                c = self._conn.execute(
                    "DELETE FROM inject_extract_daily WHERE day < ?", (cut,))
                self._conn.commit()
                return int(c.rowcount or 0)
        except Exception:
            logger.debug("[inject_extract_trend] prune 失败（已忽略）", exc_info=True)
            return 0


# ── 模块级单例 + 默认关闸门（与 identity_trend_store 同构）────────────────────
_STORE: Optional[InjectExtractTrendStore] = None
_ENABLED = False
_CFG_LOCK = threading.Lock()


def configure_inject_extract_trend(
    *, enabled: bool, db_path: Any = ":memory:", retention_days: float = 90.0,
) -> Optional[InjectExtractTrendStore]:
    """启动期装配（幂等）。``enabled=False`` → 关闭旁路写入（record 恒 no-op）。"""
    global _STORE, _ENABLED
    with _CFG_LOCK:
        _ENABLED = bool(enabled)
        if not _ENABLED:
            return _STORE
        if _STORE is None:
            try:
                _STORE = InjectExtractTrendStore(
                    db_path, retention_days=retention_days)
            except Exception:
                logger.warning("[inject_extract_trend] 建库失败，禁用落库", exc_info=True)
                _STORE = None
                _ENABLED = False
        return _STORE


def get_inject_extract_trend_store() -> Optional[InjectExtractTrendStore]:
    """供读端点取 store；未配置 → None。"""
    return _STORE


def record_inject_extract_trend(rec: Optional[Dict[str, Any]]) -> None:
    """健康上报旁路写入：未启用 / 无 store → 立即返回（零开销）。绝不抛。

    入参是 ``InjectHealthStore.record`` 返回的**规范记录**（extract 已归一 snake），
    防御性地仍走 ``_norm_extract`` 同一归一器——两处各自解析键名早晚漂移。
    """
    if not _ENABLED or _STORE is None:
        return
    r = rec or {}
    try:
        from src.web.desktop_inject_health import _norm_extract
        ex = _norm_extract(r.get("extract"))
        _STORE.add_sample(
            platform=str(r.get("platform") or ""),
            account_id=str(r.get("account_id") or ""),
            decorated=ex.get("decorated", 0),
            unresolved=ex.get("unresolved", 0),
            ingest_tried=ex.get("ingest_tried", 0),
            ingest_keyed=ex.get("ingest_keyed", 0),
        )
    except Exception:
        logger.debug("[inject_extract_trend] record 失败（已忽略）", exc_info=True)


def reset_inject_extract_trend() -> None:
    """测试钩子：清空单例与开关。"""
    global _STORE, _ENABLED
    with _CFG_LOCK:
        _STORE = None
        _ENABLED = False


__all__ = [
    "InjectExtractTrendStore",
    "ratio_bucket",
    "suggest_extract_threshold",
    "suggest_extract_thresholds",
    "configure_inject_extract_trend",
    "get_inject_extract_trend_store",
    "record_inject_extract_trend",
    "reset_inject_extract_trend",
]
