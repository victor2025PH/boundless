"""LLM 口语化改写（活人感 A 档，2026-07-14）——本地小模型深度口语化，规则档兜底。

背景（语音活人感诊断的最大单点杠杆）：规则档 ``voice_colloquial.colloquialize`` 零延迟、
确定性，但只做「书面词替换 + 句首迟疑词」，一段话中间仍是书面结构。LLM 档用 LAN 本地
模型（``ai.fallback`` 的 qwen3:30b，**不占云成本**）做**语境贴合**的口语化——拆生硬长句、
自然口语连接、语气词落在该落的地方，更接近「真人在说」。

把「非确定性 LLM + 有延迟」驯服成生产可用的四道工程护栏：
  1. **缓存**（进程级 LRU，键=原文+情绪+lead+style）：非确定性结果缓存化 → 同原文永远
     同口语版（对 TTS 缓存/预渲染键友好）+ 省重复调用。
  2. **端点熔断**：连续失败 ≥N 次进冷却期，冷却期直接回落（不让挂掉的端点每条拖满超时）。
  3. **输出消毒 + 校验**：剥元话语前缀/引号；长度[0.3x,1.8x]、语言（中文）、非空校验
     不过 → 判为异常回落（防 LLM 发挥过度/截断/输出解释/串语言）。
  4. **失败即回落**：任何异常/超时/校验不过 → ``None``，调用方回落规则档（绝不阻塞语音）。

短句 / 非中文 no-op 与规则档同口径（保预渲染命中 + 防中文口语词 garble 外语）。
``async``（在 TTS 合成 async 上下文调用）；``ai_client`` 可注入（测试）。

可单测纯函数：build_colloquial_prompt / sanitize_llm_output / _cache_key。
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── 情绪 → 语气词（喂给 LLM 的语气基调）─────────────────────────────────────
_EMOTION_TONE = {
    "neutral": "自然放松", "warm": "温暖亲切", "happy": "开心愉快",
    "playful": "俏皮活泼", "excited": "兴奋雀跃", "empathetic": "温柔共情",
    "apologetic": "诚恳", "calm": "平静舒缓", "sad": "低落轻声", "serious": "认真",
}


# ── 口语重塑力度（AI Live OS Agent6，2026-07-24）───────────────────────────────
# rewrite_intensity 三档，力度渐进但**事实红线三条恒定**（不改值/不编造/不串语言）：
#   light  = 仅换词 + 拆句（最保守，即使 lead=True 也不加句首连接；事实零风险）
#   natural= + 句首口语连接 + 语气词（当前生产默认，≈ 旧行为）
#   vivid  = + 主观口吻框架（「说真的/我跟你讲」）+ 停顿感（主播味/陪伴感最强）
_INTENSITY = ("light", "natural", "vivid")


def build_colloquial_prompt(emotion: str = "neutral", lead: bool = True,
                            style: str = "", disfluency: bool = False,
                            intensity: str = "natural") -> str:
    """构建口语化系统提示（纯函数）。

    ``intensity``＝口语重塑力度 light|natural|vivid（AI Live OS Agent6，2026-07-24）：
    档位越高允许的主观口吻/停顿越多，但**事实红线恒定**（数字/时间/人名/金额/承诺不改、
    不编造、不串语言）。``lead``＝是否允许句首口语连接（分条非首条关）；
    ``disfluency``＝允许一处轻口误自纠（⑥，调用方按文本 crc 低频开启）。
    """
    lvl = str(intensity or "natural").strip().lower()
    if lvl not in _INTENSITY:
        lvl = "natural"
    tone = _EMOTION_TONE.get(str(emotion or "").strip().lower(), "自然放松")
    if lvl == "vivid":
        # 陪伴态口语重塑：像真人随口说，而不是念稿——红线不变，放开主观口吻/停顿。
        parts = [
            "你是「把 AI 写的回复变成真人会怎么说」的口语重塑助手。"
            "目标：听起来像一个真实的人在微信语音里随口说，而不是念稿。",
            "红线（违反即失败）：",
            "1. 事实不可变——数字、时间、日期、金额、人名、地名、任何承诺，值和含义一律原样保留；",
            "2. 不编造——不得加入原文没有的具体事实（可加语气/口头禅/口语连接，但不加「信息」）；",
            "3. 同一种语言，不串语言。",
            "重塑方式：",
            "- 拆开生硬长句，用日常口语词（因此→所以、是否→是不是、非常→特别、无需→不用）；",
            "- 陪伴对话把「您」改成「你」（「您好」除外），去掉客服腔/播音腔；",
            "- 允许加一点点主观口吻框架（「说真的」「我跟你讲」「其实我觉得」），但别每句都加；",
            f"- 语气{tone}；",
            "- 可以有轻微语气词和自然停顿感（用省略号……表示换气），像真的在想着说；",
            "- 宁可碎一点、短一点，也不要一整段像念稿；",
            "- 真人微特征（每条最多选一种，别堆；没有把握就不用）：",
            "  · 思考时轻轻重复一个词（「我觉得……我觉得可以」）；",
            "  · 开心时不要写「哈哈/哈哈哈/嘿嘿嘿」开场——TTS 念出来极假；"
            "最多偶尔一个「嘿，」，否则靠语气本身表达开心；",
        ]
        if str(style or "").strip():
            parts.append(f"- 说话风格：{str(style).strip()}；")
        if disfluency:
            parts.append(
                "- 本条允许一处很轻的自然口误自纠（如「明天…啊不对，后天」），"
                "优先于上面的微特征，随意不刻意；")
        parts.append("只输出重塑后的那段话本身，不要引号、不要解释、不要任何前后缀。")
        return "\n".join(parts)
    # light / natural：保守档（事实零风险，力度渐进）
    parts = [
        "你是口语改写助手。把用户给的一句话改写成【适合用语音说出来】的口语版本。",
        "严格要求：",
        "1. 保持原意和所有信息不变，绝对不能增加新信息、遗漏或篡改信息——"
        "数字、时间、日期、金额、人名、地名等关键信息必须原样保留（可用中文数字念法，但值不能变）；",
        "2. 把书面表达换成日常口语（如「因此」→「所以」、「是否」→「是不是」），拆开生硬的长句；",
        f"3. 语气{tone}；",
    ]
    if lvl == "natural" and lead:
        parts.append("4. 可以在开头加一点点自然的口语连接（如「其实」「话说」），但别每句都加；")
    else:
        parts.append("4. 直接说正文，不要用语气词或开场白开头；")
    if str(style or "").strip():
        parts.append(f"5. 说话风格：{str(style).strip()}；")
    if disfluency:
        parts.append("6. 可以带至多一处很轻的自然口误自纠（如「明天…啊不对，后天」），要随意不刻意；")
    parts.append("只输出改写后的那一句话本身，不要加引号、不要解释、不要任何前后缀。")
    return "\n".join(parts)


# ── 输出消毒 / 校验（纯函数）─────────────────────────────────────────────────
_PREFIX_RE = re.compile(
    r"^\s*(口语版|口语|改写后?|结果|输出|回答|答案|译文)\s*[:：]\s*")
# 元话语标记：LLM 若加了解释段，从这些标记处截断（只保留改写正文）
_META_SPLIT_RE = re.compile(r"(?:\n\s*\n|解释[:：]|说明[:：]|注[:：]|原文[:：]|---)")
_QUOTE_CHARS = "「」『』“”\"'‘’"


_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
# 事实锚点：≥2 位数字 与 ≥2 字母的拉丁 token（金额/型号/品牌/套餐名）。
# 单位数/单字母噪声大（「一」「3点」的 3 常被合法改写成汉字）故不入锚点集。
_ANCHOR_RE = re.compile(r"\d[\d,.]*\d|[A-Za-z]{2,}")


def _anchor_set(text: str) -> set:
    """文本里的事实锚点（数字串归一去掉千分位/尾点，拉丁统一小写）。"""
    out = set()
    for tok in _ANCHOR_RE.findall(str(text or "")):
        t = tok.strip().lower().rstrip(".")
        if t and t[0].isdigit():
            t = t.replace(",", "").replace("，", "")
        if len(t) >= 2:
            out.add(t)
    return out


def lost_anchors(original: str, rewritten: str) -> list:
    """原文有、改写里没了的事实锚点（排序返回；空=红线未破）。

    口语化的书面契约里第一条红线就是「数字/金额/型号原样保留」，但此前**从未校验**
    ——2026-07-28 实录：本地模型把「团队版198美金」改写成「168美金」这类篡改，
    只要长度落在 0.6~1.8 倍就一路放行。锚点是确定性的、零误伤的那一半。
    """
    o = _anchor_set(original)
    if not o:
        return []
    return sorted(o - _anchor_set(rewritten))


def sanitize_llm_output(
    raw: str, original: str, *, max_expand: float = 1.8, min_keep: float = 0.6,
) -> Optional[str]:
    """消毒 + 校验 LLM 口语化输出。异常（空/超长/超短/串语言/元话语/丢锚点）→ None。纯函数。"""
    from src.ai.voice_colloquial import _is_chinese_dominant

    t = str(raw or "").strip()
    if not t:
        return None
    t = _PREFIX_RE.sub("", t).strip()
    # 元话语段截断（防「改写正文\n\n解释：...」把解释念出来）
    m = _META_SPLIT_RE.search(t)
    if m:
        t = t[:m.start()].strip()
    t = t.strip(_QUOTE_CHARS).strip()
    if not t:
        return None
    core = str(original or "").strip()
    if not core:
        return None
    # 长度守卫：过短=截断/丢信息，过长=发挥过度/夹带解释（+8 给短文本余量）。
    # 缩水下限 0.3→0.6（2026-07-27 实锤：讲故事回复被口语化 67→33 腰斩，客户听到
    # 半截故事投诉「怎么说话说一半」——口语化是**改写**不是摘要，砍掉 >40% 必是
    # 内容丢失；拒了回落规则档＝完整原文照念，永远不劣化）。
    if len(t) < len(core) * float(min_keep):
        return None
    if len(t) > len(core) * float(max_expand) + 8:
        return None
    # 语言守卫：原文中文而输出串了别的语言 → 拒（防 garble）
    if not _is_chinese_dominant(t):
        return None
    # 事实锚点守卫：数字/金额/型号丢了或被改 → 拒（红线，零误伤的那一半；
    # 语义漂移那一半在 llm_colloquialize 里用嵌入余弦兜，见该函数注释）
    _lost = lost_anchors(core, t)
    if _lost:
        logger.info("[voice_colloquial_llm] 拒改写：事实锚点丢失 %s", _lost[:3])
        return None
    return t


# ── 缓存（进程级 LRU）────────────────────────────────────────────────────────
_CACHE: "OrderedDict[str, str]" = OrderedDict()
_CACHE_MAX = 512
_CACHE_LOCK = threading.Lock()


def _cache_key(text: str, emotion: str, lead: bool, style: str,
               disfluency: bool = False, intensity: str = "natural") -> str:
    raw = (f"{text}\x1f{emotion}\x1f{int(bool(lead))}\x1f{style}"
           f"\x1f{int(bool(disfluency))}\x1f{intensity}")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _cache_get(key: str) -> Optional[str]:
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    return None


def _cache_put(key: str, value: str) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = value
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


# ── 端点熔断（连续失败 → 冷却期直接回落，不让挂掉的端点每条拖满超时）──────────
_FAIL_THRESHOLD = 3
_COOLDOWN_SEC = 60.0
_fail_streak = 0
_cooldown_until = 0.0
_last_ok_ts = 0.0     # 最近一次成功改写（wall clock，健康信号用）
_last_fail_ts = 0.0   # 最近一次失败（wall clock，健康信号用）
_CB_LOCK = threading.Lock()


def _in_cooldown() -> bool:
    with _CB_LOCK:
        return time.monotonic() < _cooldown_until


def _record_success() -> None:
    global _fail_streak, _cooldown_until, _last_ok_ts
    with _CB_LOCK:
        _fail_streak = 0
        _cooldown_until = 0.0
        _last_ok_ts = time.time()


def _record_failure() -> None:
    global _fail_streak, _cooldown_until, _last_fail_ts
    with _CB_LOCK:
        _fail_streak += 1
        _last_fail_ts = time.time()
        if _fail_streak >= _FAIL_THRESHOLD:
            _cooldown_until = time.monotonic() + _COOLDOWN_SEC
            logger.info("[voice_colloquial_llm] 连续 %d 次失败 → 冷却 %ds（回落规则档）",
                        _fail_streak, int(_COOLDOWN_SEC))


def health_signal() -> dict:
    """健康信号快照（HealthWatchdog 巡检消费，2026-07-15 九连败静默事故修复）。

    此前端点长期挂掉只会「每 60s 熔断-重试」无限循环，日志有记录但无人被通知，
    语音口语化静默降级规则档。信号口径与 avatar_voice_stats.hang_signal 对齐：
    连败数 + 最近成/败时刻，阈值判断留在 watchdog（可配置、可测试）。
    """
    with _CB_LOCK:
        return {
            "fail_streak": int(_fail_streak),
            "in_cooldown": time.monotonic() < _cooldown_until,
            "last_ok_ts": float(_last_ok_ts),
            "last_fail_ts": float(_last_fail_ts),
        }


# ── 本地 AIClient 懒加载（默认关，开了才构造一次；测试可注入）──────────────────
_AI_CLIENT: Optional[Any] = None
_AI_LOCK = threading.Lock()


def _get_ai_client(injected: Optional[Any] = None) -> Optional[Any]:
    global _AI_CLIENT
    if injected is not None:
        return injected
    if _AI_CLIENT is None:
        with _AI_LOCK:
            if _AI_CLIENT is None:
                try:
                    from src.ai.ai_client import AIClient
                    from src.utils.config_manager import ConfigManager
                    _AI_CLIENT = AIClient(ConfigManager())
                except Exception as exc:
                    logger.warning("[voice_colloquial_llm] AIClient 懒加载失败: %s", exc)
                    return None
    return _AI_CLIENT


# 语义地板：改写与原文的嵌入余弦下限。2026-07-28 实测校准（bge-m3，9 对样本）：
# 真口语化 0.862~0.986（最低是「因此我们无需担心→所以咱不用担心」这种整句换词），
# 漂移 0.526~0.696（答非所问 0.684 / 换主题 0.526 / 回答式 0.696）——0.78 落在
# +0.166 的干净间隔正中。刻意**不**指望余弦抓数字篡改（「198→168」余弦 0.974，
# 语义几乎不变），那半边由 lost_anchors 确定性兜住，两层各管一半。
DEFAULT_MIN_SIMILARITY = 0.78


async def _semantically_same(
    client: Any, original: str, rewritten: str, min_similarity: float,
) -> bool:
    """改写是否仍在说同一件事（嵌入余弦 ≥ 阈值）。

    **fail-open**：无 embed / 端点抖动 / 返回空 → True（放行）。守卫自身故障不该
    把口语化整条链拖死；且拒绝的代价很小（回落规则档＝原文照念，永远不劣化），
    所以宁可在能判时判严、判不了时放过。
    """
    if min_similarity <= 0:
        return True
    embed = getattr(client, "embed", None)
    if embed is None:
        return True
    try:
        vecs = await embed([original, rewritten])
        if not vecs or len(vecs) < 2 or not vecs[0] or not vecs[1]:
            return True
        va, vb = vecs[0], vecs[1]
        num = sum(x * y for x, y in zip(va, vb))
        na = sum(x * x for x in va) ** 0.5
        nb = sum(x * x for x in vb) ** 0.5
        if not na or not nb:
            return True
        sim = num / (na * nb)
    except Exception as exc:
        logger.debug("[voice_colloquial_llm] 语义校验不可用（放行）: %s", exc)
        return True
    if sim < float(min_similarity):
        logger.info(
            "[voice_colloquial_llm] 拒改写：语义漂移 cos=%.3f < %.2f | 原=%r 改=%r",
            sim, min_similarity, original[:30], rewritten[:30])
        return False
    return True


async def llm_colloquialize(
    text: str,
    *,
    ai_client: Optional[Any] = None,
    emotion: str = "neutral",
    lead: bool = True,
    style: str = "",
    min_chars: int = 12,
    timeout_sec: float = 8.0,
    max_expand: float = 1.8,
    disfluency: bool = False,
    intensity: str = "natural",
    provider: str = "local",
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> Optional[str]:
    """本地 LLM 口语化。命中缓存直接返回；失败/超时/熔断/校验不过 → None（回落规则档）。

    短句（<min_chars）/ 非中文 → None（与规则档同口径 no-op）。
    ``disfluency``＝本条允许一处轻口误自纠（⑥，调用方按 crc 低频开启；进缓存键）。
    ``intensity``＝口语重塑力度 light|natural|vivid（AI Live OS Agent6）：vivid 允许主观
    口吻框架/停顿，输出会比原文长一些 → 自动放宽长度上限（红线仍由 sanitize 守）。

    内容保持双层（2026-07-28 事故：本地模型把「嗨，我在宿务这边刚开完店，你吃饭了吗」
    改写成「刚忙完啊我吃过了你那边店开起来肯定一堆事吧…」＝**回答**而不是改写，
    38 字对 18 字刚好卡在 1.8 倍上限内一路放行，人设语音说出的话与要说的话完全无关）：
      ① ``sanitize_llm_output`` 里的 ``lost_anchors``——数字/金额/型号丢了即拒（确定性）；
      ② 本函数的 ``_semantically_same``——嵌入余弦 < ``min_similarity`` 即拒（fail-open）。
    两层都不过就回落规则档（原文照念），永远不劣化。
    """
    core = str(text or "").strip()
    if len(core) < max(1, int(min_chars)):
        return None
    from src.ai.voice_colloquial import _is_chinese_dominant
    if not _is_chinese_dominant(core):
        return None

    # vivid 档加了主观口吻框架，天然更长 → 放宽膨胀上限（≤1.8 会误杀正常重塑）。
    if str(intensity or "").strip().lower() == "vivid" and max_expand < 2.2:
        max_expand = 2.2

    key = _cache_key(core, emotion, bool(lead), style, bool(disfluency), intensity)
    hit = _cache_get(key)
    if hit is not None:
        return hit or None          # 缓存空串＝已知无有效改写 → 回落规则档

    if _in_cooldown():
        return None                 # 端点冷却期：秒回落，不调 LLM

    client = _get_ai_client(ai_client)
    if client is None:
        return None

    # provider：local=LAN 本地模型（ai.fallback，零云成本）｜cloud=主云端模型
    # （ai.base_url/model，质量稳但每条一次调用）｜auto=云端优先、失败回落本地。
    # 本地节点缺模型/离线时 local 档会静默回落规则档（书面稿直送 TTS＝念稿感），
    # 运营可用 cloud 换确定的口语质量。
    prov = str(provider or "local").strip().lower()
    order = {"cloud": ("cloud",), "auto": ("cloud", "local")}.get(prov, ("local",))
    system = build_colloquial_prompt(emotion, bool(lead), style,
                                     disfluency=bool(disfluency),
                                     intensity=intensity)
    raw = None
    for _p in order:
        _fn = getattr(
            client, "rewrite_cloud" if _p == "cloud" else "rewrite_local", None)
        if _fn is None:
            continue
        try:
            raw = await _fn(system, core, timeout_sec=timeout_sec)
        except Exception as exc:
            logger.debug("[voice_colloquial_llm] rewrite_%s 异常: %s", _p, exc)
            raw = None
        if raw:
            break

    out = sanitize_llm_output(raw, core, max_expand=max_expand) if raw else None
    if out and out != core:
        if not await _semantically_same(client, core, out, min_similarity):
            _record_failure()
            _cache_put(key, "")
            return None
        _record_success()
        _cache_put(key, out)
        return out
    # 校验不过 / 空 / 与原文相同：记一次失败（推进熔断），缓存空串短期不重试同句
    _record_failure()
    _cache_put(key, "")
    return None


def set_ai_client(client: Optional[Any]) -> None:
    """注入已初始化的主 AIClient（main.py bootstrap 调用）。

    修复（2026-07-24）：本模块原靠 ``_get_ai_client`` 懒建 ``AIClient(ConfigManager())``，
    但该 ConfigManager 从未 ``await load()``、AIClient 从未 ``await initialize()``
    → ``_fb_model`` 恒空 → ``rewrite_local`` 恒 None → **colloquial LLM 档实际从未生效**
    （静默回落规则档）。main.py 在 ``ai_client.initialize()`` 后注入已初始化实例，
    口语化 LLM 档才真正走 ``ai.fallback`` 本地端点。未注入时仍回落原懒加载（兼容测试）。
    """
    global _AI_CLIENT
    with _AI_LOCK:
        _AI_CLIENT = client


def reset_state() -> None:
    """清空缓存 + 熔断 + 懒加载客户端（测试用）。"""
    global _AI_CLIENT, _fail_streak, _cooldown_until, _last_ok_ts, _last_fail_ts
    with _CACHE_LOCK:
        _CACHE.clear()
    with _CB_LOCK:
        _fail_streak = 0
        _cooldown_until = 0.0
        _last_ok_ts = 0.0
        _last_fail_ts = 0.0
    with _AI_LOCK:
        _AI_CLIENT = None


__all__ = [
    "DEFAULT_MIN_SIMILARITY",
    "llm_colloquialize", "set_ai_client", "build_colloquial_prompt", "sanitize_llm_output",
    "lost_anchors", "health_signal", "reset_state",
]
