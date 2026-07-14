# -*- coding: utf-8 -*-
"""
跨设备 CRM 同步路由 — 主控作为 CRM 主库，Worker 设备通过此 API 同步对话历史。
Fix-3: 使用 SQLite 持久化，重启不丢数据，内存缓存加速读取。

端点：
  GET  /crm/contact/{name}                    — 拉取联系人的跨设备合并历史
  POST /crm/contact/{name}/interaction        — 任意设备写入一条交互记录
  GET  /crm/stats                             — 查看同步统计
  DELETE /crm/contact/{name}                  — 清除联系人历史（测试用）
"""

import logging
import time
from typing import Optional

from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/crm", tags=["crm"])

# 内存缓存：contact_name → list of interactions（加速读取）
_crm_cache: dict = {}
_MAX_HISTORY = 200
_write_count = 0
_cache_loaded = False


def _get_conn():
    from ..database import get_conn
    return get_conn()


def _preload_crm_cache():
    """启动时从DB加载最近数据到内存缓存（加速后续GET请求）。"""
    global _cache_loaded
    if _cache_loaded:
        return
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                """SELECT contact, direction, text, intent, device_id, action, ts
                   FROM crm_interactions
                   ORDER BY id DESC LIMIT 5000"""
            ).fetchall()
        # 反转后按contact分组（最旧→最新）
        by_contact: dict = {}
        for r in reversed(rows):
            c = r[0]
            if c not in by_contact:
                by_contact[c] = []
            by_contact[c].append({
                "ts": r[6], "direction": r[1], "text": r[2],
                "intent": r[3], "device_id": r[4], "action": r[5],
            })
        _crm_cache.update(by_contact)
        _cache_loaded = True
        logger.info("[CRM缓存] 预加载 %d 个联系人历史", len(by_contact))
    except Exception as e:
        logger.warning("[CRM缓存] 预加载失败: %s", e)
        _cache_loaded = True  # 避免反复失败


@router.get("/contact/{name}")
def get_contact_history(name: str, limit: int = 30, platform: str = "tiktok"):
    """拉取某联系人的跨设备合并对话历史（优先内存缓存，回退到DB）。"""
    _preload_crm_cache()
    history = _crm_cache.get(name, [])
    if not history:
        # 缓存未命中，直接查DB
        try:
            with _get_conn() as conn:
                rows = conn.execute(
                    """SELECT direction, text, intent, device_id, action, ts
                       FROM crm_interactions WHERE contact=? ORDER BY id ASC LIMIT ?""",
                    (name, _MAX_HISTORY)
                ).fetchall()
            history = [{"direction": r[0], "text": r[1], "intent": r[2],
                        "device_id": r[3], "action": r[4], "ts": r[5]} for r in rows]
            if history:
                _crm_cache[name] = history
        except Exception as e:
            logger.debug("[CRM] DB查询失败: %s", e)

    recent = history[-limit:] if len(history) > limit else history
    inbound_count = sum(1 for h in history if h.get("direction") == "inbound")
    outbound_count = sum(1 for h in history if h.get("direction") == "outbound")
    last_referral_sent = any(h.get("action") == "referral_sent" for h in history)
    devices_involved = list({h.get("device_id", "") for h in history if h.get("device_id")})
    return {
        "contact": name, "platform": platform,
        "total_messages": len(history),
        "inbound_count": inbound_count,
        "outbound_count": outbound_count,
        "last_referral_sent": last_referral_sent,
        "devices_involved": devices_involved,
        "history": recent,
    }


@router.post("/contact/{name}/interaction")
def push_contact_interaction(name: str, body: dict):
    """从任意设备推送一条交互记录到主控（双写：内存+DB）。"""
    global _write_count
    direction = body.get("direction", "inbound")
    text = (body.get("text") or "")[:500]
    intent = body.get("intent", "")
    device_id = body.get("device_id", "")
    action = body.get("action", "")
    platform = body.get("platform", "tiktok")
    ts = body.get("ts") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    record = {"ts": ts, "direction": direction, "text": text,
              "intent": intent, "device_id": device_id, "action": action}

    # 写内存缓存
    _preload_crm_cache()
    if name not in _crm_cache:
        _crm_cache[name] = []
    _crm_cache[name].append(record)
    if len(_crm_cache[name]) > _MAX_HISTORY:
        _crm_cache[name] = _crm_cache[name][-_MAX_HISTORY:]
    _write_count += 1

    # 写DB（异步不阻塞主流程）
    try:
        with _get_conn() as conn:
            conn.execute(
                """INSERT INTO crm_interactions
                   (contact, direction, text, intent, device_id, action, platform, ts, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (name, direction, text, intent, device_id, action, platform, ts, created_at)
            )
    except Exception as e:
        logger.debug("[CRM] DB写入失败(已写内存): %s", e)

    logger.debug("[CRM同步] %s ← %s %s (%s)", name, direction, text[:40], device_id[:8])
    return {"ok": True, "contact": name, "total": len(_crm_cache.get(name, []))}


@router.get("/stats")
def crm_stats():
    """查看跨设备CRM同步统计。"""
    _preload_crm_cache()
    total_contacts = len(_crm_cache)
    total_messages = sum(len(v) for v in _crm_cache.values())
    contacts_with_referral = sum(
        1 for v in _crm_cache.values()
        if any(h.get("action") == "referral_sent" for h in v)
    )
    # DB中实际记录数
    db_count = 0
    try:
        with _get_conn() as conn:
            db_count = conn.execute("SELECT COUNT(*) FROM crm_interactions").fetchone()[0]
    except Exception:
        pass
    return {
        "total_contacts": total_contacts,
        "total_messages": total_messages,
        "contacts_with_referral": contacts_with_referral,
        "write_count": _write_count,
        "db_records": db_count,
        "cache_loaded": _cache_loaded,
    }


@router.delete("/contact/{name}")
def clear_contact_history(name: str):
    """清除某联系人的历史（用于测试）。"""
    removed_cache = _crm_cache.pop(name, None)
    removed_db = 0
    try:
        with _get_conn() as conn:
            cur = conn.execute("DELETE FROM crm_interactions WHERE contact=?", (name,))
            removed_db = cur.rowcount
    except Exception as e:
        logger.debug("[CRM] DB删除失败: %s", e)
    return {"ok": True, "removed_cache": removed_cache is not None, "removed_db": removed_db}
