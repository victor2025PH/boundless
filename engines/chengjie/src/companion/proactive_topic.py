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
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


def voice_gate_verdict(
    voice_cfg: Dict[str, Any], text: str, rand01: float,
) -> tuple:
    """主动消息语音闸带原因判定（P1 透传归因）：``(ok, reason)``。

    reason ∈ disabled / bad_cfg / length / probability / ok——生产实锤配置 50%
    实际透传 6%，没有原因计数就永远不知道卡在哪一层。
    """
    v = voice_cfg or {}
    if not v.get("enabled", False):
        return (False, "disabled")
    t = str(text or "").strip()
    try:
        min_chars = int(v.get("min_chars", 4) or 4)
        max_chars = int(v.get("max_chars", 80) or 80)
        prob = float(v.get("probability", 0.5))
    except (TypeError, ValueError):
        return (False, "bad_cfg")
    if not (min_chars <= len(t) <= max_chars):
        return (False, "length")
    if float(rand01) < max(0.0, min(1.0, prob)):
        return (True, "ok")
    return (False, "probability")


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
    return bool(voice_gate_verdict(voice_cfg, text, rand01)[0])


# 主动生活照默认只在这两类开场里发：回访/问候顺手带一张"我现在的样子"最自然；
# 画像采集(ask_*)与付费预告(story_teaser)带自拍显得刻意，仪式问候已有固定节奏。
_PHOTO_DEFAULT_MODES = ("gentle_checkin", "follow_up")


def is_translation_account(platform: str, account_id: str) -> bool:
    """账号是否翻译业务线（融合实例 P1）。

    翻译线账号服务的是翻译客服场景客户，绝不主动"想你了"式陪伴触达——
    与账号级 autodraft 封顶（translation → review）同源读注册表
    ``business_line`` 标签。查询失败 / 未标注 → False（不过滤 = 旧行为）。
    """
    try:
        from src.integrations.account_registry import cached_business_line
        return cached_business_line(platform, account_id) == "translation"
    except Exception:
        return False


def photo_share_verdict(
    photo_cfg: Dict[str, Any], *, mode: str, intimacy: float, rand01: float,
) -> tuple:
    """主动生活照闸带原因判定（P1 透传归因）：``(ok, reason)``。

    reason ∈ disabled / mode / bad_cfg / min_intimacy / probability / ok。
    生产实锤：配置 25% 实际 0 张——候选全是 intimacy≈0 的生客，被 min_intimacy
    正确拦下（这是设计行为不是 bug），但没有计数就没法把「0 张」解释清楚。
    """
    p = photo_cfg or {}
    if not p.get("enabled", False):
        return (False, "disabled")
    allow = p.get("modes")
    allow_set = {str(x).strip() for x in allow} if isinstance(
        allow, (list, tuple)) and allow else set(_PHOTO_DEFAULT_MODES)
    if str(mode or "").strip() not in allow_set:
        return (False, "mode")
    try:
        min_intim = float(p.get("min_intimacy", 20))
        prob = float(p.get("probability", 0.25))
    except (TypeError, ValueError):
        return (False, "bad_cfg")
    try:
        _intim = float(intimacy)
    except (TypeError, ValueError):
        _intim = 0.0
    if _intim < min_intim:
        return (False, "min_intimacy")
    if float(rand01) < max(0.0, min(1.0, prob)):
        return (True, "ok")
    return (False, "probability")


