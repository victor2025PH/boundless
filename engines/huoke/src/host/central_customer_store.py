# -*- coding: utf-8 -*-
"""L2 中央客户画像 store — PostgreSQL backend.

动机
----
200 设备 (10 worker × 20 phones) 跨机部署, 客户画像 + 聊天 + 漏斗事件
不能再各 worker 自己 SQLite. 主控部署 PostgreSQL, 各 worker 通过 HTTP
push 写入, 人工后台从主控统一查询.

部署
----
PG 16 已在主控 (192.168.0.118:5432) 跑, db=openclaw, user=openclaw_app.
schema 见 migrations/001_central_customer_schema.sql, 5 表:
- customers (master 客户记录, AI 画像)
- customer_events (业务事件 append-only)
- customer_chats (聊天历史 append-only, 含 AI 生成标记)
- customer_handoffs (人机交接状态机)
- _schema_version (schema 演进)

API
---
单进程内**单例**, 自动管 connection pool::

    from src.host.central_customer_store import get_store

    store = get_store()
    cust_id = store.upsert_customer(
        canonical_id="fb_uid_12345",
        canonical_source="facebook",
        primary_name="さとう たかひろ",
        ai_profile={"topics": ["food", "travel"], ...},
        worker_id="worker-175",
    )
    store.record_event(cust_id, "greeting_sent", worker_id="worker-175",
                       device_id="4HUSIB...", meta={"template_id": "jp_v3"})
    store.record_chat(cust_id, channel="messenger", direction="outgoing",
                      content="...", ai_generated=True)

线程安全: psycopg2 连接池 (ThreadedConnectionPool), 每次操作 borrow + return.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────────────────────────
DEFAULT_PG_HOST = os.environ.get("OPENCLAW_PG_HOST", "127.0.0.1")
DEFAULT_PG_PORT = int(os.environ.get("OPENCLAW_PG_PORT", "5432"))
DEFAULT_PG_DB = os.environ.get("OPENCLAW_PG_DB", "openclaw")
DEFAULT_PG_USER = os.environ.get("OPENCLAW_PG_USER", "openclaw_app")
DEFAULT_PG_PASSWORD = os.environ.get("OPENCLAW_PG_PASSWORD", "")

DEFAULT_POOL_MIN = 2
DEFAULT_POOL_MAX = 20  # 200 worker 并发写, max 20 连接够 (每连接复用)


class CentralCustomerStore:
    """中央客户画像 store, 包装 PG ThreadedConnectionPool."""

    def __init__(
        self,
        host: str = DEFAULT_PG_HOST,
        port: int = DEFAULT_PG_PORT,
        dbname: str = DEFAULT_PG_DB,
        user: str = DEFAULT_PG_USER,
        password: str = DEFAULT_PG_PASSWORD,
        pool_min: int = DEFAULT_POOL_MIN,
        pool_max: int = DEFAULT_POOL_MAX,
    ):
        # F9 fix (RUNBOOK) + P2.X-3 (2026-04-30):
        #   client_encoding=utf8         — force query/data wire to UTF-8
        #   options='-c lc_messages=C'   — connection-time GUC, force PG 错误
        #     消息走 C (ASCII) locale 而不是 zh_CN.GBK. 历史注释说 SUSET 改不
        #     了, 实测 PG 13+ 允许 connection-time '-c lc_messages=C' (与 SET
        #     不同). 即使 PG 拒该参数, psycopg2 也只会警告, 不会让 connect
        #     报错 — 比"得手动 ALTER ROLE"更可移植.
        # 2026-05-13 优化: 添加 client_encoding 参数到 psycopg2.connect，
        #   确保连接级别的编码设置生效
        self._dsn = (
            f"host={host} port={port} dbname={dbname} user={user} "
            f"password={password} application_name=openclaw_central "
            f"client_encoding=utf8 "
            f"options='-c lc_messages=C'"
        )
        self._connect_kwargs = {
            "client_encoding": "utf8",
            "connect_timeout": 10,
        }
        self._pool: Optional[ThreadedConnectionPool] = None
        self._pool_lock = threading.Lock()
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._last_reconnect: float = 0.0
        self._reconnect_throttle: float = 30.0  # 30s 内不重复重建
        self._connect()

    def _connect(self) -> None:
        try:
            self._pool = ThreadedConnectionPool(
                self._pool_min, self._pool_max, dsn=self._dsn,
                **self._connect_kwargs,
            )
            logger.info("[central_store] PG pool ready (min=%d, max=%d)",
                        self._pool_min, self._pool_max)
        except UnicodeDecodeError as exc:
            # UTF-8 解码错误：可能是数据库编码或系统locale问题
            logger.error("[central_store] PG pool init failed with UnicodeDecodeError: %s", exc)
            logger.error("[central_store] 请检查：1) 数据库编码是否为UTF-8 2) 系统locale设置 3) DSN中的client_encoding参数")
            raise
        except Exception as exc:
            logger.exception("[central_store] PG pool init failed: %s", exc)
            raise

    def close(self) -> None:
        if self._pool:
            self._pool.closeall()
            self._pool = None

    def _ensure_pool(self) -> bool:
        """Ensure connection pool is alive, rebuild if dead (throttled).

        死池场景: 所有连接均被 discard 后 pool 变成空, 下次 getconn 抛 PoolError.
        节流保护: 30s 内不重复重建，避免频繁连接风暴.
        """
        if self._pool:
            return True
        with self._pool_lock:
            if self._pool:
                return True
            now = time.time()
            if now - self._last_reconnect < self._reconnect_throttle:
                return False
            self._last_reconnect = now
            try:
                self._connect()
                logger.info("[central_store] PG pool 自动重建成功")
                return True
            except Exception as exc:
                logger.error("[central_store] PG pool 重建失败: %s", exc)
                return False

    @contextmanager
    def _conn(self) -> Iterator[psycopg2.extensions.connection]:
        if not self._ensure_pool():
            raise RuntimeError("central_store pool unavailable (reconnect throttled or failed)")
        c = self._pool.getconn()
        discard = False
        try:
            yield c
            c.commit()
        except (UnicodeDecodeError, psycopg2.OperationalError) as exc:
            # Phase-13: 这两类错误 (Chinese Windows GBK PG 消息 / 连接断开)
            # 表示 connection 已坏, 从 pool 里丢弃, 让下次拿干净的
            logger.warning("[central_store] connection poisoned (%s), discarding",
                           type(exc).__name__)
            try:
                c.rollback()
            except Exception:
                pass
            discard = True
            # 连接断开时主动重置 pool，让下次请求自动重建
            if isinstance(exc, psycopg2.OperationalError):
                try:
                    self._pool.putconn(c, close=True)
                except Exception:
                    pass
                with self._pool_lock:
                    try:
                        if self._pool:
                            self._pool.closeall()
                    except Exception:
                        pass
                    self._pool = None
                    logger.warning("[central_store] 连接断开, pool 已重置, 下次请求自动重建")
                return  # putconn 已处理
            raise
        except Exception:
            try:
                c.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                if self._pool:
                    self._pool.putconn(c, close=discard)
            except Exception:
                pass

    @contextmanager
    def _cursor(self) -> Iterator[psycopg2.extras.RealDictCursor]:
        with self._conn() as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                yield cur

    # ── customers upsert ─────────────────────────────────────────────
    def upsert_customer(
        self,
        canonical_id: str,
        canonical_source: str,
        primary_name: Optional[str] = None,
        age_band: Optional[str] = None,
        gender: Optional[str] = None,
        country: Optional[str] = None,
        interests: Optional[List[str]] = None,
        ai_profile: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
        worker_id: Optional[str] = None,
        device_id: Optional[str] = None,
        customer_id: Optional[str] = None,
    ) -> str:
        """upsert by (canonical_source, canonical_id), 返回 customer_id (UUID 字符串).

        已存在: 更新非 None 字段, 保留旧值; status 字段只在显式传入时覆盖.

        customer_id 给了就用 worker 端算的 UUIDv5; 没给主控用 gen_random_uuid().
        ON CONFLICT 路径 PK 不变, 总是返回首次写入时的 customer_id.
        """
        ai_profile = ai_profile or {}
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO customers (
                    customer_id,
                    canonical_id, canonical_source, primary_name, age_band,
                    gender, country, interests, ai_profile,
                    status, last_worker_id, last_device_id
                ) VALUES (
                    COALESCE(%s::uuid, gen_random_uuid()),
                    %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                    COALESCE(%s, 'in_funnel'), %s, %s
                )
                ON CONFLICT (canonical_source, canonical_id) DO UPDATE SET
                    primary_name = COALESCE(EXCLUDED.primary_name, customers.primary_name),
                    age_band = COALESCE(EXCLUDED.age_band, customers.age_band),
                    gender = COALESCE(EXCLUDED.gender, customers.gender),
                    country = COALESCE(EXCLUDED.country, customers.country),
                    interests = COALESCE(EXCLUDED.interests, customers.interests),
                    ai_profile = customers.ai_profile || EXCLUDED.ai_profile,
                    -- status 状态机: 终态/人工接管态不可由 worker push 降级,
                    -- in_line 不可回 messenger; 其它走单调升级 (in_funnel <
                    -- in_messenger < in_line) 或 EXCLUDED 覆盖.
                    status = CASE
                        WHEN customers.status IN ('accepted_by_human', 'converted', 'lost')
                            THEN customers.status
                        WHEN customers.status = 'in_line' AND EXCLUDED.status = 'in_messenger'
                            THEN customers.status
                        WHEN customers.status = 'in_messenger' AND EXCLUDED.status = 'in_funnel'
                            THEN customers.status
                        ELSE COALESCE(EXCLUDED.status, customers.status)
                    END,
                    last_worker_id = COALESCE(EXCLUDED.last_worker_id, customers.last_worker_id),
                    last_device_id = COALESCE(EXCLUDED.last_device_id, customers.last_device_id)
                RETURNING customer_id::text
                """,
                (
                    customer_id,
                    canonical_id, canonical_source, primary_name, age_band,
                    gender, country, interests, json.dumps(ai_profile),
                    status, worker_id, device_id,
                ),
            )
            row = cur.fetchone()
            return row["customer_id"]

    # ── events ──────────────────────────────────────────────────────
    def record_event(
        self,
        customer_id: str,
        event_type: str,
        worker_id: str,
        device_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        meta = meta or {}
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO customer_events (
                    customer_id, event_type, worker_id, device_id, meta
                ) VALUES (%s, %s, %s, %s, %s::jsonb)
                RETURNING event_id::text
                """,
                (customer_id, event_type, worker_id, device_id, json.dumps(meta)),
            )
            return cur.fetchone()["event_id"]

    # ── chats ───────────────────────────────────────────────────────
    def record_chat(
        self,
        customer_id: str,
        channel: str,
        direction: str,
        content: str,
        content_lang: Optional[str] = None,
        ai_generated: bool = False,
        template_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        device_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        if direction not in ("incoming", "outgoing"):
            raise ValueError(f"invalid direction: {direction}")
        if channel not in ("facebook", "messenger", "line", "telegram"):
            raise ValueError(f"invalid channel: {channel}")
        meta = meta or {}
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO customer_chats (
                    customer_id, channel, direction, content, content_lang,
                    ai_generated, template_id, worker_id, device_id, meta
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING chat_id::text
                """,
                (
                    customer_id, channel, direction, content, content_lang,
                    ai_generated, template_id, worker_id, device_id, json.dumps(meta),
                ),
            )
            return cur.fetchone()["chat_id"]

    # ── handoffs ────────────────────────────────────────────────────
    def initiate_handoff(
        self,
        customer_id: str,
        from_stage: str,
        to_stage: str,
        initiating_worker_id: str,
        initiating_device_id: Optional[str] = None,
        ai_summary: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        meta = meta or {}
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO customer_handoffs (
                    customer_id, from_stage, to_stage,
                    initiating_worker_id, initiating_device_id,
                    ai_summary, meta
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING handoff_id::text
                """,
                (
                    customer_id, from_stage, to_stage,
                    initiating_worker_id, initiating_device_id,
                    ai_summary, json.dumps(meta),
                ),
            )
            return cur.fetchone()["handoff_id"]

    def accept_handoff(
        self,
        handoff_id: str,
        accepted_by_human: str,
    ) -> bool:
        """人工接管. 已被接管返回 False (idempotent + 防重复)."""
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE customer_handoffs
                SET accepted_by_human = %s, accepted_at = NOW(),
                    outcome = 'accepted'
                WHERE handoff_id = %s AND accepted_by_human IS NULL
                RETURNING customer_id::text
                """,
                (accepted_by_human, handoff_id),
            )
            row = cur.fetchone()
            if not row:
                return False
            # 同步 customers.status='accepted_by_human' 让 L3 看板可见
            cur.execute(
                """
                UPDATE customers SET status = 'accepted_by_human'
                WHERE customer_id = %s
                  AND status NOT IN ('converted', 'lost', 'accepted_by_human')
                """,
                (row["customer_id"],),
            )
            return True

    def complete_handoff(
        self,
        handoff_id: str,
        outcome: str,
    ) -> bool:
        if outcome not in ("converted", "lost", "timeout"):
            raise ValueError(f"invalid outcome: {outcome}")
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE customer_handoffs
                SET completed_at = NOW(), outcome = %s
                WHERE handoff_id = %s AND completed_at IS NULL
                RETURNING customer_id::text
                """,
                (outcome, handoff_id),
            )
            row = cur.fetchone()
            if not row:
                return False
            customer_id = row["customer_id"]
            # 同步 customers.status 让 L3 看板一目了然.
            # 状态机守卫: 已 converted/lost 终态保持; in_line/accepted 升级到结果.
            cur.execute(
                """
                UPDATE customers SET status = %s
                WHERE customer_id = %s
                  AND status NOT IN ('converted', 'lost')
                """,
                (outcome, customer_id),
            )
            return True

    # ── 查询 ────────────────────────────────────────────────────────
    def list_customers(
        self,
        status: Optional[str] = None,
        country: Optional[str] = None,
        worker_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        where = []
        params = []
        if status:
            where.append("c.status = %s")
            params.append(status)
        if country:
            where.append("c.country = %s")
            params.append(country)
        if worker_id:
            where.append("c.last_worker_id = %s")
            params.append(worker_id)
        where_sql = " WHERE " + " AND ".join(where) if where else ""
        params.extend([limit, offset])

        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    c.customer_id::text, c.canonical_id, c.canonical_source,
                    c.primary_name, c.age_band, c.gender, c.country, c.interests,
                    c.ai_profile, c.status, c.priority_tag,
                    c.custom_tags,
                    c.last_worker_id, c.last_device_id,
                    c.created_at, c.updated_at,
                    -- Phase-5: 最近一条聊天预览 (任何 channel / direction)
                    (SELECT json_build_object('content', ch.content,
                                              'direction', ch.direction,
                                              'ts', ch.ts)
                       FROM customer_chats ch
                       WHERE ch.customer_id = c.customer_id
                       ORDER BY ch.ts DESC LIMIT 1) AS last_chat
                FROM customers c
                {where_sql}
                ORDER BY c.updated_at DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    # ── Phase-7: A/B 实验生命周期 ─────────────────────────────────────
    def get_running_experiment(self) -> Optional[Dict[str, Any]]:
        """返当前 running 的 A/B 实验, 没有返 None."""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT experiment_id::text, name, status, variants,
                       started_at, ended_at, winner, samples, note
                FROM ab_experiments
                WHERE status = 'running'
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def archive_experiment_with_winner(
        self,
        experiment_id: str,
        winner: str,
        samples: Dict[str, int],
    ) -> bool:
        """winner graduate 后归档实验. 自动启新实验."""
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE ab_experiments
                SET status = 'completed',
                    ended_at = NOW(),
                    winner = %s,
                    samples = %s::jsonb
                WHERE experiment_id = %s AND status = 'running'
                """,
                (winner, json.dumps(samples), experiment_id),
            )
            return cur.rowcount > 0

    def start_new_experiment(
        self,
        name: str,
        variants: List[str],
        note: str = "",
    ) -> str:
        """启新 A/B 实验. variants 是 list, 例 ['v1', 'v3']."""
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO ab_experiments (name, status, variants, note)
                VALUES (%s, 'running', %s, %s)
                RETURNING experiment_id::text
                """,
                (name, variants, note),
            )
            return cur.fetchone()["experiment_id"]

    def list_experiments(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT experiment_id::text, name, status, variants,
                       started_at, ended_at, winner, samples, note
                FROM ab_experiments
                ORDER BY started_at DESC LIMIT %s
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    # ── Phase-7: 客户视图保存 ─────────────────────────────────────────
    def save_view(self, name: str, owner: str, params: Dict[str, Any]) -> str:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO customer_views (name, owner_username, params_json)
                VALUES (%s, %s, %s::jsonb)
                RETURNING view_id::text
                """,
                (name, owner, json.dumps(params)),
            )
            return cur.fetchone()["view_id"]

    def list_views(self, owner: Optional[str] = None,
                    limit: int = 50) -> List[Dict[str, Any]]:
        with self._cursor() as cur:
            if owner:
                cur.execute(
                    """
                    SELECT view_id::text, name, owner_username,
                           params_json, created_at
                    FROM customer_views
                    WHERE owner_username = %s OR owner_username = 'admin'
                    ORDER BY created_at DESC LIMIT %s
                    """,
                    (owner, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT view_id::text, name, owner_username,
                           params_json, created_at
                    FROM customer_views
                    ORDER BY created_at DESC LIMIT %s
                    """,
                    (limit,),
                )
            return [dict(r) for r in cur.fetchall()]

    def delete_view(self, view_id: str, owner: Optional[str] = None) -> bool:
        with self._cursor() as cur:
            if owner:
                cur.execute(
                    "DELETE FROM customer_views WHERE view_id = %s AND owner_username = %s",
                    (view_id, owner),
                )
            else:
                cur.execute(
                    "DELETE FROM customer_views WHERE view_id = %s",
                    (view_id,),
                )
            return cur.rowcount > 0

    def search_chats(
        self,
        q: str = "",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Phase-6/7: 全文搜聊天内容.

        Phase-6 用 ILIKE 起步.
        Phase-7 加 pg_trgm GIN: 优先用 %% 操作符 + similarity() 排序
        (走 idx_chats_content_trgm); pg_trgm 没装时静默回退 ILIKE.
        """
        if not q or len(q) < 2:
            return []
        lim = min(max(1, int(limit)), 200)
        with self._cursor() as cur:
            try:
                cur.execute(
                    """
                    SELECT
                        ch.chat_id::text, ch.customer_id::text, ch.channel, ch.direction,
                        ch.content, ch.content_lang, ch.ts,
                        c.primary_name, c.status, c.priority_tag,
                        similarity(ch.content, %s) AS sim
                    FROM customer_chats ch
                    JOIN customers c ON c.customer_id = ch.customer_id
                    WHERE ch.content %% %s OR ch.content ILIKE %s
                    ORDER BY sim DESC NULLS LAST, ch.ts DESC
                    LIMIT %s
                    """,
                    (q, q, f"%{q}%", lim),
                )
                return [dict(r) for r in cur.fetchall()]
            except Exception:
                # pg_trgm 没装 / similarity() 不可用 — fallback 纯 ILIKE
                cur.execute(
                    """
                    SELECT
                        ch.chat_id::text, ch.customer_id::text, ch.channel, ch.direction,
                        ch.content, ch.content_lang, ch.ts,
                        c.primary_name, c.status, c.priority_tag
                    FROM customer_chats ch
                    JOIN customers c ON c.customer_id = ch.customer_id
                    WHERE ch.content ILIKE %s
                    ORDER BY ch.ts DESC
                    LIMIT %s
                    """,
                    (f"%{q}%", lim),
                )
                return [dict(r) for r in cur.fetchall()]

    def search_customers(
        self,
        q: str = "",
        priority: str = "",
        status: str = "",
        ab_variant: str = "",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Phase-5: 多维过滤 + 模糊搜索客户.

        q: 模糊匹配 primary_name / canonical_id (case-insensitive)
        priority: high|medium|low
        status: in_funnel|in_messenger|in_line|...
        ab_variant: v1|v2
        """
        where = []
        params: List[Any] = []
        if q:
            where.append("(primary_name ILIKE %s OR canonical_id ILIKE %s)")
            qpat = f"%{q}%"
            params.extend([qpat, qpat])
        if priority and priority in ("high", "medium", "low"):
            where.append("priority_tag = %s")
            params.append(priority)
        if status:
            where.append("status = %s")
            params.append(status)
        if ab_variant:
            where.append("ai_profile->>'ab_variant' = %s")
            params.append(ab_variant)
        where_sql = " WHERE " + " AND ".join(where) if where else ""
        params.append(min(max(1, int(limit)), 200))

        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT customer_id::text, primary_name, status, priority_tag,
                       ai_profile, country, last_worker_id, last_device_id,
                       updated_at
                FROM customers
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    def get_customer(
        self,
        customer_id: str,
        include_events: int = 50,
        include_chats: int = 100,
    ) -> Optional[Dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT customer_id::text, canonical_id, canonical_source, "
                "primary_name, age_band, gender, country, interests, "
                "ai_profile, status, last_worker_id, last_device_id, "
                "created_at, updated_at "
                "FROM customers WHERE customer_id = %s",
                (customer_id,),
            )
            cust = cur.fetchone()
            if not cust:
                return None
            cust = dict(cust)

            cur.execute(
                "SELECT event_id::text, event_type, worker_id, device_id, meta, ts "
                "FROM customer_events WHERE customer_id = %s "
                "ORDER BY ts DESC LIMIT %s",
                (customer_id, include_events),
            )
            cust["events"] = [dict(r) for r in cur.fetchall()]

            cur.execute(
                "SELECT chat_id::text, channel, direction, content, content_lang, "
                "ai_generated, template_id, worker_id, ts "
                "FROM customer_chats WHERE customer_id = %s "
                "ORDER BY ts DESC LIMIT %s",
                (customer_id, include_chats),
            )
            cust["chats"] = [dict(r) for r in cur.fetchall()]

            cur.execute(
                "SELECT handoff_id::text, from_stage, to_stage, "
                "initiated_at, accepted_by_human, accepted_at, completed_at, "
                "outcome, ai_summary "
                "FROM customer_handoffs WHERE customer_id = %s "
                "ORDER BY initiated_at DESC",
                (customer_id,),
            )
            cust["handoffs"] = [dict(r) for r in cur.fetchall()]

            return cust

    def compute_ab_winner(
        self,
        days: int = 30,
        min_samples_per_variant: int = 10,
        min_rate_diff: float = 0.15,
        graduate_min_days: int = 7,
        graduate_min_samples: int = 30,
    ) -> Dict[str, Any]:
        """Phase-5: 启发式判断 A/B winner. 不引 scipy.
        Phase-6: 加 graduate 判定 — winner 持续 graduate_min_days 天 +
        samples ≥ graduate_min_samples/variant 后 graduated=True 100% 流量.

        判定 winner: 每个 variant 至少 min_samples_per_variant 个 outcome,
        且 conversion_rate 差 ≥ min_rate_diff → 标 winner.

        判定 graduate: 看最近 graduate_min_days 天每天的 winner 是否一致,
        且累计 samples ≥ graduate_min_samples/variant.

        返回 {winner, confidence, samples, rates, graduated, graduate_reason}.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(c.ai_profile->>'ab_variant', '') AS variant,
                    SUM(CASE WHEN h.outcome='converted' THEN 1 ELSE 0 END) AS converted_n,
                    SUM(CASE WHEN h.outcome IN ('converted', 'lost') THEN 1 ELSE 0 END) AS finished_n
                FROM customer_handoffs h
                JOIN customers c ON c.customer_id = h.customer_id
                WHERE h.completed_at IS NOT NULL
                  AND h.completed_at > NOW() - (INTERVAL '1 day' * %s)
                  AND COALESCE(c.ai_profile->>'ab_variant', '') IN ('v1', 'v2')
                GROUP BY variant
                """,
                (max(1, min(int(days), 365)),),
            )
            rows = {r["variant"]: dict(r) for r in cur.fetchall()}

        v1 = rows.get("v1") or {"converted_n": 0, "finished_n": 0}
        v2 = rows.get("v2") or {"converted_n": 0, "finished_n": 0}
        f1 = int(v1.get("finished_n") or 0)
        f2 = int(v2.get("finished_n") or 0)
        c1 = int(v1.get("converted_n") or 0)
        c2 = int(v2.get("converted_n") or 0)
        rate1 = (c1 / f1) if f1 else 0.0
        rate2 = (c2 / f2) if f2 else 0.0

        winner = None
        confidence = 0.0
        if f1 >= min_samples_per_variant and f2 >= min_samples_per_variant:
            diff = abs(rate1 - rate2)
            if diff >= min_rate_diff:
                winner = "v1" if rate1 > rate2 else "v2"
                confidence = min(1.0, diff / 0.5)

        # Phase-6: graduate 判定 — winner 稳定 + 样本够多
        graduated = False
        graduate_reason = ""
        if winner and f1 >= graduate_min_samples and f2 >= graduate_min_samples:
            # 看最近 graduate_min_days 天每天的 winner 是否一致
            with self._cursor() as cur:
                cur.execute(
                    """
                    WITH daily AS (
                        SELECT
                            DATE(h.completed_at) AS day,
                            COALESCE(c.ai_profile->>'ab_variant', '') AS variant,
                            SUM(CASE WHEN h.outcome='converted' THEN 1 ELSE 0 END)::float AS conv,
                            SUM(CASE WHEN h.outcome IN ('converted', 'lost') THEN 1 ELSE 0 END)::float AS fin
                        FROM customer_handoffs h
                        JOIN customers c ON c.customer_id = h.customer_id
                        WHERE h.completed_at IS NOT NULL
                          AND h.completed_at > NOW() - (INTERVAL '1 day' * %s)
                          AND COALESCE(c.ai_profile->>'ab_variant', '') IN ('v1', 'v2')
                        GROUP BY day, variant
                    )
                    SELECT day, variant, conv, fin FROM daily
                    WHERE fin > 0
                    ORDER BY day DESC, variant
                    """,
                    (graduate_min_days,),
                )
                daily_rows = list(cur.fetchall())
            # 每天计算 winner, 看是否一致
            from collections import defaultdict
            day_buckets = defaultdict(dict)
            for r in daily_rows:
                day_buckets[str(r["day"])][r["variant"]] = (r["conv"], r["fin"])
            consistent = True
            for day, vmap in day_buckets.items():
                v1f = vmap.get("v1", (0, 0))
                v2f = vmap.get("v2", (0, 0))
                if v1f[1] == 0 or v2f[1] == 0:
                    continue  # 某天该 variant 无样本, 跳过
                r1 = v1f[0] / v1f[1]
                r2 = v2f[0] / v2f[1]
                day_winner = "v1" if r1 > r2 else "v2" if r2 > r1 else None
                if day_winner is not None and day_winner != winner:
                    consistent = False
                    break
            if consistent and len(day_buckets) >= 2:
                graduated = True
                graduate_reason = f"winner '{winner}' 稳定超过 {len(day_buckets)} 天"

        return {
            "winner": winner,
            "confidence": round(confidence, 2),
            "samples": {"v1": f1, "v2": f2},
            "rates": {"v1": round(rate1, 3), "v2": round(rate2, 3)},
            "min_samples_required": min_samples_per_variant,
            "min_rate_diff_required": min_rate_diff,
            "graduated": graduated,
            "graduate_reason": graduate_reason,
            "graduate_min_days": graduate_min_days,
            "graduate_min_samples": graduate_min_samples,
        }

    def variant_sla_stats(self, days: int = 30) -> List[Dict[str, Any]]:
        """Phase-4: 按 ab_variant (v1/v2) 切片转化率, 给主管做 A/B 决策.

        返回 list of {variant, handled, converted, lost, pending,
                      conversion_rate, avg_minutes}.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(c.ai_profile->>'ab_variant', '(none)') AS variant,
                    COUNT(h.handoff_id) AS handled,
                    SUM(CASE WHEN h.outcome='converted' THEN 1 ELSE 0 END) AS converted_n,
                    SUM(CASE WHEN h.outcome='lost' THEN 1 ELSE 0 END) AS lost_n,
                    SUM(CASE WHEN h.completed_at IS NULL THEN 1 ELSE 0 END) AS pending_n,
                    AVG(EXTRACT(EPOCH FROM (h.completed_at - h.accepted_at))/60.0)
                        FILTER (WHERE h.completed_at IS NOT NULL AND h.accepted_at IS NOT NULL)
                        AS avg_minutes
                FROM customer_handoffs h
                JOIN customers c ON c.customer_id = h.customer_id
                WHERE h.initiated_at > NOW() - (INTERVAL '1 day' * %s)
                GROUP BY variant
                ORDER BY variant
                """,
                (max(1, min(int(days), 365)),),
            )
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                handled = int(d.get("handled") or 0)
                conv = int(d.get("converted_n") or 0)
                d["conversion_rate"] = (conv / handled) if handled else 0.0
                d["avg_minutes"] = float(d["avg_minutes"]) if d.get("avg_minutes") is not None else None
                rows.append(d)
            return rows

    def funnel_timeseries(self, days: int = 30) -> List[Dict[str, Any]]:
        """Phase-3: 历史漏斗时序 — 按天统计关键事件.

        返回 list of {date, friend_request_sent, greeting_sent, message_received,
                      wa_referral_sent, customer_converted, customer_lost}.
        """
        with self._cursor() as cur:
            cur.execute("""
                SELECT
                    DATE(ts) AS day,
                    SUM(CASE WHEN event_type='friend_request_sent' THEN 1 ELSE 0 END) AS friend_request_sent,
                    SUM(CASE WHEN event_type='greeting_sent' THEN 1 ELSE 0 END) AS greeting_sent,
                    SUM(CASE WHEN event_type='message_received' THEN 1 ELSE 0 END) AS message_received,
                    SUM(CASE WHEN event_type='wa_referral_sent' THEN 1 ELSE 0 END) AS wa_referral_sent
                FROM customer_events
                WHERE ts > NOW() - (INTERVAL '1 day' * %s)
                GROUP BY day
                ORDER BY day
            """, (max(1, min(int(days), 365)),))
            events_by_day = {str(r["day"]): dict(r) for r in cur.fetchall()}

            # converted/lost 来自 customer_handoffs.outcome
            cur.execute("""
                SELECT
                    DATE(completed_at) AS day,
                    SUM(CASE WHEN outcome='converted' THEN 1 ELSE 0 END) AS customer_converted,
                    SUM(CASE WHEN outcome='lost' THEN 1 ELSE 0 END) AS customer_lost
                FROM customer_handoffs
                WHERE completed_at IS NOT NULL
                  AND completed_at > NOW() - (INTERVAL '1 day' * %s)
                GROUP BY day
                ORDER BY day
            """, (max(1, min(int(days), 365)),))
            outcomes_by_day = {str(r["day"]): dict(r) for r in cur.fetchall()}

            # 合并 + 填零
            from datetime import datetime, timedelta
            all_days: List[Dict[str, Any]] = []
            today = datetime.utcnow().date()
            for i in range(int(days), -1, -1):
                d = today - timedelta(days=i)
                ds = d.isoformat()
                ev = events_by_day.get(ds, {})
                oc = outcomes_by_day.get(ds, {})
                all_days.append({
                    "date": ds,
                    "friend_request_sent": int(ev.get("friend_request_sent") or 0),
                    "greeting_sent": int(ev.get("greeting_sent") or 0),
                    "message_received": int(ev.get("message_received") or 0),
                    "wa_referral_sent": int(ev.get("wa_referral_sent") or 0),
                    "customer_converted": int(oc.get("customer_converted") or 0),
                    "customer_lost": int(oc.get("customer_lost") or 0),
                })
            return all_days

    def add_custom_tag(self, customer_id: str, tag: str) -> bool:
        """Phase-6: 加自定义标签 (TEXT[] PG 数组)."""
        tag = (tag or "").strip()
        if not tag or len(tag) > 50:
            return False
        with self._cursor() as cur:
            # array_append + array_distinct 防重 (PG 没原生 distinct, 用 array_remove + append)
            cur.execute(
                """
                UPDATE customers
                SET custom_tags = array_append(
                    array_remove(COALESCE(custom_tags, '{}'), %s),
                    %s
                )
                WHERE customer_id = %s
                """,
                (tag, tag, customer_id),
            )
            return cur.rowcount > 0

    def remove_custom_tag(self, customer_id: str, tag: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE customers
                SET custom_tags = array_remove(COALESCE(custom_tags, '{}'), %s)
                WHERE customer_id = %s
                """,
                (tag, customer_id),
            )
            return cur.rowcount > 0

    def update_priority(self, customer_id: str, priority_tag: str) -> bool:
        """Phase-4: 实时更新单个客户的 priority_tag.

        priority_tag in {high, medium, low}. 立即生效, 不等 recompute.
        """
        if priority_tag not in ("high", "medium", "low"):
            return False
        with self._cursor() as cur:
            cur.execute(
                "UPDATE customers SET priority_tag = %s WHERE customer_id = %s",
                (priority_tag, customer_id),
            )
            return cur.rowcount > 0

    def recompute_priority_tags(self) -> Dict[str, int]:
        """Phase-3: 客户分群 — 启发式 SQL 一次性重算所有 customers.priority_tag.

        规则:
            high: status in ('in_line', 'accepted_by_human', 'converted')
                  或 ai_profile->>'has_trigger_keyword' = 'true'
            low: status = 'lost' 或 status = 'in_funnel' 且 7 天没新事件
            medium: 默认

        返回 {high, medium, low, lost} 客户数.
        """
        with self._cursor() as cur:
            cur.execute("""
                UPDATE customers SET priority_tag = CASE
                    WHEN status IN ('converted', 'accepted_by_human', 'in_line') THEN 'high'
                    WHEN status = 'lost' THEN 'low'
                    WHEN status = 'in_funnel' AND updated_at < NOW() - INTERVAL '7 days' THEN 'low'
                    ELSE 'medium'
                END
            """)
            cur.execute("SELECT priority_tag, COUNT(*) AS n FROM customers GROUP BY priority_tag")
            return {r["priority_tag"]: int(r["n"]) for r in cur.fetchall()}

    def agent_sla_stats(self, days: int = 30) -> List[Dict[str, Any]]:
        """主管 SLA 看板: 按客服 username 统计接管 / 转化 / 平均处理时长.

        返回 list of dict, 每项含:
            - accepted_by_human (username)
            - handled (接管总数)
            - converted / lost / pending_count
            - conversion_rate (0-1)
            - avg_assign_to_complete_minutes (None if no completed)
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT
                    accepted_by_human,
                    COUNT(*) AS handled,
                    SUM(CASE WHEN outcome='converted' THEN 1 ELSE 0 END) AS converted_n,
                    SUM(CASE WHEN outcome='lost' THEN 1 ELSE 0 END) AS lost_n,
                    SUM(CASE WHEN completed_at IS NULL THEN 1 ELSE 0 END) AS pending_n,
                    AVG(EXTRACT(EPOCH FROM (completed_at - accepted_at))/60.0)
                        FILTER (WHERE completed_at IS NOT NULL AND accepted_at IS NOT NULL)
                        AS avg_minutes,
                    -- Phase-12: 接管响应时间 (initiated → accepted)
                    AVG(EXTRACT(EPOCH FROM (accepted_at - initiated_at))/60.0)
                        FILTER (WHERE accepted_at IS NOT NULL)
                        AS avg_accept_minutes,
                    COUNT(*) FILTER (WHERE accepted_at IS NOT NULL) AS accept_n,
                    -- Phase-12: 接管 → 转化时间 (accepted → completed WHERE outcome=converted)
                    AVG(EXTRACT(EPOCH FROM (completed_at - accepted_at))/60.0)
                        FILTER (WHERE outcome='converted' AND accepted_at IS NOT NULL)
                        AS avg_convert_minutes,
                    COUNT(*) FILTER (WHERE outcome='converted' AND accepted_at IS NOT NULL)
                        AS convert_n_with_time
                FROM customer_handoffs
                WHERE accepted_by_human IS NOT NULL
                  AND accepted_at > NOW() - (INTERVAL '1 day' * %s)
                GROUP BY accepted_by_human
                ORDER BY handled DESC
                """,
                (max(1, min(int(days), 365)),),
            )
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                handled = int(d.get("handled") or 0)
                conv = int(d.get("converted_n") or 0)
                d["conversion_rate"] = (conv / handled) if handled else 0.0
                d["avg_minutes"] = float(d["avg_minutes"]) if d.get("avg_minutes") is not None else None
                d["avg_accept_minutes"] = float(d["avg_accept_minutes"]) if d.get("avg_accept_minutes") is not None else None
                d["avg_convert_minutes"] = float(d["avg_convert_minutes"]) if d.get("avg_convert_minutes") is not None else None
                d["accept_n"] = int(d.get("accept_n") or 0)
                d["convert_n_with_time"] = int(d.get("convert_n_with_time") or 0)
                rows.append(d)
            return rows

    def referral_decisions_aggregate(self, days: int = 30) -> Dict[str, Any]:
        """Phase-11: referral_decision 事件聚合 (admin 调 gate 阈值用).

        - by_level: hard_block / hard_allow / soft_pass / soft_fail 计数
        - by_refer: refer=true 与 false 计数 + refer_rate
        - top_reasons: reasons[] flatten 出 top 10 (count desc)
        - avg_scores_by_refer: refer=true vs false 各自的 readiness/frust/emo 均值
        - timeseries: 每天 total + refer_n + refer_rate
        """
        days = max(1, min(int(days), 365))
        with self._cursor() as cur:
            # 总 + by_level + by_refer
            cur.execute("""
                SELECT
                    meta->>'level' AS level,
                    (meta->>'refer')::boolean AS refer,
                    COUNT(*) AS cnt
                FROM customer_events
                WHERE event_type = 'referral_decision'
                  AND ts > NOW() - (INTERVAL '1 day' * %s)
                GROUP BY level, refer
            """, (days,))
            rows = cur.fetchall()
            by_level: Dict[str, int] = {}
            by_refer = {"true": 0, "false": 0}
            total = 0
            for r in rows:
                lv = r["level"] or "unknown"
                cnt = int(r["cnt"])
                by_level[lv] = by_level.get(lv, 0) + cnt
                if r["refer"] is True:
                    by_refer["true"] += cnt
                else:
                    by_refer["false"] += cnt
                total += cnt

            # 平均分 by refer
            cur.execute("""
                SELECT
                    (meta->>'refer')::boolean AS refer,
                    AVG(NULLIF((meta->>'readiness')::float, NULL)) AS avg_readiness,
                    AVG(NULLIF((meta->>'frustration')::float, NULL)) AS avg_frust,
                    AVG(NULLIF((meta->>'emotion_overall')::float, NULL)) AS avg_emo
                FROM customer_events
                WHERE event_type = 'referral_decision'
                  AND ts > NOW() - (INTERVAL '1 day' * %s)
                GROUP BY refer
            """, (days,))
            avg_by_refer: Dict[str, Dict[str, float]] = {}
            for r in cur.fetchall():
                k = "true" if r["refer"] else "false"
                avg_by_refer[k] = {
                    "readiness": float(r["avg_readiness"]) if r["avg_readiness"] is not None else None,
                    "frustration": float(r["avg_frust"]) if r["avg_frust"] is not None else None,
                    "emotion_overall": float(r["avg_emo"]) if r["avg_emo"] is not None else None,
                }

            # top reasons (jsonb_array_elements_text 把数组 unnest)
            cur.execute("""
                SELECT reason, COUNT(*) AS cnt FROM (
                    SELECT jsonb_array_elements_text(meta->'reasons') AS reason
                    FROM customer_events
                    WHERE event_type = 'referral_decision'
                      AND ts > NOW() - (INTERVAL '1 day' * %s)
                ) sub
                GROUP BY reason
                ORDER BY cnt DESC
                LIMIT 10
            """, (days,))
            top_reasons = [
                {"reason": r["reason"], "count": int(r["cnt"])}
                for r in cur.fetchall()
            ]

            # 时序 daily refer rate
            cur.execute("""
                SELECT
                    DATE(ts) AS day,
                    COUNT(*) AS total_n,
                    SUM(CASE WHEN (meta->>'refer')::boolean THEN 1 ELSE 0 END) AS refer_n
                FROM customer_events
                WHERE event_type = 'referral_decision'
                  AND ts > NOW() - (INTERVAL '1 day' * %s)
                GROUP BY day
                ORDER BY day
            """, (days,))
            timeseries: List[Dict[str, Any]] = []
            for r in cur.fetchall():
                tot = int(r["total_n"])
                ref = int(r["refer_n"] or 0)
                timeseries.append({
                    "date": str(r["day"]),
                    "total": tot,
                    "refer_n": ref,
                    "refer_rate": (ref / tot) if tot else 0.0,
                })

            refer_rate = (by_refer["true"] / total) if total else 0.0
            return {
                "days": days,
                "total": total,
                "by_level": by_level,
                "by_refer": by_refer,
                "refer_rate": refer_rate,
                "top_reasons": top_reasons,
                "avg_scores_by_refer": avg_by_refer,
                "timeseries": timeseries,
            }

    def list_referral_decisions(
        self,
        level: str = "",
        refer: Optional[bool] = None,
        reason: str = "",
        days: int = 30,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Phase-12: referral_decision 事件详情查询 (admin 钻取调研用).

        过滤维度: level / refer / reason 子串. JOIN customers 出 primary_name.
        """
        days = max(1, min(int(days), 365))
        limit = min(max(1, int(limit)), 500)
        where = ["e.event_type = 'referral_decision'",
                 "e.ts > NOW() - (INTERVAL '1 day' * %s)"]
        params: List[Any] = [days]
        if level:
            where.append("e.meta->>'level' = %s")
            params.append(level)
        if refer is True:
            where.append("(e.meta->>'refer')::boolean = TRUE")
        elif refer is False:
            where.append("(e.meta->>'refer')::boolean = FALSE")
        if reason:
            where.append("EXISTS (SELECT 1 FROM jsonb_array_elements_text(e.meta->'reasons') r WHERE r ILIKE %s)")
            params.append(f"%{reason}%")
        where_sql = " AND ".join(where)
        params.append(limit)
        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    e.customer_id::text, e.ts, e.meta,
                    c.primary_name, c.status, c.priority_tag
                FROM customer_events e
                JOIN customers c ON c.customer_id = e.customer_id
                WHERE {where_sql}
                ORDER BY e.ts DESC
                LIMIT %s
                """,
                tuple(params),
            )
            return [dict(r) for r in cur.fetchall()]

    def list_top_high_priority(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Phase-10: 高 priority 客户 Top N (运营主动出击).

        优先 status 还在 funnel/messenger/line (未接管未转化 = 真正可操作),
        最近活跃排前.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT
                    customer_id::text, primary_name, status, priority_tag,
                    last_worker_id, last_device_id, updated_at,
                    ai_profile, country, custom_tags
                FROM customers
                WHERE priority_tag = 'high'
                  AND status IN ('in_funnel', 'in_messenger', 'in_line')
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (min(max(1, int(limit)), 50),),
            )
            return [dict(r) for r in cur.fetchall()]

    def list_top_frustrated(self, days: int = 7,
                             limit: int = 10) -> List[Dict[str, Any]]:
        """Phase-10: 高 frustration 客户 Top N (运营主动安抚).

        从 customer_events.priority_changed 拉 reason='frustration_high',
        近 N 天 distinct customer 按最近一次事件时间排.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (e.customer_id)
                    e.customer_id::text, e.ts AS event_ts, e.meta,
                    c.primary_name, c.status, c.priority_tag,
                    c.last_worker_id, c.country
                FROM customer_events e
                JOIN customers c ON c.customer_id = e.customer_id
                WHERE e.event_type = 'priority_changed'
                  AND e.meta->>'reason' = 'frustration_high'
                  AND e.ts > NOW() - (INTERVAL '1 day' * %s)
                ORDER BY e.customer_id, e.ts DESC
                """,
                (max(1, min(int(days), 90)),),
            )
            rows = [dict(r) for r in cur.fetchall()]
            rows.sort(key=lambda r: r["event_ts"], reverse=True)
            return rows[: min(max(1, int(limit)), 50)]

    def list_pending_handoffs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """L3 人工接管面板: 列出待接管 (accepted_by_human IS NULL)."""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT
                    h.handoff_id::text, h.customer_id::text, h.from_stage,
                    h.to_stage, h.initiated_at, h.ai_summary,
                    h.initiating_worker_id, h.initiating_device_id,
                    c.canonical_source, c.primary_name, c.age_band, c.gender,
                    c.country, c.interests, c.ai_profile, c.priority_tag
                FROM customer_handoffs h
                JOIN customers c ON c.customer_id = h.customer_id
                WHERE h.accepted_by_human IS NULL
                ORDER BY h.initiated_at ASC
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def funnel_stats(self, days: int = 7) -> Dict[str, int]:
        """L3 dashboard 漏斗统计."""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT status, COUNT(*) as cnt
                FROM customers
                WHERE updated_at > NOW() - INTERVAL '%s days'
                GROUP BY status
                """,
                (days,),
            )
            stats = {r["status"]: int(r["cnt"]) for r in cur.fetchall()}

            cur.execute(
                """
                SELECT event_type, COUNT(*) as cnt
                FROM customer_events
                WHERE ts > NOW() - INTERVAL '%s days'
                GROUP BY event_type
                """,
                (days,),
            )
            events = {r["event_type"]: int(r["cnt"]) for r in cur.fetchall()}

            return {"customers_by_status": stats, "events_by_type": events}


# ── 单例 ──────────────────────────────────────────────────────────────
_store_singleton: Optional[CentralCustomerStore] = None
_singleton_lock = threading.Lock()

# P2.X-3 (2026-04-30) circuit breaker:
# 真实 incident: PG 在 zh_CN.GBK locale 下返认证错误消息 → psycopg2 解码崩 →
# init 抛 UnicodeDecodeError. 历史无熔断, 4 设备并发 callback 每秒数十次
# 重试 init+reset+retry → CPU 烧爆 + 日志 152KB/2min → host 进程~3min 内
# 不响应被外层视作 down.
#
# 状态机:
#   CLOSED   (默认) — 每次调用都正常 attempt
#   失败计数累计到 _BREAKER_THRESHOLD → OPEN
#   OPEN     — 后续 _BREAKER_OPEN_SEC 秒内全部短路, raise 缓存异常,
#              不再 attempt PG. 到时间后 → HALF_OPEN
#   HALF_OPEN — 放行 1 次试探性 init. 成功 → CLOSED. 失败 → 重回 OPEN.
_BREAKER_THRESHOLD = 3       # 连续 3 次失败进熔断
_BREAKER_OPEN_SEC = 60.0     # 熔断保持 60s 不再重试
_init_breaker_state: Dict[str, Any] = {
    "consecutive_failures": 0,
    "last_failure_at": 0.0,    # monotonic time of last failure
    "last_exception": None,    # 缓存最近一次 init exception 用于短路 raise
    "open_until": 0.0,         # monotonic time, < now 表示可再试
}


def _breaker_should_short_circuit() -> Optional[BaseException]:
    """若熔断 OPEN 且未到超时, 返回缓存的 exception (调用方应直接 raise);
    否则返回 None (放行 attempt).
    """
    now = time.monotonic()
    if _init_breaker_state["open_until"] > now:
        return _init_breaker_state.get("last_exception")
    return None


def _breaker_record_failure(exc: BaseException) -> None:
    s = _init_breaker_state
    s["consecutive_failures"] = int(s.get("consecutive_failures") or 0) + 1
    s["last_failure_at"] = time.monotonic()
    s["last_exception"] = exc
    if s["consecutive_failures"] >= _BREAKER_THRESHOLD:
        s["open_until"] = time.monotonic() + _BREAKER_OPEN_SEC
        logger.error(
            "[central_store] circuit breaker OPEN for %.0fs after %d "
            "consecutive init failures (last=%s: %s). 后续调用将短路 "
            "避免 PG 不可达时烧 CPU. 修复 PG 连接后 %.0fs 内会自动放行试探.",
            _BREAKER_OPEN_SEC, s["consecutive_failures"],
            type(exc).__name__, str(exc)[:120],
            _BREAKER_OPEN_SEC,
        )


def _breaker_record_success() -> None:
    _init_breaker_state["consecutive_failures"] = 0
    _init_breaker_state["last_exception"] = None
    _init_breaker_state["open_until"] = 0.0


def get_store() -> CentralCustomerStore:
    """获取/初始化单例 (含 circuit breaker, P2.X-3)."""
    global _store_singleton
    if _store_singleton is not None:
        return _store_singleton

    # 短路检查: 在拿锁前先看, 减少锁竞争
    cached_exc = _breaker_should_short_circuit()
    if cached_exc is not None:
        raise cached_exc

    with _singleton_lock:
        if _store_singleton is not None:
            return _store_singleton
        # 双重检查: 拿到锁后再看一次熔断状态 (其他线程可能刚刚触发熔断)
        cached_exc = _breaker_should_short_circuit()
        if cached_exc is not None:
            raise cached_exc
        try:
            _store_singleton = CentralCustomerStore()
        except BaseException as exc:
            _breaker_record_failure(exc)
            raise
        else:
            _breaker_record_success()
            return _store_singleton


def reset_for_tests(**kwargs) -> CentralCustomerStore:
    """仅测试用: 重置单例."""
    global _store_singleton
    with _singleton_lock:
        if _store_singleton is not None:
            _store_singleton.close()
        _store_singleton = CentralCustomerStore(**kwargs)
        return _store_singleton


def reset_store() -> None:
    """Phase-13: 清掉 broken pool 让下次 get_store 重建.

    用于 _safe_get_store 失败自动恢复 (常见原因: PG 返 GBK 编码错误消息
    导致 psycopg2 UnicodeDecodeError, pool connection 进入坏状态).
    """
    global _store_singleton
    with _singleton_lock:
        if _store_singleton is not None:
            try:
                _store_singleton.close()
            except Exception:
                pass
            _store_singleton = None
            logger.warning("[central_store] singleton reset, will reconnect on next call")
