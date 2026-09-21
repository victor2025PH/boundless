"""发送前拟人节奏协作器（已读 → 打字续挂 → 延迟）。

把「真人回复前的表演序列」收敛为**单一纯协作器**：先发平台「已读」回执，再在
打字延迟期间**周期性**挂「正在输入 / 正在录音」状态直到延迟耗尽。多条发送路径
（编排器全自动 autosend、L3 缓冲话术，未来原生 A 线回复）共用同一节奏，消除各自
内联的重复与不一致（此前只有 autosend 有打字续挂）。

为什么是「协作器」而非「策略类」：三条路径的**发送动作**完全不同（编排器 callback /
orch.send / pyrogram 直发），强行统一发送会引入耦合；而「发送前的拟人序列」是它们
**唯一真正相同**的部分，只抽这段、发送留在各调用方，是恰到好处的抽象边界。

设计：
- **纯协作器**：所有副作用经注入的 async 回调（mark_read / typing / sleep）完成，
  零平台耦合、零 IO import → 可确定性单测（假回调 + 假 sleep 驱动）。
- **best-effort**：mark_read / typing 回调异常都吞掉并继续——拟人增强绝不阻断发送。
- **action 语义**：语音回复传 ``record_audio`` → 打字状态显示「正在录音」，否则「正在输入」。
- **计数钩子**：``on_marked`` / ``on_typing`` 在对应回调**成功**后触发（供调用方累计指标），
  异常不触发。
"""
from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Telegram chat action 约 5s 自动过期 → 打字延迟长于此值须周期续挂才不断续；
# WhatsApp presence 'composing' 亦受益于周期刷新。
DEFAULT_TYPING_REFRESH_SEC = 4.0

# 延迟低于此值时不挂打字状态（否则打字气泡闪一下消息就到，反而不真实）→ 直接静默等。
DEFAULT_MIN_TYPING_DELAY_SEC = 1.0

# 内容自适应延迟默认参数（真人「读+想+打字」时长模型）。
_DEFAULT_BASE_SEC = 1.0          # 读消息 + 起念的固定开销
_DEFAULT_PER_CHAR_SEC = 0.08     # 每字打字耗时（CJK 闲聊 ~8-12 字/秒的保守值）
_DEFAULT_JITTER = 0.2            # ±20% 随机抖动（避免同长度恒定时长露馅）
_DEFAULT_AROUSAL_SWING = 0.5     # arousal 对节奏的最大缩放幅度（±50%）


def _arousal_scale(arousal: Optional[float], swing: float = _DEFAULT_AROUSAL_SWING) -> float:
    """把回复文本的**激活度** arousal(0~1) 映射为打字速度缩放。

    真人「怎么打这条回复」：激活度高（兴奋/急切）→ 打字更快（scale<1）；激活度低
    （平静/斟酌/安慰）→ 更慢更深思（scale>1）。arousal=0.5（中性）→ scale=1.0。
    公式：``1 + (0.5 - arousal) × 2 × swing``，夹到 ``[1-swing, 1+swing]``。
    arousal=None（未知）→ 1.0（不缩放）。此信号取自回复自身（非客户消息），语义正确。
    """
    if arousal is None:
        return 1.0
    try:
        a = max(0.0, min(1.0, float(arousal)))
    except (TypeError, ValueError):
        return 1.0
    scale = 1.0 + (0.5 - a) * 2.0 * float(swing)
    return max(1.0 - swing, min(1.0 + swing, scale))


def estimate_thinking_delay(
    text: str,
    *,
    base_sec: float = _DEFAULT_BASE_SEC,
    per_char_sec: float = _DEFAULT_PER_CHAR_SEC,
    min_sec: float = 0.0,
    max_sec: float = 12.0,
    arousal: Optional[float] = None,
    jitter: float = _DEFAULT_JITTER,
    rng: Optional[Callable[[float, float], float]] = None,
) -> float:
    """按回复内容估「真人思考+打字」延迟（纯函数）。

    模型：``base_sec + per_char_sec × 字数``，再乘 ``arousal`` 节奏缩放、加 ±jitter 抖动，
    最后夹到 ``[min_sec, max_sec]``。字数越多越久（长回复本就要打更久）；``arousal`` 高
    （兴奋/急切）打字更快、低（平静/安慰）更慢——取自**回复自身**的激活度（语义正确，
    非客户情绪）。``rng`` 可注入以确定性单测。``max_sec<=0`` → 返回 0（关闭）。
    """
    _rng = rng or random.uniform
    if max_sec <= 0 or max_sec < min_sec:
        return 0.0
    n = len(str(text or "").strip())
    raw = float(base_sec) + float(per_char_sec) * n
    raw *= _arousal_scale(arousal)
    if jitter and jitter > 0:
        raw *= _rng(1.0 - jitter, 1.0 + jitter)
    return max(float(min_sec), min(float(max_sec), raw))


