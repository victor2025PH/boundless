# -*- coding: utf-8 -*-
"""
Content Studio 数据库模块。

表结构:
- studio_jobs: 内容生成任务（一个job对应一次"为某persona生成内容"的操作）
- studio_content: 每个平台的具体内容（脚本/文案/视频路径/状态）
- studio_posts: 已发布帖子的追踪记录（发布时间/平台帖子ID/互动数据）

设计原则:
- WAL模式，高并发读写安全
- 所有时间用 UTC ISO格式
- job_id/content_id 用 UUID
"""

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.host.device_registry import data_file

logger = logging.getLogger(__name__)

# 数据库文件路径：项目根/data/studio/studio.db
_DB_PATH: Path = data_file("studio/studio.db")


def get_studio_db() -> str:
    """返回 studio 数据库的绝对路径（单例模式）。"""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return str(_DB_PATH)


@contextmanager
def _get_conn():
    """
    数据库连接上下文管理器。
    - 启用 WAL 模式保证高并发读写安全
    - row_factory = sqlite3.Row 支持字典风格访问
    - 自动提交/回滚，异常时自动关闭连接
    """
    conn = sqlite3.connect(get_studio_db(), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now_utc() -> str:
    """返回当前 UTC 时间的 ISO 格式字符串。"""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 建表
# ---------------------------------------------------------------------------

def init_studio_db() -> None:
    """初始化所有 Content Studio 数据表，幂等（CREATE IF NOT EXISTS）。"""
    with _get_conn() as conn:
        conn.executescript("""
            -- 内容生成任务主表
            CREATE TABLE IF NOT EXISTS studio_jobs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id      TEXT    NOT NULL UNIQUE,          -- UUID
                persona_id  TEXT    NOT NULL,                 -- 关联的 persona
                status      TEXT    NOT NULL DEFAULT 'pending',
                    -- pending / generating / ready / publishing / published / failed
                mode        TEXT    NOT NULL DEFAULT 'full_auto',
                    -- full_auto: 全自动发布  semi_auto: 需人工审核
                platforms   TEXT    NOT NULL DEFAULT '[]',    -- JSON 数组，如 ["tiktok","instagram"]
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            );

            -- 每个平台的具体内容记录
            CREATE TABLE IF NOT EXISTS studio_content (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id           TEXT    NOT NULL REFERENCES studio_jobs(job_id),
                platform         TEXT    NOT NULL,            -- tiktok / instagram / youtube 等
                content_type     TEXT    NOT NULL DEFAULT 'video',
                    -- video / slideshow / text
                style            TEXT,                        -- 风格标签，如 "emotional" / "funny"
                script           TEXT,                        -- 完整视频脚本
                caption          TEXT,                        -- 帖子文案
                hashtags         TEXT    DEFAULT '[]',        -- JSON 数组
                visual_prompts   TEXT    DEFAULT '[]',        -- JSON 数组，AI 绘图提示词
                voiceover_text   TEXT,                        -- TTS 朗读文本
                video_path       TEXT,                        -- 原始视频文件路径
                image_paths      TEXT    DEFAULT '[]',        -- JSON 数组，图片路径（slideshow用）
                audio_path       TEXT,                        -- 背景音乐/配音路径
                final_video_path TEXT,                        -- 合成后的最终视频路径
                approved         INTEGER NOT NULL DEFAULT 0,  -- 0=未审核 1=已批准（semi_auto专用）
                status           TEXT    NOT NULL DEFAULT 'pending',
                    -- pending / generating / ready / approved / publishing / published / failed
                scheduled_for    TEXT,                        -- UTC ISO，定时发布时间（NULL=立即）
                error_msg        TEXT,                        -- 失败时的错误信息
                created_at       TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_studio_content_job_id
                ON studio_content(job_id);
            CREATE INDEX IF NOT EXISTS idx_studio_content_status
                ON studio_content(status);

            -- 已发布帖子追踪
            CREATE TABLE IF NOT EXISTS studio_posts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                content_id       INTEGER NOT NULL REFERENCES studio_content(id),
                platform         TEXT    NOT NULL,
                platform_post_id TEXT,                        -- 平台返回的帖子 ID
                device_id        TEXT,                        -- 执行发布的设备
                published_at     TEXT,                        -- UTC ISO 时间
                views            INTEGER DEFAULT 0,
                likes            INTEGER DEFAULT 0,
                comments         INTEGER DEFAULT 0,
                shares           INTEGER DEFAULT 0,
                status           TEXT    NOT NULL DEFAULT 'published'
                    -- published / deleted / failed
            );
            CREATE INDEX IF NOT EXISTS idx_studio_posts_content_id
                ON studio_posts(content_id);

            -- 框架性能跟踪表
            CREATE TABLE IF NOT EXISTS studio_framework_perf (
                framework_id TEXT PRIMARY KEY,
                approved_count INTEGER DEFAULT 0,
                rejected_count INTEGER DEFAULT 0,
                published_count INTEGER DEFAULT 0,
                total_engagement INTEGER DEFAULT 0,
                last_updated TEXT DEFAULT (datetime('now'))
            );
        """)
    # 迁移：为旧版本数据库补充 scheduled_for 列（幂等）
    with _get_conn() as conn:
        try:
            conn.execute("ALTER TABLE studio_content ADD COLUMN scheduled_for TEXT")
        except Exception:
            pass  # 列已存在，忽略

    # 迁移：为旧版本数据库补充 error_code 列（幂等）
    with _get_conn() as conn:
        try:
            conn.execute("ALTER TABLE studio_content ADD COLUMN error_code TEXT DEFAULT 'UNKNOWN'")
            conn.commit()
        except Exception:
            pass  # 列已存在，忽略

    # 重启恢复：将中断的 generating 任务标记为 failed
    with _get_conn() as conn:
        affected = conn.execute(
            "UPDATE studio_jobs SET status='failed', updated_at=? "
            "WHERE status='generating'",
            (datetime.now(timezone.utc).isoformat(),)
        ).rowcount
        if affected:
            conn.execute(
                "UPDATE studio_content SET status='failed', error_msg='服务重启中断' "
                "WHERE status IN ('pending','generating') "
                "AND job_id IN (SELECT job_id FROM studio_jobs WHERE status='failed')"
            )
            logger.warning("重启恢复: %d 个中断任务已标记为 failed", affected)

    logger.info("Studio DB 初始化完成: %s", get_studio_db())


# ---------------------------------------------------------------------------
# Jobs CRUD
# ---------------------------------------------------------------------------

def create_job(persona_id: str, platforms: List[str], mode: str = "full_auto") -> str:
    """
    创建一个新的内容生成任务。
    返回: job_id (UUID 字符串)
    """
    job_id = str(uuid.uuid4())
    now = _now_utc()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO studio_jobs
                (job_id, persona_id, status, mode, platforms, created_at, updated_at)
            VALUES (?, ?, 'pending', ?, ?, ?, ?)
            """,
            (job_id, persona_id, mode, json.dumps(platforms), now, now),
        )
    logger.debug("创建 job: %s persona=%s platforms=%s", job_id, persona_id, platforms)
    return job_id


def update_job_status(job_id: str, status: str) -> None:
    """更新任务状态，同时刷新 updated_at。"""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE studio_jobs SET status=?, updated_at=? WHERE job_id=?",
            (status, _now_utc(), job_id),
        )


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """按 job_id 查询任务，返回 dict 或 None。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM studio_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["platforms"] = json.loads(d["platforms"] or "[]")
    return d


def list_jobs(limit: int = 20) -> List[Dict[str, Any]]:
    """列出最近的任务，按创建时间倒序。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM studio_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["platforms"] = json.loads(d["platforms"] or "[]")
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Content CRUD
# ---------------------------------------------------------------------------

def create_content(
    job_id: str, platform: str, content_type: str, style: str
) -> int:
    """
    为指定 job 创建一条平台内容记录。
    返回: content id (INTEGER)
    """
    with _get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO studio_content
                (job_id, platform, content_type, style, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, platform, content_type, style, _now_utc()),
        )
        return cur.lastrowid


# ── 错误分类 ──────────────────────────────────────────────────────────────
ERROR_CATEGORIES = {
    "API_KEY_MISSING":    {"label": "API Key 未配置", "hint": "请在设置中配置 FAL_KEY 或 OPENAI_API_KEY", "severity": "blocking"},
    "LLM_FAILED":         {"label": "AI文案生成失败", "hint": "检查 LLM API Key 是否有效，或等待服务恢复", "severity": "recoverable"},
    "IMAGE_GEN_FAILED":   {"label": "图片生成失败", "hint": "检查 FAL_KEY 是否有效，或账户余额是否充足", "severity": "recoverable"},
    "VIDEO_GEN_FAILED":   {"label": "视频生成失败", "hint": "检查 FAL_KEY 是否有效，视频生成成本较高", "severity": "recoverable"},
    "DEVICE_OFFLINE":     {"label": "发布设备离线", "hint": "检查 ADB 设备连接，或使用半自动模式手动发布", "severity": "blocking"},
    "PUBLISH_FAILED":     {"label": "发布操作失败", "hint": "查看 ADB 日志，可能是 App 版本变化导致元素定位失败", "severity": "recoverable"},
    "CONTENT_POLICY":     {"label": "内容违规嫌疑", "hint": "人工检查内容后重新生成，或调整内容框架", "severity": "manual"},
    "TEMPLATE_FALLBACK":  {"label": "使用模板生成（质量较低）", "hint": "配置 LLM API Key 获得更高质量内容", "severity": "warning"},
    "UNKNOWN":            {"label": "未知错误", "hint": "查看服务器日志获取详细信息", "severity": "recoverable"},
}


def classify_error(error_msg: str) -> str:
    """根据错误信息字符串推断错误分类。"""
    if not error_msg:
        return "UNKNOWN"
    msg = error_msg.lower()
    if "api key" in msg or "apikey" in msg or "unauthorized" in msg or "401" in msg:
        return "API_KEY_MISSING"
    if "fal" in msg and ("key" in msg or "auth" in msg):
        return "API_KEY_MISSING"
    if "openai" in msg or "llm" in msg or "crewai" in msg or "completions" in msg:
        return "LLM_FAILED"
    if "image" in msg and ("fail" in msg or "error" in msg or "timeout" in msg):
        return "IMAGE_GEN_FAILED"
    if "video" in msg and ("fail" in msg or "error" in msg or "timeout" in msg):
        return "VIDEO_GEN_FAILED"
    # 发布失败先于设备离线检查（"TikTok button not found"应归为 PUBLISH_FAILED）
    if "publish" in msg or "tiktok" in msg or "instagram" in msg or "reels" in msg:
        return "PUBLISH_FAILED"
    if "adb" in msg or "device" in msg or "offline" in msg or "not found" in msg:
        return "DEVICE_OFFLINE"
    if "policy" in msg or "violat" in msg or "banned" in msg:
        return "CONTENT_POLICY"
    return "UNKNOWN"


def update_content(content_id: int, **kwargs) -> None:
    """
    灵活更新 content 字段。
    JSON 字段（hashtags / visual_prompts / image_paths）若传 list，自动序列化。
    当 status='failed' 且 error_msg 存在时，自动写入 error_code。
    """
    # 自动填充 error_code
    if kwargs.get("status") == "failed" and kwargs.get("error_msg") and "error_code" not in kwargs:
        kwargs["error_code"] = classify_error(kwargs["error_msg"])

    json_fields = {"hashtags", "visual_prompts", "image_paths"}
    set_parts = []
    values = []
    for k, v in kwargs.items():
        if k in json_fields and isinstance(v, (list, dict)):
            v = json.dumps(v, ensure_ascii=False)
        set_parts.append(f"{k}=?")
        values.append(v)
    if not set_parts:
        return
    values.append(content_id)
    sql = f"UPDATE studio_content SET {', '.join(set_parts)} WHERE id=?"
    with _get_conn() as conn:
        conn.execute(sql, values)


def get_content(content_id: int) -> Optional[Dict[str, Any]]:
    """按 id 查询 content 记录，JSON 字段自动反序列化。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM studio_content WHERE id=?", (content_id,)
        ).fetchone()
    if row is None:
        return None
    return _deserialize_content(dict(row))


