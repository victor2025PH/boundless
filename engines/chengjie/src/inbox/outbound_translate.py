"""全自动出站翻译（L2 autosend 投递前把 AI 中文回复译成客户语言）。

补「全自动聊天翻译」闭环的最后一环：此前 autosend worker 把 AI 生成的（中文）草稿
**原样**投递到客户平台——外语客户会直接收到中文。本模块在投递前把草稿文本经统一
``TranslationService``（术语表 + TM + 语检 + 多引擎 failover）译成会话客户语言，并记录
出向译文映射（供 thread 双行展示），译完再交回 worker 真发。

设计：
  - **纯决策函数**（``normalize_target`` / ``should_translate`` / ``parse_outbound_translate_cfg``）
    零副作用、可单测，路由/worker 只做薄适配。
  - ``translate_outbound_text`` 是「译 + 记录 + 降级回落」的可复用闭包体，依赖通过参数注入
    （translation_service / store），单测可塞 fake。
  - **无兜底纪律（2026-08-17 起，D-M3 2026-09-06 收口）**：翻译失败 / 校验失败 /
    目标语言判不出 → 一律返回 ``None``（HOLD 信号，原因挂 ``item["_xlate_hold"]``），
    由 worker 转成投递失败走审计/提醒链，**任何情况不发原文、不落到操作员语言 zh**。
    目标语言由 ``resolve_outbound_lang`` 五级单点决策（手动 > 档案 > 消息 > 出站历史 > 人设）。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_SOURCE = "zh"
# 不可作为翻译目标的「空/未知」语言标记（与 translation_service.normalize_lang 对齐）
_SKIP_TARGETS = {"", "unknown", "und", "auto"}

# CJK 文字（汉字 + 假名 + 谚文）：出站语言错配判定用。
# 事故背景（2026-07-31，198）：『是Steven，别担心。😊』被 detect_language 判成 en
# （汉字 4 < 拉丁 6）→「已是客户语言」跳过翻译 → 中文原样发给英文客户。
# 「文本里有没有 CJK」是确定性信号，不受检测器计票规则影响，作为跳过护栏的硬否决。
_CJK_TEXT_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
# yue/zh-tw 是一等 CJK 目标语（2026-08-29）：normalize_target 对中文变体不再折叠，
# 漏加会让「CJK 文本 → 粤语/繁体」被 lang_gate/voice_peer_lang_conflict 误判成
# 「中文发非 CJK 客户」而 409 拦截。
_CJK_LANGS = {"zh", "zh-tw", "ja", "ko", "yue"}

# 中文变体家族（互译时译文==原文仍可读，见 translate_outbound_text 的豁免注释）。
# 刻意不含 ja/ko：日/韩目标原样回吐＝没翻，客户读不懂，照 HOLD。
_ZH_FAMILY_TARGETS = {"zh", "zh-tw", "yue"}


def contains_cjk(text: str) -> bool:
    """文本是否含任何 CJK 文字（汉字/假名/谚文）。"""
    return bool(_CJK_TEXT_RE.search(str(text or "")))


_LATIN_COUNT_RE = re.compile(r"[A-Za-z]")


def cjk_substantial(text: str) -> bool:
    """CJK 是否构成文本的**实质内容**（语言硬闸的冲突判定口径）。

    「含任何 CJK 即冲突」会误伤合法引用（P1-198 复盘扫描在生产数据实锤：
    英文消息引用中文专名「村BA」——1 个汉字 / ~40 个拉丁字母——被标成错配；
    照旧口径硬闸会把这类消息误翻译甚至误 HOLD）。量化口径：
      - CJK 字符 ≥2（单字引用/emoji 混杂永不冲突），且
      - 占「CJK+拉丁」语言性字符 ≥25%，或绝对数 ≥8（长文里整句中文）。
    校准锚点：7/31 实锤『是Steven，别担心。😊』cjk=4/latin=6（40%）命中；
    「Just read about 村BA's grassroots heart…」（1/40≈2%）放行。
    """
    t = str(text or "")
    cjk = len(_CJK_TEXT_RE.findall(t))
    if cjk < 2:
        return False
    if cjk >= 8:
        return True
    latin = len(_LATIN_COUNT_RE.findall(t))
    total = cjk + latin
    return total > 0 and (cjk / float(total)) >= 0.25


def lang_is_cjk(lang: str) -> bool:
    """语言码（归一化后）是否为 CJK 语种。未知/空 → False。"""
    return normalize_target(lang) in _CJK_LANGS

# 会话语言多数决：取最近 N 条**入站**消息按新近度加权投票的窗口大小与最小样本长度。
# 单条孤立外语消息（如中文客户偶尔蹦一句英文）不足以翻转整窗多数 → 目标语言稳定，
# 修「一条英文消息把 conversations.language 标成 en → 中文回复被误翻成英文 garble
# 且语音因译文超长静默回落」的根因。
_LANG_VOTE_WINDOW = 12
_LANG_VOTE_MIN_CHARS = 2

# 语言证据剥离（P0-198，2026-08-03）：投票检测前先剥掉系统注入/语言中性内容——
# emoji 加注「（表情：中文语义）」、识图/贴纸描述「[表情] 笑哭了」、haha/ok 填充词。
# 裸文本检测曾把英文会话投成 zh（「Haha 🤣（表情：笑得满地打滚）」8 个中文字 >
# 4 个拉丁字母）→ 中文草稿被判「已是客户语言」原样发出。与 lang_policy 证据链同源。
try:
    from src.ai.lang_policy import strip_neutral_tokens as _strip_evidence
except Exception:  # pragma: no cover - 极端导入失败退回裸文本（旧行为）
    _strip_evidence = None


def parse_outbound_translate_cfg(config: Any) -> Dict[str, Any]:
    """读 config.inbox.l2_autosend.translate → {enabled, source_lang, style}。缺省全关。"""
    tr = (((config or {}).get("inbox", {}) or {}).get("l2_autosend", {}) or {}
          ).get("translate", {}) or {}
    return {
        "enabled": bool(tr.get("enabled", False)),
        "source_lang": str(tr.get("source_lang") or _DEFAULT_SOURCE).strip().lower(),
        "style": str(tr.get("style") or "chat"),
    }


# 繁体变体保留语义（不折叠成 zh）：否则 should_translate 里 zh→zh-tw 恒判同语跳过，
# autosend 链的简→繁翻译永远不会发生。zh-hk/zh-hant 归一到 zh-tw（与
# translation_service.normalize_lang 同口径）。
_ZH_VARIANTS = {"zh-tw", "zh-hk", "zh-hant"}


def normalize_target(lang: str) -> str:
    """归一化语言码：zh-CN → zh、zh-HK/zh-Hant → zh-tw；空/未知/auto → ""。"""
    low = str(lang or "").strip().lower()
    if low in _SKIP_TARGETS:
        return ""
    if low in _ZH_VARIANTS:
        return "zh-tw"
    return low.split("-")[0]


def should_translate(text: str, target_lang: str, source_lang: str) -> bool:
    """是否需要翻译：有正文 + 目标语言有效 + 目标 != 源。否则跳过（发原文）。"""
    if not str(text or "").strip():
        return False
    tgt = normalize_target(target_lang)
    if not tgt:
        return False
    return tgt != normalize_target(source_lang)


def vote_language(
    messages: List[Dict[str, Any]],
    *,
    detect: Any,
    window: int = _LANG_VOTE_WINDOW,
    min_chars: int = _LANG_VOTE_MIN_CHARS,
    direction: str = "in",
) -> str:
    """从最近若干条消息按新近度加权投票，得出会话主语言（纯函数）。

    - 默认只看入站（``direction="in"``）——出站是我们自己发的，不能拿来判**客户**语言。
      ``direction="out"`` 时改为只对出站投票：衡量「我们一直在用什么语言聊」，
      作为客户证据缺位时 CJK 冲突判定的**独立参照**（P1-198，2026-08-03）。
    - 语音/图片等媒体消息若已被转写/识别补全（``text`` 非占位）同样计入，让「客户一直发
      中文语音」被正确判为 zh，而非被一条外插文字带偏。
    - **只对语言证据文本检测**（P0-198）：系统注入的中文（emoji 加注/识图描述/占位）
      与 haha/ok 类中性词先剥离；剥空＝本条不构成证据，不投票。
    - 越新的消息权重越高（线性递减），且按**证据文本长度**加权——孤立短外语词很难翻转整窗。
    - 每条对 ``detect`` 得到的语言归一化后累加权重，取最高者；``unknown`` 不计。
    - 无有效样本 → 返回 ""（调用方回落 store 持久 language）。
    """
    if not messages:
        return ""
    recent = [m for m in messages if isinstance(m, dict)][-max(1, int(window)):]
    scores: Dict[str, float] = {}
    n = len(recent)
    want_out = str(direction or "in") == "out"
    for idx, m in enumerate(recent):
        d = str(m.get("direction") or "in")
        if want_out:
            if d != "out":
                continue
        elif d == "out":
            continue
        text = str(m.get("text") or "").strip()
        # 跳过纯媒体占位（未转写）：[语音] [图片] [媒体] 等
        if not text or (text.startswith("[") and text.endswith("]") and " " not in text):
            continue
        core = text
        if _strip_evidence is not None:
            try:
                core = (_strip_evidence(text) or "").strip()
            except Exception:
                core = text
        if len(core) < int(min_chars):
            continue
        try:
            lang = normalize_target(detect(core))
        except Exception:
            continue
        if not lang:
            continue
        # #139 混排修正（与 lang_policy.classify_evidence 同口径）：中文句夹
        # 拉丁品牌/套餐词（『你帮我看下 SMART 的 unli data promo』）会被 detect
        # 按「拉丁 > 汉字」机械判 en——画像投票跟着记 en 正是「中文客户被判
        # en」工单的第二半（第一半在生成端 classify_evidence）。
        if lang == "en":
            try:
                from src.ai.lang_policy import latin_mixed_zh
                if latin_mixed_zh(core):
                    lang = "zh"
            except Exception:  # pragma: no cover - 极端导入失败保持旧行为
                pass
        # 新近度权重（越靠后越大）× 长度权重（越长越可信，封顶避免长文一票独大）
        recency = 1.0 + idx / max(1, n - 1)
        length_w = min(4.0, len(core) / 20.0 + 0.5)
        scores[lang] = scores.get(lang, 0.0) + recency * length_w
    if not scores:
        return ""
    return max(scores.items(), key=lambda kv: kv[1])[0]


def current_inbound_burst_lang(
    messages: List[Dict[str, Any]], *, max_msgs: int = 6,
) -> str:
    """「客户此刻在说什么语言」——末尾连续入站段（burst）的强证据语言（纯函数）。

    多数决（``vote_language``）天生滞后：客户刚切换语言的那一轮强证据会被历史多数
    压过——生成端（lang_policy 强证据立即跟随）已按新语言拟稿，翻译端却按旧窗口
    多数把它整段翻回去，**两个脑子打架且翻译端永远赢**。2026-08-15 messenger 实锤
    （Wisley 会话）：老板发中文「记录了」，草稿正确生成中文，出站翻译按窗口多数
    en 把它翻成英文发出。

    口径与 lang_policy 对齐：只取**末尾连续入站**消息（撞到出站即停——更早的入站
    属于上一轮对话，归多数决管），**由新到旧逐条**判级（``classify_evidence``），
    返回第一条强证据消息的语言——与生成端「当条强证据立即跟随」同拍。**刻意不拼接
    整个 burst 再判**：混排拼接会让 detect 按字符多数稀释掉最新那条的切换信号
    （「英文长句 + 记录了」拼起来判 en，恰好复刻要修的病）。中性/弱证据（emoji、
    「ok」、孤立短拉丁）跳过继续往前找；整段无强证据返回 ""，调用方回落多数决
    ——语言稳定性与 garble 护栏语义不受影响。

    入参 ``messages`` 按 ts 升序（``list_recent_messages`` 契约）。
    """
    if not messages:
        return ""
    try:
        from src.ai.lang_policy import EvidenceStrength, classify_evidence
    except Exception:  # pragma: no cover - 极端导入失败按无证据处理
        return ""
    scanned = 0
    for m in reversed(messages):
        if not isinstance(m, dict):
            continue
        if str(m.get("direction") or "in") == "out":
            break
        text = str(m.get("text") or "").strip()
        # 纯媒体占位（[语音]/[图片]）不构成语言证据，但也不打断 burst
        if not text or (text.startswith("[") and text.endswith("]") and " " not in text):
            continue
        scanned += 1
        try:
            lang, strength = classify_evidence(text)
        except Exception:  # pragma: no cover - 单条检测失败跳过不阻断
            logger.debug("[outbound_translate] burst 单条判级失败", exc_info=True)
            lang, strength = "", ""
        if lang and strength == EvidenceStrength.STRONG:
            return normalize_target(lang)
        if scanned >= max(1, int(max_msgs)):
            break
    return ""


def _conv_language(store: Any, conversation_id: str, *, detect: Any = None) -> str:
    """best-effort 取会话客户语言。

    三层：① 末尾入站 burst 强证据（客户**此刻**在说什么——与生成端 lang_policy
    「强证据立即跟随」同拍，防翻译端拿历史多数推翻生成端的正确选择）→ ② 最近
    入站**加权多数决**（抗「偶发一条外语消息翻转会话语言」）→ ③ 回落
    ``conversations.language`` 持久值（旧行为）。失败 → ""。
    """
    if store is None or not conversation_id:
        return ""
    if detect is not None and hasattr(store, "list_recent_messages"):
        try:
            recent = store.list_recent_messages(
                conversation_id, limit=_LANG_VOTE_WINDOW) or []
            burst = current_inbound_burst_lang(recent)
            voted = vote_language(recent, detect=detect)
            if burst and voted and burst != voted:
                # 客户刚切换语言、历史多数还没跟上——正是本层存在的意义，留痕便于归因
                logger.info(
                    "[outbound_translate] burst 语言覆盖多数决 conv=%s burst=%s voted=%s",
                    conversation_id, burst, voted,
                )
            if burst:
                return burst
            if voted:
                return voted
        except Exception:
            logger.debug("[outbound_translate] 语言多数决失败 conv=%s",
                         conversation_id, exc_info=True)
    # 回落 store 持久 language
    try:
        conv = store.get_conversation(conversation_id)
    except Exception:
        logger.debug("[outbound_translate] 读会话语言失败 conv=%s", conversation_id, exc_info=True)
        return ""
    return str((conv or {}).get("language") or "")


def _detect_source(translation_service: Any, text: str) -> str:
    """检测文本真实源语言（归一化）；检测器缺失/异常 → ""（表示未知）。"""
    fn = getattr(translation_service, "detect_language", None)
    if fn is None:
        return ""
    try:
        return normalize_target(fn(text))
    except Exception:
        logger.debug("[outbound_translate] detect_language 异常", exc_info=True)
        return ""


# ── M-1 B（#234，D-M3，2026-09-06）：目标语言决策单一函数 ─────────────────────────
# 事故：Messenger 客户发 "Hi"，账号数据清除后会话语言记忆清零、单句 "Hi" 判不出 en →
# 目标语言落空 → 走「目标未知→发原文」→ 中文草稿直投外国客户。修法：目标语言按
# 「会话手动翻译设置 > 客户档案语言 > 客户消息文字系统 > 人设对外默认语言 > 全无」
# 五级单点决策；全无 → 返回 ""（调用方 HOLD，禁止自动投递，绝不落到操作员界面语言）。
_SCRIPT_RES = (
    ("ja", re.compile(r"[\u3040-\u30ff]")),                 # 假名（先于汉字：日文夹汉字）
    ("ko", re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")),    # 谚文
    ("th", re.compile(r"[\u0e00-\u0e7f]")),                 # 泰文
    ("zh", re.compile(r"[\u4e00-\u9fff\uf900-\ufaff]")),    # 汉字
    ("ru", re.compile(r"[\u0400-\u04ff]")),                 # 西里尔
    ("ar", re.compile(r"[\u0600-\u06ff]")),                 # 阿拉伯
    ("he", re.compile(r"[\u0590-\u05ff]")),                 # 希伯来
    ("hi", re.compile(r"[\u0900-\u097f]")),                 # 天城文
    ("el", re.compile(r"[\u0370-\u03ff]")),                 # 希腊
)
_LATIN_SCRIPT_RE = re.compile(r"[A-Za-z\u00c0-\u024f]")
_VIET_MARK_RE = re.compile(r"[ăâđêôơưĂÂĐÊÔƠƯạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]")
# 拉丁字母书写的语种（人设对外语言是其中之一时，拉丁单句按人设语言兜底而非恒 en）
_LATIN_LANGS = {"en", "es", "pt", "fr", "de", "it", "nl", "id", "ms", "tl", "vi", "tr",
                "pl", "sv", "da", "no", "fi", "cs", "ro", "hu"}


def script_language(text: str, *, latin_default: str = "en") -> str:
    """单句按**文字系统**判语种（纯函数）：假名→ja、谚文→ko、泰文→th、汉字→zh、西里尔→ru、
    阿拉伯→ar、希伯来→he、天城文→hi、希腊→el；拉丁字母→越南语变音符→vi，否则
    ``latin_default``（人设对外语言为拉丁语种时用它，否则 en）。无任何文字 → ""。

    与 ``detect_language``（统计检测，单词 "Hi" 判不出）互补：这是 D-M3 第三级
    「客户消息文字系统」的兜底口径——「客户用拉丁字母跟我说话，回英文/人设语言」
    比「判不出就发中文」正确得多。
    """
    t = str(text or "")
    if not t.strip():
        return ""
    counts = {code: len(rx.findall(t)) for code, rx in _SCRIPT_RES}
    latin = len(_LATIN_SCRIPT_RE.findall(t))
    best_code, best_n = "", 0
    for code, n in counts.items():
        if n > best_n:
            best_code, best_n = code, n
    if counts.get("ja", 0) > 0 and best_code == "zh":
        best_code = "ja"   # 假名在场即日文（日文汉字比例常高于假名）
    if best_n > 0 and best_n >= latin:
        return best_code
    if latin > 0:
        if _VIET_MARK_RE.search(t):
            return "vi"
        ld = normalize_target(latin_default)
        return ld if ld in _LATIN_LANGS else "en"
    return ""


def persona_default_lang(cfg_root: Any, platform: str, account_id: str, chat_key: str) -> str:
    """人设「回复语言」（persona.language：'' 跟随对方 / zh / en / th…）→ 归一语种码；
    未绑定人设 / 跟随对方 / 任何异常 → ""。这是 D-M3 第四级「人设/账号对外默认语言」。"""
    try:
        if not isinstance(cfg_root, dict) or not cfg_root:
            return ""
        from src.ai.persona_voice import resolve_effective_persona_id
        from src.utils.persona_manager import PersonaManager
        pid = resolve_effective_persona_id(
            cfg_root, str(platform or ""), str(account_id or "default"), str(chat_key or ""))
        if not pid:
            return ""
        p = PersonaManager.get_instance().get_persona_by_id(pid) or {}
        return normalize_target(str(p.get("language") or ""))
    except Exception:
        logger.debug("[outbound_translate] 人设对外语言读取失败", exc_info=True)
        return ""


def _profile_language(store: Any, contacts_store: Any, conversation_id: str) -> str:
    """客户档案语言（D-M3 第二级）＝ contacts.language_hint（运营/导入显式写的），
    经 conversations.contact_id 反查；任一环缺失 → ""。刻意**不**把自动检测落库的
    ``conversations.language`` 当档案——那是消息证据的持久化，归第三级。"""
    if store is None or contacts_store is None or not conversation_id:
        return ""
    try:
        conv = store.get_conversation(conversation_id) or {}
    except Exception:
        return ""
    contact_id = str((conv or {}).get("contact_id") or "").strip()
    if not contact_id or not hasattr(contacts_store, "get_contact"):
        return ""
    try:
        c = contacts_store.get_contact(contact_id)
    except Exception:
        return ""
    lang = normalize_target(str(getattr(c, "language_hint", "") or ""))
    return lang


def _latest_inbound_text(store: Any, conversation_id: str) -> str:
    """最近一条有正文的入站消息（文字系统兜底用）；取不到 → ""。"""
    if store is None or not conversation_id or not hasattr(store, "list_recent_messages"):
        return ""
    try:
        rows = store.list_recent_messages(conversation_id, limit=_LANG_VOTE_WINDOW) or []
    except Exception:
        return ""
    for m in reversed(rows):
        if not isinstance(m, dict) or str(m.get("direction") or "in") == "out":
            continue
        text = str(m.get("text") or "").strip()
        if not text or (text.startswith("[") and text.endswith("]") and " " not in text):
            continue
        return text
    return ""


def resolve_outbound_lang(
    conversation_id: str,
    *,
    store: Any = None,
    detect: Any = None,
    contacts_store: Any = None,
    cfg_root: Any = None,
    platform: str = "",
    account_id: str = "",
    chat_key: str = "",
) -> Tuple[str, str]:
    """出站目标语言**单点决策**（D-M3）。返回 ``(语种码, decided_by)``；判不出 →
    ``("", "")``，调用方必须 HOLD（转半自动 / 禁止自动投递），**不得**回落 zh。

    decided_by ∈ ``manual``（会话「发→X」显式设置）/ ``profile``（contacts 档案语言）/
    ``message``（客户消息证据：末尾 burst 强证据 > 加权多数决 > 持久 conversations.language）/
    ``message_script``（单句文字系统兜底：拉丁→en/人设拉丁语、假名→ja…）/
    ``outbound_history``（我们一直在用什么语言聊）/ ``persona``（人设对外默认语言）。
    """
    cid = str(conversation_id or "")
    if not cid:
        return "", ""
    if platform == "" or chat_key == "":
        parts = cid.split(":", 2)
        if len(parts) == 3:
            platform = platform or parts[0]
            account_id = account_id or parts[1]
            chat_key = chat_key or parts[2]
    # ① 会话级手动翻译设置（'auto' = 跟客户语言 → 继续往下判）
    manual = ""
    try:
        if store is not None and hasattr(store, "get_outbound_lang_if_set"):
            manual = str(store.get_outbound_lang_if_set(cid) or "").strip().lower()
    except Exception:
        manual = ""
    if manual and manual != "auto":
        t = normalize_target(manual)
        if t:
            return t, "manual"
    # ② 客户档案语言
    prof = _profile_language(store, contacts_store, cid)
    if prof:
        return prof, "profile"
    # ③ 客户消息：统计证据 → 文字系统兜底
    if detect is None:
        try:
            from src.ai.translation_service import detect_language as detect
        except Exception:
            detect = None
    msg = normalize_target(_conv_language(store, cid, detect=detect))
    if msg:
        return msg, "message"
    persona = persona_default_lang(cfg_root, platform, account_id, chat_key)
    latest = _latest_inbound_text(store, cid)
    if latest:
        sc = script_language(latest, latin_default=persona or "en")
        if sc:
            return sc, "message_script"
    # ③′ 出站历史参照（客户零证据但我们一直在用某语言聊）
    hist = normalize_target(_outbound_history_language(store, cid, detect))
    if hist:
        return hist, "outbound_history"
    # ④ 人设 / 账号对外默认语言
    if persona:
        return persona, "persona"
    return "", ""


# ---------------------------------------------------------------------------
# Q-21 B（#302 / Y82GWM，2026-09-12）：起草前「会话语言计划」——单点、可追、可见
# ---------------------------------------------------------------------------
# Y82GWM：Messenger 客户发英文「Hi」，全自动起草了中文——生成侧 resolve_reply_language 的
# default 落到 lang_prior/zh，而出站侧 resolve_outbound_lang 早就能按拉丁文字系统判 en；两条
# 链各判各的，日志里没有任何一行说「这条稿为什么用这个语言」。本段把决策收成一份
# ``ConvLangPlan``：复用上面的六级（**顺序不改**）+ 客户明确语言请求 + 账号先验兜底，
# 一行 ``[lang-plan]`` 日志，人设产线的 reply_lang **只读它**；进程级注册表供 /api/drafts、
# 会话头「对方语言未知 · 按人设语言回」chip 读取（#302：lang_unknown 不再静默 L1）。
_PLAN_CONFIDENCE = {
    "manual": 1.0, "request": 0.95, "profile": 0.9, "message": 0.85, "message_script": 0.6,
    "outbound_history": 0.5, "persona": 0.4, "account_prior": 0.3, "unknown": 0.0,
}
#: 这些 decided_by 代表「对方语言有证据」；其余（persona / account_prior / unknown）＝对方语言未知
PLAN_PEER_EVIDENCE = frozenset({"manual", "request", "profile", "message", "message_script"})
_PLAN_REG: Dict[str, Tuple[Dict[str, Any], float]] = {}
_PLAN_REG_MAX = 2000
_PLAN_REG_TTL = 6 * 3600.0


@dataclass
class ConvLangPlan:
    conversation_id: str
    reply_lang: str          # 人设产线起草语言（"" = 判不出，产线按旧 default 起草）
    xlate_target: str        # 出站翻译目标（与 reply_lang 同源；出站链仍自行 resolve，此处只记录）
    tts_lang: str            # 语音合成语言（Q-22 消费；本批只透传）
    decided_by: str          # manual/request/profile/message/outbound_history/persona/account_prior/unknown
    confidence: float
    persona_lang: str = ""   # 人设对外语言（chip 文案「按人设语言（English）回」用）
    peer_known: bool = False  # 对方语言是否有证据
    via: str = ""            # 六级原始来源（message_script 等细分；decided_by 把 message_script 归并为 message）

    @property
    def fallback(self) -> bool:
        """对方语言未知、按人设/账号默认语言起草（会话头 + 稿头需标注）。"""
        return bool(self.reply_lang) and not self.peer_known

    def as_dict(self) -> Dict[str, Any]:
        return {
            "conversation_id": self.conversation_id, "reply_lang": self.reply_lang,
            "xlate_target": self.xlate_target, "tts_lang": self.tts_lang,
            "decided_by": self.decided_by, "confidence": round(float(self.confidence), 2),
            "persona_lang": self.persona_lang, "peer_known": bool(self.peer_known),
            "fallback": self.fallback, "via": self.via,
            # Q-26 conv_state._src_lang 读的键名（会话头状态带「对方语言未知 · 按人设语言回」）
            "peer_lang_unknown": self.fallback,
            "ts": int(time.time()),
        }


#: 落库键（Q-26 ``conv_state._LANG_PLAN_KEYS`` 第一候选；重启后会话头 / 稿头 chip 仍可读）
_PLAN_KV_KEY = "conv_lang_plan:{cid}"


def _plan_note(cid: str, plan: "ConvLangPlan", store: Any = None) -> None:
    if not cid:
        return
    now = time.time()
    d = plan.as_dict()
    _PLAN_REG[cid] = (d, now)
    if len(_PLAN_REG) > _PLAN_REG_MAX:
        cutoff = now - _PLAN_REG_TTL
        for k in [x for x, (_, ts) in _PLAN_REG.items() if ts < cutoff]:
            _PLAN_REG.pop(k, None)
        if len(_PLAN_REG) > _PLAN_REG_MAX:
            for k in sorted(_PLAN_REG, key=lambda x: _PLAN_REG[x][1])[: len(_PLAN_REG) - _PLAN_REG_MAX]:
                _PLAN_REG.pop(k, None)
    # 写穿到 app_settings KV：Q-26 状态带只认 KV；进程重启后 /api/drafts、会话头 chip 也还能读。
    # 只在有决策（reply_lang 非空）时落库——「unknown」计划落库会让状态带读到空语种。
    if store is not None and plan.reply_lang and hasattr(store, "set_app_setting"):
        try:
            import json as _json
            store.set_app_setting(_PLAN_KV_KEY.format(cid=cid), _json.dumps(d, ensure_ascii=False), "lang_plan")
        except Exception:
            logger.debug("[lang-plan] KV 落库失败（忽略）conv=%s", cid, exc_info=True)


def peek_conv_lang_plan(conversation_id: str, store: Any = None) -> Optional[Dict[str, Any]]:
    """读最近一次为该会话算出的语言计划（进程注册表 → 给了 store 再回落 KV；无 → None，绝不抛）。"""
    cid = str(conversation_id or "")
    try:
        rec = _PLAN_REG.get(cid)
        if rec:
            return dict(rec[0])
    except Exception:
        return None
    if store is None or not cid or not hasattr(store, "get_app_setting"):
        return None
    try:
        import json as _json
        raw = store.get_app_setting(_PLAN_KV_KEY.format(cid=cid), "")
        if isinstance(raw, str) and raw.strip().startswith("{"):
            d = _json.loads(raw)
            return dict(d) if isinstance(d, dict) and d else None
    except Exception:
        pass
    return None


def _reset_plan_registry_for_tests() -> None:
    _PLAN_REG.clear()


def build_conv_lang_plan(
    conversation_id: str,
    *,
    store: Any = None,
    cfg_root: Any = None,
    platform: str = "",
    account_id: str = "",
    chat_key: str = "",
    detect: Any = None,
    contacts_store: Any = None,
    history: Optional[List[Dict[str, Any]]] = None,
    log: bool = True,
) -> ConvLangPlan:
    """起草前算一次「这条会话该用什么语言回」，登记 + 一行 ``[lang-plan]`` 日志。

    决策：``resolve_outbound_lang`` 六级原样（① manual 最高，不改顺序）→ 若六级结果**不是**
    manual 且历史里有客户**明确语言请求**（「用日语聊吧」/ please speak japanese，
    ``lang_policy.latest_explicit_request``）→ 以请求为准（``request``）→ 六级全空时才补一级
    账号先验（``lang_prior``，``account_prior``）；仍空 → ``unknown``（reply_lang=""，产线按旧
    default 起草，chip 标「对方语言未知」）。任何异常 → unknown 计划，绝不阻断拟稿。
    """
    cid = str(conversation_id or "")
    if (platform == "" or chat_key == "") and cid.count(":") >= 2:
        parts = cid.split(":", 2)
        platform = platform or parts[0]
        account_id = account_id or parts[1]
        chat_key = chat_key or parts[2]
    lang, by = "", ""
    try:
        lang, by = resolve_outbound_lang(
            cid, store=store, detect=detect, contacts_store=contacts_store,
            cfg_root=cfg_root, platform=platform, account_id=account_id, chat_key=chat_key)
    except Exception:
        logger.debug("[lang-plan] resolve_outbound_lang 异常 conv=%s", cid, exc_info=True)
    persona = ""
    try:
        persona = persona_default_lang(cfg_root, platform, account_id, chat_key)
    except Exception:
        persona = ""
    if by != "manual" and history:
        try:
            from src.ai.lang_policy import latest_explicit_request
            req = normalize_target(latest_explicit_request(history) or "")
            if req:
                lang, by = req, "request"
        except Exception:
            pass
    if not lang:
        try:
            from src.ai.lang_prior import initial_lang_hint
            prior = normalize_target(initial_lang_hint(
                platform=platform, account_id=account_id, chat_key=chat_key,
                config=cfg_root if isinstance(cfg_root, dict) else {}) or "")
            if prior:
                lang, by = prior, "account_prior"
        except Exception:
            pass
    via = by or "unknown"
    by = "message" if via == "message_script" else via   # 文字系统兜底仍是「客户消息证据」
    plan = ConvLangPlan(
        conversation_id=cid, reply_lang=lang, xlate_target=lang, tts_lang=lang,
        decided_by=by, confidence=_PLAN_CONFIDENCE.get(via, 0.0),
        persona_lang=persona, peer_known=(by in PLAN_PEER_EVIDENCE), via=via,
    )
    _plan_note(cid, plan, store=store)
    if log:
        logger.info(
            "[lang-plan] conv=%s reply=%s xlate=%s tts=%s by=%s via=%s conf=%.2f persona=%s fallback=%d",
            cid, plan.reply_lang or "-", plan.xlate_target or "-", plan.tts_lang or "-",
            plan.decided_by, via, plan.confidence, persona or "-", 1 if plan.fallback else 0)
    return plan


def _outbound_history_language(store: Any, conversation_id: str, detect: Any) -> str:
    """「我们最近**已投递**的消息是什么语言」——客户证据缺位时的独立参照（P1-198）。

    与生成端决策完全解耦：已发出的历史是客户实际收到的地面真相，不受本轮
    reply_lang 判定对错影响。仅用于 CJK 冲突判定（待发文本含 CJK 而我们一直
    用非 CJK 语言在聊 → 大概率是错语言草稿），不作为常规翻译目标。
    """
    if store is None or not conversation_id or detect is None:
        return ""
    if not hasattr(store, "list_recent_messages"):
        return ""
    try:
        recent = store.list_recent_messages(
            conversation_id, limit=_LANG_VOTE_WINDOW) or []
        return vote_language(recent, detect=detect, direction="out")
    except Exception:
        logger.debug("[outbound_translate] 出站历史语言参照失败 conv=%s",
                     conversation_id, exc_info=True)
        return ""


def peer_language_hint(store: Any, conversation_id: str, *, detect: Any = None) -> str:
    """best-effort「该用什么语言跟这个客户说话」——生成侧单一事实源（P2-198）。

    组合三层证据（与出站翻译/硬闸同源）：
      ① 最近入站消息加权多数决（系统注入已剥离）；
      ② ``conversations.language`` 持久值（unknown/空 归一为 ""——**Telegram 的
        ingest 从不写该列**，旧消费方直接读它等于永远拿到 unknown，主动触达的
        prompt 语言硬约束因此从未生效过，生产实锤 telegram:8244899… 30 天收到
        6 条中文晨安）；
      ③ 出站历史参照（「我们一直在用什么语言聊」）。
    全落空 → ""（调用方按「语言未知」处理）。

    注意：出站翻译的 target 解析**刻意不含**第 ③ 层常规化（那边只在 CJK 冲突
    判定时用出站参照，不作常规翻译目标）——本函数是给 prompt 生成侧用的提示，
    错了最多措辞语言保守，不会触发翻译动作，故三层可以都上。
    """
    if store is None or not conversation_id:
        return ""
    if detect is None:
        try:
            from src.ai.translation_service import detect_language as detect
        except Exception:
            detect = None
    lang = normalize_target(_conv_language(store, conversation_id, detect=detect))
    if lang:
        return lang
    return normalize_target(
        _outbound_history_language(store, conversation_id, detect))


def _gate_record(outcome: str, *, conversation_id: str = "", target: str = "") -> None:
    """语言硬闸观测埋点（best-effort，任何异常绝不影响投递链）。"""
    try:
        from src.inbox.outbound_lang_stats import get_outbound_lang_stats
        get_outbound_lang_stats().record(
            outcome, conversation_id=conversation_id, target=target)
    except Exception:
        logger.debug("[outbound_translate] 硬闸埋点失败", exc_info=True)


def _report_block_safe(cid: str, target: str, reason: str) -> None:
    """HOLD → delivery_block 上报（弹窗+主机错误日志，best-effort 绝不抛）。"""
    try:
        from src.ops.delivery_block import report_block
        report_block(
            "translate", reason=str(reason or "hold")[:120],
            conversation_id=cid, detail=f"target={target}")
    except Exception:
        logger.debug("[outbound_translate] delivery_block 上报失败", exc_info=True)


# 无可译内容（纯 emoji/标点/数字/空白，无任何文字系统字符）→ 翻译是无操作，
# 直接放行不算兜底（严格 HOLD 化后若不放行，「👍」会被引擎原样回显再被
# degraded 判定误拦）。字母表覆盖：拉丁(含扩展)/西里尔/阿拉伯/泰/希伯来/
# 天城文/CJK/假名/谚文。
_TRANSLATABLE_RE = re.compile(
    r"[A-Za-z\u00C0-\u024F\u0370-\u03FF\u0400-\u04FF\u0590-\u05FF"
    r"\u0600-\u06FF\u0900-\u097F\u0E00-\u0E7F\u3040-\u30FF"
    r"\u4E00-\u9FFF\uF900-\uFAFF\uAC00-\uD7AF]")

# ── 实施93b：URL 保护（翻译前掩码 / 译后还原） ────────────────────────────
# 动机：CTA 追踪短链（/r/{token}）随自动跟进追加进待译文本后，翻译引擎
# （尤其 LLM 系）可能改写/转写/吞掉 URL——链接错一个字符就是死链，整条
# 引导白发。占位 token 选 [[[L0]]] 形态：纯 ASCII 大写+三重方括号，NMT 与
# LLM 引擎实测几乎必然原样保留，且不会与自然语言/markdown 撞形。
_URL_RE = re.compile(r"https?://[^\s<>\"'）】\]]+", re.IGNORECASE)
_URL_TOKEN_RE = re.compile(r"\[\[\[L(\d{1,2})\]\]\]")


def _protect_urls(text: str) -> Dict[str, Any]:
    """把文本中的 URL 替换为 [[[Ln]]] 占位。无 URL 返回空 dict（调用方零开销路径）。"""
    urls: List[str] = []

    def _sub(m: "re.Match[str]") -> str:
        urls.append(m.group(0))
        return f"[[[L{len(urls) - 1}]]]"

    masked = _URL_RE.sub(_sub, text or "")
    if not urls:
        return {}
    return {"masked": masked, "urls": urls}


def _strip_url_tokens(masked: str) -> str:
    """去掉占位 token 后的剩余文本（用于判断「除链接外还有没有可译内容」）。"""
    return _URL_TOKEN_RE.sub(" ", masked or "")


def _restore_urls(translated: str, url_map: Dict[str, Any]) -> str:
    """把译文中的占位 token 还原为原 URL；被引擎吞掉的占位对应 URL 追加到文末。

    追加而非 HOLD：链接必达是 CTA 链路的硬要求，消息其余部分已是合格译文，
    因为一个被吞的占位扣下整条消息，比「链接挪到末尾」代价更大。
    """
    urls: List[str] = list(url_map.get("urls") or [])
    seen: set = set()

    def _sub(m: "re.Match[str]") -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(urls):
            seen.add(idx)
            return urls[idx]
        return m.group(0)  # 越界 token（引擎幻觉造的）原样留着，可见即可查

    restored = _URL_TOKEN_RE.sub(_sub, translated or "")
    lost = [u for i, u in enumerate(urls) if i not in seen]
    if lost:
        restored = restored.rstrip() + "\n" + "\n".join(lost)
    return restored


def parse_outbound_lang_gate_cfg(config: Any) -> Dict[str, Any]:
    """读 ``inbox.l2_autosend.lang_gate`` → ``{enabled}``。**默认开**。

    语言硬闸不是新功能子系统，是 2026-07-31 CJK HOLD 安全不变量向「未开启
    出站翻译的部署」的延伸——那类部署此前对「中文草稿发给外语客户」零防护
    （护栏活在翻译回调里，翻译一关护栏一起没了）。默认开、只拦 CJK↔非 CJK
    的高置信冲突（不碰 en/es 之类的低置信差异），``enabled: false`` 是逃生门。
    兼容 ``lang_gate: false`` 布尔简写。
    """
    lg = (((config or {}).get("inbox", {}) or {}).get("l2_autosend", {}) or {}
          ).get("lang_gate", {})
    if isinstance(lg, bool):
        return {"enabled": lg}
    if not isinstance(lg, dict):
        lg = {}
    return {"enabled": bool(lg.get("enabled", True))}


def _mark_hold(item: Dict[str, Any], reason: str, *, target: str = "",
               decided_by: str = "", attempts: int = 0) -> None:
    """把 HOLD 原因挂回 item（M-1 B #234）：worker 据此把失败原因写成
    ``translate_hold:<reason>``（lang_unknown → 客户语言未知转人工；其余 → 翻译失败
    待确认），面板/审计/铃铛都能看到「为什么没发」。返回值仍是 None（旧调用方零变化）。
    ``attempts``（Q-39 B #326）＝HOLD 前一共打了几次引擎（首发 + 重试），worker 文案「已重试 N 次」。"""
    try:
        rec = {"reason": str(reason or "hold"), "target": str(target or ""),
               "decided_by": str(decided_by or "")}
        if int(attempts or 0) > 1:
            rec["attempts"] = int(attempts)   # 只在真重试过时带（M-1 B 三键契约不动）
        item["_xlate_hold"] = rec
    except Exception:
        pass


