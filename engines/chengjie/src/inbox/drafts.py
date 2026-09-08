"""DraftService — 统一草稿/审批层（Phase B）。

read-through 聚合：实时直读各平台源表，归一成 UnifiedDraft；reply_drafts 表
只存 inbox 自发草稿与风险 overlay，不镜像平台草稿（避免陈旧一致性问题）。

resolve 派发：统一 draft_id = "{source_kind}:{source_id}"，反解后路由到对应
source adapter，由 adapter 翻译成平台原生 resolve 调用（runner 行为不变）。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .draft_models import (
    LinePendingAdapter,
    MessengerApprovalAdapter,
    WhatsAppPendingAdapter,
    UnifiedDraft,
)
# #160 I-1（2026-09-04）：放行决策单一入口 + 影子台账。本文件任何地方要算 L0–L4
# 都必须经 policy_decide（直接或经 risk_to_autopilot 薄壳），不许自己再写一套映射。
from .autosend_policy import decide as policy_decide, Decision as _PolicyDecision
from . import autosend_shadow_log as _shadow_log

logger = logging.getLogger(__name__)

# ── B2 敏感关键词强制升级表（不依赖 LLM，规则层兜底）─────────────────
# 格式：(pattern, forced_risk_level)  — 按顺序匹配，首中即止
# high / medium 现在只决定 **would_hold_level**（影子台账口径，#160 v2 全放行）；
# 发送行为由 autosend_policy.decide 单点决定。
# 2026-09-04 整词化：英文 \b 整词/词组（`card.*number` 这类跨词通配曾把整句
# 吃进去；`bank holiday`≠银行卡、`hotpot`≠hot），中文精确短语（裸「骗」把
# 「你骗我啦」这类打趣也算作诈骗 → 改「骗子/诈骗/被骗/骗钱/骗局」）。
# ASCII 词边界（Python `\b` 把 CJK 当 \w，「请问可以refund吗」的 refund 前后没有边界）
_LB = r"(?<![A-Za-z0-9_])"
_RB = r"(?![A-Za-z0-9_])"
_SENSITIVE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # high: 支付/账号安全/直接要钱（AI 稿里出现＝AI 自己要说付款/账号/密码）
    (re.compile(
        _LB + r"(?:refund|payment|pay\s+(?:me|now|here|first|via|by)|wire\s*transfer"
        r"|transfer\s+(?:to|it\s+to|the\s+money|funds)|bank\s+(?:card|account|transfer|details)"
        r"|card\s+number|account\s+number|password|passcode|otp|verification\s+code"
        r"|(?:send|make)\s+(?:the\s+|a\s+)?deposit|deposit\s+(?:to|into))" + _RB
        + r"|退款|退钱|付款|支付|转账|银行卡|卡号|账号密码|密码|验证码|二维码.{0,4}付|打款|汇款",
        re.IGNORECASE,
    ), "high"),
    # medium: 优惠/折扣/投诉/敏感服务
    (re.compile(
        _LB + r"(?:discount|coupon|free\s+shipping|complaint|lawyer|attorney|lawsuit"
        r"|sue\s+(?:you|them|him|her|us)|scam(?:mer)?|fraud|police)" + _RB
        + r"|优惠|折扣|免费|投诉|律师|法律|起诉|骗子|诈骗|被骗|骗钱|骗局|报警",
        re.IGNORECASE,
    ), "medium"),
]

_RISK_RANK = {"high": 3, "medium": 2, "low": 1, "unknown": 0}


def keyword_risk_hits(text: str) -> Tuple[Optional[str], List[str]]:
    """(强制 risk_level 或 None, 命中词组列表)。

    与 ``keyword_risk_level`` 同表同口径，但把**全部**命中词收齐（不首中即止）——
    影子台账要的就是「到底哪个正则在响」；level 取最高档。
    """
    t = str(text or "")
    best: Optional[str] = None
    hits: List[str] = []
    for pattern, level in _SENSITIVE_PATTERNS:
        matched = False
        for m in pattern.finditer(t):
            matched = True
            h = re.sub(r"\s+", " ", m.group(0)).strip().lower()
            if h and h not in hits:
                hits.append(h)
        if matched and (best is None
                        or _RISK_RANK.get(level, 0) > _RISK_RANK.get(best, 0)):
            best = level
    return best, hits


def keyword_risk_level(text: str) -> Optional[str]:
    """检测文本是否命中敏感关键词，返回强制 risk_level（high/medium）或 None。

    仅负责升级（不降级）：若已有 LLM 判定，调用方自行取 max。
    """
    t = str(text or "")
    for pattern, level in _SENSITIVE_PATTERNS:
        if pattern.search(t):
            return level
    return None


def _as_list(v: Any) -> List[str]:
    """risk_reasons 列在 store 里可能是 JSON 串/列表/空——统一成 list[str]。"""
    if not v:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x)]
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("["):
            try:
                import json as _json
                arr = _json.loads(s)
                return [str(x) for x in arr if str(x)] if isinstance(arr, list) else []
            except Exception:
                return []
        return [p.strip() for p in s.split(",") if p.strip()]
    return [str(v)]


def _max_risk(a: str, b: Optional[str]) -> str:
    """取两个 risk_level 中更高的（high > medium > low > unknown）。"""
    _RANK = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
    ra = _RANK.get(str(a or "unknown").lower(), 0)
    rb = _RANK.get(str(b or "unknown").lower(), 0)
    if ra >= rb:
        return str(a or "unknown")
    return str(b)


class DraftService:
    def __init__(
        self,
        *,
        inbox_store: Optional[Any] = None,
        line_services: Optional[List[Any]] = None,
        wa_services: Optional[List[Any]] = None,
        messenger_service: Optional[Any] = None,
        risk_fn: Optional[Any] = None,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._store = inbox_store
        self._cfg: Dict[str, Any] = cfg or {}
        # 同步零成本规则风险函数 risk_fn(text)->(level, reasons)（P0-c）；
        # 用于给无 overlay 的草稿实时算风险徽章，不调 LLM。
        self._risk_fn = risk_fn
        self._adapters = [
            LinePendingAdapter(line_services or []),
            WhatsAppPendingAdapter(wa_services or []),
            MessengerApprovalAdapter(messenger_service),
        ]
        self._by_kind = {a.source_kind: a for a in self._adapters}
        # inbox 草稿人工通过后的真投递回调（2026-07-29 修「通过≠发送」断链）：
        # async (draft_row: dict) -> Any，由 bootstrap 在 AutosendWorker 可投递时注入
        # （deliver 关/未启用 worker 时为 None → 保持旧「仅 DB 标记」语义）。
        self._inbox_deliver_cb: Optional[Any] = None
        # 陈旧草稿护栏阈值（小时）；随投递回调注入，未接线时不生效。见 _stale_check。
        self._stale_approve_hours: float = 0.0

    # ── 读：跨平台统一列表（read-through）─────────────────────

    def list_drafts(
        self, *, status: str = "pending", platform: str = "", limit: int = 50,
        conversation_id: str = "",
    ) -> List[Dict[str, Any]]:
        """跨源统一草稿列表。``conversation_id``（B86，实施68 P1-12）＝会话级精确
        过滤：体检/收件箱草稿面板此前拿「全平台前 N 条」再前端按 chat_key 筛——
        平台积压超过 N 时本会话的稿子掉出窗口，坐席看到「计数 4、点开空」。
        计数（reply_diagnosis 按 cid 直查 store）与列表必须同源，这里就是同源点。
        """
        conversation_id = str(conversation_id or "")
        drafts: List[UnifiedDraft] = []
        for adapter in self._adapters:
            if platform and adapter.platform != platform:
                continue
            try:
                drafts.extend(adapter.list_drafts(status=status, limit=limit))
            except Exception:
                logger.debug("source adapter %s 列举失败", adapter.source_kind, exc_info=True)
        # 会话过滤在合并层做（平台 adapter 无该参数；UnifiedDraft 恒带 conversation_id）
        if conversation_id:
            drafts = [d for d in drafts
                      if str(getattr(d, "conversation_id", "") or "") == conversation_id]
        # inbox 自发草稿（无平台表，存在 reply_drafts）。
        # 2026-07-29 修可见性断链：inbox 草稿行自带真实 platform（telegram/whatsapp…），
        # 此前仅在 platform 为空或字面 "inbox" 时列出 → 工作台按会话平台过滤
        # （/api/drafts?platform=telegram）永远看不到它们（生产实测 pending 积压 199h
        # 无人处理的根因之一）。现按行内 platform 匹配；platform 空/"inbox" 保持旧行为。
        if self._store is not None:
            try:
                for row in self._store.list_drafts(
                        source_kind="inbox", status=status, limit=limit,
                        conversation_id=conversation_id):
                    if platform and platform != "inbox" and str(
                            row.get("platform") or "") != platform:
                        continue
                    drafts.append(_row_to_unified(row))
            except Exception:
                logger.debug("inbox 自发草稿列举失败", exc_info=True)
        # 风险 overlay 合并：平台草稿挂上 reply_drafts 里的 risk 元数据
        self._merge_overlays(drafts)
        # 无 overlay 风险的草稿：用同步规则函数现算（零成本，不调 LLM）
        self._apply_quick_risk(drafts)
        # 收件箱自发草稿无 chat_name 列 → 批量从 conversations 主表回填 display_name
        # （一次 IN 查询，不逐条打库；查不到/异常保持原样，工作台回落 chat_key）
        try:
            _need = [d for d in drafts if not d.chat_name and d.conversation_id]
            if _need and self._store is not None:
                _convs = self._store.get_conversations_for_ids(
                    [d.conversation_id for d in _need])
                for d in _need:
                    _c = _convs.get(d.conversation_id) or {}
                    d.chat_name = str(_c.get("display_name") or "")
        except Exception:
            logger.debug("chat_name 回填失败（忽略）", exc_info=True)
        drafts.sort(key=lambda d: d.created_ts or 0, reverse=True)
        return [d.to_dict() for d in drafts[:limit]]

    def _apply_quick_risk(self, drafts: List[UnifiedDraft]) -> None:
        """对仍无明确风险（overlay 未覆盖）的草稿，用规则函数现算 risk + autopilot。"""
        if self._risk_fn is None:
            return
        for d in drafts:
            if d.risk_level and d.risk_level != "unknown":
                continue  # 已有 overlay/LLM 风险，不覆盖
            text = d.peer_text or d.draft_text
            if not text:
                continue
            try:
                level, reasons = self._risk_fn(text)
            except Exception:
                continue
            d.risk_level = level or "low"
            if reasons and not d.risk_reasons:
                d.risk_reasons = list(reasons)
            if not d.autopilot_level:
                d.autopilot_level = risk_to_autopilot(d.risk_level, "review")

    def _merge_overlays(self, drafts: List[UnifiedDraft]) -> None:
        if self._store is None:
            return
        for d in drafts:
            if d.source_kind == "inbox":
                continue
            try:
                ov = self._store.get_overlay(d.source_kind, d.source_id)
            except Exception:
                ov = None
            if ov:
                d.risk_level = ov.get("risk_level") or d.risk_level
                d.risk_reasons = ov.get("risk_reasons") or d.risk_reasons
                d.autopilot_level = ov.get("autopilot_level") or d.autopilot_level
                d.translated_preview = ov.get("translated_preview") or d.translated_preview

    def get_draft(self, draft_id: str) -> Optional[Dict[str, Any]]:
        kind, _, sid = str(draft_id or "").partition(":")
        if kind == "inbox" and self._store is not None:
            row = self._store.get_draft(draft_id)
            # reply_drafts 无 chat_name 列 → 单条从 conversations 主表回填 display_name
            # （查不到/异常保持原样，工作台回落 chat_key）
            try:
                if row and not row.get("chat_name") and row.get("conversation_id"):
                    _c = self._store.get_conversation(row["conversation_id"]) or {}
                    row["chat_name"] = str(_c.get("display_name") or "")
            except Exception:
                logger.debug("chat_name 回填失败（忽略）", exc_info=True)
            return row
        adapter = self._by_kind.get(kind)
        if adapter is None:
            return None
        for d in adapter.list_drafts(status="", limit=200):
            if d.source_id == sid:
                self._merge_overlays([d])
                return d.to_dict()
        return None

    # ── 写：统一 resolve 派发 ─────────────────────────────────

    def set_inbox_deliver_callback(
        self, cb: Any, *, stale_approve_hours: float = 24.0
    ) -> None:
        """注册 inbox 草稿人工通过后的真投递回调（async (draft_row)->Any）。

        由 bootstrap 在 AutosendWorker 具备投递能力（send_callback 非 None）时注入；
        未注入=保持「通过仅 DB 标记」旧语义（由 inbox.auto_draft.human_deliver 决定，
        默认开——`l2_autosend.deliver` 是「AI 可否自己发」，不该闸住人的明示决定）。

        ``stale_approve_hours``：超此龄的草稿禁止 ``approve``（原样发）——见
        ``_stale_check``。与回调同参注入，因为「能真发」与「需要陈旧护栏」是同一件事。
        """
        self._inbox_deliver_cb = cb
        self._stale_approve_hours = float(stale_approve_hours or 0)

    def _stale_check(
        self,
        draft: Dict[str, Any],
        action: str,
        *,
        force_override: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """陈旧草稿护栏：太老的稿子**原样发出**＝当场穿帮，拦下让坐席重生成。

        为什么需要（2026-07-29）：「点通过」此前只标记不发送（断链），修好后一键
        就真发。而生产实测待审队列年龄 5.0h ～ **213.1h（8.9 天）**，5/7 超 24h，
        内容又极度依赖当下情境——「我刚到家，娃正在客厅拼乐高」「我现在就在
        Seawall 这边」。8 天后原样发出去不是尴尬，是**穿帮**（人设可信度当场归零）。
        修好断链等于**激活了这个风险**，所以护栏必须同轮补上。

        规则（刻意只拦最危险的那一种）：
          - ``approve``（原样发 AI 原稿）+ 超龄 → 拦，回 409 + ``too_stale``；
          - ``edit_send``（坐席已改写过文本）→ **放行**：终稿是人写的，稿龄不再代表内容陈旧；
          - ``reject``/``autosend`` 等 → 不介入；
          - ``force_override``（主管专属）→ 放行（明知故发的逃生门）。
        阈值随投递回调一起注入（``set_inbox_deliver_callback(stale_approve_hours=)``）：
        没接线＝压根不会发出去，无需护栏，两者天然同生共死。0 或负 = 关闭。
        """
        if action != "approve" or force_override:
            return None
        max_h = float(self._stale_approve_hours or 0)
        if self._inbox_deliver_cb is None:
            return None            # 未接线＝压根不会发出去，无需任何护栏
        if str(draft.get("draft_id") or "").partition(":")[0] != "inbox":
            return None            # 其余渠道由各自 runner 消费，不走本投递链
        try:
            created = float(draft.get("created_ts") or draft.get("created_at") or 0)
        except (TypeError, ValueError):
            created = 0.0
        # created<=0（无时间戳）时 age/replied 无从判断不拦（宁可放过不误拦），
        # 但 account_offline 与稿龄无关，仍要查——判定统一收在 _approve_block_reason。
        reason = self._approve_block_reason(draft, created, max_h)
        if not reason:
            return None
        age_h = (time.time() - created) / 3600.0 if created > 0 else 0.0
        if reason == "account_offline":
            return {
                "ok": False,
                "code": 409,
                "account_offline": True,
                "stale_reason": reason,
                "error": ("该账号已退出登录，通过了也发不出去："
                          "请先在账号管理中重新登录，或改写后待账号恢复再发"),
            }
        return {
            "ok": False,
            "code": 409,
            "too_stale": True,
            "stale_reason": reason,
            "age_hours": round(age_h, 1),
            "max_age_hours": max_h,
            "error": (
                (f"这条会话在草稿生成后已经回复过了（稿龄 {age_h:.0f}h）："
                 "再原样发一遍会重复或自相矛盾，请重新生成或改写后发送")
                if reason == "replied" else
                (f"草稿已过期 {age_h:.0f} 小时（上限 {max_h:.0f}h）："
                 "原样发出会与当下情境脱节，请重新生成或改写后发送")),
        }

    #: 「已回过」判定的宽限窗（小时）：坐席分两条说（先「在的~」再发正文）是正常节奏，
    #: 不该被当成重复。超过它才认为「上一条回复已自成一轮」。
    _REPLIED_GRACE_H = 2.0

    def _approve_block_reason(
        self, draft: Dict[str, Any], created_ts: float, max_age_h: float,
    ) -> str:
        """「原样通过」会不会被拦，以及为什么。``""``＝放行。

        **护栏与工作台徽标共用同一入口**（这是本方法存在的唯一理由）：若徽标另算一套，
        坐席会看到「没标记」却被 409 拦下——比没有徽标更糟（他会以为系统坏了）。
        三档：`account_offline`＝所属账号已退出登录（通过了也发不出去，先于稿龄判定，
        与 stale 配置无关）；`age`＝单纯超龄（内容与当下情境脱节）；`replied`＝草稿
        生成后**已经回过**（再原样发一遍＝重复或自相矛盾，比过时更糟，故未超龄也拦）。
        """
        if self._account_offline_block(draft):
            return "account_offline"
        if max_age_h <= 0 or created_ts <= 0:
            return ""
        age_h = (time.time() - created_ts) / 3600.0
        if age_h > max_age_h:
            return "age"
        # 未超龄时才需要查会话（省一次 DB：超龄已成定局）
        if age_h > min(self._REPLIED_GRACE_H, max_age_h) and self._replied_after(
                draft, created_ts):
            return "replied"
        return ""

    @staticmethod
    def _account_offline_block(draft: Dict[str, Any]) -> bool:
        """草稿所属账号是否「已退出登录且无在跑 worker」——通过＝必然投递失败。

        P0 真相化（2026-07-31）配套：已退出账号的会话在收件箱保留可见，其待审草稿
        也仍在队列里；不拦的话坐席点「通过」只会撞投递失败的事后报错。判定与发送
        路由 ``_account_send_block`` 同口径：注册表 offline + 编排器无在跑 worker
        （运行时为准，防状态陈旧误拦）。conversation_id 形如
        ``{platform}:{account_id}:{chat_key}``，取前两段；解析不出/查不到一律放行。
        """
        cid = str(draft.get("conversation_id") or "")
        parts = cid.split(":", 2)
        if len(parts) < 3:
            return False
        plat, acct = parts[0], parts[1]
        if not plat or not acct or acct == "default":
            return False
        try:
            from src.integrations.account_registry import get_account_registry
            row = get_account_registry().get(plat, acct)
            if not row or str(row.get("status") or "") != "offline":
                return False
            try:
                from src.integrations.account_orchestrator import get_orchestrator
                if get_orchestrator().owns(plat, acct):
                    return False
            except Exception:
                pass
            return True
        except Exception:
            return False

    def approve_block_reason(self, draft: Dict[str, Any]) -> str:
        """公开入口：这条草稿现在点「通过」会被拦吗（``""``/``"age"``/``"replied"``）。

        供 `/api/drafts` 列表给工作台出**预判徽标**——让坐席在点之前就知道，
        而不是撞到 409 才发现。未接线（压根不会发）时恒放行，与护栏同口径。
        """
        if self._inbox_deliver_cb is None:
            return ""
        if str(draft.get("draft_id") or "").partition(":")[0] != "inbox":
            return ""
        try:
            created = float(draft.get("created_ts") or draft.get("created_at") or 0)
        except (TypeError, ValueError):
            created = 0.0   # 时间戳坏≠可跳过 account_offline（与 _stale_check 同口径）
        return self._approve_block_reason(
            draft, created, float(self._stale_approve_hours or 0))

    def conversation_replied_after(self, draft: Dict[str, Any]) -> bool:
        """该草稿生成之后，会话是否已发出过回复（公开入口，供护栏与巡检共用）。

        护栏用它拦「再发一遍」；积压巡检用它把**账目残留**（内容已人工回过、草稿行没人
        处置）与**客户真的在等**分开计数——两者处置完全不同，混成一个数字会让运维
        对告警失去信任。``created_ts`` 缺失/非法 → False（宁可算「在等」不误判已回）。
        """
        try:
            created = float(draft.get("created_ts") or draft.get("created_at") or 0)
        except (TypeError, ValueError):
            return False
        if created <= 0:
            return False
        return self._replied_after(draft, created)

    def _replied_after(self, draft: Dict[str, Any], created_ts: float) -> bool:
        """草稿生成之后，这条会话**是否已经发出过回复**（出站消息）。

        为什么单看年龄不够：坐席常走「采用文案 → 改写 → 手动发送」，而**发送路由不处置
        草稿行**（实测确认），于是那行永远 pending。之后任何窗口点「通过」＝**再发一遍**
        （多开重复提交的又一个入口）。2026-07-29 抽查生产 7 条待审确认当时 0 例孤儿，
        但机制活着——投递已接通后这就是实弹，故按「已回过」直接拦。

        刻意**只认出站**：客户连发两条（纯入站推进）只说明回复迟了，原样发仍然合理，
        拦它只会白挡坐席。取数走既有 ``list_recent_messages``（DESC 取尾），读不到就
        返回 False（宁可放过不误拦）。
        """
        store = getattr(self, "_store", None)
        cid = str(draft.get("conversation_id") or "")
        if store is None or not cid or created_ts <= 0:
            return False
        try:
            rows = store.list_recent_messages(cid, limit=30) or []
        except Exception:
            logger.debug("陈旧护栏读最近消息失败（放行）", exc_info=True)
            return False
        for m in rows:
            try:
                if not str(m.get("direction") or "").startswith("out"):
                    continue
                # B63③：投递失败留痕不算「已经回过」——客户什么也没收到，按它拦
                # approve 会挡住唯一还能把话送出去的路。
                if str(m.get("status") or "") in ("failed", "resent"):
                    continue
                if float(m.get("ts") or 0) > created_ts + 1.0:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def record_failed_outbound_mirror(
        self, conversation_id: str, text: str, reason: str = "",
    ) -> str:
        """B63③（实施64 P1-4）：自动投递终局失败 → 会话消息流留痕（供 worker 调用）。

        薄包装 store.record_failed_outbound：store 缺席/旧版无该方法/任何异常
        一律安静返回空串——留痕是可观测性增强，绝不反噬投递主链。
        ``reason``（实施72 P3）＝拦截/错误原因码，随留痕行落库供气泡自解释；
        旧 store 无 reason 形参 → TypeError 回落旧签名（原因丢弃，行为兼容）。
        """
        store = getattr(self, "_store", None)
        fn = getattr(store, "record_failed_outbound", None)
        if fn is None:
            return ""
        try:
            try:
                return str(fn(conversation_id, text, reason=reason) or "")
            except TypeError:
                return str(fn(conversation_id, text) or "")
        except Exception:  # noqa: BLE001
            logger.debug("投递失败留痕写入失败（忽略）", exc_info=True)
            return ""

    @property
    def inbox_deliver_wired(self) -> bool:
        """人工通过→真投递 是否已接线（可观测化「注入本身是静默的」这个盲区）。

        没有它的话，链路断裂只能**事后**从「有人通过过草稿但投递计数恒 0」反推
        （见 HealthWatchdog._check_human_deliver_chain）——那要等真有坐席点过通过、
        且期间客户什么也没收到。有了这个布尔值，配置漂移/注入抛异常被吞/重构漏接线
        都能在**零流量时**直接看出来。
        """
        return self._inbox_deliver_cb is not None

    def _schedule_inbox_delivery(self, draft_row: Dict[str, Any]) -> bool:
        """把人工通过的 inbox 草稿排进真投递（事件循环后台任务，不阻塞处置响应）。

        返回是否成功排入。无回调 / 无运行中事件循环（纯同步测试、离线脚本）→ False，
        行为退回「仅标记」。回调（AutosendWorker.deliver_human_approved）自吞异常并
        负责失败审计 + 事件提醒，这里绝不抛。
        """
        cb = self._inbox_deliver_cb
        if cb is None:
            return False
        text = str(draft_row.get("final_text") or draft_row.get("draft_text") or "").strip()
        if not text or not str(draft_row.get("chat_key") or ""):
            return False
        try:
            import asyncio
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("人工通过投递跳过：当前线程无事件循环 draft=%s",
                         draft_row.get("draft_id"))
            return False
        try:
            loop.create_task(cb(dict(draft_row)))
            return True
        except Exception:
            logger.warning("人工通过投递任务排入失败 draft=%s",
                           draft_row.get("draft_id"), exc_info=True)
            return False

    def resolve(self, draft_id: str, action: str, *, text: str = "", by: str = "") -> Dict[str, Any]:
        kind, _, sid = str(draft_id or "").partition(":")
        action = str(action or "").strip().lower()
        if action not in {"approve", "reject", "edit_send", "cancel"}:
            return {"ok": False, "error": f"不支持的动作: {action}", "code": 400}

        # "inbox" source 草稿（auto_generate_draft 生成）：直接按 draft_id 更新状态，
        # 无需渠道适配器。update_draft_status 自带「仅 pending/enriching 可转换」的
        # 原子闸门——两个窗口/坐席同时处置同一草稿，只有一个成功，另一个拿 409
        # （already_resolved），审计/事件/投递都只发生一次。
        if kind == "inbox" and self._store is not None:
            status = _action_to_status(action)
            try:
                updated = self._store.update_draft_status(
                    draft_id, status=status, final_text=text or "", decided_by=by
                )
                if not updated:
                    row = self._store.get_draft(draft_id)
                    if row is None:
                        return {"ok": False, "error": "草稿不存在", "code": 404}
                    return {
                        "ok": False,
                        "error": "草稿已被处理（其他窗口或同事）",
                        "code": 409,
                        "already_resolved": True,
                        "current_status": str(row.get("status") or ""),
                    }
                return {"ok": True, "draft_id": draft_id, "status": status, "source": "inbox"}
            except Exception as e:
                logger.debug("inbox draft resolve 失败: %s", e)
                return {"ok": False, "error": str(e), "code": 500}

        adapter = self._by_kind.get(kind)
        if adapter is None:
            return {"ok": False, "error": f"未知草稿来源: {kind}", "code": 400}
        result = adapter.resolve(sid, action, text=text, by=by)
        # 同步 overlay 状态（best-effort，便于审计/SLA）
        if result.get("ok") and self._store is not None:
            try:
                self._store.upsert_draft({
                    "source_kind": kind, "source_id": sid,
                    "platform": adapter.platform,
                    "status": _action_to_status(action),
                    "final_text": text or "",
                    "decided_by": by,
                })
            except Exception:
                logger.debug("overlay 状态同步失败", exc_info=True)
        return result

    # ── 风险 overlay 写入（接 Phase C1 分析结果）───────────────

    def apply_analysis(
        self, draft_id: str, analysis: Dict[str, Any], *, automation_mode: str = "review"
    ) -> Dict[str, Any]:
        """把 ChatAnalysis 风险结果写进草稿 overlay，并计算 autopilot_level。

        analysis: ChatAnalysis.to_dict() 形态（risk_level/risk_reasons/...）。
        返回 {ok, autopilot_level, autosend_allowed}。
        """
        if self._store is None:
            return {"ok": False, "error": "no store"}
        kind, _, sid = str(draft_id or "").partition(":")
        risk_level = str(analysis.get("risk_level") or "low")
        _reasons = list(analysis.get("risk_reasons") or [])
        # 单一入口：LLM 分析的 peer 风险也只经 policy 定档（shadow 下不降档、进台账）
        decision = policy_decide(
            peer_risk=risk_level, peer_reasons=_reasons,
            risk_hits=list(analysis.get("risk_hits") or []),
            automation_mode=automation_mode,
        )
        autopilot = decision.level
        _platform = (self._by_kind.get(kind).platform if kind in self._by_kind else "")
        try:
            self._store.upsert_draft({
                "source_kind": kind, "source_id": sid,
                "platform": _platform,
                "risk_level": risk_level,
                "risk_reasons": _reasons,
                "autopilot_level": autopilot,
                "translated_preview": str(analysis.get("translated_preview") or ""),
                "status": "pending",
            })
        except Exception:
            logger.debug("apply_analysis overlay 写入失败", exc_info=True)
            return {"ok": False, "error": "overlay write failed"}
        if decision.shadow is not None:
            # 账号/会话键从刚写的 overlay 行回读（analysis 载荷本身不带账号）
            _acct, _ck = "", str(analysis.get("conversation_id") or "")
            try:
                _row = self._store.get_draft(draft_id) or {}
                _acct = str(_row.get("account_id") or "")
                _ck = str(_row.get("chat_key") or _row.get("conversation_id") or _ck)
                _platform = str(_row.get("platform") or _platform)
            except Exception:
                pass
            self._record_shadow(
                decision, stage="analysis", platform=_platform, account_id=_acct,
                conv_key=_ck, draft_id=draft_id,
                text="", lang=str(analysis.get("language") or ""),
                intent=str(analysis.get("intent") or ""),
                emotion=str(analysis.get("emotion") or ""),
            )
        return {
            "ok": True,
            "autopilot_level": autopilot,
            "autosend_allowed": decision.autosend_allowed,
            "shadow": decision.shadow.to_dict() if decision.shadow else None,
        }

    # ── B2 强制风险执行 + 审计闭环 ───────────────────────────────

    def resolve_with_audit(
        self,
        draft_id: str,
        action: str,
        *,
        text: str = "",
        by: str = "",
        force_override: bool = False,
        deliver: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """带 L4 强制拦截 + 审计的统一处置入口（替代裸 resolve）。

        安全不变量：
          - L4（high risk）：approve/edit_send 被强制拦截，写 blocked 审计；
            除非 force_override=True（主管专属）。
          - L2（auto_ai + low）：autosend 动作直接走 approve，写 autosend 审计。
          - L3/L4 的所有正常审批也写审计，保证不漏记。
          - 关键词强制升级：peer_text/draft_text 命中敏感词时 risk 升级（不降级）。

        deliver（2026-07-29 修「通过≠发送」断链）：
          - None（默认）＝自动判定：人工 approve/edit_send 的 inbox 草稿在处置成功后
            排入真投递（经注入的 AutosendWorker 回调，复用出站翻译/发图指令/桌面
            受控出站同一条链）；autosend 动作不排（AutosendWorker 自己投递，防双发）。
          - True＝显式排投递（bulk-autosend 路由等「人工触发的 autosend」用）。
          - False＝显式只标记。
          LINE/WA/Messenger 渠道草稿不经此路径——各渠道 runner 消费 approved 行。
        """
        action = str(action or "").strip().lower()
        # 获取当前草稿（含 overlay 风险数据）
        draft = self.get_draft(draft_id)
        if draft is None:
            return {"ok": False, "error": "草稿不存在", "code": 404}

        stale = self._stale_check(draft, action, force_override=force_override)
        if stale is not None:
            return stale

        # 关键词强制升级 risk（peer_text 或 draft_text 命中则升 risk + autopilot）
        kw_risk = keyword_risk_level(
            str(draft.get("peer_text") or "") + " " + str(draft.get("draft_text") or "")
        )
        base_risk = str(draft.get("risk_level") or "unknown")
        effective_risk = _max_risk(base_risk, kw_risk)
        if kw_risk and effective_risk != base_risk:
            # 实时更新 overlay（best-effort）。档位只认 policy（经 risk_to_autopilot 薄壳）；
            # 草稿行不带 automation_mode 时按现有档位反推（L2 行＝auto_ai 会话），
            # 否则 shadow 下会把正在自动发的 L2 行误写成 L1（#160 单一入口收口）。
            _mode_hint = str(
                draft.get("automation_mode")
                or ("auto_ai" if str(draft.get("autopilot_level") or "") == "L2"
                    else "review"))
            autopilot_from_kw = risk_to_autopilot(effective_risk, _mode_hint)
            try:
                if self._store is not None:
                    kind, _, sid = str(draft_id or "").partition(":")
                    self._store.upsert_draft({
                        "source_kind": kind, "source_id": sid,
                        "risk_level": effective_risk,
                        "autopilot_level": autopilot_from_kw,
                        "status": "pending",
                    })
            except Exception:
                logger.debug("keyword risk overlay write failed", exc_info=True)
            draft["risk_level"] = effective_risk
            draft["autopilot_level"] = autopilot_from_kw

        autopilot = str(draft.get("autopilot_level") or "L1")
        conv_id = str(draft.get("conversation_id") or "")

        # ── L4 强制拦截 ──
        if autopilot == "L4" and action in {"approve", "edit_send", "autosend"}:
            if not force_override:
                self._write_audit(
                    draft_id, autopilot, "blocked", by,
                    reason="L4 high-risk blocked (no force_override)",
                    risk_level=effective_risk,
                    conversation_id=conv_id,
                )
                return {
                    "ok": False,
                    "error": "L4 高风险草稿已被强制拦截，需主管强制放行",
                    "code": 422,
                    "autopilot_level": "L4",
                    "blocked": True,
                }
            # force_override 路径
            self._write_audit(
                draft_id, autopilot, "force_override", by,
                reason="supervisor forced override of L4 block",
                risk_level=effective_risk,
                conversation_id=conv_id,
            )

        # ── 常规审批 (L3/L4 force_override) 写审计 ──
        elif autopilot in {"L3", "L4"} or action == "autosend":
            audit_action = "autosend" if action == "autosend" else action
            self._write_audit(
                draft_id, autopilot, audit_action, by,
                risk_level=effective_risk,
                conversation_id=conv_id,
            )

        # autosend 转为 approve 下发
        real_action = "approve" if action == "autosend" else action

        result = self.resolve(draft_id, real_action, text=text, by=by)

        # 人工通过 → 真投递（inbox 草稿此前只标记不发送：坐席点「发送」客户收不到，
        # 生产实测 14 天零人工投递皆因此断链）。仅在处置成功后排入；闸门保证同一草稿
        # 全局只会被排入一次（另一窗口/worker 的竞态方拿 409 不会走到这里）。
        _kind = str(draft_id or "").partition(":")[0]
        _should_deliver = (
            deliver if deliver is not None
            else action in ("approve", "edit_send")
        )
        if (result.get("ok") and _kind == "inbox" and _should_deliver
                and self._store is not None):
            try:
                _fresh = self._store.get_draft(draft_id)
                if _fresh:
                    result["delivery"] = (
                        "scheduled" if self._schedule_inbox_delivery(_fresh)
                        else "skipped")
            except Exception:
                logger.debug("人工通过投递调度失败（忽略）", exc_info=True)

        # P1: 草稿成功批准后，发布 draft_resolved 事件（供 CRM 同步/外部集成订阅）
        if result.get("ok") and action in ("approve", "edit_send", "autosend"):
            try:
                from src.integrations.shared.event_bus import get_event_bus
                _meta = self._store.get_conv_meta(conv_id) if self._store and conv_id else {}
                _csat = float(_meta.get("csat_score") or -1) if _meta else -1
                get_event_bus().publish("draft_resolved", {
                    "draft_id": draft_id,
                    "conversation_id": conv_id,
                    "agent_id": by,
                    "action": action,
                    "autopilot_level": autopilot,
                    "risk_level": effective_risk,
                    "intent": str((_meta or {}).get("last_intent") or ""),
                    "emotion": str((_meta or {}).get("last_emotion") or ""),
                    "csat": _csat if _csat >= 0 else None,
                    "text_preview": str(text or "")[:80],
                    "platform": str((_meta or {}).get("platform") or ""),
                })
            except Exception:
                logger.debug("P1 draft_resolved 发布失败（已忽略）", exc_info=True)

        # R3: 草稿成功批准后，按配置延迟调度 CSAT 问卷
        if result.get("ok") and action in ("approve", "edit_send", "autosend") and conv_id and self._store is not None:
            try:
                _survey_cfg = (self._cfg.get("workspace") or {}).get("csat_survey") or {}
                if bool(_survey_cfg.get("enabled", False)):
                    import uuid as _uuid
                    _delay = float(_survey_cfg.get("delay_minutes", 5)) * 60
                    _sid = "srv_" + _uuid.uuid4().hex[:10]
                    self._store.schedule_csat_survey(
                        survey_id=_sid,
                        conversation_id=conv_id,
                        draft_id=draft_id,
                        agent_id=by,
                        delay_seconds=_delay,
                    )
                    logger.info("R3 CSAT survey scheduled id=%s conv=%s delay=%.0fs", _sid, conv_id, _delay)
            except Exception:
                logger.debug("R3 CSAT survey 调度失败（已忽略）", exc_info=True)

        # Q1: 草稿成功批准后，在后台生成并存储对话摘要
        if result.get("ok") and action in ("approve", "edit_send", "autosend") and conv_id and self._store is not None:
            try:
                import threading as _th
                from src.inbox.summary import generate_conv_summary, enrich_summary_with_history
                _meta_for_sum = self._store.get_conv_meta(conv_id) or {}
                _draft_created = float(draft.get("created_at") or 0.0)

                def _do_summary():
                    try:
                        s = generate_conv_summary(
                            conv_meta=_meta_for_sum,
                            action=action,
                            agent_id=by,
                            sent_text=text or "",
                            created_ts=_draft_created,
                        )
                        s = enrich_summary_with_history(
                            s,
                            _meta_for_sum.get("intent_history") or [],
                            _meta_for_sum.get("emotion_history") or [],
                        )
                        if s:
                            self._store.update_conv_summary(conv_id, s)
                    except Exception:
                        logger.debug("Q1 摘要生成失败（已忽略）", exc_info=True)

                _th.Thread(target=_do_summary, daemon=True).start()
            except Exception:
                logger.debug("Q1 摘要线程启动失败（已忽略）", exc_info=True)

        # M1: 草稿成功处置后计算并写入 CSAT 评分
        if result.get("ok") and conv_id and self._store is not None:
            try:
                from src.inbox.csat import calculate_csat
                meta = self._store.get_conv_meta(conv_id)
                # 获取近期决策（含本次 force_override）
                recent = self._store.list_draft_audit(limit=50)
                conv_decisions = [r for r in recent if str(r.get("conversation_id") or "") == conv_id][:10]
                csat = calculate_csat(meta, conv_decisions)
                self._store.update_conv_csat(conv_id, csat)
            except Exception:
                logger.debug("M1 CSAT 计算失败（已忽略）", exc_info=True)

        # M3: 坐席人工编辑的回复质量监控（检测代发风险）
        sent_text = str(text or "").strip()
        if result.get("ok") and sent_text and action in ("approve", "edit_send"):
            try:
                from src.ai.chat_assistant_service import quick_analyze
                from src.integrations.shared.event_bus import get_event_bus
                analysis = quick_analyze(sent_text)
                reply_risk = str(analysis.get("risk_level") or "low")
                if reply_risk in ("high", "critical"):
                    # 写审计日志
                    self._write_audit(
                        draft_id, autopilot, "reply_risk_detected", by,
                        reason=f"坐席回复风险 {reply_risk}: {', '.join(analysis.get('risk_reasons') or [])}",
                        risk_level=reply_risk,
                        conversation_id=conv_id,
                    )
                    # 发布 SSE/Webhook 事件
                    get_event_bus().publish("human_reply_risk", {
                        "draft_id": draft_id,
                        "conversation_id": conv_id,
                        "agent_id": by,
                        "risk_level": reply_risk,
                        "risk_reasons": analysis.get("risk_reasons") or [],
                        "text_preview": sent_text[:80],
                        "autopilot_level": autopilot,
                    })
                    logger.warning(
                        "M3 human_reply_risk detected: draft=%s agent=%s risk=%s",
                        draft_id, by, reply_risk,
                    )
            except Exception:
                logger.debug("M3 回复质量检查失败（已忽略）", exc_info=True)

        return result

    def record_autosend_failure(
        self,
        draft_id: str,
        *,
        conversation_id: str = "",
        reason: str = "",
        autopilot_level: str = "L2",
    ) -> None:
        """全自动投递失败时写审计（action=autosend_failed），供安全条/记录弹窗可视化。

        注：草稿已在 resolve_with_audit 阶段写过 autosend（DB 标记 approved），此处补记
        平台投递失败，便于坐席看到「自动发了但没送达」。不回滚状态（宁丢不重发）。
        """
        self._write_audit(
            draft_id, autopilot_level, "autosend_failed", "autosend_worker",
            reason=reason, conversation_id=conversation_id,
        )

    def _write_audit(
        self,
        draft_id: str,
        autopilot_level: str,
        action: str,
        agent_id: str,
        *,
        reason: str = "",
        risk_level: str = "",
        conversation_id: str = "",
    ) -> None:
        if self._store is None:
            return
        try:
            self._store.record_draft_audit(
                draft_id,
                autopilot_level=autopilot_level,
                action=action,
                agent_id=agent_id,
                reason=reason,
                risk_level=risk_level,
                conversation_id=conversation_id,
            )
        except Exception:
            logger.debug("draft audit write failed", exc_info=True)

    def list_audit(
        self,
        *,
        draft_id: str = "",
        agent_id: str = "",
        since_ts: float = 0.0,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """查审计日志（透传到 store.list_draft_audit）。"""
        if self._store is None:
            return []
        try:
            return self._store.list_draft_audit(
                draft_id=draft_id, agent_id=agent_id,
                since_ts=since_ts, limit=limit,
            )
        except Exception:
            logger.debug("list_audit failed", exc_info=True)
            return []

    def risk_summary(self) -> Dict[str, Any]:
        """按 autopilot_level 统计待处理草稿数（L0–L4 分布）。"""
        all_pending = self.list_drafts(status="pending", limit=500)
        counts: Dict[str, int] = {"L0": 0, "L1": 0, "L2": 0, "L3": 0, "L4": 0, "unknown": 0}
        for d in all_pending:
            lvl = str(d.get("autopilot_level") or "unknown")
            if lvl not in counts:
                lvl = "unknown"
            counts[lvl] += 1
        return {
            "total_pending": len(all_pending),
            "by_level": counts,
        }

    # ── E2：入站消息 → 自动草稿生成 ──────────────────────────────

    def auto_generate_draft(
        self,
        conv: Dict[str, Any],
        text: str,
        *,
        automation_mode: str = "review",
        enrich: bool = False,
    ) -> Optional[str]:
        """入站新消息触发，自动生成 inbox 草稿（best-effort，不抛异常）。

        安全不变量：
          1. 若该会话已有 pending/enriching 草稿，跳过（幂等保护，避免洪泛）。
          2. 空文本跳过（媒体无文由 ingest 侧填占位符后再进入）。
          3. high → L4 拦截草稿；medium → L3 必审；low → L2 自动放行。
          4. 草稿文本取规则建议首条（<1ms，不调 LLM）。

        ``enrich=True``（Phase 2）：草稿先建为 **停泊态** ``status='enriching'``——
        不进 pending 队列、不触发 autosend、不在待审列表露出半成品。规则模板正文仅作
        兜底占位；随后由调用方（main.py 回调）异步走人设产线（与手动「生成草稿」同一条）
        生成正文，再经 ``enrich_draft`` 收尾翻成 pending + 重新定级。这样「全自动回复」
        与「手动生成草稿」共用人设/上下文/KB，不再是干瘪规则模板。

        返回生成的 draft_id，跳过/失败时返回 None。
        """
        if self._store is None:
            return None
        t = str(text or "").strip()
        if not t:
            return None

        conv_id = str(conv.get("conversation_id") or "")
        platform = str(conv.get("platform") or "")
        account_id = str(conv.get("account_id") or "default")
        chat_key = str(conv.get("chat_key") or "")
        chat_name = str(conv.get("display_name") or "")

        if not conv_id:
            return None

        # O-1 A（#252 #253 · D-O1）：会话已因停联 / 自伤冻结 → 后续入站**不起草**
        # （XAM4KV 21:03:38「Never write me again」之后又发一条的口子在这里堵上）。
        # 只读标志，判定在 stop_contact.frozen_reason；解冻由人工（unfreeze_conversation）。
        try:
            from src.inbox.stop_contact import frozen_reason as _frozen_reason, log_action as _sc_log
            _frz = _frozen_reason(self._store, conv_id)
        except Exception:
            _frz, _sc_log = "", None
        if _frz:
            if _sc_log is not None:
                _sc_log("skipped", conversation_id=conv_id, reason=_frz,
                        extra="stage=inbound_no_draft")
            return None

        try:
            # 幂等保护：同一会话已有 pending/enriching 草稿则跳过——但若 peer_text
            # 与本次入站不同，说明是陈旧草稿（客户又发了新消息），作废后重生成。
            #
            # ⚠ 媒体占位符不携带消息身份：两条**内容不同**的语音在这里都是「[语音]」
            # （转录发生在 enrich 之后，且只回写消息行、不回填草稿快照）。按文本相等
            # 判「同一条消息」会把它们判成重复 → 跳过拟稿 → 转录也不跑 → 下一条语音
            # 仍是「[语音]」→ 会话**永久死锁**（2026-08-22 WA 实锤：03:45 首条转录成功，
            # 其后 03:47 与 08-23 05:29 两条全空、零回复）。故占位符一律按陈旧处理：
            # 最坏是多拟一稿，而误跳过的代价是客户再也收不到回复。
            _existing = self._store.list_drafts(
                source_kind="inbox", conversation_id=conv_id, limit=20
            )
            _active = [d for d in (_existing or [])
                       if str(d.get("status") or "") in ("pending", "enriching")]
            if _active:
                try:
                    from src.inbox.media_enrich import is_placeholder_only
                    _no_identity = is_placeholder_only(t)
                except Exception:
                    _no_identity = False
                _stale = [d for d in _active
                          if _no_identity
                          or str(d.get("peer_text") or "").strip() != t]
                if _stale:
                    for d in _stale:
                        try:
                            self._store.update_draft_status(
                                str(d.get("draft_id") or ""),
                                status="cancelled",
                                decided_by="stale_peer",
                            )
                        except Exception:
                            logger.debug("作废陈旧草稿失败", exc_info=True)
                    _active = [d for d in _active if d not in _stale]
                if _active:
                    logger.debug(
                        "auto_generate_draft 跳过（已有 pending/enriching）: %s", conv_id)
                    return None
        except Exception:
            pass

        # R1: 检测第一条消息，自动触发问候草稿（config-gated）
        try:
            _greeting_cfg = self._cfg.get("auto_greeting", {}) if self._cfg else {}
            _greeting_enabled = bool(_greeting_cfg.get("enabled", False))
            if _greeting_enabled:
                from src.inbox.greeting import should_auto_greet, build_greeting_draft
                from src.ai.chat_assistant_service import detect_language
                _conv_meta = self._store.get_conv_meta(conv_id)
                if should_auto_greet(_conv_meta, enabled=True):
                    # 首条消息常是 Hi/emoji（检测落空）→ lang_prior 先验
                    # （账号配置/WA 国码）先于 zh 兜底，与回复产线同口径。
                    _hint = ""
                    try:
                        from src.ai.lang_prior import initial_lang_hint
                        _hint = initial_lang_hint(
                            platform=platform, account_id=account_id,
                            chat_key=chat_key, config=self._cfg or {})
                    except Exception:
                        _hint = ""
                    # P0-198（2026-08-03）：改走 evidence_lang 证据口径。
                    # 旧写法两处失真：① detect_language 落空返回 "unknown"
                    # （truthy）→ `or _hint` 永不生效，与上面注释的意图相反；
                    # ② 系统注入的「（表情：中文）」加注会把外语客户的首条
                    # 消息判成 zh → 欢迎语直接用错语言开场。
                    from src.ai.lang_policy import evidence_lang
                    _lang = evidence_lang(t) or _hint or "zh"
                    _greet_draft = build_greeting_draft(
                        conv, _lang,
                        templates_store=self._store,
                        automation_mode=automation_mode,
                    )
                    # 与主拟稿同口径：显式代龄，防同会话行沿用旧 created_at（见下）。
                    _greet_draft.setdefault("created_at", time.time())
                    _greet_id = self._store.upsert_draft(_greet_draft)
                    logger.info("R1 auto_greeting draft=%s conv=%s lang=%s", _greet_id, conv_id, _lang)
                    return _greet_id
        except Exception:
            logger.debug("R1 auto_greeting 失败（已忽略）", exc_info=True)

        try:
            from src.ai.chat_assistant_service import quick_analyze, _suggestions, detect_language
            analysis = quick_analyze(t)
            _kw_level, _kw_hits = keyword_risk_hits(t)
            risk_level = _max_risk(analysis.get("risk_level", "low"), _kw_level)
            _peer_reasons = list(analysis.get("risk_reasons") or [])
            if _kw_level and "keyword" not in _peer_reasons:
                _peer_reasons.append("keyword")
            _risk_hits = list(analysis.get("risk_hits") or [])
            _risk_hits += [h for h in _kw_hits if h not in _risk_hits]
            # 档位**只认** autosend_policy.decide（#160 v2：shadow 下风险不降档，
            # 「本会被扣」进影子台账；review/manual 档由会话档位自身决定，与风险无关）
            _decision = policy_decide(
                peer_risk=risk_level, peer_reasons=_peer_reasons,
                risk_hits=_risk_hits, automation_mode=automation_mode,
                conversation_frozen=False,   # 上方已按 frozen_reason 早退，这里必然未冻结
            )
            autopilot = _decision.level

            lang = analysis.get("language", "zh")
            intent = analysis.get("intent", "")
            emotion = analysis.get("emotion", "平稳")
            suggestions = _suggestions(t, lang=lang, intent=intent, emotion=emotion, risk=risk_level)
            draft_text = suggestions[0].text if suggestions else "感谢您的消息，我们稍后为您回复。"

            # S3: 从 conv_meta 继承 trace_id，传播到草稿
            _trace_id = ""
            try:
                _cm = self._store.get_conv_meta(conv_id) or {}
                _trace_id = str(_cm.get("trace_id") or "")
            except Exception:
                pass

            # enrich=True：停泊态落库，待人设产线补全后再翻 pending（见 enrich_draft）。
            _status = "enriching" if enrich else "pending"
            # O-1 A（D-O1）「最多一条」：硬停放行的这一条稿在 risk_reasons 带 HARD_STOP_PASS_MARK
            # （worker / enrich 对冻结会话只认它）。stop_contact → 正文换成 farewell_text（人设
            # 口吻一句话，不经 AI 生成：「I hear you… Take care」正是 AI 稿），直接 pending 不停泊
            # 不补全，另带 FAREWELL_MARK；self_harm → 照常停泊补全，AI 那一句陪伴发完即冻结。
            if getattr(_decision, "hard_stop", "") and autopilot == "L2":
                from src.inbox.stop_contact import (
                    FAREWELL_MARK, HARD_STOP_PASS_MARK, farewell_text,
                )
                _peer_reasons = list(_peer_reasons)
                if HARD_STOP_PASS_MARK not in _peer_reasons:
                    _peer_reasons.append(HARD_STOP_PASS_MARK)
                if getattr(_decision, "farewell", False):
                    draft_text = farewell_text(lang)
                    _status = "pending"
                    if FAREWELL_MARK not in _peer_reasons:
                        _peer_reasons.append(FAREWELL_MARK)
            draft_id = self._store.upsert_draft({
                "source_kind": "inbox",
                "source_id": conv_id,  # 用 conv_id 作为 source_id 保证每会话唯一幂等键
                "conversation_id": conv_id,
                "platform": platform,
                "account_id": account_id,
                "chat_key": chat_key,
                "chat_name": chat_name,
                "peer_text": t,
                "draft_text": draft_text,
                "draft_lang": lang,
                "risk_level": risk_level,
                "risk_reasons": _peer_reasons,
                "autopilot_level": autopilot,
                "status": _status,
                # 显式刷新代龄：同会话幂等键是对同一行 upsert，不带它重拟稿会沿用
                # 第一代 created_at → fresh_guard 把每版新稿都判成「入站晚于拟稿」
                # 作废 → 全自动永久哑火（2026-08-13 坐席机实锤）。
                "created_at": time.time(),
                "trace_id": _trace_id,
            })
            # Q2: 草稿创建后即时计算质量评分（亚毫秒，不阻塞流程）
            try:
                from src.inbox.quality import calculate_draft_quality
                _q_score, _q_bd = calculate_draft_quality(
                    draft_text, peer_text=t,
                    risk_level=risk_level, lang=lang,
                )
                self._store.update_draft_quality(draft_id, _q_score, _q_bd)
            except Exception:
                logger.debug("Q2 质量评分写入失败（已忽略）", exc_info=True)

            # 影子台账：旧规则本会扣（L3/L4）但已放行 → 落一行（不改变发送行为）。
            # 日志同时带 shadow=<reason> hits=<命中词>——放行了也要能从日志看出「本来会被拦」。
            _sh = _decision.shadow
            if _sh is not None:
                self._record_shadow(
                    _decision, stage="peer", platform=platform, account_id=account_id,
                    conv_key=chat_key or conv_id, draft_id=draft_id, text=draft_text,
                    lang=str(lang or ""), intent=str(intent or ""),
                    emotion=str(emotion or ""), peer_text=t,
                    peer_msg=self._latest_inbound_msg_id(conv_id, t),
                )
            logger.info(
                "auto_generate_draft OK conv=%s level=%s draft_id=%s shadow=%s hits=%s",
                conv_id, autopilot, draft_id,
                (_sh.hold_reason if _sh else "-"),
                ("|".join(_sh.risk_hits[:4]) if _sh and _sh.risk_hits else "-"),
            )
            # O-1 A（D-O1）：硬停 → 冻结会话（需人工 + 「客户要求停联」+ 档位 manual + 通知）；
            # 告别稿已在上面落库为 L2 pending，worker 放行它一条后本会话再无自动出站。
            # risk=high 非停联 → 稿已是 L1 人审，再打「需人工」让列表可见。全部 best-effort。
            try:
                _hard = str(getattr(_decision, "hard_stop", "") or "")
                if _hard:
                    from src.inbox.stop_contact import freeze_conversation, log_action as _sc_log2
                    _sc_log2("farewell" if getattr(_decision, "farewell", False) else "held",
                             conversation_id=conv_id, reason=_hard, draft_id=str(draft_id),
                             hits=_risk_hits, extra=f"level={autopilot} lang={lang}")
                    freeze_conversation(
                        self._store, platform=platform, account_id=account_id,
                        chat_key=chat_key, conversation_id=conv_id, reason=_hard,
                        hits=_risk_hits, chat_name=chat_name)
                elif getattr(_decision, "review_required", False):
                    from src.integrations.protocol_autoreply import tag_needs_human
                    tag_needs_human(
                        self._store,
                        {"platform": platform, "account_id": account_id, "chat_key": chat_key},
                        reason="high_risk", source="system")
                    logger.info(
                        "[stop-contact] conv=%s action=review reason=risk_high draft=%s hits=%s",
                        conv_id, draft_id, "|".join(_risk_hits[:4]) or "-")
            except Exception:
                logger.debug("auto_generate_draft 硬停/人审落点失败（已忽略）", exc_info=True)
            if autopilot == "L1": logger.info("auto_generate_draft L1 conv=%s draft_id=%s reason=%s", conv_id, draft_id, __import__("src.inbox.l1_reason", fromlist=["peek"]).peek(conv_id) or "-")  # D-M10（M-2 E #235）：level=L1 带 reason=（cooldown/no_persona/lang_unknown/first_contact/weak_evidence/…），原因由 autodraft_helpers 推导登记，本行只读不改判定
            # G1：向事件总线发布 draft_created，供 SSE 实时通知坐席工作台
            try:
                from src.integrations.shared.event_bus import get_event_bus
                get_event_bus().publish("draft_created", {
                    "draft_id": draft_id,
                    "conversation_id": conv_id,
                    "platform": platform,
                    "autopilot_level": autopilot,
                    "risk_level": risk_level,
                    "peer_text": t[:100],
                    "chat_name": chat_name,
                })
            except Exception:
                logger.debug("draft_created 事件发布失败", exc_info=True)
            return draft_id
        except Exception:
            logger.debug("auto_generate_draft 失败", exc_info=True)
            return None

    def _guard_late_reply_excuses(self, reply: str, draft: Dict[str, Any], draft_id: str) -> str:
        """O-1 E：沉寂 ≥72h 的首回剥编造迟回理由（persona_guard.strip_late_reply_excuses）。

        判不出沉寂（无 store / 无消息行 / 首次接触无出站 → 那不是「迟回」）→ 原稿不动。
        日志 ``[persona-guard] late_excuse=… action=strip|replace silence=…h draft=…``。
        """
        from src.inbox.humanize import silence_before_inbound
        from src.utils.persona_guard import (
            LATE_REPLY_SILENCE_HOURS, detect_late_reply_excuses, strip_late_reply_excuses,
        )
        if not detect_late_reply_excuses(reply):
            return reply
        store = self._store
        conv = str(draft.get("conversation_id") or "")
        if store is None or not conv or not hasattr(store, "list_recent_messages"):
            return reply
        try:
            draft_ts = float(draft.get("created_ts") or draft.get("created_at") or 0)
        except (TypeError, ValueError):
            draft_ts = 0.0
        if draft_ts <= 0:
            return reply
        rows = store.list_recent_messages(conv, limit=12)
        silence, _anchor = silence_before_inbound(rows, draft_ts=draft_ts)
        if silence is None or silence < LATE_REPLY_SILENCE_HOURS * 3600.0:
            return reply
        out, rep = strip_late_reply_excuses(reply)
        if rep.get("action") in ("strip", "replace"):
            logger.info(
                "[persona-guard] late_excuse=%s action=%s silence=%.1fh draft=%s",
                "|".join(str(h) for h in (rep.get("hits") or [])[:4]) or "-",
                rep.get("action"), silence / 3600.0, draft_id)
            return out
        return reply

    def enrich_draft(
        self,
        draft_id: str,
        *,
        reply_text: str,
        reply_lang: str = "",
        automation_mode: str = "review",
    ) -> bool:
        """Phase 2：用人设产线生成的正文收尾停泊态草稿（与手动「生成草稿」同源）。

        安全不变量：
          - 对生成正文二次风控（keyword_risk_level，只升不降）；
          - effective_risk = max(草稿原 peer 风险, 回复风险)；
          - autopilot 按 effective_risk + automation_mode 重算——高风险**回复**会被顶到
            L3/L4 而不再自动发（防止 AI 把敏感话术自动送出）。
          - 仅当草稿仍为 ``enriching`` 时生效（幂等 + 防与人工竞态）。

        reply_text 为空（生成失败）→ 返回 False，调用方应改用 ``release_enriching_draft``
        兜底（保留规则模板占位，降级为旧行为）。返回是否成功收尾。
        """
        if self._store is None:
            return False
        reply = str(reply_text or "").strip()
        if not reply:
            return False
        draft = self._store.get_draft(draft_id)
        if draft is None or str(draft.get("status") or "") != "enriching":
            return False
        # #32② / #145⑤：出站稿不得主动提起对方已撤回的内容（入站已含则不剥）
        try:
            from src.inbox.withdrawn_cite import apply_to_reply
            reply, _wh = apply_to_reply(
                reply,
                str(draft.get("conversation_id") or ""),
                inbound=str(draft.get("peer_text") or ""),
            )
            if _wh:
                logger.info(
                    "[withdrawn_cite] enrich_draft 剥离主动引用 draft=%s hits=%s",
                    draft_id, _wh[:5])
        except Exception:
            logger.debug("withdrawn_cite enrich skip", exc_info=True)
        base_risk = str(draft.get("risk_level") or "low")
        _peer_reasons = _as_list(draft.get("risk_reasons"))
        reply_risk, _reply_hits = keyword_risk_hits(reply)
        _reply_reasons = ["keyword"] if reply_risk else []
        # O-1 C（#253 · D-O3）陪伴域客服腔守卫：AI 稿命中「I hear you / Take care / 如有需要 /
        # 您…」→ 按人设口吻确定性改写一次（剥句 + 您→你）；剥完为空（整段客服腔）→ 以
        # reply_risk=high 经 decide 单一入口转人工审（不在这里自算档位）。销售域不启用。
        try:
            from src.utils.persona_guard import companion_tone_guard_active, rewrite_service_tone
            if companion_tone_guard_active(self._cfg or None):
                _rw, _rep = rewrite_service_tone(reply)
                _act = str(_rep.get("action") or "clean")
                if _act != "clean":
                    logger.info(
                        "[persona-guard] service_tone=%s three_part=%s cond_close=%s formal_you=%d "
                        "action=%s draft=%s",
                        "|".join(str(h) for h in (_rep.get("hits") or [])[:4]) or "-",
                        bool(_rep.get("three_part")), bool(_rep.get("conditional_close")),
                        int(_rep.get("formal_you") or 0), _act, draft_id)
                if _act == "rewrite":
                    reply = _rw
                elif _act == "review":
                    reply_risk = "high"
                    _reply_reasons = list(_reply_reasons) + ["service_tone"]
                    _reply_hits = list(_reply_hits) + [str(h) for h in (_rep.get("hits") or [])[:4]]
        except Exception:
            logger.debug("enrich_draft 客服腔守卫异常（放行原稿）", exc_info=True)
        # O-1 E（#255 8FJDUK ①）：沉寂 ≥72h 后的首回**不编造迟回理由**——AI 不知道这几天发生了
        # 什么，「buried in work / 手机坏了」都是编的；命中即剥句，剥完为空换如实「刚看到」。
        # 沉寂判定复用 humanize.silence_before_inbound（本轮入站首条 ↔ 之前最后一条出站）。
        try:
            reply = self._guard_late_reply_excuses(reply, draft, draft_id)
        except Exception:
            logger.debug("enrich_draft 迟回理由守卫异常（放行原稿）", exc_info=True)
        effective_risk = _max_risk(base_risk, reply_risk)
        # 档位只认 policy：入站风险 + AI 稿风险一起进 decide（shadow 下不降档）。
        # 台账去重：入站侧「本会被扣」已在 auto_generate_draft 落过一行，这里只在
        # **AI 稿把扣稿档位推高/新引入**时再落（stage=reply）——同一稿不记两遍。
        # O-1 A：已冻结会话上的停泊稿不得翻成第二条出站（decide 见 conversation_frozen）；
        # 冻结当刻放行的那一条（risk_reasons 带 HARD_STOP_PASS_MARK）除外——它就是「最多一条」。
        try:
            from src.inbox.stop_contact import (
                frozen_reason as _frozen_reason, is_hard_stop_pass_draft as _is_pass,
            )
            _frozen = (bool(_frozen_reason(self._store, str(draft.get("conversation_id") or "")))
                       and not _is_pass(draft))
        except Exception:
            _frozen = False
        _peer_only = policy_decide(
            peer_risk=base_risk, peer_reasons=_peer_reasons,
            automation_mode=automation_mode, conversation_frozen=_frozen,
        )
        _decision = policy_decide(
            peer_risk=base_risk, peer_reasons=_peer_reasons,
            reply_risk=reply_risk or "low", reply_reasons=_reply_reasons,
            risk_hits=_reply_hits, automation_mode=automation_mode,
            conversation_frozen=_frozen,
        )
        autopilot = _decision.level
        lang = reply_lang or str(draft.get("draft_lang") or "")
        ok = self._store.finalize_draft_enrichment(
            draft_id,
            draft_text=reply,
            autopilot_level=autopilot,
            risk_level=effective_risk,
            draft_lang=lang,
            status="pending",
        )
        if ok:
            try:
                from src.inbox.quality import calculate_draft_quality
                q, bd = calculate_draft_quality(
                    reply, peer_text=str(draft.get("peer_text") or ""),
                    risk_level=effective_risk, lang=lang,
                )
                self._store.update_draft_quality(draft_id, q, bd)
            except Exception:
                logger.debug("enrich_draft 质量分写入失败（已忽略）", exc_info=True)
            _sh = _decision.shadow
            _new_hold = _sh is not None and (
                _peer_only.shadow is None
                or _peer_only.shadow.would_hold_level != _sh.would_hold_level)
            if _new_hold:
                self._record_shadow(
                    _decision, stage="reply",
                    platform=str(draft.get("platform") or ""),
                    account_id=str(draft.get("account_id") or ""),
                    conv_key=str(draft.get("chat_key") or draft.get("conversation_id") or ""),
                    draft_id=draft_id, text=reply,
                    lang=str(lang or ""), peer_text=str(draft.get("peer_text") or ""),
                    peer_msg=self._latest_inbound_msg_id(
                        str(draft.get("conversation_id") or ""),
                        str(draft.get("peer_text") or "")),
                )
            logger.info(
                "enrich_draft OK draft_id=%s level=%s risk=%s shadow=%s hits=%s",
                draft_id, autopilot, effective_risk,
                (_sh.hold_reason if _sh else "-"),
                ("|".join(_reply_hits[:4]) if _reply_hits else "-"),
            )
        return ok

    def _record_shadow(
        self, decision: _PolicyDecision, *, stage: str, platform: str,
        account_id: str, conv_key: str, draft_id: str, text: str,
        lang: str = "", intent: str = "", emotion: str = "", peer_text: str = "",
        peer_msg: Tuple[str, str] = ("", ""),
    ) -> None:
        """影子台账落行 + stop_contact/self_harm 即时告警。best-effort，绝不影响发送。

        persona_id 在此惰性解析（只在真要落行时才算——影子命中是低频事件）：走出站链
        同一口径 ``resolve_effective_persona_id``，任何失败落空串。
        """
        sh = decision.shadow
        if sh is None:
            return
        try:
            rec = _shadow_log.build_record(
                platform=platform, account_id=account_id, conv_key=conv_key,
                draft_id=draft_id, would_hold_level=sh.would_hold_level,
                hold_reason=sh.hold_reason, peer_risk=sh.peer_risk,
                peer_reasons=sh.peer_reasons, reply_risk=sh.reply_risk,
                reply_reasons=sh.reply_reasons, risk_hits=sh.risk_hits,
                text=text, stage=stage, automation_mode=decision.automation_mode,
                policy_mode=decision.policy_mode,
                lang=lang, intent=intent, emotion=emotion, peer_text=peer_text,
                persona_id=self._shadow_persona_id(platform, account_id, conv_key),
                peer_msg_id=(peer_msg[0] if peer_msg else ""),
                peer_msg_match=(peer_msg[1] if peer_msg else ""),
            )
            _shadow_log.record(rec)
            _shadow_log.maybe_alert(rec)
        except Exception:
            logger.debug("autosend_shadow 记账失败（已忽略）", exc_info=True)

    def _latest_inbound_msg_id(self, conversation_id: str, peer_text: str = "") -> Tuple[str, str]:
        """触发拟稿的入站消息 id + 匹配方式。与 autodraft_helpers.enrich_auto_draft 的
        _peer_msg_id 同源（``list_recent_messages`` 里的入站行）。

        返回 ``(message_id, how)``：how=``exact``（正文与 peer_text 逐字相同——客户连发
        两条时不串行）/ ``newest``（没有逐字相同的，取最新入站；同秒双入站极罕见场景下
        可能取到相邻那条，一个月后用 peer_text_fp 互校）/ ``""``（会话无消息行，不猜）。
        只在影子落行时调用（低频）。"""
        if self._store is None or not conversation_id:
            return "", ""
        try:
            rows = self._store.list_recent_messages(conversation_id, limit=10) or []
        except Exception:
            return "", ""
        want = str(peer_text or "").strip()
        newest: Dict[str, Any] = {}
        for r in rows:
            if str(r.get("direction") or "in") != "in":
                continue
            if want and str(r.get("text") or "").strip() == want:
                return str(r.get("message_id") or ""), "exact"
            if float(r.get("ts") or 0) >= float(newest.get("ts") or 0):
                newest = r
        mid = str(newest.get("message_id") or "") if newest else ""
        return mid, ("newest" if mid else "")

    @staticmethod
    def _shadow_persona_id(platform: str, account_id: str, chat_key: str) -> str:
        if not platform or not account_id:
            return ""
        try:
            from src.compliance.runtime import runtime_config
            from src.ai.persona_voice import resolve_effective_persona_id
            return str(resolve_effective_persona_id(
                runtime_config() or {}, platform, account_id, chat_key) or "")
        except Exception:
            return ""

    def reconcile_shadow_outcomes(self, **kw: Any) -> int:
        """把影子台账里「放行后还没终局」的稿对照草稿行终态，写 outcome 行（sent/cancelled/…）。

        由 AutosendWorker 每 tick 末尾调用（单一收口点：worker 的 6 处取消路径与
        投递成败都体现在草稿行上，这里按结果读，不在各处埋钩子）。best-effort。
        """
        try:
            return int(_shadow_log.reconcile_outcomes(self._store, **kw))
        except Exception:
            logger.debug("autosend_shadow reconcile 失败（已忽略）", exc_info=True)
            return 0

    def release_enriching_draft(self, draft_id: str) -> bool:
        """人设补全失败的兜底：把停泊草稿原样翻 pending（保留规则模板占位，降级旧行为）。"""
        if self._store is None:
            return False
        draft = self._store.get_draft(draft_id)
        if draft is None or str(draft.get("status") or "") != "enriching":
            return False
        return self._store.finalize_draft_enrichment(
            draft_id,
            draft_text=str(draft.get("draft_text") or ""),
            autopilot_level=str(draft.get("autopilot_level") or "L1"),
            status="pending",
        )

    # ── 统计 ─────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        by_platform: Dict[str, Dict[str, int]] = {}
        total_pending = 0
        for adapter in self._adapters:
            try:
                pend = adapter.list_drafts(status="pending", limit=500)
            except Exception:
                pend = []
            by_platform[adapter.platform] = {"pending": len(pend)}
            total_pending += len(pend)
        return {"total_pending": total_pending, "by_platform": by_platform}


def _action_to_status(action: str) -> str:
    return {
        "approve": "approved", "edit_send": "approved",
        "reject": "rejected", "cancel": "cancelled",
    }.get(action, "pending")


# ── 风险分层（L0–L4）：接 Phase C1 的 ChatAnalysis.risk_level ──────────

_HIGH = "high"
_MEDIUM = "medium"


def risk_to_autopilot(risk_level: str, automation_mode: str) -> str:
    """把风险等级 + 自动化模式映射到 L0–L4 —— **薄壳，只认 autosend_policy.decide**。

    L0 仅翻译(manual) / L1 草稿待审(review/multi_choice) / L2 auto_ai 放行 /
    L3、L4 只在 ``policy_mode=enforce`` 下由风险产生。#160 v2（2026-09-04）默认
    ``shadow``：风险不再降档，high/medium 在 auto_ai 下照样 L2，「本会被扣」进影子台账。
    旧表见 ``autosend_policy.legacy_level``。**不许在别处再算一遍档位。**
    """
    return policy_decide(
        peer_risk=risk_level, automation_mode=automation_mode,
    ).level


def is_autosend_allowed(risk_level: str, automation_mode: str) -> bool:
    """是否允许自动发送＝policy 判 L2。shadow 档下仅由会话档位决定（auto_ai 即放行）；
    enforce 档下 medium/high 仍禁自动发。"""
    return risk_to_autopilot(risk_level, automation_mode) == "L2"


# 自动化档位「激进度」序（越大＝AI 介入越深/越自动）：
#   manual(不拟稿) < review(拟稿人审) < multi_choice(多选) < auto_ai(低风险自动发)
_MODE_RANK = {"manual": 0, "review": 1, "multi_choice": 2, "auto_ai": 3}


def cap_automation_mode(conv_mode: str, platform_ceiling: Optional[str]) -> str:
    """按「平台档位上限」封顶会话档位：返回二者中**较不激进**的一个。

    用途：某平台链路不稳时先降级（如 Messenger 置 ``review`` 上限）——``auto_ai`` 会话被
    降到 ``review``（AI 仍拟稿、强制人审、**绝不自动发**），但坐席显式设为 ``manual`` 的
    会话仍保持 ``manual``（尊重人工下调）。这是「关闭全自动但保留草稿助攻」的最小机制，
    优于一刀切 ``skip_platforms``（后者连草稿都不生成）。

    ``platform_ceiling`` 为空/非法 → 不封顶，原样返回 ``conv_mode``（零行为变更）。
    """
    cm = str(conv_mode or "review").lower()
    ceil = str(platform_ceiling or "").lower()
    if ceil not in _MODE_RANK:
        return cm
    if cm not in _MODE_RANK:
        cm = "review"
    return cm if _MODE_RANK[cm] <= _MODE_RANK[ceil] else ceil


def _row_to_unified(row: Dict[str, Any]) -> UnifiedDraft:
    return UnifiedDraft(
        draft_id=str(row.get("draft_id") or ""),
        source_kind=str(row.get("source_kind") or "inbox"),
        source_id=str(row.get("source_id") or ""),
        platform=str(row.get("platform") or ""),
        account_id=str(row.get("account_id") or "default"),
        chat_key=str(row.get("chat_key") or ""),
        conversation_id=str(row.get("conversation_id") or ""),
        peer_text=str(row.get("peer_text") or ""),
        draft_text=str(row.get("final_text") or row.get("draft_text") or ""),
        draft_lang=str(row.get("draft_lang") or ""),
        status=str(row.get("status") or "pending"),
        created_ts=float(row.get("created_at") or 0),
        decided_by=str(row.get("decided_by") or ""),
        risk_level=str(row.get("risk_level") or "low"),
        risk_reasons=row.get("risk_reasons") or [],
        autopilot_level=str(row.get("autopilot_level") or ""),
        translated_preview=str(row.get("translated_preview") or ""),
        trace_id=str(row.get("trace_id") or ""),
    )
