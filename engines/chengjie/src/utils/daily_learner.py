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
"""

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.utils.kb_gate import is_system_placeholder, looks_like_kb_query

_logger = logging.getLogger("ai_chat_assistant.DailyLearner")

LAST_RUN_META_KEY = "learner_last_run"


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
        self._ensure_table()

    @property
    def ai_ready(self) -> bool:
        return self._ai is not None

    def attach_ai(self, ai_client) -> None:
        """AI 客户端晚绑定（启动顺序/热接入后补挂，缓存实例即刻恢复生成能力）。"""
        if ai_client is not None:
            self._ai = ai_client

    _MIGRATION_CONFIDENCE = (
        "ALTER TABLE kb_drafts ADD COLUMN confidence INTEGER DEFAULT 0",
    )
    _MIGRATION_DUP = (
        "ALTER TABLE kb_drafts ADD COLUMN dup_entry_id TEXT DEFAULT ''",
        "ALTER TABLE kb_drafts ADD COLUMN dup_entry_title TEXT DEFAULT ''",
        "ALTER TABLE kb_drafts ADD COLUMN dup_score REAL DEFAULT 0",
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
        conn.close()

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
        miss_stats = self._kb.get_miss_stats(top_k=100)
        for m in miss_stats:
            q = m["query"].strip()
            if q.startswith("[TRANSLATE:"):
                continue  # 翻译缺口标记走独立管线，不计入学习漏斗
            funnel["miss_total"] += 1
            if is_system_placeholder(q):
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
                    if q and not is_system_placeholder(q) and q not in seen_queries:
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
                    if q and not is_system_placeholder(q) and q not in seen_queries:
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
                    })
            return drafts

        except json.JSONDecodeError:
            _logger.warning("AI 返回的 JSON 解析失败")
            return []
        except Exception as e:
            _logger.error("AI 生成草稿失败: %s", e)
            return []

    def save_drafts(self, drafts: List[Dict]) -> int:
        """保存草稿到数据库，自动标记与现有 KB 的重复。"""
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        saved = 0
        with self._conn() as c:
            for d in drafts:
                draft_id = str(uuid.uuid4())[:8]
                triggers = d.get("triggers", [])
                if isinstance(triggers, list):
                    triggers = ",".join(triggers)
                dup = self.check_duplicate(d)
                dup_eid = dup["entry_id"] if dup else ""
                dup_etitle = dup["entry_title"] if dup else ""
                dup_score = dup["score"] if dup else 0
                try:
                    c.execute(
                        "INSERT INTO kb_drafts "
                        "(id,source,query,hit_count,category,title,triggers,"
                        "example_reply,ai_reasoning,status,created_at,confidence,"
                        "dup_entry_id,dup_entry_title,dup_score) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (draft_id, d["source"], d["query"], d.get("hit_count", 1),
                         d.get("category", ""), d.get("title", ""),
                         triggers, d.get("example_reply", ""),
                         d.get("ai_reasoning", ""), "pending", now,
                         d.get("confidence", 0),
                         dup_eid, dup_etitle, dup_score)
                    )
                    if dup:
                        _logger.info("草稿 %s 疑似重复: KB=%s score=%.2f",
                                     draft_id, dup_eid, dup_score)
                    saved += 1
                except sqlite3.IntegrityError:
                    pass
        return saved

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
                             min_miss_count: int = 2) -> Dict:
        """手动喂料：把运营指定的问题入队；AI 可用则当场生成草稿。

        人工点名的问题**不过问题样式过滤**（人的判断优先于启发式）；
        重复（已有 pending/approved 同题草稿）直接短路，不烧 LLM。
        """
        q = str(query or "").strip()[:200]
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

        material = {"source": "manual", "query": q, "count": max(1, int(min_miss_count))}
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
        with self._conn() as c:
            c.execute(
                "UPDATE kb_drafts SET dup_entry_id=?, dup_entry_title=?, dup_score=? WHERE id=?",
                (dup_eid, dup_etitle, dup_score, draft_id)
            )
        return dup

    def approve_draft(self, draft_id: str, operator: str = "") -> Optional[str]:
        """审核通过：将草稿入库为正式知识条目"""
        draft = self.get_draft(draft_id)
        if not draft or draft["status"] != "pending":
            return None

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
        })

        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as c:
            c.execute(
                "UPDATE kb_drafts SET status='approved', reviewed_by=?, reviewed_at=? WHERE id=?",
                (operator, now, draft_id)
            )

        _logger.info("草稿 %s 已审核通过，入库为条目 %s", draft_id, entry_id)
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

    def approve_all_pending(self, operator: str = "") -> int:
        """一键全部通过"""
        drafts = self.list_drafts(status="pending", limit=200)
        count = 0
        for d in drafts:
            if self.approve_draft(d["id"], operator):
                count += 1
        return count

    def batch_action(self, draft_ids: List[str], action: str, operator: str = "") -> Dict:
        """A2: 批量通过或拒绝指定草稿"""
        approved = 0
        rejected = 0
        failed = []
        for did in draft_ids:
            try:
                if action == "approve":
                    if self.approve_draft(did, operator):
                        approved += 1
                    else:
                        failed.append(did)
                elif action == "reject":
                    self.reject_draft(did, operator)
                    rejected += 1
            except Exception:
                failed.append(did)
        return {"approved": approved, "rejected": rejected, "failed": failed}

    def stats(self) -> Dict:
        with self._conn() as c:
            pending = c.execute("SELECT COUNT(*) FROM kb_drafts WHERE status='pending'").fetchone()[0]
            approved = c.execute("SELECT COUNT(*) FROM kb_drafts WHERE status='approved'").fetchone()[0]
            rejected = c.execute("SELECT COUNT(*) FROM kb_drafts WHERE status='rejected'").fetchone()[0]
            dup_flagged = c.execute(
                "SELECT COUNT(*) FROM kb_drafts WHERE status='pending' AND dup_score > 0"
            ).fetchone()[0]
        out = {"pending": pending, "approved": approved, "rejected": rejected,
               "dup_flagged": dup_flagged}
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

    # ── A3: Semantic duplicate detection ─────────────────────────────

    _DUP_BM25_THRESHOLD = 8.0
    _DUP_TRIGGER_THRESHOLD = 0.4

    def check_duplicate(self, draft: Dict) -> Optional[Dict]:
        """
        Multi-layer duplicate check against existing KB (no API calls).
        Layer 1: Trigger set overlap (Jaccard ≥ 0.4)
        Layer 2: BM25 text search score (≥ 8.0)
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

        # Layer 2: BM25 search
        try:
            result = self._kb.search(search_text, top_k=3)
            entries = result.get("entries", [])
            if entries:
                top = entries[0]
                bm25_score = top.get("_score", 0)
                if bm25_score >= self._DUP_BM25_THRESHOLD:
                    return {
                        "entry_id": top["id"],
                        "entry_title": top.get("title", ""),
                        "score": round(min(bm25_score / 15.0, 0.99), 2),
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
