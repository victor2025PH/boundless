"""文本短句分条（bubble）— 陪伴域出站「像真人连发几条」的纯函数核心。

设计对齐语音 ``pack_voice_parts`` / Stephanie2 节奏模型，但刻意**先尊重 LLM
换行合同**（``ai_client._build_context_prompt`` 陪伴域已要求「每行 1 句、会被
拆成独立消息」）——算法切句只是兜底。

接线口径（调用方负责）：
  - 翻译之后、``_send_via`` 之前拆（对**译文**拆，避免中文分条后整段译碎）。
  - **仅编排器拥有的账号**拆条：LINE/WA/Messenger RPA runner 已有
    ``human_pacing.split_message``，再拆会双重切碎。
  - 群聊 / 桌面桥 / 结构化长单（含不可切断的 URL 独占超长行）保守不切或整段保留。
"""
from __future__ import annotations

import random
import re
import threading
from typing import Any, Dict, List, Optional

# 不可切断的「整块」：URL / 纯长数字单（订单号/手机号），硬切会毁掉可点性或可复制性
_URL_RE = re.compile(r"(?i)https?://\S+|www\.\S+")
_LONG_DIGIT_RE = re.compile(r"\d{8,}")

# ── 长度语义（2026-07-31，198 实锤修正）───────────────────────────────────────
# max_chars=60 是按**中文字符**校准的（60 个汉字≈一大段话）；同一个 60 落在英文上
# 只有 ~10 个词，翻译后的英文必然被拦腰切开（实测断点全落在 57-60 字符的空格处，
# 例：'Nice, 35 is a great age. I'm 41 myself, kind of an old man' + 'now haha 😄'）。
# 修法＝按「信息量」计长：CJK 字符权重 1.0，其它字符（拉丁/数字/空格/emoji）0.25
# ——同一个 max_chars 预算对英文自动放大 ~4 倍，中文行为保持不变。
_CJK_CHAR_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_LATIN_CHAR_WEIGHT = 0.25

# ── 条间节奏「时代缺省」（2026-08-14 两步收口，173 实录「第 2/3 条机关枪」）──
# 第一步先修英文手速刻度错用（加权刻度 0.03×0.25=0.0075s/字符≈1600 字符/分钟，
# 英文打字分量趋近 0）；第二步按运营拍板把**典型条间隔整体抬到 5–10 秒量级**。
# 模型＝ min(max_gap, max(lo, uniform(lo,hi)×0.55 + 下一条打字耗时))，设计要点：
# 让「思考+打字」自然落进 5–10s，而不是靠地板/封顶硬夹——恒 lo（节拍器）与
# 恒触顶都是另一种机器味。为此两个手速必须同步抬到真人刻度（旧 CJK 0.03s/字
# ＝每秒 33 字属超人手速，中文打字分量趋近 0，光抬 lo/hi 会让地板恒生效）：
#   - CJK 0.22s/字 ≈ 270 字/分钟（快手打字员）：18 字中文泡打字 ≈ 4s；
#   - 拉丁 0.15s/字符 ≈ 80wpm：40 字符英文泡打字 ≈ 6s。
# 叠加思考分量 uniform(3,8)×0.55 = 1.65–4.4s 后，典型泡（中文 12–25 字 /
# 英文 20–60 字符）条间隔自然落在 5–10s，短句略快、长句触 12s 封顶——分布
# 有方差、地板极少绑定。总预算 40s 是 SLA 软顶：手动链在 HTTP 请求内同步等待
# 全部条间隔（前端 60s abort），4 间隔 ×12s 封顶=48s+发送往返会贴线，40s 预算
# 等比压缩保节奏形状；典型 2 间隔（和 ~14s）永不触发。
# 显式配置永远优先（含 0）；要回旧「机关枪时代」行为按 rapid 预设（0.8/2.5/6）。
# reply_pacing_settings.FIELDS 的同名 default 必须与此同值（门禁钉住）。
DEFAULT_GAP_SEC_LO = 3.0
DEFAULT_GAP_SEC_HI = 8.0
DEFAULT_PER_CHAR_SEC = 0.22
DEFAULT_LATIN_PER_CHAR_SEC = 0.15
DEFAULT_MAX_GAP_SEC = 12.0
DEFAULT_TOTAL_BUDGET_SEC = 40.0

