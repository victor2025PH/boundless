"""坐席 presence + 会话租约协调器。

优先持久化到 InboxStore；store 不可用时回落进程内 dict（单实例开发/测试）。
所有状态变更经 EventBus 广播，供 /api/workspace/stream SSE 消费。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

VALID_STATUS = {"online", "busy", "offline"}


class AgentCoordinator:
    """坐席协作状态机（薄封装 InboxStore 或内存）。"""

    #: 窗口条目视为存活的窗口期（前端心跳 30s；3 倍冗余容忍丢拍）
    WINDOW_TTL_SEC = 90.0

    def __init__(
        self,
        *,
        store=None,
        claim_ttl_sec: float = 900,
        presence_stale_sec: float = 120,
    ) -> None:
        self._store = store
        self.claim_ttl_sec = max(60.0, float(claim_ttl_sec or 900))
        self.presence_stale_sec = max(30.0, float(presence_stale_sec or 120))
        # 内存回落
        self._presence: Dict[str, Dict[str, Any]] = {}
        self._claims: Dict[str, Dict[str, Any]] = {}
        # P2 多开观测（2026-07-29）：agent_id → {win_id: {ts, path, standby}}。
        # 刻意进程内不落库：窗口是秒级易逝状态，心跳丢失 TTL 自清；重启清零无妨。
        self._windows: Dict[str, Dict[str, Dict[str, Any]]] = {}

    @classmethod
    def from_request(cls, request, config_manager=None) -> "AgentCoordinator":
        store = getattr(request.app.state, "inbox_store", None)
        ttl, stale = 900.0, 120.0
        try:
            cfg = (getattr(config_manager, "config", None) or {}) if config_manager else {}
            ws = (cfg.get("workspace") or {}) if isinstance(cfg, dict) else {}
            ttl = float(ws.get("claim_ttl_sec") or ttl)
            stale = float(ws.get("presence_stale_sec") or stale)
        except Exception:
            pass
        coord = getattr(request.app.state, "agent_coordinator", None)
        if isinstance(coord, AgentCoordinator):
            coord.claim_ttl_sec = max(60.0, ttl)
            coord.presence_stale_sec = max(30.0, stale)
            if store is not None:
                coord._store = store
            return coord
        inst = cls(store=store, claim_ttl_sec=ttl, presence_stale_sec=stale)
        request.app.state.agent_coordinator = inst
        return inst

    def _publish(self, event_type: str, data: Dict[str, Any]) -> None:
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish(event_type, data)
        except Exception:
            logger.debug("EventBus publish 失败", exc_info=True)

    def _now(self) -> float:
        return time.time()

    def _purge_mem_claims(self) -> None:
        now = self._now()
        dead = [k for k, v in self._claims.items() if float(v.get("expires_at") or 0) < now]
        for k in dead:
            self._claims.pop(k, None)

    # ── presence ─────────────────────────────────────────────

    def get_presence(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """单坐席 presence 行（store/内存同口径；无行 → None）。"""
        if self._store is not None:
            return self._store.get_agent_presence(agent_id)
        return self._presence.get(agent_id)

    def set_presence(
        self,
        agent_id: str,
        *,
        display_name: str = "",
        status: str = "online",
    ) -> Dict[str, Any]:
        st = str(status or "online").lower()
        if st not in VALID_STATUS:
            raise ValueError(f"invalid status: {st}")
        # 审计差分用旧值（2026-08-19：状态切换此前零审计，主管无法追溯
        # 「昨晚为什么没人接单」）。读失败按空处理，绝不阻断写入。
        try:
            prev_st = str((self.get_presence(agent_id) or {}).get("status") or "")
        except Exception:
            prev_st = ""
        if self._store is not None:
            row = self._store.upsert_agent_presence(
                agent_id, display_name=display_name, status=st,
            )
        else:
            now = self._now()
            row = {
                "agent_id": agent_id,
                "display_name": display_name or self._presence.get(agent_id, {}).get("display_name", ""),
                "status": st,
                "last_seen_at": now,
                "updated_at": now,
            }
            self._presence[agent_id] = row
        if prev_st != st:
            # 只记「状态真变化」（heartbeat 同态保活每 30s 一拍，逐拍记账＝纯噪音）
            try:
                from src.ops.ops_events import get_ops_event_store
                ev = get_ops_event_store()
                if ev is not None:
                    ev.record(
                        "presence_change",
                        account_id=str(agent_id or ""),
                        platform="workspace",
                        reason=f"{prev_st or 'new'}->{st}",
                        detail=str(display_name or ""),
                    )
            except Exception:
                logger.debug("presence 审计落库失败（忽略）", exc_info=True)
        self._publish("agent_presence", row)
        return row

    def heartbeat(
        self,
        agent_id: str,
        *,
        display_name: str = "",
        status: str = "",
    ) -> Dict[str, Any]:
        cur_status = status or "online"
        if self._store is not None:
            prev = self._store.get_agent_presence(agent_id)
            if not status and prev:
                cur_status = prev.get("status") or "online"
        elif agent_id in self._presence and not status:
            cur_status = self._presence[agent_id].get("status") or "online"
        return self.set_presence(agent_id, display_name=display_name, status=cur_status)

    def list_presence(self) -> List[Dict[str, Any]]:
        if self._store is not None:
            return self._store.list_agent_presence(active_within_sec=self.presence_stale_sec)
        # 口径与 store 分支统一（presence_policy.roster_visible：心跳窗口内三态如实
        # 返回，含手选「离开」）——此前内存分支剔 offline、SQLite 分支不剔，两个
        # 分支行为分叉，测试环境与生产各看一套。
        from src.workspace.presence_policy import roster_visible
        cutoff = self._now() - self.presence_stale_sec
        return [
            v for v in self._presence.values()
            if float(v.get("last_seen_at") or 0) >= cutoff
            and roster_visible(v.get("status"))
        ]

    # ── P2 多开观测：坐席窗口注册表（进程内，TTL 自清）───────────

    def record_window(
        self,
        agent_id: str,
        win_id: str,
        *,
        path: str = "",
        standby: bool = False,
    ) -> None:
        """记录一次窗口心跳（workspace_base 每 30s 随 presence heartbeat 上报）。

        win_id 来自前端 __wsMultiWin（每窗口一次性 uuid）；同坐席多窗口各自上报，
        据此可见「谁开了几个窗口、几个在待机」——多开治理的读数面 + 席位模型的数据地基。
        """
        aid = str(agent_id or "").strip()
        wid = str(win_id or "").strip()
        if not aid or not wid:
            return
        wins = self._windows.setdefault(aid, {})
        wins[wid] = {
            "ts": self._now(),
            "path": str(path or "")[:80],
            "standby": bool(standby),
        }
        self._purge_windows()

    def _purge_windows(self) -> None:
        cutoff = self._now() - self.WINDOW_TTL_SEC
        for aid in list(self._windows):
            wins = self._windows[aid]
            for wid in [w for w, rec in wins.items()
                        if float(rec.get("ts") or 0) < cutoff]:
                wins.pop(wid, None)
            if not wins:
                self._windows.pop(aid, None)

    def windows_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """{agent_id: {windows, standby, paths}}——仅统计 TTL 内存活窗口。"""
        self._purge_windows()
        out: Dict[str, Dict[str, Any]] = {}
        for aid, wins in self._windows.items():
            if not wins:
                continue
            out[aid] = {
                "windows": len(wins),
                "standby": sum(1 for r in wins.values() if r.get("standby")),
                "paths": sorted({str(r.get("path") or "") for r in wins.values()}),
            }
        return out

    # ── claims ───────────────────────────────────────────────

    def get_claim(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        if self._store is not None:
            return self._store.get_conversation_claim(conversation_id)
        self._purge_mem_claims()
        return self._claims.get(conversation_id)

    def list_claims(self) -> List[Dict[str, Any]]:
        if self._store is not None:
            return self._store.list_conversation_claims()
        self._purge_mem_claims()
        return list(self._claims.values())

    def claim(
        self,
        conversation_id: str,
        agent_id: str,
        *,
        agent_name: str = "",
        force: bool = False,
    ) -> Dict[str, Any]:
        if self._store is not None:
            result = self._store.set_conversation_claim(
                conversation_id, agent_id,
                agent_name=agent_name, ttl_sec=self.claim_ttl_sec, force=force,
            )
        else:
            self._purge_mem_claims()
            existing = self._claims.get(conversation_id)
            if existing and existing.get("agent_id") != agent_id and not force:
                result = {"ok": False, "reason": "already_claimed", "claim": existing}
            else:
                now = self._now()
                claim = {
                    "conversation_id": conversation_id,
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "claimed_at": now,
                    "expires_at": now + self.claim_ttl_sec,
                }
                self._claims[conversation_id] = claim
                result = {"ok": True, "claim": claim}
        if result.get("ok"):
            self._publish("conversation_claim", {
                "action": "claimed",
                "conversation_id": conversation_id,
                "claim": result.get("claim"),
            })
        return result

    def renew_claim(self, conversation_id: str, agent_id: str) -> Dict[str, Any]:
        if self._store is not None:
            result = self._store.renew_conversation_claim(
                conversation_id, agent_id, ttl_sec=self.claim_ttl_sec,
            )
        else:
            self._purge_mem_claims()
            existing = self._claims.get(conversation_id)
            if not existing:
                result = {"ok": False, "reason": "not_claimed"}
            elif existing.get("agent_id") != agent_id:
                result = {"ok": False, "reason": "not_owner", "claim": existing}
            else:
                existing["expires_at"] = self._now() + self.claim_ttl_sec
                result = {"ok": True, "claim": existing}
        return result

    def release_claim(
        self,
        conversation_id: str,
        agent_id: str,
        *,
        force: bool = False,
    ) -> Dict[str, Any]:
        if self._store is not None:
            result = self._store.release_conversation_claim(
                conversation_id, agent_id, force=force,
            )
        else:
            self._purge_mem_claims()
            existing = self._claims.get(conversation_id)
            if not existing:
                result = {"ok": True, "released": False}
            elif existing.get("agent_id") != agent_id and not force:
                result = {"ok": False, "reason": "not_owner", "claim": existing}
            else:
                self._claims.pop(conversation_id, None)
                result = {"ok": True, "released": True, "conversation_id": conversation_id}
        if result.get("ok") and result.get("released"):
            self._publish("conversation_claim", {
                "action": "released",
                "conversation_id": conversation_id,
                "agent_id": agent_id,
            })
        return result


def web_funnel_snapshot(request, config_manager=None) -> Dict[str, Any]:
    """今日 web 渠道漏斗迷你指标（best-effort）。"""
    import time as _time
    from datetime import datetime, timezone

    day_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    ).timestamp()
    out: Dict[str, Any] = {
        "day_start": day_start,
        "web_sessions": 0,
        "web_inbound": 0,
        "web_outbound_ai": 0,
        "web_outbound_agent": 0,
        "handoff_sent": 0,
        "line_converted": 0,
        "by_stage": {},
        # 子系统是否启用——让前端区分"未启用"与"启用但今日无数据"，避免误读空心 0。
        "contacts_enabled": False,
        "stage_source": "unavailable",
    }
    store = getattr(request.app.state, "inbox_store", None)
    if store is not None:
        try:
            convs = store.list_conversations(limit=500, platform="web")
            out["web_sessions"] = len(convs)
            for c in convs:
                cid = str(c.get("conversation_id") or "")
                if not cid:
                    continue
                msgs = store.list_messages(cid, limit=200)
                for m in msgs:
                    ts = float(m.get("ts") or 0)
                    if ts < day_start:
                        continue
                    if m.get("direction") == "in":
                        out["web_inbound"] += 1
                    elif m.get("direction") == "out":
                        # 粗分：display_name 空 / by agent 标记在 source 不可见时用 automation
                        out["web_outbound_ai"] += 1
        except Exception:
            logger.debug("web funnel inbox 统计失败", exc_info=True)

    contacts = getattr(request.app.state, "contacts", None)
    cstore = getattr(contacts, "store", None) if contacts else None
    if cstore is not None:
        out["contacts_enabled"] = True
        out["stage_source"] = "contacts"
        try:
            with cstore._lock:  # noqa: SLF001
                rows = cstore._conn.execute(  # noqa: SLF001
                    "SELECT j.funnel_stage, COUNT(*) AS n FROM journeys j "
                    "INNER JOIN channel_identities ci ON ci.contact_id = j.contact_id "
                    "WHERE ci.channel = 'web' GROUP BY j.funnel_stage",
                ).fetchall()
            out["by_stage"] = {r["funnel_stage"]: r["n"] for r in rows}
            out["handoff_sent"] = sum(
                n for st, n in out["by_stage"].items()
                if st in {"HANDOFF_SENT", "LINE_ADDED", "LINE_ACCEPTED", "LINE_ENGAGED", "BONDED", "CONVERTED"}
            )
            out["line_converted"] = sum(
                n for st, n in out["by_stage"].items()
                if st in {"LINE_ENGAGED", "BONDED", "CONVERTED"}
            )
        except Exception:
            logger.debug("web funnel contacts 统计失败", exc_info=True)
    out["ts"] = _time.time()
    return out
