"""人设化智能回复——单一事实源（Phase 1：生成产线收敛）。

历史上「给客户拟一条人设化回复」这件事有多处实现：手动「生成草稿」按钮走
``/api/desktop/smart-reply``、协议自动回复走 ``SkillManager.process_message``、
收件箱全自动草稿走规则模板 ``_suggestions``——后者既无人设、又无上下文、又不查 KB，
导致「全自动回复」与「手动生成草稿」质量割裂。

本模块把 ``/api/desktop/smart-reply`` 的产线逻辑（SkillManager 意图→策略→KB→
``AIClient.generate_reply_with_intent``，PersonaManager 注入后台人设、禁机器措辞、
可选译文）抽成**唯一**的异步函数 ``generate_persona_reply``，供以下三处复用：

  1. ``/api/desktop/smart-reply``（手动按钮，薄壳调用）
  2. ``DraftService.auto_generate_draft``（收件箱全自动草稿，Phase 2 接入）
  3. （后续）协议自动回复，统一上下文装配

层级：仅依赖 ``src.ai``（AIClient/TranslationService）、``src.utils.persona_manager``，
不反向依赖 ``src.web``，故可被 inbox/web 双向复用、可纯单测。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _tc_metric(name: str) -> None:
    """时间推理/复读守卫埋点（best-effort，绝不影响生成）。"""
    try:
        from src.monitoring.metrics_store import get_metrics_store
        get_metrics_store().record_inbox_draft_event(name)
    except Exception:
        pass


def _prompt_addenda(
    persona: Any,
    account: Any,
    conv: Any,
    *,
    lang: str = "zh",
    inbound: str = "",
    config: Optional[Dict[str, Any]] = None,
    persona_id: str = "",
) -> str:
    """Q-6 F 单一接线点：相册场景清单 + 本会话已发媒体 + Q-1 身份补丁（+ 无匹配/封锁）。

    空段不出现。纯装配：I/O 失败则跳过该段，绝不抛。
    """
    chunks: List[str] = []
    pid = str(persona_id or "").strip()
    if not pid and isinstance(persona, dict):
        pid = str(persona.get("id") or persona.get("persona_id") or "").strip()
    ck = str(conv or "").strip()

    # ① 相册可展示场景
    try:
        from src.inbox.prompt_addenda import album_scene_addendum
        from src.companion.persona_media import album_kind_counts
        from src.companion.persona_media_store import get_persona_media_store
        rows = []
        if pid:
            st = get_persona_media_store()
            if st is not None:
                rows = st.list(pid, enabled_only=True) or []
        block = album_scene_addendum(album_kind_counts(rows), lang=lang)
        if block:
            chunks.append(block)
    except Exception:
        logger.debug("[persona_reply] album_scene_addendum 跳过", exc_info=True)

    # ② 本会话已发媒体
    try:
        from src.inbox.prompt_addenda import sent_media_addendum
        items: List[Dict[str, Any]] = []
        if ck:
            try:
                from src.companion.persona_media_store import get_persona_media_store
                from src.companion.persona_media import scene_kind_of, row_scene_class
                st = get_persona_media_store()
                if st is not None:
                    hist = st.sent_history(ck, max_age_days=7) or {}
                    by_id = {}
                    try:
                        if pid:
                            for r in st.list(pid) or []:
                                by_id[str(r.get("id") or "")] = r
                    except Exception:
                        by_id = {}
                    for it in list(hist.get("items") or [])[-12:]:
                        mid = str(it.get("id") or "")
                        row = by_id.get(mid) or {}
                        scene = ""
                        if row:
                            scene = scene_kind_of(row) or row_scene_class(row)
                        ts = float(it.get("ts") or 0)
                        when = ""
                        if ts:
                            try:
                                when = time.strftime("%m-%d %H:%M", time.localtime(ts))
                            except Exception:
                                when = ""
                        items.append({"id": mid, "scene": scene, "when": when})
            except Exception:
                items = []
            block = sent_media_addendum(items, conv_known=True, lang=lang)
            if block:
                chunks.append(block)
    except Exception:
        logger.debug("[persona_reply] sent_media_addendum 跳过", exc_info=True)

    # ③ Q-1 身份补丁
    try:
        from src.inbox.prompt_addenda import identity_addendum
        block = identity_addendum(persona, account, lang=lang)
        if block:
            chunks.append(block)
    except Exception:
        logger.debug("[persona_reply] identity_addendum 跳过", exc_info=True)

    # ④ Q-8 F（#264）：当地时间 + 今日日程（借口只从日程取；「工作」类借口同客户每日 ≤1）
    try:
        from src.inbox.excuse_budget import build_time_schedule_addendum
        block = build_time_schedule_addendum(persona, ck, config or {}, lang=lang)
        if block:
            chunks.append(block)
    except Exception:
        logger.debug("[persona_reply] time_schedule_addendum 跳过", exc_info=True)

    # 无匹配回喂：生成前自探相册（B 线 autosend 在拟稿之后，不探则本轮仍会圆谎）
    try:
        from src.ai.companion_selfie import detect_selfie_request, extract_requested_scene
        from src.companion.persona_media import requested_scene_kind
        from src.inbox.prompt_addenda import album_miss_addendum
        from src.inbox.image_autosend import (
            consume_album_miss, pick_registered_media, note_album_miss,
        )
        pending = consume_album_miss(ck) if ck else {}
        scene = (
            requested_scene_kind(inbound)
            or extract_requested_scene(inbound)
            or str((pending or {}).get("scene") or "")
        )
        ask = bool(detect_selfie_request(inbound) or scene or pending)
        miss = False
        if ask and pid:
            row = None
            try:
                row = pick_registered_media(
                    config or {}, pid, inbound, conv_key=ck)
            except Exception:
                row = None
            if row is None:
                miss = True
                if ck:
                    note_album_miss(ck, inbound, scene)
        elif pending:
            miss = True
        if miss:
            block = album_miss_addendum(scene, lang=lang)
            if block:
                chunks.append(block)
    except Exception:
        logger.debug("[persona_reply] album_miss_addendum 跳过", exc_info=True)

    # C/D 封锁提示
    try:
        from src.inbox.media_claim_block import blocked_addendum
        block = blocked_addendum(ck, lang=lang)
        if block:
            chunks.append(block)
    except Exception:
        logger.debug("[persona_reply] blocked_addendum 跳过", exc_info=True)

    return "\n".join(c for c in chunks if c)


class _SkipGoalInject:
    """blocked 期间临时掏空 skill_manager._inject_goal_context（不改 skill_manager.py）。"""

    def __init__(self, sm: Any, active: bool):
        self.sm = sm
        self.active = bool(active) and sm is not None
        self.orig = None

    def __enter__(self):
        if not self.active:
            return self
        self.orig = getattr(self.sm, "_inject_goal_context", None)
        if self.orig is not None:
            def _skip(*_a, **_k):
                return None
            self.sm._inject_goal_context = _skip
        return self

    def __exit__(self, *_exc):
        if self.orig is not None:
            self.sm._inject_goal_context = self.orig
        return False


def trim_stale_history(
    messages: List[Dict[str, Any]],
    *,
    now_ts: Optional[float] = None,
    gap_hours: float = 48.0,
    keep_stale: int = 3,
) -> List[Dict[str, Any]]:
    """按「时间断层」修剪历史窗口——治**陈旧上下文当新鲜话头**的幻觉（纯函数）。

    实锤事故（2026-07-13, conv 1144325634）：历史按条数（limit=30）取、不看时间。
    客户 10 天前聊过"gimme talk English"，10 天后发「给我你的近照」，30 条窗口把
    十天前的英语话题原样喂给 LLM → 产出「诶你突然换英语啦😂」幻觉（07-03 与
    07-13 两次复发，第二次连措辞都在模仿窗口里自己的旧回复）。

    规则（对齐真人记忆的"会话边界"直觉）：
    - 从最新往回找第一个相邻间隔 > ``gap_hours`` 的断层；断层之后 = 新鲜段，全保留。
    - 断层之前最多保留 ``keep_stale`` 条最近的，且在文本前加「[N天前]」时间标记——
      让模型知道那是旧事，可回忆但别当刚说的接话头。
    - 行内无 ``ts``（0/缺失）视为新鲜（向后兼容旧调用方，零行为变更）。
    - ``approx_ts``（实施72 P2）：合成时间戳的补收/回填行——ts 是入库回推值不是
      真实时刻，**间隔判断对它失效**（补收行的合成 ts 紧贴实时消息，永远测不出
      断层）。故先行打「[补收的历史消息，时间不详] 」标，让模型知道这段是旧事、
      别按"刚才"的语境接话（「大半夜回 9 天前消息」类幻觉的上下文层防线）。
    """
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    _out0: List[Dict[str, Any]] = []
    for m in msgs:
        if m.get("approx_ts"):
            m2 = dict(m)
            t = str(m2.get("text") or "")
            if t and not t.startswith("["):
                m2["text"] = "[补收的历史消息，时间不详] " + t
            _out0.append(m2)
        else:
            _out0.append(m)
    msgs = _out0
    if len(msgs) <= 1:
        return list(msgs)
    gap_sec = max(1.0, float(gap_hours)) * 3600.0
    # 找最新的断层位置：msgs 按时间升序；split=断层后首条的下标（0=无断层）。
    split = 0
    for i in range(len(msgs) - 1, 0, -1):
        ts_prev = float(msgs[i - 1].get("ts") or 0)
        ts_cur = float(msgs[i].get("ts") or 0)
        if ts_prev > 0 and ts_cur > 0 and (ts_cur - ts_prev) > gap_sec:
            split = i
            break
    if split <= 0:
        return list(msgs)
    fresh = msgs[split:]
    stale = msgs[max(0, split - max(0, int(keep_stale))):split]
    now = float(now_ts) if now_ts else (
        float(fresh[-1].get("ts") or 0) or None)
    out: List[Dict[str, Any]] = []
    for m in stale:
        m2 = dict(m)
        ts = float(m2.get("ts") or 0)
        if now and ts > 0:
            days = max(1, int((now - ts) // 86400))
            label = f"[{days}天前] " if days < 60 else "[很久以前] "
            t = str(m2.get("text") or "")
            if t and not t.startswith("["):
                m2["text"] = label + t
        out.append(m2)
    out.extend(fresh)
    return out


def normalize_history(
    messages: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], str]:
    """把渠道消息列表归一为 OpenAI 风格 history，并取最后一条入站文本。

    入参 ``messages`` 每项形如 ``{"direction": "in"|"out"|..., "text": "..."}``
    （收件箱 store 行 / 桌面壳 DOM 抓取 / smart-reply 请求体三种来源同构）。

    返回 ``(history, last_inbound)``：
      - history: ``[{"role": "user"|"assistant", "content": str}]``，已滤空。
      - last_inbound: 最后一条 ``direction in {in, inbound}`` 的文本；
        若无入站则回落 history 末条内容（兜底，保证至少有「待回复」锚点）。
    """
    history: List[Dict[str, str]] = []
    last_inbound = ""
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        t = str(m.get("text") or "").strip()
        if not t and (m.get("media_type") or m.get("media_ref")):
            try:
                from src.integrations.protocol_bridge import media_placeholder
                t = media_placeholder(str(m.get("media_type") or ""))
            except Exception:
                t = "[媒体]"
        if not t:
            continue
        is_in = m.get("direction") in ("in", "inbound")
        row: Dict[str, Any] = {"role": "user" if is_in else "assistant", "content": t}
        # P2-198：可选透传 ts——草稿链的时间断层提示（build_time_gap_hint）
        # 需要「上一条入站是什么时候」，Phase8 只在 process_message 链写
        # _turn_gap_sec，草稿链恒缺 → 提示休眠。LLM 组装只读 role/content，
        # 多带 ts 零影响；无 ts 来源（工作台 DOM 抓取）不带 = 行为不变。
        try:
            _ts = float(m.get("ts") or 0)
            if _ts > 0:
                row["ts"] = _ts
        except (TypeError, ValueError):
            pass
        history.append(row)
        if is_in:
            last_inbound = t
    if not last_inbound and history:
        last_inbound = history[-1]["content"]
    return history, last_inbound


def resolve_reply_language(
    last_inbound: str,
    history: List[Dict[str, str]] | None = None,
    *,
    explicit: str = "",
    default: str = "zh",
) -> str:
    """决定「回复正文该用哪种语言」——单一事实源（会话语言策略版）。

    委托 ``src.ai.lang_policy.resolve_conversation_language``（会话级语言契约），
    相对旧实现的行为升级：
      1. ``explicit`` 非空 → 直接采用（手动 UI 选定的目标语，最高优先，不二次猜）。
      2. 用户**明确语言请求**（"用日语聊吧" / "please speak japanese"）→ 立即生效；
         且从历史恢复既往请求（``latest_explicit_request``），无状态产线免存储即
         获得「偏好持久」语义——治「说了用日文还继续发中文」事故。
      3. 语言中性 token（whatsapp/ok/URL/数字/emoji）不构成语言证据——治
         「发一个 whatsapp 整条回复变英文」事故；短消息/弱证据一律粘住会话
         主导语言，只有脚本级/成句级强证据才立即跟随（保留切换敏捷性）。
      4. **default 先看「我们自己最近在用什么语言聊」**（P0-198，2026-08-03）：
         连串 haha/emoji/ok 的全中性窗口里，用户侧一切证据剥空，旧链落到静态
         default（lang_prior/zh）→ 英文会话凭空拟出中文稿（198 实锤链的放大器）。
         会话已建立的工作语言（最近一条有语言证据的 assistant 消息）是比静态先验
         强得多的兜底；仅作 default（最低优先级），任何用户侧证据/请求/窗口主导
         都先于它——用户真切换语言仍即时跟随。

    纯函数、零副作用、可单测；重依赖惰性导入（不抬升模块导入期成本）。
    """
    explicit = str(explicit or "").strip()
    if explicit:
        return explicit
    from src.ai.lang_policy import (
        evidence_lang,
        latest_explicit_request,
        resolve_conversation_language,
    )

    last = str(last_inbound or "").strip()
    pref = latest_explicit_request(history)
    eff_default = default
    for m in reversed(history or []):
        if isinstance(m, dict) and str(m.get("role") or "") == "assistant":
            lg = evidence_lang(str(m.get("content") or ""))
            if lg:
                eff_default = lg
                break
    decision = resolve_conversation_language(
        last,
        history,
        prev_lang="",       # 收件箱产线无跨轮内存状态——粘滞语义由 window/pref 承担
        lang_pref=pref,
        default=eff_default,
    )
    if decision.source not in ("detected", "default"):
        logger.debug(
            "[persona_reply] 语言决策: last=%r → %s (source=%s, pref=%s)",
            last[:20], decision.lang, decision.source, pref or "-",
        )
    return decision.lang or default


def _initial_lang_default(
    app: Any, platform: str, chat_key: str, conversation_id: str = ""
) -> str:
    """新会话首句语言 default（lang_prior 先验；禁用/无先验回落 zh）。

    只影响「决策链全部落空」的场景（新好友首条 Hi/emoji），任何真实语言
    证据/请求/历史窗口都优先——语义与 resolve_reply_language 的 default
    参数一致。account_id 从 conversation_id（platform:account:chat_key）
    解析（generate_persona_reply 签名无 account_id，不为此破坏契约）。
    """
    try:
        from src.ai.lang_prior import initial_lang_hint
        acct = ""
        cid = str(conversation_id or "")
        if cid.count(":") >= 2:
            acct = cid.split(":", 2)[1]
        cm = getattr(getattr(app, "state", None), "config_manager", None)
        return initial_lang_hint(
            platform=platform, account_id=acct, chat_key=chat_key,
            config=getattr(cm, "config", None) or {},
        ) or "zh"
    except Exception:
        return "zh"


def _resolve_persona_badge(
    chat_key: str, persona_id: str, used_persona_default: str
) -> Tuple[str, str]:
    """让人设徽标说真话：返回（实际生效的 persona 标识, tier）。

    按 chat_key 解析会话绑定/账号画像/domain 三级；解析失败回落传入默认值。
    """
    try:
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        resolved, tier = pm.get_persona_with_tier(chat_key or "", persona_id)
        rid = str((resolved or {}).get("id") or "")
        if tier == "account_profile":
            return (rid or persona_id or "domain", tier)
        if tier == "chat_binding":
            return (rid or "domain", tier)
        return ("domain", tier)
    except Exception:
        logger.debug("[persona_reply] persona tier 解析失败", exc_info=True)
        return (used_persona_default, "")


async def _translate_reply(app: Any, reply: str, target_lang: str) -> str:
    """把回复译成 target_lang（chat 风格）。失败/无服务返回空串。

    P0-198（2026-07-31）：中文草稿含拉丁名（『哈哈，我叫Steven啦』）时 detect 会误判
    en → identity 把**中文原文**当「译文」返回 → 工坊「填入并发送」按已译直发 → 中文
    直达外语客户。修法：① 草稿含 CJK 而目标语非 CJK → 显式钉源语言 zh（绕开检测）；
    ② 译文仍含 CJK（identity 回显/引擎失败）→ 按「无译文」返回空串，UI 不再给假译文。
    """
    if not (reply and target_lang):
        return ""
    try:
        from src.ai.translation_service import TranslationService
        from src.inbox.outbound_translate import contains_cjk, lang_is_cjk
        svc = getattr(getattr(app, "state", None), "translation_service", None)
        if not isinstance(svc, TranslationService):
            ai_client = getattr(getattr(app, "state", None), "ai_client", None)
            svc = TranslationService(ai_client=ai_client)
        _conflict = contains_cjk(reply) and not lang_is_cjk(target_lang)
        _kw = {"source_lang": "zh"} if _conflict else {}
        res = await svc.translate(reply, target_lang=target_lang, style="chat", **_kw)
        if res.ok:
            rd = res.to_dict()
            out = rd.get("translated_text") or rd.get("text") or ""
            if _conflict and contains_cjk(out):
                logger.warning(
                    "[persona_reply] 译文仍含中文（provider=%s）→ 按无译文返回，防假译文直发",
                    rd.get("provider") or "-")
                return ""
            return out
    except Exception:
        logger.debug("[persona_reply] 译文失败", exc_info=True)
    return ""


def _persona_anchor_clock(persona_id: str) -> tuple:
    """B 线时间锚点的时钟解析：人设有居住地 → (当地 naive 时间, 地名标签)。

    2026-08-22 双时钟事故修复：``build_now_anchor_hint`` 此前恒用服务器钟，
    与 ai_client 注入的人设当地钟同 prompt 打架（温哥华人设＝「中午」vs
    「深夜」）。调用点在 effective persona 解析**之后**（persona_id 已是
    会话生效人设，与出站 world_clock_guard 同源），锚点钟与守卫钟必然一致。
    解析不出（无 persona / 无居住地）→ ``(None, "")``＝服务器钟旧行为。
    任何异常吞掉——时间锚点绝不阻断拟稿。
    """
    try:
        pid = str(persona_id or "").strip()
        if not pid:
            return None, ""
        from src.utils.persona_manager import PersonaManager
        p = PersonaManager.get_instance().get_persona_by_id(pid)
        if not isinstance(p, dict):
            return None, ""
        from src.companion.persona_location import (
            persona_now,
            resolve_place_with_fallback,
        )
        place = resolve_place_with_fallback(p)
        if place is None:
            return None, ""
        return persona_now(place), place.display("zh")
    except Exception:
        return None, ""


async def generate_persona_reply(
    *,
    app: Any,
    platform: str,
    chat_key: str,
    last_inbound: str,
    history: List[Dict[str, str]],
    persona_id: str = "",
    target_lang: str = "",
    reply_lang: str = "",
    risk_level: str = "",
    media_type: str = "",
    media_ref: str = "",
    media_desc: str = "",
    conversation_id: str = "",
    peer_audio_emotion: Optional[Dict[str, Any]] = None,
    account_id: str = "",
    gloss_lang: str = "",
    agent_instruction: str = "",
    inbound_msg_id: str = "",
) -> Dict[str, Any]:
    """人设化智能回复入口（实现见 :func:`_generate_persona_reply_impl`，签名逐字相同）。

    实施97：以 ``platform`` 打合规平台作用域（``compliance.runtime.platform_scope``）——微信客服等
    强制平台上，链路内的 ``honest_identity_active()``/``notice_active()`` 读到即恒 True，人设
    ``deny_ai`` 被按 False 处理、出站守卫放行如实身份，无需把 platform 穿透 skill_manager。
    """
    from src.compliance.runtime import platform_scope
    with platform_scope(platform):
        return await _generate_persona_reply_impl(
            app=app, platform=platform, chat_key=chat_key, last_inbound=last_inbound,
            history=history, persona_id=persona_id, target_lang=target_lang,
            reply_lang=reply_lang, risk_level=risk_level, media_type=media_type,
            media_ref=media_ref, media_desc=media_desc, conversation_id=conversation_id,
            peer_audio_emotion=peer_audio_emotion, account_id=account_id,
            gloss_lang=gloss_lang, agent_instruction=agent_instruction,
            inbound_msg_id=inbound_msg_id,
        )


async def _generate_persona_reply_impl(
    *,
    app: Any,
    platform: str,
    chat_key: str,
    last_inbound: str,
    history: List[Dict[str, str]],
    persona_id: str = "",
    target_lang: str = "",
    reply_lang: str = "",
    risk_level: str = "",
    media_type: str = "",
    media_ref: str = "",
    media_desc: str = "",
    conversation_id: str = "",
    peer_audio_emotion: Optional[Dict[str, Any]] = None,
    account_id: str = "",
    gloss_lang: str = "",
    agent_instruction: str = "",
    inbound_msg_id: str = "",
) -> Dict[str, Any]:
    """人设化智能回复（单一事实源）。

    走与 ``/api/chat/test`` 同一条产线：SkillManager 识别意图 → 取回复策略 →
    KB 检索 → ``AIClient.generate_reply_with_intent``（PersonaManager 注入后台人设、
    禁「作为AI」机器措辞、融合知识库）。SkillManager 不可用时回落通用提示词，
    保证至少有草稿。

    入参：
      - app: 暴露 ``app.state``（skill_manager / ai_client / kb_store / translation_service）
      - history: ``normalize_history`` 产物（``[{role, content}]``）
      - last_inbound: 待回复的最后一条客户文本（空则直接返回 ok=False）
      - reply_lang: 生成正文语言。显式传入=最高优先；为空则回落 target_lang；
        再为空则由 ``resolve_reply_language`` 按 last_inbound/history 自动决策
        （含短消息防误切护栏）。
      - target_lang: 坐席手动选定的目标语；既作正文语言回落，又触发**额外译文**
        （``translated`` 字段）。
      - gloss_lang: 坐席 UI 语言（P2-198「直出模式」）。正文按客户语言直出时，
        额外产出一份该语言的**对照译文**（``gloss`` 字段，仅供坐席阅读、不用于发送）
        ——中文坐席从此不必把工坊切到「中文」生成再翻回去（那条链 = LLM 生成 →
        语言守卫改写 → 出站再翻译，一条回复烧 3 次 LLM 且质量经两次转译损耗）。
        已有跨语言 ``translated``（如坐席显式选中文正文 + 英文译文）时不再叠加。
      - agent_instruction: 坐席显式指令（P22「采纳并拟稿」）。进
        ``user_context["_agent_instruction"]`` 高权重块；空=旧行为。

    返回：``{ok, reply, reply_lang, persona, persona_tier, intent, translated?, gloss?}``
      - reply_lang: 本次实际采用的正文语言（决策结果，供调用方落库 draft_lang，
        无需再各自重复检测——语言决策在此收敛为单一事实源）。
    """
    last_inbound = str(last_inbound or "").strip()
    if not last_inbound:
        return {"ok": False, "detail": "无可用对话上下文", "reply": ""}

    # 分段计时（2026-08-01 观测补齐）：总耗时 [smart_reply] ms= 早就有，但「今早那次
    # 9.5 秒到底慢在生成还是翻译」无从回答——gen/xlate/gloss 三段 + 生成走了哪条路
    # （unified/direct/fallback）随 out["timings"] 返回，smart-reply 路由并进既有日志行
    # 后 pop 掉（响应契约不变）；其他调用方拿到即弃，零行为影响。
    _t_gen0 = time.monotonic()
    _gen_path = "none"

    # 语言决策单一事实源：显式 reply_lang > 坐席 target_lang > 自动决策(含短消息防误切)。
    # 自动决策的最终 default 接 lang_prior 先验（新好友首条中性消息 → 按账号
    # 配置/WA 国码定首句语言；有任何真实语言证据时先验不参与）。
    resolved_lang = (
        str(reply_lang or "").strip()
        or str(target_lang or "").strip()
        or resolve_reply_language(
            last_inbound, history,
            default=_initial_lang_default(app, platform, chat_key, conversation_id),
        )
    )

    # 账号解析（提早到语言决策后：人设解析/统一引擎两处共用）：
    # 显式 account_id 优先；缺省从 conversation_id（platform:account:chat_key）取。
    _acct = str(account_id or "").strip()
    if (not _acct or _acct == "default") and conversation_id:
        _parts = str(conversation_id).split(":", 2)
        if len(_parts) >= 3 and _parts[1]:
            _acct = str(_parts[1]).strip()

    # 人设决策单一事实源（2026-07-26 方案 A）：显式 persona_id（坐席在草稿面板
    # 手选，一次性生效）最高优先；为空 → 与出站链同一 resolver 补全
    # （会话覆写 > 账号人设；两者皆无 → 保持空串走 legacy chat/domain 链）。
    # _eff_tier 非空 = persona_id 出自 resolver，徽标直接采信该 tier（说真话）。
    _eff_tier = ""
    if not str(persona_id or "").strip():
        try:
            from src.ai.persona_voice import resolve_effective_persona
            _cm = getattr(getattr(app, "state", None), "config_manager", None)
            _pid_r, _tier_r = resolve_effective_persona(
                getattr(_cm, "config", None) or {},
                platform, _acct, str(chat_key or ""),
            )
            if _pid_r:
                persona_id, _eff_tier = _pid_r, _tier_r
        except Exception:
            logger.debug("[persona_reply] effective persona 解析跳过", exc_info=True)

    state = getattr(app, "state", None)
    sm = getattr(state, "skill_manager", None)
    if sm is None:
        tc = getattr(state, "telegram_client", None)
        sm = getattr(tc, "skill_manager", None) if tc is not None else None
    ai = getattr(state, "ai_client", None)

    # ── 时间推理锚点（P0 2026-08-12，实录：智能回复重答 4 天前已答过的问题）──
    # B 线唯一入口在此判定「待回复的最后一条入站」新鲜度：
    #   stale_answered（陈旧且已答/我方在单向连发）→ 切开场产线做跟进（reply
    #     语义此时必然产出「重答旧问题」）；坐席显式指令在场时不切（尊重人的
    #     明确意图，只保留提示注入）。
    #   stale_unanswered → 保持 reply，但注入「迟回复要带时间感」提示。
    #   fresh + 上一条用户消息距今很久 → 注入既有「对方刚回来」提示
    #     （build_time_gap_hint 措辞，之前因 ts 从不透传而全链休眠）。
    # 客户端行无 ts 时按 conversation_id 反查 inbox store 的真实时间线
    # （工作台/cp-draft 只传 {direction,text}，store 才是它们的时间真相；
    # 结构判定不依赖 ts，桌面 DOM 抓不到时间也能兜住实录事故）。
    _tc_meta: Dict[str, Any] = {}
    _time_hint = ""
    _tccfg: Dict[str, Any] = {}
    try:
        from src.inbox.time_context import (
            build_followup_note,
            build_late_reply_hint,
            build_now_anchor_hint,
            classify_reply_anchor,
            resolve_time_context_cfg,
        )
        _cm_tc = getattr(state, "config_manager", None)
        _tccfg = resolve_time_context_cfg(getattr(_cm_tc, "config", None) or {})
        if _tccfg.get("enabled"):
            _stale_sec = float(_tccfg["stale_after_hours"]) * 3600.0
            _anchor = classify_reply_anchor(
                history, stale_after_sec=_stale_sec,
                min_monologue=int(_tccfg["min_monologue"]))
            _anchor["source"] = "client"
            if _anchor["kind"] == "fresh" and not _anchor["ts_known"] \
                    and _tccfg.get("store_lookup"):
                _ibx_tc = getattr(state, "inbox_store", None)
                _cid_tc = str(conversation_id or "").strip()
                if not _cid_tc and platform and _acct and chat_key:
                    from src.inbox.normalizer import conv_id as _conv_id_fn
                    _cid_tc = _conv_id_fn(platform, _acct, str(chat_key))
                if _ibx_tc is not None and _cid_tc:
                    try:
                        # B87：对端手机删的消息（软删）不再当「客户最近说的」话题锚点
                        # ——钧口径「AI 可记但不主动提已删内容」在拟稿链上的落点。
                        # 旧 store 无 include_deleted 形参 → TypeError 回落旧签名。
                        try:
                            _rows_tc = _ibx_tc.list_recent_messages(
                                _cid_tc, limit=int(_tccfg["store_limit"]),
                                include_deleted=False)
                        except TypeError:
                            _rows_tc = _ibx_tc.list_recent_messages(
                                _cid_tc, limit=int(_tccfg["store_limit"]))
                        _a2 = classify_reply_anchor(
                            _rows_tc, stale_after_sec=_stale_sec,
                            min_monologue=int(_tccfg["min_monologue"]))
                        if _a2.get("ts_known"):
                            _a2["source"] = "store"
                            _anchor = _a2
                    except Exception:
                        logger.debug("[persona_reply] 锚点 store 反查跳过",
                                     exc_info=True)
            _tc_meta = {
                "kind": _anchor["kind"], "ts_known": _anchor["ts_known"],
                "age_hours": round(float(_anchor["age_sec"]) / 3600.0, 1),
                "outbound_after": _anchor["outbound_after"],
                "source": _anchor.get("source") or "client",
            }
            if (
                _anchor["kind"] == "stale_answered"
                and not str(agent_instruction or "").strip()
            ):
                _tc_metric("time_anchor_followup")
                out = await generate_topic_opener(
                    app=app, platform=platform, chat_key=chat_key,
                    history=history, persona_id=persona_id,
                    target_lang=target_lang,
                    conversation_id=conversation_id, account_id=_acct,
                    gloss_lang=gloss_lang,
                    followup_note=build_followup_note(_anchor),
                )
                out["time_anchor"] = dict(_tc_meta, followup=True)
                return out
            # 双时钟事故修复（2026-08-22）：锚点钟＝人设当地钟（有居住地时），
            # 与 ai_client 的【当前真实时间】同源；无居住地回落服务器钟。
            _anchor_now, _anchor_place = _persona_anchor_clock(persona_id)
            _hints: List[str] = [build_now_anchor_hint(
                local_now=_anchor_now, place_label=_anchor_place)]
            if _anchor["kind"] == "stale_unanswered" and _anchor["ts_known"]:
                _tc_metric("time_anchor_late_hint")
                _hints.append(build_late_reply_hint(_anchor["age_sec"]))
            elif (
                _anchor["kind"] == "fresh"
                and float(_anchor.get("prev_user_gap_sec") or 0)
                >= float(_tccfg["return_gap_hours"]) * 3600.0
            ):
                from src.inbox.inbound_enrich import build_time_gap_hint
                _gh = build_time_gap_hint(_anchor["prev_user_gap_sec"])
                if _gh:
                    _tc_metric("time_anchor_return_hint")
                    _hints.append(_gh)
            _time_hint = "\n".join(h for h in _hints if h)
    except Exception:
        logger.debug("[persona_reply] 时间锚点判定跳过", exc_info=True)

    # 驾驶舱 P2（2026-08-13）：交接提醒——刚被人工接管处理过的会话，AI 接回后
    # 第一批稿要衔接人工说过的内容（防交还后自相矛盾/重新自我介绍的穿帮）。
    # 与时间锚点共用 extra_hint 单一消费口 → 统一引擎/直连回落/终极兜底三条
    # 生成路径一处接线全覆盖。TTL 窗判定在 takeover.handback_note 内；异常跳过。
    try:
        from src.inbox.takeover import handback_note as _hb_note
        _ibx_hb = getattr(state, "inbox_store", None)
        _hb = _hb_note(str(conversation_id or ""), store=_ibx_hb)
        if _hb:
            _time_hint = f"{_time_hint}\n{_hb}" if _time_hint else _hb
    except Exception:
        logger.debug("[persona_reply] 交接提醒跳过", exc_info=True)

    # P-1 C（#259 #254 · 34585H E/F）：生成侧「AI 指纹」硬禁 + few-shot——禁破折号 / 分号 /
    # 列表、不引用上下文没有的「对方说过」、无历史不假装熟悉、一两句。与时间锚点共用
    # extra_hint 单一消费口 → 统一引擎 / 直连 / 兜底三条路径一处全覆盖。后处理
    # （enrich_draft 起草层 humanize + claim_guard）仍是最终保证。配置 inbox.auto_draft.style_hint。
    try:
        from src.inbox.draft_style_hint import build_style_hint, history_has_peer_turns
        _cm_sh = getattr(state, "config_manager", None)
        _sh = build_style_hint(
            resolved_lang, has_history=history_has_peer_turns(history),
            config=getattr(_cm_sh, "config", None) or None, sample_text=str(last_inbound or ""))
        if _sh:
            _time_hint = f"{_time_hint}\n{_sh}" if _time_hint else _sh
    except Exception:
        logger.debug("[persona_reply] style hint 跳过", exc_info=True)

    # Q-6 A/C/D/F（#263 #266）：单一接线点注入相册场景 / 已发媒体 / 身份补丁；
    # 入站质问记 spiral；blocked 期间 goal-inject 在调用侧掏空。
    _persona_obj: Any = None
    _account_obj: Any = None
    try:
        if persona_id:
            from src.utils.persona_manager import PersonaManager
            _persona_obj = PersonaManager.get_instance().get_persona_by_id(str(persona_id))
    except Exception:
        _persona_obj = None
    try:
        from src.integrations.account_registry import get_account_registry
        if platform and _acct:
            _account_obj = get_account_registry().get(platform, _acct)
    except Exception:
        _account_obj = None
    _cid_q6 = str(conversation_id or "").strip()
    _blocked_now = False
    try:
        from src.ai.companion_selfie import detect_media_complaint
        from src.inbox.media_claim_block import (
            SPIRAL_THRESHOLD, is_blocked, note_lie_caught, spiral_count,
        )
        from src.inbox.image_autosend import last_media_receipt
        _pending = is_blocked(_cid_q6) or bool(last_media_receipt(_cid_q6))
        _ckind = detect_media_complaint(last_inbound, media_pending=bool(_pending))
        if _ckind in ("lie_caught", "unfulfilled") and _cid_q6:
            rec = note_lie_caught(_cid_q6, kind=_ckind)
            if int(rec.get("spiral_n") or 0) >= SPIRAL_THRESHOLD:
                try:
                    from src.integrations.protocol_autoreply import tag_needs_human
                    _ibx_h = getattr(state, "inbox_store", None)
                    tag_needs_human(
                        _ibx_h,
                        {"platform": platform, "account_id": _acct, "chat_key": chat_key},
                        reason="media_lie_caught_repeat",
                        source="media_claim_block",
                    )
                except Exception:
                    logger.debug("[persona_reply] lie_caught 转人工跳过", exc_info=True)
        _blocked_now = is_blocked(_cid_q6)
        _ = spiral_count  # 保留导入供调试；计数已在 note 里
    except Exception:
        logger.debug("[persona_reply] media_claim_block 跳过", exc_info=True)
    try:
        _cm_q6 = getattr(state, "config_manager", None)
        _cfg_q6 = getattr(_cm_q6, "config", None) or {}
        _add = _prompt_addenda(
            _persona_obj or persona_id, _account_obj or _acct, _cid_q6,
            lang=resolved_lang, inbound=last_inbound, config=_cfg_q6,
            persona_id=str(persona_id or ""),
        )
        if _add:
            _time_hint = f"{_time_hint}\n{_add}" if _time_hint else _add
    except Exception:
        logger.debug("[persona_reply] _prompt_addenda 跳过", exc_info=True)

    # P1-198 续（2026-08-02）：坐席「客户情绪」人工标注 → 拟稿指令。生效判据
    # （TTL/标签在场）与 NBA 卡「生效中」徽标同源（effective_mood 单一仲裁）；
    # 显式坐席指令优先，标注句仅在余量内追加（merge_agent_instruction）。
    # 本函数是 B 线唯一入口（auto-draft / 工坊智能回复 / replybus 都经这里）——
    # 一处接线全链生效。任何异常按无标注处理，绝不阻断拟稿。
    try:
        _ibx = getattr(state, "inbox_store", None)
        if _ibx is not None and str(conversation_id or "").strip():
            from src.inbox.effective_mood import (
                agent_mood_directive,
                merge_agent_instruction,
                record_mood_consume,
                resolve_mood_steering_cfg,
            )
            _cmgr = getattr(state, "config_manager", None)
            _ms = resolve_mood_steering_cfg(getattr(_cmgr, "config", None) or {})
            if _ms["enabled"]:
                _mood_meta = _ibx.get_conv_meta(str(conversation_id)) or {}
                _mdir = agent_mood_directive(
                    _mood_meta, now=time.time(), ttl_hours=_ms["ttl_hours"])
                if _mdir:
                    agent_instruction = merge_agent_instruction(
                        agent_instruction, _mdir)
                    record_mood_consume("draft_directive")
    except Exception:
        logger.debug("[persona_reply] mood directive 注入跳过", exc_info=True)

    # 对方机器人守卫 P1（2026-08-03）：疑似自动化对方（bot_score 达灰区观察线
    # 或已判定 bot、且未被运营覆写为真人）→ 拟稿注入「礼貌收尾勿追问」感知块。
    # 与 mood directive 同一注入口（B 线唯一入口，一处接线全链生效）；
    # best-effort，绝不阻断拟稿。
    try:
        _ibx_pb = getattr(state, "inbox_store", None)
        if _ibx_pb is not None and str(conversation_id or "").strip():
            from src.inbox.effective_mood import (
                merge_agent_instruction as _pb_merge,
            )
            from src.inbox.peer_bot_guard import draft_awareness_hint
            _cmgr_pb = getattr(state, "config_manager", None)
            _pb_row = _ibx_pb.get_conversation(str(conversation_id)) or {}
            _pb_hint = draft_awareness_hint(
                _pb_row, getattr(_cmgr_pb, "config", None) or {})
            if _pb_hint:
                agent_instruction = _pb_merge(agent_instruction, _pb_hint)
    except Exception:
        logger.debug("[persona_reply] peer_bot hint 注入跳过", exc_info=True)

    reply = None
    used_persona = ""
    used_intent = ""
    kb_refs: list = []    # P2 证据链：本稿引用的 KB 条目（前端知识 chip / 工坊 chips 用）
    used_unified = False  # 统一引擎已自带记忆写回 → 避免文末重复写
    goal_applied: Optional[Dict[str, Any]] = None  # P25：目标注入观测（透传前端）
    _goal_skip = _SkipGoalInject(sm, bool(_blocked_now))
    _goal_skip.__enter__()

    # ★ 统一规则引擎（单一事实源·彻底对齐）：优先走 SkillManager.generate_inbox_draft，
    # 与原生 bot/RPA 同享情感引擎/陪伴阶段/慢思考/人设守卫/危机兜底/记忆读写全栈规则。
    # 可经 config inbox.auto_draft.unified_pipeline=false 秒级回退到下方直连路径。
    if (
        sm is not None
        and hasattr(sm, "generate_inbox_draft")
        and getattr(sm, "ai_client", None) is not None
    ):
        _unified_on = True
        try:
            _cfg = getattr(getattr(sm, "config", None), "config", None) or {}
            _unified_on = bool(
                ((_cfg.get("inbox") or {}).get("auto_draft") or {})
                .get("unified_pipeline", True)
            )
        except Exception:
            _unified_on = True
        if _unified_on:
            try:
                _res = await sm.generate_inbox_draft(
                    text=last_inbound,
                    chat_key=chat_key,
                    platform=platform,
                    history=history,
                    persona_id=persona_id,
                    reply_lang=resolved_lang,
                    risk_level=risk_level,
                    media_type=media_type,
                    media_ref=media_ref,
                    media_desc=media_desc,
                    conversation_id=conversation_id,
                    peer_audio_emotion=peer_audio_emotion,
                    account_id=_acct,
                    agent_instruction=str(agent_instruction or "").strip()[:400],
                    inbound_msg_id=str(inbound_msg_id or "").strip(),
                    extra_hint=_time_hint,
                )
                if _res and (_res.get("reply") or "").strip():
                    reply = _res["reply"]
                    used_intent = _res.get("intent") or ""
                    used_persona = persona_id or "domain"
                    used_unified = True
                    _gen_path = "unified"
                    kb_refs = list(_res.get("kb_refs") or [])
                    _ga = _res.get("goal_applied")
                    if isinstance(_ga, dict):
                        goal_applied = dict(_ga)
            except Exception:
                logger.debug("[persona_reply] 统一引擎失败，回落直连", exc_info=True)
                reply = None

    # 主路径（回落）：人设 + KB + 策略（与 /api/chat/test 一致）
    if not reply and sm is not None and getattr(sm, "ai_client", None) is not None:
        try:
            user_id = f"desktop:{platform}:{chat_key}" or "__desktop__"
            intent = sm._recognize_intent(last_inbound)
            used_intent = intent
            try:
                strategy, _sid = sm.get_strategy_for_intent(intent, user_id)
            except Exception:
                strategy = {}
            kb_context = ""
            kb = getattr(state, "kb_store", None)
            if kb is not None:
                try:
                    _res = kb.search(last_inbound, top_k=3, lang="zh")
                    kb_context = kb.build_ai_context_from_result(_res, lang="zh")
                    try:
                        from src.utils.kb_refs import extract_kb_refs
                        kb_refs = extract_kb_refs(_res)
                    except Exception:
                        kb_refs = []
                except Exception:
                    kb_context = ""
            ctx: Dict[str, Any] = {
                "user_id": user_id,
                "chat_id": chat_key or user_id,
                "channel": "desktop",
                "platform": platform,
                "intent": intent,
                "current_intent": intent,
                "_reply_strategy": strategy or {},
                "reply_lang": resolved_lang,
            }
            if persona_id:
                ctx["account_persona_id"] = persona_id
            if len(history) > 1:
                hist = history[:-1] if history[-1]["role"] == "user" else history
                ctx["_conversation_history"] = hist[-20:]
            if kb_context:
                ctx["kb_context"] = kb_context
            _ainst = str(agent_instruction or "").strip()[:400]
            if _ainst:
                ctx["_agent_instruction"] = _ainst
            # 时间推理提示（与统一引擎同口径）：直连回落路径也要有时间感，
            # 否则引擎一异常回落，时间盲就静默复发。
            if _time_hint:
                ctx["_topic_switch_hint"] = _time_hint
            # P25（2026-08-05）：直连回落路径此前不注入工作目标——统一引擎一旦
            # 异常回落，目标方向就静默消失。与统一链同一注入口补平，观测元数据
            # 同样透传（chain 仍记 draft：同一产线的降级形态，不是新链）。
            if hasattr(sm, "_inject_goal_context"):
                try:
                    sm._inject_goal_context(
                        ctx, platform=platform, chat_key=str(chat_key or ""),
                        account_id=_acct, conversation_id=conversation_id,
                        chain="draft", inbound_text=last_inbound,
                    )
                except Exception:
                    logger.debug(
                        "[persona_reply] direct 目标注入跳过", exc_info=True)
                _gm = ctx.get("_goal_inject_meta")
                if isinstance(_gm, dict):
                    goal_applied = dict(_gm)
            # ★ 情景记忆注入（单一事实源补全）：全自动/手动产线此前不读长期记忆，导致
            # 「跨会话记不住（如名字）」。复用 SkillManager 既有读取逻辑，按 chat_key 命中
            # 该联系人的长期事实，写入 ctx["_episodic_memory_text"]——generate_reply 会把它
            # 作为「用户长期记忆要点」注入系统提示。读/写用同一 key，保证闭环一致。
            if chat_key and hasattr(sm, "_inject_episodic_into_context"):
                try:
                    sm._inject_episodic_into_context(
                        ctx, str(chat_key), "",
                        current_user_text=last_inbound,
                        platform=platform,
                    )
                except Exception:
                    logger.debug("[persona_reply] 情景记忆注入跳过", exc_info=True)
            so: Dict[str, Any] = {}
            for _sk in ("temperature", "max_tokens", "context_rounds", "model", "thinking_budget"):
                if _sk in (strategy or {}):
                    so[_sk] = strategy[_sk]
            reply = await sm.ai_client.generate_reply_with_intent(
                user_message=last_inbound,
                intent=intent,
                user_context=ctx,
                strategy_overrides=so or None,
            )
            # 目标注入的配平守卫（统一引擎内部第 9 步同口径）：直连路径现在
            # 也可能带出目录链接/价格措辞，商业事实必须过同一道闸
            if reply and hasattr(sm, "_apply_goal_link_guard"):
                try:
                    reply = sm._apply_goal_link_guard(
                        reply, ctx, log_prefix="[persona_reply] ")
                except Exception:
                    logger.debug(
                        "[persona_reply] direct 目标守卫跳过", exc_info=True)
            used_persona = persona_id or "domain"
            if reply:
                _gen_path = "direct"
        except Exception:
            logger.debug("[persona_reply] 人设主路径失败，回落通用", exc_info=True)
            reply = None

    # 兜底：SkillManager 不可用时退回通用提示词（保证至少有草稿）
    if not reply and ai is not None:
        lines = []
        for m in history[-12:]:
            who = "客户：" if m.get("role") == "user" else "我："
            lines.append(who + str(m.get("content") or ""))
        # 兜底走 ai.chat（不过 generate_reply_with_intent，故无 LANGUAGE RULE 守卫），
        # 这里直接把语言要求写进提示，避免兜底路径回错语言。
        _lang_hint = (
            f"必须完全使用客户的语言（{resolved_lang}）回复，不要夹杂其它语言。"
            if resolved_lang and resolved_lang != "zh" else ""
        )
        _ainst = str(agent_instruction or "").strip()[:400]
        _inst_block = (
            f"\n【坐席指令——本条必须完成】\n{_ainst}\n"
            if _ainst else ""
        )
        _time_block = f"\n{_time_hint}\n" if _time_hint else ""
        prompt = (
            "你是温暖、自然、像真人一样的线上陪伴/客服。基于以下对话，草拟我的下一条回复。"
            f"{_lang_hint}"
            f"{_inst_block}"
            f"{_time_block}"
            "口吻自然口语化，禁止出现「作为AI/作为一个AI/有什么可以帮您」等机器措辞，"
            "只输出回复正文。\n\n对话：\n" + "\n".join(lines) + "\n\n我的回复："
        )
        try:
            reply = await ai.chat(prompt)
            if reply:
                _gen_path = "fallback"
        except Exception:
            logger.debug("[persona_reply] 兜底失败", exc_info=True)
            reply = None

    try:
        _goal_skip.__exit__(None, None, None)
    except Exception:
        pass

    reply = (reply or "").strip()
    if reply and _cid_q6:
        try:
            from src.ai.outbound_promise_guard import apply_blocked_media_rewrite
            reply = apply_blocked_media_rewrite(
                reply, _cid_q6, sample_text=last_inbound) or reply
        except Exception:
            logger.debug("[persona_reply] blocked rewrite 跳过", exc_info=True)
        try:
            from src.inbox.media_claim_block import tick_outbound
            tick_outbound(_cid_q6)
        except Exception:
            pass
    # ── 出站复读守卫（P0 2026-08-12）：生成稿与我方最近已发消息近重复 → 带
    # 负样本重生成一次（相似度归一化/阈值与 proactive_variety 生产校准同源）。
    # 实录：智能回复几乎逐字复读了 4 天前已发出的回答——对方看过的话原样再说
    # 一遍是最快的穿帮方式。仅统一引擎路径做重写（生产主路径）；直连/兜底路径
    # 只打标不重写（repeat_risk 透传给调用方与日志）。
    _repeat_risk = False
    if reply and _tccfg.get("enabled") and _tccfg.get("repeat_guard"):
        try:
            from src.utils.proactive_variety import most_similar
            _recent_out = [
                str(m.get("content") or "") for m in (history or [])
                if isinstance(m, dict) and m.get("role") == "assistant"
            ][-6:]
            _thr = float(_tccfg.get("repeat_threshold") or 0.6)
            _dup = most_similar(reply, _recent_out, threshold=_thr)
            if _dup:
                _tc_metric("repeat_guard_hit")
                if used_unified and sm is not None:
                    from src.inbox.time_context import build_repeat_rewrite_hint
                    _res2 = await sm.generate_inbox_draft(
                        text=last_inbound,
                        chat_key=chat_key,
                        platform=platform,
                        history=history,
                        persona_id=persona_id,
                        reply_lang=resolved_lang,
                        risk_level=risk_level,
                        media_type=media_type,
                        media_ref=media_ref,
                        media_desc=media_desc,
                        conversation_id=conversation_id,
                        peer_audio_emotion=peer_audio_emotion,
                        account_id=_acct,
                        agent_instruction=str(agent_instruction or "").strip()[:400],
                        inbound_msg_id=str(inbound_msg_id or "").strip(),
                        extra_hint=((_time_hint + "\n") if _time_hint else "")
                        + build_repeat_rewrite_hint(_dup),
                    )
                    _r2 = str((_res2 or {}).get("reply") or "").strip()
                    if _r2:
                        reply = _r2
                        _tc_metric("repeat_guard_rewritten")
                        if most_similar(reply, _recent_out, threshold=_thr):
                            _repeat_risk = True
                            _tc_metric("repeat_guard_stuck")
                    else:
                        _repeat_risk = True
                else:
                    _repeat_risk = True
                if _repeat_risk:
                    logger.warning(
                        "[persona_reply] 复读守卫：生成稿与最近出站雷同且未能改写"
                        "（conv=%s）", conversation_id or chat_key)
        except Exception:
            logger.debug("[persona_reply] 复读守卫跳过", exc_info=True)
    # ★ 情景记忆写回（闭环）：本轮成功生成回复后，按与读取相同的 key 抽取并落库事实，
    # 让全自动/手动产线像 native bot 一样「越聊越记得」。fire-and-forget——绝不阻塞、
    # 失败也不影响回复发送；记忆开关/抽取意图门控仍由 SkillManager 内部既有逻辑把关。
    if reply and not used_unified and sm is not None and chat_key and hasattr(sm, "_episodic_memory_extract_async"):
        try:
            import asyncio as _aio
            _aio.create_task(sm._episodic_memory_extract_async(
                str(chat_key), last_inbound, reply, used_intent or "", "", platform,
            ))
        except Exception:
            logger.debug("[persona_reply] 情景记忆写回调度跳过", exc_info=True)
    persona_tier = ""
    if reply:
        if persona_id and _eff_tier:
            # resolver 补全的 persona（含会话覆写）→ tier 直接采信，徽标说真话
            used_persona, persona_tier = persona_id, _eff_tier
        else:
            used_persona, persona_tier = _resolve_persona_badge(
                chat_key, persona_id, used_persona
            )

    _gen_ms = int((time.monotonic() - _t_gen0) * 1000)

    out: Dict[str, Any] = {
        "ok": bool(reply),
        "reply": reply,
        "reply_lang": resolved_lang,
        "persona": used_persona,
        "persona_tier": persona_tier,
        "intent": used_intent,
        "kb_refs": kb_refs,
        # P25 观测：目标有没有进本条草稿、为什么（meta 缺失=生成走了无引擎
        # 兜底或旧引擎，如实标 unknown 不编原因）
        "goal_applied": goal_applied or {"injected": False, "reason": "unknown"},
    }
    if _tc_meta:
        out["time_anchor"] = _tc_meta
    if _repeat_risk:
        out["repeat_risk"] = True
    _t_x0 = time.monotonic()
    translated = await _translate_reply(app, reply, target_lang)
    if translated:
        out["translated"] = translated
    _t_g0 = time.monotonic()
    await _attach_gloss(app, out, reply, resolved_lang, gloss_lang)
    _t_end = time.monotonic()
    out["timings"] = {
        "gen_ms": _gen_ms,
        "xlate_ms": int((_t_g0 - _t_x0) * 1000),
        "gloss_ms": int((_t_end - _t_g0) * 1000),
        "gen_path": _gen_path,
    }
    return out


async def _attach_gloss(
    app: Any, out: Dict[str, Any], reply: str,
    resolved_lang: str, gloss_lang: str,
) -> None:
    """P2-198 直出模式：正文是客户语言时，附一份坐席 UI 语言的对照译文（只读）。

    条件：有正文 + 请求了 gloss + 正文语言 ≠ gloss 语言 + 尚无「真的跨语言」
    translated（translated==reply 是「指定语言直出」的回显，不算）。
    对照走 MT/翻译栈而非再烧一次 LLM；失败静默（对照缺失不阻断草稿）。
    """
    g_lang = str(gloss_lang or "").strip().lower()
    if not (reply and g_lang):
        return
    if g_lang.split("-")[0] == str(resolved_lang or "").strip().lower().split("-")[0]:
        return
    _t = str(out.get("translated") or "")
    if _t and _t != reply:
        return  # 已有跨语言译文可读，不再叠第二份对照
    try:
        g = await _translate_reply(app, reply, g_lang)
    except Exception:
        logger.debug("[persona_reply] gloss 生成失败（忽略）", exc_info=True)
        return
    if g and g.strip() and g != reply:
        out["gloss"] = g
        out["gloss_lang"] = g_lang


# ── P1-198：工坊「开启新话题」模式（2026-07-31）─────────────────────────────────
# 客户实测诉求：回复工坊只会「承接上文」，坐席想主动推进节奏（暖场/换话题）没有工具。
# 刻意**不走** SkillManager.generate_inbox_draft 统一引擎——那是「回复客户消息」的
# 语义（意图识别/记忆写回都按客户消息设计），把系统指令喂进去会污染意图统计与记忆。
# 这里独立组装：人设解析与回复链同一 resolver、语言与回复链同一决策器、切入点复用
# proactive 子系统的轮换词表（同一套「防模板壳」纪律），生成后走同一 persona 徽标
# 与译文护栏。**不写记忆**（没有新的客户事实）。

def build_opener_directive(
    *,
    angle: str,
    reply_lang: str,
    recent_out: Optional[List[str]] = None,
    goal_intent: str = "",
    goal_push: str = "",
    goal_title: str = "",
    followup_note: str = "",
) -> str:
    """主动开场生成指令（纯函数，可单测）。

    纪律与 proactive P19 同源：禁「好久没联系/最近怎么样/在吗」模板壳；优先跟进
    记忆/历史里的具体事实；给出当日轮换的切入点参考；列出最近已发内容防复读。

    P25（2026-08-05）目标接入：此前「开新话题」是唯一完全不消费工作目标的
    生成链（坐席设「获取客户职业」，开场仍按轮换词表聊红酒相册）。有活跃目标
    且今日力度 soft/direct → 切入点让位给目标的今日推进方向（angle 降为备选，
    记忆跟进也限定「与该方向相关」防两个「优先」打架）；力度 none（今天只
    陪伴）或无目标 → 输出与旧版逐字一致。

    ``followup_note``（P0 2026-08-12 时间推理）：reply 锚点判定为「陈旧已答」
    切到本产线时的跟进语境（勿重答旧问题/勿复读连发内容），置于指令最前；
    空串 = 输出与旧版逐字一致（既有断言零回归）。
    """
    _fu = str(followup_note or "").strip()
    _fu_head = (_fu + "\n") if _fu else ""
    lang_line = (
        f"必须完全用客户的语言（{reply_lang}）来写正文。"
        if reply_lang and reply_lang != "zh" else "用中文来写正文。"
    )
    avoid = ""
    rec = [str(t or "").strip() for t in (recent_out or []) if str(t or "").strip()]
    if rec:
        joined = "\n".join(f"- {t[:60]}" for t in rec[-3:])
        avoid = (
            "你最近已经发过下面这些消息，新话题的内容、开头和句式都不要与之雷同：\n"
            + joined + "\n"
        )
    _gi = str(goal_intent or "").strip()
    _gp = str(goal_push or "").strip().lower()
    if _gi and _gp in ("soft", "direct"):
        _gt = str(goal_title or "").strip()
        head = (
            f"这个会话有进行中的工作目标「{_gt}」，" if _gt
            else "这个会话有进行中的工作目标，"
        )
        manner = (
            "可以比较直接地把话题引到这个方向，但绝不硬销、不连环追问"
            if _gp == "direct"
            else "顺着日常话题自然带向这个方向，绝不生硬转折"
        )
        return (
            _fu_head
            + "请你主动开启一个新话题，给对方发一条 1-2 句的消息（不是回答对方上一条，"
            "而是自然地把对话推进到新的方向）。\n"
            + head + f"本条新话题优先服务它——今日推进方向：{_gi}。\n"
            + manner + f"；实在接不上时，退回这个备选切入点：{angle}。\n"
            "如果历史或记忆里有对方提过的、与该方向相关的具体事，优先从那件事切入。\n"
            "绝不要用「好久没联系」「最近怎么样」「在吗」这类模板问候；"
            "最多问一个问题，不要连环发问。\n"
            + avoid + lang_line + "只输出这条消息的正文。"
        )
    return (
        _fu_head
        + "请你主动开启一个新话题，给对方发一条 1-2 句的消息（不是回答对方上一条，"
        "而是自然地把对话推进到新的方向）。\n"
        f"今天的切入点参考：{angle}。\n"
        "如果历史或记忆里有对方提过的具体事（爱好/计划/工作/家人/上次聊到的事），"
        "优先自然地跟进那件事。\n"
        "绝不要用「好久没联系」「最近怎么样」「在吗」这类模板问候；"
        "最多问一个问题，不要连环发问。\n"
        + avoid + lang_line + "只输出这条消息的正文。"
    )


async def generate_topic_opener(
    *,
    app: Any,
    platform: str,
    chat_key: str,
    history: List[Dict[str, str]],
    persona_id: str = "",
    target_lang: str = "",
    conversation_id: str = "",
    account_id: str = "",
    gloss_lang: str = "",
    agent_instruction: str = "",
    followup_note: str = "",
) -> Dict[str, Any]:
    """生成「主动开启新话题」的开场消息（工坊 opener 模式）。

    与 ``generate_persona_reply`` 的关系：同一套人设解析 / 语言决策 / 译文护栏，
    但**不需要** last_inbound（没人说话也能主动开场），不走统一拟稿引擎、不写记忆。
    返回结构与回复链一致（{ok, reply, reply_lang, persona, persona_tier, intent,
    translated?}），前端零分叉。

    ``agent_instruction``（P22.1 / P28）：前端可在 opener 态带指令（「开新话题」+
    目标驱动拟稿）；注入 ``_agent_instruction`` 与目标块，不再依赖「强制切 reply」。

    ``followup_note``（P0 2026-08-12）：reply 锚点「陈旧已答」切换而来时的跟进
    语境（见 time_context.build_followup_note）；空=纯开场旧行为。
    """
    history = list(history or [])
    resolved_lang = (
        str(target_lang or "").strip()
        or resolve_reply_language(
            "", history,
            default=_initial_lang_default(app, platform, chat_key, conversation_id),
        )
    )

    _acct = str(account_id or "").strip()
    if (not _acct or _acct == "default") and conversation_id:
        _parts = str(conversation_id).split(":", 2)
        if len(_parts) >= 3 and _parts[1]:
            _acct = str(_parts[1]).strip()

    _eff_tier = ""
    if not str(persona_id or "").strip():
        try:
            from src.ai.persona_voice import resolve_effective_persona
            _cm = getattr(getattr(app, "state", None), "config_manager", None)
            _pid_r, _tier_r = resolve_effective_persona(
                getattr(_cm, "config", None) or {},
                platform, _acct, str(chat_key or ""),
            )
            if _pid_r:
                persona_id, _eff_tier = _pid_r, _tier_r
        except Exception:
            logger.debug("[persona_reply] opener persona 解析跳过", exc_info=True)

    # 切入点：与 proactive 子系统同一轮换词表（同用户同日恒定、隔天自动换）
    try:
        from src.utils.proactive_topic import checkin_angle
        angle = checkin_angle(f"{platform}:{chat_key}")
    except Exception:
        angle = "分享你此刻正在做的一件小事，或轻轻问一句TA这会儿在忙什么"

    recent_out = [
        str(m.get("content") or "") for m in history
        if isinstance(m, dict) and m.get("role") == "assistant"
    ][-3:]

    state = getattr(app, "state", None)
    sm = getattr(state, "skill_manager", None)
    if sm is None:
        tc = getattr(state, "telegram_client", None)
        sm = getattr(tc, "skill_manager", None) if tc is not None else None
    ai = getattr(state, "ai_client", None)

    # P25（2026-08-05）：ctx 提前到 directive 之前组装——工作目标注入要先行。
    # 「开新话题」此前是唯一完全不消费目标的生成链；现走 skill_manager 同一
    # 注入口（settle-on-read / hold / observe / 力度全语义与回复链同源），
    # `_goal_block` 进系统提示、今日意图喂进开场指令（none 力度只进块不带方向）。
    user_id = f"desktop:{platform}:{chat_key}" or "__desktop__"
    ctx: Dict[str, Any] = {
        "user_id": user_id,
        "chat_id": chat_key or user_id,
        "channel": "desktop",
        "platform": platform,
        "intent": "proactive_opener",
        "current_intent": "proactive_opener",
        "reply_lang": resolved_lang,
    }
    if persona_id:
        ctx["account_persona_id"] = persona_id
    if history:
        ctx["_conversation_history"] = history[-20:]
    _ainst = str(agent_instruction or "").strip()[:400]
    if _ainst:
        ctx["_agent_instruction"] = _ainst

    goal_meta: Dict[str, Any] = {}
    _opener_blocked = False
    try:
        from src.inbox.media_claim_block import is_blocked as _is_blk
        _opener_blocked = _is_blk(str(conversation_id or ""))
    except Exception:
        _opener_blocked = False
    if sm is not None and hasattr(sm, "_inject_goal_context") and not _opener_blocked:
        try:
            sm._inject_goal_context(
                ctx, platform=platform, chat_key=str(chat_key or ""),
                account_id=_acct, conversation_id=conversation_id,
                chain="opener",
            )
        except Exception:
            logger.debug("[persona_reply] opener 目标注入跳过", exc_info=True)
        _gm = ctx.get("_goal_inject_meta")
        if isinstance(_gm, dict):
            goal_meta = dict(_gm)

    directive = build_opener_directive(
        angle=angle, reply_lang=resolved_lang, recent_out=recent_out,
        goal_intent=str(goal_meta.get("intent") or ""),
        goal_push=str(goal_meta.get("push_level") or ""),
        goal_title=str(goal_meta.get("title") or ""),
        followup_note=followup_note,
    )
    # P-1 C（#259 · ZH3ZQ5③）：开场链同一段硬禁 + few-shot 追加进 directive（主路径 user_message
    # 与兜底 prompt 都吃它）；零对方历史 → 明说「首次接触，不回忆、不假装熟悉」——
    # 「that hiking trail you mentioned」在源头就少产生，后处理 claim_guard 兜底。
    try:
        from src.inbox.draft_style_hint import build_style_hint, history_has_peer_turns
        _cm_sh_o = getattr(getattr(app, "state", None), "config_manager", None)
        _sh_o = build_style_hint(
            resolved_lang, has_history=history_has_peer_turns(history, min_turns=1),
            config=getattr(_cm_sh_o, "config", None) or None)
        if _sh_o:
            directive = f"{directive}\n\n{_sh_o}"
    except Exception:
        logger.debug("[persona_reply] opener style hint 跳过", exc_info=True)

    # 当前时刻锚点（P0 2026-08-12）：开场/跟进同样要有时段感（晚上不发
    # 「早安」体开场）。经 _topic_switch_hint 既有消费口注入；配置同一闸门。
    try:
        from src.inbox.time_context import (
            build_now_anchor_hint,
            resolve_time_context_cfg,
        )
        _cm_tc = getattr(getattr(app, "state", None), "config_manager", None)
        if resolve_time_context_cfg(
                getattr(_cm_tc, "config", None) or {}).get("enabled"):
            # 与回复链同款：opener 锚点也按人设当地钟（双时钟事故修复）。
            _anchor_now_o, _anchor_place_o = _persona_anchor_clock(persona_id)
            ctx["_topic_switch_hint"] = build_now_anchor_hint(
                local_now=_anchor_now_o, place_label=_anchor_place_o)
    except Exception:
        logger.debug("[persona_reply] opener 时间锚点跳过", exc_info=True)

    reply = None
    if sm is not None and getattr(sm, "ai_client", None) is not None:
        try:
            # 情景记忆注入：开场跟进「对方提过的具体事」正需要长期事实
            if chat_key and hasattr(sm, "_inject_episodic_into_context"):
                try:
                    sm._inject_episodic_into_context(
                        ctx, str(chat_key), "",
                        current_user_text="", platform=platform,
                    )
                except Exception:
                    logger.debug("[persona_reply] opener 记忆注入跳过", exc_info=True)
            reply = await sm.ai_client.generate_reply_with_intent(
                user_message=directive,
                intent="proactive_opener",
                user_context=ctx,
            )
            # 与拟稿链步骤 9 同口径的出站商业事实守卫：目标开场可能带出目录
            # 链接/价格措辞（catalog 模板已把 _goal_cta 暂存 ctx，读后即焚），
            # opener 此前没有任何商业守卫——接入目标的同时必须配平
            if reply and hasattr(sm, "_apply_goal_link_guard"):
                try:
                    reply = sm._apply_goal_link_guard(
                        reply, ctx, log_prefix="[opener] ")
                except Exception:
                    logger.debug(
                        "[persona_reply] opener 目标守卫跳过", exc_info=True)
        except Exception:
            logger.debug("[persona_reply] opener 主路径失败，回落通用", exc_info=True)
            reply = None

    if not reply and ai is not None:
        lines = []
        for m in history[-12:]:
            who = "客户：" if m.get("role") == "user" else "我："
            lines.append(who + str(m.get("content") or ""))
        prompt = (
            "你是温暖、自然、像真人一样的线上陪伴。"
            + directive
            + ("\n\n最近的对话（供参考）：\n" + "\n".join(lines) if lines else "")
        )
        try:
            reply = await ai.chat(prompt)
        except Exception:
            logger.debug("[persona_reply] opener 兜底失败", exc_info=True)
            reply = None

    reply = (reply or "").strip()
    used_persona, persona_tier = "", ""
    if reply:
        if persona_id and _eff_tier:
            used_persona, persona_tier = persona_id, _eff_tier
        else:
            used_persona, persona_tier = _resolve_persona_badge(
                chat_key, persona_id, persona_id or "domain")

    out: Dict[str, Any] = {
        "ok": bool(reply),
        "reply": reply,
        "reply_lang": resolved_lang,
        "persona": used_persona,
        "persona_tier": persona_tier,
        "intent": "proactive_opener",
        "kb_refs": [],
        "mode": "opener",
        # P25 观测：目标有没有进本条开场、为什么（no_goal/hold/observe/...）
        # ——「设了目标为什么生成的话题不相关」从黑箱变成 API 可读字段
        "goal_applied": goal_meta or {"injected": False, "reason": "no_engine"},
    }
    if not reply:
        out["detail"] = "开场生成失败"
    translated = await _translate_reply(app, reply, target_lang)
    if translated:
        out["translated"] = translated
    await _attach_gloss(app, out, reply, resolved_lang, gloss_lang)
    return out
