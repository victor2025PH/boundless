"""出站语音按语言路由音色（粤语→粤语 TTS + 通用「音色跟随文本语种」）。

背景：克隆声后端（avatar_clone/CosyVoice）用普通话音色读粤语文本，发音"唔咸唔淡"；
edge 链则更糟——音色是**按语言训练的**（ja-JP-NanamiNeural 念中文=「日本腔中文」，
2026-07-23 生产实锤：客户投诉"老是讲日语"，其实文本是中文、音色是日语女声）。
本模块把「本条合成文本的语种」与「本次要用的音色」对齐：

1. **粤语路由**（既有）：文本含粤语特征字 → 切地道粤语 Edge 音色。
2. **follow_text 通用路由**（2026-07-23 新增）：
   - 生效面=**edge_tts 链**（顶层或 voice_profile 生效后端为 edge_tts）；
     openai/elevenlabs 天然多语、克隆链原生跟随文本语种（Phase6 实证），不动主后端。
   - 检测文本语种（``translation_service.detect_language``，确定性零 LLM）；
     与当前音色的 BCP47 语言前缀不一致 → 换成该语种的映射音色
     （``follow_text.voices`` 覆写 > 内置 ``EDGE_VOICE_BY_LANG``）。
   - **Multilingual 音色豁免**（如 en-US-AvaMultilingualNeural）：自适应语种，不换。
   - **拒发守卫**：语种明确但无音色可映射（含配置残缺）→ 返回 ``reject:<lang>``，
     调用方放弃语音回落文字——「宁可没语音，不发错语言的语音」。
   - 克隆链虽不动主后端，但把 ``fallback_voice`` 对齐文本语种，克隆失败回落 edge
     时不再用错语言音色兜底。

设计：
- 纯函数 + 配置门控 ``voice_lang_route.enabled``（默认关，基线零行为变更）；
  ``follow_text.enabled`` 随父开关默认开（开了语言路由=要正确的语言路由）。
- 路由是**主动选择**而非兜底降级 → 不受 ``no_edge_fallback`` 拒发约束；
- 本模块的语种→Edge 音色映射是**单一事实源**（proactive_voice_foreign 复用）。

接线点（全部出站语音路径同口径）：
- ``inbox/voice_autosend._synth_ogg`` —— System Z autosend / protocol 自动回复
  （WA Baileys、messenger-web…）/ 主动触达中文语音（stage_voice_file 共用）；
- ``client/sender._maybe_send_voice_reply`` —— 原生 Telegram 自动语音回复；
- ``web/routes/unified_inbox_send_routes`` 手动坐席发语音 —— 坐席显式覆写
  voice/backend 时跳过路由（尊重人工选择），否则同自动路径；
- 试听/测试端点（voice_routes tts-test、voice_live preview）**刻意不路由**：
  那是「听这套配置本身」的工具，路由会掩盖操作员想验证的音色。

局限（诚实边界）：Edge 音色不是人设克隆音（音色会变）。要"同一把声讲外语"
需克隆后端原生多语（CosyVoice3 支持 zh/en/ja/ko 等；下阶段按语种能力分流）。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 粤语专用/高频特征字（书面普通话几乎不用）。一个字算一个 marker。
_CANTO_CHARS = "嘅咁咗唔哋喺嚟氹嗰乜嘢佢睇畀冇諗攞嗌埋啱噉嚿餸靚嗮曬啵嘞喇喎咩囉噶嗌咯嘞"
# 粤语双字组合（比单字更强的信号）
_CANTO_BIGRAMS = (
    "点解", "而家", "咁样", "系咪", "唔系", "唔好", "唔使", "唔通", "几好",
    "好耐", "琴日", "听日", "宜家", "得闲", "冇问题", "麻麻地", "咪住",
    "識聽", "识听", "講嘢", "讲嘢", "俾我", "畀我", "睇下", "谂住",
)

# ── 语种前缀 → Edge 神经声（女声系，与陪伴人设性别一致；可经配置覆写）─────────
# 单一事实源：proactive_voice_foreign（主动外语开场）与 follow_text 路由共用。
# 覆盖 translation_service.detect_language 能返回的全部明确语种。
EDGE_VOICE_BY_LANG: Dict[str, str] = {
    "zh": "zh-CN-XiaoxiaoNeural",
    "en": "en-US-JennyNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
    "th": "th-TH-PremwadeeNeural",
    "vi": "vi-VN-HoaiMyNeural",
    "id": "id-ID-GadisNeural",
    "ms": "ms-MY-YasminNeural",
    "es": "es-ES-ElviraNeural",
    "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",
    "it": "it-IT-ElsaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "ar": "ar-EG-SalmaNeural",
    "hi": "hi-IN-SwaraNeural",
    "tr": "tr-TR-EmelNeural",
    "tl": "fil-PH-BlessicaNeural",
    "fil": "fil-PH-BlessicaNeural",
    "km": "km-KH-SreymomNeural",
    "he": "he-IL-HilaNeural",
    "el": "el-GR-AthinaNeural",
}

# follow_text 拒发守卫的 tag 前缀（调用方判 tag.startswith 即可识别）
REJECT_TAG_PREFIX = "reject:"

# 与 TTSPipeline 的克隆后端集合同口径（克隆链原生跟随文本语种，不动主后端）
_CLONE_BACKENDS = frozenset({
    "avatar_clone", "minicpm_clone", "voice_clone_lan", "voice_clone_command",
    "coqui_http",
})

# ── 克隆链语种能力表（P0 2026-08-31「日文怪声」事故）─────────────────────────
# 「跟随翻译发声」把文字译成客户语言后交克隆链念，但克隆引擎语种能力各不相同
# （IndexTTS-2 只有中英，假名超出建模范围 → 念出不是任何语言的怪声，坐席只能
# 靠耳朵事后发现）。此表=事前判定的单一事实源：effective-config 据此回传
# voice_langs，前端在目标语超出能力时于**生成前**警示。
# 口径=**确证能念**才进表（表内缺失→警示）；整条链拿不准→返回空元组=不警示，
# 宁可漏警不冤枉（与孤儿引用门禁「只守高置信」同哲学）。
#   index_tts / moss_ttsd：官方仅中英（日/西/阿是 IndexTTS-2.5 才加的，hub 未升级）；
#   fish_speech：2026-08-02 hub 真机实证 en/es（ko 稳定失败、ja 已撤，见下）；
#   avatar_clone（7852 CosyVoice3）：官方宣称 zh/en/ja/ko，本表只登记**实测念对**
#     的 zh/en/es/tl（#161 见下）。
# ⚠ 本表是**引擎语言能力**（模型层），不等于「任意声纹档×该引擎」可直接开闸：
# 2026-08-31 实测 hub fish_speech 对 8-29 重登的「-智聊」档全语种翻车（zh CER
# 0.53 / en 声纹 0.38=别人的声音 / ja 疑似哑音→ASR 幻觉套话）——档是 IndexTTS-2
# 形态注册的，fish 消费不到正确参考音；8-02 的实证档属 fish 时代，已不可比。
# 故 lang_engines 任何改派开闸前必跑 tools/verify_clone_lang.py 过全套机器判据
# （信封/RIFF/采样率指纹/能量/ASR 语种自检/CER/声纹）+ 人耳抽检。
CLONE_ENGINE_LANGS: Dict[str, Tuple[str, ...]] = {
    "index_tts": ("zh", "en"),
    "moss_ttsd": ("zh", "en"),
    # #161（2026-09-03 钧机 1.0.71 报告 22:46-23:04）：日语连三次 CER>0.35、
    # 重合成仍是「ゾオパパパ」乱音 → ja 从 fish 表撤下（8-02 的 ja 实证属
    # fish 时代的档，8-31 起 hub 档以 IndexTTS-2 形态注册，fish 消费不到正确
    # 参考音，旧实证已不可比）。要重新开闸须先过 tools/verify_clone_lang.py。
    "fish_speech": ("zh", "en", "es"),
    # 7852/CosyVoice3（avatar_clone 后端的实际引擎）：官方宣称 zh/en/ja/ko，
    # 但「官方宣称」不是本表的入表口径——只登记**实测念对**的语种。
    # 1145 证据群：zh/es/tl 克隆成功且校验通过；ja 乱音；ko 无任何实证。
    "cosyvoice": ("zh", "en", "es", "tl"),
    "cosyvoice3": ("zh", "en", "es", "tl"),
}
# 后端 → 该后端实际跑的引擎（能力真值仍取自 CLONE_ENGINE_LANGS，两表同源）。
# #161 前 avatar_clone 在此另写一份 (zh,en,ja,ko)，与引擎表各说各话：hub 未开
# 的部署走后端表 → ja/ko 一路放行到合成，念出乱音后靠「有能量」放行发给客户。
_CLONE_BACKEND_ENGINE: Dict[str, str] = {
    "avatar_clone": "cosyvoice3",
    "minicpm_clone": "index_tts",
    "voice_clone_lan": "index_tts",
    "voice_clone_command": "index_tts",
    "coqui_http": "index_tts",
}
_CLONE_BACKEND_LANGS: Dict[str, Tuple[str, ...]] = {
    b: CLONE_ENGINE_LANGS[e] for b, e in _CLONE_BACKEND_ENGINE.items()
}


def clone_voice_langs(
    avatar_voice_cfg: Optional[Dict[str, Any]],
    backend: str = "",
    persona_id: str = "",
) -> Tuple[str, ...]:
    """解析克隆链**主路**的可念语种前缀（拿不准返回空元组=前端不警示）。

    优先级：``avatar_voice.voice_langs`` 运营显式覆写（部署方最了解自家节点）
    → hub_fish 钉住引擎查表（hub 开且人设命中 allowlist=主路走 hub；hub 档上
    引擎未钉=能力未知）→ 克隆后端缺省表。纯函数、绝不抛。
    """
    av = avatar_voice_cfg if isinstance(avatar_voice_cfg, dict) else {}
    ov = av.get("voice_langs")
    if isinstance(ov, (list, tuple)) and ov:
        seen: list = []
        for x in ov:
            p = str(x or "").strip().lower().split("-")[0]
            if p and p not in seen:
                seen.append(p)
        return tuple(seen)
    hf = av.get("hub_fish") if isinstance(av.get("hub_fish"), dict) else {}
    # hub 分支只对 avatar_clone 家族成立（hub 调用点在 _try_avatar_clone 内部）；
    # minicpm_clone / voice_clone_lan / coqui_http 走各自直连节点根本不经 hub，
    # 套 hub 引擎表会把「粤语线/通用语种路由改写后的后端」误判成 hub 能力
    # （2026-08-31 修：clone_langs 通用路由改写 backend=minicpm_clone 后曾被
    # hub 表误拦）。backend 为空（effective-config 未解析场景）沿用 hub 语义。
    _b = str(backend or "").strip().lower()
    if bool(hf.get("enabled")) and _b in ("", "avatar_clone"):
        allow = hf.get("persona_allowlist") or []
        pid = str(persona_id or "").strip()
        if not allow or (pid and pid in allow):
            eng = str(hf.get("tts_engine") or "").strip()
            if not eng:
                return ()  # hub 档上引擎未知：不确定就不警示
            eng = _norm_engine(eng)
            base = CLONE_ENGINE_LANGS.get(eng, ())
            if not base:
                return ()  # 引擎未登记＝能力未知
            # P1 语种→引擎路由（2026-08-31）：hub_fish.lang_engines 把特定语种
            # 改派到 hub 上另一台引擎（如 {ja: fish_speech}）——该语种若在目标
            # 引擎能力表内，则计入主路可念集合（与 _try_hub_fish 的择引擎逻辑
            # 同一份配置，SSOT 口径自动一致）。映射到未登记引擎的语种不计入
            # （宁可少报能力，绝不虚报）。
            le = hf.get("lang_engines") if isinstance(
                hf.get("lang_engines"), dict) else {}
            if not le:
                return base
            out = list(base)
            for lang_key, eng_name in le.items():
                p = str(lang_key or "").strip().lower().split("-")[0]
                if not p or p in out:
                    continue
                mapped = _norm_engine(str(eng_name or "").strip())
                if p in CLONE_ENGINE_LANGS.get(mapped, ()):
                    out.append(p)
            return tuple(out)
    return _CLONE_BACKEND_LANGS.get(str(backend or "").strip().lower(), ())


def _norm_engine(name: str) -> str:
    """引擎名归一（复用 avatar_voice 的别名表；不可用时退化小写）。"""
    n = str(name or "").strip()
    if not n:
        return ""
    try:
        from src.ai.avatar_voice import normalize_engine_name
        return normalize_engine_name(n) or n.lower()
    except Exception:
        return n.lower()


def clone_route_langs(config: Optional[Dict[str, Any]]) -> Tuple[str, ...]:
    """``voice_lang_route.clone_langs`` 已开通的语种前缀（路由整体关＝空元组）。

    消费方＝主动触达 planner 的克隆能力并集（clone_voice_langs 只看克隆主链，
    看不见按语种改派的克隆节点——不并进来，日/韩客户的主动语音开场会被错误
    收窄到 edge 通用声）。纯函数、绝不抛。
    """
    try:
        rc = (config or {}).get("voice_lang_route") or {}
        if not rc.get("enabled", False):
            return ()
        cl = rc.get("clone_langs") if isinstance(rc.get("clone_langs"), dict) else {}
        out = []
        for k, v in cl.items():
            if not isinstance(v, dict):
                continue
            p = str(k or "").strip().lower().split("-")[0]
            if p and p != "zh" and p not in out:
                out.append(p)
        return tuple(out)
    except Exception:
        return ()


def hub_engine_for_lang(
    avatar_voice_cfg: Optional[Dict[str, Any]],
    lang: str,
) -> str:
    """按语种取 hub 引擎覆写（``hub_fish.lang_engines``；无映射返回 ""）。

    供 ``_try_hub_fish`` 在合成前择引擎：返回配置里的**原始引擎名**（hub 认
    什么名就配什么名，不做归一——归一表是本仓的别名认知，下发以运营配置为准）。
    纯函数、绝不抛。
    """
    av = avatar_voice_cfg if isinstance(avatar_voice_cfg, dict) else {}
    hf = av.get("hub_fish") if isinstance(av.get("hub_fish"), dict) else {}
    le = hf.get("lang_engines") if isinstance(hf.get("lang_engines"), dict) else {}
    if not le:
        return ""
    p = str(lang or "").strip().lower().split("-")[0]
    if not p:
        return ""
    hit = le.get(p)
    if hit is None:
        # 容错：配置键带地区码（ja-JP）也认前缀
        for k, v in le.items():
            if str(k or "").strip().lower().split("-")[0] == p:
                hit = v
                break
    return str(hit or "").strip()


def is_clone_backend(backend: str) -> bool:
    """该后端是否属克隆链（``_CLONE_BACKENDS`` 或 ``*_clone`` 命名约定）。"""
    b = str(backend or "").strip().lower()
    return bool(b) and (b in _CLONE_BACKENDS or b.endswith("_clone"))


def clone_lang_gate(
    text: str,
    avatar_voice_cfg: Optional[Dict[str, Any]],
    backend: str = "",
    persona_id: str = "",
) -> str:
    """合成前语种能力闸（P0 2026-08-31「日文怪声」全链收口）。

    UI 的 ``voice_langs`` 警示只护住坐席手动面板；A 线自动语音回复 / B 线
    autosend / 主动触达三条自动链仍会把克隆引擎念不了的语种（假名/泰文…）
    直接送进合成，产出「不是任何语言的怪声」发给客户。本函数供 TTSPipeline
    在合成前单点判定：

    返回被拦语种前缀（如 ``"ja"``）＝主路念不了，调用方应跳过克隆链；
    返回 ``""``＝可念 / 非克隆后端 / 语种不明 / 能力表未知——**拿不准一律放行**
    （与 ``clone_voice_langs`` 空表语义一致：宁可漏拦不误拦，漏拦最坏=旧行为）。
    纯函数、绝不抛。
    """
    if not is_clone_backend(backend):
        return ""
    t = str(text or "")
    lang = detect_text_lang(t)
    if not lang or lang == "unknown":
        return ""
    prefix = lang.split("-")[0]
    # 误判护栏：中文正文夹带颜文字/引用里的零星假名（ヾ(≧▽≦*)o）会让脚本
    # 正则整句判成 ja——闸门若因此拦掉中文克隆、把中文转去 edge 日语声，就是
    # 2026-07-23「日本腔中文」事故重演。汉字主体 + 假名占比极低 → 按中文放行
    # （放行最坏=旧行为：引擎照读中文，假名字符本就会被引擎忽略/念错一两个音）。
    if prefix == "ja":
        kana = len(_KANA_RE.findall(t))
        han = len(_CJK_RE.findall(t))
        if kana < 2 or (han and kana * 5 < han):
            return ""
    langs = clone_voice_langs(avatar_voice_cfg, backend, persona_id)
    if not langs or prefix in langs:
        return ""
    return prefix


# opencc 转换器进程级缓存（懒加载软依赖；False=已探明不可用，不再重试 import）
_T2S_CC: Any = None


def to_simplified_for_tts(text: str) -> str:
    """繁体中文 → 简体，供克隆链**发音输入**用（P0 2026-08-31 zh-tw 接入）。

    中英克隆引擎（IndexTTS-2/MOSS）按简体+拼音建模，繁体次常见字会念错/跳字；
    繁简读音相同，转换只改喂给引擎的文本，不改展示/记录层（译稿、消息镜像仍是
    繁体原文）。三重豁免，转换器内建防误伤：
    - 非中文主体文本原样返回——日文汉字与繁体同形（``聞く``→``闻く`` 会把
      日文改写成简中混排，edge 日语声照着念就错了）；
    - 粤语文本原样返回（粤语用字不属繁简映射，且已有 117 粤语专线路由）；
    - opencc 缺失/异常原样返回（软依赖，绝不阻塞合成）。
    """
    t = str(text or "")
    if not t:
        return t
    try:
        lang = detect_text_lang(t)
        if lang.split("-")[0] != "zh":
            return t
        if is_cantonese_text(t):
            return t
        global _T2S_CC
        if _T2S_CC is False:
            return t
        if _T2S_CC is None:
            try:
                import opencc
                _T2S_CC = opencc.OpenCC("t2s")
            except Exception:
                _T2S_CC = False
                return t
        out = _T2S_CC.convert(t)
        return out if out else t
    except Exception:
        return t


_LATIN_LETTER_RE = re.compile(r"[A-Za-z\u00C0-\u024F]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 平/片假名（clone_lang_gate 的 ja 误判护栏用；与 translation_service 的 ja 脚本
# 正则同区间）
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
# 整字表脚本语种（detect_text_lang 的短文本护栏豁免名单）：与
# translation_service._SCRIPT_RE 的语种键对齐 + vi（拉丁系但有独占变音符，
# 同属「字符即信号」）。这些语种的字符不被 CJK/拉丁计数捕获，不豁免=恒 unknown。
_SCRIPT_LANGS = frozenset(
    {"ja", "ko", "th", "km", "ar", "ru", "hi", "he", "el", "vi"})


def count_cantonese_markers(text: str) -> int:
    """统计粤语特征信号数（单字 1 分、双字组合 2 分）。"""
    t = str(text or "")
    if not t:
        return 0
    score = sum(1 for ch in t if ch in _CANTO_CHARS)
    for bg in _CANTO_BIGRAMS:
        if bg in t:
            score += 2
    return score


def is_cantonese_text(text: str, *, min_markers: int = 2) -> bool:
    """文本是否为粤语书写（特征分 >= 阈值）。短文本按比例放宽由调用方决定。"""
    return count_cantonese_markers(text) >= max(1, int(min_markers))


def default_edge_voice_for_lang(lang: str) -> str:
    """语种前缀 → 内置 Edge 音色（无映射返回空串）。"""
    return EDGE_VOICE_BY_LANG.get(
        str(lang or "").strip().lower().split("-")[0], "")


def edge_voice_lang_prefix(voice_id: str) -> str:
    """Edge 音色 ID → BCP47 语言前缀（``ja-JP-NanamiNeural`` → ``ja``）。

    非 BCP47 形态（克隆 speaker/自定义名）返回空串=无法判断。
    """
    v = str(voice_id or "").strip()
    if not v or "-" not in v:
        return ""
    head = v.split("-", 1)[0].lower()
    # BCP47 primary subtag 为 2-3 位字母（fil 等三位也合法）
    if 2 <= len(head) <= 3 and head.isalpha():
        return head
    return ""


def is_voice_placeholder(voice_id: str) -> bool:
    """音色/说话人字段是否为**占位串**（如 ``___`` / ``--`` / ``…``）。

    #137/#140（2026-09-02 钧机诊断包实锤）：人设工作室把半成品语音设置以
    ``voice: "___"`` 落库，路由层把它当「一个语种不匹配的音色」→ 每条语音都
    被切去 edge 通用声，克隆档形同虚设。判据＝非空但不含任何字母/数字
    （unicode 口径）——真实音色（BCP47 / 克隆登记名）必含字母数字，绝不误伤。
    空串**不算**占位（空=本来就未设置，语义由调用方自行处理）。
    """
    v = str(voice_id or "").strip()
    return bool(v) and not any(ch.isalnum() for ch in v)


def _is_multilingual_voice(voice_id: str) -> bool:
    """Multilingual 系 Edge 音色自适应文本语种，任何语言都无需换声。"""
    return "multilingual" in str(voice_id or "").lower()


def _effective_backend_of(voice_cfg: Dict[str, Any]) -> str:
    """与 TTSPipeline._effective_backend 同口径：voice_profile 生效则其 backend 优先。"""
    cfg = voice_cfg or {}
    vp = cfg.get("voice_profile") if isinstance(cfg.get("voice_profile"), dict) else {}
    base = str(cfg.get("backend") or "edge_tts").strip().lower()
    if vp and bool(vp.get("enabled", False)):
        return str(vp.get("backend") or base).strip().lower()
    return base


def _effective_voice_of(voice_cfg: Dict[str, Any]) -> str:
    """与 TTSPipeline._effective_voice 同口径：voice_profile 生效则 speaker_id 优先。"""
    cfg = voice_cfg or {}
    vp = cfg.get("voice_profile") if isinstance(cfg.get("voice_profile"), dict) else {}
    if vp and bool(vp.get("enabled", False)):
        return str(vp.get("speaker_id") or cfg.get("voice") or "").strip()
    return str(cfg.get("voice") or "").strip()


def detect_text_lang(text: str) -> str:
    """确定性检测文本语种（复用全局 detect_language；异常/空 → ``unknown``）。

    短文本护栏：内容量不足（CJK < 2 且拉丁字母 < 4，如 "OK"/"？"）→ ``unknown``，
    防止把中文会话里的一句 "OK" 误路由成英文音色。

    整字表脚本语种豁免（P0 2026-08-31）：泰文/韩文/西里尔/阿拉伯文等语种的
    字符本身就是高置信信号，但 CJK+拉丁双计数对它们恒为 0——旧护栏把这些
    语种一律判 ``unknown``，follow_text 路由与克隆语种闸对 th/ko/ru/ar 整条
    盲区（EDGE_VOICE_BY_LANG 里明明备了这些语种的音色）。detect_language 判出
    脚本语种即直接采信，护栏只管 zh/拉丁系的歧义短文本。
    """
    t = str(text or "").strip()
    if not t:
        return "unknown"
    try:
        from src.ai.translation_service import detect_language
        lang = (detect_language(t) or "").strip().lower()
    except Exception:
        return "unknown"
    if not lang or lang == "unknown":
        return "unknown"
    if lang.split("-")[0] in _SCRIPT_LANGS:
        return lang
    cjk = len(_CJK_RE.findall(t))
    letters = len(_LATIN_LETTER_RE.findall(t))
    if cjk < 2 and letters < 4:
        return "unknown"
    return lang


def _route_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return (config or {}).get("voice_lang_route") or {}


def _follow_cfg(rc: Dict[str, Any]) -> Dict[str, Any]:
    ft = rc.get("follow_text")
    return dict(ft) if isinstance(ft, dict) else {}


def _mapped_voice(follow: Dict[str, Any], lang: str) -> str:
    """语种 → 音色：配置覆写（follow_text.voices）优先，内置映射兜底。"""
    prefix = str(lang or "").strip().lower().split("-")[0]
    overrides = follow.get("voices")
    if isinstance(overrides, dict):
        v = overrides.get(prefix) or overrides.get(lang)
        if v:
            return str(v).strip()
    return default_edge_voice_for_lang(prefix)


def _route_cantonese(
    voice_cfg: Dict[str, Any], text: str, rc: Dict[str, Any],
) -> Optional[Tuple[Dict[str, Any], str]]:
    """既有粤语路由（最高优先）。未命中返回 None。"""
    yue = rc.get("cantonese") or {}
    if not yue.get("enabled", True):
        return None
    min_markers = int(yue.get("min_markers", 2) or 2)
    if not is_cantonese_text(text, min_markers=min_markers):
        return None
    new_cfg = dict(voice_cfg or {})
    backend = str(yue.get("backend") or "edge_tts").strip().lower()
    new_cfg["backend"] = backend
    new_cfg["voice"] = str(yue.get("voice") or "zh-HK-HiuMaanNeural")
    # RVC 变声是普通话克隆链的附件，粤语路由下关掉防串味
    new_cfg.pop("rvc", None)
    if backend in _CLONE_BACKENDS:
        # 粤语克隆分支（2026-08-30，接 117:7852 CosyVoice3 原生粤语克隆）：
        # backend 配成克隆类＝运营声明「这个后端会念粤语」→ 不再停 voice_profile
        # （停掉=丢参考音=克隆形同虚设）。cantonese.voice_profile 是 **merge 覆写**
        # ——以人设自己的 voice_profile 为底、只覆盖给出的键：典型只覆
        # clone_base_url（指到粤语克隆节点）+ backend，参考音仍跟人设走＝
        # **同一把声讲粤语**（跨人设通用，不必为每个人设各写一份粤语档）；
        # 要粤语专用参考音时覆写 reference_audio_path 即可。
        # 克隆失败回落 edge 时必须落粤语 Edge 声——回落回普通话音色念粤文
        # 正是本路由要消灭的事故形态。
        vp_override = yue.get("voice_profile")
        if isinstance(vp_override, dict) and vp_override:
            base_vp = (voice_cfg or {}).get("voice_profile")
            merged = dict(base_vp) if isinstance(base_vp, dict) else {}
            merged.update(vp_override)
            new_cfg["voice_profile"] = merged
        new_cfg["fallback_voice"] = str(
            yue.get("fallback_voice") or "zh-HK-HiuMaanNeural")
    else:
        # 粤语音色链自身的兜底也用同音色（避免回落回普通话音色）
        new_cfg["fallback_voice"] = new_cfg["voice"]
        # 人设克隆声必须一并停用：TTSPipeline._effective_backend 里
        # voice_profile.enabled=true 时 voice_profile.backend 优先于顶层 backend，
        # 不清掉会把本次路由静默盖回克隆链（普通话腔读粤语，路由形同虚设）。
        new_cfg["voice_profile"] = {"enabled": False}
    logger.info(
        "[lang_voice_route] 粤语文本 → 切 %s (%s) markers>=%d",
        new_cfg["backend"], new_cfg["voice"], min_markers)
    return new_cfg, "yue"


def _route_clone_lang(
    voice_cfg: Dict[str, Any], text: str, rc: Dict[str, Any],
) -> Optional[Tuple[Dict[str, Any], str]]:
    """通用「语种 → 克隆节点」路由（P2 2026-08-31，粤语专线机制的泛化）。

    配置 ``voice_lang_route.clone_langs``（缺省空＝不启用，零行为变化）::

        clone_langs:
          ja:
            backend: minicpm_clone                  # 缺省 minicpm_clone（fish 契约直连）
            voice_profile:                          # merge 覆写：以人设档为底、只覆给出键
              clone_base_url: http://127.0.0.1:7852
              clone_text_prefix: "<|ja|>"           # CosyVoice 系跨语言标签（2026-08-31
                                                    # 实测必需：缺了=日文汉字按中文读音念）
            fallback_voice: ja-JP-NanamiNeural      # 缺省按语种取内置 Edge 表

    命中＝检测语种在映射内且非中文（zh 主链本就克隆；yue 有专线在前，特征字
    检测比脚本检测更准）。参考音跟人设走＝**同一把声讲外语**。节点×语种是否
    真能念由 ``tools/verify_clone_lang.py`` 验收后运营才落配置（配置即背书），
    故改写携带 ``_lang_route_cleared``——TTSPipeline 语种能力闸对该语种放行。
    """
    cl = rc.get("clone_langs") if isinstance(rc.get("clone_langs"), dict) else {}
    if not cl:
        return None
    lang = detect_text_lang(text)
    if not lang or lang == "unknown":
        return None
    prefix = lang.split("-")[0]
    if prefix == "zh":
        return None
    spec = cl.get(prefix)
    if spec is None:
        for k, v in cl.items():   # 配置键带地区码（ja-JP）也认前缀
            if str(k or "").strip().lower().split("-")[0] == prefix:
                spec = v
                break
    if not isinstance(spec, dict):
        return None
    new_cfg = dict(voice_cfg or {})
    backend = str(spec.get("backend") or "minicpm_clone").strip().lower()
    new_cfg["backend"] = backend
    vp_override = spec.get("voice_profile") \
        if isinstance(spec.get("voice_profile"), dict) else {}
    base_vp = (voice_cfg or {}).get("voice_profile")
    merged = dict(base_vp) if isinstance(base_vp, dict) else {}
    merged.update(vp_override)
    if merged:
        # 人设档 backend（多为 avatar_clone）经 _effective_backend 优先于顶层——
        # 不强制对齐会把本次路由静默盖回原链（粤语线同款坑，注释见 _route_cantonese）
        merged["backend"] = str(vp_override.get("backend") or backend)
        new_cfg["voice_profile"] = merged
    fb = str(spec.get("fallback_voice") or "").strip() \
        or default_edge_voice_for_lang(prefix)
    if fb:
        new_cfg["fallback_voice"] = fb
    new_cfg["_lang_route_cleared"] = prefix
    logger.info(
        "[lang_voice_route] 语种 %s → 克隆节点路由（backend=%s base=%s）",
        prefix, backend, str(merged.get("clone_base_url") or "-"))
    return new_cfg, prefix


def _recover_clone_profile(
    voice_cfg: Dict[str, Any], config: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """音色未配置时回查可用克隆档：已合并档（人设层）→ 全局 voice_reply → 兼容层。

    可用判据与 persona_voice #93 修补同口径：克隆类 backend + owner_consent +
    （参考音 或 有效 speaker_id）。``enabled`` 显式 False＝运营手动停用，绝不复活。
    命中返回该档的副本，全部落空返回 None。纯函数、绝不抛。
    """
    cands = []
    vp = (voice_cfg or {}).get("voice_profile")
    if isinstance(vp, dict):
        cands.append(vp)
    cfg = config or {}
    for block in (
        ((cfg.get("telegram") or {}).get("voice_reply") or {}),
        ((cfg.get("messenger_rpa") or {}).get("voice_output") or {}),
    ):
        gvp = block.get("voice_profile") if isinstance(block, dict) else None
        if isinstance(gvp, dict):
            cands.append(gvp)
    for cand in cands:
        try:
            backend = str(cand.get("backend") or "").strip().lower()
            if backend not in _CLONE_BACKENDS:
                continue
            if cand.get("enabled") is False:
                continue
            if not bool(cand.get("owner_consent")):
                continue
            spk = str(cand.get("speaker_id") or "").strip()
            if is_voice_placeholder(spk):
                spk = ""
            ref = str(cand.get("reference_audio_path") or "").strip()
            if not (ref or spk):
                continue
            return dict(cand)
        except Exception:
            continue
    return None


def _route_follow_text(
    voice_cfg: Dict[str, Any], text: str, rc: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[Dict[str, Any], str]]:
    """通用「音色跟随文本语种」路由。未命中/不适用返回 None。"""
    follow = _follow_cfg(rc)
    if not follow.get("enabled", True):
        return None
    lang = detect_text_lang(text)
    if not lang or lang == "unknown":
        return None
    prefix = lang.split("-")[0]
    backend = _effective_backend_of(voice_cfg)

    # 克隆链：原生跟随文本语种，不动主后端；只把 edge 兜底音色对齐语种，
    # 防克隆失败时用错语言音色兜底（默认兜底 zh 声念英文同样是错的）。
    if backend in _CLONE_BACKENDS:
        mapped = _mapped_voice(follow, prefix)
        if mapped:
            cur_fb = str(voice_cfg.get("fallback_voice") or "").strip()
            if edge_voice_lang_prefix(cur_fb) != prefix and not _is_multilingual_voice(cur_fb):
                new_cfg = dict(voice_cfg)
                new_cfg["fallback_voice"] = mapped
                logger.debug(
                    "[lang_voice_route] 克隆链兜底音色对齐语种 %s → %s",
                    prefix, mapped)
                # tag 留空：主后端未变，不影响 no_edge 语义与观测口径
                return new_cfg, ""
        return None

    # 非 edge 的公共后端（openai/elevenlabs/pyttsx3…）：多语自适应或非 BCP47
    # 音色体系，不路由。
    if backend != "edge_tts":
        return None

    cur_voice = _effective_voice_of(voice_cfg)
    if _is_multilingual_voice(cur_voice):
        return None

    # ── 音色未配置守卫（#93/#137/#140，2026-09-02 钧机实锤）──────────────────
    # 当前音色为空串/占位串（如 ``___``）＝**没有配置音色**，不是「一个语种
    # 不匹配的音色」——旧逻辑按语种不匹配切 edge，把明明登记成功的克隆声
    # 整台机器静默换成通用声（日志「文本语种 zh ≠ 音色 ___ → 切 edge_tts」
    # 三行实锤，同机 22:32 克隆登记与云端合成全部成功）。正确动作：回查
    # 人设/全局克隆档（voice_reply.voice_profile，backend=avatar_clone 等），
    # 有可用克隆档 → 走克隆（克隆链原生跟随文本语种，无需换声）；确实没有
    # 克隆 → 按语种取 edge 音色，但日志如实说「未配置」而非「不匹配」。
    if not str(cur_voice or "").strip() or is_voice_placeholder(cur_voice):
        recovered = _recover_clone_profile(voice_cfg, config)
        mapped = _mapped_voice(follow, prefix)
        if recovered is not None:
            new_cfg = dict(voice_cfg or {})
            new_vp = dict(recovered)
            new_vp["enabled"] = True
            new_cfg["voice_profile"] = new_vp
            new_cfg["backend"] = str(
                new_vp.get("backend") or "avatar_clone").strip().lower()
            if is_voice_placeholder(str(new_cfg.get("voice") or "")):
                new_cfg.pop("voice", None)
            if mapped:   # 克隆失败回落 edge 时兜底音色也对齐文本语种
                new_cfg["fallback_voice"] = mapped
            logger.info(
                "[lang_voice_route] 音色未配置（%s）→ 回查克隆档命中"
                "（backend=%s speaker=%s）→ 走克隆",
                repr(cur_voice or ""), new_cfg["backend"],
                new_vp.get("speaker_id") or "-")
            # tag 留空：这是把被占位串挤掉的克隆主链**恢复**，不是语言路由改声
            return new_cfg, ""
        if not mapped:
            if bool(follow.get("reject_unmapped", True)):
                logger.info(
                    "[lang_voice_route] 音色未配置且语种 %s 无音色映射 → "
                    "拒发语音回落文字", prefix)
                return dict(voice_cfg or {}), f"{REJECT_TAG_PREFIX}{prefix}"
            return None
        new_cfg = dict(voice_cfg or {})
        new_cfg["backend"] = "edge_tts"
        new_cfg["voice"] = mapped
        new_cfg["fallback_voice"] = mapped
        new_cfg.pop("rvc", None)
        new_cfg["voice_profile"] = {"enabled": False}
        logger.info(
            "[lang_voice_route] 音色未配置（%s）且无克隆档可回查 → "
            "按文本语种 %s 选 edge 音色 (%s)",
            repr(cur_voice or ""), prefix, mapped)
        return new_cfg, prefix

    cur_prefix = edge_voice_lang_prefix(cur_voice)
    if cur_prefix == prefix:
        return None

    mapped = _mapped_voice(follow, prefix)
    if not mapped:
        # 拒发守卫：语种明确但无音色可映射 → 宁缺毋滥，让调用方回落文字。
        if bool(follow.get("reject_unmapped", True)):
            logger.info(
                "[lang_voice_route] 文本语种 %s 无音色映射（当前 %s）→ 拒发语音回落文字",
                prefix, cur_voice or "-")
            return dict(voice_cfg or {}), f"{REJECT_TAG_PREFIX}{prefix}"
        return None

    new_cfg = dict(voice_cfg or {})
    new_cfg["backend"] = "edge_tts"
    new_cfg["voice"] = mapped
    new_cfg["fallback_voice"] = mapped
    new_cfg.pop("rvc", None)
    new_cfg["voice_profile"] = {"enabled": False}
    logger.info(
        "[lang_voice_route] 文本语种 %s ≠ 音色 %s → 切 edge_tts (%s)",
        prefix, cur_voice or "-", mapped)
    return new_cfg, prefix


def route_voice_cfg_for_text(
    voice_cfg: Dict[str, Any],
    text: str,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], str]:
    """按回复文本语言改写本次合成的 voice_cfg。

    返回 ``(voice_cfg, route_tag)``：
    - 未命中路由 → 原样返回、tag 空串；
    - 粤语路由命中 → 改写副本（backend/voice 切粤语 TTS）、tag="yue"；
    - follow_text 命中 → 改写副本（edge 音色对齐文本语种）、tag=语种前缀（如 "en"）；
    - 拒发守卫命中 → tag="reject:<lang>"，调用方应放弃语音回落文字；
    - 克隆链只对齐 fallback_voice 时 → 返回改写副本、tag 空串（主后端未变）；
    - 音色未配置（空串/占位串如 ``___``）→ 回查克隆档命中则恢复克隆主链
      （tag 空串），否则按语种选 edge 音色（tag=语种前缀）。
    绝不抛异常；任何异常按未命中处理。
    """
    try:
        rc = _route_cfg(config)
        if not rc.get("enabled", False):
            return voice_cfg, ""
        _record_stats("check")
        hit = _route_cantonese(voice_cfg, text, rc)
        if hit is None:
            # 通用语种克隆路由（粤语专线之后、edge 跟随之前）：专线最准（特征字
            # 检测）、克隆路由次之（同一把声讲外语）、edge 跟随兜底（换通用声）
            hit = _route_clone_lang(voice_cfg, text, rc)
        if hit is None:
            hit = _route_follow_text(voice_cfg, text, rc, config)
        if hit is not None:
            _record_stats("hit", hit[1])
            return hit
        return voice_cfg, ""
    except Exception:
        logger.debug("[lang_voice_route] 路由异常，按未命中处理", exc_info=True)
        return voice_cfg, ""


def _record_stats(kind: str, tag: str = "") -> None:
    """路由观测埋点（lang_route_stats 单例；best-effort，绝不影响路由本身）。"""
    try:
        from src.ai.lang_route_stats import get_lang_route_stats
        st = get_lang_route_stats()
        if kind == "check":
            st.record_check()
        elif is_reject_tag(tag):
            st.record_rejected(tag[len(REJECT_TAG_PREFIX):])
        elif tag:
            st.record_routed(tag)
        else:
            st.record_fallback_aligned()
    except Exception:
        pass


def is_reject_tag(tag: str) -> bool:
    """route tag 是否为「语言不匹配拒发」守卫命中。"""
    return str(tag or "").startswith(REJECT_TAG_PREFIX)


def fallback_reason_from_extra(extra: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """把合成结果 ``extra`` 归纳成前端可分支的回落原因枚举 ``(reason, lang)``。

    P0 2026-08-31（skuio 0831 提示风暴复盘）：语种能力闸把日语刻意改道 edge
    标准声后，前端只拿得到 ``fallback_from`` 一个布尔级事实，于是把「刻意的
    语言路由」也播报成「克隆音色暂不可用/通道中断→重试/报障」——归因错误
    直接制造无效工单（实测通道当时全绿）。原因在管线 extras 里本来就有
    （``clone_lang_blocked``/``primary_error``），只是没转发；本函数是唯一
    归纳口，tts-test 与 send-voice 两路由同源消费。

    取值：``lang_unsupported``（语种超出克隆主路能力，lang=被拦语种前缀，
    **保护性改道非故障**）/ ``quota``（Token 钱包耗尽降级）/ ``channel_down``
    （克隆后端不可达或合成失败）/ ``voice_name_mapped``（音色名不合法被映射成
    通用音色，无回落链）。判不出返回 ``("", "")``＝前端走旧通用文案，不猜。
    纯函数、绝不抛。
    """
    ex = extra if isinstance(extra, dict) else {}
    lang = str(ex.get("clone_lang_blocked") or "").strip()
    perr = str(ex.get("primary_error") or "").strip()
    if not lang and perr.startswith("clone_lang_unsupported:"):
        lang = perr.split(":", 1)[1].strip()
    if lang:
        return "lang_unsupported", lang
    if perr == "token_wallet_exhausted":
        return "quota", ""
    if ex.get("fallback_from"):
        return "channel_down", ""
    if ex.get("voice_mapped_from"):
        return "voice_name_mapped", ""
    return "", ""


__all__ = [
    "CLONE_ENGINE_LANGS",
    "EDGE_VOICE_BY_LANG",
    "REJECT_TAG_PREFIX",
    "clone_lang_gate",
    "clone_route_langs",
    "clone_voice_langs",
    "count_cantonese_markers",
    "default_edge_voice_for_lang",
    "detect_text_lang",
    "edge_voice_lang_prefix",
    "fallback_reason_from_extra",
    "hub_engine_for_lang",
    "is_cantonese_text",
    "is_clone_backend",
    "is_reject_tag",
    "is_voice_placeholder",
    "to_simplified_for_tts",
    "route_voice_cfg_for_text",
]