def estimate_typing_lead(
    text: str,
    *,
    per_char_sec: float = _DEFAULT_PER_CHAR_SEC,
    latin_per_char_sec: Optional[float] = None,
    min_sec: float = 1.2,
    max_sec: float = 10.0,
) -> float:
    """估「可见打字」时长（发送前挂『正在输入』的那一段，纯函数）。

    与 ``estimate_thinking_delay``（总延迟=读+想+打字）不同，这里只估**打字**本身：
    真人的节奏是「已读 → 放着想（无输入状态）→ 临发前打字几秒 → 消息到」。把整段
    延迟全程挂『正在输入』（旧行为）等于宣称「我打了 45 秒字只打出 20 个字」——
    比不挂更假。长度经 ``reply_split.typing_time_sec``：默认 CJK 加权刻度
    （英文字符 0.25 权重＝旧行为锚）；``latin_per_char_sec`` 显式给出时英文按
    真实手速计（加权刻度把英文打字耗时低估 ~4 倍）。夹到 [min_sec, max_sec]
    （一条 bubble ≤ 数十字，10s 封顶足够）。
    """
    s = str(text or "").strip()
    try:
        from src.inbox.reply_split import typing_time_sec
        t = typing_time_sec(
            s, per_char_sec=max(0.0, float(per_char_sec)),
            latin_per_char_sec=latin_per_char_sec)
    except Exception:
        t = max(0.0, float(per_char_sec)) * float(len(s))
    lead = 0.6 + t
    return max(float(min_sec), min(float(max_sec), lead))


def resolve_typing_lead(
    block: Optional[Dict[str, Any]],
    *,
    text: str = "",
    persona_id: str = "",
    platform: str = "",
) -> float:
    """按延迟配置块解析本条文本的「可见打字」时长（人设>平台>全局覆写同 resolve_pacing）。

    ``per_char_sec`` 与思考延迟模型共用同一键（同一个人设同一手速——修「首条按
    0.08s/字、条间按 0.03s/字」的双手速裂缝）；解析失败回默认，绝不抛。
    """
    b = _apply_scoped_overrides(block, platform=platform, persona_id=persona_id)
    try:
        per_char = float(b.get("per_char_sec", _DEFAULT_PER_CHAR_SEC))
    except (TypeError, ValueError):
        per_char = _DEFAULT_PER_CHAR_SEC
    latin_raw = b.get("latin_per_char_sec")
    try:
        latin = float(latin_raw) if latin_raw is not None else None
    except (TypeError, ValueError):
        latin = None
    return estimate_typing_lead(
        text, per_char_sec=per_char, latin_per_char_sec=latin)


def _apply_persona_overrides(
    block: Optional[Dict[str, Any]], persona_id: str,
) -> Dict[str, Any]:
    """把 ``block.persona_overrides[persona_id]`` 合并到顶层节奏参数上（纯函数）。

    人设化节奏：不同人设可有不同打字速度/上限/是否自适应（急性子 ``per_char_sec`` 小、
    慢热型大）。覆盖放在延迟块内的 ``persona_overrides`` 映射——避免改人设 schema/跨文件
    耦合，运营在同一处即可见节奏全貌。无 persona_id / 无对应覆盖 → 原样返回顶层默认
    （零行为变更）。返回的 dict 已移除 ``persona_overrides`` 自身键（不参与后续解析）。
    """
    b = dict(block or {})
    ov = b.pop("persona_overrides", None)
    if persona_id and isinstance(ov, dict):
        pov = ov.get(str(persona_id))
        if isinstance(pov, dict):
            b.update({k: v for k, v in pov.items() if k != "persona_overrides"})
    return b


def _apply_scoped_overrides(
    block: Optional[Dict[str, Any]], *, platform: str = "", persona_id: str = "",
) -> Dict[str, Any]:
    """平台覆写 → 人设覆写 依序合并到顶层节奏参数（纯函数，P1 2026-08-03）。

    优先级 = **人设 > 平台 > 全局**：平台是渠道级兜底（Messenger 网页链路慢就
    整体放缓），人设是「这个人怎么说话」（急性子在哪个平台都急），后应用者胜。
    两张覆写表都是**键级**合并——平台只给 max_sec 时 min_sec 仍取全局；
    unknown 平台/人设 → 原样（零行为变更）。返回 dict 已剥除两张覆写表自身键。
    """
    b = dict(block or {})
    pov = b.pop("platform_overrides", None)
    if platform and isinstance(pov, dict):
        entry = pov.get(str(platform).lower())
        if isinstance(entry, dict):
            b.update({k: v for k, v in entry.items()
                      if k not in ("platform_overrides", "persona_overrides")})
    return _apply_persona_overrides(b, persona_id)


def _derive_arousal(text: str) -> Optional[float]:
    """从回复文本估激活度 arousal（best-effort，语义正确：取自**回复自身**）。

    懒依赖 ``analyze_emotion``——仅在此装配点用一次，保持 estimate_thinking_delay 纯净。
    任何异常/不可用 → None（不缩放）。
    """
    t = str(text or "").strip()
    if not t:
        return None
    try:
        from src.utils.emotional_context import analyze_emotion
        emo = analyze_emotion(t) or {}
        a = emo.get("arousal")
        return float(a) if a is not None else None
    except Exception:
        return None


