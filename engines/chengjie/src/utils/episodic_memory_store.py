"""
Episodic memory: short, persistent user-specific facts for multi-session continuity.
Stored in SQLite (default: same file as ContextStore bot.db), separate table.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("EpisodicMemoryStore")

# R3：稳定层（已巩固的人设级记忆）在重排时的小幅加权（仅 use_salience_rerank 时生效）
_STABLE_TIER_BOOST = 0.05

# 去重灰区带宽：cos ∈ [threshold-带宽, threshold) 记为「差一点就并」观测对。
# 0.17 取自 2026-07-26 bge-m3 生产校准（应并组 min 0.741 vs 默认阈 0.92）。
_DEDUP_GRAY_BAND = 0.17

# bullets 年龄标注：raw 层事实距今 ≥ 该秒数才加「（X天前提到）」后缀——
# 持久事实 48h 内不标注（防噪声）；stable 层永不标注。
# 瞬态事实（天气/当下动作，与抽取「持久性铁律」同族）6h 即标注——
# 否则旧雨事实要等满 48h 仍以现在时注入（2026-07-27 生产窗口事故）。
_AGE_HINT_MIN_SEC = 48 * 3600
_AGE_HINT_TRANSIENT_SEC = 6 * 3600
_TRANSIENT_FACT_RE = re.compile(
    r"(下雨|下大雨|在下雨|好热|好冷|天气|在吃饭|刚到家|好困|正在)"
)


def _looks_transient_fact(text: str) -> bool:
    """内容像转瞬状态（天气/当下动作）→ 用更短的年龄标注门槛。"""
    return bool(_TRANSIENT_FACT_RE.search(text or ""))


# #96（0830 Steven 实锤）：称呼/名字类事实判定——无溯源旧条目降权只对这一类
# 生效（这类条目方向抽反的后果是「拿人设名叫客户」级身份穿帮，其余旧条目
# 不背这个险）。词表保守：称呼语义的显式标记词，不碰泛泛的「叫」单字
# （「用户叫外卖」会误伤）。
_NAME_CLASS_FACT_RE = re.compile(
    r"(称呼|自称|昵称|名字|叫我|叫她|叫他|叫TA|希望我叫|希望被叫|"
    r"(?i:call(?:ed)?\s+(?:me|him|her|them)|name\s+is|'s\s+name|nickname))"
)


def _looks_name_class_fact(text: str) -> bool:
    """内容像称呼/名字类事实（#96 无溯源降权的适用面判定）。"""
    return bool(_NAME_CLASS_FACT_RE.search(text or ""))