# ── Q-39 B（#326 THBSHN，2026-09-15）：空串 / 超时可恢复重试 + 目标语重起草 ─────────
# 实录：03:56 草稿中文、[lang-plan] reply=en，[xlate] provider=none err=ai:empty → 直接 HOLD；
# 03:53 同会话 deepseek timeout 31.6s，05:27 起翻译全好——一次抖动就把这条消息永远扣住。
# 修法（不动「不发原文」纪律）：① 同引擎重试 1 次（间隔 gap_sec）→ ② 换路由表下一引擎 1 次
# → ③ 仍空且会话全自动 → 按 lang-plan 目标语重起草一次（``redraft`` 回调由调用方注入）
# → 三步都不行才 HOLD，并写 xlate_hold_marker 让状态带出人话 + 「重试翻译」。
# ``lang_unknown`` / ``target_lang_mismatch`` / ``engine_refusal`` / ``cjk_residue`` 不重试
# （语种 / 内容判定，重打同一句只会得到同一个答案）。


def parse_xlate_retry_cfg(config: Any) -> Dict[str, Any]:
    """``inbox.l2_autosend.translate.retry`` → ``{enabled, gap_sec, redraft}``（默认全开 / 2s）。
    兼容 ``retry: false`` 布尔简写。"""
    try:
        tr = ((((config or {}).get("inbox") or {}).get("l2_autosend") or {})
              .get("translate") or {}).get("retry", {})
    except Exception:
        tr = {}
    if isinstance(tr, bool):
        return {"enabled": tr, "gap_sec": 2.0, "redraft": tr}
    if not isinstance(tr, dict):
        tr = {}
    try:
        gap = float(tr.get("gap_sec", 2.0))
    except (TypeError, ValueError):
        gap = 2.0
    return {"enabled": bool(tr.get("enabled", True)), "gap_sec": max(0.0, gap),
            "redraft": bool(tr.get("redraft", True))}


