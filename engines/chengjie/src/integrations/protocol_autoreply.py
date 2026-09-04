"""协议账号 7×24 自动回复（Phase 3）。

protocol 模式 worker（Telegram pyrogram / WhatsApp Baileys）收到入站消息后,
可在服务端**无人值守**生成并发送回复——这是 RPA/桌面 webview 之外、真正能挂大量
账号、跨会话全程托管的路径。

安全设计（双闸门 + 风控 + 冷却）：
  - 全局闸门 ``config.protocol_autoreply.enabled``（默认 False）
  - 账号闸门 registry ``meta.auto_reply``（默认 False）——两者皆开才会自动发
  - 高风险（支付/密码/账号安全，复用 keyword_risk_level）→ 不发,转人工
  - 每会话冷却 + 同文入站短窗去重 → 防刷屏/回环；超时后同文可再回
    （2026-07-22：原「同文永久去重」导致客户连发两句「你在干嘛」第二句永静默）

生成复用生产级入口 ``SkillManager.process_message``（与真 bot/RPA 同一条产线,
带人设/意图/策略/KB）；发送复用 ``AccountOrchestrator.send``（并回写收件箱线程）。

本模块核心 ``run_autoreply`` 全程依赖注入（registry/generate/send/risk_fn）,
不依赖 FastAPI/pyrogram,可纯单测。``build_reply_hook(app)`` 在 web 层接线生产依赖。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# 每会话最近一次自动回复：key=platform:account_id:chat_key → (last_inbound_text, ts)
_last_reply: Dict[str, tuple] = {}
# 每会话最近一次**成功发出**时刻：key → send_ts（P2 连发间隔地板，2026-08-12）。
# 与 _last_reply 语义不同：那是「最近处理的入站」占位（生成期即被覆写，发送失败也留痕），
# 连发地板要对照的是「上一条真的发出去多久了」——只在 send 成功后落点。
# 已知边界（刻意不解）：两条入站并发在途时本表还没有第一条的 send_ts，地板兜不住
# 乱序/并发窗口——那是调度序问题，不是节奏问题；串行多轮快问快答才是目标场景。
_last_sent: Dict[str, float] = {}
_LAST_SENT_CAP = 4096
AUTO_COOLDOWN_SEC = 5.0  # 同会话两次自动发的最小间隔，防刷屏/回环
# 同文入站去重窗口：窗口内相同文本/media_ref 判 duplicate；超时后允许再回
# （客户催一句「你在干嘛」是正常聊天，不能永久静默）。
AUTO_DEDUP_SEC = 45.0


def is_autoreply_enabled(cfg: Dict[str, Any], account_row: Dict[str, Any]) -> bool:
    """双闸门：全局 protocol_autoreply.enabled 且 账号 meta.auto_reply 皆为真。"""
    try:
        if not ((cfg or {}).get("protocol_autoreply") or {}).get("enabled", False):
            return False
    except Exception:
        return False
    meta = (account_row or {}).get("meta") or {}
    return bool(meta.get("auto_reply"))


def _account_effective_pa(cfg: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    """账号级有效 protocol_autoreply：全局有效设置 ⊕ 账号 meta.autoreply_override。"""
    glob = (cfg or {}).get("protocol_autoreply") or {}
    override = (row.get("meta") or {}).get("autoreply_override") or {}
    if not override:
        return dict(glob)
    try:
        from src.integrations.protocol_autoreply_settings import merge_account_override
        return merge_account_override(glob, override)
    except Exception:
        return dict(glob)


def _default_risk(text: str) -> str:
    try:
        from src.inbox.drafts import keyword_risk_level
        return keyword_risk_level(text) or "low"
    except Exception:
        return "low"


def _risk_policy_decide(reply: str):
    """AI 稿高风险 → 经 autosend_policy.decide 单一入口（#160 v2）。

    本链是「无人值守直发」，等价于 auto_ai 会话；入站侧不在这里判（生成前已由
    SkillManager 处理），所以 peer_risk 固定 low、只带 reply_risk=high。命中词用
    ``keyword_risk_hits`` 补齐（注入的 risk_fn 只回档位）。
    """
    from src.inbox.autosend_policy import decide as _decide
    hits: list = []
    try:
        from src.inbox.drafts import keyword_risk_hits
        _lvl, hits = keyword_risk_hits(reply)
    except Exception:
        hits = []
    return _decide(
        peer_risk="low", peer_reasons=[], reply_risk="high", reply_reasons=["keyword"],
        risk_hits=hits, automation_mode="auto_ai",
    )


def _shadow_hold(decision: Any, *, platform: str, account_id: str, chat_key: str,
                 reply: str, inbound: str, ts: float) -> Optional[Dict[str, Any]]:
    """影子档放行 → 台账 hold 行（stage=protocol，不登记 reconcile：本链无草稿行，
    发送结果紧接着由 ``_shadow_outcome`` 当场落）。best-effort，绝不影响发送。"""
    sh = getattr(decision, "shadow", None)
    if sh is None:
        return None
    try:
        from src.inbox import autosend_shadow_log as _sl
        try:
            from src.ai.chat_assistant_service import detect_language as _dl
            lang = str(_dl(inbound) or "")
        except Exception:
            lang = ""
        rec = _sl.build_record(
            platform=platform, account_id=account_id, conv_key=chat_key,
            draft_id=f"proto:{platform}:{account_id}:{chat_key}:{int(ts)}",
            would_hold_level=sh.would_hold_level, hold_reason=sh.hold_reason,
            peer_risk=sh.peer_risk, peer_reasons=sh.peer_reasons,
            reply_risk=sh.reply_risk, reply_reasons=sh.reply_reasons,
            risk_hits=sh.risk_hits, text=reply, stage="protocol",
            automation_mode=decision.automation_mode, policy_mode=decision.policy_mode,
            lang=lang, peer_text=inbound, ts=ts,
        )
        _sl.record(rec, track_outcome=False)
        _sl.maybe_alert(rec)
        return rec
    except Exception:
        logger.debug("[protocol-autoreply] 影子台账落行失败（忽略）", exc_info=True)
        return None


def _shadow_outcome(rec: Optional[Dict[str, Any]], outcome: str, reason: str) -> None:
    """影子放行稿的去向：sent / delivery_failed（reason 类别与 B 线 send_health 同口径）。"""
    if not rec:
        return
    try:
        from src.inbox import autosend_shadow_log as _sl
        r = str(reason or "")
        if outcome == "delivery_failed":
            try:
                from src.inbox.send_health import classify_fail_reason
                r = f"{classify_fail_reason(r)}:{r[:48]}"
            except Exception:
                pass
        _sl.record_outcome(rec, outcome, r)
    except Exception:
        logger.debug("[protocol-autoreply] 影子去向落行失败（忽略）", exc_info=True)


# 触发「转人工」的原因（自动回复未能安全送出 → 需要坐席接管）
HANDOFF_REASONS = frozenset({
    "high_risk", "empty_reply", "generate_error", "send_error",
    "quota_hour", "quota_day", "circuit_open", "off_hours",
})


def _parse_hhmm(s: str, default_min: int) -> int:
    """'09:30' → 570（一天内的分钟数）。解析失败回 default_min。"""
    try:
        hh, mm = str(s or "").split(":", 1)
        return (int(hh) % 24) * 60 + (int(mm) % 60)
    except Exception:
        return default_min


def within_business_hours(cfg: Dict[str, Any], now: Optional[float] = None) -> bool:
    """是否在「营业时段」内。未配置 / 未启用 → 恒 True（7×24）。

    config.protocol_autoreply.hours = {enabled, start:'HH:MM', end:'HH:MM', tz_offset}
    支持跨夜窗口（start>end，如 22:00→06:00）。
    """
    h = ((cfg or {}).get("protocol_autoreply") or {}).get("hours") or {}
    if not h.get("enabled"):
        return True
    now = now if now is not None else time.time()
    tz_offset = float(h.get("tz_offset", 8))
    local = now + tz_offset * 3600.0
    minutes = int(local // 60) % 1440
    start = _parse_hhmm(h.get("start", "09:00"), 540)
    end = _parse_hhmm(h.get("end", "23:00"), 1380)
    if start <= end:
        return start <= minutes < end
    return minutes >= start or minutes < end  # 跨夜


def pick_delay(cfg: Dict[str, Any]) -> float:
    """按 config.protocol_autoreply.delay = {min_sec, max_sec} 取随机拟人延迟（秒）。

    未配置 / 非法 → 0（不延迟）。
    ⚠ 遗留入口：正常发送路径已改走 ``resolve_send_pacing_block`` + humanize
    ``resolve_pacing``（单一节奏源收口·第三链，2026-08-07）；本函数仅作
    节奏解析异常时的兜底与旧测试契约，勿再新增调用方。"""
    d = ((cfg or {}).get("protocol_autoreply") or {}).get("delay") or {}
    try:
        lo = float(d.get("min_sec", 0) or 0)
        hi = float(d.get("max_sec", 0) or 0)
    except Exception:
        return 0.0
    if hi <= 0 or hi < lo:
        return 0.0
    return random.uniform(max(0.0, lo), hi)


# 协议链「什么都没配」时的兜底拟人节奏（2026-08-07 存量节点秒回收口）。
# 语义＝安全默认：一个卖「像真人」的自动回复 bot，其发送链的**缺省**行为必须是
# 「等几秒再发」而非「秒回」。区间与桌面种子 / 示例配置同口径（8–20s 自适应）。
DEFAULT_PROTOCOL_PACING: Dict[str, Any] = {
    "min_sec": 8, "max_sec": 20, "adaptive": True,
}


def resolve_send_pacing_block(
    cfg: Dict[str, Any], acct_pa: Dict[str, Any],
) -> Dict[str, Any]:
    """选出协议链本次发送生效的延迟配置块（纯函数；单一节奏源收口·第三链）。

    2026-08-07 老板实锤根因：本链此前只读独立键 ``protocol_autoreply.delay``，
    而设置页「回复节奏」滑杆写的是 ``inbox.l2_autosend.deliver_delay``——
    ChatX 独立包默认档（``l2_autosend.deliver=false``）下协议链自发消息，
    滑杆拖到哪都秒回。follow 判定收口于 ``humanize.resolve_following_delay_block``
    （与 A 线 ``sender.run_prereply_humanize`` / coverage 自检**同一函数**，改
    follow 规则不再三处漂移）：own 配了值优先 → 否则跟随滑杆 → follow:false 保留
    「本链秒回」逃生阀。

    **兜底默认（2026-08-07 P0.6，存量节点收口）**：`_ensure_seeded` 只在 config
    缺失时播种，**存量 ChatX 节点重装保留旧 config**（无 deliver_delay）→ 新代码
    跟随滑杆也是空 → resolve_pacing 得 0 → 依旧秒回。故当 own 与 slider **都没配**
    （max_sec<=0）时回落 ``DEFAULT_PROTOCOL_PACING``（8–20s），让存量节点仅凭新代码
    即拟人、无需改 config / 推 overlay。**尊重显式秒回**：``protocol_autoreply.delay
    .follow: false`` 时按用户意图返回其 own 块（空＝秒回逃生阀），不套默认。
    本兜底**仅作用于协议链**（本函数），A/B 线节奏不受影响（各自装配口不同）。
    """
    own = acct_pa.get("delay") if isinstance(acct_pa, dict) else None
    slider = (((cfg or {}).get("inbox") or {}).get("l2_autosend") or {}
              ).get("deliver_delay")
    block, _src = effective_protocol_pacing(own, slider)
    return block


def effective_protocol_pacing(
    own: Any, slider: Any,
) -> "tuple[Dict[str, Any], str]":
    """协议链**最终生效**延迟块 + 来源标注（纯 ``(own, slider)`` 核心，2026-08-07）。

    runtime（``resolve_send_pacing_block``）与设置页自检 banner
    （``reply_pacing_settings.chain_pacing_coverage``）**共用本函数**——banner 因此
    永远与 runtime 同口径，绝不出现「横幅说秒回、实际有节奏」的说谎（延续
    ``resolve_following_delay_block`` 单一源同一铁律）。返回 ``(block, source)``：

    - ``source='own'``    — 独立键 ``protocol_autoreply.delay`` 配了值，用它；
    - ``source='slider'`` — own 未配，跟随 ``inbox.l2_autosend.deliver_delay``；
    - ``source='default'``— own 与 slider **都没配**（max_sec<=0）→ 回落
      ``DEFAULT_PROTOCOL_PACING``（存量节点重装保留旧 config、无任何节奏键时，
      仅凭新代码即拟人，不必改 config/推 overlay/等种子）；
    - ``source='instant'``— own 显式 ``follow: false``（用户明示「本链就要秒回」）→
      返回其 own 块（通常空＝0 延迟），**不**套兜底默认（尊重意图）。
    """
    from src.inbox.humanize import resolve_following_delay_block
    block, following = resolve_following_delay_block(own, slider)
    if isinstance(own, dict) and own.get("follow") is False:
        return block, "instant"
    try:
        _mx = float((block or {}).get("max_sec", 0) or 0)
    except (TypeError, ValueError):
        _mx = 0.0
    if _mx <= 0:
        return dict(DEFAULT_PROTOCOL_PACING), "default"
    return block, ("slider" if following else "own")


def humanize_flags(cfg: Dict[str, Any], platform: str) -> tuple:
    """协议链拟人开关生效值 ``(mark_read, typing)``（纯函数）。

    与 ``AutosendWorker._humanize_flag`` 同键同语义：设置页「回复先标已读」
    ``inbox.l2_autosend.mark_read_before_reply`` /「正在输入」``typing_indicator``
    （默认皆开）为全局档，``platform_humanize.{platform}`` 键级覆写优先。
    """
    l2 = (((cfg or {}).get("inbox") or {}).get("l2_autosend") or {})
    mr = bool(l2.get("mark_read_before_reply", True))
    tp = bool(l2.get("typing_indicator", True))
    ph = l2.get("platform_humanize")
    ov = ph.get(str(platform or "").lower()) if isinstance(ph, dict) else None
    if isinstance(ov, dict):
        if ov.get("mark_read") is not None:
            mr = bool(ov.get("mark_read"))
        if ov.get("typing") is not None:
            tp = bool(ov.get("typing"))
    return mr, tp
# 值得落审计的原因（过滤掉门控/冷却/去重等噪声）
AUDIT_REASONS = frozenset({"ok"}) | HANDOFF_REASONS


def _result(reason: str, *, sent: bool = False, **extra: Any) -> Dict[str, Any]:
    """统一结果结构：decision/reason 恒在；兼容旧键 sent/skipped。"""
    r: Dict[str, Any] = {
        "decision": "sent" if sent else "skipped", "reason": reason,
    }
    if sent:
        r["sent"] = True
    else:
        r["skipped"] = reason
    r.update(extra)
    return r


async def run_autoreply(
    payload: Dict[str, Any],
    *,
    registry: Any,
    cfg: Dict[str, Any],
    generate: Callable[..., Awaitable[Optional[str]]],
    send: Callable[..., Awaitable[Any]],
    risk_fn: Callable[[str], str] = _default_risk,
    now: Optional[float] = None,
    limiter: Any = None,
    sleep: Optional[Callable[[float], Awaitable[Any]]] = None,
    inbox_mode_fn: Optional[Callable[[str, str, str], str]] = None,
    mark_read: Optional[Callable[..., Awaitable[Any]]] = None,
    typing: Optional[Callable[..., Awaitable[Any]]] = None,
) -> Dict[str, Any]:
    """核心：判断并执行一次自动回复。返回结构化结果（便于测试/观测）。

    依赖注入：registry.get(platform, account_id) / generate(...) / send(...)。
    ``limiter`` 可选（按账号限速 + 熔断）；None 表示不限流（保持纯逻辑可测）。
    ``sleep`` 可选（拟人化发送延迟，生产传 asyncio.sleep）；None 表示不延迟。
    ``inbox_mode_fn`` 可选（Phase 3 防双发）：(platform, account_id, chat_key)→会话
    automation_mode。若返回 ``auto_ai`` 表示该会话已由**收件箱全自动**（草稿/审批/
    autosend 链路，带人设+二次风控）托管，本直发链路早退，避免对同一条消息双生成、双发。
    ``mark_read`` / ``typing`` 可选（2026-08-07 拟人链补齐）：投递前「已读」回执与
    延迟尾段「正在输入」状态回调（kwargs: platform/account_id/chat_key[/action]），
    生产由 build_reply_hook 经编排器接线；None＝跳过（旧行为，纯单测零负担）。
    """
    if (payload or {}).get("direction", "in") != "in":
        return _result("not_inbound")
    platform = str(payload.get("platform") or "")
    account_id = str(payload.get("account_id") or "")
    chat_key = str(payload.get("chat_key") or "")
    text = str(payload.get("text") or "").strip()
    # 媒体消息（纯图片/语音/视频，无 caption）也应回复：有 media_ref 即视为有内容，
    # 生成阶段再识别补全（见 build_reply_hook._generate）。此前 text 必填 → 纯媒体被判
    # incomplete 早退，正是「WhatsApp/协议号收到图片 AI 不理」的根因之一。
    media_type = str(payload.get("media_type") or "")
    media_ref = str(payload.get("media_ref") or "")
    if not (platform and account_id and chat_key):
        return _result("incomplete")
    if not text and not media_ref:
        return _result("incomplete")

    try:
        row = registry.get(platform, account_id) or {}
    except Exception:
        row = {}
    if not is_autoreply_enabled(cfg, row):
        return _result("disabled")

    # ── 群/频道 opt-in 闸（P1 2026-08-20）────────────────────────────────
    # 本直发链没有任何群策略（无被@判定/发言概率/群冷却，生成上下文历史上还
    # 硬编码 private）——把私聊口吻的 AI 回复直发进群就是误发。群消息只有坐席
    # 显式确认过全自动（confirm_group 闸 → 显式 auto_ai → resolve 返回 auto_ai）
    # 才继续；档位读不出（store 异常/未接线）对群 fail-closed。群判定与 B 线
    # 同源（is_group_conversation：chat_type 集合 + TG 负 id 启发），chat_type
    # 同时看顶层与 source（TG 协议 worker 放 source.chat_type）。telegram 群经
    # 本链同样受闸——resolve 的 TG 豁免只属 pyrogram A 线自己的群闸。
    try:
        from src.inbox.ingest import is_group_conversation
        _src0 = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        _is_group_msg = bool(is_group_conversation({
            "chat_type": str(payload.get("chat_type")
                             or (_src0 or {}).get("chat_type") or ""),
            "platform": platform,
            "chat_key": chat_key,
        }))
    except Exception:
        _is_group_msg = False
    if _is_group_msg:
        _gmode = ""
        if inbox_mode_fn is not None:
            try:
                _gmode = str(inbox_mode_fn(platform, account_id, chat_key) or "")
            except Exception:
                _gmode = ""
        if _gmode != "auto_ai":
            return _result("group_optin_required", inbound=text)

    # Phase 3 防双发 + 人审闸：与收件箱 UI / A 线同一口径（allows_direct_autosend）。
    # - auto_ai + l2 deliver 开 → 让位 System Z（防双发）
    # - auto_ai + deliver 关 → 不让位（2026-07-22：让位=吞消息）
    # - manual / review / multi_choice → 直发静音（「停 AI / AI草稿我审」必须生效）
    if inbox_mode_fn is not None:
        try:
            from src.inbox.automation_mode import (
                allows_direct_autosend,
                human_gate_skip_reason,
            )
            _im = str(inbox_mode_fn(platform, account_id, chat_key) or "")
            if allows_direct_autosend(_im):
                _deliver_on = bool(
                    ((((cfg or {}).get("inbox") or {}).get("l2_autosend") or {})
                     .get("deliver"))
                )
                if _deliver_on:
                    logger.info(
                        "[protocol-autoreply] inbox_autopilot yield %s:%s:%s",
                        platform, account_id, chat_key,
                    )
                    return _result("inbox_autopilot", inbound=text)
                logger.warning(
                    "[protocol-autoreply] auto_ai but l2 deliver=off → "
                    "keep protocol path %s:%s:%s",
                    platform, account_id, chat_key,
                )
            else:
                _skip = human_gate_skip_reason(_im)
                if _skip:
                    return _result(_skip, inbound=text)
        except Exception:
            logger.debug("[protocol-autoreply] inbox_mode_fn 检查失败（忽略）", exc_info=True)

    # 双面板融合驾驶权锁（surface_fusion，2026-08-13 第二批）：该账号自动化持有者
    # 是「原生面板」→ 本直发链让位（与上方 automation_mode 同族的归属判定）。
    # P0 只闸了 AutosendWorker，但 ChatX 独立包默认档（l2_autosend.deliver=false）
    # 下**本链才是自动发送主力**——不闸这里，owner=native 时照发＝锁不闭合。
    # 开关/owner 判定都在 autosend_blocked 内（fusion 未开恒 False），fail-open：
    # 锁故障绝不闸死自动回复。刻意不打「需人工」标签——原生面正在处理，非故障。
    try:
        from src.integrations.surface_fusion import (
            autosend_blocked as _sf_blocked,
            note_pilot_yield as _sf_note_yield,
        )
        if _sf_blocked(cfg, platform, account_id):
            _sf_note_yield(platform, account_id, "a_line")  # 让位观测（P4）
            logger.info(
                "[protocol-autoreply] surface pilot=native yield %s:%s:%s",
                platform, account_id, chat_key,
            )
            return _result("pilot_native", inbound=text)
    except Exception:
        logger.debug("[protocol-autoreply] 驾驶权判定失败（放行）", exc_info=True)

    # License 到期硬阻断（Sprint2）：enforce 开且授权失效(只读) → 决策期早退，不生成不发。
    # 默认 enforce=false → 恒放行，零破坏；fail-open。
    try:
        from src.licensing.gate import is_outbound_blocked
        from src.licensing.license_manager import get_license_manager
        if is_outbound_blocked(get_license_manager().status()):
            return _result("license_readonly", inbound=text)
    except Exception:
        logger.debug("[protocol-autoreply] license 检查失败（忽略）", exc_info=True)

    # G1 全局 Kill-Switch：紧急冻结时在决策期就早退（不生成、不发、不浪费 token）；
    # 与预热闸门正交（无视 companion_send_gate.enabled）。入站仍由收件箱 ingest 收录，
    # 故不另打人工标签（避免全局停发时人工队列被瞬时灌爆），等同 disabled 抑制。
    try:
        from src.ops.kill_switch import is_blocked as _ks_blocked
        _ks_on, _ks_scope, _ks_reason = _ks_blocked(platform, account_id)
    except Exception:
        _ks_on, _ks_scope = False, ""
    if _ks_on:
        logger.warning("[kill-switch] 冻结发送 %s:%s（scope=%s）", platform, account_id, _ks_scope)
        return _result("kill_switch", inbound=text)

    # G3 金丝雀放量：启用且本号不在 cohort → 决策期早退（不生成、不发；与 disabled 同抑制，
    # 不打人工标签避免放量期人工队列被灌爆）。未启用→零破坏。
    try:
        from src.ops.canary import is_held as _canary_held
        _ch_on, _ = _canary_held(platform, account_id, cfg)
    except Exception:
        _ch_on = False
    if _ch_on:
        return _result("canary_hold", inbound=text)

    ts = now if now is not None else time.time()
    # 账号级有效设置 = 全局有效 protocol_autoreply ⊕ 账号 meta.autoreply_override
    acct_pa = _account_effective_pa(cfg, row)
    acct_cfg = {"protocol_autoreply": acct_pa}
    # 营业时段外：不自动发，转人工（坐席上班后处理）
    if not within_business_hours(acct_cfg, ts):
        return _result("off_hours", inbound=text)
    # 账号工作时间班表（inbox.work_schedule，2026-08-04）：与 A 线直发 /
    # B 线 autosend 同一 should_hold 判定——账号休息中不直发（危机消息
    # severe/elevated 在判定内穿透照发），复用同一 off_hours 语义（转人工
    # 打标 + 审计）。与上面的 protocol_autoreply.hours（本链路遗留营业时段）
    # 并存：任一判休即休。fail-open：判定异常一律放行。
    try:
        from src.inbox.work_hours_gate import (
            should_hold_auto_reply,
            work_schedule_cfg,
        )
        if should_hold_auto_reply(
                work_schedule_cfg(cfg), platform, account_id,
                peer_text=text, now_ts=ts):
            return _result("off_hours", inbound=text)
    except Exception:
        logger.debug("[protocol-autoreply] 班表判定失败（放行）", exc_info=True)

    # 账号级闸门：限速 / 熔断（区别于下方会话级去重/冷却）
    account_key = f"{platform}:{account_id}"
    ov_rate = acct_pa.get("rate") or {}
    if limiter is not None:
        allowed, why = limiter.allow(
            account_key, ts,
            hourly=ov_rate.get("hourly"), daily=ov_rate.get("daily"))
        if not allowed:
            return _result(why, inbound=text)

    key = f"{platform}:{account_id}:{chat_key}"
    # 去重标识：纯媒体消息 text 为空，用 media_ref（每条唯一）避免两张不同图被判重复。
    dedup_text = text or media_ref
    last = _last_reply.get(key)
    if last is not None:
        age = ts - float(last[1] or 0)
        if last[0] == dedup_text and age < AUTO_DEDUP_SEC:
            logger.info(
                "[protocol-autoreply] duplicate skip %s age=%.1fs text=%r",
                key, age, str(dedup_text)[:40],
            )
            return _result("duplicate")
        if age < AUTO_COOLDOWN_SEC:
            return _result("cooldown")
    # 先占位（含本条入站标识），避免生成期间同条消息重复触发
    _last_reply[key] = (dedup_text, ts)

    # 生效人设（2026-07-26）：与 autodraft/autosend 同一共享解析器——
    # 会话覆写(开关开) → meta.persona_id → meta.persona_ids[0] → config 默认。
    # 修两处旧缺口：① 只读单数 persona_id，QR 号 sync 写的是复数 persona_ids
    # → 恒空；② cp-persona 换绑对协议号自动回复不生效。
    # registry 用调用方已持有的 row 适配（不重查全局库——保住"调用方给什么行
    # 就按什么行解析"的旧契约，测试/多 registry 语境同样成立）。失败回落旧直读。
    try:
        from src.ai.persona_voice import resolve_effective_persona_id as _repi

        class _RowRegistry:
            def get(self, _platform, _account_id):
                return row

        persona_id = _repi(
            cfg, platform, account_id, str(chat_key or ""),
            registry=_RowRegistry())
    except Exception:
        persona_id = str((row.get("meta") or {}).get("persona_id") or "")
    try:
        reply = await generate(
            text=text, platform=platform, account_id=account_id,
            chat_key=chat_key, persona_id=persona_id,
            media_type=media_type, media_ref=media_ref,
        )
    except Exception:
        logger.warning("[protocol-autoreply] 生成失败 %s", key, exc_info=True)
        opened = limiter.record_failure(account_key) if limiter is not None else False
        return _result("generate_error", inbound=text, breaker_opened=opened)
    reply = str(reply or "").strip()
    if not reply:
        return _result("empty_reply", inbound=text)

    risk = "low"
    try:
        risk = (risk_fn(reply) if risk_fn else "low") or "low"
    except Exception:
        risk = "low"
    # #160 v2（2026-09-04 老板拍板，本链 2026-09-04 11:0x 收编）：AI 稿命中高风险词
    # **不再直接转人工**——与草稿链同走 autosend_policy.decide 单一入口：shadow 档
    # 照发 + 影子台账落一行（stage=protocol）+ 发送后当场写去向；enforce 档保留旧行为
    # （high_risk → 转人工打标）。本链旧规则只对 high 动手（medium 一直放行），故这里
    # 只对 high 走 policy，台账口径与旧行为逐字一致。
    _shadow_rec: Optional[Dict[str, Any]] = None
    if risk == "high":
        _decision = None
        try:
            _decision = _risk_policy_decide(reply)
        except Exception:
            logger.debug("[protocol-autoreply] policy decide 异常（按 enforce 旧行为）", exc_info=True)
        if _decision is None or _decision.level != "L2":
            logger.warning("[protocol-autoreply] 命中高风险，转人工不自动发：%s", key)
            return _result("high_risk", text=reply, inbound=text, risk=risk)
        _shadow_rec = _shadow_hold(
            _decision, platform=platform, account_id=account_id, chat_key=chat_key,
            reply=reply, inbound=text, ts=(ts if now is not None else time.time()))
        logger.info(
            "[protocol-autoreply] 高风险稿已放行（shadow）%s hold=%s hits=%s",
            key, (_decision.shadow.hold_reason if _decision.shadow else "-"),
            ("|".join((_decision.shadow.risk_hits or [])[:4]) if _decision.shadow else "-"))

    # 单段落收口（2026-08-09）：本链不具备分条能力，而 bubbles 开启时拟稿合同是
    # 「每行一句」——多行文本从本链整条发出＝「一条消息带结构化换行」（2026-08-08
    # 客户实锤的 AI 感形态）。折叠成自然单段再发；bubbles 关闭时 ai_client 出口
    # 已折叠过，此处幂等。纯文本操作，绝不阻断发送。
    try:
        from src.inbox.reply_split import collapse_paragraphs as _collapse
        reply = _collapse(reply) or reply
    except Exception:
        pass

    # 拟人化发送前序列（模拟「看到→想→打字」；只在确定要发时才等，跳过的不浪费）。
    # 单一节奏源收口·第三链（2026-08-07）：延迟经 resolve_send_pacing_block 解析——
    # 显式 protocol_autoreply.delay 优先，否则跟随设置页滑杆键 deliver_delay
    # （platform/persona 覆写、adaptive 按回复长度估时并扣生成耗时，均随
    # resolve_pacing 同步生效）；观测打点 protocol/* 进设置页「节奏观测」——
    # 此前本链零观测，秒回在所有仪表盘上隐形。已读/打字回调在手且开关开时走
    # humanize 协作器（已读 → 静默思考 → 临发前挂「正在输入」），与 A/B 线同一
    # 节奏模型；无回调时保持单次裸 sleep（兼容注入 sleep 断言次数的既有测试契约）。
    # 解析异常回落旧 pick_delay——节奏是增强，绝不阻断发送。
    if sleep is not None:
        _pace_block: Dict[str, Any] = {}
        _pr = None
        try:
            from src.inbox.humanize import resolve_pacing
            _pace_block = resolve_send_pacing_block(cfg, acct_pa)
            _pr = resolve_pacing(
                _pace_block, text=reply,
                elapsed_sec=(max(0.0, time.time() - ts) if now is None else 0.0),
                persona_id=persona_id, platform=platform)
            d = _pr.delay
            # P2 连发间隔地板（2026-08-12，与 B 线 worker 同一对纯函数）：距上一条
            # **成功发出**不足 min_gap_sec 时补等待（±15% 抖动）。快问快答场景每条
            # 回复的 adaptive 延迟大多被生成耗时抵扣光（残余 ~2s），5s AUTO_COOLDOWN
            # 只丢弃不排队——没有本地板就是「机关枪式连发」。键随 deliver_delay 块
            # 继承（跟随滑杆/own 显式配均可），未配=0=关。失败静默（节奏是增强）。
            try:
                from src.inbox.humanize import (
                    apply_min_gap_floor,
                    resolve_min_gap_sec,
                )
                _gap = resolve_min_gap_sec(
                    _pace_block, platform=platform, persona_id=persona_id)
                _prev_sent = _last_sent.get(key)
                if _gap > 0 and _prev_sent is not None:
                    _nowf = time.time() if now is None else ts
                    d, _floored = apply_min_gap_floor(
                        d, since_last_send_sec=max(0.0, _nowf - _prev_sent),
                        min_gap_sec=_gap)
                    if _floored:
                        from dataclasses import replace as _dc_replace
                        _pr = _dc_replace(_pr, delay=d, floored=True)
            except Exception:
                logger.debug(
                    "[protocol-autoreply] 连发地板解析失败（忽略）", exc_info=True)
            try:
                from src.integrations.humanize_metrics import record_pacing
                record_pacing(
                    f"protocol/{platform or '-'}/{persona_id or '-'}", _pr)
            except Exception:
                pass
        except Exception:
            logger.debug("[protocol-autoreply] 节奏解析失败，回落旧延迟", exc_info=True)
            d = pick_delay(acct_cfg)
        _mr_cb = None
        _tp_cb = None
        if mark_read is not None or typing is not None:
            _mr_on, _tp_on = humanize_flags(cfg, platform)
            if mark_read is not None and _mr_on:
                async def _mr_cb():
                    await mark_read(platform=platform, account_id=account_id,
                                    chat_key=chat_key)
            if typing is not None and _tp_on:
                async def _tp_cb(action):
                    await typing(platform=platform, account_id=account_id,
                                 chat_key=chat_key, action=action)
        if _mr_cb is not None or _tp_cb is not None:
            try:
                from src.inbox.humanize import (
                    resolve_typing_lead,
                    run_presend_humanization,
                )
                await run_presend_humanization(
                    delay=d, action="typing", mark_read=_mr_cb, typing=_tp_cb,
                    sleep=sleep,
                    typing_lead_sec=resolve_typing_lead(
                        _pace_block, text=reply,
                        persona_id=persona_id, platform=platform))
                d = 0.0   # 协作器已消费延迟（含已读/打字），下方裸 sleep 不再等
            except Exception:
                logger.debug(
                    "[protocol-autoreply] 拟人序列失败，回落裸延迟", exc_info=True)
        if d > 0:
            try:
                await sleep(d)
            except Exception:
                pass

    try:
        await send(platform=platform, account_id=account_id,
                   chat_key=chat_key, text=reply)
    except Exception as _send_ex:
        logger.warning("[protocol-autoreply] 发送失败 %s", key, exc_info=True)
        _err = str(_send_ex)
        # 影子放行的稿没发出去 → 去向当场落 delivery_failed（类别与 B 线同口径）
        _shadow_outcome(_shadow_rec, "delivery_failed", _err)
        # 闸门拦截（send_gate_blocked:*）是配置性节流，不是基础设施故障——
        # 不喂熔断计数（否则限流会连带把断路器打开、雪上加霜）；
        # 错误串带回 res 供 hook 层发「限流拦截」告警（2026-07-22 可见性铁律）。
        if _err.startswith("send_gate_blocked"):
            return _result("send_error", text=reply, inbound=text, risk=risk,
                           breaker_opened=False, error=_err,
                           shadow_released=_shadow_rec is not None)
        opened = limiter.record_failure(account_key) if limiter is not None else False
        return _result("send_error", text=reply, inbound=text, risk=risk,
                       breaker_opened=opened, error=_err,
                       shadow_released=_shadow_rec is not None)
    _shadow_outcome(_shadow_rec, "sent", "protocol_autoreply")
    # 发送成功后用「发送时刻」刷新冷却基准 + 记账号配额/闭合熔断
    send_ts = ts if now is not None else time.time()
    _last_reply[key] = (dedup_text, send_ts)
    # P2 连发地板账本：只记成功发出（失败不占位，下条不背无谓等待）。软上限防长跑撑爆。
    _last_sent[key] = send_ts
    if len(_last_sent) > _LAST_SENT_CAP:
        for _k in sorted(_last_sent, key=_last_sent.get)[:_LAST_SENT_CAP // 2]:
            _last_sent.pop(_k, None)
    if limiter is not None:
        limiter.record_sent(account_key, send_ts)
        limiter.record_success(account_key)
    return _result("ok", sent=True, text=reply, inbound=text, risk=risk,
                   shadow_released=_shadow_rec is not None)


# 自动回复未能安全送出时，给会话打的标签（在统一收件箱里高亮，供坐席接管）
HANDOFF_TAG = "需人工"


def record_decision_audit(audit: Any, payload: Dict[str, Any],
                          res: Dict[str, Any]) -> bool:
    """把一次有意义的决策落审计（门控/冷却/去重等噪声不记）。返回是否记录。"""
    reason = str((res or {}).get("reason") or "")
    if reason not in AUDIT_REASONS:
        return False
    from src.inbox.normalizer import conv_id
    platform = str(payload.get("platform") or "")
    account_id = str(payload.get("account_id") or "")
    chat_key = str(payload.get("chat_key") or "")
    audit.record(
        platform=platform, account_id=account_id, chat_key=chat_key,
        conversation_id=conv_id(platform, account_id, chat_key),
        inbound=str(payload.get("text") or ""),
        reply=str(res.get("text") or ""),
        risk=str(res.get("risk") or ""),
        decision=str(res.get("decision") or ""),
        reason=reason,
    )
    return True


def needs_handoff(res: Dict[str, Any]) -> bool:
    return str((res or {}).get("reason") or "") in HANDOFF_REASONS


# 告警防抖：同账号同类告警 30 分钟最多发一次（避免配额耗尽时刷屏）
_alert_seen: Dict[str, float] = {}
_ALERT_DEBOUNCE_SEC = 1800.0


def publish_alert(kind: str, payload: Dict[str, Any], detail: str = "",
                  now: Optional[float] = None) -> bool:
    """熔断 / 配额耗尽等运维告警 → EventBus（WebhookNotifier 转钉钉/飞书/企微）
    + 集团 TG 中继（ops_alert；未配 EVENT_INGEST_KEY 的部署自动降级只落日志）。

    防抖：同 (kind, platform, account) 30 分钟一次。返回是否真的发了。
    2026-07-22 可见性铁律：WebhookNotifier 常见"0 个端点"（未配 webhook）——
    只发 event_bus 等于没人看见，故同报 ops_alert 直达运营手机。
    """
    platform = str(payload.get("platform") or "")
    account_id = str(payload.get("account_id") or "")
    key = f"{kind}:{platform}:{account_id}"
    ts = now if now is not None else time.time()
    last = _alert_seen.get(key)
    if last is not None and ts - last < _ALERT_DEBOUNCE_SEC:
        return False
    _alert_seen[key] = ts
    sent = False
    try:
        from src.integrations.shared.event_bus import get_event_bus
        get_event_bus().publish("autoreply_alert", {
            "kind": kind, "platform": platform, "account_id": account_id,
            "detail": detail,
        })
        sent = True
    except Exception:
        logger.debug("[protocol-autoreply] 告警发布失败", exc_info=True)
    try:
        from src.ops.ops_alert import notify as _ops_notify
        _ops_notify(kind, f"⚠️ {platform}:{account_id} {detail or kind}",
                    account_id=f"{platform}:{account_id}", reason=kind)
    except Exception:
        logger.debug("[protocol-autoreply] ops_alert 转发失败", exc_info=True)
    return sent


def clear_needs_human(store: Any, conversation_id: str) -> bool:
    """坐席接管（人工发出消息）后清除 HANDOFF_TAG。返回是否真的清除了。"""
    if store is None or not conversation_id:
        return False
    try:
        tags = list(store.get_conv_tags(conversation_id) or [])
    except Exception:
        return False
    if HANDOFF_TAG not in tags:
        return False
    try:
        store.set_conv_tags(conversation_id, [t for t in tags if t != HANDOFF_TAG])
    except Exception:
        return False
    # 实施74（实施69 P1-1）：标摘了元数据同清（best-effort；旧 store 无此方法跳过）
    try:
        if hasattr(store, "set_handoff_meta"):
            store.set_handoff_meta(conversation_id, None)
    except Exception:
        logger.debug("[protocol-autoreply] handoff_meta 清除失败（忽略）",
                     exc_info=True)
    return True


def tag_needs_human(store: Any, payload: Dict[str, Any], *,
                    reason: str = "", source: str = "system",
                    now: Optional[float] = None) -> bool:
    """给会话打 HANDOFF_TAG（已存在则跳过）。store 需提供 get/set_conv_tags。

    实施74（实施69 P1-1）：打标同存 ``{reason, ts, source}`` 元数据
    （``store.set_handoff_meta``，旧 store 缺方法自动跳过）——「需人工」从
    裸结论变成可解释（何时/为何/谁打的），前端 chip 悬停直读。
    """
    if store is None:
        return False
    from src.inbox.normalizer import conv_id
    cid = conv_id(
        str(payload.get("platform") or ""),
        str(payload.get("account_id") or ""),
        str(payload.get("chat_key") or ""),
    )
    try:
        tags = list(store.get_conv_tags(cid) or [])
    except Exception:
        tags = []
    if HANDOFF_TAG in tags:
        return False
    tags.append(HANDOFF_TAG)
    try:
        store.set_conv_tags(cid, tags)
    except Exception:
        return False
    try:
        if hasattr(store, "set_handoff_meta"):
            store.set_handoff_meta(cid, {
                "reason": str(reason or ""),
                "ts": float(now if now is not None else time.time()),
                "source": str(source or "system"),
            })
    except Exception:
        logger.debug("[protocol-autoreply] handoff_meta 写入失败（忽略）",
                     exc_info=True)
    return True


def media_context_extra(
    *,
    media_type: str,
    media_ref: str,
    media_desc: str = "",
    text: str = "",
) -> Dict[str, Any]:
    """入站媒体 → prompt 上下文标记（纯函数，2026-08-16 键盘实锤后收口）。

    - 语音且转写成功（``text`` 已是真实内容而非 ``[语音]`` 占位）→ 只标
      ``_peer_message_is_voice``（ai_client 走【语音消息】口语化提示块）。
      **不**标 ``_peer_message_is_media``：媒体块对无 desc 的语音会注入
      「内容暂无法识别请追问」（与转写文本自相矛盾）；若 user_context 里还驻留
      陈旧 ``_media_desc``，更会把旧图片当"对方刚发来的媒体"喂给模型——
      8/01 的键盘照片描述驻留 15 天，8/16 客户语音问新闻、AI 夸键盘。
    - 其余（图片/视频/贴纸/转写失败的语音…）→ 媒体块标记照旧。
    """
    _mk = str(media_type or "").strip().lower() or "media"
    if _mk == "voice":
        _t = str(text or "").strip()
        try:
            from src.inbox.media_enrich import is_placeholder_only as _ipo
            transcribed = bool(_t) and not _ipo(_t)
        except Exception:
            transcribed = bool(_t)
        if transcribed:
            return {"_peer_message_is_voice": True}
    out: Dict[str, Any] = {
        "_peer_message_is_media": True,
        "_media_kind": _mk,
        "_inbox_peer_kind": _mk,
        "_media_ref": str(media_ref),
    }
    if media_desc:
        out["_media_desc"] = media_desc
    return out


def build_reply_hook(app: Any) -> Callable[[Dict[str, Any]], Awaitable[None]]:
    """生产接线：从 app.state 取 skill_manager/config/orchestrator，返回异步 hook。"""

    def _cfg() -> Dict[str, Any]:
        cm = getattr(app.state, "config_manager", None)
        base = (getattr(cm, "config", None) or {}) if cm is not None else {}
        try:
            from src.integrations.protocol_autoreply_settings import (
                cfg_with_settings,
            )
            return cfg_with_settings(base)
        except Exception:
            return base

    async def _generate(*, text, platform, account_id, chat_key, persona_id,
                        media_type="", media_ref=""):
        sm = getattr(app.state, "skill_manager", None)
        if sm is None:
            tc = getattr(app.state, "telegram_client", None)
            sm = getattr(tc, "skill_manager", None) if tc is not None else None
        if sm is None or not hasattr(sm, "process_message"):
            return None
        # 媒体识别补全：对方发来图片/语音/视频 → 共享识别层变成可喂 AI 的文本。
        # 无兜底纪律（2026-08-17）：图片/语音识别失败＝不生成（宁可不回，不装懂）。
        media_desc = ""
        if media_ref:
            try:
                from src.inbox.media_enrich import (
                    enrich_inbound_media_text, is_placeholder_only,
                    media_wait_sec_from_cfg,
                )
                _tc = getattr(app.state, "telegram_client", None)
                _vtr = getattr(_tc, "voice_transcriber", None) if _tc is not None else None
                _enriched, media_desc = await enrich_inbound_media_text(
                    media_type=media_type, media_ref=media_ref,
                    caption=("" if is_placeholder_only(text) else text),
                    config=_cfg(), voice_transcriber=_vtr,
                    # 拟稿等图（2026-08-23）：边车「先落行后补文件」窗口内不再
                    # 抢跑生成——预算内等文件就绪再识别，等不到才走降级/扣留。
                    wait_file_sec=media_wait_sec_from_cfg(_cfg()),
                )
                if _enriched and _enriched.strip():
                    text = _enriched.strip()
                    # 识别结果回写收件箱消息行（与全自动草稿链 update_message_text 同口径）：
                    # 让坐席台/时间线/媒体卡看到"[图片内容] …/转写"而非裸 [图片] 占位。
                    # best-effort + only_if_empty=True，绝不踩掉已有真实内容、失败不影响回复。
                    if media_desc:
                        try:
                            _store = getattr(app.state, "inbox_store", None)
                            if _store is not None:
                                from src.inbox.normalizer import conv_id
                                _store.update_message_text(
                                    conv_id(platform, account_id, chat_key),
                                    text=text, media_ref=media_ref,
                                    only_if_empty=True,
                                )
                        except Exception:
                            logger.debug(
                                "[protocol-autoreply] 识别结果回写收件箱失败（忽略）",
                                exc_info=True)
            except Exception:
                logger.debug("[protocol-autoreply] 媒体识别补全失败", exc_info=True)
        _mt_l = str(media_type or "").strip().lower()
        # 识别失败处置（实施56 P1，2026-08-22）：media_degrade_reply 开 → 放行生成，
        # ai_client 媒体块对无 desc 媒体自带「自然承认收到+温和追问」话术（诚实降级，
        # 不装懂）；关（默认）→ 维持 08-17 无兜底纪律：拦下 + 上报 + 不回。
        _degrade_ok = False
        if media_ref and not media_desc and _mt_l in (
                "image", "photo", "sticker", "voice", "audio"):
            try:
                from src.inbox.media_enrich import media_degrade_reply_enabled
                _degrade_ok = media_degrade_reply_enabled(_cfg())
            except Exception:
                _degrade_ok = False
        if media_ref and _mt_l in ("image", "photo", "sticker") and not media_desc:
            if _degrade_ok:
                logger.info(
                    "[protocol-autoreply] 图片未识别 → 降级诚实回复"
                    "（media_degrade_reply）%s:%s:%s",
                    platform, account_id, chat_key)
            else:
                try:
                    from src.ops.delivery_block import report_block
                    from src.inbox.normalizer import conv_id as _vcid
                    report_block(
                        "vision", reason="enrich_failed", platform=platform,
                        conversation_id=_vcid(platform, account_id, chat_key))
                except Exception:
                    logger.debug("[protocol-autoreply] vision hold 上报失败",
                                 exc_info=True)
                logger.warning(
                    "[protocol-autoreply] 图片未看懂 → 跳过自动回复 %s:%s:%s",
                    platform, account_id, chat_key)
                return None
        if media_ref and _mt_l in ("voice", "audio") and not media_desc:
            if _degrade_ok:
                logger.info(
                    "[protocol-autoreply] 语音未转写 → 降级诚实回复"
                    "（media_degrade_reply）%s:%s:%s",
                    platform, account_id, chat_key)
            else:
                try:
                    from src.ops.delivery_block import report_block
                    from src.inbox.normalizer import conv_id as _acid
                    report_block(
                        "asr", reason="enrich_failed", platform=platform,
                        conversation_id=_acid(platform, account_id, chat_key))
                except Exception:
                    logger.debug("[protocol-autoreply] asr hold 上报失败",
                                 exc_info=True)
                logger.warning(
                    "[protocol-autoreply] 语音未转写 → 跳过自动回复 %s:%s:%s",
                    platform, account_id, chat_key)
                return None
        # N 线 核心1：复用共享 companion_context 装配标准上下文（与 A 线同一套）。
        # 记忆/情绪由 skill_manager 内部按 platform+user_id+chat_id 注入；
        # 此处保证平台/会话标识 + 人设一致（协议线默认私聊）。
        from src.utils.companion_context import build_companion_context
        _emo = getattr(app.state, "emotion_enhancer", None)
        if _emo is None:
            _tc = getattr(app.state, "telegram_client", None)
            _emo = getattr(_tc, "emotion_enhancer", None) if _tc is not None else None
        _extra = {"channel": "protocol",
                  # account_id 必带（2026-07-22 真机复盘）：selfie/媒体发送走
                  # 编排器路（_try_send_selfie_media 路①）要求 platform+account_id
                  # +chat_key 齐全——缺它则协议线（WA 等）生成了图也发不出，
                  # 静默回落文字（客户要图永远只收到婉拒）。
                  "account_id": account_id}
        # 让 ai_client 多模态 prompt 知道"这是媒体消息 + 识别摘要"（与 inbound_enrich 同字段口径）
        if media_ref:
            _extra.update(media_context_extra(
                media_type=media_type, media_ref=media_ref,
                media_desc=media_desc, text=text))
        # 驾驶舱 P2（2026-08-13）：交接提醒——A 线自有对话历史**不含**人工在
        # 原生页说的话，这条提醒是它交还后唯一的连续性信号。经 _line_merge_keys
        # 白名单进 user_context 的 _topic_switch_hint 消费口；偶发被入站 enrich
        # 的同键提示覆盖＝可接受（丢一次提醒，不丢功能）。异常静默不阻断生成。
        try:
            from src.inbox.normalizer import conv_id as _mk_cid
            from src.inbox.takeover import handback_note as _hb_note
            _hb = _hb_note(
                _mk_cid(platform, account_id, chat_key),
                store=getattr(app.state, "inbox_store", None),
            )
            if _hb:
                _extra["_topic_switch_hint"] = _hb
        except Exception:
            logger.debug("[protocol-autoreply] 交接提醒跳过", exc_info=True)
        # 群上下文诚实（P1 2026-08-20）：能走到这里的群都是坐席显式确认过
        # 全自动的（run_autoreply 群闸）——此前 chat_type 硬编码 private，
        # 人设当 1:1 聊、群语境全丢。从会话库取真类型，取不到回落 private。
        _ct = "private"
        try:
            _ibx0 = getattr(app.state, "inbox_store", None)
            if _ibx0 is not None:
                from src.inbox.normalizer import conv_id as _ct_cid
                _ct_raw = str(((_ibx0.get_conversation(
                    _ct_cid(platform, account_id, chat_key)) or {})
                    .get("chat_type") or "")).strip().lower()
                if _ct_raw == "channel":
                    _ct = "channel"
                elif _ct_raw in ("group", "supergroup", "megagroup", "gigagroup"):
                    _ct = "group"
        except Exception:
            _ct = "private"
        ctx = build_companion_context(
            platform=platform,
            chat_id=chat_key,
            text=text,
            chat_type=_ct,
            persona_id=persona_id,
            emotion_enhancer=_emo,
            extra=_extra,
        )
        _reply = await sm.process_message(
            text, user_id=f"{platform}:{account_id}:{chat_key}", context=ctx
        )
        # B51 反复读闸（实施64 P1-2，与 autodraft_helpers 同口径）：回复与对方
        # 上一条近逐字 → 重生成一次；复发 → 本轮不回（鹦鹉稿比沉默更伤）。
        try:
            from src.ai.reply_echo_guard import reply_echoes_inbound
            if (isinstance(_reply, str) and _reply
                    and reply_echoes_inbound(_reply, text)):
                logger.warning(
                    "[protocol-autoreply] 反复读闸命中 %s:%s:%s：%r → 重生成",
                    platform, account_id, chat_key, _reply[:60])
                _r2 = await sm.process_message(
                    text, user_id=f"{platform}:{account_id}:{chat_key}",
                    context=ctx)
                if (isinstance(_r2, str) and _r2
                        and not reply_echoes_inbound(_r2, text)):
                    _reply = _r2
                else:
                    logger.warning(
                        "[protocol-autoreply] 反复读闸复发 %s:%s:%s → 本轮不回",
                        platform, account_id, chat_key)
                    return None
        except Exception:
            logger.debug(
                "[protocol-autoreply] 反复读闸异常（放行原稿）", exc_info=True)
        # 盲断言闸门（2026-08-23 P0-2，与 autodraft_helpers 同口径）：无识图
        # 描述的图片轮，回复不得断言/猜测画面内容——降级指令是软约束，模型
        # 违约就整稿换成诚实追问。语言按稿面文字系统粗判（下游出站语言闸/
        # 翻译层会再对齐客户语言）。
        if (_mt_l in ("image", "photo", "sticker") and not media_desc
                and isinstance(_reply, str) and _reply):
            try:
                from src.inbox.media_enrich import (
                    blind_image_assertion, honest_image_ask,
                )
                if blind_image_assertion(_reply):
                    logger.info(
                        "[protocol-autoreply] 盲断言拦截 %s:%s:%s：%r → 诚实追问",
                        platform, account_id, chat_key, _reply[:60])
                    _lang = "zh" if any(
                        "\u4e00" <= ch <= "\u9fff" for ch in _reply) else "en"
                    _reply = honest_image_ask(
                        _lang, seed=f"{platform}:{account_id}:{chat_key}")
            except Exception:
                logger.debug(
                    "[protocol-autoreply] 盲断言闸门异常（放行原稿）",
                    exc_info=True)
        return _reply

    async def _send(*, platform, account_id, chat_key, text):
        from src.integrations.account_orchestrator import get_orchestrator
        cfg = _cfg()
        # G1 Kill-Switch（防御兜底）：无视预热闸门是否开，直达发送边界的硬停——
        # 任何绕过 run_autoreply 决策直接调 _send 的路径（如编排器/手动）也被冻结覆盖。
        try:
            from src.ops.kill_switch import is_blocked as _ks_blocked
            _ks_on, _ks_scope, _ = _ks_blocked(platform, account_id)
        except Exception:
            _ks_on, _ks_scope = False, ""
        if _ks_on:
            raise RuntimeError(f"kill_switch_blocked:{_ks_scope}")
        # 出站语言闸（P3-198，2026-08-04）：协议号自动回复的直发边界——此前不经
        # AutosendWorker 翻译回调，「中文回复发给外语客户」在这条链零防护（与
        # 主动触达同类缺口）。与 L2/主动触达同一函数同一配置：translate 开→译成
        # 客户语言；关→lang_gate（默认开）gate-only 只拦 CJK↔非 CJK 实质冲突。
        # 刻意放在语音分支**之前**：语音稿与文本同源，闸后语音念的就是客户语言。
        # HOLD → 抛错走 run_autoreply 既有失败链，绝不把冲突语言的内容发出。
        try:
            from src.inbox.outbound_translate import (
                parse_outbound_lang_gate_cfg as _plg,
                parse_outbound_translate_cfg as _pot,
                translate_outbound_text as _tot,
            )
            _otx = _pot(cfg or {})
            if _otx.get("enabled") or _plg(cfg or {}).get("enabled"):
                _svc = getattr(app.state, "translation_service", None)
                if _svc is not None:
                    from src.inbox.normalizer import conv_id
                    _gated = await _tot(
                        {"conversation_id": conv_id(platform, account_id, chat_key),
                         "text": text},
                        translation_service=_svc,
                        store=getattr(app.state, "inbox_store", None),
                        source_lang=_otx.get("source_lang") or "zh",
                        style=_otx.get("style") or "chat",
                        gate_only=not _otx.get("enabled"))
                    if _gated is None:
                        raise RuntimeError(
                            "lang_gate_hold: 文本语言与客户语言冲突且翻译不可用，已拦截")
                    if _gated:
                        text = _gated
        except RuntimeError:
            raise
        except Exception:
            logger.debug("[protocol-autoreply] 语言闸异常（放行）", exc_info=True)
        # 语音自动回复（方案B · 2026-07-20）：复用 Path B 的跨平台 autosend_voice
        #   （经 orch.send_media → WhatsApp 边车 /send-media 发 ogg/opus 语音条）。受
        #   inbox.l2_autosend.voice 配置门控（enabled / trigger / 长度 / 人设白名单 / 克隆音优先），
        #   人设声音取会话绑定人设的 voice_profile。判定该发语音 → 发语音并早退（跳过文本）；
        #   否则回落文本。全程 try/except：语音任何异常都绝不影响文本回复这条主路径；
        #   orch.send_media 内部自带 Kill-Switch + 反封号闸门，与文本同守。
        try:
            _vb = (((cfg or {}).get("inbox") or {}).get("l2_autosend") or {}).get("voice") or {}
            if _vb.get("enabled"):
                from types import SimpleNamespace as _SNS
                from src.inbox.autosend_helpers import autosend_voice as _av
                _shim = _SNS(
                    config=getattr(app.state, "config_manager", None),
                    logger=logger,
                    inbox_store=getattr(app.state, "inbox_store", None),
                    _web_loop=None,
                )
                if await _av(_shim, platform, account_id, chat_key, text):
                    return {"delivered": True, "delivered_as": "voice"}
        except Exception:
            logger.debug("[protocol-autoreply] 语音尝试失败，回落文本", exc_info=True)
        # WP-4 系统级披露（compliance.disclosure.notice，基线关）：每会话首条
        # AI 出站前置披露语（持久防重，键=conv_id）。刻意放在语言闸之后（披露语
        # 已是客户语言，不再过翻译）、语音分支之后（克隆声绝不念披露语；语音
        # 先行的会话由首条文本回复补披露——标记只在真应用时才烧）。
        try:
            from src.compliance.disclosure import apply_disclosure
            from src.inbox.normalizer import conv_id as _conv_id_fn
            _dc_cid = _conv_id_fn(platform, account_id, chat_key)
            _dc_lh = ""
            try:
                from src.inbox.outbound_translate import peer_language_hint
                _dc_st = getattr(app.state, "inbox_store", None)
                if _dc_st is not None:
                    _dc_lh = peer_language_hint(_dc_st, _dc_cid) or ""
            except Exception:
                _dc_lh = ""
            text, _ = apply_disclosure(cfg or {}, _dc_cid, text, lang_hint=_dc_lh)
        except Exception:
            logger.debug("[protocol-autoreply] 披露注入异常（原样发送）",
                         exc_info=True)
        # N 线 核心3：发送前反封号闸门（A/B 两线共用 companion_send_gate；默认关→零破坏）。
        # 拦截 → 抛错，交由 run_autoreply 既有熔断/转人工处理。
        # exempt_peers 白名单（2026-07-22）：测试号免限额，联调不再与养号策略打架。
        from src.skills.companion_send_gate import (
            evaluate, gate_enabled, peer_exempt,
        )
        if gate_enabled(cfg) and peer_exempt(cfg, chat_key):
            # #77（0830 AW7MUV）：豁免命中显式留痕——「白名单真读到了」有正面证据
            logger.info(
                "[send_gate] 白名单豁免命中 %s:%s → peer=%s（本次发送不受额度限制）",
                platform, account_id, chat_key)
        elif gate_enabled(cfg):
            try:
                from src.integrations.account_registry import get_account_registry
                from src.integrations.protocol_autoreply_limits import (
                    get_autoreply_limiter,
                )
                from src.skills.account_signals import build_account_signals
                # N3 信号接线：真 sends_today(限额计数) + age_days/proxy/banned(注册表)
                sig = build_account_signals(
                    platform, account_id,
                    registry=get_account_registry(),
                    limiter=get_autoreply_limiter(cfg),
                )
                dec = evaluate(sig, cfg)
            except Exception:
                logger.debug("[send_gate] 信号装配失败，放行", exc_info=True)
                dec = {"allowed": True}
            if not dec.get("allowed", True):
                # #77：拦截即知目标——peer 进错误串（run_autoreply 日志/审计原样携带）
                raise RuntimeError(
                    f"send_gate_blocked:{dec.get('reason')}|peer={chat_key}")
        orch = get_orchestrator(cfg)
        try:
            return await orch.send(platform, account_id, chat_key, text)
        except Exception as _send_exc:
            # G2 封号信号自动急停：风控错误 → 分级处置（退避/暂停/封禁），再抛回既有熔断
            try:
                from src.ops.ban_signal import handle_send_exception as _g2
                from src.integrations.account_registry import get_account_registry
                _g2(platform, account_id, _send_exc,
                    registry=get_account_registry(), alert=publish_alert)
            except Exception:
                pass
            raise

    async def hook(payload: Dict[str, Any]) -> None:
        from src.integrations.account_registry import get_account_registry
        from src.integrations.protocol_autoreply_limits import (
            get_autoreply_limiter,
        )
        import asyncio
        cfg = _cfg()

        # Phase 3：per-conv 有效档位（显式 > 全局 auto_draft），与 UI/A 线同源。
        def _inbox_mode(platform: str, account_id: str, chat_key: str) -> str:
            store = getattr(app.state, "inbox_store", None)
            if store is None:
                return ""
            try:
                from src.inbox.normalizer import conv_id
                from src.inbox.automation_mode import resolve_automation_mode
                return resolve_automation_mode(
                    store, conv_id(platform, account_id, chat_key), cfg,
                )
            except Exception:
                return ""

        # 拟人链回调（2026-08-07）：与 autosend 同经编排器分发（TG 协议号
        # pyrogram read_chat_history / send_chat_action，WA Baileys /read，
        # LINE sendChatChecked；不支持的 worker 编排器静默 False）。hook 本就
        # 跑在 web loop（与 _send 同域），无需跨线程调度；开关判定
        # （mark_read_before_reply / typing_indicator / platform_humanize）
        # 在 run_autoreply 内按调用实时读 cfg——设置页保存即生效，免重启。
        async def _mark_read(*, platform, account_id, chat_key):
            from src.integrations.account_orchestrator import get_orchestrator
            return await get_orchestrator(cfg).mark_read(
                platform, account_id, str(chat_key))

        async def _typing(*, platform, account_id, chat_key, action="typing"):
            from src.integrations.account_orchestrator import get_orchestrator
            return await get_orchestrator(cfg).send_chat_action(
                platform, account_id, str(chat_key), action)

        res = await run_autoreply(
            payload, registry=get_account_registry(), cfg=cfg,
            generate=_generate, send=_send,
            limiter=get_autoreply_limiter(cfg), sleep=asyncio.sleep,
            inbox_mode_fn=_inbox_mode,
            mark_read=_mark_read, typing=_typing,
        )
        # Phase 5：熔断刚触发 → 告警（断路器自身已在冷却期拦截后续）
        if res.get("breaker_opened"):
            logger.warning(
                "[protocol-autoreply] 账号 %s:%s 连续失败触发熔断，冷却期内暂停自动回复",
                payload.get("platform"), payload.get("account_id"))
            publish_alert("circuit_open", payload, "连续失败触发熔断，已暂停自动回复")
        # Phase 8：配额耗尽 → 告警（防抖，避免刷屏）
        elif res.get("reason") in ("quota_hour", "quota_day"):
            publish_alert(res["reason"], payload, "自动回复配额已用尽，转人工")
        # 2026-07-22 可见性铁律：发送闸门拦截绝不静默（真机事故：warmup_cap 拦下
        # 全部回复，运营毫不知情）。event_bus + 集团 TG 双通道（防抖在各自内部）。
        elif str(res.get("error") or "").startswith("send_gate_blocked"):
            try:
                from src.integrations.shared.send_guard import notify_send_blocked
                notify_send_blocked(
                    str(payload.get("platform") or ""),
                    str(payload.get("account_id") or ""),
                    str(res.get("error") or ""))
            except Exception:
                logger.debug("[protocol-autoreply] 限流告警失败", exc_info=True)
        # Phase 4：审计 + 转人工（best-effort，绝不影响主流程）
        try:
            from src.integrations.protocol_autoreply_audit import (
                get_autoreply_audit,
            )
            record_decision_audit(get_autoreply_audit(), payload, res)
        except Exception:
            logger.debug("[protocol-autoreply] 审计写入失败", exc_info=True)
        try:
            if needs_handoff(res):
                tag_needs_human(
                    getattr(app.state, "inbox_store", None), payload,
                    reason=str((res or {}).get("reason") or ""),
                    source="system",
                )
        except Exception:
            logger.debug("[protocol-autoreply] 转人工打标失败", exc_info=True)

    return hook