# ── 拆条形态「1.0.75 收紧」（#210，D-L3，2026-09-06）──────────────────────────
# 82BF95 实锤：付费用户的客户因为「TG 上打字方式和 WhatsApp 差别很大」识破 AI
# ——逐句拆条（per_sentence）+ 24 字加权门槛让两句英文短回复也被拆成 6–15s 的
# 固定两拍，成了机器节奏。老板拍板三条：出厂关；**仅草稿显式换行才拆**（算法切句
# 只作兜底且默认关）；**短回复永不拆**。
#   - explicit_newline_only=True：只认草稿里的显式换行（LLM 换行合同 / 坐席手写
#     多行）；无换行的整段绝不再按句界切开，逐句模式也不再行内二次切句。
#   - min_total_chars=80（加权）：CJK 80 字 / 英文 ≈320 字符以下一律整条——
#     「I guess you just bring out a different side of me here. But it's still
#     me…」这种两句话（加权 ≈26）永远单条发出。
# reply_pacing_settings.FIELDS 的同名 default 必须与此同值（门禁钉住）。
DEFAULT_MIN_TOTAL_CHARS = 80
DEFAULT_EXPLICIT_NEWLINE_ONLY = True

# 句子边界：CJK 句末标点后天然可断；拉丁 .!? 只在后随空白时算句界
# （防 3.5 / example.com / U.S. 之类被误切）。句中逗号/空格**绝不**再作硬切点
# ——切不出句界就整句保留，宁可长一条也不把一句话说一半。
_SENT_BOUNDARY_RE = re.compile(r"(?:(?<=[。！？；…])|(?<=[.!?])(?=\s))")
# CJK 主导的超长单句兜底切点（软逗号级，仅 CJK 文本放开——中文逗号处断句仍然成话，
# 英文逗号处断句就是半句话）
_CJK_SOFT_BOUNDARY_RE = re.compile(r"(?<=[。！？；…，、：])")
# 片段是否以「词」开头（字母/数字/CJK）；不是 → 该片段（emoji/引号/括号尾）并回前句
_LEAD_WORD_RE = re.compile(r"^[0-9A-Za-z\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")


def weighted_len(text: str) -> float:
    """信息量长度：CJK 记 1.0、其它字符记 0.25（含空格/emoji）。

    用它替代 ``len()`` 做分条预算判定，使 ``max_chars`` 的「中文刻度」对
    拉丁文本自动等效放大，而中文行为逐字不变。
    """
    s = str(text or "")
    cjk = len(_CJK_CHAR_RE.findall(s))
    return cjk + (len(s) - cjk) * _LATIN_CHAR_WEIGHT


def typing_time_sec(
    text: str,
    *,
    per_char_sec: float,
    latin_per_char_sec: Optional[float] = None,
) -> float:
    """估「真人打这段文本」的耗时（纯函数，2026-08-09）。

    CJK 字符按 ``per_char_sec``；其它字符（拉丁/数字/空格/emoji）默认沿用加权
    刻度（``per_char_sec × 0.25``＝旧 ``weighted_len × per_char`` 行为，零变更锚）。
    显式给 ``latin_per_char_sec`` 时非 CJK 字符按它计——加权刻度是给「分条**预算**」
    校准的，挪用到打字**耗时**会把英文手速虚高 ~4 倍（60 字符英文只算 1.2s，
    真人 40-60 wpm 要 6s+），英文会话条间偏机关枪的根因就在这。
    """
    s = str(text or "")
    cjk = len(_CJK_CHAR_RE.findall(s))
    other = len(s) - cjk
    p = max(0.0, float(per_char_sec))
    lp = p * _LATIN_CHAR_WEIGHT
    if latin_per_char_sec is not None:
        try:
            lp = max(0.0, float(latin_per_char_sec))
        except (TypeError, ValueError):
            lp = p * _LATIN_CHAR_WEIGHT
    return cjk * p + other * lp


