"""群戏自然度评测 —— 「这场戏演得像不像真人」的量化刻度（纯函数，零 I/O）。

为什么用香农熵来测「假」
------------------------
封号与被识破的头号特征不是「说了广告」，而是**模板化**：同一批号翻来覆去用同一套
句式、同一个梗、同一种句长。人眼一扫就知道是机器，风控更是一抓一个准。而
「模板化」在信息论上有精确对应物——**冗余度**：同样长度的文本，复读的那份熵低。

借鉴 AgentGroupChat 的思路，本模块用**字符 bigram 的归一化香农熵**度量群聊
「有序中的多样性」：

- 熵**过低** ＝ 词面高度重合 ＝ 模板复读 ＝ 一眼假 → ``robotic``；
- 熵**顶格** ＝ 前后毫无词面呼应 ＝ 各说各话 ＝ 不像一场对话 → ``chaotic``；
- 落在中间那条带上，才是「有序中的多样性」＝ 真人群聊。

用字符 n-gram 而非分词：本仓多语种（中/英/日/泰/越…），字符 n-gram 免分词器、
跨语种同一把尺，且对中文这种无空格文字天然友好。

三条正交轴（缺一不可）
----------------------
单看熵会漏掉两类同样「一眼假」的失败：

1. ``interval_cv``（发言间隔变异系数 σ/μ）——真人群聊是**泊松式**到达，CV≈1；
   定时器驱动的机器人间隔整整齐齐，CV≈0。词写得再花，掐着秒表发也是假。
2. ``balance``（发言人轮换均衡度，说话人分布的归一化熵）——一个号包场刷屏，
   剩下三个号陪站，不构成群聊。

归一化口径与刻度依据（阈值可调，但请连同依据一起改）
--------------------------------------------------
**熵的归一化**取 ``H / log2(N)``（N＝该窗口 bigram 总 token 数）：全部 token
互不相同时取到 1.0，重复越多越低——本质是「1 − 冗余度」。

⚠ 该比值**随文本变长而自然下降**（Heaps 定律：语料越长，常用 bigram 复现越多），
直接对全场算会让「长戏」被误判 robotic。故本模块改为**滑窗均值**：按
:data:`ENTROPY_WINDOW` 条发言为一窗滑动求熵再取平均，每窗 token 量级恒定，
分数不再随场次长度漂移。实测刻度（8 条 × 约 15 字的窗口）：

- 8 条近乎雷同的台词 → ≈0.55；
- 8 条自然多样的群聊 → ≈0.95+；
- 判据 :data:`ENTROPY_ROBOTIC` = 0.82 落在两者之间的空档里。

``interval_cv``：泊松过程 CV=1.0，真人群聊实测常在 0.5~1.5；纯定时器 CV=0。
:data:`CV_ROBOTIC` = 0.15 只抓「机械整齐」这一极端，不误伤节奏偏稳的真人。

``balance``：说话人分布熵 / log2(说话人数)，全均衡=1.0，单人独占=0.0。

⚠ **chaotic 是本模块最弱的一条轴**（如实声明）：词面统计看不见语义连贯——
真人接话常常一个字都不重复（"号切到崩溃" ← "统一挂后台就好了"）。故 chaotic
判据刻意保守：必须**同时**满足「熵顶格 + 相邻发言词面零重合 + 样本足够」才判，
宁可漏判不可误判。真正的连贯性轨应是嵌入余弦或 LLM-as-judge，属后续增量。

**样本不足不假装打分**：< :data:`MIN_SAMPLES` 条直接返回 ``verdict="insufficient"``
且 ``ok=False``——把 3 条台词的熵当质量分是自欺欺人。消费方（回归门禁）**必须先看
``ok`` 再用 ``score``**。
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# ── 刻度常量（改阈值请连同上文「刻度依据」一起改） ────────────────────────────

#: 低于此熵 → 模板复读（8×15 字窗口下：全复读≈0.55 / 自然≈0.95+）
ENTROPY_ROBOTIC = 0.82
#: 熵顶格（几乎零 token 复现）——chaotic 的必要非充分条件
ENTROPY_CHAOTIC = 0.995
#: 相邻发言词面重合率低于此 → 配合熵顶格才判 chaotic
COHESION_CHAOTIC = 0.10
#: 间隔变异系数低于此 → 机械整齐（真人 0.5~1.5，定时器 ≈0）
CV_ROBOTIC = 0.15
#: 间隔 CV 达到此值即认为节奏足够拟人（满分线，非及格线）
CV_NATURAL = 0.45
#: 轮换均衡度低于此 → 记 issue（不改 verdict，见模块 docstring）
BALANCE_MIN = 0.70
#: 单号发言占比高于此 → 刷屏 issue
TOP_SHARE_MAX = 0.55
#: 少于此条数不予打分（3 条台词算不出可信的熵）
MIN_SAMPLES = 3
#: 熵滑窗宽度（条）——固定窗口消除「长戏熵自然下滑」的伪信号
ENTROPY_WINDOW = 8
#: 判为 robotic / chaotic 时的综合分封顶——保证 score 与 verdict 同向（见下）
ROBOTIC_SCORE_CAP = 0.55
CHAOTIC_SCORE_CAP = 0.40
#: 窗口 token 数低于此则该窗不参与（几个字的窗口熵无意义）
_MIN_WINDOW_TOKENS = 4
#: 参与评测的事件类型：只算真发出去的内容（与 ShowState.lines_by_account 同口径）；
#: yield/terminate 是导演控制事件，human 是真人原话，都不该计入 AI 的演技分。
SCOREABLE_KINDS: Tuple[str, ...] = ("line", "media")

#: 词面重合率的停用字/词（高频功能字到处都是，留着会让任何两句都"重合"）
_STOP_CHARS: Set[str] = set(
    "的了是我你他她它们也都就不在有个这那很吧啊呢吗和跟对与之为以以及但还会"
    "要能好没上下来去说到过又只再把被让给于把地得着呀哦嗯啦么什怎"
)
_STOP_WORDS: Set[str] = {
    "the", "and", "for", "you", "not", "but", "with", "that", "this", "have",
    "are", "was", "just", "can", "all", "its", "his", "her", "they", "them",
}

_VERDICT_NATURAL = "natural"
_VERDICT_ROBOTIC = "robotic"
_VERDICT_CHAOTIC = "chaotic"
_VERDICT_INSUFFICIENT = "insufficient"


# ── 取值与清洗（脏数据一律软降级，绝不抛） ──────────────────────────────────


def _get(ev: Any, name: str, default: Any) -> Any:
    """从 ShowEvent（或形状相近的 dict）取字段；取不到/为 None → default。"""
    try:
        v = ev.get(name, default) if isinstance(ev, dict) else getattr(ev, name, default)
    except Exception:  # noqa: BLE001 —— 评测器不该被畸形入参掀翻
        return default
    return default if v is None else v


def _normalize_text(text: Any) -> str:
    """归一化：折叠空白 + casefold。空白折叠让「换行/多空格」不产生伪 n-gram。"""
    try:
        s = str(text or "")
    except Exception:  # noqa: BLE001
        return ""
    return " ".join(s.split()).casefold()


def _scoreable(events: Any) -> List[Any]:
    """筛出参与评测的事件（跳过 None / 非法 kind / 不可迭代入参）。"""
    if not isinstance(events, Iterable) or isinstance(events, (str, bytes)):
        return []
    out: List[Any] = []
    try:
        for ev in events:
            if ev is None:
                continue
            kind = str(_get(ev, "kind", "line") or "").strip().lower()
            if kind in SCOREABLE_KINDS:
                out.append(ev)
    except Exception:  # noqa: BLE001 —— 迭代器自身炸了也不上抛
        return out
    return out


def _content_tokens(text: Any) -> Set[str]:
    """内容词集合：CJK 取非停用单字（字＝语素），拉丁取 ≥3 字母的词。"""
    s = _normalize_text(text)
    out: Set[str] = set()
    buf: List[str] = []

    def _flush() -> None:
        if not buf:
            return
        w = "".join(buf)
        buf.clear()
        if len(w) >= 3 and not w.isdigit() and w not in _STOP_WORDS:
            out.add(w)

    for ch in s:
        if ch.isascii() and ch.isalnum():
            buf.append(ch)
            continue
        _flush()
        if ch.isspace() or not ch.isalnum() or ch in _STOP_CHARS:
            continue
        out.add(ch)
    _flush()
    return out


# ── 熵 ──────────────────────────────────────────────────────────────────────


def _ngram_counts(text: Any, n: int) -> Counter:
    """单条文本的字符 n-gram 计数（长度不足 n → 空）。"""
    s = _normalize_text(text)
    if n < 1 or len(s) < n:
        return Counter()
    return Counter(s[i:i + n] for i in range(len(s) - n + 1))


def _entropy_from_counts(counts: Counter) -> float:
    """由 n-gram 计数算归一化香农熵 ``H / log2(N)``，钳到 [0,1]。

    N≤1 时熵无定义（一个 token 谈不上多样性）→ 0.0。
    """
    total = int(sum(counts.values()))
    if total <= 1:
        return 0.0
    h = 0.0
    for c in counts.values():
        p = c / total
        if p > 0:
            h -= p * math.log2(p)
    return max(0.0, min(1.0, h / math.log2(total)))


def shannon_entropy(text: Any, *, n: int = 2) -> float:
    """单段文本的**归一化**字符 n-gram 香农熵，0.0..1.0。

    1.0 ＝ 所有 n-gram 互不相同（零冗余）；越低 ＝ 重复越多（越模板化）。
    空串 / 长度不足 n / 只有一个 n-gram → 0.0（无从判定，不假装给高分）。

    注意归一化分母是 ``log2(总 token 数)``，故**同一段文本越长、自然复现越多、
    比值越低**；跨长度横比请走 :func:`naturalness_score` 的滑窗均值口径。
    """
    try:
        n = max(1, int(n))
    except (TypeError, ValueError):
        n = 2
    return _entropy_from_counts(_ngram_counts(text, n))


def _windowed_entropy(texts: Sequence[str], *, n: int = 2) -> Tuple[float, int]:
    """滑窗熵均值 → ``(entropy, 参与窗口数)``；无有效窗口 → ``(0.0, 0)``。

    固定窗宽让归一化分母量级恒定，分数不随场次总长漂移（见模块 docstring）。
    """
    valid = [t for t in texts if t]
    if not valid:
        return 0.0, 0
    width = min(ENTROPY_WINDOW, len(valid))
    per_msg = [_ngram_counts(t, n) for t in valid]
    vals: List[float] = []
    for start in range(0, len(valid) - width + 1):
        agg: Counter = Counter()
        for c in per_msg[start:start + width]:
            agg.update(c)
        if sum(agg.values()) >= _MIN_WINDOW_TOKENS:
            vals.append(_entropy_from_counts(agg))
    if not vals:
        return 0.0, 0
    return sum(vals) / len(vals), len(vals)


# ── 发言人均衡度 ────────────────────────────────────────────────────────────


def _speaker_counts(events: Any) -> Counter:
    c: Counter = Counter()
    for ev in _scoreable(events):
        who = str(_get(ev, "speaker_account", "") or "").strip()
        c[who or "?"] += 1
    return c


def speaker_balance(events: Any) -> float:
    """发言人轮换均衡度，0.0..1.0（1.0 ＝ 各号发言数完全均等）。

    口径＝说话人分布的归一化香农熵 ``H / log2(k)``（k＝实际开口的号数）。
    **k≤1 → 0.0**：一个号从头说到尾不是群聊，是独角戏，判为完全失衡。
    """
    counts = _speaker_counts(events)
    total = int(sum(counts.values()))
    k = len(counts)
    if total <= 0 or k <= 1:
        return 0.0
    h = 0.0
    for c in counts.values():
        p = c / total
        if p > 0:
            h -= p * math.log2(p)
    return max(0.0, min(1.0, h / math.log2(k)))


# ── 间隔 ────────────────────────────────────────────────────────────────────


def _coerce_intervals(raw: Any) -> List[float]:
    """外部传入的间隔序列 → 干净的非负浮点列表（脏值丢弃）。"""
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes)):
        return []
    out: List[float] = []
    try:
        for v in raw:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(f) and f >= 0:
                out.append(f)
    except Exception:  # noqa: BLE001
        return out
    return out


def _derive_intervals(events: Sequence[Any]) -> List[float]:
    """从事件 ``ts`` 现算相邻间隔（按时间升序；非法/缺失 ts 丢弃）。"""
    ts: List[float] = []
    for ev in events:
        try:
            t = float(_get(ev, "ts", 0.0))
        except (TypeError, ValueError):
            continue
        if math.isfinite(t) and t > 0:
            ts.append(t)
    ts.sort()
    return [b - a for a, b in zip(ts, ts[1:])]


def _interval_cv(intervals: Sequence[float]) -> float:
    """变异系数 σ/μ（总体标准差）。均值≤0（全部同一瞬间发出）→ 0.0 ＝ 最机械。"""
    n = len(intervals)
    if n < 1:
        return 0.0
    mean = sum(intervals) / n
    if mean <= 0:
        return 0.0
    var = sum((x - mean) ** 2 for x in intervals) / n
    return math.sqrt(var) / mean


def _cohesion(texts: Sequence[str]) -> float:
    """相邻发言的词面重合率＝「至少共享一个内容词」的相邻对占比。

    只作 chaotic 的**辅助**证据：真人接话常零词面重合（语义承接≠词面重复），
    单独用它判离题会大面积误伤，故仅在熵顶格时才纳入判据。
    """
    valid = [t for t in texts if t]
    if len(valid) < 2:
        return 0.0
    toks = [_content_tokens(t) for t in valid]
    pairs = len(toks) - 1
    hit = sum(1 for a, b in zip(toks, toks[1:]) if a & b)
    return hit / pairs if pairs else 0.0


def line_similarity(a: Any, b: Any) -> float:
    """两条台词的内容词 **overlap 系数** ``|A∩B| / min(|A|,|B|)`` ∈ [0,1]。

    给 live 的复读闸用。选 overlap 而不是 Jaccard：CJK 内容词以单字语素为主，
    复读的两条哪怕句式全换，交集也只占并集的三成左右（Jaccard 压不出间隔），
    但交集能覆盖短句的一半——2026-07-27 首场灰度实录（solo_matrixx b3/b4 都在
    说「消息收在后台看、不用切」）：overlap = 0.50，而全部正常拍两两 ≤ 0.25。
    阈值见 ``live.MAX_LINE_SIMILARITY``，标定门禁在 test_group_show_naturalness。
    """
    ta, tb = _content_tokens(a), _content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(min(len(ta), len(tb)))


# ── 综合评分 ────────────────────────────────────────────────────────────────


def _ramp(x: float, lo: float, hi: float) -> float:
    """线性斜坡：≤lo → 0.0，≥hi → 1.0，中间线性插值。"""
    if hi <= lo:
        return 1.0 if x >= hi else 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def _insufficient(n: int) -> Dict[str, Any]:
    """样本不足的明确结果——不打分、不装作有结论。"""
    return {
        "ok": False,
        "insufficient": True,
        "samples": n,
        "score": 0.0,
        "entropy": 0.0,
        "entropy_measurable": False,
        "interval_cv": 0.0,
        "cv_measurable": False,
        "balance": 0.0,
        "cohesion": 0.0,
        "top_share": 0.0,
        "top_speaker": "",
        "speakers": 0,
        "verdict": _VERDICT_INSUFFICIENT,
        "issues": [f"样本不足：有效发言 {n} 条（少于 {MIN_SAMPLES} 条不予打分）"],
    }


def naturalness_score(
    events: Sequence[Any],
    *,
    intervals: Optional[Sequence[float]] = None,
) -> Dict[str, Any]:
    """给一场戏打自然度分。**纯函数**：不读配置、不碰 DB、不调 LLM。

    :param events: ``ShowEvent`` 序列（只有 ``kind in`` :data:`SCOREABLE_KINDS`
        的事件参与评测）。也容忍同形状 dict 与脏数据。
    :param intervals: 相邻发言的实际间隔秒；不传则由 ``ShowEvent.ts`` 现算。
        （runtime 手里的「计划等待时长」比落库 ts 更贴近真实节奏，故留此入口。）
    :returns: 见模块 docstring。关键键：``score`` / ``entropy`` /
        ``interval_cv`` / ``balance`` / ``verdict`` / ``issues``；
        **``ok=False`` 时 ``score`` 无意义**（样本不足），消费方须先判 ``ok``。

    综合分 ＝ 0.45×熵 + 0.30×间隔拟人度 + 0.25×均衡度（权重依据：模板化是最致命
    也最容易被风控抓的特征，故熵权重最高；节奏次之；均衡度是「戏好不好看」而非
    「像不像人」，权重最低）。

    **单号独演（说话人数 ≤ 1）直接判 robotic**：这与「多号失衡只记 issue」是两件事——
    一个话痨带几个附和在真群里很常见（戏难看），而一个号自问自答演完整场在真群里根本
    不存在（结构性造假）。修此判定前，真实排练出现过「全场只有一个号在说话」的 issue
    与 ``verdict=natural`` 同时出现，判词自相矛盾。

    加权和之上再加 **verdict 封顶**（:data:`ROBOTIC_SCORE_CAP` /
    :data:`CHAOTIC_SCORE_CAP`）：纯加权和会让「文本全复读、但节奏与轮换完美」的
    假戏拿到 0.61，和「刚好踩线自然」的真戏（约 0.62）几乎并列——排序与判词打架，
    门禁只看分数就会放行硬伤。封顶后 ``score`` 与 ``verdict`` 严格同向，门禁可以
    只读一个数。
    """
    evs = _scoreable(events)
    n = len(evs)
    if n < MIN_SAMPLES:
        return _insufficient(n)

    texts = [_normalize_text(_get(ev, "text", "")) for ev in evs]
    entropy, windows = _windowed_entropy(texts)
    entropy_measurable = windows > 0
    cohesion = _cohesion(texts)

    ivals = _coerce_intervals(intervals) if intervals is not None else _derive_intervals(evs)
    # 一个间隔算不出方差；两个起才谈得上"齐不齐"
    cv_measurable = len(ivals) >= 2
    cv = _interval_cv(ivals) if cv_measurable else 0.0

    counts = _speaker_counts(evs)
    balance = speaker_balance(evs)
    top_speaker, top_n = counts.most_common(1)[0] if counts else ("", 0)
    top_share = (top_n / n) if n else 0.0

    issues: List[str] = []

    # ① 熵：过低＝模板复读
    robotic_by_entropy = entropy_measurable and entropy < ENTROPY_ROBOTIC
    if robotic_by_entropy:
        issues.append(
            f"台词高度雷同（bigram 熵 {entropy:.2f} < {ENTROPY_ROBOTIC}）："
            "像同一套模板复读，一眼假"
        )
    if not entropy_measurable:
        issues.append("台词过短，熵不可判（已不计入判据，避免误判为模板化）")

    # ② 间隔：过于整齐＝定时器
    robotic_by_cv = cv_measurable and cv < CV_ROBOTIC
    if robotic_by_cv:
        issues.append(
            f"发言间隔过于整齐（CV {cv:.2f} < {CV_ROBOTIC}）：真人不会掐着秒表说话"
        )
    if not cv_measurable:
        issues.append("间隔样本不足，节奏未参与判据")

    # ③ 熵顶格 + 词面零重合 ＝ 各说各话（保守判据，见模块 docstring）
    chaotic = (
        entropy_measurable
        and n >= 6
        and entropy >= ENTROPY_CHAOTIC
        and cohesion <= COHESION_CHAOTIC
    )
    if chaotic:
        issues.append(
            f"前后毫无呼应（熵 {entropy:.2f} 顶格、相邻发言词面重合率 "
            f"{cohesion:.0%}）：各说各话，不像同一场对话"
        )

    # ④ 均衡度：多号失衡只记 issue 不改 verdict（一个话痨带几个附和是"戏难看"，
    #    不是"不像人"，真群里很常见）；但**单号独演要判 robotic**——
    #    一个号在群里自问自答演完整条种草弧线，真人群聊里不存在这种模式，
    #    那不是戏难看，是最扎眼的机器人特征，也是风控最容易抓的形状。
    solo = len(counts) <= 1
    if balance < BALANCE_MIN:
        if solo:
            issues.append(f"全场只有 {top_speaker} 一个号在说话：这不是群聊")
        else:
            issues.append(f"发言严重失衡（均衡度 {balance:.2f} < {BALANCE_MIN}）")
    if top_share > TOP_SHARE_MAX and not solo:
        issues.append(f"{top_speaker} 占了 {top_share:.0%} 的发言：单号刷屏")

    if robotic_by_entropy or robotic_by_cv or solo:
        verdict = _VERDICT_ROBOTIC
    elif chaotic:
        verdict = _VERDICT_CHAOTIC
    else:
        verdict = _VERDICT_NATURAL

    # 不可判的轴给中性分 0.6，既不奖励也不当作失败（宁可不判，不可错判）
    ent_sub = _ramp(entropy, 0.55, 0.88) if entropy_measurable else 0.6
    cv_sub = _ramp(cv, 0.05, CV_NATURAL) if cv_measurable else 0.6
    score = 0.45 * ent_sub + 0.30 * cv_sub + 0.25 * balance
    if verdict == _VERDICT_ROBOTIC:
        score = min(score, ROBOTIC_SCORE_CAP)
    elif verdict == _VERDICT_CHAOTIC:
        score = min(score, CHAOTIC_SCORE_CAP)

    return {
        "ok": True,
        "insufficient": False,
        "samples": n,
        "score": round(max(0.0, min(1.0, score)), 4),
        "entropy": round(entropy, 4),
        "entropy_measurable": entropy_measurable,
        "interval_cv": round(cv, 4),
        "cv_measurable": cv_measurable,
        "balance": round(balance, 4),
        "cohesion": round(cohesion, 4),
        "top_share": round(top_share, 4),
        "top_speaker": str(top_speaker),
        "speakers": len(counts),
        "verdict": verdict,
        "issues": issues,
    }


__all__ = [
    "ENTROPY_ROBOTIC",
    "ENTROPY_CHAOTIC",
    "CV_ROBOTIC",
    "CV_NATURAL",
    "BALANCE_MIN",
    "TOP_SHARE_MAX",
    "MIN_SAMPLES",
    "ENTROPY_WINDOW",
    "ROBOTIC_SCORE_CAP",
    "CHAOTIC_SCORE_CAP",
    "SCOREABLE_KINDS",
    "naturalness_score",
    "shannon_entropy",
    "speaker_balance",
]
