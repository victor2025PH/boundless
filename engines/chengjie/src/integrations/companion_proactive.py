"""P2 主动话题调度：沉默检测 + 冷却 → 选 P1 话题 → 经回调发出。

陪伴型 AI 的核心差异点：用户久未说话时，**主动**自然回到对方在意的事
（"上次你说在备考，后来怎么样？"），而不是被动等消息。本模块负责"何时发"，
话题种子由 P1 ``select_proactive_topic`` 产出（经 ``opener_fn`` 注入），真实文案
由回复生成层产出（经 ``send_fn`` 注入）。

设计（与 reactivation_loop / care_dispatcher 同范式）：
- ``plan_proactive_sends`` 是**确定性纯函数**：给定会话快照 + 冷却表 + 时钟，
  决定该给哪些会话主动开场、用什么指令。零 IO、可单测。
- ``CompanionProactiveLoop`` 是**薄异步循环**：now/sleep/send/cooldown 全可注入，
  单测用假时钟 + 假发送确定性驱动，无需真账号、无需长 sleep。
- **默认关**：上层 ``companion.proactive_topic.enabled`` 控；本模块只是机制，不自启。

护栏：
- 沉默不足不打扰（min_silent_hours）。
- 冷却：同一会话两次主动开场至少间隔 cooldown_hours，避免骚扰。
- 安静时段（quiet_start..quiet_end，默认 23–8）不发，错过则下个 tick 再看。
- **只在我方说完后冷场才主动**：若最后一条是对方消息（last_direction=="in"），
  那是"我欠回复"（SLA 范畴），不在此主动开场，交给坐席/自动回复处理。
- 归档会话不打扰。
- **与 proactive_care(Phase O) 去重**：若某会话已被"记忆驱动关怀"队列排了待发项
  （has_pending_care 返回 True），则本沉默话题让路——避免同一个人被两套主动系统
  同时打扰（care 引用具体约定/事件，优先级更高、更不像"机器到点打卡"）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _in_quiet_hours(hour: int, start: int, end: int) -> bool:
    """当前小时是否落在安静时段。start>end 表示跨午夜（如 23..8）。"""
    start %= 24
    end %= 24
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _resolve_user_clock(
    provider: Optional[Callable[[str], Optional[Any]]], cid: str,
) -> Optional[Any]:
    """注入式取该会话的用户时钟；无 provider / 解析失败 → None（＝服务器钟旧行为）。

    provider 是 IO（读收件箱/记忆），一个坏会话绝不能炸掉整个 tick 的规划。
    """
    if provider is None:
        return None
    try:
        return provider(cid)
    except Exception:
        logger.debug("[proactive] user_clock_provider 失败 cid=%s", cid, exc_info=True)
        return None


def should_skip_recent_active(
    last_ts: float, *, now: float, min_silent_hours: float,
) -> bool:
    """发送前用**最新** last_ts 复核：对方是否近期活跃到不该被主动打扰（纯函数）。

    ``plan_proactive_sends`` 基于 tick 起点快照筛沉默；但从"规划→AI 生成→真发"有秒级~更久
    间隔，对方可能刚开口。发送前拿**最新** last_ts 再判一次：仍在 min_silent 窗口内 →
    True（跳过，别对刚聊过的人发主动"好久不见"，语音更突兀）。last_ts<=0（未知）→ False
    （不拦，退回既有行为）。min_silent_hours<=0 → False（未设阈值不拦）。
    """
    try:
        lt = float(last_ts or 0.0)
        msh = float(min_silent_hours or 0.0)
    except (TypeError, ValueError):
        return False
    if lt <= 0 or msh <= 0:
        return False
    return (float(now) - lt) < msh * 3600.0


def _ledger_entry(value: Any) -> Dict[str, Any]:
    """冷却表条目多格式解析：旧 ``float ts`` / v2 ``{ts, streak, last_text}`` /
    v3 增 ``sent_ts``（上次**真实发送**时间）与 ``obs_n/obs_replied``（回应观察）。

    - 旧格式（升级前的存量文件）没有回应信息 → streak 按 1 保守处理：那批正是
      被反复问候过的会话，若对方其实回过话，规划器会用 last_in_ts 把 streak 归零。
    - ``sent_ts`` 与 ``ts`` 分离（P2）：``mark_attempt``（变体守卫拦下）只推 ``ts``
      防重烧 LLM，但**响应语义**（streak/回复率）必须对照真实发送时刻——否则对方
      明明回过话，也会因我们自己一次被拦的尝试被误判「未回」继续退避。
      缺失时回落 ``ts``（v2 存量语义不变）。
    """
    if isinstance(value, dict):
        def _f(key: str) -> float:
            try:
                return float(value.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        def _i(key: str) -> int:
            try:
                return max(0, int(value.get(key) or 0))
            except (TypeError, ValueError):
                return 0

        ts = _f("ts")
        # 键**缺失**（v2 存量）才回落 ts（那时 ts 就是真实发送）；显式 0（只
        # attempt 过、从未真发）必须保 0——否则重载后 attempt 时间被当成真实
        # 发送，幻影观察污染回复率。
        sent_ts = _f("sent_ts") if "sent_ts" in value else ts
        return {
            "ts": ts, "sent_ts": sent_ts, "streak": _i("streak"),
            "last_text": str(value.get("last_text") or ""),
            "obs_n": _i("obs_n"), "obs_replied": _i("obs_replied"),
        }
    try:
        ts = float(value or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    return {"ts": ts, "sent_ts": ts, "streak": 1 if ts > 0 else 0,
            "last_text": "", "obs_n": 0, "obs_replied": 0}


def plan_proactive_sends(
    conversations: List[Dict[str, Any]],
    *,
    cooldown_map: Dict[str, Any],
    opener_fn: Callable[..., Dict[str, Any]],
    now: Optional[float] = None,
    min_silent_hours: float = 24.0,
    cooldown_hours: float = 72.0,
    max_per_tick: int = 3,
    quiet_start_hour: float = 23.0,
    quiet_end_hour: float = 8.0,
    has_pending_care: Optional[Callable[[str], bool]] = None,
    on_crisis_block: Optional[Callable[[Dict[str, Any]], None]] = None,
    pacing_cfg: Optional[Dict[str, Any]] = None,
    priority_fn: Optional[Callable[[Dict[str, Any]], float]] = None,
    user_clock_provider: Optional[Callable[[str], Optional[Any]]] = None,
    backoff_cfg: Optional[Dict[str, Any]] = None,
    response_pacing_cfg: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """决定本轮该主动开场的会话清单（确定性纯函数）。

    Args:
        conversations: 会话快照列表，每项含 ``conversation_id/platform/account_id/
            chat_key/last_ts/last_direction/archived`` 及给 opener 的 ``memory_key/
            stage/intimacy``。
        cooldown_map: ``{conversation_id: 上次主动开场时间戳}``。
        opener_fn: ``opener_fn(memory_key=, silent_hours=, stage=, intimacy=,
            last_emotion=, contact_key=) -> {mode, directive, fact, context_facts, ...}``
            （即 build_proactive_opener；``last_emotion`` 供情绪护栏判低谷 soft 抑制；
            ``contact_key`` 供付费解锁预告查端用户真实权益、排除已解锁者）。
        has_pending_care: 可选谓词 ``(conversation_id) -> bool``——返回 True 表示该会话
            已被 proactive_care(Phase O) 排了待发关怀，本主动话题让路跳过（去重）。
        on_crisis_block: 可选回调 ``(conversation_snapshot) -> None``——当 opener 因近期
            severe 危机被护栏拦下（``blocked == "crisis_severe"``）时调用，让派发层把该
            用户排进 care 队列（危机关怀升级：把"静默"变"接住"）。IO 留在回调里、纯函数
            不落库；失败吞掉、绝不影响其余会话的计划。
        now: 注入"现在"（测试用）。

    Returns:
        计划列表 ``[{conversation_id, platform, account_id, chat_key, mode,
        directive, fact, context_facts, scenario_id, feature, silent_hours}]``，
        按沉默时长降序，截断到 max_per_tick。
        pacing_cfg: 可选，``parse_adaptive_pacing_cfg`` 产出；enabled 时按 intimacy
            逐会话缩放 min_silent_hours / cooldown_hours。
        priority_fn: 可选排序增益 ``(plan) -> float``（如营销目标桥：有 auto 档活跃
            目标的会话优先占每 tick 名额）。只影响**排序**不影响准入——所有护栏照旧；
            回调异常按 0 处理。提供时 plan 带 ``goal_priority`` 字段（预览可见）。
        user_clock_provider: 可选 ``(cid) -> UserClock|None``。**不给**＝服务器钟在安静
            时段就整 tick 早退（零成本、零行为变化）；**给了**则撤掉全局早退，改为逐会话
            过 ``user_clock.in_quiet_hours``——那里收口了三档安全语义：显式信号
            （replace）只看用户钟、行为推断（narrow）「服务器安静 **或** 用户安静都算
            安静」只收窄绝不新开窗口、弱信号（advisory）完全不参与调度。解析排在
            沉默/冷却等便宜过滤**之后**；provider 异常按无时钟处理。
            ``silent_hours`` / pacing / 冷却均为纯时长差，与时区无关，故一律不动。
        backoff_cfg: 可选，``parse_no_reply_backoff_cfg`` 产出。**未回退避**（P0
            2026-07-29）：上一条主动开场后对方没回过话（按快照 ``last_in_ts`` 与
            冷却条目 streak 判定）→ 冷却按 multiplier^streak 拉长、封顶
            max_backoff_hours；``stop_after>0`` 且达到 → 该会话彻底停发直到对方
            先开口。None＝不退避（旧行为）。``cooldown_map`` 兼容旧 float 与新
            ledger dict 两种条目格式。
        response_pacing_cfg: 可选，``parse_response_pacing_cfg`` 产出。**回复率
            反哺**（P2）：账本 obs_n/obs_replied 的长期回复率 → 冷却×stretch
            （慢性低回复，与 streak 急性退避正交叠加、封顶仍 720h）或 ×relax
            （慢性高回复，≤1 且 ≥0.5）；样本 < min_obs 不判。None＝不反哺。
    """
    from src.companion.user_clock import in_quiet_hours as _user_in_quiet_hours
    from src.companion.user_clock import schedule_clock as _schedule_clock
    from src.utils.proactive_pacing import (
        backoff_cooldown_hours,
        backoff_exhausted,
        calibrate_response_thresholds,
        effective_cooldown_hours,
        effective_min_silent_hours,
        never_replied_exhausted,
        response_rate_factor,
        unanswered_streak,
    )
    now = now if now is not None else time.time()
    # P6 分位阈值自适应：auto_thresholds 开启且账本人群够大时，low/high 按本库
    # 各会话长期回复率分布的分位数校准（每次规划从 cooldown_map 现算——账本在
    # 长大，阈值随之漂移；人群不足/同质自动回落配置值）。纯函数，预览同口径。
    _resp_cfg_eff = response_pacing_cfg
    if response_pacing_cfg is not None and (
            response_pacing_cfg.get("auto_thresholds") or {}).get("enabled"):
        _obs_pairs = []
        for _v in (cooldown_map or {}).values():
            _e = _ledger_entry(_v)
            _obs_pairs.append((_e.get("obs_n", 0), _e.get("obs_replied", 0)))
        _cal = calibrate_response_thresholds(_obs_pairs, response_pacing_cfg)
        _resp_cfg_eff = dict(
            response_pacing_cfg,
            low_rate=_cal["low_rate"], high_rate=_cal["high_rate"])
    local_hour = time.localtime(now).tm_hour
    if (user_clock_provider is None
            and _in_quiet_hours(local_hour, int(quiet_start_hour),
                                int(quiet_end_hour))):
        return []  # 安静时段不打扰（有用户时钟时下沉到每会话判定，见循环内）

    plans: List[Dict[str, Any]] = []
    for c in conversations or []:
        if not isinstance(c, dict) or c.get("archived"):
            continue
        cid = str(c.get("conversation_id") or "")
        if not cid:
            continue
        # 对方最后发言 = 我欠回复，不在此主动开场
        if str(c.get("last_direction") or "") == "in":
            continue
        # 与 proactive_care 去重：已排关怀的会话让路（care 引用具体约定，优先）
        if has_pending_care is not None:
            try:
                if has_pending_care(cid):
                    continue
            except Exception:
                logger.debug("[proactive] has_pending_care 失败 cid=%s", cid, exc_info=True)
        try:
            last_ts = float(c.get("last_ts") or 0)
        except (TypeError, ValueError):
            last_ts = 0.0
        if last_ts <= 0:
            continue
        silent_hours = (now - last_ts) / 3600.0
        try:
            _intim = float(c.get("intimacy") or 0.0)
        except (TypeError, ValueError):
            _intim = 0.0
        _stage = str(c.get("stage") or "")
        _eff_silent = effective_min_silent_hours(
            _intim, stage=_stage, base_hours=min_silent_hours, pacing_cfg=pacing_cfg)
        _eff_cool = effective_cooldown_hours(
            _intim, stage=_stage, base_hours=cooldown_hours, pacing_cfg=pacing_cfg)
        if silent_hours < _eff_silent:
            continue
        entry = _ledger_entry(cooldown_map.get(cid))
        # 冷却窗看 ts（含 attempt 推时，防每 tick 重烧 LLM）；响应语义（streak/
        # 回复率）只对照 sent_ts（真实发送）——被守卫拦下的尝试绝不算「又一次未回」。
        last_pro = entry["ts"]
        last_sent = float(entry.get("sent_ts") or 0.0)
        try:
            _last_in = float(c.get("last_in_ts") or 0.0)
        except (TypeError, ValueError):
            _last_in = 0.0
        # 未回退避：上次真实发送后对方没回过话 → streak 生效，冷却按倍数拉长
        _streak = unanswered_streak(last_sent, entry["streak"], _last_in)
        # P2 回复率反哺：慢性信号先缩放基础冷却（stretch≥1 / relax∈[0.5,1]），
        # 急性 streak 退避随后在其上翻倍并封顶——两信号正交叠加。
        _resp_factor = 1.0
        if _resp_cfg_eff is not None:
            _resp_factor = response_rate_factor(
                entry.get("obs_n", 0), entry.get("obs_replied", 0),
                _resp_cfg_eff)
            _eff_cool *= _resp_factor
        if backoff_cfg is not None:
            if backoff_exhausted(_streak, backoff_cfg):
                continue  # 连续未回达到上限：彻底停发，等对方先开口
            # P1：从未开口的联系人给足 N 次尝试后出圈（0 互动不属陪伴回访语义）
            if never_replied_exhausted(_streak, _last_in, backoff_cfg):
                continue
            _eff_cool = backoff_cooldown_hours(_eff_cool, _streak, backoff_cfg)
        if last_pro and (now - last_pro) < _eff_cool * 3600.0:
            continue
        # 用户时钟安静时段（逐会话）：解析故意排在沉默/冷却等便宜过滤之后，且在
        # opener_fn（读记忆，本循环最贵的一步）之前——半夜的人连开场都不必生成。
        clock: Optional[Any] = None
        conv_hour = local_hour
        if user_clock_provider is not None:
            clock = _resolve_user_clock(user_clock_provider, cid)
            if _user_in_quiet_hours(
                    clock, now, quiet_start=int(quiet_start_hour),
                    quiet_end=int(quiet_end_hour), server_hour=local_hour):
                continue
            conv_hour = _schedule_clock(clock, now)[0]
        try:
            opener = opener_fn(
                memory_key=str(c.get("memory_key") or ""),
                silent_hours=silent_hours,
                stage=str(c.get("stage") or ""),
                intimacy=_intim,
                last_emotion=str(c.get("last_emotion") or ""),
                last_emotion_intensity=float(c.get("last_emotion_intensity") or -1.0),
                contact_key=str(c.get("conversation_id") or ""),
                min_silent_hours=_eff_silent,
            ) or {}
        except Exception:
            logger.debug("[proactive] opener_fn 失败 cid=%s", cid, exc_info=True)
            continue
        # 危机关怀升级：被情绪护栏拦下的 severe 会话 → 排进 care 队列（best-effort），
        # 再正常跳过（mode 为空，不会作普通主动文案发出）。
        if str(opener.get("blocked") or "") == "crisis_severe" and on_crisis_block is not None:
            try:
                on_crisis_block(c)
            except Exception:
                logger.debug("[proactive] on_crisis_block 失败 cid=%s", cid, exc_info=True)
        mode = str(opener.get("mode") or "")
        directive = str(opener.get("directive") or "")
        if not mode or not directive:
            continue
        plans.append({
            "conversation_id": cid,
            "platform": str(c.get("platform") or ""),
            "account_id": str(c.get("account_id") or ""),
            "chat_key": str(c.get("chat_key") or ""),
            "mode": mode,
            "directive": directive,
            "fact": str(opener.get("fact") or ""),
            "context_facts": [
                str(f).strip()
                for f in (opener.get("context_facts") or [])
                if str(f).strip()
            ],
            # Stage 3：剧情邀约/付费预告携带归因元数据（转化漏斗用；其余 opener 为空串）。
            "scenario_id": str(opener.get("scenario_id") or ""),
            "feature": str(opener.get("feature") or ""),
            "silent_hours": round(silent_hours, 1),
            # 措辞档位（P0：好久没联系只许 ≥14 天档说）——prompt 框定层消费
            "gap_bucket": str(opener.get("gap_bucket") or ""),
            "effective_min_silent_hours": round(_eff_silent, 2),
            "effective_cooldown_hours": round(_eff_cool, 2),
            # 未回退避观测：连续几条主动没得到回应、对方最后开口时间
            "unanswered_streak": _streak,
            "last_in_ts": _last_in,
            # P2 回复率反哺观测：长期观察数与本次冷却倍率（1.0=未生效）
            "response_obs": int(entry.get("obs_n") or 0),
            "response_factor": round(_resp_factor, 2),
            "intimacy": round(_intim, 1),
            "stage": _stage,
            # 观测/排障：安静时段按谁的钟判的（server=服务器钟）、时钟偏移、本地小时
            "clock_source": str(getattr(clock, "source", "") or "server"),
            "clock_offset": round(float(getattr(clock, "offset_hours", 0.0) or 0.0), 1),
            "local_hour": conv_hour,
        })

    if priority_fn is not None:
        # 营销目标等业务优先级：先按增益、同增益再按沉默时长——目标会话优先
        # 占据每 tick 名额，但绝不放宽任何准入护栏（上面的过滤已全部走完）。
        for p in plans:
            try:
                p["goal_priority"] = float(priority_fn(p) or 0.0)
            except Exception:
                p["goal_priority"] = 0.0
        plans.sort(
            key=lambda p: (p.get("goal_priority", 0.0), p["silent_hours"]),
            reverse=True)
    else:
        plans.sort(key=lambda p: p["silent_hours"], reverse=True)
    return plans[: max(0, int(max_per_tick))]


class JsonCooldownStore:
    """极简文件持久冷却表（best-effort）：``{conversation_id: 上次主动开场 ts}``。"""

    def __init__(self, path: Any) -> None:
        self.path = Path(path)
        self._data: Dict[str, float] = {}
        try:
            if self.path.exists():
                self._data = {
                    str(k): float(v)
                    for k, v in (json.loads(self.path.read_text("utf-8")) or {}).items()
                }
        except Exception:
            self._data = {}

    def snapshot(self) -> Dict[str, float]:
        return dict(self._data)

    def mark(self, conversation_id: str, ts: float) -> None:
        self._data[str(conversation_id)] = float(ts)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, ensure_ascii=False), "utf-8")
        except Exception:
            logger.debug("[proactive] 冷却表落盘失败", exc_info=True)


# 回应观察滑窗：obs_n 达到窗口即双双减半（整数半衰遗忘）——早期行为不永久
# 主导比率，近期回应习惯权重更高；纯整数无时间戳，账本体积恒定。
_OBS_HALVING_WINDOW = 20


class JsonProactiveLedger:
    """主动开场账本（冷却表 v3，P0 未回退避 + P2 回复率反哺的数据面）。

    条目 ``{conversation_id: {"ts": 冷却时间戳, "sent_ts": 上次真实发送,
    "streak": 连续未回次数, "last_text": 上次主动文案(截断),
    "obs_n"/"obs_replied": 「主动→是否得到回应」长期观察（半衰滑窗）}}``。
    旧 ``{cid: float}`` / v2 dict 读取时透明升级；写盘一律新格式。

    - ``mark_send``：真发成功后登记——对方在**上次真实发送**（sent_ts，而非可能
      被 attempt 推高的 ts）之后回过话 → streak 重置为 1，否则 +1；同时把上一条
      发送的「结局」记进 obs 观察（回了/没回），并记本次文案给「禁止相似」用。
    - ``mark_attempt``：尝试过但没发出（如变体守卫拦下复读文案）→ 只推 ``ts``
      防每 tick 重烧 LLM；**不动** sent_ts / streak / last_text / obs
      （没发出去不算打扰，更不能算「又一次未回」）。
    """

    def __init__(self, path: Any) -> None:
        self.path = Path(path)
        self._data: Dict[str, Dict[str, Any]] = {}
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text("utf-8")) or {}
                self._data = {
                    str(k): _ledger_entry(v) for k, v in raw.items()
                }
        except Exception:
            self._data = {}

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        return {k: dict(v) for k, v in self._data.items()}

    def entry(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        e = self._data.get(str(conversation_id))
        return dict(e) if e else None

    def _persist(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, ensure_ascii=False), "utf-8")
        except Exception:
            logger.debug("[proactive] 主动账本落盘失败", exc_info=True)

    def mark_send(
        self, conversation_id: str, ts: float,
        *, last_in_ts: float = 0.0, text: str = "",
    ) -> None:
        cid = str(conversation_id)
        prev = self._data.get(cid)
        # 只认 sent_ts（内存条目一律经 _ledger_entry 规范化，legacy 已在装载时
        # 回落）——「只 attempt 过」的条目 sent_ts=0，不产生幻影回应观察。
        prev_sent = float(prev.get("sent_ts") or 0.0) if prev else 0.0
        try:
            _li = float(last_in_ts or 0.0)
        except (TypeError, ValueError):
            _li = 0.0
        replied_since = prev_sent > 0 and _li >= prev_sent
        streak = 1 if (prev is None or replied_since) else int(
            prev.get("streak") or 0) + 1
        # P2：上一条真实发送的结局此刻已知 → 记入长期回应观察（半衰滑窗）
        obs_n = int(prev.get("obs_n") or 0) if prev else 0
        obs_replied = int(prev.get("obs_replied") or 0) if prev else 0
        if prev_sent > 0:
            obs_n += 1
            if replied_since:
                obs_replied += 1
            if obs_n >= _OBS_HALVING_WINDOW:
                obs_n //= 2
                obs_replied = min(obs_n, (obs_replied + 1) // 2)
        self._data[cid] = {
            "ts": float(ts), "sent_ts": float(ts), "streak": streak,
            "last_text": str(text or "")[:80],
            "obs_n": obs_n, "obs_replied": obs_replied,
        }
        self._persist()

    def mark_attempt(self, conversation_id: str, ts: float) -> None:
        cid = str(conversation_id)
        e = self._data.get(cid) or _ledger_entry(None)
        e["ts"] = float(ts)
        self._data[cid] = e
        self._persist()

    def mark(self, conversation_id: str, ts: float) -> None:
        """旧接口兼容：无响应信息的登记，按未回 +1 保守处理。"""
        self.mark_send(conversation_id, ts, last_in_ts=0.0, text="")


class CompanionProactiveLoop:
    """陪伴主动话题派发循环（薄监督；机制与时钟解耦，可单测）。"""

    def __init__(
        self,
        *,
        conversations_provider: Callable[[], List[Dict[str, Any]]],
        opener_fn: Callable[..., Dict[str, Any]],
        send_fn: Callable[[Dict[str, Any]], Awaitable[bool]],
        cooldown_store: Any,
        interval_sec: float = 900.0,
        first_delay_sec: float = 0.0,
        min_silent_hours: float = 24.0,
        cooldown_hours: float = 72.0,
        max_per_tick: int = 3,
        quiet_start_hour: float = 23.0,
        quiet_end_hour: float = 8.0,
        dry_run: bool = False,
        has_pending_care: Optional[Callable[[str], bool]] = None,
        on_crisis_block: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_sent: Optional[Callable[[Dict[str, Any]], None]] = None,
        ritual_fn: Optional[Callable[[List[Dict[str, Any]], float], List[Dict[str, Any]]]] = None,
        ritual_cooldown: Any = None,
        fresh_activity_provider: Optional[Callable[[str], float]] = None,
        pacing_cfg: Optional[Dict[str, Any]] = None,
        priority_fn: Optional[Callable[[Dict[str, Any]], float]] = None,
        user_clock_provider: Optional[Callable[[str], Optional[Any]]] = None,
        backoff_cfg: Optional[Dict[str, Any]] = None,
        response_pacing_cfg: Optional[Dict[str, Any]] = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._conversations_provider = conversations_provider
        self._opener_fn = opener_fn
        self._send_fn = send_fn
        # 发送前活跃复核（None=不复核，旧行为）：规划→生成→真发有间隔，对方可能刚开口。
        # 真发前拿最新 last_ts 再判一次，近期活跃则跳过——不对刚聊过的人发主动「好久不见」。
        self._fresh_activity_provider = fresh_activity_provider
        self._pacing_cfg = pacing_cfg
        self._priority_fn = priority_fn
        # 未回退避（None=不退避旧行为）：对「主动了但没回」的会话按倍数拉长冷却
        self._backoff_cfg = backoff_cfg
        # 回复率反哺（None=不反哺）：长期回复率缩放冷却（慢性信号，正交于 streak）
        self._response_pacing_cfg = response_pacing_cfg
        # 用户时钟（None=服务器钟旧行为）：安静时段逐会话判，别把「对方的凌晨」当白天
        self._user_clock_provider = user_clock_provider
        self._cooldown = cooldown_store
        self._ritual_fn = ritual_fn
        self._ritual_cooldown = ritual_cooldown
        self._has_pending_care = has_pending_care
        self._on_crisis_block = on_crisis_block
        self._on_sent = on_sent
        self._interval = float(interval_sec)
        self._first_delay = max(0.0, float(first_delay_sec))
        self._min_silent_hours = float(min_silent_hours)
        self._cooldown_hours = float(cooldown_hours)
        self._max_per_tick = int(max_per_tick)
        self._quiet_start = float(quiet_start_hour)
        self._quiet_end = float(quiet_end_hour)
        self._dry_run = bool(dry_run)
        self._now = now
        self._sleep = sleep
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def run_once(self) -> Dict[str, int]:
        """一次幂等派发步：扫描 → 计划 → 发送 → 记冷却。返回 {planned, sent}。"""
        try:
            convs = list(self._conversations_provider() or [])
        except Exception:
            logger.debug("[proactive] 会话快照获取失败", exc_info=True)
            return {"planned": 0, "sent": 0}
        plans = plan_proactive_sends(
            convs,
            cooldown_map=(self._cooldown.snapshot() if self._cooldown else {}),
            opener_fn=self._opener_fn,
            now=self._now(),
            min_silent_hours=self._min_silent_hours,
            cooldown_hours=self._cooldown_hours,
            max_per_tick=self._max_per_tick,
            quiet_start_hour=self._quiet_start,
            quiet_end_hour=self._quiet_end,
            has_pending_care=self._has_pending_care,
            on_crisis_block=self._on_crisis_block,
            pacing_cfg=self._pacing_cfg,
            priority_fn=self._priority_fn,
            user_clock_provider=self._user_clock_provider,
            backoff_cfg=self._backoff_cfg,
            response_pacing_cfg=self._response_pacing_cfg,
        )
        # 每日仪式问候（晨 / 晚安）：时段驱动、独立每日每档去重；与沉默回访互补。
        # 同一会话本 tick 既到仪式点又够沉默时，仪式优先（不重复打扰一人）。
        if self._ritual_fn is not None:
            try:
                ritual_plans = list(self._ritual_fn(convs, self._now()) or [])
            except Exception:
                logger.debug("[proactive] ritual_fn 失败", exc_info=True)
                ritual_plans = []
            if ritual_plans:
                ritual_ids = {p.get("conversation_id") for p in ritual_plans}
                plans = ritual_plans + [
                    p for p in plans if p.get("conversation_id") not in ritual_ids]
        sent = 0
        for p in plans:
            # 发送前活跃复核：从规划到此刻对方可能刚开口 → 用最新 last_ts 再判一次，
            # 近期活跃则跳过（不发、不记冷却，下轮候选自然刷新）。仪式问候（有 ritual_key，
            # 晨晚安/节日按时点驱动）不受此限——它本就不以「沉默」为前提。
            if (self._fresh_activity_provider is not None
                    and not p.get("ritual_key")):
                try:
                    _cid = str(p.get("conversation_id") or "")
                    _fresh_ts = float(self._fresh_activity_provider(_cid) or 0.0)
                    _msh = float(
                        p.get("effective_min_silent_hours")
                        or self._min_silent_hours)
                    if should_skip_recent_active(
                            _fresh_ts, now=self._now(),
                            min_silent_hours=_msh):
                        logger.info(
                            "[proactive] skip cid=%s 发送前复核发现近期活跃"
                            "（不发主动开场）", _cid)
                        continue
                except Exception:
                    logger.debug("[proactive] 发送前活跃复核异常 cid=%s",
                                 p.get("conversation_id"), exc_info=True)
            ok = False
            try:
                ok = True if self._dry_run else bool(await self._send_fn(p))
            except Exception:
                logger.debug("[proactive] send_fn 失败 cid=%s",
                             p.get("conversation_id"), exc_info=True)
                ok = False
            if ok:
                # 仪式问候记每日每档冷却；沉默回访记会话冷却（互不干扰）。
                rk = p.get("ritual_key")
                if rk and self._ritual_cooldown is not None:
                    self._ritual_cooldown.mark(rk, self._now())
                elif self._cooldown:
                    if hasattr(self._cooldown, "mark_send"):
                        # 账本 v2：登记响应状态（未回 streak）与本次文案（反复读用）
                        self._cooldown.mark_send(
                            p["conversation_id"], self._now(),
                            last_in_ts=float(p.get("last_in_ts") or 0.0),
                            text=str(p.get("_sent_text") or ""))
                    else:
                        self._cooldown.mark(p["conversation_id"], self._now())
                sent += 1
                # Stage 3：发送成功钩子（转化漏斗埋点等）。best-effort，绝不影响派发。
                if self._on_sent is not None:
                    try:
                        self._on_sent(p)
                    except Exception:
                        logger.debug("[proactive] on_sent 失败 cid=%s",
                                     p.get("conversation_id"), exc_info=True)
        if plans:
            logger.info("[proactive] tick: planned=%d sent=%d dry_run=%s",
                        len(plans), sent, self._dry_run)
        try:
            from src.companion.proactive_stats import record_tick
            record_tick(planned=len(plans), sent=sent, dry_run=self._dry_run)
        except Exception:
            pass
        return {"planned": len(plans), "sent": sent}

    async def _loop(self) -> None:
        try:
            # 首轮延迟：等编排器 worker 拉起/收件箱回放完成再开跑——启动即 tick 会
            # 在 worker 未 running 时误走"主客户端回落"（用错账号发送的隐患），
            # 且重启风暴期反复打扰同一批用户。
            if self._first_delay > 0:
                await self._sleep(self._first_delay)
            while self._running:
                try:
                    await self.run_once()
                except Exception:
                    logger.exception("[proactive] run_once 异常")
                await self._sleep(self._interval)
        except asyncio.CancelledError:
            logger.info("[proactive] loop cancelled")
        except Exception:
            logger.exception("[proactive] loop 退出")

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="companion_proactive_loop")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


__all__ = [
    "plan_proactive_sends",
    "JsonCooldownStore",
    "JsonProactiveLedger",
    "CompanionProactiveLoop",
]