#: P0-3（#343 #326，2026-09-21）：低置信 HOLD 原因码（状态带 / 台账 / 「重试翻译」都认这一串）
LOW_CONF_REASON = "low_confidence"


def parse_xlate_min_confidence(config: Any) -> float:
    """``inbox.l2_autosend.translate.min_confidence`` → [0,1]；默认 ``TIER_LOW``（0.5）。

    ``translation_confidence`` 是确定性硬错信号（空 / 原样回吐 / 目标脚本缺失 / 长度离谱），
    只有明确的硬错才会砸破 0.5——门槛是「别把明显错的发出去」，不是语义质检。
    ``0`` = 关门（旧行为：只要引擎 ok 就发）。"""
    try:
        from src.ai.translation_confidence import TIER_LOW as _dflt
    except Exception:
        _dflt = 0.5
    try:
        tr = ((((config or {}).get("inbox") or {}).get("l2_autosend") or {})
              .get("translate") or {})
        v = float(tr.get("min_confidence", _dflt)) if isinstance(tr, dict) else float(_dflt)
    except (TypeError, ValueError, AttributeError):
        v = float(_dflt)
    return max(0.0, min(1.0, v))


def _result_confidence(res: Any) -> float:
    """结果上的确定性置信分；未评分（无字段 / 非数）→ -1.0（不进低置信门）。"""
    try:
        c = float(getattr(res, "confidence", -1.0))
    except (TypeError, ValueError):
        return -1.0
    return c if c == c else -1.0   # NaN → 未评分


