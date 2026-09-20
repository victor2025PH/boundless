# -*- coding: utf-8 -*-
"""会话级「一键接管 / 交还」（驾驶舱 P0，2026-08-13）。

产品语义（双面分工方案 v2 §2.2）：坐席对**单个会话**显式接管 → 该会话 AI 全停
→ 去原生页人工聊 → 一键交还，AI 恢复。与账号级驾驶权锁（surface_fusion pilot
lock）分工明确：锁管「整号交给原生面」（如老板自己的号），本模块管「这个客户
我来聊」——日常主力动作（一个号 100 个会话只接 1 个大客户，不该停掉整号 AI）。

接管 = 三件事打包成一个原子动作：
  1. 档位切 ``manual``（快照接管前档位，交还时原样恢复）——A 线 human_gate、
     B 线拟稿/投递、protocol 直发链全部天然让位（复用既有 automation_mode 语义，
     不新造闸门）；
  2. **取消该会话在途 pending/enriching 草稿**（防「接管前几秒拟出的 L2 稿」
     被事件驱动 worker 秒投的竞态双发——原方案没写这条，实施中深挖补上）；
  3. 会话打 ``TAKEOVER_TAG`` 标签 → 列表 chips 即刻可见「人工接管中」
     （复用既有 tag 渲染路径，零新增列表渲染代码）。

交还 = 恢复接管前档位（无显式档位则回配置全局默认）+ 摘 tag + 记时长进历史。

落盘 ``<config_dir>/takeover_registry.json``（mtime 失效缓存 + 进程锁 + 原子写，
与 surface_pilot.json 同哲学：手改文件下次读取即见、测试经 AITR_DATA_DIR 天然
隔离进 tmp）。fail-open：文件坏/缺 → 空表；除「档位写入失败」外一切软失败——
tag/草稿取消是增强，绝不阻断接管主语义。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ai_chat_assistant.takeover")

#: 会话标签（数据值，与 HANDOFF_TAG「需人工」同族的中文数据 tag，非 i18n 键）
TAKEOVER_TAG = "人工接管中"

_REGISTRY_FILE = "takeover_registry.json"
_HISTORY_CAP = 200
#: 接管时清理在途草稿的扫描上限（单会话在途稿极少，50 已远超实际）
_CANCEL_SCAN_LIMIT = 50
#: 交接提醒有效窗（交还后多久内的 AI 生成要带「衔接人工」提醒）。刻意 TTL-only
#: 不做消费计数——窗内多注入几次是无害冗余，换来零额外写盘/零竞态。
HANDBACK_NOTE_TTL_SEC = 2 * 3600.0
#: 交接记录保留条数（按 until 最新截留）
_HANDBACK_CAP = 100
#: 交接摘录：接管窗内本方出站最多保留条数 / 单条字数（喂 prompt 不是会话备份）
_HANDBACK_QUOTE_CAP = 5
_HANDBACK_QUOTE_CHARS = 80

_lock = threading.Lock()
_cache: Dict[str, Any] = {"path": "", "mtime": -1.0, "data": None}


def _registry_path() -> Path:
    from src.licensing.data_paths import config_dir
    return config_dir() / _REGISTRY_FILE


def _empty() -> Dict[str, Any]:
    return {"active": {}, "history": [], "handbacks": {}}


def _coerce(raw: Any) -> Dict[str, Any]:
    """把磁盘 JSON 归一成注册表结构（缺键/脏类型给空缺省）。"""
    data = _empty()
    if isinstance(raw, dict):
        if isinstance(raw.get("active"), dict):
            data["active"] = raw["active"]
        if isinstance(raw.get("history"), list):
            data["history"] = raw["history"]
        if isinstance(raw.get("handbacks"), dict):
            data["handbacks"] = raw["handbacks"]
    return data


def _load() -> Dict[str, Any]:
    """读注册表（mtime 失效缓存；文件缺失/坏 JSON → 空结构，绝不抛）。"""
    path = _registry_path()
    spath = str(path)
    try:
        mtime = os.path.getmtime(spath)
    except OSError:
        mtime = -1.0
    with _lock:
        if (_cache["path"] == spath and _cache["mtime"] == mtime
                and _cache["data"] is not None):
            return _cache["data"]
    data = _empty()
    if mtime >= 0:
        try:
            with open(spath, "r", encoding="utf-8") as f:
                data = _coerce(json.load(f))
        except Exception:
            logger.warning("takeover_registry.json 解析失败，按空表处理", exc_info=True)
    with _lock:
        _cache.update({"path": spath, "mtime": mtime, "data": data})
    return data


def _read_disk_locked() -> Dict[str, Any]:
    """锁内绕缓存重读磁盘最新盘面（写路径专用，防并发写互相覆盖）。"""
    try:
        with open(str(_registry_path()), "r", encoding="utf-8") as f:
            return _coerce(json.load(f))
    except Exception:
        return _empty()


def _write_locked(data: Dict[str, Any]) -> None:
    """锁内原子写 + 缓存失效。写失败向上抛（调用方决定语义）。"""
    path = _registry_path()
    data["history"] = data.get("history", [])[-_HISTORY_CAP:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, str(path))
    _cache.update({"path": "", "mtime": -1.0, "data": None})


def get_takeover(conversation_id: str) -> Optional[Dict[str, Any]]:
    """该会话当前接管记录；未接管 → None。"""
    cid = str(conversation_id or "").strip()
    if not cid:
        return None
    entry = (_load().get("active") or {}).get(cid)
    return dict(entry) if isinstance(entry, dict) else None


def list_active() -> List[Dict[str, Any]]:
    """全部进行中接管（按开始时间升序=接管最久的在前）。"""
    active = (_load().get("active") or {})
    out = [dict(v) for v in active.values() if isinstance(v, dict)]
    out.sort(key=lambda e: float(e.get("since") or 0))
    return out


def _cancel_inflight_drafts(store: Any, conversation_id: str) -> int:
    """取消该会话在途（pending/enriching）草稿，防接管后被 worker 竞态投递。

    best-effort：store 缺方法/查询异常一律 0，不阻断接管。原子闸门语义由
    ``update_draft_status`` 的 expected_statuses 保证——worker 已抢走的稿这里
    改不动（返 False），不误伤已投递终态。
    """
    cancelled = 0
    if store is None:
        return 0
    for status in ("pending", "enriching"):
        try:
            rows = store.list_drafts(
                conversation_id=conversation_id, status=status,
                limit=_CANCEL_SCAN_LIMIT) or []
        except Exception:
            logger.debug("[takeover] 列举在途草稿失败（忽略）", exc_info=True)
            continue
        for r in rows:
            draft_id = str((r or {}).get("draft_id") or "")
            if not draft_id:
                continue
            try:
                if store.update_draft_status(
                        draft_id, status="cancelled", decided_by="takeover"):
                    cancelled += 1
            except Exception:
                logger.debug("[takeover] 取消草稿 %s 失败（忽略）", draft_id,
                             exc_info=True)
    return cancelled


def _snapshot_outbound(store: Any, conversation_id: str,
                       since: float, until: float) -> List[str]:
    """接管窗内本方出站原文摘录（fail-soft；store 无此方法/抛错 → []）。

    窗口按消息 ``ts`` 闭区间 ``[since, until]``，两端各放 2s 钟偏。只取
    ``direction=out``：工作台手发 + 协议镜像 + Messenger sidecar 人工回流
    都走这条；AI 在交还之后的回复 ts 落在 until 之后，进不了摘录。
    """
    if store is None or not conversation_id:
        return []
    fn = getattr(store, "list_recent_messages", None)
    if not callable(fn):
        return []
    try:
        rows = fn(conversation_id, limit=40) or []
    except TypeError:
        try:
            rows = fn(conversation_id) or []
        except Exception:
            return []
    except Exception:
        logger.debug("[takeover] 出站摘录读取失败（忽略）", exc_info=True)
        return []
    lo = float(since)
    hi = float(until)
    quotes: List[str] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("direction") or "") != "out":
            continue
        try:
            ts = float(r.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts < (lo - 2.0) or ts > (hi + 2.0):
            continue
        text = " ".join(str(r.get("text") or "").split())
        if not text:
            mt = str(r.get("media_type") or "").strip()
            if mt:
                text = f"[{mt}]"
            else:
                continue
        if len(text) > _HANDBACK_QUOTE_CHARS:
            text = text[:_HANDBACK_QUOTE_CHARS].rstrip() + "…"
        quotes.append(text)
    return quotes[-_HANDBACK_QUOTE_CAP:]


def _set_tag(store: Any, conversation_id: str, present: bool) -> None:
    """加/摘 TAKEOVER_TAG（best-effort，失败只记日志）。"""
    if store is None:
        return
    try:
        tags = list(store.get_conv_tags(conversation_id) or [])
        if present and TAKEOVER_TAG not in tags:
            store.set_conv_tags(conversation_id, tags + [TAKEOVER_TAG])
        elif not present and TAKEOVER_TAG in tags:
            store.set_conv_tags(
                conversation_id, [t for t in tags if t != TAKEOVER_TAG])
    except Exception:
        logger.debug("[takeover] 会话标签更新失败（忽略）", exc_info=True)


def start_takeover(store: Any, conversation_id: str, *, by: str = "",
                   now: Optional[float] = None) -> Dict[str, Any]:
    """接管一个会话。返回 ``{ok, entry?, already?, reason?}``。

    - 已在接管中 → ``{ok: True, already: True, entry}``（幂等，双击/双窗口安全）；
    - 档位写入失败 → ``{ok: False, reason: "mode_set_failed"}``（核心语义失败
      即整体失败——档位没切成功等于没接管，绝不假装成功）；
    - tag/草稿取消失败不影响结果（增强项）。
    """
    cid = str(conversation_id or "").strip()
    if not cid:
        return {"ok": False, "reason": "conv_required"}
    if store is None:
        return {"ok": False, "reason": "store_unready"}
    existing = get_takeover(cid)
    if existing is not None:
        return {"ok": True, "already": True, "entry": existing}

    ts = time.time() if now is None else float(now)
    # 接管前档位快照（None=从未显式设置 → 存空串，交还时回配置全局默认）
    try:
        prev_mode = store.get_automation_mode_if_set(cid)
    except Exception:
        prev_mode = None
    try:
        store.set_automation_mode(cid, "manual", source="takeover")
    except TypeError:
        # 旧 store / 测试假件无 source 形参
        try:
            store.set_automation_mode(cid, "manual")
        except Exception:
            logger.error("[takeover] 档位切换失败 %s", cid, exc_info=True)
            return {"ok": False, "reason": "mode_set_failed"}
    except Exception:
        logger.error("[takeover] 档位切换失败 %s", cid, exc_info=True)
        return {"ok": False, "reason": "mode_set_failed"}

    cancelled = _cancel_inflight_drafts(store, cid)
    _set_tag(store, cid, True)

    entry = {
        "conversation_id": cid,
        "platform": cid.split(":", 1)[0] if ":" in cid else "",
        "by": str(by or ""),
        "since": ts,
        "prev_mode": str(prev_mode or ""),
        "cancelled_drafts": int(cancelled),
    }
    try:
        with _lock:
            data = _read_disk_locked()
            data["active"][cid] = entry
            data["history"].append({"op": "start", "ts": ts, "cid": cid,
                                    "by": entry["by"]})
            _write_locked(data)
    except Exception:
        # 注册表写失败：档位已切（核心语义成立），但计时/提醒/徽章缺失——
        # 如实降级返回 ok + registry_failed 标记，日志留根因。
        logger.error("[takeover] 注册表写入失败 %s", cid, exc_info=True)
        return {"ok": True, "entry": entry, "registry_failed": True}
    logger.info("[takeover] start %s by=%s prev_mode=%s cancelled=%d",
                cid, by, entry["prev_mode"], cancelled)
    return {"ok": True, "entry": entry}


def end_takeover(store: Any, conversation_id: str, *, by: str = "",
                 config: Optional[Dict[str, Any]] = None,
                 now: Optional[float] = None) -> Dict[str, Any]:
    """交还一个会话。返回 ``{ok, duration_sec?, restored_mode?, reason?}``。

    档位恢复语义：接管前有显式档位 → 原样恢复（review 用户回 review）；
    没有 → 恢复配置全局默认（``inbox.auto_draft.automation_mode``，缺省 auto_ai）。
    """
    cid = str(conversation_id or "").strip()
    if not cid:
        return {"ok": False, "reason": "conv_required"}
    entry = get_takeover(cid)
    if entry is None:
        return {"ok": False, "reason": "not_active"}
    if store is None:
        return {"ok": False, "reason": "store_unready"}

    ts = time.time() if now is None else float(now)
    restored = str(entry.get("prev_mode") or "")
    if not restored:
        try:
            from src.inbox.automation_mode import (
                global_automation_mode_from_config,
            )
            restored = global_automation_mode_from_config(config)
        except Exception:
            restored = "auto_ai"
    try:
        store.set_automation_mode(cid, restored, source="takeover_end")
    except TypeError:
        try:
            store.set_automation_mode(cid, restored)
        except Exception:
            logger.error("[takeover] 交还档位恢复失败 %s", cid, exc_info=True)
            return {"ok": False, "reason": "mode_set_failed"}
    except Exception:
        logger.error("[takeover] 交还档位恢复失败 %s", cid, exc_info=True)
        return {"ok": False, "reason": "mode_set_failed"}

    _set_tag(store, cid, False)
    # 显式判 None（勿用 `or`：since=0.0 是合法时刻，falsy 短路会把时长算成 0）
    _since = entry.get("since")
    since_f = float(_since) if _since is not None else ts
    duration = max(0.0, ts - since_f)
    # 摘录在注册表锁外读 store（IO 不堵接管写盘）；失败=[]，提醒退回无原文口径。
    quotes = _snapshot_outbound(store, cid, since_f, ts)
    try:
        with _lock:
            data = _read_disk_locked()
            data["active"].pop(cid, None)
            data["history"].append({
                "op": "end", "ts": ts, "cid": cid, "by": str(by or ""),
                "duration_sec": round(duration, 1),
            })
            # 交接记录（P2 交接提醒的数据源）：TTL 窗内 AI 生成注入「衔接人工」
            # 提醒。按 until 最新截留 _HANDBACK_CAP 条防无限长。
            hbs = data.get("handbacks") or {}
            hbs[cid] = {"until": ts, "since": since_f,
                        "by": str(by or ""),
                        "duration_sec": round(duration, 1),
                        "outbound": quotes}
            if len(hbs) > _HANDBACK_CAP:
                keep = sorted(hbs.items(),
                              key=lambda kv: float((kv[1] or {}).get("until") or 0),
                              reverse=True)[:_HANDBACK_CAP]
                hbs = dict(keep)
            data["handbacks"] = hbs
            _write_locked(data)
    except Exception:
        logger.error("[takeover] 注册表清除失败 %s", cid, exc_info=True)
    logger.info("[takeover] end %s by=%s duration=%.0fs restored=%s quotes=%d",
                cid, by, duration, restored, len(quotes))
    return {"ok": True, "duration_sec": round(duration, 1),
            "restored_mode": restored}


def handback_note(conversation_id: str,
                  now: Optional[float] = None,
                  store: Any = None) -> str:
    """交还后 TTL 窗内给 AI 的「交接提醒」（零 LLM 结构化文本；窗外/无记录返 ""）。

    语义（双面分工方案 v2 P2）：接管期间人工在原生页/工作台说的话，AI 接回后
    第一批回复必须**衔接**而不是当没发生过。B 线 inbox 历史里 TG/WA 出站通常
    已在；A 线 skill_manager 自有历史**不含**原生页人工话——本提醒是它唯一的
    连续性信号，所以有原文就引用，没有就诚实说「工作台没记到，别编造」。

    ``store`` 可选：生成当下按 ``[since, until]`` 再读一次出站（交还后才回流
    的镜像、且消息 ts 仍落在接管窗内的，能补进摘录）。正在接管中（未交还）
    不返提醒——那时 AI 根本不该说话（档位闸着）。
    """
    cid = str(conversation_id or "").strip()
    if not cid:
        return ""
    ts = time.time() if now is None else float(now)
    hb = (_load().get("handbacks") or {}).get(cid)
    if not isinstance(hb, dict):
        return ""
    try:
        until = float(hb.get("until") or 0)
    except (TypeError, ValueError):
        return ""
    if until <= 0 or (ts - until) > HANDBACK_NOTE_TTL_SEC or ts < until:
        return ""
    try:
        since_f = float(hb.get("since")) if hb.get("since") is not None else until
    except (TypeError, ValueError):
        since_f = until
    quotes = [str(q).strip() for q in (hb.get("outbound") or []) if str(q).strip()]
    if store is not None:
        live = _snapshot_outbound(store, cid, since_f, until)
        if live:
            quotes = live
    ago_min = max(1, int((ts - until) // 60))
    dur_min = max(1, int(float(hb.get("duration_sec") or 0) // 60))
    head = (
        f"【交接提醒】本会话刚由人工坐席接管处理过（约 {ago_min} 分钟前交还，"
        f"人工处理了约 {dur_min} 分钟）。"
    )
    if quotes:
        numbered = "；".join(f"{i + 1}. {q}" for i, q in enumerate(quotes))
        return (
            f"{head}接管期间人工发出的消息原文：{numbered}。"
            "你现在接回，必须衔接这些内容——不要自相矛盾、不要重复问已经答过的问题、"
            "不要重新自我介绍，语气自然延续，就像同一个人一直在聊。"
        )
    return (
        f"{head}接管期间工作台没有记录到人工出站（可能人只看没回，"
        "或原生页消息尚未回流）。接回后先看会话历史里最近的本方消息再开口；"
        "没有就简短接上，必须衔接现场，不要编造接管期间发生了什么、"
        "不要重新自我介绍。"
    )


def overdue_takeovers(threshold_sec: float,
                      now: Optional[float] = None) -> List[Dict[str, Any]]:
    """接管时长 ≥ 阈值的进行中记录（看门狗提醒用）。"""
    ts = time.time() if now is None else float(now)
    thr = max(0.0, float(threshold_sec or 0))
    out = []
    for e in list_active():
        _since = e.get("since")
        elapsed = ts - (float(_since) if _since is not None else ts)
        if elapsed >= thr:
            e = dict(e)
            e["elapsed_sec"] = round(elapsed, 1)
            out.append(e)
    return out


def takeover_stats() -> Dict[str, Any]:
    """观测口径：进行中数 + 历史起止计数 + 平均时长（近 _HISTORY_CAP 窗口）。"""
    data = _load()
    hist = [h for h in (data.get("history") or []) if isinstance(h, dict)]
    ends = [h for h in hist if h.get("op") == "end"]
    durations = [float(h.get("duration_sec") or 0) for h in ends]
    return {
        "active": len(data.get("active") or {}),
        "started": sum(1 for h in hist if h.get("op") == "start"),
        "ended": len(ends),
        "avg_duration_sec": (round(sum(durations) / len(durations), 1)
                             if durations else 0.0),
    }


def _reset_cache_for_tests() -> None:
    with _lock:
        _cache.update({"path": "", "mtime": -1.0, "data": None})
