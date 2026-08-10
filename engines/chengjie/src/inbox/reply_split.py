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

    gap_lo = max(0.0, _f("gap_sec_lo", 0.8))
    gap_hi = max(gap_lo, _f("gap_sec_hi", 2.5))
    # latin_per_char_sec（2026-08-09）：非 CJK 字符的独立手速；None=沿用
    # per_char_sec×0.25 加权刻度（零行为变更锚）。见 typing_time_sec docstring。
    latin_raw = bubbles.get("latin_per_char_sec")
    latin_pcs: Optional[float]
    try:
        latin_pcs = (max(0.0, min(2.0, float(latin_raw)))
                     if latin_raw is not None else None)
    except (TypeError, ValueError):
        latin_pcs = None
    # P3 随机保留组：0..0.5——本可分条的消息按此概率强制整段（干净因果对照，
    # 与 bubbles 组比 3 天回复率）；0=不留对照（默认）。
    holdout = min(0.5, max(0.0, _f("holdout_pct", 0.0)))
    # per_sentence（2026-08-07 运营拍板）：全自动回复「每句一条」而非按 max_chars 打包
    # 多句成段。开时 split_reply_parts 走逐句成条（仍受 max_parts 封顶、超短尾并入）。
    per_sentence = bool(bubbles.get("per_sentence", False))
    # 逐句模式默认放宽条数：3 条常把 4-5 句并回段落，与「每句一条」相悖；未显式配 max_parts
    # 时逐句默认 5（硬上限），显式配了则尊重运营。
    default_max_parts = 5 if (per_sentence and "max_parts" not in bubbles) else 3
    return {
        "enabled": bool(bubbles.get("enabled", False)),
        "per_sentence": per_sentence,
        "holdout_pct": holdout,
        "max_parts": _i("max_parts", default_max_parts, lo=1, hi=5),
        "max_chars": _i("max_chars", 60, lo=20, hi=300),
        "min_tail_chars": _i("min_tail_chars", 4, lo=0, hi=40),
        "min_total_chars": _i("min_total_chars", 24, lo=0, hi=500),
        "gap_sec_lo": gap_lo,
        "gap_sec_hi": gap_hi,
        "per_char_sec": max(0.0, _f("per_char_sec", 0.03)),
        "latin_per_char_sec": latin_pcs,
        # 条间延迟封顶（2026-08-04）：旧硬编码 6s 使「人设手速慢」在条间被一刀切
        # 截断（60 字中文按真人手速要 5-8s+ 打字）；默认仍 6=零行为变更，运营可放宽。
        "max_gap_sec": max(1.0, min(60.0, _f("max_gap_sec", 6.0))),
        # 条间隔序列总预算（秒；0=关，默认关）：所有条间隔之和超预算 → 等比例压缩
        # （保节奏形状），压缩后仍有 0.6s 地板。防 per_sentence 5 条×20s 拖满 80s。
        "total_budget_sec": max(0.0, min(300.0, _f("total_budget_sec", 0.0))),
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
                   min_tail_chars: int) -> Optional[List[str]]:
    """LLM 换行合同路径：≥2 非空行 → 按行成条；超长行再**句界**打包。

    2026-07-31 起超长行不再走语音侧 ``pack_voice_parts``（那是 TTS 切块逻辑，
    会在逗号/空格处硬切凑长度）；改 ``_pack_text_parts``（句界打包 + 加权长度），
    英文行不会再被拦腰切开。
    """
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    expanded: List[str] = []
    for ln in lines:
        if weighted_len(ln) <= max_chars or _is_atomic_chunk(ln):
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
    min_total_chars: int = 24,
    per_sentence: bool = False,
) -> List[str]:
    """把回复拆成 1..max_parts 条短消息文本。

    优先级：
      1. 空 → ``[]``
      2. 总长（加权）< min_total_chars → 单条（短回复不装样子拆）
      3. LLM 换行合同（≥2 行）→ 按行
      4. 否则 ``_pack_text_parts`` 句界打包兜底（只在句末标点断，绝不句中硬切）
      5. 异常 / 切不出第二条 → ``[原文]``
    纯函数、防御式。

    2026-07-31（198 实锤）：长度判定全部改**加权长度**（CJK=1.0/其它=0.25），
    英文文本不再套用中文刻度被 60 字符拦腰切开；兜底打包从语音侧
    ``pack_voice_parts``（逗号/空格硬切）换成句界打包。
    """
    t = str(text or "").strip()
    if not t:
        return []
    max_parts = max(1, min(5, int(max_parts)))
    max_chars = max(20, int(max_chars))
    min_tail_chars = max(0, int(min_tail_chars))
    min_total_chars = max(0, int(min_total_chars))
    if max_parts <= 1:
        return [t]

    # 逐句模式（运营「每句一条」）：整段是原子块不拆；否则每句独占一条。
    # 刻意**不**套 min_total_chars（那是"太短不值得拆"的字数闸，与"每句一条"相悖，
    # 且英文按加权长度会被误判为短）；过短/单句由 _sentence_parts 的 min_tail 合并 +
    # 「不足两条→整段」自然兜住，不会把「好的。嗯。」拆碎。
    if per_sentence:
        if _is_atomic_chunk(t) and len(t.split()) <= 3:
            return [t]
        sp = _sentence_parts(t, max_parts=max_parts, min_tail_chars=min_tail_chars)
        return sp if len(sp) >= 2 else [t]

    if min_total_chars and weighted_len(t) < min_total_chars:
        return [t]

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
    gap_sec_lo: float = 0.8,
    gap_sec_hi: float = 2.5,
    per_char_sec: float = 0.03,
    latin_per_char_sec: Optional[float] = None,
    max_gap_sec: float = 6.0,
    rng: Optional[random.Random] = None,
) -> float:
    """条间延迟 = 基础思考抖动 + 下一条长度×打字速率（Stephanie2 简化版）。

    打字分量走 ``typing_time_sec``：默认加权刻度（CJK=1.0/其它=0.25，旧行为锚）；
    ``latin_per_char_sec`` 显式给出时英文按真实手速计（加权刻度会把英文打字
    耗时低估 ~4 倍，英语客户看到的条间仍是机关枪）。封顶 ``max_gap_sec``
    （默认 6=旧行为；运营可放宽让慢热人设的条间节奏跟上手速）。
    ``rng`` 可注入以便测试确定性。
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
    gap_sec_lo: float = 0.8,
    gap_sec_hi: float = 2.5,
    per_char_sec: float = 0.03,
    latin_per_char_sec: Optional[float] = None,
    max_gap_sec: float = 6.0,
    total_budget_sec: float = 0.0,
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
