"""生成层口语分叉（Phase G）— 一次 LLM 调用同时产「书面版 + 口语版」。

为什么（2026-07-14 活人感冲刺收官）：此前链路是「LLM 出书面回复 → 语音发送前
再调一次本地 LLM 口语化改写（voice_colloquial_llm，2~8s）」。两个缺陷：
① 多一次 LLM 往返（延迟+算力）；② 改写方只看到成品文本，语境贴合度天然低于
生成方（生成方手里有完整人设/记忆/上下文）。本模块把口语化上移到生成层：
对「会发语音的中文会话」在 prompt 里多要一个 ``[口语版]`` 段，同一次调用产出
两版——书面版进镜像/记忆（眼睛的字），口语版直接送 TTS（耳朵的话）。

安全设计（核心不变量）：
  - **标记绝不泄漏**：只要请求过口语版，返回前无条件剥离标记段——即使 LLM
    输出畸形（标记写在句中/口语段为空），书面版也绝不带 ``[口语版]`` 字样。
  - **哈希匹配即失效**：口语版按「书面版全文哈希」暂存；发语音时书面文本若被
    任何后处理改过（人设守卫改写/危机兜底覆盖/翻译/加后缀）→ 哈希对不上 →
    自动回落既有 colloquial 链。危机兜底文案绝不会被错配的口语版顶替。
  - **门控克制**：仅 telegram.voice_reply 开 + trigger 可能命中 + colloquial
    开且 ``generated: true`` + 用户消息中文为主，才多花这几十 token。
  - **验证后才用**：口语段须非空、中文为主（书面中文时）、长度比例合理，
    不合格 → 丢弃口语段但仍剥标记（书面版永远干净）。

进程级暂存（LRU + TTL）：跨「生成（skill_manager 线程）→ 发送（sender）」两个
调用点传值，键=书面文本哈希——不需要在十几层调用链里穿参数。

可单测纯函数：should_request_spoken_variant / build_spoken_variant_instruction /
split_spoken_variant / stash_spoken_variant / take_spoken_variant。
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

# 标记：要求 LLM 另起一行以此开头输出口语版（方/尖括号、可带冒号都认）
SPOKEN_MARKER = "[口语版]"
# 刻意**不锚定行首**：实测 DeepSeek 常把标记接在正文末尾同一行（「…来看看？[口语版] 我在
# 宿舍呢…」）。行首锚定时这类输出既取不到口语版（白吃 token + 口语化静默失效），书面版还
# 会带着标记连同重复内容发给客户并被 TTS 念出来。宽口径匹配同时修掉这两件事。
_MARKER_RE = re.compile(r"[ \t>*\-]*[\[【]\s*口语版\s*[\]】][:：]?[ \t]*")

# 暂存：sha1(书面版) -> (expire_monotonic, 口语版)
_STORE: "OrderedDict[str, Tuple[float, str]]" = OrderedDict()
_LOCK = threading.Lock()
# B 线（autosend）草稿→投递可隔着审批/humanize 延迟，600s 覆盖正常窗口；
# 键=全文哈希 → 即使窗口内撞同文本，其口语版也同样有效，放宽 TTL 无错配风险。
STORE_TTL_SEC = 600.0
STORE_CAP = 64


def should_request_spoken_variant(
    raw_cfg: Optional[Dict[str, Any]], *, is_peer_voice: bool, text: str,
) -> bool:
    """本条消息是否值得让 LLM 多产一个口语版。纯函数。

    条件：voice_reply 开 && trigger 可能命中（when_peer_voice 需对方是语音）
    && colloquial.enabled+generated && 用户消息中文为主（口语化只做中文，
    非中文会话多要一段纯属浪费 token）。
    """
    cfg = raw_cfg or {}
    vr = (cfg.get("telegram") or {}).get("voice_reply") or {}
    if not vr.get("enabled", False):
        return False
    trig = str(vr.get("trigger", "when_peer_voice")).strip().lower()
    if trig == "never":
        return False
    if trig == "when_peer_voice" and not is_peer_voice:
        return False
    col = ((cfg.get("avatar_voice") or {}).get("colloquial") or {})
    if not (col.get("enabled", False) and col.get("generated", False)):
        return False
    try:
        from src.ai.voice_colloquial import _is_chinese_dominant
        return _is_chinese_dominant(str(text or ""))
    except Exception:
        return False


def should_request_spoken_variant_autosend(
    raw_cfg: Optional[Dict[str, Any]], *, peer_sent_voice: bool, text: str,
) -> bool:
    """B 线（System Z autosend 草稿）版门控：``inbox.l2_autosend.voice`` 口径。

    与 A 线同结构，trigger 语义对齐 voice_autosend.decide_voice：
    never→False；when_peer_voice→对方本条是语音才要；always/smart→要
    （smart 评分在投递时判，生成期先备着口语版——未用只是几十 token）。
    """
    cfg = raw_cfg or {}
    vb = (((cfg.get("inbox") or {}).get("l2_autosend") or {}).get("voice") or {})
    if not vb.get("enabled", False):
        return False
    trig = str(vb.get("trigger", "when_peer_voice")).strip().lower()
    if trig == "never":
        return False
    if trig == "when_peer_voice" and not peer_sent_voice:
        return False
    col = ((cfg.get("avatar_voice") or {}).get("colloquial") or {})
    if not (col.get("enabled", False) and col.get("generated", False)):
        return False
    try:
        from src.ai.voice_colloquial import _is_chinese_dominant
        return _is_chinese_dominant(str(text or ""))
    except Exception:
        return False


def build_spoken_variant_instruction(*, disfluency: bool = False,
                                     intensity: str = "natural") -> str:
    """prompt 指令块：正常回复后另起一行输出口语版。

    ``disfluency``（⑥ 口误自纠，由调用方按文本 crc 低频开启）：允许口语版带
    **一处**很轻的自然口误自纠（「明天…啊不对，是后天」）——最像真人也最易
    做作，所以确定性低频 + 每条至多一处。
    ``intensity``＝口语重塑力度 light|natural|vivid（AI Live OS Agent6，2026-07-24）：
    vivid 放开「真人随口说」的主观口吻，但**事实红线不变**（意思/数字/承诺不改、不编造）。
    """
    base = (
        "【语音版输出——本条回复将以语音条发送】\n"
        f"正常写完回复后，另起一行，以 {SPOKEN_MARKER} 开头，再写一遍这条回复的"
        "口语说法，当成你正拿着手机给对方录语音（P0-1 语音条脚本范式，2026-08-03）：\n"
        "· 意思和所有事实与正文一致、同一种语言，但按「嘴巴说」重新组织：短句、顺口、"
        "可以有省略和碎句，去掉书面连接词/列表符号/括号注释/emoji；\n"
        "· 每句都要短，一句超过十五个字就拆成两句说，句与句用句号或省略号分开；\n"
        "· 不要用「哈哈/嘿/哎呀/嗨/诶」这类感叹词、笑声或口头禅开头，第一个字直接"
        "进入正文——笑意和情绪由声音表达，不靠字面；\n"
        "· 正文若以问题收尾可以保留那个问题，但不要为了找话新加反问。"
    )
    if str(intensity or "natural").strip().lower() == "vivid":
        base += (
            "\n· 可以有一点点「真人随口说」的主观口吻（比如「说真的」「我跟你讲」"
            "放在句中），像真的在想着对你说，但绝不编造原文没有的事实。"
        )
    if disfluency:
        base += (
            "\n· 这一版里可以有至多一处很轻的口误自纠（比如「明天…啊不对，后天」），"
            "要自然随意，不要刻意。"
        )
    return base + "\n除正文和这一行外不要输出任何解释。"


def want_disfluency(raw_cfg: Optional[Dict[str, Any]], text: str) -> bool:
    """⑥ 口误自纠门控：``colloquial.disfluency`` 开 && crc32(text)%N==0。

    默认 N=5（约 1/5，比旧 1/7 略放量——ChatGPT 式口误/思考感）；
    可用 ``colloquial.disfluency_every`` 覆盖（夹在 2..20）。确定性；纯函数。
    """
    cfg = raw_cfg or {}
    col = ((cfg.get("avatar_voice") or {}).get("colloquial") or {})
    if not col.get("disfluency", False):
        return False
    try:
        import zlib
        every = int(col.get("disfluency_every", 5) or 5)
        every = max(2, min(20, every))
        return zlib.crc32(str(text or "").encode("utf-8")) % every == 0
    except Exception:
        return False


def _spoken_valid(written: str, spoken: str) -> bool:
    """口语段质量校验：非空/长度比例/语言一致（书面中文→口语必须中文）。"""
    if not spoken or len(spoken) < 4:
        return False
    wl = max(1, len(written))
    if not (0.3 * wl <= len(spoken) <= 2.2 * wl + 16):
        return False    # 过短=截断，过长=发挥过度/夹带
    try:
        from src.ai.voice_colloquial import _is_chinese_dominant
        if _is_chinese_dominant(written) and not _is_chinese_dominant(spoken):
            return False
    except Exception:
        pass
    return True


def split_spoken_variant(raw: str) -> Tuple[str, Optional[str]]:
    """LLM 原始输出 → (书面版, 口语版|None)。防御式纯函数。

    不变量：返回的书面版**绝不含标记**（畸形输出也剥干净）；口语段不合格 →
    (干净书面版, None)。无标记 → 原文原样返回。
    """
    text = str(raw or "").strip()
    if not text:
        return "", None
    m = _MARKER_RE.search(text)
    if not m:
        return text, None
    written = text[: m.start()].strip()
    spoken = text[m.end():].strip()
    # LLM 复读标记 / 口语版又分段 → 只取第一段，其余剥掉
    m2 = _MARKER_RE.search(spoken)
    if m2:
        spoken = spoken[: m2.start()].strip()
    spoken = spoken.strip("「」『』\"'").strip()
    if not written:
        # 标记在开头没有正文（畸形）：把口语段当正文用，不信任分叉
        return (spoken or text.replace(SPOKEN_MARKER, "").strip()), None
    if not _spoken_valid(written, spoken):
        return written, None
    return written, spoken


def _key(written: str, scope: str = "") -> str:
    """书面版哈希；``scope``=account_id 时双号互不抢口语暂存。"""
    base = hashlib.sha1(str(written or "").strip().encode("utf-8")).hexdigest()
    sc = str(scope or "").strip()
    if sc and sc != "default":
        return f"{sc}:{base}"
    return base


def stash_spoken_variant(
    written: str, spoken: str, *, scope: str = "",
) -> None:
    """按书面版哈希暂存口语版（LRU+TTL；best-effort 绝不抛）。"""
    try:
        if not (written and spoken):
            return
        k = _key(written, scope)
        now = time.monotonic()
        with _LOCK:
            _STORE[k] = (now + STORE_TTL_SEC, spoken)
            _STORE.move_to_end(k)
            while len(_STORE) > STORE_CAP:
                _STORE.popitem(last=False)
    except Exception:
        pass


def take_spoken_variant(written: str, *, scope: str = "") -> Optional[str]:
    """取（并消费）书面版对应的口语版；被后处理改过/过期/没有 → None。"""
    try:
        k = _key(written, scope)
        with _LOCK:
            hit = _STORE.pop(k, None)
        if not hit:
            return None
        expire, spoken = hit
        if time.monotonic() > expire:
            return None
        return spoken or None
    except Exception:
        return None


def reset_store() -> None:
    """测试辅助：清空暂存。"""
    with _LOCK:
        _STORE.clear()
