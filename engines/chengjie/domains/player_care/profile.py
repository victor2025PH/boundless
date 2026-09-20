"""player_care 画像 + 阶段机（B2）。

把 hooks 每轮写进 ``user_context["_player_facts"]`` 的只读事实落到 contacts 侧旁表
``player_profiles``（src/contacts/store.py），并按事件推阶段：

    new_friend → chatting → mentioned_game → registered → depositing → active → dormant

* 阶段只前进不后退（dormant 例外：沉默超过 ``dormant_after_days`` 进入，再来消息回到
  进入前的阶段）。
* 触发信号全部来自「对方说的话」或「网关给的事实」，不做任何推断：
  - chatting：对方第 2 条入站；
  - mentioned_game：对方文本自己聊到游戏 / 无聊想玩（GAME_MENTION_RE）；
  - registered：网关 /lookup 查到该玩家（usable）；
  - depositing：网关事实里出现充值记录（detect_deposit，保守正则，待 B1.5 真样例定稿）
    或 B3 同步显式 ``note_deposit``；
  - active：不同自然日的充值 ≥ ``active_min_deposit_days``（默认 2）。
* profile_key：手机号 639… 为主键；还没拿到手机号时用 ``platform:external_id`` 占位，
  拿到后 rebind 合并（B5 handoff 同键）。

日报：``daily_report(day)`` 按我方账号（owner_slot）× 阶段给快照 + 当日计数。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.contacts.store import ContactStore

logger = logging.getLogger(__name__)

STAGE_NEW_FRIEND = "new_friend"
STAGE_CHATTING = "chatting"
STAGE_MENTIONED_GAME = "mentioned_game"
STAGE_REGISTERED = "registered"
STAGE_DEPOSITING = "depositing"
STAGE_ACTIVE = "active"
STAGE_DORMANT = "dormant"

STAGES: List[str] = [
    STAGE_NEW_FRIEND, STAGE_CHATTING, STAGE_MENTIONED_GAME, STAGE_REGISTERED,
    STAGE_DEPOSITING, STAGE_ACTIVE, STAGE_DORMANT,
]
_ORDER = {s: i for i, s in enumerate(STAGES)}
_LINEAR = STAGES[:-1]  # dormant 不在线性序里

# 对方自己聊到游戏 / 无聊想玩（英 / 中 / 菲）。故意不放任何具体游戏名，避免把厂商词表带进核心。
GAME_MENTION_RE = re.compile(
    r"(?:\bgames?\b|\bslots?\b|\bcasino\b|\bjackpot\b|\bbet(?:ting)?\b|\bspin\b|\bplay(?:ing)?\b"
    r"|\blaro\b|\bnaglalaro\b|\bmaglaro\b|\bsugal\b|\btaya\b|\bbored\b|\bboring\b"
    r"|游戏|打游戏|玩游戏|玩儿|老虎机|赌|好无聊|无聊)",
    re.IGNORECASE,
)

# 网关事实里的充值记录：关键词后跟一个 >0 的金额。宁漏勿误——只认这个形状，
# 真实 chatx_text 样例到手后（B1.5）再定稿。
_DEPOSIT_RE = re.compile(
    r"(?:deposit(?:s|ed)?|top-?ups?|recharge[sd]?|cash-?in|充值|存款|上分)"
    r"[^\n\d]{0,24}?(?P<amt>\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)


def detect_deposit(facts_text: Any) -> bool:
    """网关事实文本里是否有一条金额 > 0 的充值记录（只看事实，不推断）。"""
    for m in _DEPOSIT_RE.finditer(str(facts_text or "")):
        try:
            if float(m.group("amt").replace(",", "")) > 0:
                return True
        except ValueError:
            continue
    return False


def stage_rank(stage: str) -> int:
    return _ORDER.get(str(stage or ""), -1)


def advance(current: str, target: str) -> str:
    """线性阶段只前进：返回 rank 更高者；dormant 由 sweep / wake 单独处理。"""
    if current == STAGE_DORMANT or target == STAGE_DORMANT:
        return target if target != STAGE_DORMANT else current
    if target not in _ORDER or current not in _ORDER:
        return current if current in _ORDER else STAGE_NEW_FRIEND
    return target if _ORDER[target] > _ORDER[current] else current


def day_key(ts: float) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")


def resolve_profile_cfg(cfg_root: Any) -> Dict[str, Any]:
    """``player_care.profile`` 段（domain defaults / config.yaml），全部有缺省。"""
    root = cfg_root
    if hasattr(root, "config"):
        root = getattr(root, "config") or {}
    if not isinstance(root, dict):
        root = {}
    pc = root.get("player_care") if isinstance(root.get("player_care"), dict) else {}
    prof = pc.get("profile") if isinstance(pc.get("profile"), dict) else {}
    contacts = root.get("contacts") if isinstance(root.get("contacts"), dict) else {}
    return {
        "enabled": bool(prof.get("enabled", True)),
        "db_path": str(prof.get("db_path") or contacts.get("db_path") or ""),
        "dormant_after_days": float(prof.get("dormant_after_days", 7) or 7),
        "active_min_deposit_days": int(prof.get("active_min_deposit_days", 2) or 2),
    }


class PlayerProfileService:
    """画像 / 阶段 / 日计数的写入口，底下就是 ContactStore 的 player_* 方法。"""

    def __init__(self, store: ContactStore, *, dormant_after_days: float = 7,
                 active_min_deposit_days: int = 2) -> None:
        self.store = store
        self.dormant_after_sec = max(0.0, float(dormant_after_days)) * 86400
        self.active_min_deposit_days = max(1, int(active_min_deposit_days))

    # ── 键 ────────────────────────────────────────────────────────────────
    @staticmethod
    def make_key(phone: str, platform: str, external_id: str) -> str:
        p = str(phone or "").strip()
        if p:
            return p
        return f"{str(platform or 'unknown').strip() or 'unknown'}:{str(external_id or '').strip()}"

    def resolve_key(self, *, phone: str, platform: str, external_id: str,
                    prev_key: str = "") -> str:
        """拿到手机号且之前是占位键 → 合并到手机号键。"""
        key = self.make_key(phone, platform, external_id)
        pk = str(prev_key or "").strip()
        if pk and pk != key and ":" in pk and ":" not in key:
            try:
                self.store.rebind_player_profile(pk, key)
            except Exception:
                logger.debug("[player_care] rebind %s→%s 失败", pk, key, exc_info=True)
        return key

    # ── 每轮入站落库 ──────────────────────────────────────────────────────
    def record_inbound(
        self, *, key: str, text: str, platform: str, account_id: str, external_id: str,
        phone: str = "", uid: str = "", contact_id: str = "",
        facts: Optional[Dict[str, Any]] = None, looked_up: bool = False,
        round_kind: str = "", now: Optional[float] = None,
    ) -> Dict[str, Any]:
        now = float(now if now is not None else time.time())
        today = day_key(now)
        prev = self.store.get_player_profile(key)
        is_new = prev is None
        prev = prev or {}
        stage_before = str(prev.get("stage") or STAGE_NEW_FRIEND)
        inbound = int(prev.get("inbound_count") or 0) + 1

        stage = stage_before
        if stage == STAGE_DORMANT:
            stage = str(prev.get("stage_before_dormant") or STAGE_CHATTING)
        if inbound >= 2:
            stage = advance(stage, STAGE_CHATTING)

        fields: Dict[str, Any] = {
            "platform": platform or prev.get("platform") or "",
            "account_id": account_id or prev.get("account_id") or "",
            "external_id": external_id or prev.get("external_id") or "",
            "phone_e164": phone or prev.get("phone_e164") or "",
            "uid": uid or prev.get("uid") or "",
            "contact_id": contact_id or prev.get("contact_id") or "",
            "last_seen": int(now),
            "inbound_count": inbound,
        }

        if GAME_MENTION_RE.search(str(text or "")):
            stage = advance(stage, STAGE_MENTIONED_GAME)
            fields["mentioned_game_at"] = int(now)

        found = bool(looked_up and facts and facts.get("found"))
        if looked_up:
            fields["lookups"] = int(prev.get("lookups") or 0) + 1
            fields["last_lookup_at"] = int(now)
            fields["last_found"] = found
            fields["last_error"] = "" if found else str((facts or {}).get("error") or "")
        if found:
            ftext = str((facts or {}).get("text") or "")
            fields["facts_text"] = ftext
            games = (facts or {}).get("games") or []
            if games:
                fields["games"] = games
            if (facts or {}).get("agent"):
                fields["agent"] = str(facts["agent"])
            stage = advance(stage, STAGE_REGISTERED)
            if not prev.get("registered_at"):
                fields["registered_at"] = int(now)
            if detect_deposit(ftext):
                stage, dep_fields = self._apply_deposit(prev, stage, today, now)
                fields.update(dep_fields)
        if round_kind == "visible":
            fields["visible_hits"] = int(prev.get("visible_hits") or 0) + 1

        if stage != stage_before:
            fields["stage"] = stage
            fields["stage_changed_at"] = int(now)
            if stage_before == STAGE_DORMANT:
                fields["stage_before_dormant"] = ""

        row = self.store.upsert_player_profile(key, **fields)
        self.store.bump_player_daily(
            today, fields["account_id"],
            inbound=1, lookups=1 if looked_up else 0, found=1 if found else 0,
            visible=1 if round_kind == "visible" else 0,
            new_profiles=1 if is_new else 0,
            stage_ups=1 if (stage != stage_before and stage_rank(stage) > stage_rank(stage_before)
                            and stage_before != STAGE_DORMANT) else 0,
        )
        return row

    def _apply_deposit(self, prev: Dict[str, Any], stage: str, today: str, now: float):
        days = int(prev.get("deposit_days") or 0)
        last_day = str(prev.get("deposit_last_day") or "")
        if last_day != today:
            days += 1
        fields = {"deposit_seen_at": int(now), "deposit_days": days, "deposit_last_day": today}
        stage = advance(stage, STAGE_DEPOSITING)
        if days >= self.active_min_deposit_days:
            stage = advance(stage, STAGE_ACTIVE)
        return stage, fields

    def note_deposit(self, key: str, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """B3 同步 / 外部事件显式记一笔充值（金额不落库，只记「有」）。"""
        prev = self.store.get_player_profile(key)
        if prev is None:
            return None
        now = float(now if now is not None else time.time())
        stage_before = str(prev.get("stage") or STAGE_NEW_FRIEND)
        stage = stage_before if stage_before != STAGE_DORMANT else str(prev.get("stage_before_dormant") or STAGE_CHATTING)
        stage = advance(stage, STAGE_REGISTERED)
        stage, fields = self._apply_deposit(prev, stage, day_key(now), now)
        if stage != stage_before:
            fields["stage"] = stage
            fields["stage_changed_at"] = int(now)
            self.store.bump_player_daily(day_key(now), str(prev.get("account_id") or ""), stage_ups=1)
        return self.store.upsert_player_profile(key, **fields)

    def record_gate_hit(self, key: str, account_id: str = "", *, now: Optional[float] = None) -> None:
        now = float(now if now is not None else time.time())
        prev = self.store.get_player_profile(key)
        acct = account_id or str((prev or {}).get("account_id") or "")
        if prev is not None:
            self.store.upsert_player_profile(key, gate_hits=int(prev.get("gate_hits") or 0) + 1)
        self.store.bump_player_daily(day_key(now), acct, gate_hits=1)

    # ── B3 同步：不算入站，只刷网关事实 ────────────────────────────────
    def record_sync(self, key: str, facts: Dict[str, Any], *, now: Optional[float] = None) -> Dict[str, Any]:
        """player_sync 拉到的一次 lookup 结果写回画像。返回
        ``{"row": 新行, "events": ["registered"|"deposit"...], "stage_before": ...}``；
        不动 last_seen / inbound_count（这不是对方在说话），dormant 不唤醒。"""
        now = float(now if now is not None else time.time())
        prev = self.store.get_player_profile(key)
        if prev is None:
            return {"row": None, "events": [], "stage_before": ""}
        stage_before = str(prev.get("stage") or STAGE_NEW_FRIEND)
        found = bool(facts.get("found"))
        fields: Dict[str, Any] = {
            "lookups": int(prev.get("lookups") or 0) + 1,
            "last_lookup_at": int(now),
            "last_found": found,
            "last_error": "" if found else str(facts.get("error") or ""),
        }
        events: List[str] = []
        live = stage_before if stage_before != STAGE_DORMANT else str(prev.get("stage_before_dormant") or STAGE_CHATTING)
        stage = live
        if found:
            ftext = str(facts.get("text") or "")
            fields["facts_text"] = ftext
            if facts.get("games"):
                fields["games"] = facts["games"]
            if facts.get("agent"):
                fields["agent"] = str(facts["agent"])
            if facts.get("uid") and not prev.get("uid"):
                fields["uid"] = str(facts["uid"])
            stage = advance(stage, STAGE_REGISTERED)
            if not prev.get("registered_at"):
                fields["registered_at"] = int(now)
                events.append("registered")
            if detect_deposit(ftext):
                day = day_key(now)
                if str(prev.get("deposit_last_day") or "") != day:
                    events.append("deposit")
                stage, dep = self._apply_deposit(prev, stage, day, now)
                fields.update(dep)
        if stage_before == STAGE_DORMANT:
            # 沉默中：网关事实只刷「唤醒后该回到的阶段」，人仍算 dormant
            if stage != live:
                fields["stage_before_dormant"] = stage
        elif stage != stage_before:
            fields["stage"] = stage
            fields["stage_changed_at"] = int(now)
            self.store.bump_player_daily(day_key(now), str(prev.get("account_id") or ""), stage_ups=1)
        row = self.store.upsert_player_profile(key, **fields)
        self.store.bump_player_daily(day_key(now), str(prev.get("account_id") or ""),
                                     lookups=1, found=1 if found else 0)
        return {"row": row, "events": events, "stage_before": stage_before}

    # ── 沉默 → dormant（B3 sync 定时调；日报前也顺手扫一遍）──────────────
    def dormant_sweep(self, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """把沉默超期的画像置为 dormant，返回本次新置入的行（已 dormant 的不重复）。"""
        if self.dormant_after_sec <= 0:
            return []
        now = float(now if now is not None else time.time())
        cutoff = int(now - self.dormant_after_sec)
        out: List[Dict[str, Any]] = []
        for row in self.store.list_player_profiles(seen_before=cutoff, limit=5000):
            if row.get("stage") == STAGE_DORMANT:
                continue
            out.append(self.store.upsert_player_profile(
                row["profile_key"], stage=STAGE_DORMANT,
                stage_before_dormant=str(row.get("stage") or STAGE_NEW_FRIEND),
                stage_changed_at=int(now),
            ))
        return out

    def apply_dormancy(self, *, now: Optional[float] = None) -> int:
        return len(self.dormant_sweep(now=now))

    # ── 日报：按我方账号 × 阶段 ───────────────────────────────────────────
    def daily_report(self, day: Optional[str] = None, *, now: Optional[float] = None) -> Dict[str, Any]:
        now = float(now if now is not None else time.time())
        day = str(day or day_key(now))
        by_acct = self.store.count_player_profiles_by_stage()
        stats = {r["account_id"]: r for r in self.store.player_daily_stats(day)}
        accounts = sorted(set(by_acct) | set(stats))
        rows: List[Dict[str, Any]] = []
        totals = {s: 0 for s in STAGES}
        total_counters = {k: 0 for k in ("inbound", "lookups", "found", "visible", "gate_hits", "new_profiles", "stage_ups")}
        for acct in accounts:
            stages = {s: int(by_acct.get(acct, {}).get(s, 0)) for s in STAGES}
            counters = {k: int(stats.get(acct, {}).get(k, 0)) for k in total_counters}
            for s in STAGES:
                totals[s] += stages[s]
            for k in total_counters:
                total_counters[k] += counters[k]
            rows.append({"account_id": acct, "contacts": sum(stages.values()), "stages": stages, **counters})
        return {
            "day": day,
            "accounts": rows,
            "totals": {"contacts": sum(totals.values()), "stages": totals, **total_counters},
        }

    @staticmethod
    def render_daily_report(report: Dict[str, Any]) -> str:
        head = f"player_care 日报 {report.get('day', '')}"
        lines = [head, "账号 | 联系人 | " + " | ".join(STAGES) + " | 入站 | 查网关 | 查到 | 明用 | 数字闸 | 新增 | 升阶"]
        for r in report.get("accounts", []):
            st = r["stages"]
            lines.append(
                f"{r['account_id'] or '(unknown)'} | {r['contacts']} | "
                + " | ".join(str(st[s]) for s in STAGES)
                + f" | {r['inbound']} | {r['lookups']} | {r['found']} | {r['visible']} | {r['gate_hits']}"
                + f" | {r['new_profiles']} | {r['stage_ups']}"
            )
        t = report.get("totals", {})
        st = t.get("stages", {})
        lines.append(
            f"合计 | {t.get('contacts', 0)} | " + " | ".join(str(st.get(s, 0)) for s in STAGES)
            + f" | {t.get('inbound', 0)} | {t.get('lookups', 0)} | {t.get('found', 0)} | {t.get('visible', 0)}"
            + f" | {t.get('gate_hits', 0)} | {t.get('new_profiles', 0)} | {t.get('stage_ups', 0)}"
        )
        return "\n".join(lines)


# ── 进程级单例：hooks 拿 config 就能用；测试 / 主程序可注入 ─────────────────
_service: Optional[PlayerProfileService] = None
_service_sig: str = ""


def set_profile_service(svc: Optional[PlayerProfileService]) -> None:
    global _service, _service_sig
    _service = svc
    _service_sig = "injected" if svc is not None else ""


def get_profile_service(cfg_root: Any) -> Optional[PlayerProfileService]:
    """按配置解析 contacts.db 路径并复用同一个 ContactStore（与 contacts 子系统同一文件，
    B5 handoff 可按 contact_id 关联）。profile.enabled=false → None。"""
    global _service, _service_sig
    if _service_sig == "injected":
        return _service
    cfg = resolve_profile_cfg(cfg_root)
    if not cfg["enabled"]:
        return None
    db_path = Path(cfg["db_path"]) if cfg["db_path"] else None
    cfg_path = getattr(cfg_root, "config_path", None)
    if not cfg_path and (db_path is None or not db_path.is_absolute()):
        return None  # 没有配置文件位置（裸 dict / None）就不落盘，避免测试写进仓库
    cfg_dir = Path(cfg_path).parent if cfg_path else Path(".")
    if db_path is None:
        db_path = cfg_dir / "contacts.db"
    elif not db_path.is_absolute():
        db_path = cfg_dir / db_path
    sig = f"{db_path}|{cfg['dormant_after_days']}|{cfg['active_min_deposit_days']}"
    if _service is None or sig != _service_sig:
        try:
            store = ContactStore(db_path=db_path)
        except Exception:
            logger.warning("[player_care] 画像库打不开 %s，本轮不落库", db_path, exc_info=True)
            return None
        _service = PlayerProfileService(
            store, dormant_after_days=cfg["dormant_after_days"],
            active_min_deposit_days=cfg["active_min_deposit_days"],
        )
        _service_sig = sig
    return _service
