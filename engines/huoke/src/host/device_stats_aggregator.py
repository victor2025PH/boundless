"""
device_stats_aggregator.py
每5分钟轮询 W03 /tiktok/devices，将设备统计写入本地 device_daily_stats 表。
在 api.py startup 事件中启动后台线程。
"""
from __future__ import annotations
import json, logging, os, sqlite3, threading, time, urllib.request as _ur
from datetime import datetime

logger = logging.getLogger("device_stats_agg")

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "openclaw.db")
_W03_BASE = "http://192.168.0.103:8000"
_POLL_INTERVAL = 300  # 5 minutes

_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _get_db():
    return sqlite3.connect(os.path.normpath(_DB_PATH))


def _fetch_w03_devices() -> list[dict]:
    """从 W03 拉取 /tiktok/devices 列表，返回设备列表。"""
    try:
        req = _ur.Request(f"{_W03_BASE}/tiktok/devices", headers={"Accept": "application/json", "Connection": "close"})
        resp = _ur.urlopen(req, timeout=8)
        try:
            data = json.loads(resp.read())
        finally:
            resp.close()
        if isinstance(data, list):
            return data
        return data.get("devices", data.get("items", []))
    except Exception as e:
        logger.warning(f"W03 fetch failed: {e}")
        return []


def _upsert_stats(device_id: str, date: str, row: dict):
    """Upsert device_daily_stats row，增量更新（取最大值）。"""
    conn = _get_db()
    try:
        existing = conn.execute(
            "SELECT sessions_count, videos_watched, follows_count, dms_sent, algo_score FROM device_daily_stats WHERE device_id=? AND date=?",
            (device_id, date)
        ).fetchone()

        sessions   = max(int(row.get("sessions_today") or 0), existing[0] if existing else 0)
        watched    = max(int(row.get("today_watched") or 0),   existing[1] if existing else 0)
        follows    = max(int(row.get("today_followed") or 0),  existing[2] if existing else 0)
        dms_sent   = max(int(row.get("today_dms") or 0),       existing[3] if existing else 0)
        algo_score = max(float(row.get("algo_score") or 0),    existing[4] if existing else 0)

        conn.execute("""
            INSERT INTO device_daily_stats
                (device_id, date, sessions_count, videos_watched, follows_count, dms_sent, algo_score, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(device_id, date) DO UPDATE SET
                sessions_count = excluded.sessions_count,
                videos_watched = excluded.videos_watched,
                follows_count  = excluded.follows_count,
                dms_sent       = excluded.dms_sent,
                algo_score     = excluded.algo_score,
                updated_at     = CURRENT_TIMESTAMP
        """, (device_id, date, sessions, watched, follows, dms_sent, algo_score))
        conn.commit()
    except Exception as e:
        logger.error(f"upsert_stats error: {e}")
    finally:
        conn.close()


def _run_once():
    """执行一次轮询：拉 W03 数据 → 写本地 DB。"""
    devices = _fetch_w03_devices()
    if not devices:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    updated = 0
    for dev in devices:
        did = dev.get("device_id") or dev.get("id") or ""
        if not did:
            continue
        _upsert_stats(did, today, dev)
        updated += 1
    logger.info(f"device_stats_agg: updated {updated} devices for {today}")


def _loop():
    logger.info("device_stats_aggregator started")
    while not _stop_event.is_set():
        try:
            _run_once()
        except Exception as e:
            logger.error(f"aggregator loop error: {e}")
        _stop_event.wait(timeout=_POLL_INTERVAL)
    logger.info("device_stats_aggregator stopped")


def _ensure_table():
    """确保 device_daily_stats 表存在，不存在则创建（兼容 worker 节点首次启动）。"""
    conn = _get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS device_daily_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                date TEXT NOT NULL,
                sessions_count INTEGER DEFAULT 0,
                videos_watched INTEGER DEFAULT 0,
                follows_count INTEGER DEFAULT 0,
                dms_sent INTEGER DEFAULT 0,
                dms_responded INTEGER DEFAULT 0,
                leads_qualified INTEGER DEFAULT 0,
                algo_score REAL DEFAULT 0,
                online_minutes INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(device_id, date)
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dds_device_date ON device_daily_stats(device_id, date)")
        conn.commit()
    except Exception as e:
        logger.warning("建表失败（可忽略）: %s", e)
    finally:
        conn.close()


def start():
    """在应用启动时调用一次，启动后台轮询线程。"""
    global _thread
    if _thread and _thread.is_alive():
        return
    _ensure_table()
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="device-stats-agg")
    _thread.start()


def stop():
    """优雅停止（可选，daemon线程会随主进程退出）。"""
    _stop_event.set()


def get_history(device_id: str, days: int = 7) -> list[dict]:
    """返回设备最近 N 天的统计，用于 sparkline。"""
    conn = _get_db()
    try:
        rows = conn.execute("""
            SELECT date, sessions_count, videos_watched, follows_count,
                   dms_sent, dms_responded, leads_qualified, algo_score
            FROM device_daily_stats
            WHERE device_id = ?
            ORDER BY date DESC
            LIMIT ?
        """, (device_id, days)).fetchall()
        result = []
        for r in rows:
            result.append({
                "date":           r[0],
                "sessions":       r[1],
                "watched":        r[2],
                "follows":        r[3],
                "dms_sent":       r[4],
                "dms_responded":  r[5],
                "leads_qualified": r[6],
                "algo_score":     r[7],
            })
        # 返回按日期升序（sparkline从左到右）
        return list(reversed(result))
    finally:
        conn.close()