def list_content_by_job(job_id: str) -> List[Dict[str, Any]]:
    """列出某个 job 下的所有 content 记录。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM studio_content WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()
    return [_deserialize_content(dict(r)) for r in rows]


def list_pending_approval() -> List[Dict[str, Any]]:
    """
    返回所有待人工审核的内容（semi_auto 模式专用）。
    条件: status='ready' AND approved=0
    """
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.* FROM studio_content c
            JOIN studio_jobs j ON c.job_id = j.job_id
            WHERE c.status='ready' AND c.approved=0 AND j.mode='semi_auto'
            ORDER BY c.created_at DESC
            """
        ).fetchall()
    return [_deserialize_content(dict(r)) for r in rows]


def list_ready_to_publish(platform: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    返回所有可立即发布的内容。
    - full_auto: status='ready'
    - semi_auto: status='ready' AND approved=1
    """
    with _get_conn() as conn:
        sql = """
            SELECT c.* FROM studio_content c
            JOIN studio_jobs j ON c.job_id = j.job_id
            WHERE c.status='ready'
              AND (j.mode='full_auto' OR (j.mode='semi_auto' AND c.approved=1))
        """
        params: list = []
        if platform:
            sql += " AND c.platform=?"
            params.append(platform)
        sql += " ORDER BY c.created_at ASC"
        rows = conn.execute(sql, params).fetchall()
    return [_deserialize_content(dict(r)) for r in rows]


def approve_content(content_id: int) -> None:
    """人工批准内容，approved=1，status 推进到 'approved'。"""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE studio_content SET approved=1, status='approved' WHERE id=?",
            (content_id,),
        )
        # 提取 framework_id 用于性能跟踪（从 script 字段的 JSON 元数据中读取）
        try:
            row = conn.execute("SELECT script FROM studio_content WHERE id=?", (content_id,)).fetchone()
            if row and row["script"]:
                import json as _json
                meta = _json.loads(row["script"]) if row["script"].startswith("{") else {}
                fw_id = meta.get("framework_id", "")
                if fw_id:
                    record_framework_event(fw_id, "approved")
        except Exception:
            pass


def reject_content(content_id: int, reason: str) -> None:
    """拒绝内容，status='failed'，记录拒绝原因。"""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE studio_content SET status='failed', error_msg=? WHERE id=?",
            (f"[REJECTED] {reason}", content_id),
        )
        # 提取 framework_id 用于性能跟踪
        try:
            row = conn.execute("SELECT script FROM studio_content WHERE id=?", (content_id,)).fetchone()
            if row and row["script"]:
                import json as _json
                meta = _json.loads(row["script"]) if row["script"].startswith("{") else {}
                fw_id = meta.get("framework_id", "")
                if fw_id:
                    record_framework_event(fw_id, "rejected")
        except Exception:
            pass


def record_framework_event(framework_id: str, event: str) -> None:
    """记录框架使用事件（approved/rejected/published），用于建议引擎权重优化。"""
    if not framework_id:
        return
    col_map = {"approved": "approved_count", "rejected": "rejected_count", "published": "published_count"}
    col = col_map.get(event)
    if not col:
        return
    with _get_conn() as conn:
        conn.execute(
            f"INSERT INTO studio_framework_perf(framework_id,{col}) VALUES(?,1) "
            f"ON CONFLICT(framework_id) DO UPDATE SET {col}={col}+1, last_updated=datetime('now')",
            (framework_id,)
        )
        conn.commit()


def get_framework_perf() -> dict:
    """返回所有框架的性能数据，key=framework_id。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT framework_id, approved_count, rejected_count, published_count FROM studio_framework_perf"
        ).fetchall()
    return {
        r["framework_id"]: {
            "approved": r["approved_count"],
            "rejected": r["rejected_count"],
            "published": r["published_count"],
            "approval_rate": round(r["approved_count"] / max(r["approved_count"] + r["rejected_count"], 1), 2),
        }
        for r in rows
    }