def is_retryable_xlate_error(err: str) -> bool:
    """瓶颈抖动族（ai:empty / timeout / provider_unavailable / translate_exception…）→ True。"""
    e = str(err or "").strip()
    if not e:
        return False
    if e == "translate_exception":
        return True
    try:
        from src.ai.translation_service import TranslationService
        return bool(TranslationService.is_retryable_error(e))
    except Exception:
        tail = e.split(":", 1)[1].strip() if ":" in e else e
        return any(tail.startswith(t) for t in (
            "empty", "timeout", "Timeout", "provider_unavailable", "translate_failed",
            "unavailable", "Connect", "Connection", "ServerDisconnected"))


def _hold_marker_write(store: Any, cid: str, *, reason: str, target: str,
                       draft_id: str, attempts: int, text: str = "") -> None:
    try:
        from src.inbox.xlate_hold_marker import mark as _mark
        _mark(cid, reason=reason, target=target, draft_id=draft_id, attempts=attempts,
              store=store, text=text)
    except Exception:
        logger.debug("[xlate] hold marker 写入失败（忽略）", exc_info=True)


def _hold_marker_clear(store: Any, cid: str) -> None:
    try:
        from src.inbox.xlate_hold_marker import clear as _clear
        _clear(cid, store=store)
    except Exception:
        logger.debug("[xlate] hold marker 清除失败（忽略）", exc_info=True)