@dataclass
class PacingResult:
    """一次延迟解析的结果 + 诊断（供观测：目标 vs 实际 vs 已耗时 vs 激活度）。"""
    delay: float          # 本次**还需等待**的秒数（最终值）
    target: float         # 估出的目标思考时长（未扣已耗时；非自适应=随机值）
    elapsed: float        # 扣除的已耗时（秒）
    arousal: Optional[float]  # 回复激活度（自适应时用；None=未知/不适用）
    adaptive: bool        # 是否自适应模式
    enabled: bool         # 该延迟配置是否启用（max_sec>0）
    floored: bool = False  # min_residual_sec 残余下限是否兜住了本次（观测用）
    # O-1 D（D-O4）「拟人程度」三档（profile 在场时才有值；legacy 模型全 0 / None）
    profile: str = ""      # natural / fast / slow；"" = legacy（min/max/adaptive 旧模型）
    read_sec: float = 0.0  # 读消息分量（含按入站字数加成）
    think_sec: float = 0.0  # 思考分量
    type_sec: float = 0.0  # 打字分量（= 可见「正在输入」时长）
    stop_sec: float = 0.0  # 发出前停止 composing 的静默秒数
    typing_lead: Optional[float] = None  # profile 模型给出的可见打字时长（None=沿用 resolve_typing_lead）


# ── O-1 D（#253 #254 · D-O4，2026-09-08）「拟人程度」三档：读 / 想 / 打字 分量模型 ──────
# FW78ZP 骨架：读延迟 3–6s + 每 10 词 +1s（上限 20s）±30%；思考 3–8s；打字 英文 35–45 wpm /
# 中日韩 60–90 字/分 ±30%（上限 90s）；发出前 1s 停 composing。事故：起草→发出恒 12–13s 与
# 长度无关——旧模型 base 1 + 0.08×字数 对 60 字英文只有 5.8s，被 min_sec=8 夹成常数。
# **只在延迟块显式带 ``profile`` 时启用**（legacy 块行为逐字节不变，存量部署 / 既有测试零变化）；
# 出厂基线换 ``profile: natural`` 由 O-2 / N-5 的 config 基线带（见落点表）。
PACING_PROFILES: Dict[str, Dict[str, float]] = {
    "natural": dict(read_min=3.0, read_max=6.0, read_per_10_words=1.0, read_cap=20.0,
                    think_min=3.0, think_max=8.0,
                    wpm_min=35.0, wpm_max=45.0, cpm_min=60.0, cpm_max=90.0, type_cap=90.0,
                    jitter=0.3, stop_before_send=1.0, min_gap_sec=8.0, min_residual_sec=2.0),
    "fast": dict(read_min=1.5, read_max=3.5, read_per_10_words=0.6, read_cap=10.0,
                 think_min=1.0, think_max=4.0,
                 wpm_min=50.0, wpm_max=65.0, cpm_min=95.0, cpm_max=130.0, type_cap=45.0,
                 jitter=0.3, stop_before_send=0.5, min_gap_sec=4.0, min_residual_sec=1.5),
    "slow": dict(read_min=5.0, read_max=10.0, read_per_10_words=1.5, read_cap=30.0,
                 think_min=6.0, think_max=14.0,
                 wpm_min=25.0, wpm_max=35.0, cpm_min=45.0, cpm_max=70.0, type_cap=120.0,
                 jitter=0.3, stop_before_send=1.5, min_gap_sec=12.0, min_residual_sec=3.0),
}
PACING_PROFILE_NAMES = tuple(PACING_PROFILES.keys())
# 渠道级档位参数缺省（键级合并在档位之上、显式块键 / platform_overrides 之下）。
# wechat（PC 副驾）：稿子到驱动还要 3s 轮询 + 开会话/粘贴/点发送/等回显 ≈ 5–10s 才落地，且微信对端看不到「正在输入」，
# 打字分量只是空等——读/想/打字都收短，避免和驱动侧延迟叠成 40–60s 才回（2026-09-21）。
PACING_PLATFORM_DEFAULTS: Dict[str, Dict[str, float]] = {
    "wechat": dict(read_min=1.5, read_max=4.0, read_cap=10.0, think_min=1.5, think_max=5.0,
                   cpm_min=90.0, cpm_max=130.0, wpm_min=50.0, wpm_max=65.0, type_cap=30.0),
}
_CJK_TEXT_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


@dataclass
class HumanPacing:
    read: float
    think: float
    type_: float
    stop: float
    total: float          # read + think + type_（stop 含在 type_ 尾段，不另加）
    words: int            # 入站词数（读分量依据）
    reply_units: int      # 出站计量单位数（拉丁=词 / CJK=字）
    cjk: bool