def photo_share_gate(
    photo_cfg: Dict[str, Any], *, mode: str, intimacy: float, rand01: float,
) -> bool:
    """主动消息是否附生活照（纯函数，Phase16）。

    - ``enabled`` 关（默认关）→ False；
    - ``mode`` 不在 ``modes``（默认 gentle_checkin/follow_up）→ False；
    - ``intimacy < min_intimacy``（默认 20）→ False——生人阶段发自拍既轻浮又像营销号；
    - ``rand01 < probability``（默认 0.25）→ True——偶尔一张才有惊喜感，每条都带就假了。
    """
    return bool(photo_share_verdict(
        photo_cfg, mode=mode, intimacy=intimacy, rand01=rand01)[0])


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
        # 融合实例 P1：授权档位闸门（gate 默认关 = 恒放行零变化）。
        # 档位不含 companion → 主动触达调度整体不启（预览面板亦无意义）。
        try:
            from src.licensing.feature_gate import feature_enabled as _feat_on
            if enabled and not _feat_on(
                    "companion", assistant.config.config or {}):
                assistant.logger.info(
                    "companion proactive_topic 跳过：授权档位未含 companion（feature gate）")
                enabled = False
        except Exception:
            pass
        # 预览（可观测面板）仅需 inbox + skill_manager；ai 仅"真发"时才需要。
        # 故即便未启用 / ai 未就绪，也先挂上"会发给谁、引用哪条记忆"的预览能力，
        # 让运营在真正开闸前先 dry-run 看清本轮候选。
        if assistant.inbox_store is None or assistant.skill_manager is None:
            assistant.logger.info(
                "companion proactive_topic 跳过（inbox_store/skill_manager 未就绪，预览亦不可用）")
            return
        from src.integrations.companion_proactive import (
            CompanionProactiveLoop,
            JsonCooldownStore,
            JsonProactiveLedger,
            plan_proactive_sends,
        )

        scan_limit = int(cfg.get("scan_limit", 200))
        min_silent_hours = float(cfg.get("min_silent_hours", 24))
        from src.utils.proactive_pacing import (
            parse_adaptive_pacing_cfg,
            parse_no_reply_backoff_cfg,
            parse_response_pacing_cfg,
        )
        _pacing_cfg = parse_adaptive_pacing_cfg(cfg)
        # P0 未回退避（默认开）：上一条主动没得到回应 → 冷却×3^streak 封顶月频。
        # 修实锤事故：近 14 天 45% 的主动发送是「上一条没回又发下一条」。
        _backoff_cfg = parse_no_reply_backoff_cfg(cfg)
        if not _backoff_cfg.get("enabled"):
            _backoff_cfg = None
        # P2 回复率反哺（默认开，relax 默认 1.0=只减不增）：账本长期回复率缩放冷却
        _response_cfg = parse_response_pacing_cfg(cfg)
        if not _response_cfg.get("enabled"):
            _response_cfg = None

        def _live_pacing_cfg():
            """每次调用现读 pacing（P1-198）：ConfigManager 热重载后，主动触达的
            节奏参数（min_silent/cooldown/max_per_tick/安静时段/自适应/退避/反哺/
            dry_run）立即跟进，不必重启——198 实锤：演示档 24h→4h 改完热重载对
            循环无效，因为旧实现把参数固化进循环实例属性。预览与真发循环共用本
            闭包（同口径，预览不骗人）。读不到/异常 → 空 dict＝各消费方用启动值。"""
            try:
                _c = getattr(assistant.config, "config", None) or {}
                _pt = ((_c.get("companion") or {}).get("proactive_topic") or {})
                if not isinstance(_pt, dict):
                    return {}
                _bo = parse_no_reply_backoff_cfg(_pt)
                _rp = parse_response_pacing_cfg(_pt)
                return {
                    "min_silent_hours": float(_pt.get("min_silent_hours", 24)),
                    "cooldown_hours": float(_pt.get("cooldown_hours", 72)),
                    "max_per_tick": int(_pt.get("max_per_tick", 3)),
                    "quiet_start_hour": float(_pt.get("quiet_start_hour", 23)),
                    "quiet_end_hour": float(_pt.get("quiet_end_hour", 8)),
                    "dry_run": bool(_pt.get("dry_run", False)),
                    "pacing_cfg": parse_adaptive_pacing_cfg(_pt),
                    "backoff_cfg": _bo if _bo.get("enabled") else None,
                    "response_pacing_cfg": _rp if _rp.get("enabled") else None,
                }
            except Exception:
                return {}
        # P3 媒体形态反哺（默认关，overlay 开）：voice/photo 概率按分形态回复率
        # 相对 text 基线自校准（只换形态不换总量；min_intimacy 等护栏全不动）。
        from src.companion.proactive_media_feedback import (
            collect_media_feedback,
            effective_media_probability,
            parse_media_feedback_cfg,
        )
        _mfb_cfg = parse_media_feedback_cfg(cfg)
        _mfb_on = bool(_mfb_cfg.get("enabled"))
        _mfb_cache: Dict[str, Any] = {"ts": 0.0, "data": {}}

        def _media_fb_snapshot() -> Dict[str, Any]:
            """三臂回复率+倍率快照（30min TTL；查询异常回空=全走配置值）。"""
            if not _mfb_on:
                return {}
            _now_fb = time.time()
            if _now_fb - float(_mfb_cache["ts"] or 0.0) > 1800.0:
                try:
                    _mfb_cache["data"] = collect_media_feedback(
                        assistant.inbox_store, _mfb_cfg, now=_now_fb) or {}
                except Exception:
                    _mfb_cache["data"] = {}
                _mfb_cache["ts"] = _now_fb
            return _mfb_cache["data"]

        def _fb_probability(kind: str, base_prob: float) -> float:
            """某形态的有效发送概率 = 配置值 × 反哺倍率（未启用/无数据=配置值）。"""
            snap = _media_fb_snapshot()
            arm = snap.get(kind) if isinstance(snap, dict) else None
            if not isinstance(arm, dict):
                return float(base_prob or 0.0)
            return effective_media_probability(
                base_prob, float(arm.get("factor") or 1.0))

        # P5 checkin 效能门控（默认关，overlay 开）：数据证明 checkin 回复率
        # 远低于富开场时，按比例跳过「什么钩子都没有」的兜底问候——没话找话
        # 不如今天不说。样本不足恒 0（机制先上线，数据到位自动激活）。
        from src.companion.proactive_mode_gate import (
            collect_mode_gate,
            parse_mode_gate_cfg,
            should_skip_checkin,
        )
        _mg_cfg = parse_mode_gate_cfg(cfg)
        _mg_on = bool(_mg_cfg.get("enabled"))
        _mg_cache: Dict[str, Any] = {"ts": 0.0, "data": {}}

        def _mode_gate_snapshot() -> Dict[str, Any]:
            """两臂回复率+跳过概率快照（30min TTL；异常回空=不跳过）。"""
            if not _mg_on:
                return {}
            _now_mg = time.time()
            if _now_mg - float(_mg_cache["ts"] or 0.0) > 1800.0:
                try:
                    _mg_cache["data"] = collect_mode_gate(
                        assistant.inbox_store, _mg_cfg, now=_now_mg) or {}
                except Exception:
                    _mg_cache["data"] = {}
                _mg_cache["ts"] = _now_mg
            return _mg_cache["data"]

        # P0 变体守卫（默认开）：新开场与「上次主动/末尾未回连发」文案雷同 →
        # 换写一次，仍雷同则本轮放弃（宁可不发也不复读「好久没联系」x3）。
        _v_cfg = cfg.get("variety") if isinstance(cfg.get("variety"), dict) else {}
        _variety_on = bool(_v_cfg.get("enabled", True))
        from src.utils.proactive_variety import DEFAULT_SIMILARITY_THRESHOLD
        try:
            _sim_threshold = float(_v_cfg.get(
                "similarity_threshold", DEFAULT_SIMILARITY_THRESHOLD))
        except (TypeError, ValueError):
            _sim_threshold = DEFAULT_SIMILARITY_THRESHOLD

        # ── 用户时钟（companion.user_clock，默认关）────────────────────────────
        # 主动触达此前全锚定服务器本地钟（UTC+8），客户却遍布十几个时区 →「早安」发到
        # 对方半夜。enabled + schedule **都为真**才把调度基准换成用户钟；两个开关分开是
        # 因为时钟推断还有别的用途（prompt 注入「对方那边几点」），开那个不等于敢改调度。
        _uc_raw = (comp.get("user_clock") or {})
        _uc_enabled = (bool(_uc_raw.get("enabled", False))
                       and bool(_uc_raw.get("schedule", False)))
        _uc_cfg = dict(_uc_raw)
        try:
            _uc_cfg.update(
                min_samples=int(_uc_raw.get("min_samples", 24) or 24),
                min_margin=float(_uc_raw.get("min_margin", 0.35) or 0.35),
                ttl_sec=float(_uc_raw.get("ttl_sec", 900) or 900),
            )
        except (TypeError, ValueError):
            _uc_cfg = dict(_uc_raw)
        # 地区节日（companion.locale_holidays）：给用户侧节日问候用「对方那边过什么节」
        # 替代中文公历日历。与用户时钟开关独立——没有时区推断时它仍可按会话语种定国家，
        # 用服务器日期查该国节日，属纯增益。
        _lh_raw = (comp.get("locale_holidays") or {})
        _lh_enabled = (bool(_lh_raw.get("enabled", False))
                       and bool(_lh_raw.get("greet_user_side", False)))

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

        # Telegram 系统 peer（官方通知号 / 匿名代理 / 自己的收藏夹）——绝不主动"想你了"。
        # 判据走 store 的单一事实源：此前这里是一份独立硬编码，多带 42777/1087968824
        # 但**漏了 Saved Messages**，等于「给账号自己的云笔记发想你了」只差一条占位会话。
        from src.inbox.store import is_system_peer as _is_system_peer

        # worker session 不认识的 peer（发送报 PEER_ID_INVALID）——黑名单。
        # 不过滤则每 tick 重试同一批坏 peer（失败不记冷却→按沉默降序永远排前），
        # 正常候选永远轮不上；反复对无效 peer 打 API 也是风控信号。
        # 落盘持久化（2026-07-27 实锤：开发期频繁重启，进程内集每次清零 →
        # 同一批已注销 peer 每次重启后重烧 2 次失败才回黑名单）；对方若复活，
        # 运营删 companion_bad_peers.json 即可恢复。
        _bad_peers: set = set()
        _bad_peers_path = (
            Path(assistant.config.config_path).parent / "companion_bad_peers.json")
        try:
            if _bad_peers_path.exists():
                _bad_peers.update(
                    str(x)
                    for x in (json.loads(_bad_peers_path.read_text("utf-8")) or [])
                    if str(x))
        except Exception:
            assistant.logger.debug("[proactive] bad_peers 装载失败", exc_info=True)

        def _persist_bad_peers() -> None:
            try:
                _bad_peers_path.parent.mkdir(parents=True, exist_ok=True)
                _bad_peers_path.write_text(
                    json.dumps(sorted(_bad_peers), ensure_ascii=False), "utf-8")
            except Exception:
                assistant.logger.debug("[proactive] bad_peers 落盘失败", exc_info=True)

        # ── 死 peer 共享登记表收敛（2026-07-29）────────────────────────────
        # 收敛前：A 线 sender 与本模块各维护一份黑名单 → 一条链拉黑的死号另一条链
        # 还在打（实录：已注销用户被每 15min 重试、2h ×17 次 INPUT_USER_DEACTIVATED，
        # 无效重发累积风控信号）。现共用 src/ops/dead_peer_registry：
        #   · 分类走同一纯函数 classify_send_error（单一事实源，杜绝两处词表漂移）；
        #   · 黑名单双写（本地 json 保留＝可回退不丢历史 + registry 跨链共享）、
        #     双读（任一命中即跳过）；
        #   · reason 级 TTL 自动生效（注销恒永久 / 被拉黑可配 TTL 到期探路重试）。
        # gated on ops.dead_peer_registry.enabled（默认关 → 纯本地旧行为，零破坏）。
        # 惰性缓存**只存成功值**：曾把 None 也缓存 → flag 从关到开（config 热重载）
        # 后永不重读（2026-07-29 灰度实测：重启到配置热重载之间有数分钟窗口，
        # 本函数在窗口内被调用一次即把 None 钉死，灰度开关看似生效实则无效）。
        # flag 关时的重读成本＝几层 dict 取值，可忽略。
        _dp_reg_cache: list = []      # [registry]，仅缓存成功解析

        def _dp_registry():
            if _dp_reg_cache:
                return _dp_reg_cache[0]
            reg = None
            try:
                from src.ops.dead_peer_registry import (
                    dead_peer_enabled, get_dead_peer_registry,
                )
                if dead_peer_enabled(assistant.config):
                    _dpc = ((assistant.config.config.get("ops") or {}).get(
                        "dead_peer_registry") or {})
                    _tbr = _dpc.get("ttl_by_reason")
                    reg = get_dead_peer_registry(
                        path=str(_bad_peers_path.parent / "dead_peers.json"),
                        ttl_sec=float(_dpc.get("ttl_sec") or 0),
                        ttl_by_reason=_tbr if isinstance(_tbr, dict) else None,
                        # 历史黑名单一次性迁入共享表（新表为空时）
                        legacy_paths=[str(_bad_peers_path)])
            except Exception:
                assistant.logger.debug("[proactive] 死 peer 登记表不可用", exc_info=True)
                reg = None
            if reg is not None:
                _dp_reg_cache.append(reg)
            return reg

        def _is_bad_peer(cid: str, platform: str = "telegram") -> bool:
            """本地集 or 共享登记表任一命中 → 视为死 peer（跨链共享的读侧）。"""
            if not cid:
                return False
            if cid in _bad_peers:
                return True
            reg = _dp_registry()
            try:
                return bool(reg is not None and reg.is_blocked(platform, cid))
            except Exception:
                return False

        def _mark_bad_peer(cid: str, *, platform: str = "telegram",
                           reason: str = "blocked") -> None:
            """拉黑（写侧双写）。``reason`` 须是 registry 的永久类，否则它按非永久忽略。"""
            if not cid:
                return
            _bad_peers.add(cid)
            _persist_bad_peers()
            reg = _dp_registry()
            if reg is not None:
                try:
                    reg.record(platform, cid, reason)
                except Exception:
                    assistant.logger.debug("[proactive] 死 peer 登记失败", exc_info=True)

        # P1 opt-out 静默注册表（2026-07-29）：用户明说「别再发了」→ 静默 mute_days
        # 天（默认 30），只拦主动触达不拦正常回复；对方 opt-out 后又主动开口 → 自动
        # 解除（optout_active 判定）。落盘防重启丢失；过期条目装载时懒清理。
        _oo_cfg = cfg.get("optout") if isinstance(cfg.get("optout"), dict) else {}
        _optout_on = bool(_oo_cfg.get("enabled", True))
        try:
            _optout_days = float(_oo_cfg.get("mute_days", 30) or 30)
        except (TypeError, ValueError):
            _optout_days = 30.0
        _optout_mutes: Dict[str, Dict[str, Any]] = {}
        _optout_path = (
            Path(assistant.config.config_path).parent / "companion_optout_mute.json")
        try:
            if _optout_path.exists():
                _raw_oo = json.loads(_optout_path.read_text("utf-8")) or {}
                _now0 = time.time()
                _optout_mutes.update({
                    str(k): dict(v) for k, v in _raw_oo.items()
                    if isinstance(v, dict)
                    and float(v.get("until") or 0) > _now0})
        except Exception:
            assistant.logger.debug("[proactive] optout 注册表装载失败", exc_info=True)

        def _persist_optout() -> None:
            try:
                _optout_path.parent.mkdir(parents=True, exist_ok=True)
                _optout_path.write_text(
                    json.dumps(_optout_mutes, ensure_ascii=False), "utf-8")
            except Exception:
                assistant.logger.debug("[proactive] optout 注册表落盘失败", exc_info=True)

        # 主客户端回落路径吞异常只回 False（拿不到错误类型）→ 按连败计数拉黑：
        # 连败 2 次进 _bad_peers（2026-07-27 实锤：INPUT_USER_DEACTIVATED 会话
        # 每 tick 烧掉一个名额；单败不拉防网络抖动误伤）。
        _send_fail_streak: dict = {}

        def _note_send_result(cid: str, ok: bool,
                              platform: str = "telegram") -> None:
            if ok:
                _send_fail_streak.pop(cid, None)
                return
            n = _send_fail_streak.get(cid, 0) + 1
            _send_fail_streak[cid] = n
            if n >= 2:
                # 连败拉黑是**推测性**的（这条路径拿不到错误类型）→ 记可解除的
                # blocked 而非不可逆的 deactivated，将来配了 reason TTL 能探路恢复。
                _mark_bad_peer(cid, platform=platform, reason="blocked")
                assistant.logger.info(
                    "[proactive] 发送连败 ×%d 已拉黑 %s（已落盘，重启不再重试）", n, cid)

        def _account_can_send(platform: str, account_id: str) -> bool:
            """该账号有真实发送通道才让其会话进候选。

            编排器受管协议号（orch.owns）→ worker 发送 ✓；default 账号 → 主 A 线
            客户端回落 ✓；其余（如 tg-desktop 桌面工作台镜像）没有出站通道——
            若不过滤，_send 会错用**主账号**向别人的会话发消息（张冠李戴事故）。
            融合实例 P1：翻译业务线账号一票否决（翻译客服客户收到"想你了"=事故）。
            """
            if is_translation_account(platform, account_id):
                return False
            try:
                from src.integrations.account_orchestrator import get_orchestrator
                if get_orchestrator(assistant.config.config or {}).owns(
                        platform, account_id):
                    return True
            except Exception:
                pass
            return (account_id == "default"
                    and assistant.telegram_client is not None)

        # 本 tick 会话快照索引 {cid: conv}——用户时钟/地区节日 provider 只拿到 cid，
        # 需要回查该会话的 memory_key / language 才能解析。每 tick 由 _conversations() 重建。
        _conv_index: Dict[str, Dict[str, Any]] = {}
        # 用户时钟接管观测：proactive_stats 没有对应入口（record_tick/voice/photo 三个），
        # 刻意不改那个模块 → 每 tick 一行 info 摘要，够回答「接管了多少个会话」。
        _uc_seen: Dict[str, int] = {"resolved": 0, "takeover": 0}

        def _uc_tick_summary() -> None:
            """把上一 tick 累计的用户时钟接管数记一行日志并清零（best-effort）。"""
            try:
                if _uc_seen["resolved"]:
                    from src.companion.user_clock import dump_stats as _uc_stats
                    assistant.logger.info(
                        "[proactive] 用户时钟：上轮解析出 %d 个会话的时钟，其中 %d 个"
                        "接管了调度（推断源累计 %s）",
                        _uc_seen["resolved"], _uc_seen["takeover"], _uc_stats())
            except Exception:
                pass
            _uc_seen["resolved"] = 0
            _uc_seen["takeover"] = 0

        def _conversations():
            # 扫描平台：默认全平台（编排器能发的私聊都纳入主动触达）；
            # 可配 companion.proactive_topic.platforms 白名单收窄。
            _uc_tick_summary()
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
                if _is_bad_peer(str(r.get("conversation_id") or ""), _pf):
                    continue  # 死 peer（本地集 or 共享登记表：含 A 线 sender 拉黑的）
                if _pf == "telegram":
                    if _is_system_peer(
                            _pf, str(r.get("account_id") or ""), _ck):
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
            # dirs 兼作「这条会话到底有没有消息」的判据（见下方占位会话护栏），故取不到时
            # 要能区分「真的没消息」与「查失败」——查失败一律不主动发（fail-closed）：
            # tick 每 15 分钟一轮，漏一轮零代价；而无法核实就外呼的代价是给陌生人群发。
            dirs_ok = True
            try:
                dirs = assistant.inbox_store.last_message_dirs(cids)
            except Exception:
                dirs = {}
                dirs_ok = False
                assistant.logger.warning(
                    "[proactive] 末条方向查询失败 → 本轮不主动发送（无法核实是否真交谈过）")
            # P0 未回退避判据：对方最后一次开口时间（查失败 → 全 0 = 按存量 streak
            # 保守退避，方向是「更少打扰」，安全）。
            try:
                last_in_map = assistant.inbox_store.last_inbound_ts_map(cids)
            except Exception:
                last_in_map = {}
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
            _sm_for_key = getattr(assistant, "skill_manager", None)
            for r in rows:
                cid = str(r.get("conversation_id") or "")
                # ⚠ 会话占位护栏（2026-07-26，目录同步上线后新增的风险面）：
                # 目录同步把「云端会话列表」建成会话占位（upsert_protocol_chats），让工作台
                # 贴近手机所见。这些占位带着**手机上的真实 last_ts**（可能沉默数月）却
                # 从未经本系统交谈过——库里一条消息都没有，故末条方向为空。
                # 不拦的话首轮同步后主动触达会把这批陌生会话按沉默降序当成「好久不见的老
                # 朋友」挨个问候：既是老板没授权的外呼，也重演 2026-07-13 那次
                # PEER_ID_INVALID → ban_signal → 冻结主账号 1h 的事故路径。
                # 判据取「末条方向为空」而非另查消息计数：真交谈过的会话必有 in/out 之一，
                # 且 dirs 本轮已查好（零额外查询）。
                if not dirs_ok or not (dirs.get(cid) or {}).get("direction"):
                    continue
                # 收件箱档位闸：仅全自动会话可主动触达。手动/人审会话被坐席接管后
                # 不应再「想你了」冷开场（与 A 线直发闸同一口径）。
                try:
                    from src.inbox.automation_mode import (
                        allows_direct_autosend,
                        resolve_automation_mode,
                    )
                    _amode = resolve_automation_mode(
                        assistant.inbox_store, cid,
                        getattr(assistant.config, "config", None) or {})
                    if not allows_direct_autosend(_amode):
                        continue
                except Exception:
                    pass
                chat_key = str(r.get("chat_key") or "")
                platform = str(r.get("platform") or "telegram")
                account_id = str(r.get("account_id") or "default")
                # P0-1（2026-07-25）：记忆键与写入侧**同源**——草稿/A 线落库用
                # `_episodic_storage_key`（含 account 分桶 + CPI canonical），此前这里
                # 传裸 chat_key 做前缀查询 → 双号(companion)账号的主动开场/生日/槽位
                # 采集全部读不到记忆（回访式开场退化成 generic）。软失败回落裸键。
                _mem_key = chat_key
                if _sm_for_key is not None and hasattr(
                        _sm_for_key, "_episodic_storage_key"):
                    try:
                        _mem_key = _sm_for_key._episodic_storage_key(
                            chat_key, "", platform, account_id=account_id,
                        ) or chat_key
                    except Exception:
                        _mem_key = chat_key
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
                _last_in_ts = float(last_in_map.get(cid) or 0.0)
                # P1 opt-out：静默期内不进候选（对方 opt-out 后又开口 → 自动解除）。
                # 放在快照层 = 沉默回访/仪式/节点问候共用同一道闸。
                if _optout_on and cid in _optout_mutes:
                    try:
                        from src.utils.proactive_optout import optout_active
                        if optout_active(
                                _optout_mutes.get(cid), now=time.time(),
                                last_in_ts=_last_in_ts):
                            continue
                    except Exception:
                        pass
                out.append({
                    "conversation_id": cid,
                    "platform": platform,
                    "account_id": account_id,
                    "chat_key": chat_key,
                    "last_ts": r.get("last_ts") or 0,
                    # 会话首次建立时间 ≈ 首次接触 → 供「认识 N 天」纪念日计算（Stage P）
                    "first_seen_ts": r.get("created_at") or 0,
                    "last_direction": (dirs.get(cid) or {}).get("direction") or "",
                    # 对方最后一次开口（未回退避判据；从未入站 = 0）
                    "last_in_ts": _last_in_ts,
                    "archived": bool(meta.get("archived")),
                    # 私聊：episodic 记忆 key 与写入侧同源（账号分桶 + CPI canonical）
                    "memory_key": _mem_key,
                    "stage": _stage,
                    "intimacy": _intim,
                    # 会话语言（ingest 持续标注）：用户时钟/地区节日在缺显式信号时按语种
                    # 兜底猜国家（跨洲通用语刻意不猜，见 user_clock/locale_holidays）。
                    "language": str(r.get("language") or "").strip().lower(),
                    "last_emotion": str(
                        (meta_intel.get(cid) or {}).get("last_emotion") or ""),
                })
            _conv_index.clear()
            _conv_index.update({str(c["conversation_id"]): c for c in out})
            return out

        def _resolve_clock(cid):
            """该会话的用户时钟（``UserClock|None``）。绝不抛：解析服务未上线 / 模块缺失 /
            任何异常 → None，规划器按服务器钟走＝本能力上线前的行为。"""
            try:
                from src.companion.user_clock_resolver import (
                    resolve_for_conversation,
                )
                conv = _conv_index.get(cid) or {}
                # episodic store 实际挂在 skill_manager 上（assistant 无此属性），
                # 两处都探一遍，免得自述城市信号白丢。
                _epi = getattr(assistant, "_episodic_store", None)
                if _epi is None:
                    _epi = getattr(
                        assistant.skill_manager, "_episodic_store", None)
                return resolve_for_conversation(
                    cid,
                    inbox_store=assistant.inbox_store,
                    episodic_store=_epi,
                    memory_key=str(conv.get("memory_key") or ""),
                    cfg=_uc_cfg,
                )
            except Exception:
                return None

        def _user_clock(cid):
            """规划器用的时钟 provider＝裸解析 + 接管观测计数（解析不出时不计数，
            这样解析服务没上线就一行日志都不刷）。"""
            clock = _resolve_clock(cid)
            try:
                if clock is not None:
                    _uc_seen["resolved"] += 1
                    if getattr(clock, "trust", "") in ("replace", "narrow"):
                        _uc_seen["takeover"] += 1
            except Exception:
                pass
            return clock

        def _locale_holiday(cid):
            """该会话所在地区今天适合问候的节日 ``(key, 名称)``；无 → None。绝不抛。

            国家优先取时钟推断结果（自述城市/号码国码最硬），退而按会话语种猜；
            日期取**用户钟下的今天**（对方的 12-25 不是服务器的 12-25）。只认
            ``greet=True`` 的条目——国庆/国难日之类绝不群发「快乐」。"""
            try:
                from src.companion.locale_holidays import (
                    country_for_language,
                    holidays_on,
                )
                from src.companion.user_clock import user_now
                conv = _conv_index.get(cid) or {}
                clock = _resolve_clock(cid)
                lang = str(conv.get("language") or "")
                country = str(getattr(clock, "country", "") or "")
                if not country:
                    country = country_for_language(lang)
                if not country:
                    return None
                today = user_now(clock, time.time()).date()
                for h in holidays_on(today, country):
                    if not getattr(h, "greet", False):
                        continue
                    name = (str(getattr(h, "name_en", "") or "")
                            if lang.startswith("en")
                            else str(getattr(h, "name_zh", "") or ""))
                    name = name or str(getattr(h, "name_zh", "") or "")
                    key = str(getattr(h, "key", "") or "")
                    if key and name:
                        return (key, name)
                return None
            except Exception:
                assistant.logger.debug(
                    "[proactive] 地区节日解析失败 cid=%s", cid, exc_info=True)
                return None

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
            # P5 checkin 效能门控：升级链（生活分享/天气/画像采集）都没接住、
            # 最终仍是裸 checkin → 按两臂回复率差距的比例本日跳过（crc32(cid#日)
            # 确定性——15min tick 重掷会把「跳过」磨成「延迟」）。预览走同一
            # _opener，看板与真发口径天然一致。
            if _mg_on and str((op or {}).get("mode") or "") == "gentle_checkin":
                try:
                    _sp = float(
                        (_mode_gate_snapshot() or {}).get("skip_prob") or 0.0)
                    if _sp > 0 and should_skip_checkin(
                            str(contact_key or memory_key or ""), _sp):
                        try:
                            from src.companion.proactive_stats import (
                                record_checkin_gate,
                            )
                            record_checkin_gate()
                        except Exception:
                            pass
                        return {"mode": "", "directive": "", "fact": "",
                                "silent_hours": (op or {}).get(
                                    "silent_hours", 0.0)}
                except Exception:
                    assistant.logger.debug(
                        "[proactive] checkin 门控异常（放行）", exc_info=True)
            return op

        cd_path = Path(assistant.config.config_path).parent / "companion_proactive_cooldown.json"
        # 账本 v2（P0）：除冷却时间外记「连续未回 streak + 上次主动文案」；
        # 旧 float 格式文件透明升级，读写同一路径。
        _cd_store = JsonProactiveLedger(cd_path)

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

        def _goal_priority(plan):
            # 营销目标排序增益（companion.goals.bridge，默认关）：auto 档活跃目标
            # 的会话优先占每 tick 名额——只改排序不改准入；桥关时恒 0（退化为纯
            # 沉默时长降序）。轻量 store 读，绝不抛。
            try:
                from src.companion.goals.bridge import plan_priority
                return plan_priority(
                    assistant.config.config or {},
                    getattr(assistant.config, "config_path", None), plan)
            except Exception:
                return 0.0

        def _proactive_preview(limit=50):
            """可观测预览（dry-run）：本轮"会主动联系谁、引用哪条记忆、带哪些背景"。
            不发送、不写冷却；即便功能未启用也可调用（开闸前先看清候选）。"""
            lim = max(1, min(int(limit or 50), 200))
            try:
                convs = _conversations()
            except Exception:
                convs = []
            try:
                cooldown_map = JsonProactiveLedger(cd_path).snapshot()
            except Exception:
                cooldown_map = {}
            # 预览展示全部候选（最多 lim 条），不受 max_per_tick 截断；
            # 另标出本 tick 实际会发的前 N 条（目标优先级 + 沉默时长同真发口径，
            # 预览才不骗人）。P1-198：pacing 改为现读（与真发循环同一闭包），
            # 运营改完 overlay 刷新预览立即看到新节奏，不再显示启动时的旧值。
            _lv = _live_pacing_cfg()
            _pp_live = dict(_pp_params)
            for _k in ("min_silent_hours", "cooldown_hours",
                       "quiet_start_hour", "quiet_end_hour"):
                if _k in _lv:
                    _pp_live[_k] = _lv[_k]
            _max_tick_live = int(_lv.get("max_per_tick", _real_max_per_tick))
            _pacing_live = _lv.get("pacing_cfg", _pacing_cfg)
            _backoff_live = _lv.get("backoff_cfg", _backoff_cfg)
            _response_live = _lv.get("response_pacing_cfg", _response_cfg)
            # P1-198 候选诊断：闸口原因收集（「为什么今天不发」从翻代码变成看板可读）
            _skips: list = []
            plans = plan_proactive_sends(
                convs, cooldown_map=cooldown_map, opener_fn=_opener,
                has_pending_care=_has_pending_care, max_per_tick=lim, **_pp_live,
                pacing_cfg=_pacing_live, priority_fn=_goal_priority,
                backoff_cfg=_backoff_live,
                response_pacing_cfg=_response_live,
                diagnostics=_skips,
                # 预览与真发同口径：安静时段按用户钟判，预览才不骗人
                user_clock_provider=_user_clock if _uc_enabled else None)
            for i, p in enumerate(plans):
                p["would_send_this_tick"] = i < _max_tick_live
            _skip_counts: dict = {}
            for _s in _skips:
                _r = str(_s.get("reason") or "other")
                _skip_counts[_r] = int(_skip_counts.get(_r, 0)) + 1
            # 样本按「离能发最近」排（min_silent 的沉默降序最有行动价值）
            _skip_samples = sorted(
                _skips, key=lambda s: float(s.get("silent_hours") or -1.0),
                reverse=True)[:12]
            return {
                "enabled": enabled,
                "dry_run": bool(_lv.get("dry_run", cfg.get("dry_run", False))),
                "scanned": len(convs),
                "candidates": len(plans),
                "max_per_tick": _max_tick_live,
                "min_silent_hours": _pp_live["min_silent_hours"],
                "cooldown_hours": _pp_live["cooldown_hours"],
                "adaptive_pacing": _pacing_live,
                "no_reply_backoff": _backoff_live or {"enabled": False},
                "variety_guard": {
                    "enabled": _variety_on,
                    "similarity_threshold": _sim_threshold},
                "quiet_hours": [_pp_live["quiet_start_hour"], _pp_live["quiet_end_hour"]],
                "care_dedup_active": care_store is not None,
                "plans": plans,
                # P1-198 候选诊断：谁被哪个闸口拦下、离能发还差多少——
                # 「主动队列看起来没动」从猜测变成一屏可读原因。
                "skip_summary": _skip_counts,
                "skipped": _skip_samples,
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

        def _persona_style(plan) -> str:
            """人设说话风格一行（personality.style + style_hint，截断）。

            P0 修「千人一面」：此前主动 prompt 只带人设名字，七个人设写出同一句
            「好久没联系啦」——被动回复链有完整人设，主动消息也该是同一个「人」。
            解析失败返回 ""（prompt 层零行为变化）。"""
            try:
                from src.ai.persona_voice import resolve_effective_persona_id
                from src.utils.persona_manager import PersonaManager
                pid = resolve_effective_persona_id(
                    assistant.config.config or {},
                    str(plan.get("platform") or ""),
                    str(plan.get("account_id") or ""),
                    str(plan.get("chat_key") or ""))
                if not pid:
                    return ""
                p = PersonaManager.get_instance().get_persona_by_id(pid) or {}
                pers = p.get("personality")
                style = str(pers.get("style") or "") if isinstance(pers, dict) else ""
                hint = " ".join(str(p.get("style_hint") or "").split())
                seg = "；".join(s for s in (style, hint) if s)
                return seg[:160]
            except Exception:
                return ""

        def _avoid_texts(plan) -> list:
            """「禁止相似」负样本：账本里上次主动的文案 + 末尾连续未回的出站消息。

            这些就是「发过、没得到回应」的开场——新文案与其雷同＝复读机实锤
            （生产数据：同句式 x3/x2/x2）。取不到任何一项都软失败返回 []。"""
            out: list = []
            cid = str(plan.get("conversation_id") or "")
            try:
                e = _cd_store.entry(cid)
                if e and e.get("last_text"):
                    out.append(str(e["last_text"]))
            except Exception:
                pass
            try:
                from src.utils.proactive_variety import trailing_unanswered_texts
                msgs = assistant.inbox_store.list_recent_messages(
                    cid, limit=12) or []
                out.extend(trailing_unanswered_texts(msgs, max_texts=3))
            except Exception:
                pass
            seen: set = set()
            dedup: list = []
            for t in out:
                k = str(t).strip()
                if k and k not in seen:
                    seen.add(k)
                    dedup.append(k)
            return dedup[:4]

        async def _gen_text(plan, scene_note: str = "", avoid_texts=None):
            """按 plan 生成"要发出去的那一句"（directive + 背景记忆 + 最近上下文）。
            只生成、不发送；ai 未就绪或空回复 → 返回 ""。真发 _send 与试发预览共用。

            Phase13 修：带 ``peer_language`` 进 prompt——给说英语的客户必须发英语
            开场（此前给全程英文会话发了中文开场+中文语音，一眼机器人）。
            Phase17：``scene_note`` 非空 = 本条会附生活照，文案须自然带到该场景
            （文案-场景对齐，图文一体）。
            P0（2026-07-29）：上下文带方向+相对时间（LLM 此前分不清「上一条是我
            自己发的问候且没被回」）；注入人设风格；``avoid_texts`` 作反复读负样本。"""
            try:
                msgs = assistant.inbox_store.list_recent_messages(
                    plan["conversation_id"], limit=10) or []
            except Exception:
                msgs = []
            from src.utils.proactive_variety import format_recent_context
            ctx = format_recent_context(msgs, now=time.time())
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
                peer_language=_peer_language(plan), scene_note=scene_note,
                persona_style=_persona_style(plan),
                avoid_texts=list(avoid_texts or []))
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
                "platform": str(conv.get("platform") or ""),
                "account_id": str(conv.get("account_id") or ""),
                "chat_key": str(conv.get("chat_key") or ""),
                "directive": str(opener.get("directive") or ""),
                "context_facts": list(opener.get("context_facts") or []),
                "mode": str(opener.get("mode") or ""),
                "gap_bucket": str(opener.get("gap_bucket") or ""),
                "silent_hours": round(silent_hours, 1),
            }
            text = await _gen_text(
                plan, avoid_texts=_avoid_texts(plan) if _variety_on else [])
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

        def _media_skip(kind: str, reason: str) -> None:
            """语音/照片分支跳过原因计数（P1 透传归因）。best-effort 绝不抛。"""
            try:
                from src.companion.proactive_stats import record_media_skip
                record_media_skip(kind, reason)
            except Exception:
                pass

        async def _try_send_voice(plan, text) -> bool:
            """主动开场语音分支：中文→克隆声(7852/预渲染)；外语→edge 多语(Phase15)。
            失败 False=回落文本。P1：每个早退/失败口记原因（修「配置 50% 实际
            6% 无人知晓」的观测盲区）。
            """
            import random as _rnd

            v_cfg = cfg.get("voice") if isinstance(cfg.get("voice"), dict) else {}
            if _mfb_on and v_cfg.get("enabled", False):
                # P3 反哺：概率按分形态回复率自校准（其余闸门原样过）
                v_cfg = dict(v_cfg, probability=_fb_probability(
                    "voice", float(v_cfg.get("probability", 0.5) or 0.5)))
            _ok, _why = voice_gate_verdict(v_cfg, text, _rnd.random())
            if not _ok:
                _media_skip("voice", _why)
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
                    if not foreign_voice_allowed(_fb, _plang):
                        _media_skip("voice", "foreign_disabled")
                        return False
                    staged = await stage_foreign_voice_file(
                        _cfg_root, platform, account_id, text,
                        peer_language=_plang)
                    if not staged:
                        _media_skip("voice", "foreign_stage_failed")
                        return False
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
                    _media_skip("voice", "foreign_deliver_failed")
                except Exception:
                    _media_skip("voice", "foreign_error")
                    assistant.logger.info(
                        "[proactive] 外语语音开场失败，回落文本", exc_info=True)
                return False

            # 中文：克隆声全套（预渲染 / 7852 混合保真）
            try:
                from src.integrations.account_orchestrator import get_orchestrator
                orch = get_orchestrator(_cfg_root)
                if not orch.owns(platform, account_id):
                    # A 线 default 账号会话无 worker 语音通道——若此原因占大头，
                    # 说明值得给 A 线补语音发送能力（P2 候选，先让数据说话）
                    _media_skip("voice", "not_owned")
                    return False
                from src.ai.persona_voice import resolve_effective_persona_id
                from src.inbox.voice_autosend import (
                    persona_allowed_for_voice,
                    stage_voice_file,
                )
                # 含会话覆写：主动语音开场与自动回复同声（换绑后不串音色）
                pid = resolve_effective_persona_id(
                    _cfg_root, platform, account_id, str(chat_key or ""))
                _l2_voice = (((_cfg_root.get("inbox") or {})
                             .get("l2_autosend") or {}).get("voice") or {})
                if not persona_allowed_for_voice(_l2_voice, pid):
                    _media_skip("voice", "persona_denied")
                    return False
                staged = await stage_voice_file(
                    _cfg_root, platform, account_id,
                    pid, text, contact_key=chat_key)
                if not staged:
                    _media_skip("voice", "stage_failed")
                    return False
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
                _media_skip("voice", "deliver_failed")
            except Exception:
                _media_skip("voice", "error")
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
            if _mfb_on and p_cfg.get("enabled", False):
                # P3 反哺：只动概率，min_intimacy/modes/daily_cap 护栏全不动
                p_cfg = dict(p_cfg, probability=_fb_probability(
                    "photo", float(p_cfg.get("probability", 0.25) or 0.25)))
            _pok, _pwhy = photo_share_verdict(
                p_cfg, mode=str(plan.get("mode") or ""),
                intimacy=float(plan.get("intimacy") or 0.0),
                rand01=_rnd.random())
            if not _pok:
                _media_skip("photo", _pwhy)
                return None
            if _photo_cap is not None and _photo_cap.would_exceed(1):
                _media_skip("photo", "daily_cap")
                return None
            _cfg_root = assistant.config.config or {}
            try:
                from src.inbox.image_autosend import resolve_image_autosend_cfg
                scfg = resolve_image_autosend_cfg(_cfg_root)
                if not scfg.get("enabled", False):
                    _media_skip("photo", "selfie_disabled")
                    return None  # 依赖发图能力总开关（companion.selfie.enabled）
                from src.integrations.account_orchestrator import get_orchestrator
                if not get_orchestrator(_cfg_root).owns_media(
                        plan["platform"], plan["account_id"]):
                    _media_skip("photo", "not_owned")
                    return None
                from src.ai.persona_voice import resolve_effective_persona_id
                # 含会话覆写：主动生活照的人设/场景池与该会话生效人设一致
                pid = resolve_effective_persona_id(
                    _cfg_root, plan["platform"], plan["account_id"],
                    str(plan.get("chat_key") or ""))
                if not pid:
                    _media_skip("photo", "no_persona")
                    return None
                # 人设级发图闸（2026-07-31，默认关；photo_capability SSOT）：
                # 主动生活照与被动要图同一门——人设没开相册，主动触达也不发图
                # （否则聊天里说「发不了照片」、主动消息却带图，自相矛盾）。
                from src.companion.photo_capability import (
                    persona_photos_enabled_by_id,
                )
                if not persona_photos_enabled_by_id(pid):
                    _media_skip("photo", "persona_photos_off")
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
                _media_skip("photo", "error")
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
                    _media_skip("photo", "stage_failed")
                    return False
                local, url, _kind = staged[:3]
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
                _media_skip("photo", "deliver_failed")
            except Exception:
                _media_skip("photo", "error")
                assistant.logger.info(
                    "[proactive] 生活照开场失败，回落语音/文本", exc_info=True)
            return False

        def _guard_offer_text(text: str, plan=None) -> str:
            """剥掉目标驱动开场里未授权的折扣/券码/赠送承诺（P14）。绝不抛。"""
            try:
                cfg = getattr(assistant.config, "config", None) or {}
                _og = (((cfg.get("companion") or {}).get("goals") or {})
                       .get("offer_guard") or {})
                if not bool(_og.get("enabled", True)):
                    return text
                from src.companion.goals import offers as offers_mod
                from src.companion.goals import site_catalog as sc
                from src.companion.goals.offer_guard import sanitize_offer_claims
                catalog = sc.load_catalog(sc.catalog_path(
                    cfg, getattr(assistant.config, "config_path", None)))
                out, n, hits = sanitize_offer_claims(
                    text,
                    allowed_texts=offers_mod.allowlist_texts(catalog),
                    allowed_free_days=offers_mod.authorized_free_days(catalog))
                if n:
                    assistant.logger.warning(
                        "[proactive] 未授权优惠承诺已剥离 %d 处: %s",
                        n, " | ".join(hits[:3]))
                    pid = ""
                    try:
                        # 人设归属（P16）：与生活照/语音分支同一解析口径
                        from src.ai.persona_voice import (
                            resolve_effective_persona_id,
                        )
                        pid = str(resolve_effective_persona_id(
                            cfg, (plan or {}).get("platform") or "",
                            (plan or {}).get("account_id") or "",
                            str((plan or {}).get("chat_key") or "")) or "")
                    except Exception:
                        pid = ""
                    try:
                        from src.companion.goals.stats import get_goal_stats
                        get_goal_stats().record_offer_claim_stripped(
                            n, samples=hits, source="proactive", persona=pid)
                    except Exception:
                        pass
                return out
            except Exception:
                return text

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
            # -2) P1 opt-out 检测（发送前、生成前——省 LLM/GPU）：对方在我们上次
            # 静默记录之后的入站里说过「别再发了」→ 静默 mute_days 天并放弃本次。
            # 检测窗口限定「上次静默记录之后的入站」防循环：静默因对方回归解除后，
            # 旧的那句退订不会再次触发静默。
            if _optout_on:
                try:
                    from src.utils.proactive_optout import detect_optout
                    _cid_oo = str(plan.get("conversation_id") or "")
                    _prev_oo = _optout_mutes.get(_cid_oo) or {}
                    _min_ts = float(_prev_oo.get("ts") or 0.0)
                    _msgs_oo = assistant.inbox_store.list_recent_messages(
                        _cid_oo, limit=20) or []
                    _in_texts = [
                        str(m.get("text") or "") for m in _msgs_oo
                        if str(m.get("direction") or "") == "in"
                        and float(m.get("ts") or 0.0) > _min_ts][-3:]
                    _hit = detect_optout(_in_texts)
                    if _hit:
                        _now_oo = time.time()
                        _optout_mutes[_cid_oo] = {
                            "ts": _now_oo,
                            "until": _now_oo + _optout_days * 86400.0,
                            "hit": _hit,
                        }
                        _persist_optout()
                        _cd_store.mark_attempt(_cid_oo, _now_oo)
                        try:
                            from src.companion.proactive_stats import (
                                record_optout_mute,
                            )
                            record_optout_mute()
                        except Exception:
                            pass
                        assistant.logger.info(
                            "[proactive] opt-out 静默 cid=%s %.0f 天（命中：%r）",
                            _cid_oo, _optout_days, _hit[:40])
                        return False
                except Exception:
                    assistant.logger.debug(
                        "[proactive] opt-out 检测异常（忽略）", exc_info=True)
            # -1) 营销目标桥（companion.goals.bridge，默认关）：auto 档目标 + auto_ai
            # 会话 → 把「今日拍」意图并进开场 directive（顺风车，不新增发送）。
            # best-effort：桥内部异常绝不影响开场本身。
            try:
                from src.companion.goals.bridge import augment_plan_with_goal
                from src.integrations.protocol_bridge import get_inbox_store
                augment_plan_with_goal(
                    assistant.config.config or {},
                    getattr(assistant.config, "config_path", None),
                    plan, inbox_store=get_inbox_store())
            except Exception:
                assistant.logger.debug("[proactive] 目标桥跳过", exc_info=True)
            # 0) 生活照预决策（Phase17/18）：先定"要不要配图 + 什么场景"，场景注入文案
            _photo_plan = await _plan_photo(plan)
            _scene = _photo_plan[1] if _photo_plan else ""
            # 1) 生成开场文案（directive + 背景记忆 + 最近上下文 ± 场景叙事），
            #    带「禁止相似」负样本（上次主动文案 + 末尾未回连发）。
            _avoid = _avoid_texts(plan) if _variety_on else []
            text = await _gen_text(plan, scene_note=_scene, avoid_texts=_avoid)
            if not text:
                return False
            # 1.05) 变体守卫（P0）：新文案与未回开场雷同 → 换写一次；仍雷同 →
            # 本轮放弃并推时间戳（不推 streak——没发出去不算打扰；推时间防每
            # 15min 重烧 LLM）。宁可不发也不做复读机。
            if _variety_on and _avoid:
                from src.utils.proactive_variety import most_similar
                _dup = most_similar(text, _avoid, threshold=_sim_threshold)
                if _dup is not None:
                    retry = await _gen_text(
                        plan, scene_note=_scene, avoid_texts=_avoid + [text])
                    if retry and most_similar(
                            retry, _avoid + [text],
                            threshold=_sim_threshold) is None:
                        text = retry
                    else:
                        _cd_store.mark_attempt(
                            plan["conversation_id"], time.time())
                        try:
                            from src.companion.proactive_stats import (
                                record_variety_block,
                            )
                            record_variety_block()
                        except Exception:
                            pass
                        assistant.logger.info(
                            "[proactive] 变体守卫拦截 cid=%s：文案与未回开场雷同"
                            "（新=%r ≈ 旧=%r），本轮放弃",
                            plan.get("conversation_id"),
                            (retry or text)[:36], _dup[:36])
                        return False
            # 1.1) 出站优惠守卫（P14）：目标桥带来的开场也可能被 LLM 加一句
            # 「给你打个折」。只守目标驱动的开场（普通陪伴开场不碰），文案/
            # 配图配文/语音稿三条分支同源，所以放在这里一次搞定。
            if (plan or {}).get("_goal_action_id"):
                text = _guard_offer_text(text, plan)
            # 账本记录用：最终要发出的文案（成功后 mark_send 写入，供下次反复读）
            plan["_sent_text"] = text
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
                    _note_send_result(plan["conversation_id"], _ok, platform)
                    return _ok
            except Exception as e:
                # PEER_ID_INVALID = session 不认识对方（无 access_hash，多为
                # 对方已注销/换号或迁移遗留会话）→ 拉黑不再重试，且升 info 可见
                # （debug 级曾把"每 tick 全军覆没"藏了一上午）。
                # 分类走共享纯函数（单一事实源，与 A 线 sender 同一词表）。
                # ⚠ 场景差异（刻意）：peer_unresolved（PEER_ID_INVALID/CHANNEL_INVALID）
                # 在 A 线是「本地缓存缺 access_hash」→ dialogs 预热能救，故不拉黑；
                # 但本模块是**私聊主动触达**，没有预热自愈路径，且这里解析不了基本就是
                # 死号/迁移遗留会话——保持既有「立即拉黑」语义（改成走连败计数会让
                # 编排器账号的坏 peer 永不拉黑：本 except 不计连败、非 default 账号也
                # 不回落主客户端 → 退回「每 tick 重烧坏 peer」老问题）。
                # 记 registry 时映射成可解除的 blocked（registry 只收永久类 reason）。
                from src.ops.dead_peer_registry import (
                    classify_send_error as _dp_classify,
                    is_permanent_reason as _dp_permanent,
                )
                _rsn = _dp_classify(e)
                if _rsn:
                    _mark_bad_peer(
                        plan["conversation_id"], platform=platform,
                        reason=_rsn if _dp_permanent(_rsn) else "blocked")
                    assistant.logger.info(
                        "[proactive] peer 不可达已拉黑 %s（%s: %s）",
                        plan["conversation_id"], _rsn, e)
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
                    _note_send_result(plan["conversation_id"], bool(ok), platform)
                    return bool(ok)
                except Exception:
                    assistant.logger.debug("[proactive] 主客户端发送失败", exc_info=True)
                    _note_send_result(plan["conversation_id"], False, platform)
                    return False
            return False

        def _on_teaser_sent(plan) -> None:
            # 营销目标桥回执：带目标意图的开场真发成功 → 今日拍记 sent（防同日重复带）。
            try:
                if (plan or {}).get("_goal_action_id"):
                    from src.companion.goals.bridge import on_proactive_sent
                    on_proactive_sent(plan)
            except Exception:
                assistant.logger.debug("[proactive] 目标拍回执失败", exc_info=True)
            _mode = str((plan or {}).get("mode") or "")
            # P1 观测：真发成功的 mode 分布（「兜底占 100%」这种退化在看板一眼可见）。
            try:
                from src.companion.proactive_stats import record_sent_mode
                record_sent_mode(_mode)
            except Exception:
                pass
            # P1：生活分享真发成功才扣周配额（规划/生成失败/被守卫拦下都不扣）。
            if _mode == "life_share":
                try:
                    assistant.skill_manager.mark_life_share_sent(
                        str((plan or {}).get("conversation_id") or ""))
                except Exception:
                    assistant.logger.debug(
                        "[proactive] life_share 配额落账失败", exc_info=True)
            # P1 质量闭环：真发文案落样本库（此前只记「试发」采样，真发的文案
            # 反而无处评分）——运营在采样面板 👍/👎 的就是真实发出的开场。
            if sample_store is not None and (plan or {}).get("_sent_text"):
                try:
                    sample_store.record_sample(
                        conversation_id=str(plan.get("conversation_id") or ""),
                        account_id=str(plan.get("account_id") or ""),
                        mode=_mode,
                        fact=str(plan.get("fact") or ""),
                        context_facts_n=len(plan.get("context_facts") or []),
                        silent_hours=float(plan.get("silent_hours") or 0.0),
                        text=str(plan.get("_sent_text") or ""))
                except Exception:
                    assistant.logger.debug(
                        "[proactive] 真发采样落库失败", exc_info=True)
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

            def _inbound_ts(cid):
                """该用户历史**入站**消息的时间戳（仅候选才查，控成本）。"""
                try:
                    msgs = assistant.inbox_store.list_recent_messages(
                        cid, limit=80) or []
                except Exception:
                    return []
                out = []
                for m in msgs:
                    if str(m.get("direction") or "") != "in":
                        continue
                    try:
                        ts = float(m.get("ts") or 0)
                    except (TypeError, ValueError):
                        ts = 0.0
                    if ts > 0:
                        out.append(ts)
                return out

            def _active_hours(cid):
                # 遗留口径：**服务器本地**小时样本（跨时区不正确，只在用户时钟未启用时用）。
                return [time.localtime(ts).tm_hour for ts in _inbound_ts(cid)]

            def _active_utc_hours(cid):
                # UTC 小时样本：与时区无关的原始事实，由规划器按该会话时钟换算成对方的
                # 本地小时再推断作息（服务器小时会把「对方的早上」记成别的时段）。
                return [
                    datetime.fromtimestamp(ts, tz=timezone.utc).hour
                    for ts in _inbound_ts(cid)
                ]

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
                    # 用户时钟未启用 → 一个新参数都不传（逐位旧行为）
                    **({
                        "user_clock_provider": _user_clock,
                        "active_utc_hours_provider": (
                            _active_utc_hours if _personalize else None),
                    } if _uc_enabled else {}),
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
                        # 各自独立：时钟接管整点/日期，地区节日换掉中文公历日历；
                        # 两个都未启用时一个参数都不传（逐位旧行为）。
                        **({"user_clock_provider": _user_clock}
                           if _uc_enabled else {}),
                        **({"locale_holiday_provider": _locale_holiday}
                           if _lh_enabled else {}),
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
            cooldown_store=_cd_store,
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
            priority_fn=_goal_priority,
            user_clock_provider=_user_clock if _uc_enabled else None,
            backoff_cfg=_backoff_cfg,
            response_pacing_cfg=_response_cfg,
            live_cfg_provider=_live_pacing_cfg,
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
