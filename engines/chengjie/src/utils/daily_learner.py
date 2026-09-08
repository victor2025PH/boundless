"""
每日自动学习模块
- 汇总 KB 未命中、弱命中、隐式负反馈
- 用 AI 自动生成知识条目草稿
- 存入 kb_drafts 表等人工审核
- 审核通过后一键入库

2026-08-02 断粮复盘改造：
- AI 客户端改为**可选**——stats/list/approve/reject/edit 全是纯 DB 操作，
  不该被"AI 不可达"连坐（zhiliao 实锤：telegram 协议客户端下线后整个
  /api/learner/* 家族假空，库里 3 条待审草稿被藏了一周多）。
- 素材收集出**漏斗计数**（总量/占位符/非问题样式/低于门槛/已有草稿），
  每次运行落 kb_meta（learner_last_run），页面可自解释"为什么是 0"。
- 新增手动喂料 feed_and_learn（运营把没答好的问题直接入队，AI 可用时当场生成）。

2026-09-06 L-4 F（#201 止血四件，D-L5——重做为「案例学习」另立批，这里只止血）：
- 「全部通过」→ 只能通过**已选**且每批 ≤ ``APPROVE_BATCH_LIMIT``（路由层还要 confirm=1）；
- 入队收窄：寒暄 / 任何「[」开头的媒体占位 / 「语音消息-下载失败」类系统文本 / 表情
  标点串不入队（``is_noise_query``）；素材只收「问题样式」的事实类提问；
- 译文：``query_zh`` 列缓存外语原文的中文译文（路由层按需翻一次，复用翻译服务缓存）；
- 私事分流：含人名 / 地点 / 金额 / 见面·日期约定（复用 J-10 ``memory_review`` 词表）的
  条目标 ``private_kinds``，通过后写入**该客户** AI 记忆（``set_memory_writer``）而
  **不进共享 KB**；进 KB 的条目 ``source=learner``（接 J-9 ``kb_entries.source``）；
- 相似度：BM25 文本命中不再折算成「99%」——只有触发词 Jaccard 是真相似度；BM25 命中
  记 ``dup_method=bm25`` / ``dup_score=0``（页面显「相似度未计算」），且必须过
  ``kb_gate.lexical_overlap_ok`` 实词重叠门、不与 vendor 预置条目比对；
- 存量：首次装载对 pending 草稿做一次性清标（噪音自动拒、假重复重算、补 private_kinds），
  ``kb_meta`` 键 ``TRIAGE_BACKFILL_META_KEY`` 幂等。

2026-09-06 M-5 E（#220，DMS45P / RJF9N7）：
- 语种按**文字系统**判（``query_lang`` → translation_service.detect_language：假名即 ja、
  谚文即 ko、泰文即 th、越南声调即 vi，再才看汉字）——日语条目不再因含汉字被当中文跳过
  翻译；路由把真实检测结果当「原语」回给页面；
- 同题草稿**合并**：save_drafts 对已有 pending 同键条目累加 hit_count 不再新建；存量并列
  待审的同题一次性合并（``merge_duplicate_pending``，其余标 rejected +
  ``reviewed_by=system:m5_dup_merge``，``kb_meta`` 键 ``DUP_MERGE_BACKFILL_META_KEY`` 幂等）。
"""

import asyncio
import json
import logging
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from src.utils.kb_gate import (is_system_placeholder, lexical_overlap_ok,
                               looks_like_kb_query)

_logger = logging.getLogger("ai_chat_assistant.DailyLearner")

LAST_RUN_META_KEY = "learner_last_run"

# ── L-4 F（#201）止血常量 ──────────────────────────────────────────────────
#: 一次最多通过多少条（路由 approve-all / batch approve 同口径）
APPROVE_BATCH_LIMIT = 20
#: 存量清标幂等键（kb_meta）
TRIAGE_BACKFILL_META_KEY = "learner_triage_backfill_l4"
#: 系统文本片段（不分大小写）：出现即视为噪音，不入队
_NOISE_FRAGMENTS = (
    "下载失败", "转写失败", "识别失败", "语音消息", "语音訊息", "voice message",
    "download failed", "transcribe failed", "[translate:",
)
# 只有表情 / 标点 / 空白（无任何字母、数字、汉字）
_NO_CONTENT_RE = re.compile(r"^[^\w\u4e00-\u9fff]*$")
#: private_kinds 取值（顺序即页面标签顺序）。O-2 C（#201，2026-09-08）：判定本体迁到
#: ``src/utils/memory_scope.personal_anchors``（与抽取链日志同一口径），本表 +4：
#: ``birthday`` / ``time``（带日号的日期，或星期·相对日·钟点与约见动词合取）/ ``place``
#: （场所词与时间或约见动词合取）/ ``personal``（第一二人称属性：我住在… / I live in…）
#: ——指令验收「约会地点 / 对方生日」两例此前都不在词表里，会被当成可进共享 KB 的条目。
PRIVATE_KINDS: Tuple[str, ...] = (
    "name", "birthday", "time", "place", "money", "meet", "address", "identity",
    "family", "health", "commitment", "personal",
)

# ── M-5 E（#220，2026-09-06）：语种按文字系统判 + 重复待审合并 ────────────────
#: 重复合并存量清理幂等键（kb_meta）
DUP_MERGE_BACKFILL_META_KEY = "learner_dup_merge_m5"
#: 系统合并的重复条目 reviewed_by 标记（「已拒绝」页可追溯）
DUP_MERGE_REVIEWER = "system:m5_dup_merge"
#: 判「原文是不是中文」用的兜底脚本表（translation_service 不可导入时）：
#: 假名 / 谚文 / 泰文 / 越南声调字母任一出现即**不是**中文——不再按汉字比例判
_SCRIPT_NON_ZH_RE = re.compile(
    r"[\u3040-\u30ff]|[\uac00-\ud7af]|[\u0e01-\u0e3a\u0e40-\u0e4e]|"
    r"[\u0103\u0102\u0111\u0110\u01a1\u01a0\u01b0\u01af\u1ea0-\u1ef9]")
_CJK_ONLY_RE = re.compile(r"[\u4e00-\u9fff]")
# 归一化查重键：去首尾空白/全半角标点/大小写/连续空白（「そっか、…のね…」与「そっか…のね」同键）
_QKEY_STRIP_RE = re.compile(
    r"[\s!-/:-@\[-`{-~\u3000-\u303f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65"
    r"…—～·]+")


def query_lang(text: str) -> str:
    """学习条目原文的语种（ISO 639-1；判不出 ``unknown``）。

    **按文字系统判**（DMS45P：日语「そっか、向こうはもう物語の世界に戻るのね」含汉字，
    页面按汉字比例当中文 → 源语=目标语跳过翻译）：复用 ``translation_service.
    detect_language``（脚本块优先：假名→ja、谚文→ko、泰文→th、越南声调→vi，再才看
    汉字），与 M-1 B 出站语言判定同一函数；导入失败时按本模块兜底表判非中文脚本。
    """
    t = str(text or "").strip()
    if not t:
        return "unknown"
    try:
        from src.ai.translation_service import detect_language
        return str(detect_language(t) or "unknown")
    except Exception:
        if _SCRIPT_NON_ZH_RE.search(t):
            return "other"
        return "zh" if _CJK_ONLY_RE.search(t) else "unknown"