def _sentence_fragments(text: str, *, soft_cjk: bool = False) -> List[str]:
    """把文本按句界切成原文切片（零字符丢失：切片拼接 == 原文）。

    ``soft_cjk=True`` 时对 CJK 文本额外放开逗号级切点（仅超长单句兜底用）。
    末尾 emoji/引号等非词首片段自动并回前句（防 'okay?' 与 '😄' 被拆两条）。
    """
    s = str(text or "")
    if not s:
        return []
    rx = _CJK_SOFT_BOUNDARY_RE if soft_cjk else _SENT_BOUNDARY_RE
    cuts = sorted({m.start() for m in rx.finditer(s) if 0 < m.start() < len(s)})
    frags: List[str] = []
    prev = 0
    for i in cuts:
        frags.append(s[prev:i])
        prev = i
    frags.append(s[prev:])
    frags = [f for f in frags if f and f.strip()]
    merged: List[str] = []
    for f in frags:
        if merged and not _LEAD_WORD_RE.match(f.strip()):
            merged[-1] = merged[-1] + f
        else:
            merged.append(f)
    return merged


def _pack_text_parts(
    text: str,
    *,
    max_chars: int,
    max_parts: int,
    min_tail_chars: int,
) -> List[str]:
    """句界打包（文本出站专用，2026-07-31 起替代语音侧 ``pack_voice_parts``）。

    不变量：**只在句末标点处断**；单句超预算=整句独占一条（绝不句中硬切）；
    切不出第二条 → 整段单条。CJK 主导的超长单句（>1.6×预算）才放开逗号级
    软切点——中文逗号断句仍成话，英文逗号断句是半句话，故仅 CJK 放开。
    """
    t = str(text or "").strip()
    if not t:
        return []
    budget = max(1.0, float(max_chars))
    frags = _sentence_fragments(t)
    if len(frags) <= 1 and weighted_len(t) > budget * 1.6:
        cjk_n = len(_CJK_CHAR_RE.findall(t))
        if cjk_n >= len(t) * 0.5:  # CJK 主导才放开逗号级软切
            frags = _sentence_fragments(t, soft_cjk=True)
    if len(frags) <= 1:
        return [t]
    keep = max(1, int(max_parts))
    parts: List[str] = []
    cur = ""
    for f in frags:
        if not cur:
            cur = f
            continue
        room_for_new = len(parts) < keep - 1
        if room_for_new and weighted_len(cur.strip()) + weighted_len(f.strip()) > budget:
            parts.append(cur)
            cur = f
        else:
            cur = cur + f
    if cur:
        parts.append(cur)
    parts = [p.strip() for p in parts if p and str(p).strip()]
    parts = _merge_short_tail(parts, min_tail_chars)
    if len(parts) < 2:
        return [t]
    return parts