def _age_hint_label(age_sec: float) -> str:
    """秒龄 → 粗粒度中文年龄串（N小时/天/周/月）。"""
    if age_sec < 86400:
        return f"{max(1, int(age_sec // 3600))}小时"
    days = int(age_sec // 86400)
    if days < 14:
        return f"{days}天"
    if days < 60:
        return f"{days // 7}周"
    return f"{days // 30}个月"


def compute_memory_storage_key(scope: str, user_id_str: str, chat_id: Any) -> str:
    """
    scope=user → 仅 user_id；scope=chat_user → 群为「chat_user_id」，私聊 chat_id==user 时退化为 user。
    """
    if (scope or "user") != "chat_user":
        return user_id_str
    try:
        cid = int(chat_id) if chat_id is not None and str(chat_id).strip() != "" else 0
    except (TypeError, ValueError):
        cid = 0
    try:
        uid = int(str(user_id_str).strip()) if str(user_id_str).strip().isdigit() else 0
    except (TypeError, ValueError):
        uid = 0
    if cid != 0 and uid != 0 and cid == uid:
        return user_id_str
    if cid == 0:
        return user_id_str
    return f"{cid}_{user_id_str}"


def strip_composite_user_id(user_id: str, platform: str = "") -> str:
    """防「完整记忆键回喂」（2026-07-27 实锤：user_identity_map 出现
    ``whatsapp:whatsapp:acct:peer`` / ``acct:whatsapp:acct:peer`` 翻倍键，
    最早 07-20——某些调用方把 conversation_id / canonical 当 chat_key 传）。

    user_id 若以 ``<platform>:`` 开头则剥掉（可重复剥治嵌套翻倍）；真实
    peer id（数字 / U-hex / jid）不可能以平台名+冒号开头，零误伤。
    platform 未知时不动（此时也不会走 CPI resolve，读写自洽）。
    """
    uid = str(user_id or "")
    plat = str(platform or "").strip().lower()
    if not uid or not plat:
        return uid
    prefix = plat + ":"
    while uid.lower().startswith(prefix) and len(uid) > len(prefix):
        uid = uid[len(prefix):]
    return uid


def _norm_for_hash(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t[:500]


def content_hash_of(text: str) -> str:
    """事实文本 → 与 ``add_fact`` 落库口径一致的 content_hash（公开工具）。

    导入批次台账（P1 2026-08-18）用它登记事实指纹，撤销按 (key, hash) 精确删；
    与写入端共用 ``_norm_for_hash`` 归一化，绝不各算一套。
    """
    return hashlib.sha256(_norm_for_hash(text).encode("utf-8")).hexdigest()


class EpisodicMemoryStore:
    _DDL = """
    CREATE TABLE IF NOT EXISTS episodic_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'general',
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_epi_user_created ON episodic_memory(user_id, created_at DESC);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_epi_user_hash ON episodic_memory(user_id, content_hash);
    """

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        # 进程级去重观测（2026-07-27）：灰区对/归并数累计，经
        # /api/workspace/metrics.episodic_dedup 出看板——「要不要上 LLM 仲裁合并」
        # 的两周观察期直接读数，不用翻日志。
        self._dedup_stats: Dict[str, Any] = {
            "scans": 0, "merged": 0, "gray_pairs": 0,
            "held_merges": 0, "observe_only_scans": 0,
            "last_gray_at": 0.0, "last_gray_example": "",
        }
        self._init_db()

    def _ensure_embedding_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(episodic_memory)")
        cols = [str(r[1]) for r in cur.fetchall()]
        if "embedding" not in cols:
            self._conn.execute("ALTER TABLE episodic_memory ADD COLUMN embedding BLOB")
            self._conn.commit()
            logger.info("episodic_memory: added column embedding")

    def _ensure_embedding_meta_columns(self) -> None:
        """记录每条向量的来源模型名与维度（``embedding_model`` / ``embedding_dim``）。

        换 embedding 模型＝换向量空间：历史坑是 nomic(768) 与 bge-m3(1024) 混存，余弦对
        不等长向量本应判 0，一旦有代码 min() 截断就会算出假相似度。有了模型/维度标注，可
        精准识别并清理/重嵌异维度旧向量（``... WHERE embedding_dim <> ?``），不再靠 blob
        字节长度反推。旧行默认空/0＝未知（不影响既有行为）。
        """
        cur = self._conn.execute("PRAGMA table_info(episodic_memory)")
        cols = [str(r[1]) for r in cur.fetchall()]
        changed = False
        if "embedding_model" not in cols:
            self._conn.execute(
                "ALTER TABLE episodic_memory ADD COLUMN embedding_model TEXT NOT NULL DEFAULT ''"
            )
            changed = True
        if "embedding_dim" not in cols:
            self._conn.execute(
                "ALTER TABLE episodic_memory ADD COLUMN embedding_dim INTEGER NOT NULL DEFAULT 0"
            )
            changed = True
        if changed:
            self._conn.commit()
            logger.info("episodic_memory: added embedding_model/embedding_dim columns")

    def _ensure_consolidation_columns(self) -> None:
        """R3：分层巩固所需列（写入即定权 + 复发计数 + 分层），向后兼容 ALTER。"""
        cur = self._conn.execute("PRAGMA table_info(episodic_memory)")
        cols = [str(r[1]) for r in cur.fetchall()]
        migrations = [
            ("salience", "ALTER TABLE episodic_memory ADD COLUMN salience REAL"),
            ("tier", "ALTER TABLE episodic_memory ADD COLUMN tier TEXT NOT NULL DEFAULT 'raw'"),
            ("hits", "ALTER TABLE episodic_memory ADD COLUMN hits INTEGER NOT NULL DEFAULT 1"),
            ("last_seen", "ALTER TABLE episodic_memory ADD COLUMN last_seen REAL"),
        ]
        changed = False
        for col, ddl in migrations:
            if col not in cols:
                self._conn.execute(ddl)
                changed = True
                logger.info("episodic_memory: added column %s", col)
        if changed:
            # 老行 last_seen 回填为 created_at，便于衰减/巩固判断
            self._conn.execute(
                "UPDATE episodic_memory SET last_seen = created_at WHERE last_seen IS NULL"
            )
            self._conn.commit()

    def _ensure_source_column(self) -> None:
        """R12：事实来源标注列（``user_stated`` / ``ai_inferred``），向后兼容 ALTER。

        旧行默认 ``user_stated``（视既有事实为用户明说，行为不变）；新写入由调用方按
        来源标注——启发式（从用户原话正则提取）= ``user_stated``，LLM 抽取（对话推断/
        概括）= ``ai_inferred``。用于晋升/推翻 stable 时按置信分级。
        """
        cur = self._conn.execute("PRAGMA table_info(episodic_memory)")
        cols = [str(r[1]) for r in cur.fetchall()]
        if "source" not in cols:
            self._conn.execute(
                "ALTER TABLE episodic_memory ADD COLUMN source TEXT NOT NULL"
                " DEFAULT 'user_stated'"
            )
            self._conn.commit()
            logger.info("episodic_memory: added column source")

    def _ensure_provenance_columns(self) -> None:
        """记忆五件套·来源溯源（#41 0830 定稿）：``source_quote``（抽取自哪句
        原话）+ ``source_ts``（那句话的时刻）。

        实锤缺口：「用户不喜欢被叫 babe」这条 AI 推断正在反向教唆称呼互换
        （#24），而「这条推断从哪来的」连运维都答不上——溯源列让每条记忆可
        回答「抽取自哪句原话、何时」，运营一眼判真伪再决定确认/编辑/删除。
        旧行默认空串/0＝无溯源（如实展示「早期条目无来源记录」）。
        """
        cur = self._conn.execute("PRAGMA table_info(episodic_memory)")
        cols = [str(r[1]) for r in cur.fetchall()]
        changed = False
        if "source_quote" not in cols:
            self._conn.execute(
                "ALTER TABLE episodic_memory ADD COLUMN source_quote TEXT"
                " NOT NULL DEFAULT ''")
            changed = True
        if "source_ts" not in cols:
            self._conn.execute(
                "ALTER TABLE episodic_memory ADD COLUMN source_ts REAL"
                " NOT NULL DEFAULT 0")
            changed = True
        if changed:
            self._conn.commit()
            logger.info("episodic_memory: added source_quote/source_ts columns")

    # J-10 A1（#183）：接地护栏丢弃记账——「外语客户零记忆」此前只在日志 WARNING 里，
    # 页面完全看不出来。按原因（no_evidence / evidence_mismatch / fact_unanchored）落表，
    # admin_summary 出 7 天读数 → 页面「有 N 条因无法核对原话未记录」。fact/evidence
    # 截短只作核对示例。
    _GROUNDING_DROPS_DDL = """
    CREATE TABLE IF NOT EXISTS episodic_grounding_drops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        user_id TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL,
        fact TEXT NOT NULL DEFAULT '',
        evidence TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_epi_gdrop_ts ON episodic_grounding_drops(ts);
    """
    _GROUNDING_DROPS_RETAIN_SEC = 30 * 86400

    def _ensure_grounding_drops_table(self) -> None:
        try:
            self._conn.executescript(self._GROUNDING_DROPS_DDL)
            self._conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic_grounding_drops ddl failed: %s", e)

    # J-10 A3（#183）：召回记账——哪条记忆在哪个会话的哪一轮被注入过 prompt。此前使用侧
    # 零埋点：算不出「被用次数 / 贡献」，也做不了「草稿旁可见」。skill_manager 注入点
    # 一行写入（best-effort 零阻断）；/api/episodic-memory/used 按会话回读最近几轮。
    _RECALL_LOG_DDL = """
    CREATE TABLE IF NOT EXISTS episodic_recall_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        batch_id TEXT NOT NULL DEFAULT '',
        memory_key TEXT NOT NULL DEFAULT '',
        row_id INTEGER NOT NULL,
        conversation_id TEXT NOT NULL DEFAULT '',
        chain TEXT NOT NULL DEFAULT '',
        draft_or_reply_id TEXT NOT NULL DEFAULT '',
        inbound_msg_id TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_epi_recall_conv ON episodic_recall_log(conversation_id, ts DESC);
    CREATE INDEX IF NOT EXISTS idx_epi_recall_key ON episodic_recall_log(memory_key, ts DESC);
    CREATE INDEX IF NOT EXISTS idx_epi_recall_row ON episodic_recall_log(row_id);
    """
    _RECALL_LOG_RETAIN_SEC = 90 * 86400

    def _ensure_recall_log_table(self) -> None:
        try:
            self._conn.executescript(self._RECALL_LOG_DDL)
            self._conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic_recall_log ddl failed: %s", e)

    def _ensure_review_columns(self) -> None:
        """J-10 A2（#183，决策 D8）例外审核 + 软删 + 召回计数列，向后兼容幂等 ALTER。

        - ``review_reason``：``conflict`` / ``high_impact`` / ``low_confidence`` /
          ``self_fact`` / ``commitment`` / 空（空＝不需要人看，照常召回）；
        - ``impact``：``high`` / ``normal``（敏感类页面标红，不阻断记录）；
        - ``status``：``active`` / ``ignored``（软删：不召回、可恢复；「过时」沿用
          ``tier='stale'``，不双写）；
        - ``conflict_group``：与同槽 stable 冲突时两条并列的组键（不自动覆盖）；
        - ``recall_count`` / ``last_recalled_ts``：注入 prompt 的次数与最近时刻
          （A3 召回记账写入；A2 自动转正读它）。
        旧行默认：不需审核 / normal / active / 未召回——行为不变。
        """
        cur = self._conn.execute("PRAGMA table_info(episodic_memory)")
        cols = [str(r[1]) for r in cur.fetchall()]
        migrations = [
            ("review_reason", "ALTER TABLE episodic_memory ADD COLUMN review_reason TEXT NOT NULL DEFAULT ''"),
            ("impact", "ALTER TABLE episodic_memory ADD COLUMN impact TEXT NOT NULL DEFAULT 'normal'"),
            ("status", "ALTER TABLE episodic_memory ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"),
            ("conflict_group", "ALTER TABLE episodic_memory ADD COLUMN conflict_group TEXT NOT NULL DEFAULT ''"),
            ("recall_count", "ALTER TABLE episodic_memory ADD COLUMN recall_count INTEGER NOT NULL DEFAULT 0"),
            ("last_recalled_ts", "ALTER TABLE episodic_memory ADD COLUMN last_recalled_ts REAL NOT NULL DEFAULT 0"),
        ]
        changed = False
        for col, ddl in migrations:
            if col not in cols:
                self._conn.execute(ddl)
                changed = True
                logger.info("episodic_memory: added column %s", col)
        try:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_epi_review"
                " ON episodic_memory(status, review_reason)")
        except Exception as e:  # noqa: BLE001
            logger.debug("idx_epi_review failed: %s", e)
        if changed:
            self._conn.commit()

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.executescript(self._DDL)
        self._conn.commit()
        self._ensure_embedding_column()
        self._ensure_embedding_meta_columns()
        self._ensure_consolidation_columns()
        self._ensure_source_column()
        self._ensure_provenance_columns()
        self._ensure_grounding_drops_table()
        self._ensure_review_columns()
        self._ensure_recall_log_table()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def add_fact(
        self,
        user_id: str,
        content: str,
        category: str = "general",
        embedding_blob: Optional[bytes] = None,
        source: str = "user_stated",
        embedding_model: str = "",
        source_quote: str = "",
        source_ts: float = 0.0,
        confidence: Optional[float] = None,
        author: str = "",
        review_reason: Optional[str] = None,
        impact: Optional[str] = None,
    ) -> Optional[int]:
        """Insert one fact; returns new row id, or None if duplicate / failed.

        R3：写入即算情绪显著性落 ``salience`` 列；重复事实（同 hash）不再插入，但
        **累加 ``hits`` 并刷新 ``last_seen``**——把"反复提起"沉淀为复发信号，供
        ``consolidate`` 把高复发事实晋升为 ``stable`` 稳定层。重复仍返回 None（向后兼容）。

        R12：``source`` 标注来源（``user_stated`` / ``ai_inferred``），供 source-aware
        巩固/推翻按置信分级（默认 ``user_stated``，兼容旧调用）。

        五件套（#41 0830）：``source_quote``/``source_ts`` 记「抽取自哪句原话、
        何时」——空值兼容旧调用（无溯源）；quote 截 200 字防原话超长撑库。

        J-10 A2（D8）：写入即打例外标——``memory_review.classify_fact`` 四类
        （self_fact / commitment / high_impact / low_confidence，``confidence`` /
        ``author`` 是它的输入）+ 本方法查同槽 ``stable`` 得第五类 ``conflict``
        （两条并列 ``conflict_group``，**不再自动覆盖** stable）。``review_reason`` /
        ``impact`` 显式传入则不再自算（导入/测试用）。
        """
        c = (content or "").strip()
        if len(c) < 2 or len(c) > 500:
            return None
        src = source if source in ("user_stated", "ai_inferred") else "user_stated"
        h = hashlib.sha256(_norm_for_hash(c).encode("utf-8")).hexdigest()
        now = time.time()
        sal = self._compute_salience(c)
        _rr, _imp = self._review_tags(
            user_id, c, source=src, confidence=confidence, author=author,
            review_reason=review_reason, impact=impact)
        _cgroup = ""
        if _rr == "conflict":
            _cgroup = self._conflict_group_for(user_id, c)
        try:
            _emb_dim = (len(embedding_blob) // 4) if embedding_blob else 0
            _emb_model = (embedding_model or "") if embedding_blob else ""
            _quote = str(source_quote or "").strip()[:200]
            try:
                _sts = float(source_ts or 0.0)
            except (TypeError, ValueError):
                _sts = 0.0
            cur = self._conn.execute(
                "INSERT INTO episodic_memory (user_id, content, content_hash, category,"
                " created_at, embedding, embedding_model, embedding_dim,"
                " salience, tier, hits, last_seen, source, source_quote, source_ts,"
                " review_reason, impact, status, conflict_group)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'raw', 1, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (user_id, c, h, (category or "general")[:32], now, embedding_blob,
                 _emb_model, _emb_dim, sal, now, src, _quote,
                 (_sts if _sts > 0 else now), _rr, _imp, _cgroup),
            )
            self._conn.commit()
            return int(cur.lastrowid) if cur.lastrowid else None
        except sqlite3.IntegrityError:
            # 复发：累加 hits + 刷新 last_seen（不新增行；返回 None 保持兼容）。
            # R12：若本次为用户明说（user_stated），把既有行升格为 user_stated——
            # "AI 先推断、用户后亲口确认" = 置信升级；绝不反向降级。
            try:
                if src == "user_stated":
                    self._conn.execute(
                        "UPDATE episodic_memory SET hits = hits + 1, last_seen = ?,"
                        " source = 'user_stated'"
                        " WHERE user_id = ? AND content_hash = ?",
                        (now, user_id, h),
                    )
                else:
                    self._conn.execute(
                        "UPDATE episodic_memory SET hits = hits + 1, last_seen = ?"
                        " WHERE user_id = ? AND content_hash = ?",
                        (now, user_id, h),
                    )
                self._conn.commit()
            except Exception as e:  # noqa: BLE001
                logger.debug("episodic recurrence bump failed: %s", e)
            return None
        except Exception as e:
            logger.debug("episodic insert failed: %s", e)
            return None

    # ── J-10 A2：例外审核队列 / 软删 / 冲突并列 ──────────────────────────────

    def _review_tags(
        self, user_id: str, content: str, *, source: str,
        confidence: Optional[float], author: str,
        review_reason: Optional[str], impact: Optional[str],
    ) -> Tuple[str, str]:
        """写入打标：显式传入优先；否则文本四类 + 同槽 stable 冲突（最高优先级）。绝不抛。"""
        try:
            from src.utils.memory_review import (
                IMPACT_HIGH, IMPACT_NORMAL, REVIEW_CONFLICT, REVIEW_REASONS, classify_fact,
            )
        except Exception:  # pragma: no cover - 防御
            return "", "normal"
        try:
            rr_auto, imp_auto = classify_fact(
                content, source=source, confidence=confidence, author=author,
                low_confidence_threshold=self._low_conf_threshold())
            if review_reason is None:
                rr = rr_auto
                # 同槽 stable 冲突＝最高优先级（数据态判定，文本四类让位）
                if self._find_stable_conflict(user_id, content) is not None:
                    rr = REVIEW_CONFLICT
            else:
                rr = str(review_reason or "").strip()
                rr = rr if rr in REVIEW_REASONS else ""
            if impact is None:
                imp = imp_auto
            else:
                imp = str(impact or "").strip()
                imp = imp if imp in (IMPACT_HIGH, IMPACT_NORMAL) else IMPACT_NORMAL
            return rr, imp
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic review tagging failed: %s", e)
            return "", "normal"

    def _low_conf_threshold(self) -> float:
        thr = getattr(self, "low_confidence_threshold", None)
        if thr is None:
            from src.utils.memory_review import DEFAULT_LOW_CONFIDENCE_THRESHOLD
            return DEFAULT_LOW_CONFIDENCE_THRESHOLD
        return float(thr)

    def _find_stable_conflict(self, user_id: str, content: str) -> Optional[int]:
        """新事实与该用户某条 **stable**（active、非 stale）事实同槽异值 → 返回那条 id。"""
        from src.utils.memory_slots import extract_slot, slots_conflict
        slot = extract_slot(content or "")
        if not slot:
            return None
        try:
            rows = self._conn.execute(
                "SELECT id, content FROM episodic_memory"
                " WHERE user_id = ? AND COALESCE(tier, 'raw') = 'stable'"
                " AND COALESCE(status, 'active') = 'active'"
                " ORDER BY created_at DESC LIMIT 300",
                (user_id,),
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic stable conflict scan failed: %s", e)
            return None
        for rid, text in rows:
            s2 = extract_slot(text or "")
            if s2 and slots_conflict(slot, s2):
                return int(rid)
        return None

    def _conflict_group_for(self, user_id: str, content: str) -> str:
        """冲突组键＝被冲突的 stable 行 id（``g<id>``）；同时把那条 stable 也挂上组键，
        两条并列可查、都不动 tier（不自动覆盖，让人选）。"""
        sid = self._find_stable_conflict(user_id, content)
        if sid is None:
            return ""
        group = f"g{sid}"
        try:
            self._conn.execute(
                "UPDATE episodic_memory SET conflict_group = ?"
                " WHERE id = ? AND COALESCE(conflict_group, '') = ''",
                (group, sid))
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic conflict_group mark failed: %s", e)
        return group

    _ROW_COLS = (
        "id, user_id, content, category, created_at,"
        " CASE WHEN embedding IS NOT NULL AND length(embedding) >= 8 THEN 1 ELSE 0 END,"
        " COALESCE(source, 'user_stated'), COALESCE(tier, 'raw'), COALESCE(hits, 1),"
        " COALESCE(source_quote, ''), COALESCE(source_ts, 0),"
        " COALESCE(status, 'active'), COALESCE(review_reason, ''), COALESCE(impact, 'normal'),"
        " COALESCE(conflict_group, ''), COALESCE(recall_count, 0), COALESCE(last_recalled_ts, 0)"
    )

    @staticmethod
    def _row_to_dict(r: Tuple[Any, ...]) -> Dict[str, Any]:
        return {
            "id": r[0],
            "memory_key": r[1],
            "content": r[2],
            "category": r[3],
            "created_at": r[4],
            "has_embedding": bool(r[5]),
            "source": r[6],
            "tier": r[7],
            "hits": int(r[8] or 1),
            # 五件套·溯源（#41）：抽取自哪句原话/何时（空=早期条目无记录）
            "source_quote": str(r[9] or ""),
            "source_ts": float(r[10] or 0),
            # J-10 A2/A3
            "status": str(r[11] or "active"),
            "review_reason": str(r[12] or ""),
            "impact": str(r[13] or "normal"),
            "conflict_group": str(r[14] or ""),
            "recall_count": int(r[15] or 0),
            "last_recalled_ts": float(r[16] or 0),
        }

    def review_queue(
        self, *, user_id: str = "", reason: str = "", limit: int = 100, offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """例外队列：``status=active`` 且 ``review_reason`` 非空的条目（其余不进队列）。

        ``conflict`` 条目附 ``conflict_with``＝同组另一条（stable）的摘要，页面并列
        「保留哪条」。``user_id`` 给定则只看该客户；``reason`` 给定则只看该类。
        按 impact（high 先）→ 新近排序。绝不抛（异常返回 []）。
        """
        lim = max(1, min(int(limit or 100), 500))
        off = max(0, min(int(offset or 0), 100000))
        where = ["COALESCE(status, 'active') = 'active'", "COALESCE(review_reason, '') != ''"]
        params: List[Any] = []
        uid = str(user_id or "").strip()
        if uid:
            where.append("user_id = ?")
            params.append(uid)
        rs = str(reason or "").strip()
        if rs:
            where.append("review_reason = ?")
            params.append(rs)
        try:
            rows = self._conn.execute(
                f"SELECT {self._ROW_COLS} FROM episodic_memory"
                f" WHERE {' AND '.join(where)}"
                " ORDER BY CASE WHEN COALESCE(impact, 'normal') = 'high' THEN 0 ELSE 1 END,"
                " created_at DESC, id DESC LIMIT ? OFFSET ?",
                params + [lim, off],
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic review_queue failed: %s", e)
            return []
        out = [self._row_to_dict(r) for r in rows]
        for item in out:
            if item["review_reason"] == "conflict" and item["conflict_group"]:
                item["conflict_with"] = self._conflict_peer(item["id"], item["conflict_group"])
        return out

    def _conflict_peer(self, row_id: int, group: str) -> Optional[Dict[str, Any]]:
        try:
            r = self._conn.execute(
                f"SELECT {self._ROW_COLS} FROM episodic_memory"
                " WHERE conflict_group = ? AND id != ? AND COALESCE(status, 'active') = 'active'"
                " ORDER BY CASE WHEN COALESCE(tier, 'raw') = 'stable' THEN 0 ELSE 1 END,"
                " created_at DESC LIMIT 1",
                (group, int(row_id)),
            ).fetchone()
        except Exception:
            return None
        return self._row_to_dict(r) if r else None

    def review_counts(self, *, user_id: str = "") -> Dict[str, Any]:
        """例外队列计数：``{"pending", "by_reason": {reason: n}, "high_impact_pending"}``。

        页面「今日需处理 M 件」/ 客户列表红点直接读。绝不抛（异常返回零）。
        """
        out: Dict[str, Any] = {
            "pending": 0,
            "by_reason": {"conflict": 0, "high_impact": 0, "low_confidence": 0,
                          "self_fact": 0, "commitment": 0},
            "high_impact_pending": 0,
        }
        where = "COALESCE(status, 'active') = 'active' AND COALESCE(review_reason, '') != ''"
        params: List[Any] = []
        uid = str(user_id or "").strip()
        if uid:
            where += " AND user_id = ?"
            params.append(uid)
        try:
            rows = self._conn.execute(
                f"SELECT review_reason, COUNT(*),"
                f" SUM(CASE WHEN COALESCE(impact, 'normal') = 'high' THEN 1 ELSE 0 END)"
                f" FROM episodic_memory WHERE {where} GROUP BY review_reason",
                params,
            ).fetchall()
            for reason, n, hi in rows:
                out["by_reason"][str(reason or "")] = int(n or 0)
                out["pending"] += int(n or 0)
                out["high_impact_pending"] += int(hi or 0)
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic review_counts failed: %s", e)
        return out

    def confirm_fact(self, row_id: int) -> Optional[str]:
        """例外确认（confirm 语义不变：转正 + 清 review_reason）——适用于任何进了队列的
        条目（含 user_stated 的 high_impact / commitment），以及旧口径的 ai_inferred。
        ``conflict`` 条目请走 :meth:`resolve_conflict`（这里也接受：视为「保留新条」）。
        返回被确认的 content；未命中返回 None。
        """
        try:
            rid = int(row_id)
        except (TypeError, ValueError):
            return None
        try:
            row = self._conn.execute(
                "SELECT content, COALESCE(review_reason, ''), COALESCE(conflict_group, '')"
                " FROM episodic_memory WHERE id = ?"
                " AND (COALESCE(source, 'user_stated') = 'ai_inferred'"
                "      OR COALESCE(review_reason, '') != '')",
                (rid,),
            ).fetchone()
            if not row:
                return None
            if row[1] == "conflict" and row[2]:
                res = self.resolve_conflict(rid)
                return str(row[0]) if res.get("kept") == rid else None
            self._conn.execute(
                "UPDATE episodic_memory"
                " SET source = 'user_stated', tier = 'stable', review_reason = '',"
                " status = 'active', last_seen = ?"
                " WHERE id = ?",
                (int(time.time()), rid),
            )
            self._conn.commit()
            return str(row[0])
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic confirm_fact failed: %s", e)
            return None

    def ignore_fact(self, row_id: int) -> Optional[str]:
        """软删「不再使用」：``status=ignored``——不召回、不进队列、不算画像；可恢复。
        返回被忽略的 content（审计）；未命中返回 None。硬删仍是 :meth:`delete_by_id`。"""
        try:
            rid = int(row_id)
        except (TypeError, ValueError):
            return None
        try:
            row = self._conn.execute(
                "SELECT content FROM episodic_memory WHERE id = ?", (rid,)).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE episodic_memory SET status = 'ignored', last_seen = ? WHERE id = ?",
                (int(time.time()), rid))
            self._conn.commit()
            return str(row[0])
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic ignore_fact failed: %s", e)
            return None

    def restore_fact(self, row_id: int) -> Optional[str]:
        """恢复软删条目（``ignored`` → ``active``）。返回 content；未命中/本就 active → None。"""
        try:
            rid = int(row_id)
        except (TypeError, ValueError):
            return None
        try:
            row = self._conn.execute(
                "SELECT content FROM episodic_memory WHERE id = ?"
                " AND COALESCE(status, 'active') = 'ignored'", (rid,)).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE episodic_memory SET status = 'active', last_seen = ? WHERE id = ?",
                (int(time.time()), rid))
            self._conn.commit()
            return str(row[0])
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic restore_fact failed: %s", e)
            return None

    def resolve_conflict(self, keep_id: int) -> Dict[str, Any]:
        """冲突组人工择一：保留 ``keep_id``（转正 stable、清 review_reason / 组键），
        同组其余条目标 ``tier='stale'``（与矛盾消解同语义：不硬删、备查）。

        返回 ``{"kept": id|None, "staled": [ids], "content": str}``；``keep_id`` 不在
        任何冲突组时 kept=None。
        """
        out: Dict[str, Any] = {"kept": None, "staled": [], "content": ""}
        try:
            kid = int(keep_id)
        except (TypeError, ValueError):
            return out
        try:
            row = self._conn.execute(
                "SELECT content, COALESCE(conflict_group, '') FROM episodic_memory"
                " WHERE id = ?", (kid,)).fetchone()
            if not row or not row[1]:
                return out
            group = str(row[1])
            others = [int(r[0]) for r in self._conn.execute(
                "SELECT id FROM episodic_memory WHERE conflict_group = ? AND id != ?",
                (group, kid)).fetchall()]
            now = int(time.time())
            self._conn.execute(
                "UPDATE episodic_memory SET tier = 'stable', review_reason = '',"
                " conflict_group = '', source = 'user_stated', status = 'active', last_seen = ?"
                " WHERE id = ?", (now, kid))
            if others:
                self._conn.executemany(
                    "UPDATE episodic_memory SET tier = 'stale', review_reason = '',"
                    " conflict_group = '' WHERE id = ?", [(i,) for i in others])
            self._conn.commit()
            out.update({"kept": kid, "staled": others, "content": str(row[0])})
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic resolve_conflict failed: %s", e)
        return out

    def list_key_stats(self) -> List[Tuple[str, int]]:
        """返回 ``[(user_id_key, fact_count), ...]``，按事实数降序。

        供跨平台记忆 key 迁移工具盘点：哪些是「裸 key」（旧产线遗留、未带 platform
        前缀），需要并入 canonical key 才能被新收件箱产线命中。
        """
        try:
            rows = self._conn.execute(
                "SELECT user_id, COUNT(*) FROM episodic_memory"
                " GROUP BY user_id ORDER BY COUNT(*) DESC"
            ).fetchall()
            return [(str(r[0]), int(r[1])) for r in rows]
        except Exception as e:
            logger.debug("list_key_stats failed: %s", e)
            return []

    def key_health(self, sample: int = 10) -> Dict[str, Any]:
        """记忆 key 健康概览：盘点「裸 key」（无 ``platform:`` 前缀）漂移。

        裸 key 是旧产线遗留或某入口漏传 platform 的产物——新收件箱引擎按 canonical
        (``platform:uid``) 读取，裸 key 下的记忆对其不可见 → 拉低命中率。一次性迁移
        （:mod:`src.utils.episodic_key_migration`）清掉存量后，本探针让**复发可观测**：
        运维/看板随时能看到 bare_keys 是否回升，而非靠低命中率事后倒查。

        返回 ``{total_keys, canonical_keys, bare_keys, bare_facts, bare_ratio,
        bare_samples:[{key,facts}...]}``；store 异常返回零值（绝不抛）。
        """
        try:
            stats = self.list_key_stats()
        except Exception:
            stats = []
        total_keys = len(stats)
        bare = [(k, n) for k, n in stats if ":" not in str(k)]
        n_sample = max(0, int(sample or 0))
        return {
            "total_keys": total_keys,
            "canonical_keys": total_keys - len(bare),
            "bare_keys": len(bare),
            "bare_facts": sum(n for _, n in bare),
            "bare_ratio": round(len(bare) / total_keys, 4) if total_keys else 0.0,
            "bare_samples": [{"key": k, "facts": n} for k, n in bare[:n_sample]],
        }

    def merge_key(self, old_key: str, new_key: str) -> int:
        """把 ``old_key`` 的事实并入 ``new_key``（幂等、按 content_hash 去重）。

        用 ``UPDATE OR IGNORE`` 迁移：与目标 key 内容重复的行迁移被忽略（保留目标既
        有），再 ``DELETE`` 掉旧 key 残留行。返回成功迁移（非重复）的行数。
        """
        old_key = str(old_key or "")
        new_key = str(new_key or "")
        if not old_key or not new_key or old_key == new_key:
            return 0
        try:
            cur = self._conn.execute(
                "UPDATE OR IGNORE episodic_memory SET user_id=? WHERE user_id=?",
                (new_key, old_key),
            )
            moved = int(cur.rowcount or 0)
            # 删除因 (user_id, content_hash) 冲突未能迁移的旧残留行
            self._conn.execute(
                "DELETE FROM episodic_memory WHERE user_id=?", (old_key,)
            )
            self._conn.commit()
            return moved
        except Exception as e:
            logger.debug("merge_key %s→%s failed: %s", old_key, new_key, e)
            try:
                self._conn.rollback()
            except Exception:
                pass
            return 0

    @staticmethod
    def _compute_salience(content: str) -> Optional[float]:
        """写入期算一次情绪显著性（0-1）；失败回 None，不阻断写入。"""
        try:
            from src.utils.memory_salience import salience_score
            return round(float(salience_score(content)), 4)
        except Exception:
            return None

    def update_embedding(
        self, row_id: int, embedding_blob: bytes, embedding_model: str = ""
    ) -> bool:
        if not embedding_blob:
            return False
        try:
            dim = len(embedding_blob) // 4  # float32 → 每维 4 字节
            cur = self._conn.execute(
                "UPDATE episodic_memory SET embedding = ?, embedding_model = ?,"
                " embedding_dim = ? WHERE id = ?",
                (embedding_blob, (embedding_model or ""), dim, int(row_id)),
            )
            self._conn.commit()
            return int(cur.rowcount or 0) > 0
        except Exception as e:
            logger.debug("episodic update_embedding failed: %s", e)
            return False

    def count(self, user_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM episodic_memory WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def prune_oldest(self, user_id: str, keep: int) -> int:
        """Keep at most `keep` rows (by recency). Returns deleted count.

        R3：``stable`` 稳定层（已巩固的人设级记忆）**永不被裁剪**——只淘汰 ``raw`` 层
        的最旧者，使长期重要记忆不会因近期琐事刷量而被挤掉。
        J-10 A2：软删（``status=ignored``）的条目最先被裁——它们本就不再使用。
        """
        n = self.count(user_id)
        if n <= keep:
            return 0
        to_drop = n - keep
        cur = self._conn.execute(
            """
            DELETE FROM episodic_memory WHERE id IN (
                SELECT id FROM episodic_memory
                WHERE user_id = ? AND COALESCE(tier, 'raw') != 'stable'
                ORDER BY CASE WHEN COALESCE(status, 'active') = 'ignored' THEN 0 ELSE 1 END,
                         created_at ASC LIMIT ?
            )
            """,
            (user_id, to_drop),
        )
        self._conn.commit()
        return int(cur.rowcount or 0)

    def resolve_contradictions(
        self,
        user_id: str,
        *,
        max_scan: int = 200,
        supersede_stable: bool = False,
        stable_min_hits: int = 2,
        source_aware: bool = False,
    ) -> Dict[str, int]:
        """R10/R11/R12 矛盾消解：同一**单值属性槽**出现冲突值时，保留最新、旧值标 ``stale``。

        例：旧"住在北京" vs 新"住在上海" → 旧条降为 ``stale``（排除出 prompt 注入，
        但保留备查/审计）。``raw`` 内消解（R10）：newest 以 ``last_seen``（回退
        ``created_at``）为准。

        R11 ``supersede_stable``：允许**新 raw 证据推翻已晋升的 ``stable`` 结论**
        （如"搬家/分手"——旧住址早已是稳定结论，新址该取代它）。但 stable 是高置信结论，
        不能被一次随口提及（"出差去上海"）冲掉，故设更高门槛：只有当 newest raw 槽值
        的**累计 hits ≥ ``stable_min_hits``**（反复提及=真的变了）时，才把同槽冲突的
        stable 条标 ``stale``。

        R12 ``source_aware``：推翻 stable 的证据**只数 ``user_stated``**（用户明说）的
        hits——AI 推断（``ai_inferred``）再多也不该推翻用户亲口确认的稳定结论。

        返回 ``{"superseded", "conflicts", "stable_superseded"}``。
        """
        from src.utils.memory_slots import extract_slot, slots_conflict

        try:
            rows = self._conn.execute(
                """
                SELECT id, content, created_at, last_seen, hits,
                       COALESCE(source, 'user_stated')
                FROM episodic_memory
                WHERE user_id = ? AND COALESCE(tier, 'raw') = 'raw'
                  AND COALESCE(status, 'active') = 'active'
                ORDER BY created_at DESC LIMIT ?
                """,
                (user_id, max(2, min(int(max_scan), 500))),
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic contradiction fetch failed: %s", e)
            return {"superseded": 0, "conflicts": 0, "stable_superseded": 0}

        # 按槽 base key 分组（pref 槽含对象，身份槽即 slot 名）
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            slot = extract_slot(r[1] or "")
            if not slot:
                continue
            ts = float(r[3] if r[3] is not None else (r[2] or 0.0))
            groups.setdefault(slot[0], []).append(
                {
                    "id": int(r[0]), "slot": slot, "ts": ts,
                    "hits": int(r[4] or 1), "source": str(r[5] or "user_stated"),
                }
            )

        # R11：可选地把 stable 层也按槽分组，供新 raw 证据推翻
        stable_groups: Dict[str, List[Dict[str, Any]]] = {}
        if supersede_stable:
            try:
                srows = self._conn.execute(
                    """
                    SELECT id, content
                    FROM episodic_memory
                    WHERE user_id = ? AND COALESCE(tier, 'raw') = 'stable'
                      AND COALESCE(status, 'active') = 'active'
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (user_id, max(2, min(int(max_scan), 500))),
                ).fetchall()
                for sr in srows:
                    sslot = extract_slot(sr[1] or "")
                    if sslot:
                        stable_groups.setdefault(sslot[0], []).append(
                            {"id": int(sr[0]), "slot": sslot}
                        )
            except Exception as e:  # noqa: BLE001
                logger.debug("episodic stable fetch failed: %s", e)

        stale_ids: List[int] = []
        stable_stale_ids: List[int] = []
        conflicts = 0
        min_hits = max(1, int(stable_min_hits))
        for base, items in groups.items():
            if len(items) < 2 and not stable_groups.get(base):
                continue
            newest = max(items, key=lambda x: x["ts"])
            group_conflicted = False
            for it in items:
                if it["id"] == newest["id"]:
                    continue
                if slots_conflict(it["slot"], newest["slot"]):
                    stale_ids.append(it["id"])
                    group_conflicted = True
            if group_conflicted:
                conflicts += 1
            # R11/R12：新 raw 证据足够强 → 推翻同槽冲突的 stable 结论；
            # source_aware 时只数 user_stated 的 hits（AI 推断不足以推翻用户明说）。
            if supersede_stable and stable_groups.get(base):
                evidence = sum(
                    it["hits"]
                    for it in items
                    if it["slot"] == newest["slot"]
                    and (not source_aware or it["source"] == "user_stated")
                )
                if evidence >= min_hits:
                    for st in stable_groups[base]:
                        if slots_conflict(st["slot"], newest["slot"]):
                            stable_stale_ids.append(st["id"])
        all_stale = stale_ids + stable_stale_ids
        if all_stale:
            try:
                self._conn.executemany(
                    "UPDATE episodic_memory SET tier = 'stale' WHERE id = ?",
                    [(i,) for i in all_stale],
                )
                # J-10 A2：R11 由系统推翻了 stable ＝ 这组冲突已被证据解决——清掉组内
                # 的 conflict 标记，否则赢的新条仍被当「待人选」挡在 prompt 外。
                if stable_stale_ids:
                    groups = [
                        str(r[0]) for r in self._conn.execute(
                            "SELECT DISTINCT COALESCE(conflict_group, '') FROM episodic_memory"
                            f" WHERE id IN ({','.join('?' * len(stable_stale_ids))})",
                            stable_stale_ids,
                        ).fetchall() if r and r[0]
                    ]
                    for g in groups:
                        self._conn.execute(
                            "UPDATE episodic_memory SET conflict_group = '',"
                            " review_reason = CASE WHEN review_reason = 'conflict'"
                            " THEN '' ELSE review_reason END"
                            " WHERE conflict_group = ?", (g,))
                self._conn.commit()
            except Exception as e:  # noqa: BLE001
                logger.debug("episodic contradiction mark failed: %s", e)
                return {"superseded": 0, "conflicts": conflicts, "stable_superseded": 0}
        return {
            "superseded": len(stale_ids),
            "conflicts": conflicts,
            "stable_superseded": len(stable_stale_ids),
        }

    def merge_near_duplicates(
        self,
        user_id: str,
        *,
        threshold: float = 0.92,
        max_scan: int = 200,
        min_raw: int = 6,
    ) -> Dict[str, int]:
        """R5 近似去重巩固：把 ``raw`` 层里**语义近似**的事实归并为一条。

        承接 R3——R3 只折叠**完全相同**（hash 相等）的事实，但"喜欢猫"/"我养了只猫"/
        "家里有猫"是同一件事的不同说法，会各占一行、稀释复发信号、挤占 prune 名额。
        本方法用 embedding 余弦相似度做**贪心聚类**（阈值默认 0.92，高=只并近义），
        每簇择优留一条（salience→hits→新近→更长者胜），并把其余条的 ``hits`` 累加到
        survivor（让"换着说法反复提"也累积成复发证据，反哺 ``consolidate`` 晋升）。

        仅作用于 ``raw``（``stable`` 是已巩固结论，不动）；O(n²) 但 n≤max_scan 且向量
        预归一化为点积。

        **观测与合并门槛解耦**（P7，2026-07-27）：raw ≥2 即扫描计灰区；
        raw < ``min_raw`` 时进入 ``observe_only``——只计灰区/应并对、绝不 DELETE。
        原「raw < min_raw 整段跳过」会让生产早期用户（常见 2–5 条）两周观察期
        灰区读数恒 0，误判成「源头已干净」。合并仍守 ``min_raw``（默认 6）。

        另计**灰区对**（``thr-0.17 ≤ cos < thr``，宽度取自 2026-07-26 bge-m3 生产校准：
        应并组下探 0.74 而禁并组上顶 0.876，两分布重叠 → 阈值不能降，灰区只能观测）：
        「差一点就并」的近义对数量是决策「要不要上 LLM 仲裁合并」的直接读数，
        随返回值/巩固日志可见，不影响任何合并行为。

        返回 ``{"merged", "clusters", "gray_pairs", "observe_only", "held_merges"}``
        （``held_merges``＝observe_only 下本应合并的条数，便于看「门槛挡住了多少」）。
        """
        from src.utils.episodic_vector import blob_to_vec

        raw_n = self._count_tier(user_id, "raw")
        if raw_n < 2:
            return {"merged": 0, "clusters": 0, "gray_pairs": 0,
                    "observe_only": False, "held_merges": 0}
        observe_only = raw_n < max(2, int(min_raw))
        thr = max(0.5, min(float(threshold or 0.92), 0.999))
        try:
            rows = self._conn.execute(
                """
                SELECT id, content, embedding, created_at, hits, salience, last_seen
                FROM episodic_memory
                WHERE user_id = ? AND COALESCE(tier, 'raw') = 'raw'
                  AND COALESCE(status, 'active') = 'active'
                  AND embedding IS NOT NULL
                ORDER BY created_at DESC LIMIT ?
                """,
                (user_id, max(2, min(int(max_scan), 500))),
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic dedupe fetch failed: %s", e)
            return {"merged": 0, "clusters": 0, "gray_pairs": 0,
                    "observe_only": observe_only, "held_merges": 0}

        # 预归一化向量 → 余弦退化为点积，省去 n² 次开方
        items: List[Dict[str, Any]] = []
        for r in rows:
            vec = blob_to_vec(r[2])
            if not vec:
                continue
            norm = sum(x * x for x in vec) ** 0.5
            if norm < 1e-9:
                continue
            inv = 1.0 / norm
            items.append({
                "id": int(r[0]),
                "content": (r[1] or "").strip(),
                "nvec": [x * inv for x in vec],
                "created_at": float(r[3] or 0.0),
                "hits": int(r[4] or 1),
                "salience": (float(r[5]) if r[5] is not None else 0.0),
                "last_seen": float(r[6] or r[3] or 0.0),
            })
        if len(items) < 2:
            return {"merged": 0, "clusters": 0, "gray_pairs": 0,
                    "observe_only": observe_only, "held_merges": 0}

        gray_low = max(0.5, thr - _DEDUP_GRAY_BAND)
        gray_pairs = 0
        gray_best: Optional[Tuple[float, str, str]] = None
        used: set = set()
        merged_total = 0
        held_merges = 0
        clusters = 0
        for i in range(len(items)):
            if items[i]["id"] in used:
                continue
            a = items[i]["nvec"]
            cluster = [items[i]]
            for j in range(i + 1, len(items)):
                if items[j]["id"] in used:
                    continue
                b = items[j]["nvec"]
                if len(a) != len(b):
                    continue  # 混维向量（换模型遗留）不可比，跳过避免假聚类误并
                dot = 0.0
                for x, y in zip(a, b):
                    dot += x * y
                if dot >= thr:
                    cluster.append(items[j])
                    used.add(items[j]["id"])
                elif dot >= gray_low:
                    gray_pairs += 1
                    if gray_best is None or dot > gray_best[0]:
                        gray_best = (
                            dot, items[i]["content"][:40], items[j]["content"][:40])
            if len(cluster) < 2:
                continue
            if observe_only:
                # 只观测：计「本应合并」条数，不写库
                held_merges += len(cluster) - 1
                clusters += 1
                continue
            survivor = max(
                cluster,
                key=lambda c: (c["salience"], c["hits"], c["created_at"], len(c["content"])),
            )
            others = [c for c in cluster if c["id"] != survivor["id"]]
            total_hits = survivor["hits"] + sum(o["hits"] for o in others)
            max_sal = max(c["salience"] for c in cluster)
            max_last = max(c["last_seen"] for c in cluster)
            try:
                self._conn.execute(
                    "UPDATE episodic_memory SET hits = ?, salience = ?, last_seen = ?"
                    " WHERE id = ?",
                    (total_hits, max_sal, max_last, survivor["id"]),
                )
                self._conn.executemany(
                    "DELETE FROM episodic_memory WHERE id = ?",
                    [(o["id"],) for o in others],
                )
                merged_total += len(others)
                clusters += 1
            except Exception as e:  # noqa: BLE001
                logger.debug("episodic dedupe merge failed: %s", e)
        if merged_total:
            self._conn.commit()
        if gray_pairs and gray_best is not None:
            logger.info(
                "episodic 去重灰区 %d 对 (%.2f≤cos<%.2f) user=%s observe_only=%s "
                "最近对: %.3f 「%s」×「%s」",
                gray_pairs, gray_low, thr, user_id, observe_only, *gray_best,
            )
        try:
            self._dedup_stats["scans"] += 1
            self._dedup_stats["merged"] += merged_total
            self._dedup_stats["gray_pairs"] += gray_pairs
            self._dedup_stats["held_merges"] = (
                int(self._dedup_stats.get("held_merges") or 0) + held_merges)
            self._dedup_stats["observe_only_scans"] = (
                int(self._dedup_stats.get("observe_only_scans") or 0)
                + (1 if observe_only else 0))
            if gray_best is not None:
                self._dedup_stats["last_gray_at"] = time.time()
                self._dedup_stats["last_gray_example"] = (
                    f"{gray_best[0]:.3f} 「{gray_best[1]}」×「{gray_best[2]}」")
        except Exception:
            pass
        return {
            "merged": merged_total,
            "clusters": clusters,
            "gray_pairs": gray_pairs,
            "observe_only": observe_only,
            "held_merges": held_merges,
        }

    def consolidate(
        self,
        user_id: str,
        *,
        min_hits: int = 2,
        min_salience: Optional[float] = None,
        dedup_threshold: Optional[float] = None,
        resolve_contradictions: bool = False,
        supersede_stable: bool = False,
        stable_min_hits: int = 2,
        source_aware: bool = False,
        inferred_min_hits: Optional[int] = None,
        auto_promote_days: Optional[float] = 7.0,
        auto_promote_min_recalls: int = 1,
    ) -> Dict[str, int]:
        """离线巩固：把 ``raw`` 层里**复发**（hits≥min_hits）或**情绪浓**
        （salience≥min_salience，若给）的事实晋升为 ``stable`` 稳定层。

        稳定层享受检索加权（见 ``get_bullets_for_prompt``）且永不被 prune 裁剪——
        即 PersonaTree/REMT 的"复发证据 → 稳定结论"思想的轻量落地。

        顺序：R10/R11 矛盾消解（旧冲突值标 stale，含新证据推翻 stable）→ R5 近义去重
        （合并近义、累加 hits）→ 晋升。先消矛盾再去重，避免把"住北京/住上海"误并。

        ``supersede_stable``（R11）：开后新 raw 证据（累计 hits≥``stable_min_hits``）
        可推翻同槽的旧 stable 结论，承接"搬家/分手"这类真实变更。

        ``source_aware``（R12）：开后按来源分级置信——``ai_inferred``（LLM 推断）晋升
        stable 需更高复发门槛（``inferred_min_hits``，默认 ``min_hits+1``），且推翻 stable
        的证据只数 ``user_stated``。``user_stated`` 走原门槛，行为不变。

        **J-10 A2（D8）第二条晋升路径**：``age ≥ auto_promote_days``（默认 7 天，None 关）
        且 ``recall_count ≥ auto_promote_min_recalls``（默认 1；A3 召回记账写入）且
        **不在冲突中**（``review_reason != 'conflict'``）→ stable。「用过、没被推翻」本身
        就是印证——不再只认「重复 ≥2 次」。两条路径都不碰 ``conflict`` 条目（两条并列等人选，
        绝不让冲突双方都成 stable）；软删（``status=ignored``）不参与。
        配置 ``memory.consolidation.auto_promote.{days, min_recalls}``。

        返回 ``{"promoted", "auto_promoted", "stable_total", "raw_total", "merged",
        "gray_pairs", "superseded", "stable_superseded"}``（``promoted`` 含 auto）。
        """
        superseded = 0
        stable_superseded = 0
        if resolve_contradictions:
            _rc = self.resolve_contradictions(
                user_id,
                supersede_stable=supersede_stable,
                stable_min_hits=stable_min_hits,
                source_aware=source_aware,
            )
            superseded = _rc.get("superseded", 0)
            stable_superseded = _rc.get("stable_superseded", 0)
        merged = 0
        gray_pairs = 0
        held_merges = 0
        observe_only = False
        if dedup_threshold is not None:
            _dd = self.merge_near_duplicates(
                user_id, threshold=float(dedup_threshold)
            )
            merged = _dd.get("merged", 0)
            gray_pairs = _dd.get("gray_pairs", 0)
            held_merges = int(_dd.get("held_merges") or 0)
            observe_only = bool(_dd.get("observe_only"))
        mh = max(2, int(min_hits or 2))
        # 既有"明说"晋升条件：复发 hits 达标，或（若给）情绪显著性达标
        stated_cond = "hits >= ?"
        stated_params: List[Any] = [mh]
        if min_salience is not None:
            try:
                ms = float(min_salience)
                stated_cond = "(hits >= ? OR COALESCE(salience, 0) >= ?)"
                stated_params = [mh, ms]
            except (TypeError, ValueError):
                pass
        if source_aware:
            # R12：ai_inferred 需更高复发门槛（默认 min_hits+1），且不走情绪捷径——
            # "AI 推断 + 情绪浓"仍只是猜测，不该轻易固化为稳定人设。
            imh = max(mh, int(inferred_min_hits)) if inferred_min_hits is not None else mh + 1
            cond = (
                "((COALESCE(source, 'user_stated') = 'user_stated' AND " + stated_cond + ")"
                " OR (COALESCE(source, 'user_stated') = 'ai_inferred' AND hits >= ?))"
            )
            params: List[Any] = [user_id] + stated_params + [imh]
        else:
            cond = stated_cond
            params = [user_id] + stated_params
        # J-10 A2：两条路径都不碰冲突中 / 软删的条目
        guard = (" AND COALESCE(review_reason, '') != 'conflict'"
                 " AND COALESCE(status, 'active') = 'active'")
        try:
            cur = self._conn.execute(
                f"""
                UPDATE episodic_memory SET tier = 'stable'
                WHERE user_id = ? AND COALESCE(tier, 'raw') = 'raw' AND {cond}{guard}
                """,
                params,
            )
            self._conn.commit()
            promoted = int(cur.rowcount or 0)
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic consolidate failed: %s", e)
            promoted = 0
        auto_promoted = 0
        if auto_promote_days is not None:
            try:
                days = float(auto_promote_days)
                min_rc = max(1, int(auto_promote_min_recalls or 1))
                if days >= 0:
                    cur2 = self._conn.execute(
                        f"""
                        UPDATE episodic_memory SET tier = 'stable'
                        WHERE user_id = ? AND COALESCE(tier, 'raw') = 'raw'
                          AND created_at <= ? AND COALESCE(recall_count, 0) >= ?{guard}
                        """,
                        (user_id, time.time() - days * 86400, min_rc),
                    )
                    self._conn.commit()
                    auto_promoted = int(cur2.rowcount or 0)
                    promoted += auto_promoted
            except Exception as e:  # noqa: BLE001
                logger.debug("episodic auto-promote failed: %s", e)
        return {
            "promoted": promoted,
            "auto_promoted": auto_promoted,
            "stable_total": self._count_tier(user_id, "stable"),
            "raw_total": self._count_tier(user_id, "raw"),
            "merged": merged,
            "gray_pairs": gray_pairs,
            "held_merges": held_merges,
            "observe_only": observe_only,
            "superseded": superseded,
            "stable_superseded": stable_superseded,
        }

    def dedup_stats_snapshot(self) -> Dict[str, Any]:
        """进程级去重观测快照（自实例创建起累计；无 PII——示例对截断 40 字）。

        ``gray_pairs`` 是「差一点就并」的近义对累计数：持续为 0 → 抽取源头已干净，
        LLM 仲裁合并不必建；持续增长 → 值得上仲裁。供 workspace metrics 消费。
        """
        return dict(self._dedup_stats)

    def observe_dedup_all_users(
        self,
        *,
        threshold: float = 0.92,
        min_raw: int = 6,
    ) -> Dict[str, Any]:
        """启动/运维：对所有 raw≥2 的用户跑一轮去重扫描（未满 min_raw 只观察）。

        填满进程级 ``dedup_stats``，让 ops 卡重启后立刻有读数，不必等下一轮聊天。
        """
        try:
            rows = self._conn.execute(
                "SELECT user_id FROM episodic_memory "
                "WHERE COALESCE(tier, 'raw') = 'raw' "
                "GROUP BY user_id HAVING COUNT(*) >= 2"
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("observe_dedup_all list failed: %s", e)
            return {"users": 0, "gray_pairs": 0, "held_merges": 0}
        gray = held = 0
        for (uid,) in rows:
            r = self.merge_near_duplicates(
                str(uid), threshold=float(threshold), min_raw=int(min_raw))
            gray += int(r.get("gray_pairs") or 0)
            held += int(r.get("held_merges") or 0)
        return {"users": len(rows), "gray_pairs": gray, "held_merges": held}

    def profile_summary(self, user_id: str, *, top_stable: int = 3) -> Dict[str, Any]:
        """R14：记忆画像聚合——按 tier/source 计数 + 取若干稳定事实摘要。

        供坐席侧栏一眼掌握"对这个用户我们确切知道什么（stable/user_stated）、哪些还只是
        AI 猜的（ai_inferred）"。排除 ``stale``（已弃）；store 异常返回空概览（绝不抛）。
        返回 ``{total, stable, raw, user_stated, ai_inferred, top_stable: [content...]}``。
        """
        empty = {
            "total": 0, "stable": 0, "raw": 0,
            "user_stated": 0, "ai_inferred": 0, "top_stable": [],
            "pending_inferred": [],
        }
        uid = str(user_id or "").strip()
        if not uid:
            return empty
        try:
            rows = self._conn.execute(
                "SELECT COALESCE(tier, 'raw'), COALESCE(source, 'user_stated'), COUNT(*)"
                " FROM episodic_memory"
                " WHERE user_id = ? AND COALESCE(tier, 'raw') != 'stale'"
                " AND COALESCE(status, 'active') = 'active'"
                " GROUP BY 1, 2",
                (uid,),
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic profile_summary failed: %s", e)
            return empty
        out = dict(empty)
        out["top_stable"] = []
        for tier, source, cnt in rows:
            c = int(cnt or 0)
            out["total"] += c
            if tier == "stable":
                out["stable"] += c
            else:
                out["raw"] += c
            if source == "ai_inferred":
                out["ai_inferred"] += c
            else:
                out["user_stated"] += c
        if out["total"] == 0:
            return empty
        try:
            n = max(0, min(int(top_stable), 10))
            if n:
                tops = self._conn.execute(
                    "SELECT content FROM episodic_memory"
                    " WHERE user_id = ? AND COALESCE(tier, 'raw') = 'stable'"
                    " AND COALESCE(status, 'active') = 'active'"
                    " ORDER BY COALESCE(salience, 0) DESC, COALESCE(hits, 1) DESC,"
                    " created_at DESC LIMIT ?",
                    (uid, n),
                ).fetchall()
                out["top_stable"] = [str(r[0]) for r in tops if r and r[0]]
        except Exception:  # noqa: BLE001
            pass
        # R15：待确认的 AI 推断（raw + ai_inferred），供坐席一键转明说
        if out["ai_inferred"]:
            try:
                pend = self._conn.execute(
                    "SELECT id, content FROM episodic_memory"
                    " WHERE user_id = ? AND COALESCE(tier, 'raw') = 'raw'"
                    " AND COALESCE(source, 'user_stated') = 'ai_inferred'"
                    " AND COALESCE(status, 'active') = 'active'"
                    " ORDER BY COALESCE(salience, 0) DESC, COALESCE(hits, 1) DESC,"
                    " created_at DESC LIMIT 6",
                    (uid,),
                ).fetchall()
                out["pending_inferred"] = [
                    {"id": int(r[0]), "content": str(r[1])}
                    for r in pend if r and r[1]
                ]
            except Exception:  # noqa: BLE001
                pass
        return out

    def confirm_inferred_fact(self, row_id: int) -> Optional[str]:
        """R15：坐席确认一条 AI 推断为属实——升格为 user_stated 且直接置 stable。

        人工背书是比"复发"更强的置信信号，故直接转稳定（而非等 consolidate）。
        J-10 A2 起委托 :meth:`confirm_fact`：除 ``ai_inferred`` 外，也接受进了例外
        队列（``review_reason`` 非空）的 user_stated 条目；确认同时清 review_reason。
        不在队列的 user_stated 行仍不命中（避免误改用户明说事实的 tier）。
        R16：返回被确认的 ``content``（供调用方写审计留痕），未命中返回 ``None``。
        """
        return self.confirm_fact(row_id)

    def update_fact_content(self, row_id: int, content: str) -> bool:
        """五件套·可编辑（#41 0830 定稿问题③）：人工改写记忆条文。

        语义（与「确认属实」同族）：
        - 内容/哈希/显著性按新文重算；
        - **向量清空**——旧向量对应旧文本，留着会按旧语义被召回（语义检索
          宁缺勿错；关键词路径立即生效，向量待下次 embed 批回填）；
        - 编辑视为人工核准 → ``source`` 升 ``user_stated``（人改过的不再是
          AI 推断），tier 不动；
        - 改成与既有条目同文（撞唯一索引）→ False 不动原行（请直接删除本条）。
        """
        c = str(content or "").strip()
        if len(c) < 2 or len(c) > 500:
            return False
        try:
            rid = int(row_id)
        except (TypeError, ValueError):
            return False
        h = hashlib.sha256(_norm_for_hash(c).encode("utf-8")).hexdigest()
        try:
            cur = self._conn.execute(
                "UPDATE episodic_memory SET content = ?, content_hash = ?,"
                " salience = ?, embedding = NULL, embedding_model = '',"
                " embedding_dim = 0, source = 'user_stated', last_seen = ?"
                " WHERE id = ?",
                (c, h, self._compute_salience(c), time.time(), rid),
            )
            self._conn.commit()
            return int(cur.rowcount or 0) > 0
        except sqlite3.IntegrityError:
            return False
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic update_fact_content failed: %s", e)
            return False

    def inferred_counts(self) -> Dict[str, int]:
        """R17：全库 AI 推断计数——``pending``（raw 待确认）与 ``total``（任意 tier）。

        确认后事实会翻成 ``user_stated``，故已从 ai_inferred 集合移出；``pending`` 是
        当前仍待坐席核实的 raw 推断（不含 stale）。store 异常返回零。
        """
        out = {"pending": 0, "total": 0}
        try:
            row = self._conn.execute(
                "SELECT"
                " SUM(CASE WHEN COALESCE(tier, 'raw') = 'raw' THEN 1 ELSE 0 END),"
                " COUNT(*)"
                " FROM episodic_memory"
                " WHERE COALESCE(source, 'user_stated') = 'ai_inferred'"
                " AND COALESCE(status, 'active') = 'active'",
            ).fetchone()
            if row:
                out["pending"] = int(row[0] or 0)
                out["total"] = int(row[1] or 0)
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic inferred_counts failed: %s", e)
        return out

    def _count_tier(self, user_id: str, tier: str) -> int:
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM episodic_memory"
                " WHERE user_id = ? AND COALESCE(tier, 'raw') = ?",
                (user_id, tier),
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    @staticmethod
    def _keyword_overlap_score(query: str, content: str) -> float:
        """Lightweight overlap: 2–4 char substrings from query (no extra deps)."""
        q = (query or "").strip()
        c = (content or "").strip()
        if len(q) < 2 or not c:
            return 0.0
        q = q[:200]
        score = 0.0
        step = 1 if len(q) < 24 else 2
        for L in (4, 3, 2):
            for i in range(0, max(1, len(q) - L + 1), step):
                frag = q[i : i + L]
                if len(frag) < L:
                    break
                if frag in c:
                    score += float(L)
        return score

    def get_bullets_for_prompt(
        self,
        user_id: str,
        max_items: int = 8,
        max_chars: int = 1200,
        query_text: Optional[str] = None,
        rerank_keywords: bool = False,
        query_embedding: Optional[List[float]] = None,
        use_vector_fusion: bool = False,
        vector_weight: float = 0.5,
        keyword_weight: float = 0.5,
        use_salience_rerank: bool = False,
        salience_weight: float = 0.15,
        recency_weight: float = 0.10,
        recency_half_life_days: float = 30.0,
        age_hints: bool = True,
    ) -> str:
        """Newline bullets; optional vector+keyword fusion when query_embedding set.

        R2（REMT-lite）：``use_salience_rerank`` 开启后，在既有相关度之上叠加
        情绪显著性 + 时间衰减重排（默认关 → 行为与旧版完全一致）。

        ``age_hints``（2026-07-26）：raw 层事实距今 ≥48h 时缀「（X天前提到）」——
        没有年龄的 bullet 会被 LLM 当成「现在时」（「那里正在下大雨」三周后仍被当
        实时状态复述），标注后陈旧事实反而成为自然回访素材（「上次你说下雨…」）。
        stable 层是巩固过的无时效结论（爱好/身份），不标注；48h 内新鲜事实不标注。
        """
        return self.get_bullets_with_ids(
            user_id, max_items, max_chars, query_text=query_text,
            rerank_keywords=rerank_keywords, query_embedding=query_embedding,
            use_vector_fusion=use_vector_fusion, vector_weight=vector_weight,
            keyword_weight=keyword_weight, use_salience_rerank=use_salience_rerank,
            salience_weight=salience_weight, recency_weight=recency_weight,
            recency_half_life_days=recency_half_life_days, age_hints=age_hints,
        )[0]

    def get_bullets_with_ids(
        self,
        user_id: str,
        max_items: int = 8,
        max_chars: int = 1200,
        query_text: Optional[str] = None,
        rerank_keywords: bool = False,
        query_embedding: Optional[List[float]] = None,
        use_vector_fusion: bool = False,
        vector_weight: float = 0.5,
        keyword_weight: float = 0.5,
        use_salience_rerank: bool = False,
        salience_weight: float = 0.15,
        recency_weight: float = 0.10,
        recency_half_life_days: float = 30.0,
        age_hints: bool = True,
    ) -> Tuple[str, List[int]]:
        """同 :meth:`get_bullets_for_prompt`，另回**实际注入的行 id 列表**（顺序＝bullet 顺序）。

        J-10 A3：注入点拿 id 去 :meth:`record_recall` 记账；不记账的调用方继续用旧方法。
        """
        from src.utils.episodic_vector import blob_to_vec, cosine_similarity

        max_items = max(1, min(int(max_items or 8), 40))
        max_chars = max(100, min(int(max_chars or 1200), 8000))
        qt = (query_text or "").strip()
        want_kw = rerank_keywords and len(qt) >= 2
        want_vec = bool(
            use_vector_fusion and query_embedding and len(query_embedding) >= 8
        )
        fetch_n = max_items * 6 if (want_kw or want_vec) else max_items * 2
        fetch_n = min(fetch_n, 120)

        # J-10 A2：软删（ignored）不召回；与 stable 冲突待人选的新条不召回
        # （stable 那条仍在——不自动覆盖、也不让 prompt 里出现两个互相矛盾的事实）
        rows = self._conn.execute(
            """
            SELECT content, embedding, created_at, salience, tier,
              COALESCE(source, 'user_stated'), COALESCE(source_quote, ''), id
            FROM episodic_memory WHERE user_id = ?
              AND COALESCE(tier, 'raw') != 'stale'
              AND COALESCE(status, 'active') = 'active'
              AND COALESCE(review_reason, '') != 'conflict'
            ORDER BY created_at DESC LIMIT ?
            """,
            (user_id, fetch_n),
        ).fetchall()
        if not rows:
            return "", []

        # (text, embedding, created_at, salience, tier, source, source_quote, row_id)
        pairs: List[Tuple[str, Optional[bytes], float, Optional[float], str, str,
                          str, int]] = [
            (
                r[0].strip(),
                r[1],
                float(r[2] or 0.0),
                (float(r[3]) if r[3] is not None else None),
                (str(r[4]) if r[4] else "raw"),
                (str(r[5]) if r[5] else "user_stated"),
                (str(r[6]) if r[6] else ""),
                int(r[7] or 0),
            )
            for r in rows if r and r[0]
        ]
        if not pairs:
            return "", []

        # R2（REMT-lite）+ R3（分层）：可选"显著性×时间衰减 + 稳定层加权"重排
        # （默认关，零行为变化）。R3：优先用写入期落库的 salience，省去每次重算。
        _rerank = None
        if use_salience_rerank:
            try:
                from src.utils.memory_salience import (
                    blend_rank,
                    recency_factor,
                    salience_score,
                )
                _now = time.time()

                def _rerank(  # noqa: E306
                    base: float,
                    text: str,
                    ts: float,
                    stored_sal: Optional[float] = None,
                    tier: str = "raw",
                ) -> float:
                    sal = stored_sal if stored_sal is not None else salience_score(text)
                    score = blend_rank(
                        base,
                        sal,
                        recency_factor(ts, _now, recency_half_life_days),
                        salience_weight=salience_weight,
                        recency_weight=recency_weight,
                    )
                    if tier == "stable":
                        score += _STABLE_TIER_BOOST
                    return score
            except Exception:
                _rerank = None

        # (text, created_at, tier, source, source_quote, row_id)——一路带到渲染层
        contents: List[Tuple[str, float, str, str, str, int]]
        if want_vec:
            vw = max(0.0, min(1.0, float(vector_weight)))
            kw_w = max(0.0, min(1.0, float(keyword_weight)))
            s = vw + kw_w
            if s > 1e-9:
                vw, kw_w = vw / s, kw_w / s
            kws = [
                self._keyword_overlap_score(qt, t) if want_kw else 0.0
                for t, *_ in pairs
            ]
            max_kw = max(kws) if kws else 0.0
            scored_rows: List[Tuple[float, str, float, str, str, str, int]] = []
            for (t, emb_blob, ts, sal, tier, src, quote, rid), kw in zip(pairs, kws):
                kw_n = (kw / max_kw) if max_kw > 1e-9 else 0.0
                ev = blob_to_vec(emb_blob)
                vs = cosine_similarity(query_embedding, ev) if ev else 0.0
                vs = max(0.0, min(1.0, (vs + 1.0) / 2.0))
                fusion = vw * vs + kw_w * kw_n
                final = _rerank(fusion, t, ts, sal, tier) if _rerank else fusion
                scored_rows.append((final, t, ts, tier, src, quote, rid))
            scored_rows.sort(key=lambda x: (-x[0], -len(x[1])))
            contents = [(x[1], x[2], x[3], x[4], x[5], x[6]) for x in scored_rows]
        elif want_kw:
            scored: List[Tuple[float, str, float, str, str, str, int]] = []
            kws2 = [self._keyword_overlap_score(qt, t) for t, *_ in pairs]
            max_kw2 = max(kws2) if kws2 else 0.0
            for (t, _, ts, sal, tier, src, quote, rid), sc in zip(pairs, kws2):
                if _rerank:
                    base = (sc / max_kw2) if max_kw2 > 1e-9 else 0.0
                    final = _rerank(base, t, ts, sal, tier)
                else:
                    final = sc
                scored.append((final, t, ts, tier, src, quote, rid))
            scored.sort(key=lambda x: (-x[0], -len(x[1])))
            contents = [(x[1], x[2], x[3], x[4], x[5], x[6]) for x in scored]
        elif _rerank:
            # 无 query（纯近期）但开了重排：以新鲜度为 base 叠加显著性 + 稳定层加权
            from src.utils.memory_salience import recency_factor as _rf
            scored3: List[Tuple[float, str, float, str, str, str, int]] = []
            for t, _, ts, sal, tier, src, quote, rid in pairs:
                base = _rf(ts, None, recency_half_life_days)
                scored3.append(
                    (_rerank(base, t, ts, sal, tier), t, ts, tier, src, quote, rid))
            scored3.sort(key=lambda x: (-x[0], -len(x[1])))
            contents = [(x[1], x[2], x[3], x[4], x[5], x[6]) for x in scored3]
        else:
            contents = [(p[0], p[2], p[4], p[5], p[6], p[7]) for p in pairs]

        now_ts = time.time()
        lines: List[str] = []
        used_ids: List[int] = []
        total = 0
        for content, ts, tier, src, quote, rid in contents:
            # 五件套·档案>推断（#41/#24 0830）：AI 推断条目在 prompt 里显式
            # 标注——LLM 才知道这条不如档案/用户原话可信，冲突时让位（实锤：
            # 「用户不喜欢被叫 babe」推断压过档案称呼字段在反向教唆）。
            marks: List[str] = []
            if src == "ai_inferred":
                marks.append("AI推断，可信度低于档案")
            # #96（0830 Steven 实锤）：无溯源的**称呼/名字类**旧条目降权——
            # 1.0.63 前存量抽取方向可疑（「用户称呼自己为steven」实为客户在
            # 称呼助手），又没有原话可对质 → 显式告知 LLM 勿据此称呼对方。
            # 只标称呼类（其余旧条目风险面不同，全标＝噪声稀释重点）。
            elif not quote and _looks_name_class_fact(content):
                marks.append("早期条目无原话溯源，方向存疑，勿据此称呼对方")
            if age_hints and tier != "stable" and ts > 0:
                age = now_ts - ts
                need = (_AGE_HINT_TRANSIENT_SEC if _looks_transient_fact(content)
                        else _AGE_HINT_MIN_SEC)
                if age >= need:
                    marks.append(f"{_age_hint_label(age)}前提到")
            line = (f"- {content}（{'；'.join(marks)}）" if marks
                    else f"- {content}")
            if total + len(line) + 1 > max_chars:
                break
            lines.append(line)
            if rid:
                used_ids.append(int(rid))
            total += len(line) + 1
            if len(lines) >= max_items:
                break
        return "\n".join(lines), used_ids

    # ── J-10 A3：召回记账 ───────────────────────────────────────────────────────

    def record_recall(
        self, row_ids: List[int], *, memory_key: str = "", conversation_id: str = "",
        chain: str = "", draft_or_reply_id: str = "", inbound_msg_id: str = "",
        now: Optional[float] = None,
    ) -> int:
        """一次注入 → ``episodic_recall_log`` 每条记忆一行（同 ``batch_id``）+ 行上
        ``recall_count += 1`` / ``last_recalled_ts``。返回写入行数；绝不抛、零阻断。

        ``chain``：``inbox``（B 线拟稿）/ ``direct``（A 线直回）等；``draft_or_reply_id``
        注入时通常还不存在，留空——J-4 草稿旁按 ``conversation_id`` 取最近一批即可。
        顺手清 90 天前的日志。
        """
        ids = []
        for x in row_ids or []:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if i > 0 and i not in ids:
                ids.append(i)
        if not ids:
            return 0
        ts = float(now if now is not None else time.time())
        batch = f"{int(ts * 1000):x}-{ids[0]}"
        mk = str(memory_key or "")[:200]
        conv = str(conversation_id or "")[:200]
        ch = str(chain or "")[:32]
        dr = str(draft_or_reply_id or "")[:64]
        im = str(inbound_msg_id or "")[:64]
        try:
            self._conn.executemany(
                "INSERT INTO episodic_recall_log (ts, batch_id, memory_key, row_id,"
                " conversation_id, chain, draft_or_reply_id, inbound_msg_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(ts, batch, mk, i, conv, ch, dr, im) for i in ids])
            self._conn.executemany(
                "UPDATE episodic_memory SET recall_count = COALESCE(recall_count, 0) + 1,"
                " last_recalled_ts = ? WHERE id = ?",
                [(ts, i) for i in ids])
            self._conn.execute(
                "DELETE FROM episodic_recall_log WHERE ts < ?",
                (ts - self._RECALL_LOG_RETAIN_SEC,))
            self._conn.commit()
            return len(ids)
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic record_recall failed: %s", e)
            return 0

    def recent_recalls(
        self, *, conversation_id: str = "", memory_key: str = "",
        batches: int = 1, limit: int = 60,
    ) -> List[Dict[str, Any]]:
        """按会话（或记忆键）回读最近 N 批注入：``[{batch_id, ts, chain, inbound_msg_id,
        conversation_id, items: [row…]}]``，最新批在前；每行带当前 content / source / tier /
        review_reason / impact / source_quote / status / recall_count（已硬删的行只剩 id）。

        供 J-4 草稿旁「本轮用了这几条记忆」与页面会话抽屉「最近被用」。绝不抛。
        """
        conv = str(conversation_id or "").strip()
        mk = str(memory_key or "").strip()
        if not conv and not mk:
            return []
        nb = max(1, min(int(batches or 1), 20))
        lim = max(1, min(int(limit or 60), 500))
        where, params = [], []  # type: List[str], List[Any]
        if conv:
            where.append("conversation_id = ?")
            params.append(conv)
        if mk:
            where.append("memory_key = ?")
            params.append(mk)
        try:
            brows = self._conn.execute(
                f"SELECT batch_id, MAX(ts), MAX(chain), MAX(inbound_msg_id), MAX(conversation_id)"
                f" FROM episodic_recall_log WHERE {' AND '.join(where)}"
                " GROUP BY batch_id ORDER BY MAX(ts) DESC LIMIT ?",
                params + [nb],
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for bid, ts, chain, imid, cid in brows:
                rids = [int(r[0]) for r in self._conn.execute(
                    "SELECT row_id FROM episodic_recall_log WHERE batch_id = ?"
                    " ORDER BY id ASC LIMIT ?", (bid, lim)).fetchall()]
                items: List[Dict[str, Any]] = []
                if rids:
                    q = ",".join("?" * len(rids))
                    found = {
                        int(r[0]): self._row_to_dict(r) for r in self._conn.execute(
                            f"SELECT {self._ROW_COLS} FROM episodic_memory WHERE id IN ({q})",
                            rids).fetchall()
                    }
                    for i in rids:
                        items.append(found.get(i) or {"id": i, "deleted": True})
                out.append({
                    "batch_id": str(bid or ""), "ts": float(ts or 0),
                    "chain": str(chain or ""), "inbound_msg_id": str(imid or ""),
                    "conversation_id": str(cid or ""), "items": items,
                })
            return out
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic recent_recalls failed: %s", e)
            return []

    def recall_stats(self, days: int = 7) -> Dict[str, Any]:
        """近 N 天召回读数：``{injections（批次数）, rows（条·次）, distinct_rows, conversations}``。"""
        d = max(1, min(int(days or 7), 90))
        since = time.time() - d * 86400
        out = {"window_days": d, "injections": 0, "rows": 0, "distinct_rows": 0,
               "conversations": 0}
        try:
            r = self._conn.execute(
                "SELECT COUNT(DISTINCT batch_id), COUNT(*), COUNT(DISTINCT row_id),"
                " COUNT(DISTINCT conversation_id) FROM episodic_recall_log WHERE ts >= ?",
                (since,)).fetchone()
            if r:
                out.update({"injections": int(r[0] or 0), "rows": int(r[1] or 0),
                            "distinct_rows": int(r[2] or 0), "conversations": int(r[3] or 0)})
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic recall_stats failed: %s", e)
        return out

    def list_rows(
        self,
        prefix: str = "",
        limit: int = 100,
        source: str = "",
        q: str = "",
        q_keys: Optional[List[str]] = None,
        offset: int = 0,
        status: str = "active",
        review: str = "",
    ) -> List[Dict[str, Any]]:
        """Admin: recent rows, optional filter on memory key (user_id) and source。

        R13：``source`` 可选筛选（``user_stated`` / ``ai_inferred``），让运营一眼分辨
        哪些记忆是 AI 推断、便于人工纠错。

        身份化检索：``q``＝人类可读联合搜索——命中 **记忆键 LIKE、内容 LIKE、
        或 q_keys 键集**（路由层先把昵称/用户名/手机号译成 conversation_id 集合
        传入）三者任一即返回；与 prefix/source 仍为 AND 关系。q_keys 单独给而
        q 为空时不生效（键集只是 q 的翻译产物，不是独立筛选维度）。

        ``offset``＝「加载更多」分页（P1）。排序带 id 次键：created_at 只有秒级
        粒度，同秒多行在纯 created_at 排序下跨页可能重排 → 翻页丢行/重行。

        J-10 A2：``status``＝``active``（默认，日常视图不见软删）/ ``ignored``（维护区
        「已不再使用」可恢复）/ ``all``；``review``＝``pending``（只看进了例外队列的）
        或某一具体原因。每行多带 status / review_reason / impact / conflict_group /
        recall_count / last_recalled_ts。
        """
        limit = max(1, min(int(limit or 100), 500))
        off = max(0, min(int(offset or 0), 100000))
        p = (prefix or "").strip()
        src = source if source in ("user_stated", "ai_inferred") else ""
        qq = (q or "").strip()
        where = []
        params: List[Any] = []
        st = str(status or "active").strip().lower()
        if st in ("active", "ignored"):
            where.append("COALESCE(status, 'active') = ?")
            params.append(st)
        rv = str(review or "").strip().lower()
        if rv == "pending":
            where.append("COALESCE(review_reason, '') != ''")
        elif rv:
            where.append("COALESCE(review_reason, '') = ?")
            params.append(rv)
        if p:
            where.append("user_id LIKE ?")
            params.append(f"%{p}%")
        if qq:
            ors = ["user_id LIKE ?", "content LIKE ?"]
            params.extend([f"%{qq}%", f"%{qq}%"])
            keys = [str(k) for k in (q_keys or []) if str(k or "").strip()]
            keys = keys[:300]  # SQLite 参数上限护栏（默认 999）
            if keys:
                ors.append(f"user_id IN ({','.join('?' * len(keys))})")
                params.extend(keys)
            where.append("(" + " OR ".join(ors) + ")")
        if src:
            where.append("COALESCE(source, 'user_stated') = ?")
            params.append(src)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.extend([limit, off])
        rows = self._conn.execute(
            f"""
            SELECT {self._ROW_COLS}
            FROM episodic_memory{clause}
            ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def admin_summary(self, days: int = 7, top_n: int = 3) -> Dict[str, Any]:
        """管理者摘要：近 N 天新增条数/覆盖用户数 + 全库记忆最多 Top-N 用户。

        ``created_at`` 为 epoch 浮点（生产实测 92/92 全 REAL），时间窗按数值
        比较——**不要**改成字符串时间戳比较：SQLite 类型序里 TEXT 恒大于
        REAL，混用会把全部历史行都算进「新增」。
        """
        d = max(1, min(int(days or 7), 90))
        n = max(1, min(int(top_n or 3), 10))
        since = time.time() - d * 86400
        # J-10 A2：口径只数 active（软删不算「AI 已记住」）；另出全库总数/覆盖客户数
        # + 例外队列计数，供页面顶部「AI 已记住 N 件事 · 覆盖 U 位客户 · 今日需处理 M 件」。
        act = "COALESCE(status, 'active') = 'active'"
        try:
            row = self._conn.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT user_id) FROM episodic_memory"
                f" WHERE created_at >= ? AND {act}",
                (since,),
            ).fetchone()
            tot = self._conn.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT user_id),"
                f" SUM(CASE WHEN COALESCE(tier, 'raw') = 'stable' THEN 1 ELSE 0 END)"
                f" FROM episodic_memory WHERE {act} AND COALESCE(tier, 'raw') != 'stale'",
            ).fetchone()
            top = self._conn.execute(
                f"SELECT user_id, COUNT(*) AS n FROM episodic_memory WHERE {act}"
                " GROUP BY user_id ORDER BY n DESC, MAX(created_at) DESC LIMIT ?",
                (n,),
            ).fetchall()
        except Exception:
            return {"window_days": d, "new_count": 0, "new_users": 0, "top": [],
                    "total_count": 0, "total_users": 0, "stable_count": 0,
                    "review": self.review_counts(),
                    "grounding_drops": self.grounding_drop_summary(days=d)}
        return {
            "window_days": d,
            "new_count": int(row[0] or 0),
            "new_users": int(row[1] or 0),
            "total_count": int(tot[0] or 0),
            "total_users": int(tot[1] or 0),
            "stable_count": int(tot[2] or 0),
            "top": [
                {"memory_key": str(r[0] or ""), "count": int(r[1] or 0)}
                for r in top
            ],
            "review": self.review_counts(),
            "grounding_drops": self.grounding_drop_summary(days=d),
            "recall": self.recall_stats(days=d),
        }

    def record_grounding_drops(
        self, user_id: str, dropped: List[Dict[str, Any]], *, now: Optional[float] = None,
    ) -> int:
        """J-10 A1：接地护栏丢弃记账（按原因）。返回写入行数；绝不抛、零阻断抽取链。

        ``dropped`` 每条 ``{"fact", "evidence", "reason"}``（``memory_grounding.
        filter_grounded_fact_items`` 的输出形状）。顺手清 30 天前的旧行。
        """
        rows = []
        ts = float(now if now is not None else time.time())
        for d in dropped or []:
            if not isinstance(d, dict):
                continue
            reason = str(d.get("reason") or "").strip()[:32]
            if not reason:
                continue
            rows.append((
                ts, str(user_id or "")[:200], reason,
                str(d.get("fact") or "")[:160], str(d.get("evidence") or "")[:120],
            ))
        if not rows:
            return 0
        try:
            self._conn.executemany(
                "INSERT INTO episodic_grounding_drops (ts, user_id, reason, fact, evidence)"
                " VALUES (?, ?, ?, ?, ?)", rows)
            self._conn.execute(
                "DELETE FROM episodic_grounding_drops WHERE ts < ?",
                (ts - self._GROUNDING_DROPS_RETAIN_SEC,))
            self._conn.commit()
            return len(rows)
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic record_grounding_drops failed: %s", e)
            return 0

    def grounding_drop_summary(
        self, days: int = 7, *, user_id: str = "", recent: int = 5,
    ) -> Dict[str, Any]:
        """近 N 天护栏丢弃读数：``{"total", "by_reason": {reason: n}, "recent": [...]}``。

        ``user_id`` 给定则只看该记忆键（客户档案抽屉用）。``recent`` 条示例给页面
        「未记录的有哪些」——fact/evidence 已在写入时截短。store 异常返回零读数。
        """
        d = max(1, min(int(days or 7), 90))
        since = time.time() - d * 86400
        out: Dict[str, Any] = {
            "window_days": d, "total": 0,
            "by_reason": {"no_evidence": 0, "evidence_mismatch": 0, "fact_unanchored": 0},
            "recent": [],
        }
        uid = str(user_id or "").strip()
        where = "ts >= ?"
        params: List[Any] = [since]
        if uid:
            where += " AND user_id = ?"
            params.append(uid)
        try:
            rows = self._conn.execute(
                f"SELECT reason, COUNT(*) FROM episodic_grounding_drops"
                f" WHERE {where} GROUP BY reason", params,
            ).fetchall()
            for reason, n in rows:
                c = int(n or 0)
                out["by_reason"][str(reason or "")] = c
                out["total"] += c
            k = max(0, min(int(recent or 0), 50))
            if k and out["total"]:
                rec = self._conn.execute(
                    f"SELECT ts, user_id, reason, fact, evidence FROM episodic_grounding_drops"
                    f" WHERE {where} ORDER BY ts DESC, id DESC LIMIT ?", params + [k],
                ).fetchall()
                out["recent"] = [
                    {"ts": float(r[0] or 0), "memory_key": str(r[1] or ""),
                     "reason": str(r[2] or ""), "fact": str(r[3] or ""),
                     "evidence": str(r[4] or "")}
                    for r in rec
                ]
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic grounding_drop_summary failed: %s", e)
        return out

    def get_row_brief(self, row_id: int) -> Optional[Dict[str, Any]]:
        """单行摘要（删除审计留痕用）：{id, memory_key, content, source}。

        与 ``confirm_inferred_fact`` 的审计留痕对称——删除是不可逆动作，
        审计里必须能看到「删的是谁的哪条记忆」，仅记 row_id 无法回溯。
        """
        try:
            rid = int(row_id)
        except (TypeError, ValueError):
            return None
        try:
            row = self._conn.execute(
                "SELECT id, user_id, content,"
                " COALESCE(source, 'user_stated')"
                " FROM episodic_memory WHERE id = ?",
                (rid,),
            ).fetchone()
        except Exception:
            return None
        if not row:
            return None
        return {
            "id": int(row[0]),
            "memory_key": str(row[1] or ""),
            "content": str(row[2] or ""),
            "source": str(row[3] or ""),
        }

    def delete_by_id(self, row_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM episodic_memory WHERE id = ?", (int(row_id),)
        )
        self._conn.commit()
        return int(cur.rowcount or 0) > 0

    def delete_by_hashes(self, user_id: str, content_hashes: List[str]) -> int:
        """按 (user_id, content_hash) 精确删除，返回删除行数。

        跨平台档案「整批撤销导入」用（P1 2026-08-18）：批次台账记录事实指纹，
        撤销按指纹删——不误伤同 key 下其他来源的事实（同文写入 add_fact 会因
        唯一索引去重返 None 不入台账，故指纹集合即本批独有写入）。
        """
        uid = str(user_id or "").strip()
        hashes = [str(h).strip() for h in (content_hashes or []) if str(h).strip()]
        if not uid or not hashes:
            return 0
        deleted = 0
        for h in hashes:
            try:
                cur = self._conn.execute(
                    "DELETE FROM episodic_memory WHERE user_id = ? AND content_hash = ?",
                    (uid, h),
                )
                deleted += int(cur.rowcount or 0)
            except Exception:
                continue
        self._conn.commit()
        return deleted

    def delete_by_source_quotes(self, chat_key: str, quotes: List[str]) -> int:
        """#146 对端删消息 → 删「抽取自那句原话」的事实。返回删除行数。

        定位＝记忆键**以组件形式**含 chat_key（``platform:acct:peer`` / ``acct:peer`` /
        ``peer`` / 群键 ``peer_uid``；纯子串不认）∧ ``source_quote`` 归一后与被删正文
        前 200 字相等，**或**（J-10 A1 起 LLM 事实的 ``source_quote`` 是客户原话里的
        一段逐字引文）引文归一后是被删正文的子串（引文归一后 ≥6 字，防「ok」级短引文
        撞遍全部消息）。只删 ``hits == 1``：复发事实＝客户在别处又说过，一条删了事实
        仍成立。quote 为空的早期条目匹配不到，如实接受。绝不抛。
        """
        from src.inbox.peer_delete_purge import key_has_component, quotes_match
        from src.ai.memory_grounding import normalize_for_match
        ck = str(chat_key or "").strip()
        qs = [str(q or "") for q in (quotes or []) if str(q or "").strip()]
        if not ck or not qs:
            return 0
        qs_norm = [normalize_for_match(q) for q in qs]

        def _sub_quote_match(quote: str) -> bool:
            qn = normalize_for_match(quote)
            if len(qn) < 6:
                return False
            return any(qn in t for t in qs_norm if t)
        try:
            rows = self._conn.execute(
                "SELECT id, user_id, content, source_quote, COALESCE(hits, 1)"
                " FROM episodic_memory"
                " WHERE COALESCE(source_quote, '') != '' AND user_id LIKE ?",
                (f"%{ck}%",),
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("delete_by_source_quotes fetch failed: %s", e)
            return 0
        victims: List[int] = []
        for rid, uid, content, quote, hits in rows:
            if not key_has_component(str(uid or ""), ck):
                continue
            if int(hits or 1) > 1:
                continue
            if any(quotes_match(quote, q) for q in qs) or _sub_quote_match(str(quote or "")):
                victims.append(int(rid))
                logger.info("[episodic] peer-deleted source → drop fact id=%s key=%s %r",
                            rid, uid, str(content or "")[:60])
        if not victims:
            return 0
        try:
            self._conn.executemany(
                "DELETE FROM episodic_memory WHERE id = ?", [(i,) for i in victims])
            self._conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("delete_by_source_quotes delete failed: %s", e)
            return 0
        return len(victims)

    def fetch_rows_missing_embedding(
        self, limit: int = 20, memory_key_prefix: str = "", force: bool = False
    ) -> List[Tuple[int, str, str]]:
        """Rows (id, memory_key, content) needing vector backfill. Optional filter on user_id.

        ``force=True`` 返回**所有**行（不只缺向量的）——换 embedding 模型后旧向量维度/空间
        与新模型不兼容，须靠 force 重跑全量重嵌。``cond`` 为内部常量，无 SQL 注入面。
        """
        limit = max(1, min(int(limit or 20), 200))
        p = (memory_key_prefix or "").strip()
        cond = "1=1" if force else "(embedding IS NULL OR length(embedding) < 8)"
        cond += " AND COALESCE(status, 'active') = 'active'"   # J-10 A2：软删不补向量
        if p:
            rows = self._conn.execute(
                f"""
                SELECT id, user_id, content FROM episodic_memory
                WHERE {cond}
                  AND user_id LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (f"%{p}%", limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"""
                SELECT id, user_id, content FROM episodic_memory
                WHERE {cond}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]

    def clear_user(self, user_id: str) -> int:
        cur = self._conn.execute(
            "DELETE FROM episodic_memory WHERE user_id = ?", (user_id,)
        )
        self._conn.commit()
        return int(cur.rowcount or 0)