def needs_translation(text: str) -> bool:
    """原文要不要补中文译文：非中文、且判得出语种（表情/数字串 unknown 不译）。"""
    lang = query_lang(text)
    return lang not in ("zh", "unknown")


def normalize_query_key(text: str) -> str:
    """待审去重键：小写 + 去标点空白。同题不同标点/空格的条目视为同一条。"""
    t = str(text or "").strip().lower()
    return _QKEY_STRIP_RE.sub("", t)


class LearnerApproveError(Exception):
    """审核通过被拒的结构化原因（路由层转人话）。

    reason ∈ private_no_customer（私事条目没有可归属的客户会话）/
    memory_unavailable（记忆存储未就绪）/ memory_write_failed。
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def is_noise_query(text: str) -> bool:
    """寒暄 / 媒体占位 / 系统文本 / 纯表情标点 → 不值得学（#201 入队收窄）。

    比 ``kb_gate.is_system_placeholder`` 更宽：**任何**「[」「【」开头的文本都算占位
    （skuio 机实录「语音消息-下载失败」没带方括号也进了队列——按片段再兜一层）；
    寒暄复用 ``greeting_lexicon.is_greeting_message``（L-1 归属文件，只 import 不改）。
    """
    t = str(text or "").strip()
    if len(t) < 2:
        return True
    if t.startswith(("[", "【")) or is_system_placeholder(t):
        return True
    low = t.lower()
    if any(f in low for f in _NOISE_FRAGMENTS):
        return True
    if _NO_CONTENT_RE.match(t):
        return True
    try:
        from src.utils.greeting_lexicon import is_greeting_message
        if is_greeting_message(t):
            return True
    except Exception:
        pass
    return False


def is_fact_question(text: str) -> bool:
    """事实类提问＝有「知识提问」样式且不是噪音——入队的最低门槛。"""
    t = str(text or "").strip()
    return bool(t) and not is_noise_query(t) and looks_like_kb_query(t)


def private_kinds(text: str) -> List[str]:
    """条目里的「客户私事」类别（空＝可进共享 KB）。

    判定本体＝``memory_scope.personal_anchors``（O-2 C 单一来源）：J-10 六类高影响 + 承诺
    + 自报姓名 + 生日 + 具体时间 + 具体地点 + 第一二人称属性。命中即分流到该客户记忆——
    共享 KB 里出现「Maria 下周三来马尼拉见我」会让别的客户被复述别人的私事（#201 跨客户
    串记忆）。判定异常 → 空（宁可进人审队列的共享侧，也不让学习器整条崩）。
    """
    try:
        from src.utils.memory_scope import personal_anchors
        kinds = personal_anchors(text)
    except Exception:
        return []
    return [k for k in PRIVATE_KINDS if k in kinds]

# 进程内最近构造的学习器（value_report 周报段 peek 用——与 peek_goal_store 同纪律：
# 消费方只探测既有单例绝不新建，新建会凭空造一个空 drafts 库把「零学习」误报成事实）。
_LAST_INSTANCE: Optional["DailyLearner"] = None


def peek_daily_learner() -> Optional["DailyLearner"]:
    """进程内既有 DailyLearner 单例；从未构造过 → None（周报段整段省略）。

    生产上 /api/todo-summary（仪表盘/案例页待办条 60s 轮询）与 /api/learner/*
    都会惰性构造并复用同一实例，peek 在实际部署里必然温热；测试要密闭就显式传参。
    """
    return _LAST_INSTANCE


def resolve_learner_ai(app=None, telegram_client=None):
    """学习引擎 AI 客户端回落链：telegram 协议客户端 → app.state.ai_client → skill_manager。

    实锤（2026-08-02）：实例迁到 RPA/收件箱形态后 telegram_client=None，
    绑死它的取法让学习队列页面在库里有数据时也渲染成全 0。
    """
    ai = getattr(telegram_client, "ai_client", None) if telegram_client else None
    if ai is not None:
        return ai
    state = getattr(app, "state", None) if app is not None else None
    if state is None:
        return None
    ai = getattr(state, "ai_client", None)
    if ai is not None:
        return ai
    sm = getattr(state, "skill_manager", None)
    return getattr(sm, "ai_client", None) if sm is not None else None


class DailyLearner:

    DRAFTS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS kb_drafts (
        id              TEXT PRIMARY KEY,
        source          TEXT NOT NULL,
        query           TEXT NOT NULL,
        hit_count       INTEGER DEFAULT 1,
        category        TEXT DEFAULT '',
        title           TEXT DEFAULT '',
        triggers        TEXT DEFAULT '',
        example_reply   TEXT DEFAULT '',
        ai_reasoning    TEXT DEFAULT '',
        status          TEXT DEFAULT 'pending',
        reviewed_by     TEXT DEFAULT '',
        created_at      TEXT NOT NULL,
        reviewed_at     TEXT DEFAULT ''
    );
    """

    def __init__(self, kb_store, ai_client=None, db_path: Optional[Path] = None):
        self._kb = kb_store
        self._ai = ai_client  # 可为 None：纯 DB 操作（审核/统计）不需要 AI
        if db_path is None:
            # 真实 KnowledgeBaseStore 的属性是 db_path（无下划线）；旧 mock/旧代码
            # 用 _db_path——两者都认，避免"不传 db_path 就 AttributeError"的暗雷。
            kb_db = getattr(kb_store, "_db_path", None) or getattr(kb_store, "db_path", None)
            if kb_db is None:
                raise ValueError("DailyLearner 需要 db_path 或带 db_path 属性的 kb_store")
            db_path = Path(kb_db).parent / "knowledge_base.db"
        self._db_path = db_path
        # L-4 F：私事条目通过后的去处（conversation_id, content, quote）→ dict|None，
        # 由路由层注入（它才拿得到 skill_manager / 记忆存储）；None＝私事不可通过。
        self._memory_writer: Optional[Callable[[str, str, str], Optional[Dict]]] = None
        self._ensure_table()
        self._triage_backfill_once()
        self._dup_merge_backfill_once()
        global _LAST_INSTANCE
        _LAST_INSTANCE = self

    @property
    def ai_ready(self) -> bool:
        return self._ai is not None

    def attach_ai(self, ai_client) -> None:
        """AI 客户端晚绑定（启动顺序/热接入后补挂，缓存实例即刻恢复生成能力）。"""
        if ai_client is not None:
            self._ai = ai_client

    def set_memory_writer(self, fn) -> None:
        """注入「写入该客户 AI 记忆」的回调（L-4 F 私事分流）。"""
        self._memory_writer = fn if callable(fn) else None

    _MIGRATION_CONFIDENCE = (
        "ALTER TABLE kb_drafts ADD COLUMN confidence INTEGER DEFAULT 0",
    )
    _MIGRATION_DUP = (
        "ALTER TABLE kb_drafts ADD COLUMN dup_entry_id TEXT DEFAULT ''",
        "ALTER TABLE kb_drafts ADD COLUMN dup_entry_title TEXT DEFAULT ''",
        "ALTER TABLE kb_drafts ADD COLUMN dup_score REAL DEFAULT 0",
    )
    # 融合 P2（2026-08-16）：溯源 + 效果回访地基——
    # source_ref＝这条素材由谁触发（case:CASE-xxx / conv:平台会话 id，空=定时采集）；
    # entry_id＝审核通过后的正式 KB 条目 id（与 kb_query_log.matched_entry_id 对账）。
    _MIGRATION_TRACE = (
        "ALTER TABLE kb_drafts ADD COLUMN source_ref TEXT DEFAULT ''",
        "ALTER TABLE kb_drafts ADD COLUMN entry_id TEXT DEFAULT ''",
    )
    # L-4 F（#201）：译文缓存 / 私事类别 / 查重方法（bm25=相似度未计算）
    _MIGRATION_TRIAGE = (
        "ALTER TABLE kb_drafts ADD COLUMN query_zh TEXT DEFAULT ''",
        "ALTER TABLE kb_drafts ADD COLUMN private_kinds TEXT DEFAULT ''",
        "ALTER TABLE kb_drafts ADD COLUMN dup_method TEXT DEFAULT ''",
    )

    def _ensure_table(self):
        conn = sqlite3.connect(str(self._db_path))
        conn.execute(self.DRAFTS_SCHEMA)
        conn.commit()
        # migration: add confidence column
        cols = {r[1] for r in conn.execute("PRAGMA table_info(kb_drafts)").fetchall()}
        if "confidence" not in cols:
            for sql in self._MIGRATION_CONFIDENCE:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            conn.commit()
        # migration: add dup columns
        if "dup_entry_id" not in cols:
            for sql in self._MIGRATION_DUP:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            conn.commit()
        # migration: add traceability columns (source_ref / entry_id)
        if "source_ref" not in cols:
            for sql in self._MIGRATION_TRACE:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            conn.commit()
        # migration: triage columns (query_zh / private_kinds / dup_method)
        if "private_kinds" not in cols:
            for sql in self._MIGRATION_TRIAGE:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            conn.commit()
        conn.close()

    def _triage_backfill_once(self) -> None:
        """存量 pending 草稿一次性清标（#201：skuio 机 166 待审 / 165 假重复 99%）。

        ① 噪音（寒暄 / 占位 / 系统文本）直接标 rejected（reviewed_by=system:l4_noise，
           「已拒绝」页仍可见可追溯）；② 带查重标的按新口径重算（BM25 折算的 99% 清掉，
           只留真触发词重叠或过了实词门的文本命中）；③ 补 private_kinds。
        幂等：kb_meta 键；kb 无 meta 能力（旧 mock）时每次构造都跑——纯 DB 幂等操作，
        代价可忽略。任何异常只记 debug，绝不影响构造。
        """
        try:
            getter = getattr(self._kb, "get_meta", None)
            if callable(getter) and getter(TRIAGE_BACKFILL_META_KEY):
                return
        except Exception:
            pass
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        stats = {"noise_rejected": 0, "dup_recalced": 0, "dup_cleared": 0, "private_tagged": 0}
        try:
            with self._conn() as c:
                rows = [dict(r) for r in c.execute(
                    "SELECT * FROM kb_drafts WHERE status='pending'").fetchall()]
            for d in rows:
                q = str(d.get("query") or "")
                if is_noise_query(q):
                    with self._conn() as c:
                        c.execute(
                            "UPDATE kb_drafts SET status='rejected', reviewed_by=?, "
                            "reviewed_at=? WHERE id=? AND status='pending'",
                            ("system:l4_noise", now, d["id"]))
                    stats["noise_rejected"] += 1
                    continue
                if d.get("dup_entry_id") or (d.get("dup_score") or 0) > 0:
                    before = bool(d.get("dup_entry_id"))
                    dup = self.recheck_duplicate(d["id"])
                    stats["dup_recalced"] += 1
                    if before and not dup:
                        stats["dup_cleared"] += 1
                if not (d.get("private_kinds") or ""):
                    kinds = private_kinds(q)
                    if kinds:
                        with self._conn() as c:
                            c.execute("UPDATE kb_drafts SET private_kinds=? WHERE id=?",
                                      (",".join(kinds), d["id"]))
                        stats["private_tagged"] += 1
            if any(stats.values()):
                _logger.info("学习队列存量清标（L-4 #201）: %s", stats)
        except Exception:
            _logger.debug("学习队列存量清标失败（忽略）", exc_info=True)
        try:
            setter = getattr(self._kb, "set_meta", None)
            if callable(setter):
                setter(TRIAGE_BACKFILL_META_KEY, json.dumps(
                    {"ts": now, **stats}, ensure_ascii=False))
        except Exception:
            pass

    def _conn(self):
        c = sqlite3.connect(str(self._db_path))
        c.row_factory = sqlite3.Row
        return c

    def collect_learning_material(self, min_miss_count: int = 2,
                                  max_items: int = 20) -> List[Dict]:
        """向后兼容薄封装：只要素材列表，不要漏斗。"""
        materials, _ = self.collect_with_funnel(
            min_miss_count=min_miss_count, max_items=max_items
        )
        return materials

    def collect_with_funnel(self, min_miss_count: int = 2,
                            max_items: int = 20) -> Tuple[List[Dict], Dict]:
        """
        从三个来源收集需要学习的素材，并输出漏斗计数（页面自解释"为什么是 0"）：
        1. miss_log 高频未命中（占位符/非问题样式在此过滤——历史池子里
           已混入陪聊闲聊与系统占位符，采集侧兜底一层）
        2. kb_feedback 负反馈（score <= 0 且未处理）
        3. 弱命中（已有条目但匹配分数低）
        """
        materials: List[Dict] = []
        seen_queries = set()
        funnel = {
            "miss_total": 0, "miss_placeholder": 0, "miss_not_question": 0,
            "miss_below_threshold": 0, "miss_qualified": 0,
            "feedback_material": 0, "weak_hits": 0,
            "already_drafted": 0, "final": 0,
            "min_miss_count": max(1, int(min_miss_count)),
        }
        min_miss_count = funnel["min_miss_count"]

        # 来源 1：高频未命中（filter 后可用名额变少，top_k 放宽到 100）
        # L-4 F（#201）入队收窄：占位符 / 寒暄 / 系统文本（is_noise_query）计入
        # miss_placeholder 一档不入队；再要「问题样式」（事实类提问）；再过 cnt 门槛
        # （min_miss_count 默认 2 ＝ 至少被问两次；miss_log 不记客户身份，
        # 「≥2 个不同客户」暂以 ≥2 次代替，见落点表待办）。
        miss_stats = self._kb.get_miss_stats(top_k=100)
        for m in miss_stats:
            q = m["query"].strip()
            if q.startswith("[TRANSLATE:"):
                continue  # 翻译缺口标记走独立管线，不计入学习漏斗
            funnel["miss_total"] += 1
            if is_noise_query(q):
                funnel["miss_placeholder"] += 1
                continue
            if not looks_like_kb_query(q):
                funnel["miss_not_question"] += 1
                continue
            if m["cnt"] < min_miss_count:
                funnel["miss_below_threshold"] += 1
                continue
            if q not in seen_queries:
                funnel["miss_qualified"] += 1
                materials.append({
                    "source": "miss",
                    "query": q,
                    "count": m["cnt"],
                    "last_at": m["last_at"],
                })
                seen_queries.add(q)

        # 来源 2：负反馈
        try:
            feedbacks = self._kb.list_feedback(limit=50)
            for fb in feedbacks:
                if fb.get("score", 0) <= 0 and not fb.get("added_to_examples"):
                    q = fb.get("user_message", "").strip()
                    # 负反馈＝「AI 答错/被改写」的直接证据，不要求 cnt 门槛，但仍不收噪音
                    if q and not is_noise_query(q) and q not in seen_queries:
                        funnel["feedback_material"] += 1
                        materials.append({
                            "source": "negative_feedback",
                            "query": q,
                            "count": 1,
                            "ai_reply": fb.get("ai_reply", ""),
                            "correction": fb.get("correction", ""),
                        })
                        seen_queries.add(q)
        except Exception:
            pass

        # 来源 3：弱命中
        try:
            suggestions = self._kb.get_auto_suggestions(
                weak_threshold=0.45, hours=168, top_k=15
            )
            for s in suggestions:
                if s.get("source") == "weak_hit":
                    q = s.get("query", "").strip()
                    if q and is_fact_question(q) and q not in seen_queries:
                        funnel["weak_hits"] += 1
                        materials.append({
                            "source": "weak_hit",
                            "query": q,
                            "count": s.get("count", 1),
                            "avg_score": s.get("avg_score", 0),
                        })
                        seen_queries.add(q)
        except Exception:
            pass

        # 去掉已有草稿
        existing = set()
        with self._conn() as c:
            rows = c.execute(
                "SELECT query FROM kb_drafts WHERE status IN ('pending','approved')"
            ).fetchall()
            existing = {r["query"] for r in rows}
        before = len(materials)
        materials = [m for m in materials if m["query"] not in existing]
        funnel["already_drafted"] = before - len(materials)

        materials.sort(key=lambda x: -x["count"])
        materials = materials[:max_items]
        funnel["final"] = len(materials)
        return materials, funnel

    async def generate_drafts(self, materials: List[Dict],
                              domain_context: str = "") -> List[Dict]:
        """用 AI 批量生成知识条目草稿"""
        if not materials:
            _logger.info("没有需要学习的素材")
            return []
        if not self._ai:
            _logger.warning("AI 客户端不可用，无法生成草稿（素材保留在池中）")
            return []

        categories = []
        try:
            from src.utils.kb_store import KB_CATEGORIES
            categories = KB_CATEGORIES
        except Exception:
            categories = ["常规咨询", "其他"]

        drafts = []
        batch_size = 5
        for i in range(0, len(materials), batch_size):
            batch = materials[i:i + batch_size]
            batch_drafts = await self._generate_batch(batch, categories, domain_context)
            drafts.extend(batch_drafts)
            if i + batch_size < len(materials):
                await asyncio.sleep(1)

        return drafts

    async def _generate_batch(self, batch: List[Dict], categories: List[str],
                              domain_context: str) -> List[Dict]:
        questions_text = ""
        for idx, m in enumerate(batch, 1):
            extra = ""
            if m.get("ai_reply"):
                extra += f"\n   AI当时回复: {m['ai_reply'][:100]}"
            if m.get("correction"):
                extra += f"\n   用户纠正: {m['correction'][:100]}"
            if m.get("avg_score"):
                extra += f"\n   匹配分数: {m['avg_score']:.2f}（偏低）"
            questions_text += f"{idx}. 用户问: \"{m['query']}\" (被问{m['count']}次){extra}\n"

        cat_list = "、".join(categories) if categories else "常规咨询、其他"

        prompt = f"""你是知识库管理助手。以下是客服系统中用户经常问但知识库没有覆盖的问题。
请为每个问题生成一个知识条目草稿。

{domain_context}

可选分类: {cat_list}

用户问题列表:
{questions_text}

请为每个问题输出 JSON 数组，每个元素包含:
- "index": 对应问题编号
- "category": 所属分类
- "title": 条目标题（简短概括）
- "triggers": 触发关键词数组（3-5个）
- "example_reply": 建议的标准回复（口语化、简洁、专业）
- "reasoning": 你的判断理由（一句话）
- "confidence": 你对这条回复质量的自信度（0-100整数，100=完全确定正确且完整，50=不太确定，0=纯猜测）

只输出 JSON 数组，不要其他内容。"""

        try:
            response = await self._ai.generate_reply(
                user_message=prompt,
                context={"_skip_emotion": True, "_skip_kb": True},
                strategy_overrides={"temperature": 0.3, "max_output_tokens": 2048}
            )
            if not response:
                return []

            json_str = response.strip()
            if json_str.startswith("```"):
                json_str = json_str.split("\n", 1)[-1].rsplit("```", 1)[0]

            items = json.loads(json_str)
            if not isinstance(items, list):
                return []

            drafts = []
            for item in items:
                idx = item.get("index", 0) - 1
                if 0 <= idx < len(batch):
                    m = batch[idx]
                    confidence = max(0, min(100, int(item.get("confidence", 50))))
                    drafts.append({
                        "source": m["source"],
                        "query": m["query"],
                        "hit_count": m["count"],
                        "category": item.get("category", "其他"),
                        "title": item.get("title", m["query"][:30]),
                        "triggers": item.get("triggers", []),
                        "example_reply": item.get("example_reply", ""),
                        "ai_reasoning": item.get("reasoning", ""),
                        "confidence": confidence,
                        "source_ref": str(m.get("source_ref", "") or ""),
                    })
            return drafts

        except json.JSONDecodeError:
            _logger.warning("AI 返回的 JSON 解析失败")
            return []
        except Exception as e:
            _logger.error("AI 生成草稿失败: %s", e)
            return []

    def _pending_key_index(self, c) -> Dict[str, str]:
        """归一化查重键 → 最早一条 pending 草稿 id（同键多条时留最早）。"""
        idx: Dict[str, str] = {}
        try:
            rows = c.execute(
                "SELECT id, query FROM kb_drafts WHERE status='pending' "
                "ORDER BY created_at ASC, id ASC").fetchall()
        except sqlite3.OperationalError:
            return idx
        for r in rows:
            k = normalize_query_key(r["query"])
            if k and k not in idx:
                idx[k] = r["id"]
        return idx

    def save_drafts(self, drafts: List[Dict]) -> int:
        """保存草稿到数据库，自动标记与现有 KB 的重复。

        M-5 E（#220）：同题（归一化后同键）已有 pending 草稿 → **合并**进那一条
        （hit_count 累加），不再并列第二条待审——此前只有手动喂料查同题，定时采集
        两轮各生成一份（RJF9N7：同一日语条目出现两次都标「重复」）。
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        saved = 0
        merged = 0
        with self._conn() as c:
            key_idx = self._pending_key_index(c)
            for d in drafts:
                qkey = normalize_query_key(d.get("query", ""))
                if qkey and qkey in key_idx:
                    try:
                        c.execute(
                            "UPDATE kb_drafts SET hit_count = hit_count + ? "
                            "WHERE id=? AND status='pending'",
                            (max(1, int(d.get("hit_count", 1) or 1)), key_idx[qkey]))
                        merged += 1
                        _logger.info("草稿同题合并进 %s（M-5 #220）: %s",
                                     key_idx[qkey], str(d.get("query", ""))[:60])
                    except sqlite3.Error:
                        _logger.debug("同题合并失败（忽略）", exc_info=True)
                    continue
                draft_id = str(uuid.uuid4())[:8]
                triggers = d.get("triggers", [])
                if isinstance(triggers, list):
                    triggers = ",".join(triggers)
                dup = self.check_duplicate(d)
                dup_eid = dup["entry_id"] if dup else ""
                dup_etitle = dup["entry_title"] if dup else ""
                dup_score = dup["score"] if dup else 0
                dup_method = str(dup.get("method", "") or "") if dup else ""
                kinds = ",".join(private_kinds(d.get("query", "")))
                try:
                    c.execute(
                        "INSERT INTO kb_drafts "
                        "(id,source,query,hit_count,category,title,triggers,"
                        "example_reply,ai_reasoning,status,created_at,confidence,"
                        "dup_entry_id,dup_entry_title,dup_score,source_ref,"
                        "dup_method,private_kinds) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (draft_id, d["source"], d["query"], d.get("hit_count", 1),
                         d.get("category", ""), d.get("title", ""),
                         triggers, d.get("example_reply", ""),
                         d.get("ai_reasoning", ""), "pending", now,
                         d.get("confidence", 0),
                         dup_eid, dup_etitle, dup_score,
                         str(d.get("source_ref", "") or "")[:120],
                         dup_method, kinds)
                    )
                    if dup:
                        _logger.info("草稿 %s 疑似重复: KB=%s score=%.2f",
                                     draft_id, dup_eid, dup_score)
                    saved += 1
                    if qkey:
                        key_idx[qkey] = draft_id   # 同批第二份同题也合并
                except sqlite3.IntegrityError:
                    pass
        if merged:
            _logger.info("本轮 %d 条同题草稿已合并进既有待审（M-5 #220）", merged)
        return saved

    def merge_duplicate_pending(self, *, reviewer: str = DUP_MERGE_REVIEWER) -> Dict:
        """把**已并列待审**的同题草稿合并为一条（M-5 E #220）。

        同键多条：留最早那条（hit_count 累加各份、译文 query_zh 取任一非空），其余标
        ``rejected`` + ``reviewed_by=system:m5_dup_merge``（「已拒绝」页可追溯，不删数据）。
        返回 ``{"groups": 同题组数, "merged": 被合并条数}``；纯 DB 幂等操作。
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        stats = {"groups": 0, "merged": 0}
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id, query, hit_count, query_zh FROM kb_drafts WHERE status='pending' "
                "ORDER BY created_at ASC, id ASC").fetchall()]
            groups: Dict[str, List[Dict]] = {}
            for r in rows:
                k = normalize_query_key(r.get("query", ""))
                if k:
                    groups.setdefault(k, []).append(r)
            for k, items in groups.items():
                if len(items) < 2:
                    continue
                stats["groups"] += 1
                keep, rest = items[0], items[1:]
                extra_hits = sum(max(1, int(x.get("hit_count") or 1)) for x in rest)
                zh = str(keep.get("query_zh") or "") or next(
                    (str(x.get("query_zh") or "") for x in rest if x.get("query_zh")), "")
                c.execute(
                    "UPDATE kb_drafts SET hit_count = hit_count + ?, query_zh=? WHERE id=?",
                    (extra_hits, zh[:400], keep["id"]))
                for x in rest:
                    c.execute(
                        "UPDATE kb_drafts SET status='rejected', reviewed_by=?, reviewed_at=? "
                        "WHERE id=? AND status='pending'",
                        (reviewer, now, x["id"]))
                    stats["merged"] += 1
        if stats["merged"]:
            _logger.info("学习队列同题待审合并（M-5 #220）: %s", stats)
        return stats

    def _dup_merge_backfill_once(self) -> None:
        """存量并列待审的同题草稿一次性合并（kb_meta 幂等；无 meta 能力时每次构造都跑，
        纯 DB 幂等）。任何异常只记 debug，绝不影响构造。"""
        try:
            getter = getattr(self._kb, "get_meta", None)
            if callable(getter) and getter(DUP_MERGE_BACKFILL_META_KEY):
                return
        except Exception:
            pass
        stats: Dict = {}
        try:
            stats = self.merge_duplicate_pending()
        except Exception:
            _logger.debug("学习队列同题合并存量清理失败（忽略）", exc_info=True)
        try:
            setter = getattr(self._kb, "set_meta", None)
            if callable(setter):
                setter(DUP_MERGE_BACKFILL_META_KEY, json.dumps(
                    {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **stats}, ensure_ascii=False))
        except Exception:
            pass

    async def run_daily_learn(self, domain_context: str = "",
                              min_miss_count: Optional[int] = None,
                              source: str = "scheduled") -> Dict:
        """执行一次完整的学习流程。

        无论收集结果如何都落 last_run（kb_meta）——「上次跑了、收集了多少、
        为什么是 0」必须对页面可见，否则全 0 页面无法自解释。
        """
        _logger.info("开始每日自动学习...")
        t0 = time.time()
        try:
            mmc = 2 if min_miss_count is None else max(1, int(min_miss_count))
        except (TypeError, ValueError):
            mmc = 2

        materials, funnel = self.collect_with_funnel(min_miss_count=mmc)
        _logger.info("收集到 %d 条学习素材", len(materials))

        result = {"collected": len(materials), "generated": 0, "saved": 0}
        if materials and not self._ai:
            # 有素材但 AI 不可用：如实记录，素材留在池里等 AI 恢复后下轮处理
            result["error"] = "ai_unavailable"
            _logger.warning("有 %d 条学习素材但 AI 客户端不可用，跳过生成", len(materials))
        elif materials:
            drafts = await self.generate_drafts(materials, domain_context)
            _logger.info("AI 生成了 %d 条草稿", len(drafts))

            saved = self.save_drafts(drafts)
            _logger.info("保存了 %d 条草稿，等待人工审核", saved)
            result.update({"generated": len(drafts), "saved": saved})

            # 清理已处理的 miss_log 条目
            for m in materials:
                if m["source"] == "miss":
                    try:
                        self._kb.delete_miss_entry(m["query"])
                    except Exception:
                        pass

        self._record_last_run(result, funnel, source,
                              duration_ms=int((time.time() - t0) * 1000))
        return result

    def _record_last_run(self, result: Dict, funnel: Dict, source: str,
                         duration_ms: int = 0) -> None:
        """把本轮运行结果落 kb_meta（best-effort，绝不影响学习流程本身）。"""
        try:
            payload = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source": source,
                "collected": result.get("collected", 0),
                "generated": result.get("generated", 0),
                "saved": result.get("saved", 0),
                "duration_ms": duration_ms,
                "funnel": funnel,
            }
            if result.get("error"):
                payload["error"] = result["error"]
            self._kb.set_meta(LAST_RUN_META_KEY, json.dumps(payload, ensure_ascii=False))
        except Exception:
            _logger.debug("last_run 落盘失败（忽略）", exc_info=True)

    def last_run(self) -> Optional[Dict]:
        try:
            raw = self._kb.get_meta(LAST_RUN_META_KEY)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def feed_and_learn(self, query: str, domain_context: str = "",
                             min_miss_count: int = 2,
                             source_ref: str = "") -> Dict:
        """手动喂料：把运营指定的问题入队；AI 可用则当场生成草稿。

        人工点名的问题**不过问题样式过滤**（人的判断优先于启发式）；
        重复（已有 pending/approved 同题草稿）直接短路，不烧 LLM。
        ``source_ref``（融合 P2）＝触发来源锚点（``case:CASE-xxx`` / ``conv:会话id``），
        随草稿落库供审核人看「这题因为谁学的」；空串=无锚点（旧调用方零变化）。
        """
        q = str(query or "").strip()[:200]
        ref = str(source_ref or "").strip()[:120]
        if len(q) < 2:
            return {"queued": False, "reason": "too_short"}

        with self._conn() as c:
            dup = c.execute(
                "SELECT id FROM kb_drafts WHERE query=? AND status IN ('pending','approved')",
                (q,)
            ).fetchone()
        if dup:
            return {"queued": False, "reason": "duplicate", "draft_id": dup["id"]}

        # 先落池（AI 不可用时素材不丢，等下轮定时学习收割）
        try:
            self._kb.seed_miss(q, min_cnt=max(1, int(min_miss_count)))
        except Exception:
            _logger.debug("seed_miss 失败（忽略）", exc_info=True)

        if not self._ai:
            return {"queued": True, "generated": 0, "reason": "ai_unavailable"}

        material = {"source": "manual", "query": q,
                    "count": max(1, int(min_miss_count)), "source_ref": ref}
        drafts = await self.generate_drafts([material], domain_context)
        saved = self.save_drafts(drafts) if drafts else 0
        if saved:
            try:
                self._kb.delete_miss_entry(q)
            except Exception:
                pass
        return {"queued": True, "generated": saved}

    # ── 草稿管理 API ──

    def list_drafts(self, status: str = "pending", limit: int = 50,
                     sort: str = "priority") -> List[Dict]:
        """List drafts. sort='priority' sorts by composite score (confidence + hit_count)."""
        order = "created_at DESC"
        if sort == "priority":
            order = "(confidence * 0.5 + MIN(hit_count * 10, 100) * 0.5) DESC, created_at DESC"
        elif sort == "confidence":
            order = "confidence DESC, created_at DESC"
        elif sort == "hit_count":
            order = "hit_count DESC, created_at DESC"
        with self._conn() as c:
            if status == "all":
                rows = c.execute(
                    f"SELECT * FROM kb_drafts ORDER BY {order} LIMIT ?",
                    (limit,)
                ).fetchall()
            else:
                rows = c.execute(
                    f"SELECT * FROM kb_drafts WHERE status=? ORDER BY {order} LIMIT ?",
                    (status, limit)
                ).fetchall()
        return [dict(r) for r in rows]

    def get_draft(self, draft_id: str) -> Optional[Dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM kb_drafts WHERE id=?", (draft_id,)).fetchone()
        return dict(row) if row else None

    def update_draft(self, draft_id: str, data: Dict) -> bool:
        """编辑草稿（审核前可以修改标题、回复等）"""
        fields = []
        values = []
        for key in ("category", "title", "triggers", "example_reply"):
            if key in data:
                fields.append(f"{key}=?")
                values.append(data[key])
        if not fields:
            return False
        values.append(draft_id)
        with self._conn() as c:
            c.execute(
                f"UPDATE kb_drafts SET {','.join(fields)} WHERE id=?",
                values
            )
        return True

    def recheck_duplicate(self, draft_id: str) -> Optional[Dict]:
        """A3: 重新检测草稿与 KB 的重复（审核前可调用以刷新结果）。"""
        draft = self.get_draft(draft_id)
        if not draft:
            return None
        dup = self.check_duplicate(draft)
        dup_eid = dup["entry_id"] if dup else ""
        dup_etitle = dup["entry_title"] if dup else ""
        dup_score = dup["score"] if dup else 0
        dup_method = str(dup.get("method", "") or "") if dup else ""
        with self._conn() as c:
            c.execute(
                "UPDATE kb_drafts SET dup_entry_id=?, dup_entry_title=?, dup_score=?, "
                "dup_method=? WHERE id=?",
                (dup_eid, dup_etitle, dup_score, dup_method, draft_id)
            )
        return dup

    @staticmethod
    def _draft_private_kinds(draft: Dict) -> List[str]:
        raw = str(draft.get("private_kinds") or "")
        return [k for k in raw.split(",") if k.strip()]

    @staticmethod
    def _draft_conversation_id(draft: Dict) -> str:
        ref = str(draft.get("source_ref") or "").strip()
        return ref[5:] if ref.startswith("conv:") else ""

    def set_translation(self, draft_id: str, query_zh: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE kb_drafts SET query_zh=? WHERE id=?",
                      (str(query_zh or "")[:400], draft_id))

    def approve_draft(self, draft_id: str, operator: str = "") -> Optional[str]:
        """审核通过。

        - 普通条目 → 入库为正式知识条目（``source=learner``，接 J-9 来源字段），返回 KB
          entry_id；
        - 「客户私事」条目（``private_kinds`` 非空）→ **不进共享 KB**，写入该客户的 AI
          记忆（``source_ref=conv:…`` 定位客户；记忆写入回调由路由注入），返回
          ``mem:<记忆键>``；没有可归属的客户或记忆存储未就绪 → 抛
          ``LearnerApproveError``（路由转人话），草稿保持 pending。
        草稿不存在 / 非 pending → None（旧契约）。
        """
        draft = self.get_draft(draft_id)
        if not draft or draft["status"] != "pending":
            return None

        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        kinds = self._draft_private_kinds(draft)
        if kinds:
            conv = self._draft_conversation_id(draft)
            if not conv:
                raise LearnerApproveError("private_no_customer", ",".join(kinds))
            if self._memory_writer is None:
                raise LearnerApproveError("memory_unavailable", ",".join(kinds))
            try:
                res = self._memory_writer(conv, str(draft.get("query") or ""),
                                          str(draft.get("query") or ""))
            except Exception as e:  # noqa: BLE001 - 记忆层异常统一转结构化原因
                raise LearnerApproveError("memory_write_failed", str(e)[:120])
            if not isinstance(res, dict) or not res.get("key"):
                raise LearnerApproveError("memory_write_failed", ",".join(kinds))
            mem_ref = f"mem:{res['key']}"
            with self._conn() as c:
                c.execute(
                    "UPDATE kb_drafts SET status='approved', reviewed_by=?, "
                    "reviewed_at=?, entry_id=? WHERE id=?",
                    (operator, now, mem_ref, draft_id))
            _logger.info("草稿 %s（客户私事 %s）已写入客户记忆 %s，未进 KB",
                         draft_id, ",".join(kinds), res["key"])
            # O-2 C：去向一行（与抽取链同前缀，诊断包一把捞齐三分流）
            _logger.info("[episodic] scope=customer reason=%s draft=%s dest=memory:%s",
                         ",".join(kinds), draft_id, res["key"])
            return mem_ref

        triggers = draft.get("triggers", "")
        if isinstance(triggers, str):
            triggers = [t.strip() for t in triggers.split(",") if t.strip()]

        entry_id = self._kb.add_entry({
            "category": draft.get("category", "其他"),
            "title": draft["title"],
            "triggers": triggers,
            "example_reply_zh": draft.get("example_reply", ""),
            "reply_mode": "ai_strict",
            "enabled": True,
            "source": "learner",
        })

        with self._conn() as c:
            # entry_id 同步落草稿行（融合 P2）：与 kb_query_log.matched_entry_id
            # 对账的钥匙——「学了有没有用」从此可回访。
            c.execute(
                "UPDATE kb_drafts SET status='approved', reviewed_by=?, "
                "reviewed_at=?, entry_id=? WHERE id=?",
                (operator, now, str(entry_id or ""), draft_id)
            )

        _logger.info("草稿 %s 已审核通过，入库为条目 %s", draft_id, entry_id)
        _logger.info("[episodic] scope=shared reason=no_personal_anchor draft=%s dest=kb:%s",
                     draft_id, entry_id)
        return entry_id

    def reject_draft(self, draft_id: str, operator: str = "") -> bool:
        """拒绝草稿"""
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as c:
            c.execute(
                "UPDATE kb_drafts SET status='rejected', reviewed_by=?, reviewed_at=? WHERE id=?",
                (operator, now, draft_id)
            )
        return True

    def approve_all_pending(self, operator: str = "",
                            draft_ids: Optional[List[str]] = None) -> int:
        """通过**已选**（L-4 F #201：不再一键清空整队列）。

        ``draft_ids`` 必填且 ≤ ``APPROVE_BATCH_LIMIT``；旧调用（不传 ids）一律 0——
        skuio 机 166 条一键入共享 KB 无确认正是本单的 P1 事故，不给回退口。
        """
        if not draft_ids:
            return 0
        ids = [str(i) for i in draft_ids][:APPROVE_BATCH_LIMIT]
        return int(self.batch_action(ids, "approve", operator=operator)["approved"])

    def batch_action(self, draft_ids: List[str], action: str, operator: str = "") -> Dict:
        """A2: 批量通过或拒绝指定草稿。

        L-4 F：通过每批 ≤ ``APPROVE_BATCH_LIMIT``（超出的进 ``skipped``）；私事条目
        通过失败带原因进 ``failed_reasons``（拒绝不设上限——清噪音不该被卡）。
        """
        approved = 0
        rejected = 0
        failed: List[str] = []
        failed_reasons: Dict[str, str] = {}
        skipped: List[str] = []
        ids = [str(i) for i in (draft_ids or [])]
        if action == "approve" and len(ids) > APPROVE_BATCH_LIMIT:
            skipped = ids[APPROVE_BATCH_LIMIT:]
            ids = ids[:APPROVE_BATCH_LIMIT]
        for did in ids:
            try:
                if action == "approve":
                    if self.approve_draft(did, operator):
                        approved += 1
                    else:
                        failed.append(did)
                elif action == "reject":
                    self.reject_draft(did, operator)
                    rejected += 1
            except LearnerApproveError as e:
                failed.append(did)
                failed_reasons[did] = e.reason
            except Exception:
                failed.append(did)
        out = {"approved": approved, "rejected": rejected, "failed": failed}
        if failed_reasons:
            out["failed_reasons"] = failed_reasons
        if skipped:
            out["skipped"] = skipped
            out["limit"] = APPROVE_BATCH_LIMIT
        return out

    def stats(self) -> Dict:
        with self._conn() as c:
            pending = c.execute("SELECT COUNT(*) FROM kb_drafts WHERE status='pending'").fetchone()[0]
            approved = c.execute("SELECT COUNT(*) FROM kb_drafts WHERE status='approved'").fetchone()[0]
            rejected = c.execute("SELECT COUNT(*) FROM kb_drafts WHERE status='rejected'").fetchone()[0]
            # L-4 F：查重标以 dup_entry_id 为准（BM25 文本命中 dup_score=0 但仍是标）
            dup_flagged = c.execute(
                "SELECT COUNT(*) FROM kb_drafts WHERE status='pending' "
                "AND (dup_score > 0 OR IFNULL(dup_entry_id,'') <> '')"
            ).fetchone()[0]
            private_pending = c.execute(
                "SELECT COUNT(*) FROM kb_drafts WHERE status='pending' "
                "AND IFNULL(private_kinds,'') <> ''"
            ).fetchone()[0]
        out = {"pending": pending, "approved": approved, "rejected": rejected,
               "dup_flagged": dup_flagged, "private_pending": private_pending,
               "approve_limit": APPROVE_BATCH_LIMIT}
        # 未命中池现存量（排除翻译标记）——状态条用，坏了不影响主数据
        try:
            with self._kb._conn() as c:
                out["miss_rows"] = c.execute(
                    "SELECT COUNT(*) FROM kb_miss_log WHERE query NOT LIKE '[TRANSLATE:%'"
                ).fetchone()[0]
        except Exception:
            pass
        lr = self.last_run()
        if lr:
            out["last_run"] = lr
        return out

    # ── 融合 P2（2026-08-16）：学习效果回访 + 周报窗口 ────────────────

    def effect_report(self, days: int = 7, top_k: int = 5) -> Dict:
        """入库条目的真实命中回访——「学了有没有用」的读数。

        数据地基＝KB 既有 ``kb_query_log``（每次检索都落 matched_entry_id，
        7 天滚动保留）——**纯只读，零热路径改动**。命中只可能发生在条目
        入库之后（matched_entry_id 引用的条目由 approve 创建），故窗口计数
        即「入库以来命中数」。kb 库不可查/旧库无表一律软失败返回空报告。
        """
        days = max(1, int(days))
        cutoff = time.time() - days * 86400.0
        cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(cutoff))
        out: Dict = {"window_days": days, "approved_n": 0,
                     "total_hits": 0, "entries": []}
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, query, title, entry_id, reviewed_at, source_ref "
                "FROM kb_drafts WHERE status='approved' AND entry_id != '' "
                "AND reviewed_at >= ? ORDER BY reviewed_at DESC LIMIT 50",
                (cutoff_iso,)
            ).fetchall()
        out["approved_n"] = len(rows)
        if not rows:
            return out
        hits_by_entry: Dict[str, int] = {}
        try:
            with self._kb._conn() as kc:
                for r in kc.execute(
                        "SELECT matched_entry_id, COUNT(*) FROM kb_query_log "
                        "WHERE hit=1 AND matched_entry_id != '' AND ts >= ? "
                        "GROUP BY matched_entry_id", (cutoff,)).fetchall():
                    hits_by_entry[str(r[0])] = int(r[1])
        except Exception:
            _logger.debug("effect_report kb_query_log 查询失败（忽略）", exc_info=True)
        entries = []
        total = 0
        for r in rows:
            h = hits_by_entry.get(str(r["entry_id"]), 0)
            total += h
            entries.append({
                "draft_id": r["id"],
                "title": r["title"] or str(r["query"] or "")[:30],
                "entry_id": r["entry_id"],
                "hits": h,
                "reviewed_at": r["reviewed_at"],
                "source_ref": r["source_ref"] or "",
            })
        entries.sort(key=lambda e: -int(e["hits"]))
        out["entries"] = entries[:max(1, int(top_k))]
        out["total_hits"] = total
        return out

    def retirement_candidates(self, min_age_days: int = 3,
                              limit: int = 10) -> List[Dict]:
        """零命中淘汰建议（融合 P3）：入库 ≥min_age_days 且检索日志全窗零命中的条目。

        诚实口径：kb_query_log 只留 7 天，「零命中」的可证窗口＝min(入库至今, 7 天)。
        两道防冤枉闸：① 日志不可查（旧库无表/KB 挂了）→ 返回空；② 日志**整体
        零流量**（没人问 ≠ 条目没用，KB 检索没在跑时人人零命中）→ 同样返回空——
        「不知道」绝不伪装成「零命中」。**只出建议不动数据**：停用/改触发词是
        /knowledge 页的显式操作，此处不造第二个 KB 写入口（防双源红线）。
        """
        age_cut_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.localtime(time.time() - max(1, int(min_age_days)) * 86400.0))
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, query, title, entry_id, reviewed_at, source_ref "
                "FROM kb_drafts WHERE status='approved' AND entry_id != '' "
                "AND reviewed_at <= ? AND reviewed_at != '' "
                "ORDER BY reviewed_at DESC LIMIT 100",
                (age_cut_iso,)).fetchall()
        if not rows:
            return []
        hit_ids = set()
        try:
            with self._kb._conn() as kc:
                log_rows = int(kc.execute(
                    "SELECT COUNT(*) FROM kb_query_log").fetchone()[0] or 0)
                if log_rows <= 0:
                    return []   # 零流量窗口：零命中不可证，宁可不出建议
                for r in kc.execute(
                        "SELECT DISTINCT matched_entry_id FROM kb_query_log "
                        "WHERE hit=1 AND matched_entry_id != ''").fetchall():
                    hit_ids.add(str(r[0]))
        except Exception:
            _logger.debug("retirement_candidates 日志不可查（不出建议）", exc_info=True)
            return []
        out: List[Dict] = []
        for r in rows:
            if str(r["entry_id"]) in hit_ids:
                continue
            out.append({
                "draft_id": r["id"],
                "title": r["title"] or str(r["query"] or "")[:30],
                "entry_id": r["entry_id"],
                "reviewed_at": r["reviewed_at"],
                "source_ref": r["source_ref"] or "",
            })
            if len(out) >= max(1, int(limit)):
                break
        return out

    def weekly_window(self, lo: float, hi: float) -> Dict:
        """[lo, hi) 窗口学习台账（value_report 周报段消费；纯 DB 零 AI）。

        ``coverage_hits``＝入库草稿的 hit_count 合计（这些问题在学会前被客户
        问过多少次）——「补上的知识缺口有多大」的量化。pending 为当前值
        （积压没有时间维度）。reviewed_at 为本模块统一的 ISO 格式，串比较安全。
        """
        lo_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(lo)))
        hi_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(hi)))
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(hit_count),0) FROM kb_drafts "
                "WHERE status='approved' AND reviewed_at >= ? AND reviewed_at < ?",
                (lo_iso, hi_iso)).fetchone()
            rejected = c.execute(
                "SELECT COUNT(*) FROM kb_drafts WHERE status='rejected' "
                "AND reviewed_at >= ? AND reviewed_at < ?",
                (lo_iso, hi_iso)).fetchone()[0]
            pending = c.execute(
                "SELECT COUNT(*) FROM kb_drafts WHERE status='pending'"
            ).fetchone()[0]
        return {"approved": int(row[0]), "coverage_hits": int(row[1] or 0),
                "rejected": int(rejected), "pending": int(pending)}

    # ── A3: Semantic duplicate detection ─────────────────────────────

    _DUP_BM25_THRESHOLD = 8.0
    _DUP_TRIGGER_THRESHOLD = 0.4

    def check_duplicate(self, draft: Dict) -> Optional[Dict]:
        """
        Multi-layer duplicate check against existing KB (no API calls).
        Layer 1: Trigger set overlap (Jaccard ≥ 0.4) —— score 是真相似度（0–1）
        Layer 2: BM25 text search（≥ 8.0）—— L-4 F（#201）起：
          - 不与 vendor 预置条目比对（skuio 机 165 条「99%」全撞在厂商播种语料上）；
          - 命中还要过 ``kb_gate.lexical_overlap_ok`` 实词重叠门（BM25 原始分 14–292，
            任何常用字重叠都能过 8.0 阈值）；
          - **不再把 BM25 分折算成百分比**（``min(score/15, 0.99)`` 就是那个恒 99%）：
            score 记 0、method=bm25，页面显「文本匹配 · 相似度未计算」。
        Returns {entry_id, entry_title, score, method} or None.
        """
        # Build search text from draft fields
        draft_triggers_raw = draft.get("triggers", "")
        if isinstance(draft_triggers_raw, list):
            draft_triggers = [t.strip().lower() for t in draft_triggers_raw if t.strip()]
        else:
            draft_triggers = [t.strip().lower()
                              for t in str(draft_triggers_raw).split(",") if t.strip()]
        draft_title = (draft.get("title") or "").strip()
        draft_query = (draft.get("query") or "").strip()
        search_text = f"{draft_title} {' '.join(draft_triggers)} {draft_query}"
        if len(search_text.strip()) < 2:
            return None

        # Layer 1: trigger overlap against KB entries
        best_trigger_match = self._check_trigger_overlap(draft_triggers)
        if best_trigger_match and best_trigger_match["score"] >= self._DUP_TRIGGER_THRESHOLD:
            return best_trigger_match

        # Layer 2: BM25 search（不含 vendor；旧 mock 不认 include_vendor 时回落旧签名）
        try:
            try:
                result = self._kb.search(search_text, top_k=3, include_vendor=False)
            except TypeError:
                result = self._kb.search(search_text, top_k=3)
            entries = (result or {}).get("entries", []) if isinstance(result, dict) else []
            probe = f"{draft_title} {draft_query}".strip() or search_text
            for top in entries:
                if str(top.get("source") or "") == "vendor":
                    continue
                bm25_score = top.get("_score", 0)
                if bm25_score < self._DUP_BM25_THRESHOLD:
                    break
                ok, _why = lexical_overlap_ok(probe, top)
                if not ok:
                    continue
                return {
                    "entry_id": top["id"],
                    "entry_title": top.get("title", ""),
                    "score": 0.0,
                    "method": "bm25",
                }
        except Exception as e:
            _logger.debug("check_duplicate BM25 search error: %s", e)

        return None

    def _check_trigger_overlap(self, draft_triggers: List[str]) -> Optional[Dict]:
        """Check if draft triggers overlap with existing KB entry triggers."""
        if not draft_triggers:
            return None
        draft_set = set(draft_triggers)
        best = None
        best_score = 0.0
        try:
            with self._kb._conn() as c:
                rows = c.execute(
                    "SELECT id, title, triggers FROM kb_entries WHERE enabled=1"
                ).fetchall()
            for row in rows:
                raw = row["triggers"] or ""
                if isinstance(raw, str):
                    try:
                        kb_triggers = [t.strip().lower() for t in json.loads(raw) if t.strip()]
                    except (json.JSONDecodeError, TypeError):
                        kb_triggers = [t.strip().lower() for t in raw.split(",") if t.strip()]
                else:
                    kb_triggers = [str(t).strip().lower() for t in raw if t]
                if not kb_triggers:
                    continue
                kb_set = set(kb_triggers)
                inter = len(draft_set & kb_set)
                if inter == 0:
                    continue
                union = len(draft_set | kb_set)
                jaccard = inter / union if union else 0.0
                if jaccard > best_score:
                    best_score = jaccard
                    best = {
                        "entry_id": row["id"],
                        "entry_title": row["title"] or "",
                        "score": round(jaccard, 2),
                        "method": "trigger_overlap",
                    }
        except Exception as e:
            _logger.debug("_check_trigger_overlap error: %s", e)
        return best