def parse_bubbles_cfg(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读 ``inbox.reply_style.bubbles``，返回归一化配置（全有默认值，永不抛）。"""
    cfg = config if isinstance(config, dict) else {}
    bubbles = (((cfg.get("inbox") or {}).get("reply_style") or {}).get("bubbles")
               if isinstance(cfg.get("inbox"), dict) else None)
    if not isinstance(bubbles, dict):
        bubbles = {}

    def _f(key: str, default: float) -> float:
        try:
            return float(bubbles.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    def _i(key: str, default: int, lo: int = 0, hi: int = 10_000) -> int:
        try:
            v = int(bubbles.get(key, default))
        except (TypeError, ValueError):
            v = int(default)
        return max(lo, min(hi, v))

    gap_lo = max(0.0, _f("gap_sec_lo", DEFAULT_GAP_SEC_LO))
    gap_hi = max(gap_lo, _f("gap_sec_hi", DEFAULT_GAP_SEC_HI))
    # latin_per_char_sec（2026-08-09 引入 / 2026-08-14 缺省落地）：非 CJK 字符的
    # 独立手速。缺省从 None（沿用 per_char_sec×0.25 加权刻度＝英文机关枪根因）
    # 改为 DEFAULT_LATIN_PER_CHAR_SEC；显式配置（含 0）永远优先。
    # 见「时代缺省」常量块与 typing_time_sec docstring。
    latin_raw = bubbles.get("latin_per_char_sec")
    latin_pcs: float
    try:
        latin_pcs = (max(0.0, min(2.0, float(latin_raw)))
                     if latin_raw is not None else DEFAULT_LATIN_PER_CHAR_SEC)
    except (TypeError, ValueError):
        latin_pcs = DEFAULT_LATIN_PER_CHAR_SEC
    # P3 随机保留组：0..0.5——本可分条的消息按此概率强制整段（干净因果对照，
    # 与 bubbles 组比 3 天回复率）；0=不留对照（默认）。
    holdout = min(0.5, max(0.0, _f("holdout_pct", 0.0)))
    # per_sentence（2026-08-07 运营拍板）：全自动回复「每句一条」而非按 max_chars 打包
    # 多句成段。开时 split_reply_parts 走逐句成条（仍受 max_parts 封顶、超短尾并入）。
    # 1.0.75 起默认关且受 explicit_newline_only 约束（开着也只按显式换行成条）。
    per_sentence = bool(bubbles.get("per_sentence", False))
    # 逐句模式默认放宽条数：3 条常把 4-5 句并回段落，与「每句一条」相悖；未显式配 max_parts
    # 时逐句默认 5（硬上限），显式配了则尊重运营。
    default_max_parts = 5 if (per_sentence and "max_parts" not in bubbles) else 3
    return {
        "enabled": bool(bubbles.get("enabled", False)),
        "per_sentence": per_sentence,
        # 仅显式换行才拆（D-L3）：显式 false ＝运营明确要回算法切句（兜底档）
        "explicit_newline_only": bool(bubbles.get(
            "explicit_newline_only", DEFAULT_EXPLICIT_NEWLINE_ONLY)),
        "holdout_pct": holdout,
        "max_parts": _i("max_parts", default_max_parts, lo=1, hi=5),
        "max_chars": _i("max_chars", 60, lo=20, hi=300),
        "min_tail_chars": _i("min_tail_chars", 4, lo=0, hi=40),
        "min_total_chars": _i("min_total_chars", DEFAULT_MIN_TOTAL_CHARS,
                              lo=0, hi=500),
        "gap_sec_lo": gap_lo,
        "gap_sec_hi": gap_hi,
        "per_char_sec": max(0.0, _f("per_char_sec", DEFAULT_PER_CHAR_SEC)),
        "latin_per_char_sec": latin_pcs,
        # 条间延迟封顶（2026-08-04 引入 / 2026-08-14 随 5–10s 换挡抬到 12）：
        # 长泡（中文 40 字+/英文 60 字符+）打字分量会顶到这里，是 SLA 硬顶。
        "max_gap_sec": max(1.0, min(60.0, _f("max_gap_sec", DEFAULT_MAX_GAP_SEC))),
        # 条间隔序列总预算（秒；0=关）：所有条间隔之和超预算 → 等比例压缩
        # （保节奏形状），压缩后仍有 0.6s 地板。2026-08-14 起默认 40：5–10s 换挡
        # 后手动链（HTTP 请求内同步等待，前端 60s abort）4 间隔×12s 封顶会贴线，
        # 40s 预算是防超时护栏；典型 2 间隔（和 ~14s）永不触发。
        "total_budget_sec": max(0.0, min(
            300.0, _f("total_budget_sec", DEFAULT_TOTAL_BUDGET_SEC))),
        "skip_groups": bool(bubbles.get("skip_groups", True)),
        "orch_only": bool(bubbles.get("orch_only", True)),
    }


# ── 单段落收口（2026-08-08 运营拍板）────────────────────────────────────────────
# 客户实锤质疑「why your messages always 2 parts」——bubbles 关闭的部署里，拟稿层
# 多行合同的产物整段一条发出，呈现为固定「段1+空行+段2」节奏＝AI 感第一破绽。
# bubbles 关时拟稿合同改「一段话」（ai_client._build_context_prompt），本函数是
# 出口硬护栏：LLM 惯性/历史 few-shot 仍可能写多行，折成单段，零内容丢失。
_CJK_JOIN_PUNCT = "。！？…，、；：～"


def collapse_paragraphs(text: str) -> str:
    """把多行/多段文本折成**一个段落**（纯函数，绝不抛）。

    连接规则（保句读、防跑句）：
      - 前行以 CJK 标点结尾 + 下行 CJK 开头 → 直接续（标点已是停顿）；
      - CJK 字 ↔ CJK 字裸边界 → 补「，」（「今天好累\\n想你了」→「今天好累，想你了」）；
      - 其余（拉丁/数字/emoji 边界）→ 单空格（英文句间天然用空格）。
    单行/空文本原样返回（仅 strip）。
    """
    raw = str(text or "").strip()
    if not raw or "\n" not in raw:
        return raw
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return ""
    out = lines[0]
    for ln in lines[1:]:
        prev_ch = out[-1]
        next_ch = ln[0]
        if prev_ch in _CJK_JOIN_PUNCT and _CJK_CHAR_RE.match(next_ch):
            out += ln
        elif _CJK_CHAR_RE.match(prev_ch) and _CJK_CHAR_RE.match(next_ch):
            out += "，" + ln
        else:
            out += " " + ln
    return out


def strip_blank_lines(text: str) -> str:
    """剔除行间**空行**（纯函数，绝不抛）：非空行原样保留（仅去行首尾空白），
    行序不变；单行/空文本仅 strip 后返回。

    与 ``collapse_paragraphs`` 的分工：collapse 折成**一个段落**（bubbles 关、
    单段落合同的出口护栏）；本函数只收「段1+空行+段2」的文章式排版——bubbles
    开时多行是投递层拆条的合法形态（换行保留），但空行不许流出生成层
    （2026-08-15 拍板「段落之间不要有空行，要链接在一起」）。拆条侧
    ``splitlines`` 本就忽略空白行，此收口对投递节奏零影响。
    """
    raw = str(text or "").strip()
    if not raw or "\n" not in raw:
        return raw
    return "\n".join(ln.strip() for ln in raw.splitlines() if ln.strip())


def looks_like_group_chat(platform: str, chat_key: str) -> bool:
    """廉价群聊启发式（零 DB）：Telegram 负数 peer id / WhatsApp ``@g.us`` jid = 群。

    WhatsApp(baileys) 是编排器接管路径，群 jid 若不识别会被当私聊拆条刷屏，
    必须在纯函数层兜住。其它平台无可靠 chat_key 约定时返回 False
    （交由调用方用 store.chat_type 补判）。
    """
    plat = str(platform or "").lower()
    key = str(chat_key or "").strip()
    if not key:
        return False
    if plat == "telegram":
        # -100… 超群 / -… 普通群；正数=私聊用户
        if key.startswith("-") and key[1:].isdigit():
            return True
    if plat == "whatsapp" and key.lower().endswith("@g.us"):
        return True
    return False


# ── 分条观测（进程级计数，autosend-status 汇总消费）────────────────────────────
_STATS_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {"sends": {}, "parts_sent": 0, "partial": 0}


def record_bubble_send(source: str, parts: int, *, partial: bool = False) -> None:
    """记一次分条发送：``source`` ∈ autosend|manual|aline（未知归 other）。"""
    src = str(source or "other").strip() or "other"
    with _STATS_LOCK:
        _STATS["sends"][src] = int(_STATS["sends"].get(src, 0)) + 1
        _STATS["parts_sent"] = int(_STATS["parts_sent"]) + max(0, int(parts))
        if partial:
            _STATS["partial"] = int(_STATS["partial"]) + 1


def bubbles_metrics_snapshot() -> Dict[str, Any]:
    """导出分条发送计数（autosend-status 的 ``bubbles`` 段）。"""
    with _STATS_LOCK:
        sends = {k: int(v) for k, v in _STATS["sends"].items()}
        return {
            "sends": sum(sends.values()),
            "sends_by_source": sends,
            "parts_sent": int(_STATS["parts_sent"]),
            "partial": int(_STATS["partial"]),
        }


def _is_atomic_chunk(seg: str) -> bool:
    """含 URL 或长数字串的片段：禁止二次硬切（保持可点/可复制）。"""
    s = str(seg or "")
    return bool(_URL_RE.search(s) or _LONG_DIGIT_RE.search(s))


def _merge_short_tail(parts: List[str], min_tail_chars: int) -> List[str]:
    chunks = [p.strip() for p in parts if p and str(p).strip()]
    if (len(chunks) >= 2 and min_tail_chars > 0
            and len(chunks[-1]) < int(min_tail_chars)):
        tail = chunks.pop()
        sep = "\n" if ("\n" in chunks[-1] or "\n" in tail) else " "
        chunks[-1] = (chunks[-1] + sep + tail).strip()
    return chunks


def _cap_part_count(parts: List[str], max_parts: int) -> List[str]:
    chunks = [p.strip() for p in parts if p and str(p).strip()]
    keep = max(1, int(max_parts))
    if len(chunks) <= keep:
        return chunks
    head = chunks[: keep - 1]
    rest = chunks[keep - 1:]
    sep = "\n" if any(len(r) > 40 for r in rest) else " "
    return head + [sep.join(rest)]


def _from_newlines(text: str, max_parts: int, max_chars: int,
                   min_tail_chars: int, *,
                   pack_overlong: bool = True) -> Optional[List[str]]:
    """LLM 换行合同路径：≥2 非空行 → 按行成条；超长行再**句界**打包。

    2026-07-31 起超长行不再走语音侧 ``pack_voice_parts``（那是 TTS 切块逻辑，
    会在逗号/空格处硬切凑长度）；改 ``_pack_text_parts``（句界打包 + 加权长度），
    英文行不会再被拦腰切开。

    ``pack_overlong=False``（explicit_newline_only 模式）：行就是条，超长行也
    原样整行一条——拆与不拆只由草稿里的显式换行决定，算法不再插手。
    """
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    expanded: List[str] = []
    for ln in lines:
        if (not pack_overlong or weighted_len(ln) <= max_chars
                or _is_atomic_chunk(ln)):
            expanded.append(ln)
            continue
        try:
            sub = _pack_text_parts(
                ln, max_chars=max_chars, max_parts=max_parts,
                min_tail_chars=min_tail_chars,
            )
            expanded.extend(sub or [ln])
        except Exception:
            expanded.append(ln)
    expanded = _merge_short_tail(expanded, min_tail_chars)
    expanded = _cap_part_count(expanded, max_parts)
    return expanded if len(expanded) >= 2 else None


def _sentence_parts(text: str, *, max_parts: int, min_tail_chars: int) -> List[str]:
    """逐句成条（per_sentence 模式核心）：LLM 换行合同优先，行内再按句界拆，
    **每个句子独占一条**（不按 max_chars 回包）。原子块（URL/长单号）整行不拆；
    超短尾并入前条；最后按 max_parts 封顶（多出的并入末条）。零字符丢失。
    """
    t = str(text or "").strip()
    if not t:
        return []
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()] or [t]
    out: List[str] = []
    for ln in lines:
        if _is_atomic_chunk(ln):
            out.append(ln)
            continue
        frags = _sentence_fragments(ln)
        if len(frags) <= 1:
            out.append(ln)
        else:
            out.extend(f.strip() for f in frags if f and f.strip())
    out = [p.strip() for p in out if p and str(p).strip()]
    out = _merge_short_tail(out, min_tail_chars)
    out = _cap_part_count(out, max_parts)
    return [p for p in out if p and str(p).strip()]


def split_reply_parts(
    text: str,
    *,
    max_parts: int = 3,
    max_chars: int = 60,
    min_tail_chars: int = 4,
    min_total_chars: int = DEFAULT_MIN_TOTAL_CHARS,
    per_sentence: bool = False,
    explicit_newline_only: bool = DEFAULT_EXPLICIT_NEWLINE_ONLY,
) -> List[str]:
    """把回复拆成 1..max_parts 条短消息文本。

    优先级：
      1. 空 → ``[]``
      2. 总长（加权）< min_total_chars → 单条（**短回复永不拆**，所有模式一致）
      3. LLM 换行合同（≥2 行）→ 按行
      4. ``explicit_newline_only``（默认开）→ 到此为止：无显式换行就整条发
      5. 兜底档（显式关掉 explicit_newline_only 才进）：逐句成条 / 句界打包
      6. 异常 / 切不出第二条 → 单条
    纯函数、防御式。

    **每一条都不含换行**（2026-09-06）：bubbles 开时拟稿合同是「每行一句」，
    决定不拆（短回复）或被 max_parts 并入末条的多行若原样发出，就是「一条消息带
    结构化换行」——2026-08-08 客户实锤「why always 2 parts」的形态，比拆条更糟。
    故单条出口 / 并行末条一律 ``collapse_paragraphs`` 折成自然单段（与 RPA
    ``human_pacing`` / 桌面桥 / 协议直发链同款收口），调用方拿到什么就发什么。

    2026-07-31（198 实锤）：长度判定全部改**加权长度**（CJK=1.0/其它=0.25），
    英文文本不再套用中文刻度被 60 字符拦腰切开；兜底打包从语音侧
    ``pack_voice_parts``（逗号/空格硬切）换成句界打包。
    2026-09-06（#210 / D-L3）：短回复门对逐句模式同样生效（旧版逐句绕过它，
    两句英文短回复也被拆成固定两拍＝82BF95 客户识破 AI 的直接形态）；算法切句
    降为显式 opt-in 的兜底档。
    """
    parts = _split_reply_parts_raw(
        text, max_parts=max_parts, max_chars=max_chars,
        min_tail_chars=min_tail_chars, min_total_chars=min_total_chars,
        per_sentence=per_sentence, explicit_newline_only=explicit_newline_only,
    )
    out: List[str] = []
    for p in parts:
        s = str(p or "")
        if "\n" in s:
            try:
                s = collapse_paragraphs(s) or s
            except Exception:
                pass
        if s.strip():
            out.append(s)
    return out


def _split_reply_parts_raw(
    text: str,
    *,
    max_parts: int,
    max_chars: int,
    min_tail_chars: int,
    min_total_chars: int,
    per_sentence: bool,
    explicit_newline_only: bool,
) -> List[str]:
    """``split_reply_parts`` 的切分核心（条内可能残留换行，由外层折叠）。"""
    t = str(text or "").strip()
    if not t:
        return []
    max_parts = max(1, min(5, int(max_parts)))
    max_chars = max(20, int(max_chars))
    min_tail_chars = max(0, int(min_tail_chars))
    min_total_chars = max(0, int(min_total_chars))
    if max_parts <= 1:
        return [t]

    # 短回复永不拆——放在一切模式之前（D-L3）。
    if min_total_chars and weighted_len(t) < min_total_chars:
        return [t]

    # 仅显式换行才拆（出厂默认）：草稿自己分了行（LLM 换行合同 / 坐席多行）就
    # 按行成条，行就是条（超长行不再被句界重切）；没有换行＝整条发出。
    if explicit_newline_only:
        lined = _from_newlines(t, max_parts, max_chars, min_tail_chars,
                               pack_overlong=False)
        return lined if (lined and len(lined) >= 2) else [t]

    # ── 以下为算法切句兜底档（需显式 explicit_newline_only=false）──
    # 逐句模式（运营「每句一条」）：整段是原子块不拆；否则每句独占一条。
    if per_sentence:
        if _is_atomic_chunk(t) and len(t.split()) <= 3:
            return [t]
        sp = _sentence_parts(t, max_parts=max_parts, min_tail_chars=min_tail_chars)
        return sp if len(sp) >= 2 else [t]

    lined = _from_newlines(t, max_parts, max_chars, min_tail_chars)
    if lined and len(lined) >= 2:
        return lined

    # 整段是「原子块」（单 URL / 长单号）→ 不切
    if _is_atomic_chunk(t) and len(t.split()) <= 3:
        return [t]

    try:
        parts = _pack_text_parts(
            t, max_chars=max_chars, max_parts=max_parts,
            min_tail_chars=min_tail_chars,
        )
    except Exception:
        return [t]
    parts = [p.strip() for p in (parts or []) if p and str(p).strip()]
    if len(parts) < 2:
        return [t]
    return parts


def inter_part_delay_sec(
    next_text: str,
    *,
    gap_sec_lo: float = DEFAULT_GAP_SEC_LO,
    gap_sec_hi: float = DEFAULT_GAP_SEC_HI,
    per_char_sec: float = DEFAULT_PER_CHAR_SEC,
    latin_per_char_sec: Optional[float] = None,
    max_gap_sec: float = DEFAULT_MAX_GAP_SEC,
    rng: Optional[random.Random] = None,
) -> float:
    """条间延迟 = 基础思考抖动 + 下一条长度×打字速率（Stephanie2 简化版）。

    打字分量走 ``typing_time_sec``：``latin_per_char_sec=None`` 时非 CJK 按
    加权刻度（CJK=1.0/其它=0.25，历史行为锚——会把英文打字耗时低估 ~4 倍，
    生产链一律从 ``parse_bubbles_cfg`` 显式透传真实手速）。封顶 ``max_gap_sec``。
    签名缺省与「时代缺省」常量同源（5–10s 量级）。``rng`` 可注入以便测试确定性。
    """
    lo = max(0.0, float(gap_sec_lo))
    hi = max(lo, float(gap_sec_hi))
    r = rng or random
    think = r.uniform(lo, hi)
    typing = typing_time_sec(
        str(next_text or ""), per_char_sec=per_char_sec,
        latin_per_char_sec=latin_per_char_sec)
    # 思考占主导、打字微调；总时长封顶防拖垮投递 SLA
    cap = max(1.0, float(max_gap_sec))
    return min(cap, max(lo, think * 0.55 + typing))


def plan_bubble_gaps(
    parts: List[str],
    *,
    gap_sec_lo: float = DEFAULT_GAP_SEC_LO,
    gap_sec_hi: float = DEFAULT_GAP_SEC_HI,
    per_char_sec: float = DEFAULT_PER_CHAR_SEC,
    latin_per_char_sec: Optional[float] = None,
    max_gap_sec: float = DEFAULT_MAX_GAP_SEC,
    total_budget_sec: float = DEFAULT_TOTAL_BUDGET_SEC,
    min_gap_floor: float = 0.6,
    rng: Optional[random.Random] = None,
) -> List[float]:
    """为 ``parts[1:]`` 预排条间延迟（纯函数，2026-08-09，返回 len-1 个间隔）。

    每条先按 ``inter_part_delay_sec`` 同模型独立估值；``total_budget_sec > 0``
    且间隔总和超预算 → **等比例压缩**——保持「长句间隔相对更长」的节奏形状
    （真人赶时间是整体加快，不是前慢后机关枪的砍尾），压缩后仍不低于
    ``min_gap_floor``（反机关枪是首要目标，预算是软约束：地板生效时总和可略超）。
    parts < 2 → ``[]``。纯算术不抛；调用方仍应 try 兜底。
    """
    ps = [str(p or "") for p in (parts or [])]
    if len(ps) < 2:
        return []
    gaps = [
        inter_part_delay_sec(
            p, gap_sec_lo=gap_sec_lo, gap_sec_hi=gap_sec_hi,
            per_char_sec=per_char_sec, latin_per_char_sec=latin_per_char_sec,
            max_gap_sec=max_gap_sec, rng=rng)
        for p in ps[1:]
    ]
    try:
        budget = float(total_budget_sec or 0.0)
    except (TypeError, ValueError):
        budget = 0.0
    total = sum(gaps)
    if budget > 0 and total > budget:
        scale = budget / total
        floor = max(0.0, float(min_gap_floor))
        gaps = [max(floor, g * scale) for g in gaps]
    return gaps


def cap_max_parts_for_platform(max_parts: int, platform: str) -> int:
    """全局 ``bubbles.max_parts`` 按渠道策略封顶（实施96 P0-3：抖音一轮 ≤2 条——
    24h 内只有 6 条配额，拆 3 条＝半轮配额；声明 1 条的平台如 qqbot 由此封成
    单段，``split_reply_parts(max_parts=1)`` 自然不拆）。未登记平台原值返回。"""
    try:
        from src.inbox.channel_policy import cap_max_parts
        return cap_max_parts(platform, int(max_parts))
    except Exception:
        return max(1, int(max_parts or 1))


def should_split_for_delivery(
    *,
    cfg: Dict[str, Any],
    platform: str,
    chat_key: str,
    orch_owns: bool,
    chat_type: str = "",
) -> bool:
    """投递侧总闸：配置开 +（非 orch_only 或编排器拥有）+ 非群。"""
    if not cfg or not cfg.get("enabled"):
        return False
    if cfg.get("orch_only", True) and not orch_owns:
        return False
    if cfg.get("skip_groups", True):
        ct = str(chat_type or "").strip().lower()
        if ct and ct not in ("private", ""):
            return False
        if looks_like_group_chat(platform, chat_key):
            return False
    return True