def _conv_is_auto(store: Any, cid: str) -> bool:
    try:
        if store is not None and cid and hasattr(store, "get_automation_mode"):
            return str(store.get_automation_mode(cid) or "") == "auto_ai"
    except Exception:
        pass
    return False


def redraft_output_ok(new_text: str, target: str, *, detect: Any = None) -> bool:
    """重起草产物能不能直接发：非空、目标非 CJK 时不得实质含 CJK、检测语种（可检时）须等于目标。"""
    t = str(new_text or "").strip()
    if not t:
        return False
    tgt = normalize_target(target)
    if not tgt:
        return False
    if not lang_is_cjk(tgt) and cjk_substantial(t):
        return False
    if detect is not None:
        try:
            det = normalize_target(str(detect(t) or ""))
        except Exception:
            det = ""
        if det and det not in _SKIP_TARGETS and det != tgt:
            # 中文变体家族互认（zh-tw / yue 检测恒回 zh）
            if not (det in _ZH_FAMILY_TARGETS and tgt in _ZH_FAMILY_TARGETS):
                return False
    return True


def _log_decision(cid: str, target: str, decided_by: str, action: str,
                  reason: str = "") -> None:
    logger.info(
        "[xlate] outbound conv=%s target=%s decided_by=%s action=%s%s",
        cid, target or "-", decided_by or "-", action,
        (" reason=" + reason) if reason else "")