# ---------------------------------------------------------------------------
# Posts CRUD
# ---------------------------------------------------------------------------

def create_post(
    content_id: int,
    platform: str,
    platform_post_id: Optional[str],
    device_id: Optional[str],
) -> int:
    """
    记录一条已发布的帖子。
    返回: post id (INTEGER)
    """
    with _get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO studio_posts
                (content_id, platform, platform_post_id, device_id, published_at, status)
            VALUES (?, ?, ?, ?, ?, 'published')
            """,
            (content_id, platform, platform_post_id, device_id, _now_utc()),
        )
        return cur.lastrowid


def update_post_stats(
    post_id: int, views: int, likes: int, comments: int, shares: int
) -> None:
    """刷新帖子互动数据（定时任务调用）。"""
    with _get_conn() as conn:
        conn.execute(
            """
            UPDATE studio_posts
            SET views=?, likes=?, comments=?, shares=?
            WHERE id=?
            """,
            (views, likes, comments, shares, post_id),
        )


def schedule_content(content_id: int, publish_at_utc) -> None:
    """设置内容的定时发布时间。publish_at_utc=None 则取消排期（approved 重置为 0）。"""
    with _get_conn() as conn:
        if publish_at_utc is None:
            conn.execute(
                "UPDATE studio_content SET scheduled_for=NULL, approved=0 WHERE id=?",
                (content_id,)
            )
        else:
            conn.execute(
                "UPDATE studio_content SET scheduled_for=?, approved=1, status='ready' WHERE id=?",
                (publish_at_utc, content_id)
            )


def list_due_scheduled_content() -> List[Dict[str, Any]]:
    """查询已到时间的定时发布内容（scheduled_for <= NOW，approved=1，status=ready）。"""
    from datetime import datetime, timezone
    now_str = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.*, j.persona_id FROM studio_content c
            JOIN studio_jobs j ON c.job_id = j.job_id
            WHERE c.status = 'ready'
              AND c.approved = 1
              AND c.scheduled_for IS NOT NULL
              AND c.scheduled_for <= ?
            ORDER BY c.scheduled_for ASC
            LIMIT 10
            """,
            (now_str,)
        ).fetchall()
    return [_deserialize_content(dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# 统计看板
# ---------------------------------------------------------------------------

def get_studio_stats() -> Dict[str, Any]:
    """
    返回 Content Studio 整体统计摘要，供仪表盘展示。
    包括: job/content/post 各状态计数，总互动量，最近活跃 persona。
    """
    with _get_conn() as conn:
        # Job 各状态数量
        job_rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM studio_jobs GROUP BY status"
        ).fetchall()
        job_stats = {r["status"]: r["cnt"] for r in job_rows}

        # Content 各状态数量
        content_rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM studio_content GROUP BY status"
        ).fetchall()
        content_stats = {r["status"]: r["cnt"] for r in content_rows}

        # 发布帖子总计 & 互动汇总
        post_agg = conn.execute(
            """
            SELECT
                COUNT(*) as total_posts,
                COALESCE(SUM(views), 0)    as total_views,
                COALESCE(SUM(likes), 0)    as total_likes,
                COALESCE(SUM(comments), 0) as total_comments,
                COALESCE(SUM(shares), 0)   as total_shares
            FROM studio_posts WHERE status='published'
            """
        ).fetchone()

        # 最活跃的 persona（发布内容最多）
        top_persona = conn.execute(
            """
            SELECT persona_id, COUNT(*) as cnt
            FROM studio_jobs
            GROUP BY persona_id
            ORDER BY cnt DESC
            LIMIT 5
            """
        ).fetchall()

    return {
        "jobs": job_stats,
        "content": content_stats,
        "posts": {
            "total": post_agg["total_posts"],
            "views": post_agg["total_views"],
            "likes": post_agg["total_likes"],
            "comments": post_agg["total_comments"],
            "shares": post_agg["total_shares"],
        },
        "top_personas": [{"persona_id": r["persona_id"], "jobs": r["cnt"]} for r in top_persona],
        "generated_at": _now_utc(),
    }


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _deserialize_content(d: Dict[str, Any]) -> Dict[str, Any]:
    """将 studio_content 的 JSON 字段反序列化为 Python 对象。"""
    for field in ("hashtags", "visual_prompts", "image_paths"):
        raw = d.get(field)
        if raw:
            try:
                d[field] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d[field] = []
        else:
            d[field] = []
    return d


def list_published_posts(days: int = 7, limit: int = 100) -> List[Dict[str, Any]]:
    """查询最近N天的已发布帖子（供效果回传轮询）。"""
    from datetime import datetime, timezone, timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, content_id, platform, platform_post_id, device_id,
                   published_at, views, likes, comments, shares, status
            FROM studio_posts
            WHERE status='published' AND published_at >= ?
            ORDER BY published_at DESC LIMIT ?
            """,
            (since, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_top_performing_frameworks(limit: int = 5) -> List[Dict[str, Any]]:
    """查询互动最高的内容关联的框架（用于强化学习）。"""
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT sc.framework_id, AVG(sp.likes + sp.comments * 2 + sp.shares * 3) as eng_score,
                   COUNT(*) as post_count
            FROM studio_posts sp
            JOIN studio_content sc ON sc.id = sp.content_id
            WHERE sc.framework_id IS NOT NULL AND sp.likes > 0
            GROUP BY sc.framework_id
            ORDER BY eng_score DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 模块加载时自动初始化
# ---------------------------------------------------------------------------
init_studio_db()
