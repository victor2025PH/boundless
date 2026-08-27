# -*- coding: utf-8 -*-
"""账号资产中心（账号资产保全 P1，2026-08-19；方案 §4-B 见 docs/实施47）。

- GET /workspace/assets              — 页面（agent/viewer 302 回工作台，其余角色可见）
- GET /api/workspace/assets/summary  — 总览 + 每账号资产卡（60s TTL 缓存，?force=1 绕过）
- GET /api/workspace/assets/ledger   — 导出/快照审计台账（ops_events 只读）

设计要点（改动前先读）：
- **全只读**。可加回句柄（reachability）与媒体计数是两条自定义聚合 SQL，走
  inbox.db 的 **read-only URI 连接**——刻意不给 store.py 加方法：接管线（工单 1-3）
  正在改该文件（2026-08-19 意向板），本线零文件重叠。
- 账号清单 = registry ``list(include_removed=True)`` ∪ ``store.account_directory()``
  （后者补「仅历史」账号）。封禁语义 = registry ``meta.banned``（ban_signal 写入，
  不改 status 列——正是 UI 无专态的根子），在本页覆盖一切其他状态。
- 兄弟线产出 **feature-probe**：export-migration（接管线工单 2）/ reconnect
  （回连认领线）端点按 app 路由表探测，未装载（22:30 重启前）前端不渲染对应
  CTA——graceful 而非 404。``features.reconnect`` 当前只随 payload 透出（迁移向导
  P1 后续消费），页面 v1 不渲染其入口。
- 联系人口径 = ``protocol_contacts_summary(include_chats=True)``（通讯录 ∪ 私聊
  peer 的「人的并集」）——TG ``get_contacts()`` 语义极窄，纯通讯录口径恒为 0
  （实施47 §1 实证）。reachability 分母是**会话档案**私聊行（username/phone 在
  conversations 身份列；book-only 联系人无身份列，计入会误报「无句柄」）。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

# 与 unified_inbox_account_routes._require_account_manager 同契约（导出/清除的
# 拒绝集）；本地复刻而非跨模块 import——那是 3900+ 行的路由模块，测试装配不该
# 为 5 行守卫背上它的全部依赖。
_MANAGE_DENY_ROLES = {"agent", "viewer"}

_SUMMARY_TTL_SEC = 60.0
_summary_cache: Dict[str, Any] = {"ts": 0.0, "payload": None}

# 台账默认 kind 集：前两个已在写（export-history / purge-history）；其余是
# 接管线按实施47 §5 契约将写入的 kind——IN 查询对不存在的 kind 零成本，
# 它们落地当天台账自动出现，无需回来改这里。
_DEFAULT_LEDGER_KINDS = (
    "account_export",
    "account_purge",
    "account_export_migration",
    "account_snapshot",
    "asset_snapshot",
)

_RECONNECT_PROBE_PATH = "/api/admin/asset/reconnect/candidates"


def _require_manager(request: Request) -> None:
    try:
        role = str(request.session.get("role", "") or "")
    except Exception:
        role = ""
    if role in _MANAGE_DENY_ROLES:
        raise HTTPException(403, tr(request, "err.perm.supervisor_required"))


def _session_role(request: Request) -> str:
    try:
        return str(request.session.get("role", "") or "")
    except Exception:
        return ""


# ── 只读 SQL（inbox.db）────────────────────────────────────────────────────────

def _ro_connect(db_path: Path) -> Optional[sqlite3.Connection]:
    """尽力以只读模式开库；URI 不可用时回落普通连接（本模块只发 SELECT）。"""
    try:
        uri = "file:///" + str(db_path).replace("\\", "/").lstrip("/") + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=5)
    except Exception:
        try:
            return sqlite3.connect(str(db_path), timeout=5)
        except Exception:
            logger.debug("[asset_center] inbox.db 连接失败", exc_info=True)
            return None


def _reachability_map(db_path: Optional[Path]) -> Dict[Tuple[str, str], Dict[str, int]]:
    """(platform, account_id) → {both, username_only, phone_only, none}。

    口径：私聊会话（chat_type IN ('private','')）、非 bot、chat_key 非空——与
    reconnect_claim 的加回清单同一人群；username/phone 取 conversations 身份列。
    """
    if not db_path:
        return {}
    conn = _ro_connect(Path(db_path))
    if conn is None:
        return {}
    out: Dict[Tuple[str, str], Dict[str, int]] = {}
    try:
        rows = conn.execute(
            """
            SELECT platform, account_id,
              SUM(CASE WHEN TRIM(username)!='' AND TRIM(phone)!='' THEN 1 ELSE 0 END),
              SUM(CASE WHEN TRIM(username)!='' AND TRIM(phone)=''  THEN 1 ELSE 0 END),
              SUM(CASE WHEN TRIM(username)=''  AND TRIM(phone)!='' THEN 1 ELSE 0 END),
              SUM(CASE WHEN TRIM(username)=''  AND TRIM(phone)=''  THEN 1 ELSE 0 END)
            FROM conversations
            WHERE chat_key != '' AND chat_type IN ('private', '')
              AND COALESCE(peer_is_bot, 0) = 0
            GROUP BY platform, account_id
            """
        ).fetchall()
        for r in rows:
            both, uo, po, none = (int(r[2] or 0), int(r[3] or 0),
                                  int(r[4] or 0), int(r[5] or 0))
            out[(str(r[0]), str(r[1]))] = {
                "both": both, "username_only": uo,
                "phone_only": po, "none": none,
            }
    except Exception:
        # 极老库缺身份列（迁移没跑过）→ 覆盖读数整体缺席，卡片显示「—」不装数。
        logger.debug("[asset_center] reachability 查询失败", exc_info=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def _media_count_map(db_path: Optional[Path]) -> Dict[Tuple[str, str], int]:
    """(platform, account_id) → 有媒体附件的消息数（media_ref 非空）。

    messages 全表扫 + join——ops 页低频访问 + 60s TTL 缓存可容；勿把本查询
    搬进高频轮询面。
    """
    if not db_path:
        return {}
    conn = _ro_connect(Path(db_path))
    if conn is None:
        return {}
    out: Dict[Tuple[str, str], int] = {}
    try:
        rows = conn.execute(
            """
            SELECT v.platform, v.account_id, COUNT(*)
            FROM messages m JOIN conversations v
              ON v.conversation_id = m.conversation_id
            WHERE TRIM(m.media_ref) != ''
            GROUP BY v.platform, v.account_id
            """
        ).fetchall()
        for r in rows:
            out[(str(r[0]), str(r[1]))] = int(r[2] or 0)
    except Exception:
        logger.debug("[asset_center] media 计数失败", exc_info=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


# ── 磁盘侧（媒体总量 / 最近备份）──────────────────────────────────────────────

def _media_disk_totals() -> Dict[str, int]:
    """协议媒体磁盘总量（文件数/字节）。新旧两根都算：22:30 搬迁前文件全在旧根、
    搬迁后在新根，求和在两种状态下都正确（冲突保源的少量残留会双计，误差可容）。"""
    files = 0
    size = 0
    try:
        from src.integrations.protocol_bridge import protocol_media_roots
        for root in protocol_media_roots():
            if not root.is_dir():
                continue
            for p in root.rglob("*"):
                try:
                    if p.is_file():
                        files += 1
                        size += p.stat().st_size
                except OSError:
                    continue
    except Exception:
        logger.debug("[asset_center] 媒体盘点失败", exc_info=True)
    return {"files": files, "bytes": size}


def _data_root() -> Path:
    """实例数据根：AITR_CONFIG_PATH 父父 > AITR_DATA_DIR > CWD（生产进程 CWD 即
    数据根，双实例部署契约）。与 protocol_bridge._media_data_root 同序。"""
    try:
        env_cfg = (os.environ.get("AITR_CONFIG_PATH") or "").strip()
        if env_cfg:
            return Path(env_cfg).expanduser().parent.parent
        env_dir = (os.environ.get("AITR_DATA_DIR") or "").strip()
        if env_dir:
            return Path(env_dir).expanduser()
    except Exception:
        pass
    return Path.cwd()


def _last_backup() -> Optional[Dict[str, Any]]:
    """最近一次实例备份（instance_backup.py 落 ``<数据根>.parent/backups``）。"""
    try:
        bdir = _data_root().parent / "backups"
        if not bdir.is_dir():
            return None
        best: Optional[Path] = None
        best_mtime = 0.0
        for p in bdir.glob("instance-backup-*.zip"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m > best_mtime:
                best, best_mtime = p, m
        if best is None:
            return None
        return {"ts": best_mtime, "file": best.name,
                "bytes": int(best.stat().st_size)}
    except Exception:
        logger.debug("[asset_center] 备份扫描失败", exc_info=True)
        return None


# ── 账号聚合 ──────────────────────────────────────────────────────────────────

def _registry_rows() -> List[Dict[str, Any]]:
    try:
        from src.integrations.account_registry import get_account_registry
        return list(get_account_registry().list(include_removed=True) or [])
    except Exception:
        logger.debug("[asset_center] 账号注册表不可用", exc_info=True)
        return []


def _resolve_status(registry_status: str, banned: bool, history_only: bool) -> str:
    if banned:
        return "banned"
    if history_only:
        return "history_only"
    return registry_status or "offline"


def _feature_flags(app_) -> Dict[str, bool]:
    paths = set()
    try:
        for r in getattr(app_, "routes", []) or []:
            p = getattr(r, "path", "")
            if p:
                paths.add(str(p))
    except Exception:
        pass
    return {
        "export_migration": any(p.endswith("/export-migration") for p in paths),
        "reconnect": _RECONNECT_PROBE_PATH in paths,
    }


def _build_summary(request: Request) -> Dict[str, Any]:
    store = getattr(request.app.state, "inbox_store", None)
    if store is None:
        raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
    db_path = getattr(store, "_db_path", None)
    reach_map = _reachability_map(db_path)
    media_map = _media_count_map(db_path)

    try:
        directory = dict(store.account_directory() or {})
    except Exception:
        logger.debug("[asset_center] account_directory 失败", exc_info=True)
        directory = {}

    seen: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in _registry_rows():
        p = str(row.get("platform") or "")
        a = str(row.get("account_id") or "")
        if not p or not a:
            continue
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        seen[(p, a)] = {
            "platform": p, "account_id": a,
            "label": str(row.get("label") or "") or a,
            "mode": str(row.get("mode") or ""),
            "registry_status": str(row.get("status") or ""),
            "banned": bool(meta.get("banned")),
            "ban_reason": str(meta.get("ban_reason") or ""),
            "last_online_at": float(row.get("last_online_at") or 0),
            "in_registry": True,
        }
    for (p, a) in directory.keys():
        key = (str(p), str(a))
        if key not in seen and key[0] and key[1]:
            seen[key] = {
                "platform": key[0], "account_id": key[1], "label": key[1],
                "mode": "", "registry_status": "", "banned": False,
                "ban_reason": "", "last_online_at": 0.0, "in_registry": False,
            }

    accounts: List[Dict[str, Any]] = []
    tot_conv = tot_msg = tot_contacts = tot_banned = 0
    for key, base in seen.items():
        p, a = key
        try:
            counts = store.count_account_data(p, a) or {}
        except Exception:
            counts = {}
        try:
            cs = store.protocol_contacts_summary(p, a, include_chats=True) or {}
        except Exception:
            cs = {}
        reach = dict(reach_map.get(key) or {})
        r_total = sum(int(reach.get(k) or 0) for k in
                      ("both", "username_only", "phone_only", "none"))
        covered = r_total - int(reach.get("none") or 0)
        reach.update({"total": r_total, "covered": covered})
        dir_ent = directory.get(key) or directory.get((p, a)) or {}
        status = _resolve_status(
            base["registry_status"], base["banned"],
            history_only=not base["in_registry"])
        n_conv = int(counts.get("conversations") or 0)
        n_msg = int(counts.get("messages") or 0)
        n_contacts = int(cs.get("total") or 0)
        tot_conv += n_conv
        tot_msg += n_msg
        tot_contacts += n_contacts
        if base["banned"]:
            tot_banned += 1
        accounts.append({
            "platform": p,
            "account_id": a,
            "label": base["label"],
            "mode": base["mode"],
            "status": status,
            "registry_status": base["registry_status"],
            "banned": base["banned"],
            "ban_reason": base["ban_reason"],
            "last_online_at": base["last_online_at"],
            "last_ts": float((dir_ent or {}).get("last_ts") or 0),
            "counts": {
                "conversations": n_conv,
                "messages": n_msg,
                "contacts": n_contacts,
                "media_files": int(media_map.get(key) or 0),
            },
            "contacts_detail": {
                "total": n_contacts,
                "in_book": int(cs.get("in_book") or 0),
                "chat_only": int(cs.get("chat_only") or 0),
                "never_spoke": int(cs.get("never_spoke") or 0),
                "silent": int(cs.get("silent") or 0),
                "with_conversation": int(cs.get("with_conversation") or 0),
            },
            "reachability": reach,
        })

    # 封禁最前（本页存在的第一理由），其余按最近活跃降序；尾键稳定排序。
    accounts.sort(key=lambda x: (
        0 if x["banned"] else 1, -float(x.get("last_ts") or 0),
        x["platform"], x["account_id"]))

    media_totals = _media_disk_totals()
    return {
        "ok": True,
        "ts": time.time(),
        "features": _feature_flags(request.app),
        "totals": {
            "accounts": len(accounts),
            "banned": tot_banned,
            "conversations": tot_conv,
            "messages": tot_msg,
            "contacts": tot_contacts,
            "media_files": media_totals["files"],
            "media_bytes": media_totals["bytes"],
        },
        "backup": _last_backup(),
        "accounts": accounts,
    }


# ── 注册 ──────────────────────────────────────────────────────────────────────

def register_asset_center_routes(app, *, page_auth, api_auth, templates) -> None:
    """挂载账号资产中心（页面 + 只读聚合 API）。"""

    @app.get("/workspace/assets", response_class=HTMLResponse)
    async def workspace_assets_page(request: Request):
        page_auth(request)
        if _session_role(request) in _MANAGE_DENY_ROLES:
            return RedirectResponse(url="/workspace", status_code=302)
        try:
            sess = request.session
        except Exception:
            sess = {}
        ctx = {
            "user_name": sess.get("username") or "",
            "user_display_name": (sess.get("display_name")
                                  or sess.get("username") or ""),
        }
        return templates.TemplateResponse(
            request, "workspace_assets.html", ctx)

    @app.get("/api/workspace/assets/summary")
    async def api_assets_summary(request: Request, force: int = 0):
        api_auth(request)
        _require_manager(request)
        now = time.time()
        if (not force and _summary_cache["payload"] is not None
                and now - float(_summary_cache["ts"]) < _SUMMARY_TTL_SEC):
            return dict(_summary_cache["payload"], cached=True)
        payload = _build_summary(request)
        _summary_cache["ts"] = now
        _summary_cache["payload"] = payload
        return dict(payload, cached=False)

    @app.get("/api/workspace/assets/ledger")
    async def api_assets_ledger(request: Request, kinds: str = "",
                                limit: int = 50):
        api_auth(request)
        _require_manager(request)
        ks = [k.strip() for k in str(kinds or "").split(",") if k.strip()]
        if not ks:
            ks = list(_DEFAULT_LEDGER_KINDS)
        limit = max(1, min(200, int(limit or 50)))
        rows: List[Dict[str, Any]] = []
        try:
            from src.ops.ops_events import get_ops_event_store
            evs = get_ops_event_store()
            if evs is not None:
                rows = evs.recent_kinds(ks, limit=limit)
        except Exception:
            logger.debug("[asset_center] 台账读取失败", exc_info=True)
            rows = []
        return {"ok": True, "rows": rows, "kinds": ks}
