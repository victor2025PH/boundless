"""B5 handoff：Messenger（story_matrix 实例）→ WA / TG（player_care 实例）身份合并。

Messenger 侧引流话术里嵌了 ``handoff_tokens`` 短码（核心 ``src/contacts/handoff.py``，
72h TTL、原子消费）。对方在 WA / TG 首条消息里把码打回来 → 本模块：

1. 从消息里抓码 → 在 **本实例 contacts.db** 找；找不到再去 ``player_care.handoff.source_db_path``
   （story 实例的 contacts.db，同一台机、只做 token 消费 + 事件回写，绝不动它的画像）。
2. 消费成功：
   - 同库（``via=local``）：确保本渠道 ChannelIdentity，走核心 ``MergeService.apply_token_merge``
     把 WA/TG 身份 relink 到 Messenger 那个 Contact（真正的身份合并）；
   - 跨库（``via=source_db``）：来源库只记 ``handoff_consumed`` 事件 + 漏斗推到 LINE_ENGAGED
     （核心漏斗把「私域已接上」统一叫 LINE_*），本地画像记 ``handoff_contact_id / handoff_ci_id``。
3. **手机号为主键**：WA 的 JID 本来就是号；TG 没号时，若 Messenger 侧留资有 ``phone`` 属性，
   用它把占位键 ``telegram:<id>`` 并到手机号键（``PlayerProfileService.resolve_key`` 的 rebind）。

失败全部吞掉（记 debug），不影响回复；同一画像只合并一次。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from src.contacts.handoff import HandoffTokenService
from src.contacts.merge import MergeService
from src.contacts.journey_fsm import transit as fsm_transit
from src.contacts.models import (
    STAGE_LINE_ACCEPTED,
    STAGE_LINE_ADDED,
    STAGE_LINE_ENGAGED,
    VALID_CHANNELS,
    HandoffToken,
)
from src.contacts.store import ContactStore

from .gateway import normalize_ph_phone
from .profile import PlayerProfileService, STAGE_CHATTING, STAGE_NEW_FRIEND, advance

logger = logging.getLogger(__name__)

VIA_LOCAL = "local"
VIA_SOURCE_DB = "source_db"

_HANDOFF_STAGES = (STAGE_LINE_ADDED, STAGE_LINE_ACCEPTED, STAGE_LINE_ENGAGED)

# 合并成功那一轮给人设的提示：接上老朋友，不问“你是谁”，不提码 / 系统。
HANDOFF_CONTEXT_BLOCK = (
    "【老朋友接上了】对方是之前在 Messenger 那边聊过、刚按约定加过来的朋友（刚发了那串接头码）。"
    "自然地接上话，像老朋友换了个地方继续聊：不要问“你是谁”，不要提到码、系统、迁移这些词，"
    "不要报任何数字。"
)


def resolve_handoff_cfg(cfg_root: Any) -> Dict[str, Any]:
    """``player_care.handoff`` 段：

    enabled（默认 true）；source_db_path（story 实例 contacts.db，空 = 只查本库；相对路径按
    配置目录解析）；link_local_identity（同库命中时是否 relink ChannelIdentity，默认 true）。
    """
    root = cfg_root
    if hasattr(root, "config"):
        root = getattr(root, "config") or {}
    if not isinstance(root, dict):
        root = {}
    pc = root.get("player_care") if isinstance(root.get("player_care"), dict) else {}
    ho = pc.get("handoff") if isinstance(pc.get("handoff"), dict) else {}
    return {
        "enabled": bool(ho.get("enabled", True)),
        "source_db_path": str(ho.get("source_db_path") or "").strip(),
        "link_local_identity": bool(ho.get("link_local_identity", True)),
    }


def _token_usable(store: ContactStore, token: str) -> Optional[HandoffToken]:
    """候选码是否仍可消费。不可用时只打日志 + 异常类名（不虚构 HTTP 码）。"""
    try:
        tok = store.get_token(token)
    except Exception:
        return None
    if tok is None:
        return None  # 等同 TokenNotFound：形状像码但不在库 → 静默换下一个候选
    if tok.is_revoked:
        logger.info("[player_care] handoff 码不可用 %s (%s)", token, "TokenRevoked")
        return None
    if tok.is_consumed:
        logger.info("[player_care] handoff 码不可用 %s (%s)", token, "TokenAlreadyConsumed")
        return None
    if tok.is_expired(store._now()):  # noqa: SLF001
        logger.info("[player_care] handoff 码不可用 %s (%s)", token, "TokenExpired")
        return None
    return tok


class PlayerHandoffService:
    """一次入站消息 → 尝试 token 合并。``local`` 是本实例 contacts.db（画像同文件），
    ``source`` 是可选的 story 实例库。"""

    def __init__(self, local: ContactStore, *, source: Optional[ContactStore] = None,
                 link_local_identity: bool = True) -> None:
        self.local = local
        self.source = source
        self.link_local_identity = bool(link_local_identity)

    # ── 找码 ──────────────────────────────────────────────────────────────
    def locate(self, text: str) -> Optional[tuple]:
        """返回 (store, via, token) —— 第一个持有可用码的库；没有 → None。"""
        cands = HandoffTokenService.extract_candidates(text or "")
        if not cands:
            return None
        for store, via in ((self.local, VIA_LOCAL), (self.source, VIA_SOURCE_DB)):
            if store is None:
                continue
            for c in cands:
                tok = _token_usable(store, c)
                if tok is not None:
                    return store, via, tok
        return None

    # ── 合并 ──────────────────────────────────────────────────────────────
    def try_merge(
        self, *, text: str, platform: str, account_id: str, external_id: str,
        phone: str = "", display_name: str = "", profile: Optional[PlayerProfileService] = None,
        profile_key: str = "", now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """消息里有可用引流码 → 消费 + 合并 + 写画像。返回摘要 dict；没码 / 失败 → None。"""
        found = self.locate(text)
        if found is None:
            return None
        store, via, tok = found
        ts = float(now if now is not None else time.time())
        platform = str(platform or "").strip().lower()
        account_id = str(account_id or "").strip()
        external_id = str(external_id or "").strip()

        # 消费方标识：同库且渠道合法 → 真 ChannelIdentity；否则用可读的复合串（跨库只是留痕）
        consumer_ci_id = f"{platform or 'unknown'}:{account_id}:{external_id}"
        local_ci = None
        if via == VIA_LOCAL and self.link_local_identity and platform in VALID_CHANNELS and external_id:
            try:
                _c, local_ci, _new = store.ensure_channel_identity(
                    channel=platform, account_id=account_id, external_id=external_id,
                    display_name=display_name,
                )
                consumer_ci_id = local_ci.channel_identity_id
            except Exception:
                logger.debug("[player_care] handoff ensure_channel_identity 失败", exc_info=True)
                local_ci = None

        svc = HandoffTokenService(store)
        try:
            consumed = svc.consume(tok.token, consumed_by_ci_id=consumer_ci_id)
        except Exception as e:  # 竞态 / 过期 / 已消费
            logger.info("[player_care] handoff 码不可用 %s (%s)", tok.token, e.__class__.__name__)
            return None

        src_ci = store.get_channel_identity(consumed.issued_from_ci_id)
        src_contact_id = str(src_ci.contact_id) if src_ci else ""
        src_attrs: Dict[str, str] = {}
        src_contact = None
        if src_contact_id:
            try:
                src_attrs = store.get_contact_attributes(src_contact_id)
                src_contact = store.get_contact(src_contact_id)
            except Exception:
                src_attrs = {}

        merged_contact_id = ""
        if local_ci is not None and src_ci is not None:
            try:
                if MergeService(store).apply_token_merge(consumed, local_ci.channel_identity_id):
                    merged_contact_id = src_contact_id
            except Exception:
                logger.debug("[player_care] handoff relink 失败", exc_info=True)

        # 来源漏斗：HANDOFF_SENT → LINE_ADDED → LINE_ACCEPTED → LINE_ENGAGED（核心把私域接上统称 LINE_*）
        if src_contact_id:
            try:
                journey = store.get_journey_by_contact(src_contact_id)
                if journey is not None:
                    store.append_event(
                        journey_id=journey.journey_id, event_type="handoff_consumed",
                        payload={"channel": platform, "account_id": account_id, "via": via,
                                 "token": consumed.token},
                    )
                    for stg in _HANDOFF_STAGES:
                        fsm_transit(store, journey_id=journey.journey_id, to_stage=stg,
                                    payload={"reason": "player_handoff", "channel": platform})
            except Exception:
                logger.debug("[player_care] handoff 来源漏斗回写失败", exc_info=True)

        # 手机号主键：本轮没号 → 借 Messenger 侧留资的 phone
        phone_n = normalize_ph_phone(phone) or normalize_ph_phone(src_attrs.get("phone") or "")
        key = profile_key
        if profile is not None:
            try:
                key = profile.resolve_key(
                    phone=phone_n, platform=platform, external_id=external_id, prev_key=profile_key,
                )
                prev = profile.store.get_player_profile(key) or {}
                fields: Dict[str, Any] = {
                    "platform": platform or prev.get("platform") or "",
                    "account_id": account_id or prev.get("account_id") or "",
                    "external_id": external_id or prev.get("external_id") or "",
                    "phone_e164": phone_n or prev.get("phone_e164") or "",
                    "handoff_source": str(src_ci.channel) if src_ci else "",
                    "handoff_token": consumed.token,
                    "handoff_contact_id": src_contact_id,
                    "handoff_ci_id": consumed.issued_from_ci_id,
                    "handoff_via": via,
                    "handoff_at": int(ts),
                }
                if merged_contact_id:
                    fields["contact_id"] = merged_contact_id
                # Messenger 那边已经聊过 → 至少是 chatting（阶段只前进）
                stage_before = str(prev.get("stage") or STAGE_NEW_FRIEND)
                stage = advance(stage_before, STAGE_CHATTING)
                if stage != stage_before:
                    fields["stage"] = stage
                    fields["stage_changed_at"] = int(ts)
                profile.store.upsert_player_profile(key, **fields)
            except Exception:
                logger.debug("[player_care] handoff 画像落库失败", exc_info=True)

        out = {
            "token": consumed.token,
            "via": via,
            "source_channel": str(src_ci.channel) if src_ci else "",
            "source_contact_id": src_contact_id,
            "source_ci_id": consumed.issued_from_ci_id,
            "source_name": str(getattr(src_contact, "primary_name", "") or (src_ci.display_name if src_ci else "")),
            "merged_contact_id": merged_contact_id,
            "phone": phone_n,
            "profile_key": key,
            "ts": ts,
        }
        logger.info("[player_care] handoff 合并 via=%s src_ci=%s → %s:%s", via,
                    consumed.issued_from_ci_id, platform, external_id)
        return out


# ── 单例（按配置） ────────────────────────────────────────────────────────────
_service: Optional[PlayerHandoffService] = None
_service_sig: str = ""


def set_handoff_service(svc: Optional[PlayerHandoffService]) -> None:
    global _service, _service_sig
    _service = svc
    _service_sig = "injected" if svc is not None else ""


def get_handoff_service(cfg_root: Any, profile: Optional[PlayerProfileService]) -> Optional[PlayerHandoffService]:
    """本库 = 画像 service 的 ContactStore（同文件）；来源库按 source_db_path 另开只读用连接。"""
    global _service, _service_sig
    if _service_sig == "injected":
        return _service
    cfg = resolve_handoff_cfg(cfg_root)
    if not cfg["enabled"] or profile is None:
        return None
    src_path = ""
    if cfg["source_db_path"]:
        p = Path(cfg["source_db_path"])
        cfg_path = getattr(cfg_root, "config_path", None)
        if not p.is_absolute():
            p = (Path(cfg_path).parent if cfg_path else Path(".")) / p
        src_path = os.path.normpath(str(p))
    sig = f"{id(profile.store)}|{src_path}|{cfg['link_local_identity']}"
    if _service is None or sig != _service_sig:
        source = None
        if src_path:
            if Path(src_path).exists():
                try:
                    source = ContactStore(db_path=Path(src_path))
                except Exception:
                    logger.warning("[player_care] handoff 来源库打不开 %s", src_path, exc_info=True)
            else:
                logger.info("[player_care] handoff source_db_path 不存在，只查本库：%s", src_path)
        _service = PlayerHandoffService(profile.store, source=source,
                                        link_local_identity=cfg["link_local_identity"])
        _service_sig = sig
    return _service
