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
  - **一般不阻塞投递**：异常 / 不可译 / 译文与原文相同 → 回落发原文，保证全自动链路不断。
    **唯一例外（2026-07-31，198 实锤）**：待发文本含 CJK 而客户语言是非 CJK 语种时，
    「回落发原文」＝把中文原样发给外语客户＝人设当场穿帮——比不发更糟。该冲突下翻译
    不可用时返回 ``None``（HOLD 信号），由 worker 转成投递失败走既有审计/提醒链。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_SOURCE = "zh"
# 不可作为翻译目标的「空/未知」语言标记（与 translation_service.normalize_lang 对齐）
_SKIP_TARGETS = {"", "unknown", "und", "auto"}

# CJK 文字（汉字 + 假名 + 谚文）：出站语言错配判定用。
# 事故背景（2026-07-31，198）：『是Steven，别担心。😊』被 detect_language 判成 en
# （汉字 4 < 拉丁 6）→「已是客户语言」跳过翻译 → 中文原样发给英文客户。
# 「文本里有没有 CJK」是确定性信号，不受检测器计票规则影响，作为跳过护栏的硬否决。
_CJK_TEXT_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_CJK_LANGS = {"zh", "ja", "ko"}


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


def normalize_target(lang: str) -> str:
    """归一化语言码：zh-CN → zh；空/未知/auto → ""（表示「不可作为目标」）。"""
    low = str(lang or "").strip().lower()
    if low in _SKIP_TARGETS:
        return ""
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


async def translate_outbound_text(
    item: Dict[str, Any],
    *,
    translation_service: Any,
    store: Any = None,
    source_lang: str = _DEFAULT_SOURCE,
    style: str = "chat",
    gate_only: bool = False,
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

    **客户证据缺位的独立参照（P1-198）**：目标语言判不出（新客户只发过贴纸/语气词）
    而待发文本含 CJK 时，改用「我们最近已投递消息的语言」当参照——已发历史是客户
    实际收到的地面真相，与本轮生成决策完全解耦；参照也缺位才盲发（并计数观测）。
    """
    text = str(item.get("text") or "")
    cid = str(item.get("conversation_id") or "")
    if not text or translation_service is None:
        return text
    if not _TRANSLATABLE_RE.search(text):
        return text  # 纯 emoji/符号/数字：无可译内容，放行（非兜底，是无操作）

    # 会话目标语言用「最近入站消息加权多数决」（detect 取自同一 translation_service，
    # 与入站落库检测同源），比 conversations.language 单值抗偶发外语翻转。
    _detect = getattr(translation_service, "detect_language", None)
    target = normalize_target(_conv_language(store, cid, detect=_detect))
    # 冲突判定用「实质性 CJK」口径（cjk_substantial）：英文消息引用个别中文
    # 专名不算冲突（生产实锤误伤面，见函数 docstring）。
    text_cjk = cjk_substantial(text)
    if not target and text_cjk:
        # 独立参照：我们一直在用什么语言聊（出站历史投票，非 CJK 才有冲突语义）
        ref = normalize_target(_outbound_history_language(store, cid, _detect))
        if ref and not lang_is_cjk(ref):
            target = ref
    if not target:
        if text_cjk:
            # CJK 文本盲发（客户证据 + 出站参照双缺位）——计数观测，别静默
            _gate_record("no_target_sent", conversation_id=cid)
        return text  # 目标语言未知 → 不翻译（发原文）

    cjk_conflict = text_cjk and not lang_is_cjk(target)
    if gate_only and not cjk_conflict:
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
            return text

    try:
        res = await translation_service.translate(
            text, target_lang=target, source_lang=eff_source, style=style,
        )
    except Exception:
        # 无兜底纪律（2026-08-17 老板拍板）：翻译失败一律 HOLD 不发——旧「非 CJK
        # 冲突回落发原文」拆除（发客户看不懂的原文＝静默替代品，掩盖翻译链故障）。
        logger.warning(
            "[outbound_translate] 翻译调用失败 → HOLD 不发（无兜底纪律）"
            "target=%s conv=%s", target, cid, exc_info=True)
        _gate_record("held", conversation_id=cid, target=target)
        _report_block_safe(cid, target, "translate_exception")
        return None

    translated = str(getattr(res, "translated_text", "") or "")
    provider = str(getattr(res, "provider", "") or "")
    err = str(getattr(res, "error", "") or "")
    ok = bool(getattr(res, "ok", False))

    # 失败 / 空 / 与原文相同（provider=identity/none 或未真译）/ 译文仍**实质性**含 CJK
    # → 一律 HOLD 不发（2026-08-17 无兜底纪律：旧「非冲突态回落原文」拆除——发
    # 客户看不懂/未真译的文本＝静默替代品）。identity 回显正是 198 泄漏的机制。
    # 译文残留判定同用 cjk_substantial：好译文保留「村BA」这类专名引用不算失败。
    degraded = (not ok or not translated or translated == text
                or (cjk_conflict and cjk_substantial(translated)))
    if degraded:
        logger.warning(
            "[outbound_translate] 译文不可用(provider=%s err=%s) → HOLD 不发"
            "（无兜底纪律）target=%s conv=%s",
            provider or "-", err or "-", target, cid)
        _gate_record("held", conversation_id=cid, target=target)
        _report_block_safe(cid, target, err or "translate_degraded")
        return None

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
    "cjk_substantial",
    "contains_cjk",
    "lang_is_cjk",
    "parse_outbound_lang_gate_cfg",
    "parse_outbound_translate_cfg",
    "normalize_target",
    "peer_language_hint",
    "should_translate",
    "translate_outbound_text",
    "vote_language",
]