def _note_target(item: Dict[str, Any], target: str, action: str = "") -> None:
    """把目标语言 / 决策动作挂回 item（O-1 B：出口后处理按客户语言选标点表；只读标注）。"""
    try:
        if target:
            item["_xlate_target"] = str(target)
        if action:
            item["_xlate_action"] = str(action)
    except Exception:
        pass


async def translate_outbound_text(
    item: Dict[str, Any],
    *,
    translation_service: Any,
    store: Any = None,
    source_lang: str = _DEFAULT_SOURCE,
    style: str = "chat",
    gate_only: bool = False,
    contacts_store: Any = None,
    cfg_root: Any = None,
    redraft: Any = None,
) -> Optional[str]:
    """出站文本终态口：翻译 / 语言硬闸（``_translate_outbound_core``）→ **确定性去 AI 标点 /
    句式后处理**（O-1 B · #253 #254 · D-O2，``outbound_humanize``）。

    ``redraft``（Q-39 B #326，可选）：``async (item, target_lang) -> str``——翻译三次都空且会话
    全自动时按目标语重起草一次；缺省 None＝没有这一步（直接 HOLD）。

    后处理挂在这里的理由：autosend 自动链 / 人工通过链 / deferred（关怀·唤醒·冲刺）/
    主动触达 / 协议直发 / 收口点铆定修正——**所有 AI 出站都先过本函数**（pass_gate_only /
    pass_same_lang / translated / 无可译内容 各返回路径统一在出口过一遍）。HOLD（None）不动。
    **绕过**：``item["origin"] ∈ {manual, verbatim, human}``（worker 人工通过稿显式标
    ``manual``）与 deferred 队列里 ``care:verbatim`` 原文行（``resolve_origin`` 只读反查）
    ——用户写的破折号是用户的。每条出站落一行 ``[outbound] conv=… punct_fix=n style_fix=n``。
    后处理任何异常 → 原样放行，绝不影响投递。
    """
    out = await _translate_outbound_core(
        item, translation_service=translation_service, store=store,
        source_lang=source_lang, style=style, gate_only=gate_only,
        contacts_store=contacts_store, cfg_root=cfg_root, redraft=redraft)
    if out is None or not str(out).strip():
        return out
    try:
        from src.inbox.outbound_humanize import apply_outbound_humanize, resolve_origin
        cid = str(item.get("conversation_id") or "")
        # 纯 emoji / 标点 / 数字（无文字系统字符）没有可去 AI 化的内容——按 identity 绕过只记日志
        origin = resolve_origin(item, store) if _TRANSLATABLE_RE.search(str(out)) else "identity"
        return apply_outbound_humanize(
            str(out), conversation_id=cid,
            lang=str(item.get("_xlate_target") or item.get("lang") or ""),
            origin=origin,
            cfg_root=(cfg_root if isinstance(cfg_root, dict) else None),
            stage=str(item.get("_xlate_action") or "xlate"))
    except Exception:
        logger.debug("[outbound_translate] 后处理异常（原样放行）", exc_info=True)
        return out