def resolve_profile(block: Optional[Dict[str, Any]]) -> str:
    """延迟块的「拟人程度」档位名；未设 / ``custom`` / 未知 → ""（legacy 模型）。"""
    try:
        p = str((block or {}).get("profile") or "").strip().lower()
    except Exception:
        return ""
    return p if p in PACING_PROFILES else ""


def _count_words(text: str) -> int:
    s = str(text or "").strip()
    if not s:
        return 0
    if _CJK_TEXT_RE.search(s):
        # 中日韩按 2 字≈1 词折算（读速口径）
        cjk = len(_CJK_TEXT_RE.findall(s))
        latin = len([w for w in re.split(r"\s+", _CJK_TEXT_RE.sub(" ", s)) if w])
        return max(1, cjk // 2 + latin)
    return len([w for w in s.split() if w])


count_words = _count_words   # 公开名（worker 日志 words= 与读分量同口径）


def estimate_human_pacing(
    reply_text: str,
    *,
    inbound_text: str = "",
    params: Optional[Dict[str, float]] = None,
    rng: Optional[Callable[[float, float], float]] = None,
) -> HumanPacing:
    """按三档参数估「读 → 想 → 打字」三段（纯函数，rng 可注入）。

    读 = uniform(read_min, read_max) + 入站词数/10 × read_per_10_words，夹 read_cap，×(1±jitter)；
    想 = uniform(think_min, think_max) ×(1±jitter)；
    打字 = 出站长度 / 手速：拉丁 词数/uniform(wpm) 分、CJK 字数/uniform(cpm) 分，×(1±jitter)，
           夹 [stop_before_send + 0.6, type_cap]（打字段至少能容下「停 composing」那一秒）。
    """
    p = dict(PACING_PROFILES["natural"])
    if isinstance(params, dict):
        p.update({k: float(v) for k, v in params.items() if isinstance(v, (int, float))})
    _rng = rng or random.uniform
    jit = max(0.0, min(0.9, float(p.get("jitter", 0.3))))

    def _j(x: float) -> float:
        return x * _rng(1.0 - jit, 1.0 + jit) if jit > 0 else x

    words_in = _count_words(inbound_text)
    read = _rng(p["read_min"], p["read_max"]) + (words_in / 10.0) * p["read_per_10_words"]
    read = min(_j(read), p["read_cap"])          # cap 在抖动之后夹（cap 是硬上限）
    think = _j(_rng(p["think_min"], p["think_max"]))
    reply = str(reply_text or "").strip()
    cjk = bool(_CJK_TEXT_RE.search(reply))
    if cjk:
        units = len(_CJK_TEXT_RE.findall(reply)) + len(
            [w for w in re.split(r"\s+", _CJK_TEXT_RE.sub(" ", reply)) if w])
        speed = _rng(p["cpm_min"], p["cpm_max"])           # 字/分
    else:
        units = len([w for w in reply.split() if w])
        speed = _rng(p["wpm_min"], p["wpm_max"])           # 词/分
    stop = max(0.0, float(p.get("stop_before_send", 0.0)))
    type_raw = (units / max(1.0, speed)) * 60.0 if units else 0.0
    type_ = min(float(p.get("type_cap", 90.0)), max(stop + 0.6, _j(type_raw)))
    total = read + think + type_
    return HumanPacing(read=read, think=think, type_=type_, stop=stop, total=total,
                       words=words_in, reply_units=units, cjk=cjk)


def resolve_following_delay_block(
    own: Optional[Dict[str, Any]], slider: Optional[Dict[str, Any]],
) -> "tuple[Dict[str, Any], bool]":
    """「未独立配值则跟随滑杆」的**单一**判定（三条自动回复链共用，2026-08-07 收口）。

    A 线原生回复（``telegram.reply_humanize.thinking_delay``）与协议 7×24 直发链
    （``protocol_autoreply.delay``）各有独立节奏键；**未实际配值**（``max_sec<=0``）
    且未显式 ``follow: false`` 时，跟随设置页「回复节奏」滑杆
    ``inbox.l2_autosend.deliver_delay``（＝B 线本键）。此判定此前在三处各写一份
    （``sender.run_prereply_humanize`` / ``protocol_autoreply.resolve_send_pacing_block``
    / ``reply_pacing_settings.chain_pacing_coverage``）——改 follow 规则会三处漂移，
    是「设了不生效/漏一条链」类事故的温床，故收口为此函数（单一事实源）。

    判据刻意用「``max_sec<=0`` 视为没配」而非「块是否存在」：出厂基准就是显式 0/0
    块，按「块存在」判定会让跟随永远走不到。返回 ``(block, following)``：
    - ``own`` 实际配了值 → ``(dict(own), False)``（显式覆写优先，存量部署零变化）；
    - ``own`` 空/零且 ``follow≠false`` → ``(dict(slider), True)``（跟随滑杆）；
    - ``own.follow is False`` → ``(dict(own), False)``（保留「本链秒回」逃生阀，
      ``own`` 通常是空块＝resolve_pacing 解析为 0 延迟）。
    ``following`` 供 coverage 标注来源（跟随滑杆 vs 独立配置），避免各处重新推导。
    """
    o = own if isinstance(own, dict) else {}

    def _off(b: Any) -> bool:
        try:
            return float((b or {}).get("max_sec", 0) or 0) <= 0
        except (TypeError, ValueError):
            return True

    if bool(o.get("follow", True)) and _off(o):
        return dict(slider if isinstance(slider, dict) else {}), True
    return dict(o), False


def resolve_pacing(
    block: Optional[Dict[str, Any]],
    *,
    text: str = "",
    arousal: Optional[float] = None,
    elapsed_sec: float = 0.0,
    persona_id: str = "",
    platform: str = "",
    rng: Optional[Callable[[float, float], float]] = None,
    inbound_text: str = "",
) -> PacingResult:
    """解析延迟配置为 ``PacingResult``（含诊断字段，供观测打点）。

    **O-1 D 三档模型**：块带 ``profile ∈ {natural, fast, slow}`` → 走 ``estimate_human_pacing``
    （读 / 想 / 打字分量；``inbound_text`` 供读分量按入站字数加成），忽略 min/max/adaptive；
    已耗时照扣、``min_residual_sec`` 按档位缺省（块显式值优先）；``typing_lead`` = 打字分量、
    ``stop_sec`` = 发出前停 composing。未带 profile → 下面的 legacy 语义逐字节不变。

    语义同 ``compute_pacing_delay``（后者是本函数 ``.delay`` 的薄封装，向后兼容）：
    - 先合并覆写层：``platform_overrides[platform]`` → ``persona_overrides[persona_id]``
      （人设 > 平台 > 全局，键级合并；见 ``_apply_scoped_overrides``）。
    - ``max_sec<=0``/非法 → enabled=False，delay=0。
    - ``adaptive=false`` → ``uniform(min,max)``（不扣 elapsed）。
    - ``adaptive=true`` → 按长度 + 回复激活度估目标，再扣已耗时 ``max(0, 目标-已耗)``；
      ``arousal`` 未给则自动从 ``text`` 估（回复自身激活度，语义正确）。
    - ``min_residual_sec``（P1，2026-08-12）：adaptive 抵扣**发生后**（elapsed>0）
      至少保留的残余延迟。修「生成/排队耗时 ≥ 目标 → 抵扣归零 → 秒回且无打字
      气泡」：残余 ≥ 该值时打字气泡也有露出窗口（配 ≥ DEFAULT_MIN_TYPING_DELAY_SEC
      才会真挂）。默认 0＝旧行为；只作用于 adaptive 分支（非自适应从不抵扣，无此洞）。
    """
    b = _apply_scoped_overrides(block, platform=platform, persona_id=persona_id)
    _rng = rng or random.uniform
    prof = resolve_profile(b)
    if prof:
        params = dict(PACING_PROFILES[prof])
        params.update(PACING_PLATFORM_DEFAULTS.get(str(platform or "").lower(), {}))
        for k in list(params.keys()):
            if k in b and isinstance(b.get(k), (int, float)) and not isinstance(b.get(k), bool):
                params[k] = float(b[k])          # 专家显式覆写单个分量参数
        hp = estimate_human_pacing(text, inbound_text=inbound_text, params=params, rng=_rng)
        if arousal is None:
            arousal = _derive_arousal(text)
        try:
            el = max(0.0, float(elapsed_sec or 0.0))
        except (TypeError, ValueError):
            el = 0.0
        residual = float(params.get("min_residual_sec", 0.0) or 0.0)
        raw = max(0.0, hp.total - el)
        # 扣掉已耗时后仍至少留「打字段」（可见打字不能被生成耗时吃光——那就是秒回）
        delay = max(residual, min(hp.total, max(raw, hp.type_ if el > 0 else raw)))
        return PacingResult(delay, hp.total, el, arousal, True, True,
                            floored=(el > 0 and raw < delay),
                            profile=prof, read_sec=hp.read, think_sec=hp.think,
                            type_sec=hp.type_, stop_sec=hp.stop,
                            typing_lead=min(hp.type_, delay))
    try:
        min_sec = float(b.get("min_sec", 0) or 0)
        max_sec = float(b.get("max_sec", 0) or 0)
    except (TypeError, ValueError):
        return PacingResult(0.0, 0.0, 0.0, None, bool(b.get("adaptive", False)), False)
    adaptive = bool(b.get("adaptive", False))
    if max_sec <= 0 or max_sec < min_sec:
        return PacingResult(0.0, 0.0, 0.0, None, adaptive, False)
    if not adaptive:
        d = _rng(min_sec, max_sec)
        return PacingResult(d, d, 0.0, None, False, True)
    try:
        base = float(b.get("base_sec", _DEFAULT_BASE_SEC))
        per_char = float(b.get("per_char_sec", _DEFAULT_PER_CHAR_SEC))
        jitter = float(b.get("jitter", _DEFAULT_JITTER))
    except (TypeError, ValueError):
        base, per_char, jitter = _DEFAULT_BASE_SEC, _DEFAULT_PER_CHAR_SEC, _DEFAULT_JITTER
    if arousal is None:
        arousal = _derive_arousal(text)
    target = estimate_thinking_delay(
        text, base_sec=base, per_char_sec=per_char,
        min_sec=min_sec, max_sec=max_sec, arousal=arousal,
        jitter=jitter, rng=_rng)
    try:
        el = max(0.0, float(elapsed_sec or 0.0))
    except (TypeError, ValueError):
        el = 0.0
    try:
        residual = max(0.0, float(b.get("min_residual_sec", 0) or 0))
    except (TypeError, ValueError):
        residual = 0.0
    if el > 0:
        raw = max(0.0, target - el)
        delay = max(residual, raw)
        return PacingResult(delay, target, el, arousal, True, True,
                            floored=residual > 0 and raw < residual)
    return PacingResult(target, target, el, arousal, True, True)


def compute_pacing_delay(
    block: Optional[Dict[str, Any]],
    *,
    text: str = "",
    arousal: Optional[float] = None,
    elapsed_sec: float = 0.0,
    persona_id: str = "",
    platform: str = "",
    rng: Optional[Callable[[float, float], float]] = None,
) -> float:
    """把一段延迟配置解析为本次**还需等待**的秒数（``resolve_pacing(...).delay`` 的薄封装）。

    单一装配点：autosend 与原生 A 线回复共用。诊断字段（目标/已耗/激活度）见 resolve_pacing。
    """
    return resolve_pacing(
        block, text=text, arousal=arousal, elapsed_sec=elapsed_sec,
        persona_id=persona_id, platform=platform, rng=rng).delay


def resolve_min_gap_sec(
    block: Optional[Dict[str, Any]],
    *,
    platform: str = "",
    persona_id: str = "",
) -> float:
    """解析「同会话连发最小间隔」秒数（``min_gap_sec``，覆写层级同 resolve_pacing）。

    与延迟块同居一个配置块（``deliver_delay.min_gap_sec``）＝随
    ``apply_deliver_delay`` 热更、随「跟随滑杆」语义被 A 线/协议链整块继承。
    解析失败/未配置 → 0（关闭，旧行为）。
    """
    b = _apply_scoped_overrides(block, platform=platform, persona_id=persona_id)
    try:
        v = b.get("min_gap_sec")
        if (v is None or v == "") and resolve_profile(b):
            # O-1 D：三档自带连发间隔缺省（块显式值优先）
            return float(PACING_PROFILES[resolve_profile(b)].get("min_gap_sec", 0.0))
        return max(0.0, float(v or 0))
    except (TypeError, ValueError):
        return 0.0


# ── O-1 D（D-O4）首次接触 / 沉寂 >6h 的首回延 1–5 min ─────────────────────────────
# 真人不会在陌生人第一条消息 10 秒内、或对方消失一天后又 10 秒内回。配置块
# ``deliver_delay.first_reply {enabled, silence_hours, min_sec, max_sec}`` 与延迟块同住
# （随 apply_deliver_delay 热更、随人设 / 平台覆写层继承）；``enabled`` 缺省 ＝
# 「延迟块带 profile」（legacy 块零变化，显式 true/false 优先）。
# 实现方式是 worker 侧「留 pending 不处置」（与班表闸同款），不是在拟人序列里睡 5 分钟
# ——串行投递下那会堵住其他会话；hold 按 (会话, 沉寂后首条入站 ts) 确定性取值，
# 同一轮连发的多条草稿同一时刻放行，且 tick 间重算不抖。
FIRST_REPLY_DEFAULTS: Dict[str, float] = dict(silence_hours=6.0, min_sec=60.0, max_sec=300.0)
# 渠道级出厂缺省（显式 ``first_reply.*`` / ``platform_overrides.<plat>.first_reply.*`` 优先）。
# wechat（PC 副驾·全自动档才走 autosend）：驱动 3s 轮询 + 五步守卫 + 拟人投递 + 同联系人间隔
# 本身已是分钟级链路，再叠 1–5 min 首回 hold 就是主人眼里的「全自动半天不回」→ 极速档
# （5–20s：仍不秒回，但不再「晾」）。其它渠道零变化。
FIRST_REPLY_PLATFORM_DEFAULTS: Dict[str, Dict[str, float]] = {
    "wechat": dict(min_sec=5.0, max_sec=20.0),
}


def resolve_first_reply_cfg(
    block: Optional[Dict[str, Any]], *, platform: str = "", persona_id: str = "",
) -> Dict[str, Any]:
    """``deliver_delay.first_reply`` 归一化；返回 ``enabled / silence_hours / min_sec / max_sec``。"""
    b = _apply_scoped_overrides(block, platform=platform, persona_id=persona_id)
    node = b.get("first_reply") if isinstance(b.get("first_reply"), dict) else {}
    out: Dict[str, Any] = dict(FIRST_REPLY_DEFAULTS)
    out.update(FIRST_REPLY_PLATFORM_DEFAULTS.get(str(platform or "").lower(), {}))
    for k in ("silence_hours", "min_sec", "max_sec"):
        try:
            if node.get(k) is not None:
                out[k] = max(0.0, float(node[k]))
        except (TypeError, ValueError):
            pass
    if out["max_sec"] < out["min_sec"]:
        out["max_sec"] = out["min_sec"]
    en = node.get("enabled")
    out["enabled"] = bool(resolve_profile(b)) if en is None else bool(en)
    return out


def silence_before_inbound(
    rows: Optional[list], *, draft_ts: float,
) -> "tuple[Optional[float], float]":
    """从会话消息行推「这轮入站之前沉寂了多久」。

    返回 ``(silence_sec, burst_start_ts)``：
    - ``silence_sec=None`` ＝ 首次接触（这轮入站之前本会话没有任何出站）；
    - 否则 ＝ 这轮入站的**首条**与其之前最后一条出站的间隔（客户连发算一轮，
      不会因为「上一条是 5 秒前客户自己发的」把沉寂算成 5 秒）；
    - ``burst_start_ts`` ＝ 这轮入站首条的 ts（hold 锚点）；无入站行时取 draft_ts。
    ``rows`` 期望 ``list_recent_messages`` 的 ts 升序输出；坏输入按「无沉寂」（0, draft_ts）
    返回——判不出就不延，宁快勿静默。
    """
    try:
        dts = float(draft_ts or 0.0)
    except (TypeError, ValueError):
        dts = 0.0
    if not isinstance(rows, list) or not rows:
        return 0.0, dts
    ordered = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            ts = float(r.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        d = str(r.get("direction") or "")
        if d not in ("in", "out"):
            continue
        if dts > 0 and ts > dts + 1.0:
            continue          # 草稿之后才到的行不属于这一轮
        ordered.append((ts, d))
    ordered.sort()
    # 从尾部回溯：先跳过本轮入站，遇到第一条出站即为沉寂起点
    burst_start = dts
    i = len(ordered) - 1
    while i >= 0 and ordered[i][1] == "in":
        burst_start = ordered[i][0]
        i -= 1
    if burst_start == dts and i == len(ordered) - 1:
        return 0.0, dts       # 最后一条是出站（本稿不是在回入站）→ 不延
    if i < 0:
        return None, burst_start
    return max(0.0, burst_start - ordered[i][0]), burst_start


def first_reply_hold_sec(
    cfg: Dict[str, Any], *, silence_sec: Optional[float], key: str,
) -> float:
    """按配置判这轮首回该加多少秒 hold（0 ＝ 不延）。确定性：同 key 同值。"""
    if not cfg or not cfg.get("enabled"):
        return 0.0
    try:
        hours = float(cfg.get("silence_hours", FIRST_REPLY_DEFAULTS["silence_hours"]))
        lo = float(cfg.get("min_sec", FIRST_REPLY_DEFAULTS["min_sec"]))
        hi = float(cfg.get("max_sec", FIRST_REPLY_DEFAULTS["max_sec"]))
    except (TypeError, ValueError):
        return 0.0
    if hi <= 0:
        return 0.0
    if silence_sec is not None and silence_sec < hours * 3600.0:
        return 0.0
    from binascii import crc32
    span = max(0.0, hi - lo)
    frac = (crc32(str(key).encode("utf-8")) % 10007) / 10007.0
    return lo + span * frac


def apply_min_gap_floor(
    delay: float,
    *,
    since_last_send_sec: Optional[float],
    min_gap_sec: float,
    rng: Optional[Callable[[float, float], float]] = None,
) -> "tuple[float, bool]":
    """同会话连发最小间隔地板（P1，2026-08-12，纯函数）。返回 ``(delay, floored)``。

    修的洞（198 实录「第 2/3 条秒回」的机制）：B 线草稿在队列里排队，adaptive 把
    「排队等待」当已耗时全额抵扣 → 第 2 条起延迟恒 0，对同一客户背靠背秒回。
    抵扣语义对**单条**是对的（客户确实已经等了那么久），但真人**连续两条出站**
    之间必有打字间隔——本地板只看「距本会话上一条出站多久」，与抵扣正交：
    ``延迟 = max(原延迟, 目标间隔 - 距上次出站秒数)``。

    - ``since_last_send_sec=None``（本会话进程内首条出站）→ 不垫（首条的节奏由
      delay 本身负责，地板只管「连发」）；
    - ``min_gap_sec<=0`` → 关闭（默认，旧行为）；
    - 目标间隔带 ±15% 抖动（``rng`` 可注入）——地板经常被踩到时（客户连发轰炸），
      恒定 N 秒一条是节拍器，不是人。
    """
    try:
        base = max(0.0, float(delay or 0.0))
    except (TypeError, ValueError):
        base = 0.0
    try:
        gap = float(min_gap_sec or 0.0)
    except (TypeError, ValueError):
        gap = 0.0
    if gap <= 0 or since_last_send_sec is None:
        return base, False
    try:
        since = max(0.0, float(since_last_send_sec))
    except (TypeError, ValueError):
        return base, False
    _rng = rng or random.uniform
    required = gap * _rng(0.85, 1.15) - since
    if required > base:
        return required, True
    return base, False


MarkReadFn = Callable[[], Awaitable[Any]]
TypingFn = Callable[[str], Awaitable[Any]]
SleepFn = Callable[[float], Awaitable[Any]]


async def run_presend_humanization(
    *,
    delay: float = 0.0,
    action: str = "typing",
    mark_read: Optional[MarkReadFn] = None,
    typing: Optional[TypingFn] = None,
    sleep: SleepFn,
    refresh_sec: float = DEFAULT_TYPING_REFRESH_SEC,
    min_typing_delay: float = DEFAULT_MIN_TYPING_DELAY_SEC,
    typing_lead_sec: Optional[float] = None,
    on_marked: Optional[Callable[[], None]] = None,
    on_typing: Optional[Callable[[], None]] = None,
    stop_before_send_sec: float = 0.0,
) -> None:
    """执行「已读 → （静默思考）→ 打字续挂 → （停 composing）→ 延迟」拟人序列。

    ``stop_before_send_sec``（O-1 D）：打字段末尾留这么多秒**不再续挂**「正在输入」——真人按
    发送前气泡会先消失一下；只在打字段长于它时生效，否则忽略。

    顺序：
      1. 若给了 ``mark_read`` → 调用一次（先看）。
      2. ``typing_lead_sec`` 非 None → **两段式**：前段静默睡（真人在想/在忙，
         无输入状态），只有最后 ``typing_lead_sec`` 秒挂打字（临发前才打字）；
         None → 旧行为，全程打字续挂（向后兼容）。
      3. 打字段分片等待，每片先挂一次 ``typing``（若给了）再睡
         ``min(refresh_sec, 剩余)``，续挂到延迟耗尽（打字气泡不断续）。
      4. ``delay <= 0`` → 不挂打字（避免气泡闪一下消息就到，反而不真实）。
      5. 打字段 < min_typing_delay → 只静默睡完，不挂打字（气泡一闪即逝更假；
         如自适应扣除已耗时后只剩零点几秒）。

    绝不抛：mark_read / typing 异常仅记 debug 并继续；``sleep`` 由调用方保证可靠
    （测试注入即时返回的假 sleep）。
    """
    if mark_read is not None:
        try:
            await mark_read()
            if on_marked is not None:
                on_marked()
        except Exception:
            logger.debug("[humanize] mark_read 失败", exc_info=True)

    remaining = max(0.0, float(delay))
    if remaining <= 0:
        return
    # 两段式：先静默「想」，只留尾部 typing_lead_sec 进打字段。
    if typing_lead_sec is not None:
        try:
            lead = max(0.0, float(typing_lead_sec))
        except (TypeError, ValueError):
            lead = remaining
        silent = max(0.0, remaining - lead)
        if silent > 0:
            await sleep(silent)
            remaining -= silent
        if remaining <= 0:
            return
    # 超短延迟护栏：低于阈值只静默等，不挂打字（避免气泡一闪即逝的机械感）。
    if remaining < max(0.0, float(min_typing_delay)):
        await sleep(remaining)
        return
    try:
        stop_tail = max(0.0, float(stop_before_send_sec or 0.0))
    except (TypeError, ValueError):
        stop_tail = 0.0
    if stop_tail > 0 and remaining > stop_tail + 0.5:
        remaining -= stop_tail
    else:
        stop_tail = 0.0
    step_max = max(0.5, float(refresh_sec))
    while remaining > 0:
        if typing is not None:
            try:
                await typing(action)
                if on_typing is not None:
                    on_typing()
            except Exception:
                logger.debug("[humanize] typing 失败", exc_info=True)
        step = min(step_max, remaining)
        await sleep(step)
        remaining -= step
    if stop_tail > 0:
        await sleep(stop_tail)   # composing 已停，静默这一段再发（真人按发送前气泡先消失）


__all__ = [
    "run_presend_humanization",
    "estimate_thinking_delay",
    "estimate_typing_lead",
    "resolve_typing_lead",
    "compute_pacing_delay",
    "resolve_pacing",
    "resolve_following_delay_block",
    "PacingResult",
    "DEFAULT_TYPING_REFRESH_SEC",
    "DEFAULT_MIN_TYPING_DELAY_SEC",
    # O-1 D 三档
    "PACING_PROFILES", "PACING_PROFILE_NAMES", "HumanPacing",
    "resolve_profile", "estimate_human_pacing", "count_words", "PACING_PLATFORM_DEFAULTS",
    "FIRST_REPLY_DEFAULTS", "FIRST_REPLY_PLATFORM_DEFAULTS", "resolve_first_reply_cfg",
    "silence_before_inbound", "first_reply_hold_sec",
]
