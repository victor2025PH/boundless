"""companion 主动话题启动(Stage4,从 main.py 整方法原样迁出,仅 self->assistant)。

maybe_start_companion_proactive(assistant): P2 主动开场——冷启+冷却→P1选题→ai生成→
worker/A线客户端发送;桌面无协议号则挂到 proactive_care。enabled=false 仍挂预览能力。
默认扫描**编排器能发的全平台私聊**（可配 platforms 白名单收窄）。

Phase13(2026-07-13)：主动消息**语音化**——主动打招呼按概率发克隆声语音条
（``companion.proactive_topic.voice``），复用 autosend 语音全套（stage_voice_file
=预渲染命中/混合保真情感/副语言/人设灰度名单），失败回落文本，绝不丢触达。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Dict


def voice_gate(
    voice_cfg: Dict[str, Any], text: str, rand01: float,
) -> bool:
    """主动消息是否发语音（纯函数）：开关 + 长度带 + 概率。

    - ``enabled`` 关 → False；
    - 长度不在 [min_chars, max_chars]（默认 4..80）→ False（太短没内容、
      太长念着累，都不适合语音开场）；
    - ``rand01 < probability``（默认 0.5）→ True——全语音太机械，文本/语音
      混发更像真人（有时打字有时懒得打字直接说）。
    """
    v = voice_cfg or {}
    if not v.get("enabled", False):
        return False
    t = str(text or "").strip()
    try:
        min_chars = int(v.get("min_chars", 4) or 4)
        max_chars = int(v.get("max_chars", 80) or 80)
        prob = float(v.get("probability", 0.5))
    except (TypeError, ValueError):
        return False
    if not (min_chars <= len(t) <= max_chars):
        return False
    return float(rand01) < max(0.0, min(1.0, prob))


# 主动生活照默认只在这两类开场里发：回访/问候顺手带一张"我现在的样子"最自然；
# 画像采集(ask_*)与付费预告(story_teaser)带自拍显得刻意，仪式问候已有固定节奏。
_PHOTO_DEFAULT_MODES = ("gentle_checkin", "follow_up")


def photo_share_gate(
    photo_cfg: Dict[str, Any], *, mode: str, intimacy: float, rand01: float,
) -> bool:
    """主动消息是否附生活照（纯函数，Phase16）。

    - ``enabled`` 关（默认关）→ False；
    - ``mode`` 不在 ``modes``（默认 gentle_checkin/follow_up）→ False；
    - ``intimacy < min_intimacy``（默认 20）→ False——生人阶段发自拍既轻浮又像营销号；
    - ``rand01 < probability``（默认 0.25）→ True——偶尔一张才有惊喜感，每条都带就假了。
    """
    p = photo_cfg or {}
    if not p.get("enabled", False):
        return False
    allow = p.get("modes")
    allow_set = {str(x).strip() for x in allow} if isinstance(
        allow, (list, tuple)) and allow else set(_PHOTO_DEFAULT_MODES)
    if str(mode or "").strip() not in allow_set:
        return False
    try:
        min_intim = float(p.get("min_intimacy", 20))
        prob = float(p.get("probability", 0.25))
    except (TypeError, ValueError):
        return False
    try:
        _intim = float(intimacy)
    except (TypeError, ValueError):
        _intim = 0.0
    if _intim < min_intim:
        return False
    return float(rand01) < max(0.0, min(1.0, prob))


async def maybe_start_companion_proactive(assistant) -> None:
    """P2：陪伴主动话题调度（默认关，companion.proactive_topic.enabled 开）。

    沉默检测 + 冷却 → P1 选题（build_proactive_opener，只回访高置信记忆）→
    ai 生成一句自然开场 → 经编排器受管 worker / 主 A 线客户端发出（自动镜像收件箱）。
    编排器能发的私聊平台（默认全平台；companion.proactive_topic.platforms 可收窄）。
    与 proactive_care(messenger 约定驱动) 互补、不重叠。
    """
    try:
        comp = (assistant.config.config.get("companion") or {})
        cfg = (comp.get("proactive_topic") or {})
        enabled = bool(cfg.get("enabled", False))
        # 预览（可观测面板）仅需 inbox + skill_manager；ai 仅"真发"时才需要。
        # 故即便未启用 / ai 未就绪，也先挂上"会发给谁、引用哪条记忆"的预览能力，
        # 让运营在真正开闸前先 dry-run 看清本轮候选。
        if assistant.inbox_store is None or assistant.skill_manager is None:
            assistant.logger.info(
                "companion proactive_topic 跳过（inbox_store/skill_manager 未就绪，预览亦不可用）")
            return
        from src.integrations.companion_proactive import (
            CompanionProactiveLoop, JsonCooldownStore, plan_proactive_sends,
        )

        scan_limit = int(cfg.get("scan_limit", 200))
        min_silent_hours = float(cfg.get("min_silent_hours", 24))
        from src.utils.proactive_pacing import parse_adaptive_pacing_cfg
        _pacing_cfg = parse_adaptive_pacing_cfg(cfg)

        # Stage T：主动画像采集——把最 bland 的 gentle_checkin 开场，在「关系够深 +
        # 该槽位未知 + 距上次问够久」时升级成"顺势自然问一句"，让缺失画像补得起来。
        # 生日(birthday, Stage R)、称呼(name, Stage T) 共用一套通用框架，按优先级择一问。
        _collect_specs = []

        def _add_collect_spec(slot, cfg_key, resolver, default_min):
            c = (cfg.get(cfg_key) or {})
            if not bool(c.get("enabled", False)):
                return
            _collect_specs.append({
                "slot": slot,
                "min_intim": float(c.get("min_intimacy", default_min)),
                "cooldown_days": float(c.get("cooldown_days", 30)),
                "resolve": resolver,
                "cd": JsonCooldownStore(
                    Path(assistant.config.config_path).parent
                    / f"companion_{slot}_ask_cooldown.json"),
            })

        _add_collect_spec(
            "birthday", "birthday_ask",
            assistant.skill_manager.resolve_birthday, 45)
        _add_collect_spec(
            "name", "name_ask",
            assistant.skill_manager.resolve_preferred_name, 35)
        min_silent_hours_base = min_silent_hours
        # mode(ask_<slot>) → 冷却 store，供发出后记冷却。
        _collect_cd_by_mode = {
            f"ask_{s['slot']}": s["cd"] for s in _collect_specs}

        # Telegram 官方服务号（通知/验证码）——绝不主动"想你了"
        _SERVICE_CHAT_KEYS = {"777000", "42777", "1087968824"}

        # worker session 不认识的 peer（发送报 PEER_ID_INVALID）——进程内黑名单。
        # 不过滤则每 tick 重试同一批坏 peer（失败不记冷却→按沉默降序永远排前），
        # 正常候选永远轮不上；反复对无效 peer 打 API 也是风控信号。
        _bad_peers: set = set()

        def _account_can_send(platform: str, account_id: str) -> bool:
            """该账号有真实发送通道才让其会话进候选。

            编排器受管协议号（orch.owns）→ worker 发送 ✓；default 账号 → 主 A 线
            客户端回落 ✓；其余（如 tg-desktop 桌面工作台镜像）没有出站通道——
            若不过滤，_send 会错用**主账号**向别人的会话发消息（张冠李戴事故）。
            """
            try:
                from src.integrations.account_orchestrator import get_orchestrator
                if get_orchestrator(assistant.config.config or {}).owns(
                        platform, account_id):
                    return True
            except Exception:
                pass
            return (account_id == "default"
                    and assistant.telegram_client is not None)

        def _conversations():
            # 扫描平台：默认全平台（编排器能发的私聊都纳入主动触达）；
            # 可配 companion.proactive_topic.platforms 白名单收窄。
            _plat_filter = cfg.get("platforms")
            if isinstance(_plat_filter, str):
                _plat_filter = [_plat_filter]
            _plat_allow = (
                {str(p).lower() for p in (_plat_filter or []) if str(p).strip()}
                if _plat_filter else None
            )
            try:
                rows = assistant.inbox_store.list_conversations(
                    limit=scan_limit) or []
            except Exception:
                return []
            # ⚠ 主动开场只面向「能发的账号 × 私聊」（2026-07-13 真机预览实锤：
            # 不过滤则候选被沉默几个月的群聊/桌面镜像会话占满——给群发
            # "好久没联系啦" / 用错账号发送，都是灾难）。群聊天然不适合
            # 一对一情感问候；ritual/milestone/沉默回访共用本快照同享此护栏。
            _filtered = []
            for r in rows:
                _ct = str(r.get("chat_type") or "").strip().lower()
                if _ct and _ct not in ("private", "user", "bot"):
                    continue
                _ck = str(r.get("chat_key") or "")
                _pf = str(r.get("platform") or "telegram")
                if _plat_allow is not None and _pf.lower() not in _plat_allow:
                    continue
                if str(r.get("conversation_id") or "") in _bad_peers:
                    continue  # session 解析不了的 peer（PEER_ID_INVALID 拉黑）
                if _pf == "telegram":
                    if _ck in _SERVICE_CHAT_KEYS:
                        continue
                    try:
                        if int(_ck) < 0:  # 负 ID=群/频道（chat_type 缺失时兜底）
                            continue
                    except (TypeError, ValueError):
                        pass
                if not _account_can_send(_pf, str(r.get("account_id") or "default")):
                    continue
                _filtered.append(r)
            rows = _filtered
            cids = [str(r.get("conversation_id") or "")
                    for r in rows if r.get("conversation_id")]
            try:
                dirs = assistant.inbox_store.last_message_dirs(cids)
            except Exception:
                dirs = {}
            try:
                tags_map = assistant.inbox_store.list_conv_tags_map(cids)
            except Exception:
                tags_map = {}
            # Phase ④续⁹：把 inbox 末条情绪并入快照——让情绪护栏的 soft 档覆盖「非危机
            # 但明显低谷」（最近一条被分析为愤怒/不满/焦虑）→ 抑制剧情邀约、留温和问候。
            try:
                meta_intel = assistant.inbox_store.get_conv_meta_for_ids(cids)
            except Exception:
                meta_intel = {}
            # Phase ④续⁵：把真实 intimacy/funnel 注入快照——既让记忆开场的沉默阈值
            # 缩放更准，也让「主动剧情邀约」能按真实关系等级判断可邀约剧情。
            # 复用 N 线已就绪的进程级 provider（resolve_*）；未注册 → 返回 None → 退回 0/""。
            try:
                from src.utils.companion_context import (
                    resolve_funnel_stage as _resolve_funnel_stage,
                    resolve_intimacy_score as _resolve_intimacy_score,
                )
            except Exception:
                _resolve_intimacy_score = None
                _resolve_funnel_stage = None
            out = []
            for r in rows:
                cid = str(r.get("conversation_id") or "")
                chat_key = str(r.get("chat_key") or "")
                platform = str(r.get("platform") or "telegram")
                account_id = str(r.get("account_id") or "default")
                meta = tags_map.get(cid, {}) or {}
                _intim = 0.0
                _stage = ""
                if _resolve_intimacy_score is not None and chat_key:
                    try:
                        _v = _resolve_intimacy_score(
                            account_id, chat_key, channel=platform)
                        _intim = float(_v) if _v is not None else 0.0
                        _stage = _resolve_funnel_stage(
                            account_id, chat_key, channel=platform) or ""
                    except Exception:
                        _intim, _stage = 0.0, ""
                out.append({
                    "conversation_id": cid,
                    "platform": platform,
                    "account_id": account_id,
                    "chat_key": chat_key,
                    "last_ts": r.get("last_ts") or 0,
                    # 会话首次建立时间 ≈ 首次接触 → 供「认识 N 天」纪念日计算（Stage P）
                    "first_seen_ts": r.get("created_at") or 0,
                    "last_direction": (dirs.get(cid) or {}).get("direction") or "",
                    "archived": bool(meta.get("archived")),
                    # 私聊：episodic 记忆 key == 对端 id == chat_key
                    "memory_key": chat_key,
                    "stage": _stage,
                    "intimacy": _intim,
                    "last_emotion": str(
                        (meta_intel.get(cid) or {}).get("last_emotion") or ""),
                })
            return out

        def _opener(*, memory_key, silent_hours, stage, intimacy,
                    last_emotion="", last_emotion_intensity=-1.0, contact_key="",
                    min_silent_hours=None):
            # ⚠ 签名必须兼容 plan_proactive_sends 的调用（含 last_emotion_intensity、
            # min_silent_hours 逐会话自适应，Phase14）——曾因缺参导致 TypeError 被逐会话吞掉、
            # 候选恒为 0（真机 preview candidates=0 实锤，2026-07-13 修）。
            _msh = float(
                min_silent_hours if min_silent_hours is not None
                else min_silent_hours_base)
            op = assistant.skill_manager.build_proactive_opener(
                memory_key, silent_hours=silent_hours, stage=stage,
                intimacy=intimacy, min_silent_hours=_msh,
                last_emotion=last_emotion,
                last_emotion_intensity=last_emotion_intensity,
                contact_key=contact_key)
            # Stage T：bland gentle_checkin → 顺势采集某缺失画像（关系深 + 槽位未知 + 未在冷却）。
            # 按优先级择一问（一次开场只问一个，不连环逼问）；便宜条件(冷却/亲密)先过滤再查
            # 记忆（resolve 是 IO），控成本。生日 capture 见 Stage S；称呼 capture 由 heuristic 落库。
            if _collect_specs and str((op or {}).get("mode") or "") == "gentle_checkin":
                try:
                    import time as _t
                    from src.utils.profile_collect import should_ask_profile_slot
                    cid = str(contact_key or memory_key or "")
                    _now = _t.time()
                    for spec in _collect_specs:
                        last_ask = float(
                            (spec["cd"].snapshot().get(cid)) or 0)
                        if float(intimacy) < spec["min_intim"]:
                            continue
                        if (_now - last_ask) < spec["cooldown_days"] * 86400.0:
                            continue
                        if spec["resolve"](memory_key) is not None:
                            continue  # 该槽位已知 → 不问
                        if not should_ask_profile_slot(
                                opener_mode="gentle_checkin", intimacy=intimacy,
                                min_intimacy=spec["min_intim"], slot_known=False,
                                last_ask_ts=last_ask, now=_now,
                                cooldown_days=spec["cooldown_days"]):
                            continue
                        ask = assistant.skill_manager.build_profile_ask_opener(
                            spec["slot"], memory_key=memory_key, stage=stage,
                            intimacy=intimacy, last_emotion=last_emotion,
                            contact_key=contact_key)
                        if ask.get("mode"):
                            ask["silent_hours"] = (op or {}).get(
                                "silent_hours", 0.0)
                            return ask
                except Exception:
                    assistant.logger.debug("[proactive] 画像采集升级跳过", exc_info=True)
            return op

        cd_path = Path(assistant.config.config_path).parent / "companion_proactive_cooldown.json"

        # 与 proactive_care(Phase O) 去重：已排关怀的会话让路（best-effort）。
        # 仅在 care 子系统已就绪（store 已挂 web_app.state）时生效，否则不去重、无害。
        care_store = None
        try:
            care_store = getattr(
                getattr(assistant._web_app, "state", None), "care_schedule_store", None)
        except Exception:
            care_store = None

        def _has_pending_care(conversation_id: str) -> bool:
            if care_store is None:
                return False
            try:
                return int(care_store.count_pending_by_contact(conversation_id)) > 0
            except Exception:
                return False

        # Phase ④续⁸：危机关怀升级——severe 近期危机的沉默用户被情绪护栏拦下时，
        # 不只静默，而是排一条高优先 care 待办（人工/关怀兜底），把"静默"变"接住"。
        # 幂等：排进后 has_pending_care→True，下个 tick 该会话整段让路、不会重排。
        _crisis_escalation_on = bool(cfg.get("crisis_care_escalation", True))

        def _on_crisis_block(conv) -> None:
            if care_store is None or not _crisis_escalation_on:
                return
            cid = str((conv or {}).get("conversation_id") or "")
            if not cid:
                return
            try:
                import time as _time
                from src.contacts.care_commitment import CareCommitment
                from src.contacts.care_schedule import CRISIS_CARE_TOPIC
                _now = _time.time()
                care_store.add_commitment(
                    CareCommitment(
                        due_at=_now,            # 立即到期 → 下个派发 tick 即可被关怀/坐席接住
                        event_at=_now,
                        topic=CRISIS_CARE_TOPIC,  # 派发器据此切「克制陪伴」语气模板
                        sentiment="negative",
                        anchor_text="",
                        source_text="近期危机信号，主动护栏拦下打扰，转关怀回访",
                        confidence=1.0,
                    ),
                    contact_key=cid,
                    platform=str((conv or {}).get("platform") or ""),
                    account_id=str((conv or {}).get("account_id") or ""),
                    chat_key=str((conv or {}).get("chat_key") or ""),
                )
            except Exception:
                assistant.logger.debug("[proactive] 危机关怀升级排队失败 cid=%s", cid, exc_info=True)

        # 采样评分回流存储（质量闭环）：试发采样落库，供 👍/👎 评分 + 调参看板。
        sample_store = None
        try:
            from src.integrations.companion_sample_store import (
                get_companion_sample_store,
            )
            _sdb = Path(assistant.config.config_path).parent / "companion_samples.db"
            sample_store = get_companion_sample_store(_sdb)
            assistant._web_app.state.companion_sample_store = sample_store
        except Exception:
            sample_store = None
            assistant.logger.debug("[proactive] 采样评分存储初始化失败", exc_info=True)

        # few-shot 风格示范注入（默认关，人审样本后开）：把人工高赞/改写样本作口吻示范
        # 拼进生成 prompt（只学风格不照抄内容），让评分数据反哺生成——自我改进环。
        _fs_cfg = (cfg.get("few_shot") or {})
        _fs_enabled = bool(_fs_cfg.get("enabled", False))
        _fs_max = int(_fs_cfg.get("max_examples", 3))

        _pp_params = dict(
            min_silent_hours=min_silent_hours,
            cooldown_hours=float(cfg.get("cooldown_hours", 72)),
            quiet_start_hour=float(cfg.get("quiet_start_hour", 23)),
            quiet_end_hour=float(cfg.get("quiet_end_hour", 8)),
        )
        _real_max_per_tick = int(cfg.get("max_per_tick", 3))

        def _proactive_preview(limit=50):
            """可观测预览（dry-run）：本轮"会主动联系谁、引用哪条记忆、带哪些背景"。
            不发送、不写冷却；即便功能未启用也可调用（开闸前先看清候选）。"""
            lim = max(1, min(int(limit or 50), 200))
            try:
                convs = _conversations()
            except Exception:
                convs = []
            try:
                cooldown_map = JsonCooldownStore(cd_path).snapshot()
            except Exception:
                cooldown_map = {}
            # 预览展示全部候选（最多 lim 条），不受 max_per_tick 截断；
            # 另标出本 tick 实际会发的前 N 条（按沉默时长降序）。
            plans = plan_proactive_sends(
                convs, cooldown_map=cooldown_map, opener_fn=_opener,
                has_pending_care=_has_pending_care, max_per_tick=lim, **_pp_params,
                pacing_cfg=_pacing_cfg)
            for i, p in enumerate(plans):
                p["would_send_this_tick"] = i < _real_max_per_tick
            return {
                "enabled": enabled,
                "dry_run": bool(cfg.get("dry_run", False)),
                "scanned": len(convs),
                "candidates": len(plans),
                "max_per_tick": _real_max_per_tick,
                "min_silent_hours": min_silent_hours,
                "cooldown_hours": _pp_params["cooldown_hours"],
                "adaptive_pacing": _pacing_cfg,
                "quiet_hours": [_pp_params["quiet_start_hour"], _pp_params["quiet_end_hour"]],
                "care_dedup_active": care_store is not None,
                "plans": plans,
            }

        ai_name = "她"
        try:
            ai_name = str((assistant.config.get_ai_config() or {}).get("ai_name") or "她")
        except Exception:
            ai_name = "她"

        def _peer_language(plan) -> str:
            """会话语言（inbox ingest 持续标注的 conversations.language）。"""
            try:
                conv = assistant.inbox_store.get_conversation(
                    str(plan.get("conversation_id") or "")) or {}
                return str(conv.get("language") or "").strip().lower()
            except Exception:
                return ""

        async def _gen_text(plan, scene_note: str = ""):
            """按 plan 生成"要发出去的那一句"（directive + 背景记忆 + 最近上下文）。
            只生成、不发送；ai 未就绪或空回复 → 返回 ""。真发 _send 与试发预览共用。

            Phase13 修：带 ``peer_language`` 进 prompt——给说英语的客户必须发英语
            开场（此前给全程英文会话发了中文开场+中文语音，一眼机器人）。
            Phase17：``scene_note`` 非空 = 本条会附生活照，文案须自然带到该场景
            （文案-场景对齐，图文一体）。"""
            ctx_lines = []
            try:
                msgs = assistant.inbox_store.list_recent_messages(
                    plan["conversation_id"], limit=6) or []
                ctx_lines = [str(m.get("text") or "").strip()
                             for m in msgs if str(m.get("text") or "").strip()]
            except Exception:
                ctx_lines = []
            ctx = "\n".join(ctx_lines[-6:])[:600]
            # few-shot 风格示范（默认关）：人工认可样本作口吻示范，反哺生成。
            # 按当前 plan 的 mode 分桶取示范（follow_up/gentle_checkin/ritual_* 各用各的口吻）。
            fs_block = ""
            if _fs_enabled and sample_store is not None:
                try:
                    from src.integrations.companion_sample_store import (
                        build_few_shot_block,
                    )
                    rows = (sample_store.list_recent(limit=50, rating="down")
                            + sample_store.list_recent(limit=50, rating="up"))
                    fs_block = build_few_shot_block(
                        rows, max_examples=_fs_max,
                        mode=str(plan.get("mode") or "")) or ""
                except Exception:
                    fs_block = ""
            # Stage O：prompt 组装抽成纯函数，按 mode 自适应框定（仪式问候不再套「久别重逢」）。
            from src.utils.proactive_prompt import build_proactive_prompt
            prompt = build_proactive_prompt(
                ai_name, plan, recent_context=ctx, few_shot_block=fs_block,
                peer_language=_peer_language(plan), scene_note=scene_note)
            try:
                text = await assistant.ai_client.chat(prompt)
            except Exception:
                return ""
            return (text or "").strip()

        async def _proactive_generate(conversation_id, slot=""):
            """试发采样：对某会话生成 AI 实际会说的那句话，但**不发送、不写冷却**。
            让运营开闸前先读到真实文案（会真实调用一次 AI，有 token 成本）。

            Stage O：``slot`` ∈ {morning,night} 时试发**每日仪式问候**（晨/晚安），
            走 build_ritual_opener；空则试发沉默回访开场（原行为）。两者采样同表，
            按 mode 分桶喂 few-shot（ritual_* 与 follow_up 各学各的口吻）。"""
            if assistant.ai_client is None:
                return {"generated": False, "reason": "ai_not_ready", "message": "AI 未就绪"}
            cid = str(conversation_id or "")
            if not cid:
                return {"generated": False, "reason": "missing",
                        "message": "缺 conversation_id"}
            try:
                conv = next((c for c in (_conversations() or [])
                             if str(c.get("conversation_id")) == cid), None)
            except Exception:
                conv = None
            if conv is None:
                return {"generated": False, "reason": "not_found",
                        "message": "会话不在当前扫描范围"}
            import time as _time
            try:
                last_ts = float(conv.get("last_ts") or 0)
            except (TypeError, ValueError):
                last_ts = 0.0
            silent_hours = (_time.time() - last_ts) / 3600.0 if last_ts > 0 else 0.0
            _slot = str(slot or "").strip().lower()
            try:
                if _slot in ("morning", "night"):
                    opener = assistant.skill_manager.build_ritual_opener(
                        _slot,
                        memory_key=str(conv.get("memory_key") or ""),
                        stage=str(conv.get("stage") or ""),
                        intimacy=float(conv.get("intimacy") or 0.0),
                        last_emotion=str(conv.get("last_emotion") or ""),
                        contact_key=cid) or {}
                    silent_hours = 0.0
                else:
                    opener = _opener(
                        memory_key=str(conv.get("memory_key") or ""),
                        silent_hours=silent_hours,
                        stage=str(conv.get("stage") or ""),
                        intimacy=float(conv.get("intimacy") or 0.0)) or {}
            except Exception:
                opener = {}
            if not opener.get("mode") or not opener.get("directive"):
                return {"generated": False, "reason": "not_eligible",
                        "message": ("该会话当前不构成仪式问候（危机抑制/关系太浅）"
                                    if _slot in ("morning", "night")
                                    else "该会话当前不构成主动开场（沉默不足/无可回访记忆）")}
            plan = {
                "conversation_id": cid,
                "directive": str(opener.get("directive") or ""),
                "context_facts": list(opener.get("context_facts") or []),
                "mode": str(opener.get("mode") or ""),
            }
            text = await _gen_text(plan)
            # 采样落库（质量闭环）：供运营 👍/👎 评分回流；失败不影响返回文案。
            sample_id = None
            if sample_store is not None and text:
                try:
                    sample_id = sample_store.record_sample(
                        conversation_id=cid,
                        account_id=str(conv.get("account_id") or ""),
                        mode=str(opener.get("mode") or ""),
                        fact=str(opener.get("fact") or ""),
                        context_facts_n=len(opener.get("context_facts") or []),
                        silent_hours=silent_hours, text=text)
                except Exception:
                    sample_id = None
            return {
                "generated": bool(text),
                "text": text,
                "sample_id": sample_id,
                "mode": str(opener.get("mode") or ""),
                "fact": str(opener.get("fact") or ""),
                "context_facts": [str(f) for f in (opener.get("context_facts") or [])],
                "silent_hours": round(silent_hours, 1),
            }

        try:
            assistant._web_app.state.companion_proactive_preview = _proactive_preview
            assistant._web_app.state.companion_proactive_generate = _proactive_generate
        except Exception:
            assistant.logger.debug("[proactive] 预览/试发回调挂载失败", exc_info=True)

        if not enabled:
            assistant.logger.info(
                "companion proactive_topic 未启用"
                "（预览可用：GET /api/companion/proactive/preview）")
            return
        if assistant.ai_client is None:
            assistant.logger.info(
                "companion proactive_topic 已启用但 ai 未就绪，调度不启动（预览仍可用）")
            return

        async def _run_on_web_loop(coro_factory):
            """把编排器协程调度到 web loop 执行（worker 的 pyrogram client 活在
            web 线程 loop 上；直接 await 会跨 loop 报 "attached to a different
            loop"——与 autosend_helpers 的 marshalling 同口径）。"""
            _wl = getattr(assistant, "_web_loop", None)
            if _wl is not None and _wl.is_running():
                fut = asyncio.run_coroutine_threadsafe(coro_factory(), _wl)
                return await asyncio.wrap_future(fut)
            return await coro_factory()

        async def _try_send_voice(plan, text) -> bool:
            """主动开场语音分支：中文→克隆声(7852/预渲染)；外语→edge 多语(Phase15)。
            失败 False=回落文本。
            """
            import random as _rnd

            v_cfg = cfg.get("voice") if isinstance(cfg.get("voice"), dict) else {}
            if not voice_gate(v_cfg, text, _rnd.random()):
                return False
            _plang = _peer_language(plan)
            platform = plan["platform"]
            account_id = plan["account_id"]
            chat_key = plan["chat_key"]
            _cfg_root = assistant.config.config or {}

            async def _deliver_staged(staged) -> bool:
                if not staged:
                    return False
                local, url = staged[0], staged[1]
                try:
                    from src.integrations.account_orchestrator import get_orchestrator
                    orch = get_orchestrator(_cfg_root)
                    if not orch.owns(platform, account_id):
                        return False
                    res = await _run_on_web_loop(lambda: orch.send_media(
                        platform, account_id, chat_key,
                        media_path=local, media_url=url, media_type="voice",
                        caption="", inbox_text=text))
                    return bool((res or {}).get("delivered"))
                except Exception:
                    return False

            # 外语：edge 多语神经声（不占克隆 GPU；文案已是客户语言）
            from src.companion.proactive_voice_foreign import (
                foreign_voice_allowed,
                is_chinese_peer_language,
                resolve_foreign_voice_cfg,
                stage_foreign_voice_file,
            )
            if _plang and not is_chinese_peer_language(_plang):
                try:
                    _fb = resolve_foreign_voice_cfg(_cfg_root)
                    if foreign_voice_allowed(_fb, _plang):
                        staged = await stage_foreign_voice_file(
                            _cfg_root, platform, account_id, text,
                            peer_language=_plang)
                        if await _deliver_staged(staged):
                            assistant.logger.info(
                                "[proactive] 外语语音开场已发 %s:%s chat=%s lang=%s mode=%s",
                                platform, account_id, chat_key, _plang,
                                plan.get("mode"))
                            try:
                                from src.companion.proactive_stats import record_voice
                                record_voice(foreign=True)
                            except Exception:
                                pass
                            return True
                except Exception:
                    assistant.logger.info(
                        "[proactive] 外语语音开场失败，回落文本", exc_info=True)
                return False

            # 中文：克隆声全套（预渲染 / 7852 混合保真）
            try:
                from src.integrations.account_orchestrator import get_orchestrator
                orch = get_orchestrator(_cfg_root)
                if not orch.owns(platform, account_id):
                    return False
                from src.ai.persona_voice import resolve_account_persona_id
                from src.inbox.voice_autosend import (
                    persona_allowed_for_voice,
                    stage_voice_file,
                )
                pid = resolve_account_persona_id(_cfg_root, platform, account_id)
                _l2_voice = (((_cfg_root.get("inbox") or {})
                             .get("l2_autosend") or {}).get("voice") or {})
                if not persona_allowed_for_voice(_l2_voice, pid):
                    return False
                staged = await stage_voice_file(
                    _cfg_root, platform, account_id,
                    pid, text, contact_key=chat_key)
                if await _deliver_staged(staged):
                    assistant.logger.info(
                        "[proactive] 语音开场已发 %s:%s chat=%s mode=%s",
                        platform, account_id, chat_key, plan.get("mode"))
                    try:
                        from src.companion.proactive_stats import record_voice
                        record_voice(foreign=False)
                    except Exception:
                        pass
                    return True
            except Exception:
                assistant.logger.info("[proactive] 语音开场失败，回落文本", exc_info=True)
            return False

        # 主动生活照每日预算（进程级，防 GPU 被主动触达吃满 + 反自拍刷屏）。
        _photo_cap = None
        try:
            _p_cfg0 = cfg.get("photo") if isinstance(cfg.get("photo"), dict) else {}
            if _p_cfg0.get("enabled", False):
                from src.integrations.rpa_base.daily_cap import DailyCapTracker
                _photo_cap = DailyCapTracker(
                    daily_cap=int(_p_cfg0.get("daily_cap", 6) or 6))
        except Exception:
            _photo_cap = None

        async def _plan_photo(plan):
            """生活照预决策（Phase17：文案-场景对齐）——在**生成文案之前**决定
            本条是否配图、配什么场景。命中返回 ``(persona_id, scene)``，否则 None。
            这样场景能注入文案 prompt（"你正在便利店夜班…"），图文强关联。

            Phase18「场景反选」：话题贴合优先——用一次极小 LLM 调用从场景池挑与
            directive 最贴合的场景（回访"备考"→书桌/图书馆而非夜市）；LLM 答 0/失败
            → 回落原时段轮换。``photo.scene_by_topic=false`` 可关。"""
            import random as _rnd

            p_cfg = cfg.get("photo") if isinstance(cfg.get("photo"), dict) else {}
            if not photo_share_gate(
                    p_cfg, mode=str(plan.get("mode") or ""),
                    intimacy=float(plan.get("intimacy") or 0.0),
                    rand01=_rnd.random()):
                return None
            if _photo_cap is not None and _photo_cap.would_exceed(1):
                return None
            _cfg_root = assistant.config.config or {}
            try:
                from src.inbox.image_autosend import resolve_image_autosend_cfg
                scfg = resolve_image_autosend_cfg(_cfg_root)
                if not scfg.get("enabled", False):
                    return None  # 依赖发图能力总开关（companion.selfie.enabled）
                from src.integrations.account_orchestrator import get_orchestrator
                if not get_orchestrator(_cfg_root).owns_media(
                        plan["platform"], plan["account_id"]):
                    return None
                from src.ai.persona_voice import resolve_account_persona_id
                pid = resolve_account_persona_id(
                    _cfg_root, plan["platform"], plan["account_id"])
                if not pid:
                    return None
                from src.ai.companion_selfie import pick_scene_hint, scene_pool
                from src.utils.persona_manager import PersonaManager
                persona = PersonaManager.get_instance().get_persona_by_id(pid) or {}
                scene = ""
                # 场景反选（话题贴合）：仅回访/问候类有实际话题时有意义。
                if bool(p_cfg.get("scene_by_topic", True)) and assistant.ai_client:
                    pool = scene_pool(persona, scfg.get("scene_rotation"))
                    directive = str(plan.get("directive") or "").strip()
                    if pool and directive:
                        try:
                            from src.ai.companion_selfie import (
                                build_scene_choice_instruction,
                                parse_scene_choice,
                            )
                            raw = await assistant.ai_client.chat(
                                build_scene_choice_instruction(
                                    directive, plan.get("context_facts"), pool))
                            idx = parse_scene_choice(raw, len(pool))
                            if idx >= 1:
                                scene = pool[idx - 1]
                                assistant.logger.info(
                                    "[proactive] 场景反选命中 idx=%d scene=%r",
                                    idx, scene)
                        except Exception:
                            assistant.logger.debug(
                                "[proactive] 场景反选失败，回落轮换", exc_info=True)
                if not scene:
                    scene = pick_scene_hint(
                        persona,
                        default_scene=str(scfg.get("scene_hint") or ""),
                        fallback_scenes=scfg.get("scene_rotation"))
                return (pid, scene)
            except Exception:
                assistant.logger.debug("[proactive] 生活照预决策异常", exc_info=True)
                return None

        async def _try_send_photo(plan, text, pid, scene) -> bool:
            """主动开场生活照发送（Phase16/17）：按 ``_plan_photo`` 选定的场景出图
            （PuLID 锁脸 + vision_gate 体检），与场景对齐的文案作配文图文一体发出。
            失败 False=回落语音/文本（文案已带场景叙事，纯文本发出同样成立）。"""
            platform = plan["platform"]
            account_id = plan["account_id"]
            chat_key = plan["chat_key"]
            _cfg_root = assistant.config.config or {}
            try:
                from src.inbox.image_autosend import stage_image_file
                staged = await stage_image_file(
                    _cfg_root, platform, account_id, pid,
                    {"kind": "selfie", "scene": scene})
                if not staged:
                    return False
                local, url, _kind = staged
                from src.integrations.account_orchestrator import get_orchestrator
                orch = get_orchestrator(_cfg_root)
                res = await _run_on_web_loop(lambda: orch.send_media(
                    platform, account_id, chat_key,
                    media_path=local, media_url=url, media_type="image",
                    caption=text, inbox_text=("[图片] " + text).strip()))
                if bool((res or {}).get("delivered")):
                    if _photo_cap is not None:
                        _photo_cap.record_sent(1)
                    assistant.logger.info(
                        "[proactive] 生活照开场已发 %s:%s chat=%s mode=%s scene=%r",
                        platform, account_id, chat_key, plan.get("mode"), scene)
                    try:
                        from src.companion.proactive_stats import record_photo
                        record_photo()
                    except Exception:
                        pass
                    return True
            except Exception:
                assistant.logger.info(
                    "[proactive] 生活照开场失败，回落语音/文本", exc_info=True)
            return False

        def _log_outreach(plan, kind: str) -> None:
            """主动触达落 outreach_log（batch_id=proactive_topic，note=派发形态）。
            复用 P61 回执统计：photo/voice/text 的回复率可经 outreach_response_stats
            分批次回看（A/B 评估数据源）。best-effort 绝不抛。"""
            try:
                st = assistant.inbox_store
                if st is not None and hasattr(st, "record_outreach"):
                    st.record_outreach(
                        str(plan.get("conversation_id") or ""),
                        batch_id=f"proactive_topic:{kind}",
                        platform=str(plan.get("platform") or ""),
                        account_id=str(plan.get("account_id") or ""),
                        status="sent",
                        note=str(plan.get("mode") or ""))
            except Exception:
                assistant.logger.debug("[proactive] outreach 落库失败", exc_info=True)

        async def _send(plan):
            # 0) 生活照预决策（Phase17/18）：先定"要不要配图 + 什么场景"，场景注入文案
            _photo_plan = await _plan_photo(plan)
            # 1) 生成开场文案（directive + 背景记忆 + 最近上下文 ± 场景叙事）
            text = await _gen_text(
                plan, scene_note=_photo_plan[1] if _photo_plan else "")
            if not text:
                return False
            platform = plan["platform"]
            account_id = plan["account_id"]
            chat_key = plan["chat_key"]
            # 1.3) 生活照分支（Phase16/17）：场景自拍 + 对齐文案作配文，图文一体
            if _photo_plan and await _try_send_photo(
                    plan, text, _photo_plan[0], _photo_plan[1]):
                _log_outreach(plan, "photo")
                return True
            # 1.5) 语音开场分支（Phase13）：按概率发克隆声语音条；失败回落文本
            if await _try_send_voice(plan, text):
                _log_outreach(plan, "voice")
                return True
            # 2) 优先编排器受管 worker（自动回写收件箱出站镜像）。
            # ⚠ 必须经 web loop 调度：worker 的 pyrogram client 绑定 web 线程
            # loop，从主 loop 直接 await → "attached to a different loop"
            # （2026-07-13 真机 planned=3 sent=0 根因之一）。
            try:
                from src.integrations.account_orchestrator import get_orchestrator
                orch = get_orchestrator(assistant.config.config or {})
                if orch.owns(platform, account_id):
                    res = await _run_on_web_loop(lambda: orch.send(
                        platform, account_id, chat_key, text))
                    _ok = bool((res or {}).get("delivered", True))
                    if _ok:
                        _log_outreach(plan, "text")
                    return _ok
            except Exception as e:
                # PEER_ID_INVALID = session 不认识对方（无 access_hash，多为
                # 对方已注销/换号或迁移遗留会话）→ 拉黑不再重试，且升 info 可见
                # （debug 级曾把"每 tick 全军覆没"藏了一上午）。
                _es = (type(e).__name__ + " " + str(e)).upper().replace(" ", "_")
                # 永久性发送错误 → 拉黑：peer 解析不了 / 对方已注销 / 被对方拉黑
                _permanent = any(k in _es for k in (
                    "PEERIDINVALID", "PEER_ID_INVALID",
                    "INPUT_USER_DEACTIVATED", "USERDEACTIVATED",
                    "USER_IS_BLOCKED", "USERISBLOCKED",
                    "YOU_BLOCKED_USER", "CHAT_WRITE_FORBIDDEN"))
                if _permanent:
                    _bad_peers.add(plan["conversation_id"])
                    assistant.logger.info(
                        "[proactive] peer 不可达已拉黑 %s（%s）",
                        plan["conversation_id"], e)
                else:
                    assistant.logger.info(
                        "[proactive] 编排器发送失败 %s: %s",
                        plan["conversation_id"], e)
            # 3) 回落：主 A 线客户端——**仅限 default 账号的会话**。
            # ⚠ 编排器账号的会话绝不回落主客户端：A 线 session 不认识对方 peer
            # （pyrogram 本地缓存无此 peer）→ PEER_ID_INVALID → ban_signal 误判
            # 风控信号 → kill-switch 冻结主账号 1 小时（2026-07-13 真机事故）。
            # worker 未就绪时宁可本 tick 不发（不记冷却，下轮自然重试）。
            if (platform == "telegram" and account_id == "default"
                    and assistant.telegram_client is not None):
                try:
                    target = int(chat_key)
                except (TypeError, ValueError):
                    target = chat_key
                try:
                    ok = await assistant.telegram_client.send_message(target, text)
                    if ok:
                        _log_outreach(plan, "text")
                    return bool(ok)
                except Exception:
                    assistant.logger.debug("[proactive] 主客户端发送失败", exc_info=True)
                    return False
            return False

        def _on_teaser_sent(plan) -> None:
            _mode = str((plan or {}).get("mode") or "")
            # Stage T：画像采集发出 → 记对应槽位冷却（cooldown_days 内不再问同一人，避免反复打听）。
            _collect_cd = _collect_cd_by_mode.get(_mode)
            if _collect_cd is not None:
                try:
                    import time as _t
                    _cid = str((plan or {}).get("conversation_id") or "")
                    if _cid:
                        _collect_cd.mark(_cid, _t.time())
                except Exception:
                    assistant.logger.debug("[proactive] 画像采集冷却落盘失败", exc_info=True)
            # Stage 3：付费预告（story_teaser）发出即记一条漏斗事件，供归因转化率。
            if _mode != "story_teaser":
                return
            funnel = assistant._companion_funnel_store
            if funnel is None:
                return
            try:
                funnel.record_teaser(
                    str(plan.get("conversation_id") or ""),
                    str(plan.get("scenario_id") or ""),
                    str(plan.get("feature") or ""))
            except Exception:
                assistant.logger.debug("[proactive] 预告漏斗埋点失败", exc_info=True)

        # Stage L：每日仪式感主动问候（晨安 / 晚安，按用户活跃时段择时）。默认关。
        # 与沉默回访共用同一发送回路 / 情绪护栏 / care 去重；独立每日每档冷却。
        _ritual_fn = None
        _ritual_cd = None
        _r_cfg = (cfg.get("daily_ritual") or {})
        if bool(_r_cfg.get("enabled", False)):
            from src.utils.daily_ritual import plan_daily_rituals as _plan_rituals
            _ritual_cd = JsonCooldownStore(
                Path(assistant.config.config_path).parent
                / "companion_ritual_cooldown.json")

            def _ritual_opener(*, slot, memory_key, stage, intimacy,
                               last_emotion="", last_emotion_intensity=-1.0,
                               contact_key=""):
                return assistant.skill_manager.build_ritual_opener(
                    slot, memory_key=memory_key, stage=stage, intimacy=intimacy,
                    last_emotion=last_emotion,
                    last_emotion_intensity=last_emotion_intensity,
                    contact_key=contact_key)

            _personalize = bool(_r_cfg.get("personalize_active_hour", True))

            def _active_hours(cid):
                # 该用户历史**入站**消息的本地小时样本，推断习惯晨 / 晚点（仅候选才查）。
                try:
                    msgs = assistant.inbox_store.list_recent_messages(
                        cid, limit=80) or []
                except Exception:
                    return []
                hrs = []
                for m in msgs:
                    if str(m.get("direction") or "") != "in":
                        continue
                    try:
                        ts = float(m.get("ts") or 0)
                    except (TypeError, ValueError):
                        ts = 0.0
                    if ts > 0:
                        hrs.append(time.localtime(ts).tm_hour)
                return hrs

            _r_morning = tuple(_r_cfg.get("morning_window", [7, 10]))
            _r_night = tuple(_r_cfg.get("night_window", [21, 24]))
            _r_min_intim = float(_r_cfg.get("min_intimacy", 20))
            _r_gap = float(_r_cfg.get("min_quiet_gap_hours", 3))
            _r_max = int(_r_cfg.get("max_per_tick", 5))

            # Stage P：纪念日·节日仪式（认识 N 天 / 节日）——事件驱动、复用 ritual_key
            # 同一冷却表去重；节点优先于每日晨/晚安（同会话同 tick 不重复打扰）。默认关。
            _m_cfg = (cfg.get("milestone_ritual") or {})
            _m_enabled = bool(_m_cfg.get("enabled", False))
            _plan_milestones = None
            _milestone_opener = None
            if _m_enabled:
                from src.utils.milestone_ritual import (
                    DEFAULT_ANNIVERSARY_DAYS as _M_DEF_ANNIV,
                    plan_milestone_rituals as _plan_milestones,
                )

                def _milestone_opener(*, event_type, event_label="", days=0,
                                      memory_key="", stage="", intimacy=0.0,
                                      last_emotion="", last_emotion_intensity=-1.0,
                                      contact_key=""):
                    return assistant.skill_manager.build_milestone_opener(
                        event_type=event_type, event_label=event_label, days=days,
                        memory_key=memory_key, stage=stage, intimacy=intimacy,
                        last_emotion=last_emotion,
                        last_emotion_intensity=last_emotion_intensity,
                        contact_key=contact_key)

                _m_greet_hour = int(_m_cfg.get("greet_hour", 10))
                _m_min_intim = float(_m_cfg.get("min_intimacy", 30))
                _m_max = int(_m_cfg.get("max_per_tick", 5))
                _m_anniv = _m_cfg.get("anniversary_days") or list(_M_DEF_ANNIV)
                _m_holidays = _m_cfg.get("holiday_calendar") or None
                # Stage Q：生日仪式——从记忆扫出 (月,日)，当天庆生（最高优先级节点）。
                _m_bday_on = bool(_m_cfg.get("celebrate_birthday", True))

                def _birthday_provider(memory_key):
                    return assistant.skill_manager.resolve_birthday(memory_key)

            def _ritual_fn(convs, now_ts):
                daily = _plan_rituals(
                    convs,
                    ritual_sent=(_ritual_cd.snapshot() if _ritual_cd else {}),
                    opener_fn=_ritual_opener,
                    now=now_ts,
                    morning_window=_r_morning,
                    night_window=_r_night,
                    min_intimacy=_r_min_intim,
                    min_quiet_gap_hours=_r_gap,
                    max_per_tick=_r_max,
                    has_pending_care=_has_pending_care,
                    active_hours_provider=_active_hours if _personalize else None,
                ) or []
                if _plan_milestones is None:
                    return daily
                try:
                    mil = _plan_milestones(
                        convs,
                        ritual_sent=(_ritual_cd.snapshot() if _ritual_cd else {}),
                        opener_fn=_milestone_opener,
                        now=now_ts,
                        greet_hour=_m_greet_hour,
                        min_intimacy=_m_min_intim,
                        max_per_tick=_m_max,
                        anniversary_milestones=_m_anniv,
                        holiday_calendar=_m_holidays,
                        has_pending_care=_has_pending_care,
                        birthday_provider=(
                            _birthday_provider if _m_bday_on else None),
                    ) or []
                except Exception:
                    assistant.logger.debug("[milestone] 规划失败", exc_info=True)
                    mil = []
                if not mil:
                    return daily
                # 节点优先：同会话本 tick 既有节点又到晨/晚安档，只发节点（更高情感价值）
                mil_ids = {p.get("conversation_id") for p in mil}
                return mil + [
                    p for p in daily if p.get("conversation_id") not in mil_ids]

        def _fresh_last_ts(conversation_id: str) -> float:
            """发送前复核用：该会话此刻最新 last_ts（权威=收件箱）。取不到 → 0（不拦）。"""
            try:
                conv = assistant.inbox_store.get_conversation(
                    str(conversation_id or "")) or {}
                return float(conv.get("last_ts") or 0.0)
            except Exception:
                return 0.0

        loop = CompanionProactiveLoop(
            conversations_provider=_conversations,
            opener_fn=_opener,
            send_fn=_send,
            fresh_activity_provider=_fresh_last_ts,
            cooldown_store=JsonCooldownStore(cd_path),
            interval_sec=float(cfg.get("interval_sec", 900)),
            # 首 tick 前等 worker 拉起（编排器监督循环 15s 起步 + 连接耗时）
            first_delay_sec=float(cfg.get("first_delay_sec", 90)),
            min_silent_hours=min_silent_hours,
            cooldown_hours=float(cfg.get("cooldown_hours", 72)),
            max_per_tick=int(cfg.get("max_per_tick", 3)),
            quiet_start_hour=float(cfg.get("quiet_start_hour", 23)),
            quiet_end_hour=float(cfg.get("quiet_end_hour", 8)),
            dry_run=bool(cfg.get("dry_run", False)),
            has_pending_care=_has_pending_care,
            on_crisis_block=_on_crisis_block,
            on_sent=_on_teaser_sent,
            ritual_fn=_ritual_fn,
            ritual_cooldown=_ritual_cd,
            pacing_cfg=_pacing_cfg,
        )
        await loop.start()
        assistant._companion_proactive_loop = loop
        assistant.logger.info(
            "✅ companion proactive_topic 调度已启动"
            "（interval=%ss min_silent=%sh cooldown=%sh dry_run=%s）",
            cfg.get("interval_sec", 900), min_silent_hours,
            cfg.get("cooldown_hours", 72), cfg.get("dry_run", False))
    except Exception as ex:
        assistant.logger.warning("companion proactive_topic 启动跳过: %s", ex)
        assistant.logger.debug("companion proactive_topic 启动异常", exc_info=True)