async def _translate_outbound_core(
    item: Dict[str, Any],
    *,
    translation_service: Any,
    store: Any = None,
    source_lang: str = _DEFAULT_SOURCE,
    style: str = "chat",
    gate_only: bool = False,
    contacts_store: Any = None,
    cfg_root: Any = None,
    redraft: Any = None,
) -> Optional[str]:
    """把一条待投递文本译成会话客户语言；记录出向译文映射。**自带「已是客户语言则跳过」护栏**。

    item: ``{conversation_id, text, ...}``（AutosendWorker 的 to_deliver 载荷 / deferred 主动触达）。
    返回**应真正发出的文本**：成功译则返回译文，一般情况失败回落原文（绝不抛）。

    关键设计——**先检测真实源语言再决定是否翻译**：陪伴回复栈（skill_manager / reactivation）
    多按客户语言直接生成，盲目按 config 源语言（如 zh）翻译会把已是客户语言的文本 garble。
    故：检测文本实际语言，若已等于目标语言 → 跳过；否则用**检测到的源语言**翻译（比 config 假定更准）。

    **CJK 冲突硬护栏（2026-07-31，198 实锤『是Steven，别担心。😊』原样发出）**：
    文本含 CJK 而目标语言是非 CJK 语种时——
      - 「检测语言==目标语言」的跳过护栏**失效**（混排短句检测不可信，含 CJK 即必须翻）；
      - 源语言取「检测到的 CJK 语种」（ja/ko 文本不误按 zh 翻），否则回落配置源（zh）；
      - 翻译异常/失败/译文仍含 CJK → 返回 ``None``（HOLD，别发）——发中文给外语客户
        是人设事故，比这条消息不发出去更糟。调用方（AutosendWorker）把 None 转成
        投递失败，走既有重试/审计/坐席提醒链。

    **gate_only 模式（P1-198，2026-08-03）**：``inbox.l2_autosend.translate.enabled=false``
    的部署此前对错语言草稿零防护（护栏活在翻译回调里，翻译一关护栏一起没了）。
    gate_only=True 时本函数只做「语言硬闸」：非 CJK 冲突一律原样放行（尊重运营
    关闭常规翻译的决定），CJK 冲突则照常抢救翻译 / HOLD。

    **目标语言单点决策（M-1 B #234，D-M3，2026-09-06）**：``resolve_outbound_lang`` 五级
    （会话手动 > 客户档案 > 客户消息证据/文字系统 > 出站历史 > 人设对外语言）；**全无 →
    HOLD**（``item["_xlate_hold"]={"reason":"lang_unknown"}``，返回 None）——旧「目标未知→
    发原文」拆除：15:44 Messenger 实锤就是这条路把中文直投给只说了一句 "Hi" 的客户。
    任何路径不得落到操作员界面语言 zh。校验失败（含 target_lang_mismatch）同样 HOLD 并
    带原因，worker 标「翻译失败待确认」，不发原文。
    """
    text = str(item.get("text") or "")
    cid = str(item.get("conversation_id") or "")
    if not text or translation_service is None:
        return text
    if not _TRANSLATABLE_RE.search(text):
        return text  # 纯 emoji/符号/数字：无可译内容，放行（非兜底，是无操作）

    _detect = getattr(translation_service, "detect_language", None)
    # B67（实施67 P1-8）：会话级「发→X」显式设置最优先——用户声明的意图不依赖
    # 检测/投票；具体语种=钉死目标；'auto'=显式要求跟客户语言。显式意图在场时
    # gate_only 升级为完整翻译——「发→英」开着却只拦 CJK 冲突＝手动才译的旧断链复发。
    explicit = ""
    try:
        if store is not None and cid and hasattr(store, "get_outbound_lang_if_set"):
            explicit = str(
                store.get_outbound_lang_if_set(cid) or "").strip().lower()
    except Exception:
        explicit = ""
    target, decided_by = resolve_outbound_lang(
        cid, store=store, detect=_detect, contacts_store=contacts_store,
        cfg_root=cfg_root, platform=str(item.get("platform") or ""),
        account_id=str(item.get("account_id") or ""),
        chat_key=str(item.get("chat_key") or ""))
    if explicit:
        gate_only = False   # 显式设置（含 auto）在场 → 完整翻译
    text_cjk = cjk_substantial(text)
    if not target:
        # D-M3：目标语言全无 → 转半自动，禁止自动投递（曾是 no_target_sent 盲发）
        _gate_record("held_lang_unknown", conversation_id=cid)
        _mark_hold(item, "lang_unknown")
        _report_block_safe(cid, "", "lang_unknown")
        _log_decision(cid, "", "", "hold", "lang_unknown")
        logger.warning(
            "[outbound_translate] 客户语言判不出（手动/档案/消息/人设全无）→ HOLD 不发，"
            "转人工确认 conv=%s cjk=%s", cid, text_cjk)
        return None

    # 冲突判定用「实质性 CJK」口径（cjk_substantial）：英文消息引用个别中文
    # 专名不算冲突（生产实锤误伤面，见函数 docstring）。
    cjk_conflict = text_cjk and not lang_is_cjk(target)
    _note_target(item, target)
    if gate_only and not cjk_conflict:
        _log_decision(cid, target, decided_by, "pass_gate_only")
        _note_target(item, target, "pass_gate_only")
        return text  # 硬闸模式：无冲突不翻译（运营已关闭常规出站翻译），免source检测
    detected = _detect_source(translation_service, text)
    if cjk_conflict:
        # 冲突态：跳过护栏失效；源语言优先信「检测到的 CJK 语种」，检测非 CJK
        # （混排误判）则回落配置源。
        eff_source = (detected if detected in _CJK_LANGS
                      else (normalize_target(source_lang) or _DEFAULT_SOURCE))
    else:
        # 检测命中目标语言即「文本已是客户语言」→ 跳过（防 garble，
        # 覆盖主动触达已 in-lang 的消息）
        eff_source = detected or normalize_target(source_lang) or source_lang
        if eff_source == target:
            _log_decision(cid, target, decided_by, "pass_same_lang")
            _note_target(item, target, "pass_same_lang")
            return text

    # 实施93b URL 保护：翻译前把 URL 换成引擎几乎必然原样保留的占位 token，
    # 译后还原——防翻译引擎改写/转写链接（CTA 追踪短链被改一个字符=死链，
    # 自动发送+自动翻译叠加后这是常态路径不是边缘）。纯 URL 消息直接跳过
    # 翻译（链接没有可译内容）；占位符被引擎吞掉时把对应 URL 追加到文末
    # （链接必达，位置降级可接受——比 HOLD 整条消息或发丢链版本都诚实）。
    _url_map = _protect_urls(text)
    if _url_map:
        text_masked = _url_map["masked"]
        if not _TRANSLATABLE_RE.search(_strip_url_tokens(text_masked)):
            return text          # 纯链接/链接+标点：无可译内容，原样放行
    else:
        text_masked = text

    _attempts = 1
    _exc_failed = False
    try:
        res = await translation_service.translate(
            text_masked, target_lang=target, source_lang=eff_source, style=style,
        )
    except Exception:
        # 无兜底纪律（2026-08-17 老板拍板）：翻译失败一律 HOLD 不发——旧「非 CJK
        # 冲突回落发原文」拆除（发客户看不懂的原文＝静默替代品，掩盖翻译链故障）。
        # Q-39 B：异常同属「引擎没回话」，先走下方同引擎 / 换引擎重试再 HOLD。
        logger.warning(
            "[outbound_translate] 翻译调用失败（先重试，仍败则 HOLD 不发）"
            "target=%s conv=%s", target, cid, exc_info=True)
        res = None
        _exc_failed = True

    translated = str(getattr(res, "translated_text", "") or "")
    provider = str(getattr(res, "provider", "") or "")
    err = str(getattr(res, "error", "") or "") or ("translate_exception" if _exc_failed else "")
    ok = bool(getattr(res, "ok", False))

    # 失败 / 空 / 与原文相同（provider=identity/none 或未真译）/ 译文仍**实质性**含 CJK
    # → 一律 HOLD 不发（2026-08-17 无兜底纪律：旧「非冲突态回落原文」拆除——发
    # 客户看不懂/未真译的文本＝静默替代品）。identity 回显正是 198 泄漏的机制。
    # 译文残留判定同用 cjk_substantial：好译文保留「村BA」这类专名引用不算失败。
    # 例外（2026-08-30 中文变体）：zh→zh-tw/yue 简繁同形句（「一起加油」逐字同形）
    # 转换后原样返回是**正确结果**而非未真译——原样发出客户完全可读，HOLD 反而
    # 误扣合法消息。豁免收窄到中文变体家族互译（ja/ko 目标原样回吐仍按未译 HOLD）。
    # 93b：质量判定全部在**掩码域**比较（translated vs text_masked）——URL 换
    # 占位不改变任何既有语义，无 URL 时 text_masked == text 逐字节旧行为。
    _same_text_ok = (translated == text_masked
                     and target in _ZH_FAMILY_TARGETS
                     and (eff_source in _ZH_FAMILY_TARGETS or not eff_source))
    def _usable(_t: str) -> bool:
        return bool(_t) and not (_t == text_masked and not _same_text_ok) \
            and not (cjk_conflict and cjk_substantial(_t))

    # ── P0-3（#343）：低置信门。引擎 ok 但确定性置信分 < min_confidence（目标脚本缺失 /
    # 原样回吐 / 长度离谱）→ 不当成功发，按「换引擎重译 → 重起草 → HOLD」走；同引擎不重打
    # （同一句同引擎大概率同一个错）。未评分（-1）的结果不进门（旧引擎 / 测试桩零变化）。
    _min_conf = parse_xlate_min_confidence(cfg_root)
    _conf = _result_confidence(res)

    def _conf_ok(_r: Any) -> bool:
        _c = _result_confidence(_r)
        return _min_conf <= 0.0 or _c < 0.0 or _c >= _min_conf

    _low_conf = bool(ok and translated and _usable(translated) and not _conf_ok(res))
    if _low_conf:
        logger.info("[xlate] low confidence conf=%.2f < %.2f provider=%s target=%s conv=%s → 重译",
                    _conf, _min_conf, provider or "-", target, cid)

    degraded = (not ok or not translated or not _usable(translated) or _low_conf)
    _decided_action = "translated"
    if degraded:
        _why = (LOW_CONF_REASON if _low_conf else
                (err or ("cjk_residue" if (cjk_conflict and cjk_substantial(translated))
                         else "translate_degraded")))
        # ── Q-39 B（#326）：可恢复族先重试，不改「不发原文」纪律 ──────────────
        _rt = parse_xlate_retry_cfg(cfg_root)
        _retryable = (_rt["enabled"] and hasattr(translation_service, "retry_once")
                      and (_low_conf or ((not ok or not translated)
                                         and is_retryable_xlate_error(_why))))
        if _retryable:
            import asyncio as _aio
            _failed_engine = provider if provider not in ("", "none", "identity", "license") else ""
            _steps = [] if _low_conf else [("same", _failed_engine)]
            try:
                _nxt = (translation_service.next_engine_after(_failed_engine, target)
                        if hasattr(translation_service, "next_engine_after") else "")
            except Exception:
                _nxt = ""
            if _nxt and _nxt != _failed_engine:
                _steps.append(("next", _nxt))
            elif _low_conf:
                logger.info("[xlate] low confidence 无可换引擎 conv=%s target=%s", cid, target)
            for _step, _eng in _steps:
                if _rt["gap_sec"] > 0 and _step == "same":
                    try:
                        await _aio.sleep(float(_rt["gap_sec"]))
                    except Exception:
                        pass
                _attempts += 1
                try:
                    _r = await translation_service.retry_once(
                        text_masked, target_lang=target, source_lang=eff_source,
                        style=style, engine=_eng)
                except Exception as _exc:  # noqa: BLE001
                    logger.debug("[xlate] retry step=%s engine=%s 异常", _step, _eng, exc_info=True)
                    err = f"{_eng or 'ai'}:{type(_exc).__name__}"
                    continue
                _t2 = str(getattr(_r, "translated_text", "") or "")
                if bool(getattr(_r, "ok", False)) and _usable(_t2) and _conf_ok(_r):
                    translated, provider = _t2, str(getattr(_r, "provider", "") or _eng or "")
                    ok, err, degraded, _low_conf = True, "", False, False
                    _conf = _result_confidence(_r)
                    logger.info(
                        "[xlate] retry ok step=%s engine=%s attempt=%d conv=%s target=%s（首发 err=%s）",
                        _step, provider or "-", _attempts, cid, target, _why)
                    _decided_action = f"translated_retry_{_step}"
                    break
                if bool(getattr(_r, "ok", False)) and _t2 and not _conf_ok(_r):
                    err = LOW_CONF_REASON
                else:
                    err = str(getattr(_r, "error", "") or err or "translate_failed")
                logger.info("[xlate] retry fail step=%s engine=%s attempt=%d err=%s conv=%s",
                            _step, _eng or "-", _attempts, err, cid)
            if degraded:
                _why = err or _why
        # ── ③ 仍空且全自动 → 按 lang-plan 目标语重起草一次（调用方注入 redraft）──
        if degraded and _retryable and _rt["redraft"] and redraft is not None \
                and str(item.get("origin") or "") not in ("manual", "human", "verbatim") \
                and _conv_is_auto(store, cid):
            try:
                _new = str(await redraft(item, target) or "").strip()
            except Exception:
                logger.debug("[xlate] redraft 异常", exc_info=True)
                _new = ""
            if _new and redraft_output_ok(_new, target, detect=_detect):
                logger.info("[xlate] redraft lang=%s reason=%s conv=%s attempts=%d",
                            target, _why, cid, _attempts)
                _log_decision(cid, target, decided_by, "redraft", _why)
                _note_target(item, target, "redraft")
                try:
                    item["_xlate_redraft"] = {"reason": _why, "target": target}
                except Exception:
                    pass
                _hold_marker_clear(store, cid)
                return _new
            if _new:
                logger.info("[xlate] redraft 产物语种不合目标（target=%s）→ 弃用 conv=%s", target, cid)
    if degraded:
        # D-M3：校验失败（含 target_lang_mismatch / identity 回显 / 引擎拒绝）一律拦下，
        # 原因挂回 item → worker 标「翻译失败待确认」，任何情况不发原文。
        logger.warning(
            "[outbound_translate] 译文不可用(provider=%s err=%s attempts=%d) → HOLD 不发"
            "（无兜底纪律）target=%s decided_by=%s conv=%s",
            provider or "-", err or "-", _attempts, target, decided_by or "-", cid)
        _gate_record("held", conversation_id=cid, target=target)
        _mark_hold(item, _why, target=target, decided_by=decided_by, attempts=_attempts)
        if _why == LOW_CONF_REASON and isinstance(item.get("_xlate_hold"), dict):
            item["_xlate_hold"]["confidence"] = round(float(_conf), 2)
            item["_xlate_hold"]["min_confidence"] = round(float(_min_conf), 2)
        _report_block_safe(cid, target, err or "translate_degraded")
        _log_decision(cid, target, decided_by, "hold", _why)
        # Q-39 B：状态带人话「翻译引擎没回话 · 这条没发 · 重试翻译」（xlate_hold_marker，
        # 下一次同会话翻译成功即清）
        _hold_marker_write(store, cid, reason=_why, target=target,
                           draft_id=str(item.get("draft_id") or ""), attempts=_attempts,
                           text=text)
        return None
    if _url_map:
        translated = _restore_urls(translated, _url_map)
    _log_decision(cid, target, decided_by, _decided_action)
    _note_target(item, target, "translated")
    _hold_marker_clear(store, cid)

    if cjk_conflict and gate_only:
        # 硬闸救回（仅 gate_only 计数）：常规翻译模式下 CJK→客户语言是设计内的
        # 例行路径（草稿中文生成 + 出站翻译），计进「救回」会把真异常淹没。
        logger.info(
            "[outbound_translate] 语言硬闸救回：CJK→%s 已翻译投递 conv=%s", target, cid)
        _gate_record("rescued", conversation_id=cid, target=target)
    if store is not None and cid:
        try:
            store.record_outbound_translation(
                cid, translated, text,
                source_lang=eff_source, target_lang=target,
                provider=provider, error=err,
            )
        except Exception:
            logger.debug("[outbound_translate] 记录出向译文失败 conv=%s", cid, exc_info=True)
    return translated


__all__ = [
    "LOW_CONF_REASON",
    "parse_xlate_min_confidence",
    "cjk_substantial",
    "contains_cjk",
    "lang_is_cjk",
    "parse_outbound_lang_gate_cfg",
    "parse_outbound_translate_cfg",
    "parse_xlate_retry_cfg",
    "is_retryable_xlate_error",
    "redraft_output_ok",
    "normalize_target",
    "peer_language_hint",
    "persona_default_lang",
    "resolve_outbound_lang",
    "script_language",
    "should_translate",
    "translate_outbound_text",
    "vote_language",
]
