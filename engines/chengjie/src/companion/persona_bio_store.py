# -*- coding: utf-8 -*-
"""人设长传记检索层（SQLite 分块库 + 关键词/语义混合检索）——H1 线，2026-07-27。

「从文档创建人设」只把 背景 ≤1500 字 + 16 条核心记忆 进常驻 prompt，原文档几万字
的**长尾细节**（大学城市/前夫名字/某年经历……）被丢弃——客户追问时 AI 只能编。
本模块把原始文档全文按段落分块入库，聊天时按客户问题**混合检索命中才注入**
（预算内截断），不命中零开销——与 ``companion.bazi`` 同为「注入而非短路」范式
（块经 ``user_context["_persona_bio_block"]`` → ``_build_context_prompt`` 消费）。

设计（对齐 ``persona_media_store`` 的线程安全 SQLite + 模块级单例范式）：
- **语义单元分块**（I1，2026-07-28）：入库单位是 150~280 字的聚焦单元
  （段落 → 中英脚本段 → 句 → 超长硬切，段内相邻单元 40 字重叠），不是
  600 字大块——大块把多主题揉在一起会稀释块向量、让语义信号不可分。
  注入时再做**句级重排**，每个命中单元只取最相关的 1~2 句（跨单元按句去重）。
- 线程安全（单连接 + Lock，``check_same_thread=False``，WAL）；``:memory:`` 供单测。
- 关键词打分与 ``memory_grounding._content_tokens`` 同族：CJK bigram + 拉丁小写词
  （≥3 字符）+ 数字串；命中数 < ``min_hits`` 的块不要（防泛化问题误注入）。
- 入库预计算每块 embedding（热路零重嵌）；查询混合分 =
  ``w_kw * kw_norm + w_sem * cosine``；嵌入不可用时软降级纯关键词（零阻断）。
- **模块级 API 全部软失败**（DB 异常返回 None/空/False，绝不让聊天链崩）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from array import array
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from src.utils.episodic_vector import cosine_similarity

logger = logging.getLogger(__name__)

# 默认库路径（与其它 config/*.db 同处）；main.py 启动期可用 configure_* 显式指定。
DEFAULT_DB_PATH = "config/persona_bio.db"

MAX_BIO_CHARS = 200_000       # 全文上限（store 超截断；路由层同口径拒 413）
CHUNK_MAX_CHARS = 600         # legacy 分块目标上限（``chunk_mode: legacy`` 时生效）
DEFAULT_BLOCK_BUDGET = 600    # 注入块默认预算（总长硬夹）
PREVIEW_CHARS = 120           # meta 预览取首块前 N 字

# ── 语义单元分块（I1 线，2026-07-28）─────────────────────────────────────────
# 600 字符大块把多主题揉在一起（真实 33k 文档里「日语中小学时间线」与「都灵大学
# 履历」同处一块）→ 块向量被稀释、语义信号不可分，只能靠词表 hack 救场。改切
# 150~280 字的**聚焦单元**让向量本身可分；词表 hack 保留但退居兜底。
UNIT_TARGET_CHARS = 240       # 单元目标长度（正文攒到此长度即断）
UNIT_MIN_CHARS = 120          # 低于此长度算碎片，回并前一单元（同脚本且不超上限）
UNIT_MAX_CHARS = 320          # 单元**正文**硬上限（叠加重叠前缀后最多 +UNIT_OVERLAP_CHARS）
UNIT_OVERLAP_CHARS = 40       # 相邻重叠（仅「同段同脚本段内因长度切开」的相邻单元）
DEFAULT_TOP_K = 3             # 单元更小 → 注入多取一个命中
_SCRIPT_MIN_RUN = 60          # 中英脚本切换断段的最小连续长度（防 inline 专名误切）
_MAX_SENTS_PER_UNIT = 2       # 每命中单元最多注入几句
# 邻接扩展后句池变宽，名额若不跟着放宽等于白扩：实测「他在哪里跟你求婚的」的地名句
# 在扩展池里排第 3（0.935），旧的 2 个名额刚好把它切掉。名额随池内单元数放宽但**硬
# 封顶**——预算是真正的稀缺资源，放太多会把后面命中的注入挤掉。
_MAX_SENTS_PER_SPAN = 3
_SENT_SEM_WEIGHT = 2.0        # 句级语义分权重（仅 sentence_semantic:true 时生效）
# 句向量入库门槛：短句嵌入方向不稳（与 `_SEM_MIN_CHARS` 同源教训），且句级重排
# 本来就靠关键词兜底 → 不值得为它们多打一次嵌入。
_SENT_EMB_MIN_CHARS = 12
# 句级语义轨默认开：入库期预算句向量后查询侧零额外往返（见 `_build_sentence_rows`）。
_DEFAULT_SENTENCE_SEMANTIC = True
_CHUNK_CACHE_MAX_PERSONAS = 8   # 解析后单元视图常驻几个人设（LRU）

# 混合检索默认权重（personas.bio_retrieval.* 可覆写；两者归一化到和为 1）
_DEFAULT_SEMANTIC = True
_DEFAULT_KW_WEIGHT = 0.4
_DEFAULT_SEM_WEIGHT = 0.6
_DEFAULT_SEM_FLOOR = 0.55
# 纯语义准入门槛（关键词 0 命中时）：真机冒烟 2026-07-27 显示 0.55 会让
# 英文婚姻碎片（sem≈0.556）压过中文离婚叙事（sem≈0.498）——抬高到 0.62。
_DEFAULT_SEM_FLOOR_KW0 = 0.62
# 纯语义准入的**长度**门槛（kw==0 时）：33k 文档 A/B 校准（2026-07-28）显示
# 误注入主要来自 5~20 字的短碎片——短文本嵌入方向不稳、天然贴近任意查询
# （``"现在的工作："`` 拿到全场最高余弦）。比继续抬 sem_floor 精准：只掐噪声源，
# 不牺牲长单元召回。有关键词证据（kw>0）时不受此限。
_SEM_MIN_CHARS = 40
# 邻接单元扩展（A/B 校准 2026-07-28）：答案跨单元边界的一类——「他在哪里跟你求婚的」
# 命中的是求婚现场单元，但地名 `水晶宫/Palacio de Cristal` 落在相邻单元，40 字重叠
# 没兜住；而把 `unit_overlap_chars` 调到 80 反而净 -1 条（重叠越长 = 向量越糊）。
# 正解是**查询期**扩：命中单元的同段邻居并入句池，由既有句级重排决定要不要取——
# 邻居句得分为 0 就自然不进注入，句数仍受 `_MAX_SENTS_PER_SPAN` 硬封顶。
# A/B（真实 33k 文档 56 例）：90.9% → 93.2%，长尾类 26/26，零回退 → 默认开。
_DEFAULT_NEIGHBOR_EXPAND = True
_NEIGHBOR_RADIUS = 1            # 每侧最多吃几个邻居（不传递）
_NEIGHBOR_SPAN_MAX_CHARS = 720  # 缝合后总长封顶（≈ 3 个满长单元）
# 关键词准入的语义否决线（A/B 校准 2026-07-28）：原词命中权重 2 已等于 eff_min，
# 于是**单个泛化词**就能放行整块——「Do you have any pets at home?」全靠 `home`
# 命中 `work-from-home`/`hometown` 注入了工作段（`pet` 全库零命中）；「你最喜欢什么
# 颜色」全靠 `喜欢` 命中「喜欢独处」。两例的锚词（pets/颜色）都不在文档里，本该空注入，
# 而语义轨其实已经判对（0.44/0.49，都低于 sem_floor）——漏的是「词面巧合可以无视
# 语义反对」这条缺口。故给 kw 准入补一条低位语义否决：低于此线视为两轨互相反对，不注入。
# ⚠ 先试过「不同词门槛」（要求 ≥2 个不同 token 命中）：负例 10/12→12/12 但正例掉 5 条，
# 净 -3。根因是 base_n 数的是 CJK bigram 与单复数变体，彼此并不独立——「你会说几种
# 语言」只需 `语言` 命中，「brothers or sisters」本身是析取（命中一个就对）。别再试。
# 阈值扫描（56 例）：0.42 无变化 / **0.45 → 92.9%，+1 零回退** / 0.48 起吃正例
# （「你家住在哪条街」）/ 0.50 掉 6 条正例。**安全窗很窄**（0.441 < v ≤ ~0.46）：
# 误召的代价是多注入一段无关文字，漏召的代价是 AI 不知道自己住哪——非对称，宁低勿高。
# 调高前必须重跑 A/B，别凭直觉往上推。
_DEFAULT_SEM_VETO = 0.45
# 否决权的生效前提：本次查询在**全库**至少有一个单元拿到这个语义分。达不到 = 嵌入轨
# 对这条查询整体失灵（老库缺向量 / 单元向量被填充稀释到与查询正交 / 端点退化），
# 这种时候让它否决关键词等于把唯一有效的轨也掐掉——I1/M2 要救的正是「单元向量糊了
# 但句向量很锐」那一类。有一个门禁（邻接扩展那条）就是靠这个前提才不被误杀。
_SEM_VETO_MIN_SIGNAL = 0.2
# 文档派生实体别名（M9 线 2026-07-28）：入库期从传记正文共现票选「老公→josé luis」
# 类别名（persona_bio_aliases 表），查询期与全局表/profile 派生表合并。真实 33k
# 文档端到端 A/B：92.9% → **96.4%（+2 零回退）**——「你老公对你好吗」从空注入救成
# 正确注入婚史段；「你爸爸是干什么工作的」也顺带修好（父亲名把此前排第 4、被
# top_k 切掉的父亲名片单元拉进前三——**人名别名本身就是最强的词特异性信号**，
# 泛化词别名（工作→公司）反而净 -1，见 _QUERY_ALIASES 注释）。
_DEFAULT_DOC_ALIASES = True

EmbedFn = Callable[[str], Optional[List[float]]]
_UNSET: Any = object()  # 区分「未设置→可懒取」与「显式 None→禁用嵌入」

_DDL = """
CREATE TABLE IF NOT EXISTS persona_bio_chunks (
    persona_id TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    text       TEXT NOT NULL DEFAULT '',
    embedding  TEXT,
    PRIMARY KEY (persona_id, idx)
);
CREATE TABLE IF NOT EXISTS persona_bio_meta (
    persona_id TEXT NOT NULL PRIMARY KEY,
    chars      INTEGER NOT NULL DEFAULT 0,
    chunks     INTEGER NOT NULL DEFAULT 0,
    updated_ts REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS persona_bio_sents (
    persona_id TEXT NOT NULL,
    sent_key   TEXT NOT NULL,
    embedding  TEXT NOT NULL,
    PRIMARY KEY (persona_id, sent_key)
);
CREATE TABLE IF NOT EXISTS persona_bio_aliases (
    persona_id TEXT NOT NULL PRIMARY KEY,
    aliases    TEXT NOT NULL DEFAULT '{}'
);
"""

_BLOCK_HEADER = (
    "【人设资料参考】以下是\u201c你\u201d自己的过往资料片段"
    "（仅当对方问到相关话题时自然引用，绝不照读、绝不一次性全说）："
)

# ── 纯函数：分块 / 关键词打分 ────────────────────────────────────────────────

_TITLE_TAIL_RE = re.compile(r"[：:]\s*$")   # 字段式标题行（冒号收尾）→ 归属下文
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z']{2,}")   # 拉丁词 ≥3 字符（短词噪声大）
_NUM_RE = re.compile(r"\d+")


def _split_bio_chunks_legacy(text: str, max_chars: int = CHUNK_MAX_CHARS) -> List[str]:
    """旧行为（``chunk_mode: legacy`` 回退口）：段落贪心合并到 ≤max_chars。"""
    limit = max(1, int(max_chars))
    paras = [p.strip() for p in str(text or "").splitlines()]
    pieces: List[str] = []
    for p in paras:
        if not p:
            continue
        while len(p) > limit:          # 超长单段硬切
            pieces.append(p[:limit])
            p = p[limit:]
        if p:
            pieces.append(p)
    chunks: List[str] = []
    buf = ""
    for piece in pieces:
        if not buf:
            buf = piece
        elif len(buf) + 1 + len(piece) <= limit:
            buf = buf + "\n" + piece
        else:
            chunks.append(buf)
            buf = piece
    if buf:
        chunks.append(buf)
    return chunks


def _char_script(ch: str) -> str:
    """字符脚本类：``c``=CJK/假名，``l``=拉丁字母，``''``=中性（数字/标点/空白）。"""
    if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff":
        return "c"
    if ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
        return "l"
    return ""


def script_segments(text: str, min_run: int = _SCRIPT_MIN_RUN) -> List[tuple]:
    """段落 → ``[(片段, 脚本)]``，中英脚本切换处断开（主题边界启发）。

    真实人设文档大量「中文叙事 + 英文对照」，混进同一向量最毁语义。但
    ``前夫Marco是都灵本地人`` 这种 inline 专名不能切——**连续同脚本不足
    ``min_run`` 的短游程一律并入邻段**，只有成规模的脚本切换才算主题边界。
    """
    s = str(text or "")
    if not s:
        return []
    runs: List[List[Any]] = []          # [script, start, end)
    cur = ""
    start = 0
    for i, ch in enumerate(s):
        k = _char_script(ch)
        if not k:
            continue                     # 中性字符归属当前游程
        if not cur:
            cur, start = k, 0            # 前导中性字符并入首段
        elif k != cur:
            runs.append([cur, start, i])
            cur, start = k, i
    if not cur:
        return [(s, "")]                 # 纯数字/标点段落
    runs.append([cur, start, len(s)])

    floor = max(1, int(min_run))
    changed = True
    while changed and len(runs) > 1:
        changed = False
        i = 1
        while i < len(runs):             # 同脚本相邻游程合并
            if runs[i][0] == runs[i - 1][0]:
                runs[i - 1][2] = runs[i][2]
                runs.pop(i)
                changed = True
            else:
                i += 1
        for i in range(len(runs)):       # 短游程并入邻段（一次一个，回环再判）
            if runs[i][2] - runs[i][1] >= floor or len(runs) <= 1:
                continue
            if i > 0:
                runs[i - 1][2] = runs[i][2]
                runs.pop(i)
            else:
                runs[1][1] = runs[0][1]
                runs.pop(0)
            changed = True
            break
    return [(s[a:b], k) for k, a, b in runs]


# 句末：CJK 标点直接断；ASCII . ! ? 需后接空白/结尾才断（防 3.5 / U.S. 被切碎）
_CJK_SENT_END = "。！？；"
_ASCII_SENT_END = ".!?"


def split_sentences(text: str) -> List[str]:
    """文本 → 句列表（保留句末标点；纯空白片段丢弃）。"""
    s = str(text or "")
    out: List[str] = []
    buf: List[str] = []
    n = len(s)
    for i, ch in enumerate(s):
        buf.append(ch)
        if ch in _CJK_SENT_END or ch == "\n":
            out.append("".join(buf))
            buf = []
        elif ch in _ASCII_SENT_END and (i + 1 >= n or s[i + 1].isspace()):
            out.append("".join(buf))
            buf = []
    if buf:
        out.append("".join(buf))
    return [p.strip() for p in out if p.strip()]


def _unit_pieces(text: str, target: int) -> List[tuple]:
    """全文 → ``[(片段, 段号, 脚本段号, 脚本)]``：段落 → 脚本段 → 句 → 超长硬切。"""
    pieces: List[tuple] = []
    para_idx = 0
    for line in str(text or "").splitlines():
        para = line.strip()
        if not para:
            continue
        for seg_idx, (seg_text, script) in enumerate(script_segments(para)):
            for sent in split_sentences(seg_text):
                rest = sent
                while len(rest) > target:        # 无句读的超长串 → 硬切
                    pieces.append((rest[:target], para_idx, seg_idx, script))
                    rest = rest[target:]
                if rest:
                    pieces.append((rest, para_idx, seg_idx, script))
        para_idx += 1
    return pieces


def _pack_units(pieces: List[tuple], target: int, unit_max: int) -> List[list]:
    """句片段 → 单元 ``[[正文, 首片键, 末片键], ...]``（键=``((段号,脚本段号), 脚本)``）。

    段落 / 脚本段边界**总是断开**（边界优先于长度）；段内按长度攒到 target 即断。
    产生的短碎片交给下一步回并——先断后并比「边界处凑长度」更能保住段落纯度。
    """
    units: List[list] = []
    buf = ""
    first: Any = None
    last: Any = None

    for text, para, seg, script in pieces:
        key = ((para, seg), script)
        if buf:
            joiner = "\n" if para != last[0][0] else ""
            if key[0] == last[0] and len(buf) + len(joiner) + len(text) <= unit_max:
                buf += joiner + text
                last = key
                if len(buf) >= target:
                    units.append([buf, first, last])
                    buf, first, last = "", None, None
                continue
            units.append([buf, first, last])
        buf, first, last = text, key, key
        if len(buf) >= target:
            units.append([buf, first, last])
            buf, first, last = "", None, None
    if buf:
        units.append([buf, first, last])
    return units


def _is_title_fragment(text: str) -> bool:
    """整体是 ``xxx：`` 形式的标题行（冒号收尾，中英文冒号皆算）。"""
    return bool(_TITLE_TAIL_RE.search(str(text or "")))


def _merge_short_units(units: List[list], unit_min: int, unit_max: int) -> List[list]:
    """碎片（< unit_min）回并邻近单元；跨脚本不并（宁可短，别污染向量）。

    **标题行归属下文**（I1 校准，2026-07-28）：``母亲名字：`` / ``现在的工作：``
    这类冒号收尾的字段式标题，语义属于**后**文而非前文。原先一律回并前一单元
    会留下 5 字的孤立标题单元——短文本向量方向不稳，真实 33k 文档里
    ``"现在的工作："`` 拿到了全场最高余弦（0.602）把正确答案挤到第二名。
    改为与后一单元合并（标题 + 内容 = 一个语义单元）；与后合并会超
    ``unit_max`` 或跨脚本时，退回与前合并的旧行为。

    ⚠ **``unit_min`` 在这里是愿望值不是不变量**，别当 bug 修（M5 诊断 2026-07-28）：
    单趟 best-effort + 两道硬闸门（同脚本 / 不超 ``unit_max``）→ 真实 33k 文档
    仍留 30% 短块。根因已查明：``script_segments`` 按**整行主导字符**打脚本标签，
    而中文名片行的字段名是中文、值是拉丁（``公司名称：W Barcelona``），字节大头在值
    → 整行被标成 ``l``，与上下文中文名片行跨脚本，同脚本闸门把整段名片切成
    16/27/12 字碎块（76 个残留短单元里 59 个卡在这一条）。
    但**修它是净回退**：11 种合并策略在真实语料上单跑全部劣于基线，「彻底让
    ``unit_min`` 生效」最差（-5/-6，强行凑长度把无关内容缝进同一向量）；即便配上
    名片降权收窄 + 别名补齐 + ``top_k=4`` 四件套齐上，在**当前**检索栈
    （句级语义 + 邻接扩展 + 语义否决）上仍是 -2，且目标用例一条没修好——它那 +3
    是在缺 I5/M2 的旧栈上测的，与现有层重叠。要重启这条路线必须整体重测，
    别只改分块。诊断脚本 ``tmp_chunk_diag.py``（结构分析 + 11 策略 A/B）可复跑。
    """
    out: List[list] = []
    pending = [list(u) for u in units]
    i = 0
    while i < len(pending):
        u = pending[i]
        body = str(u[0])
        short = len(body) < unit_min
        if short and _is_title_fragment(body) and i + 1 < len(pending):
            nxt = pending[i + 1]
            if u[2][1] == nxt[1][1]:                       # 同脚本才并
                joiner = "\n" if u[2][0][0] != nxt[1][0][0] else ""
                if len(body) + len(joiner) + len(nxt[0]) <= unit_max:
                    pending[i + 1] = [body + joiner + nxt[0], u[1], nxt[2]]
                    i += 1
                    continue
            # 与后合并超限/跨脚本 → 落到下面的「与前合并」旧路径
        if out and short and u[1][1] == out[-1][2][1]:
            joiner = "\n" if u[1][0][0] != out[-1][2][0][0] else ""
            if len(out[-1][0]) + len(joiner) + len(body) <= unit_max:
                out[-1][0] = out[-1][0] + joiner + body
                out[-1][2] = u[2]
                i += 1
                continue
        out.append(u)
        i += 1
    return out


def _apply_overlap(units: List[list], overlap: int) -> List[str]:
    """相邻单元加重叠前缀（RAG 标准做法，防答案正好跨界被切断）。

    只在「同段同脚本段内因长度切开」的相邻单元之间加——段落/脚本切换本就是
    主题边界，跨界重叠只会把上一主题的尾巴灌进下一个单元的向量。
    """
    out: List[str] = []
    n = max(0, int(overlap))
    for i, u in enumerate(units):
        body = str(u[0])
        if i > 0 and n > 0 and u[1][0] == units[i - 1][2][0]:
            tail = str(units[i - 1][0])[-n:].lstrip()
            if tail:
                body = tail + body
        out.append(body)
    return out


def units_are_contiguous(prev_text: str, cur_text: str, overlap: int) -> bool:
    """``cur`` 是否紧接 ``prev``（同段因长度切开）。

    判据＝反解 ``_apply_overlap`` 的重叠前缀：它只在「同段同脚本段内因长度切开」
    的相邻单元间加，所以**重叠前缀存在本身就是同段的证据**，无需给库表加段落
    列。两者必须成对修改——``_apply_overlap`` 的前缀口径一变，这里的反解即失效。
    """
    n = max(0, int(overlap))
    if n <= 0:
        return False
    prev, cur = str(prev_text or ""), str(cur_text or "")
    tail = prev[-n:].lstrip()
    return bool(tail) and cur.startswith(tail)


def stitch_units(texts: Sequence[str], overlap: int) -> str:
    """把相邻单元缝回原文：连续的去掉重复的重叠前缀，不连续的换行相接。"""
    parts: List[str] = []
    prev = ""
    n = max(0, int(overlap))
    for t in texts:
        cur = str(t or "")
        if not cur:
            continue
        if prev and units_are_contiguous(prev, cur, n):
            parts.append(cur[len(prev[-n:].lstrip()):])
        else:
            parts.append(("\n" if parts else "") + cur)
        prev = cur
    return "".join(parts)


def neighbor_span(
    texts: Sequence[str], pos: int, overlap: int,
    max_chars: int = _NEIGHBOR_SPAN_MAX_CHARS,
    radius: int = _NEIGHBOR_RADIUS,
) -> tuple:
    """命中单元 ``pos`` 向两侧吃**同段**邻居 → ``(start, end)`` 左闭右开。

    只吃连续邻居（段落/脚本边界天然截断），半径与总长双封顶——放开会把整段
    灌进句池，既拖慢重排又让预算被无关句抢走。
    """
    if not texts or not (0 <= int(pos) < len(texts)):
        return (0, 0)
    p = int(pos)
    start, end = p, p + 1
    budget = max(0, int(max_chars)) - len(str(texts[p] or ""))
    for _ in range(max(0, int(radius))):
        grew = False
        if start > 0 and units_are_contiguous(texts[start - 1], texts[start], overlap):
            cost = len(str(texts[start - 1] or ""))
            if cost <= budget:
                start -= 1
                budget -= cost
                grew = True
        if end < len(texts) and units_are_contiguous(texts[end - 1], texts[end], overlap):
            cost = len(str(texts[end] or ""))
            if cost <= budget:
                end += 1
                budget -= cost
                grew = True
        if not grew:
            break
    return (start, end)


def split_bio_chunks(
    text: str,
    max_chars: int = CHUNK_MAX_CHARS,
    *,
    mode: Optional[str] = None,
    target_chars: Optional[int] = None,
    min_chars: Optional[int] = None,
    max_unit_chars: Optional[int] = None,
    overlap_chars: Optional[int] = None,
) -> List[str]:
    """全文 → 检索单元列表。

    ``chunk_mode: unit``（默认）＝语义单元分块，切分优先级
    ① 空行/段落边界 ② 中英脚本切换（主题边界启发）③ 句末标点 ④ 超长硬切；
    相邻同段单元之间加 ``UNIT_OVERLAP_CHARS`` 重叠；过短碎片回并前一单元
    （冒号收尾的字段式标题行例外——归属**后**一单元，见 ``_merge_short_units``）。
    单元**正文** ≤ ``UNIT_MAX_CHARS``，含重叠前缀最多再多 ``UNIT_OVERLAP_CHARS``。

    ``chunk_mode: legacy`` 回退旧的「段落贪心合并到 ``max_chars``」行为
    （``max_chars`` 仅在 legacy 下生效）。
    """
    cfg = _retrieval_settings()
    eff_mode = str(mode or cfg.get("chunk_mode") or "unit").strip().lower()
    if eff_mode == "legacy":
        return _split_bio_chunks_legacy(text, max_chars)

    target = max(16, int(target_chars if target_chars is not None
                         else cfg.get("unit_target_chars", UNIT_TARGET_CHARS)))
    unit_max = max(target, int(max_unit_chars if max_unit_chars is not None
                               else cfg.get("unit_max_chars", UNIT_MAX_CHARS)))
    unit_min = max(1, min(target, int(min_chars if min_chars is not None
                                      else cfg.get("unit_min_chars", UNIT_MIN_CHARS))))
    overlap = max(0, int(overlap_chars if overlap_chars is not None
                         else cfg.get("unit_overlap_chars", UNIT_OVERLAP_CHARS)))

    pieces = _unit_pieces(text, target)
    if not pieces:
        return []
    units = _merge_short_units(
        _pack_units(pieces, target, unit_max), unit_min, unit_max)
    return [u for u in _apply_overlap(units, overlap) if u.strip()]


# 会话虚词字符（真机冒烟教训 2026-07-27：「你大学在哪个城市读的呀」的
# 「的呀/在哪/你大」等虚词 bigram 把无关块顶成了最高分——查询侧 bigram
# 任一字符落在此集即剔除，只留内容词；全虚词查询 → 空集 → 不注入，
# 与「宁缺勿错」一致。刻意不含 前/后/名/家/好 等可构成内容词的字。）
_QUERY_STOP_CHARS = frozenset(
    "你我他她它的了是在有个么什哪怎呀啊呢吗吧哦嘛啦这那就都也还很"
    "不没要想说和跟与去过来到得着为"
)
# 拉丁侧虚词表（对应 CJK 的 `_QUERY_STOP_CHARS`——原先只有中文侧有停用过滤，
# 英文查询裸奔：``Do you have any pets at home?`` 里 have/any/home 与内容词
# 同权，泛用词把无关块顶上去而真正的 pet 一个都没命中）。刻意**只收功能词**，
# 不收 work/job/home/name/born 这类可作内容锚点的实词。
_QUERY_STOP_WORDS = frozenset("""
about above after again against all also and any are was were been being
because before below between both but can could did does doing done down
during each few for from further had has have having her hers here him his
its into just may might more most must nor not now off once only other our
ours out over own please same she should some such than that the their
theirs them then there these they this those too under until upon very
what when where which while who whom whose why will with within without you
your yours yeah yes okay thanks thank tell say said know like want get got
make made take taken come came would shall
""".split())


def _fold_plural(word: str) -> Optional[str]:
    """朴素单复数回退：``sisters→sister``（文档写单数时关键词轨才够得着）。"""
    if len(word) <= 3 or not word.endswith("s") or word.endswith("ss"):
        return None
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    return word[:-1]


def query_tokens(query: str) -> set:
    """query → CJK bigram ∪ 拉丁小写词(≥3 字符) ∪ 数字串。

    与 ``memory_grounding._content_tokens`` 同族口径（bigram 只在连续 CJK 段内
    生成，不跨标点/空格）；含会话虚词字符的 bigram 剔除（见 ``_QUERY_STOP_CHARS``）。

    虚词剔除是**逐 bigram** 判定的，因此夹在两个内容字中间的虚词会把这对内容字
    一起带走：``你结过婚吗`` 的 结/过/婚 four bigram 全含虚词 → token 空集 →
    整条查询检索不到任何东西。故在原口径之外再补一轮「压掉虚词后重新成对」的
    bigram（``结过婚`` → ``结婚``）。真实 33k 文档 A/B（2026-07-28）：叠加拉丁
    停用词与单复数回退后命中率 70.5% → 79.5%，负例误召不变。
    """
    s = str(query or "")
    toks: set = set()
    for run in _CJK_RUN_RE.findall(s):
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg[0] in _QUERY_STOP_CHARS or bg[1] in _QUERY_STOP_CHARS:
                continue
            toks.add(bg)
        squeezed = "".join(ch for ch in run if ch not in _QUERY_STOP_CHARS)
        for i in range(len(squeezed) - 1):
            toks.add(squeezed[i:i + 2])
    for t in _LATIN_RE.findall(s):
        low = t.lower()
        if low in _QUERY_STOP_WORDS:
            continue
        toks.add(low)
        singular = _fold_plural(low)
        if singular and singular not in _QUERY_STOP_WORDS:
            toks.add(singular)
    toks.update(_NUM_RE.findall(s))
    return toks


# 查询别名（真机冒烟再优化）：客户口语 ≠ 文档用词时，关键词轨先扩一跳。
# 值是「整词/短语」加入 token 集（不是再跑 bigram），拉丁小写。
# 前夫→丈夫/离婚：叙事块靠「离婚」召回；名片「婚否/Marital Status: Divorced」
# 由 `_PROFILE_CARD_RE` 降权，避免压过长叙事（真机冒烟 2026-07-27）。
_QUERY_ALIASES: Dict[str, tuple] = {
    "前夫": ("丈夫", "离婚", "husband", "divorce", "ex-husband"),
    "前妻": ("妻子", "离婚", "wife", "divorce", "ex-wife"),
    "大学": ("本科", "毕业", "university", "college", "bachelor", "都灵"),
    "男友": ("男朋友", "恋人", "boyfriend"),
    "女友": ("女朋友", "恋人", "girlfriend"),
    # 亲属称谓：客户说口语（妈妈/爸爸），文档写书面语（母亲 29 次 / 父亲 37 次，
    # 「妈妈」「爸爸」全文零出现）→ kw 恒 0 被 sem_floor_kw0 拒掉。这是纯词汇
    # 同义映射，值刻意只放**称谓本身**——不引「离婚」那类状态词（会命中人设
    # 名片的「婚姻状况：离婚」，2026-07-27 踩过）。单字键只做触发，不进 token。
    "妈妈": ("母亲", "老妈", "mother"),
    "妈": ("母亲", "老妈", "mother"),
    "母上": ("母亲", "老妈", "mother"),
    "爸爸": ("父亲", "老爸", "father"),
    "爸": ("父亲", "老爸", "father"),
    "哥哥": ("兄弟", "brother"),
    "弟弟": ("兄弟", "brother"),
    "姐姐": ("姊妹", "sister"),
    "妹妹": ("姊妹", "sister"),
    "老公": ("丈夫", "husband"),
    "丈夫": ("老公", "husband"),
    "老婆": ("妻子", "wife"),
    "妻子": ("老婆", "wife"),
    # 职业/学历：文档写「职业：设计师」「大阪府立枚方高等学校」，客户问「工作」
    # 「高中」——「工作」「高中」全文零出现 → kw 恒 0，而这些块语义分 0.53~0.58
    # 又够不到 sem_floor_kw0=0.62 → 整块空注入（A/B 校准 2026-07-28）。
    # 同亲属别名的教训：值只放**同义名词**，不引状态词。
    # ⚠ 试过补「职位/公司/company/position」（工作名片写的是「公司名称：/职位：」，
    # 「职业/岗位」在当前工作名片上零出现）：**净 -1**——问「你父亲的工作」时那些
    # 泛化词把分喂给了讲公司的块，稀释掉唯一的判别词「父亲」，而目标用例
    # `Where do you work now?` 一条都没修好。同族教训见 sem_veto 注释。
    "工作": ("职业", "岗位", "job", "occupation"),
    "上班": ("职业", "岗位", "工作"),
    "职业": ("工作", "岗位", "occupation"),
    "work": ("职业", "岗位"),
    "job": ("职业", "岗位"),
    "occupation": ("职业", "岗位"),
    "高中": ("高等学校", "high school"),
    "初中": ("中学校", "junior high"),
    "小学": ("小学校", "elementary"),
}
_PROFILE_CARD_RE = re.compile(
    r"婚否|Marital\s*Status|性别：|Gender:|生肖：|Chinese\s*Zodiac",
    re.I,
)
# ⚠ 「整块降权」与「把名片行合并成整段」天生冲突：合并后 200+ 字的名片块里有
# 身高/语言/爱好等真答案，整块降权会**连坐**把 kw 压成 0 → 空注入（实测「你身高
# 多少」「你会说几种语言」直接空）。曾试过加长度闸门（只对 <120 字碎块降权）来配
# 合分块改动，但分块那条本身是净回退（见 _merge_short_units 注释），闸门也就没了
# 存在理由——真要重启那条路线，两者必须同批改并整体 A/B。
# 寒暄查询（今天天气真好）——token 全集落在此集合则直接不检索。
_CHITCHAT_TOKS = frozenset({
    "今天", "天天", "天气", "气真", "真好", "下雨", "怎么样", "好多",
    "哈哈", "嘿嘿", "在吗", "你好", "晚安", "早安", "早呀", "嗨嗨",
})
# 问句壳 token：打分时剔除，避免「叫什么名字」命中所有含「英文名字」的块。
_QUESTION_SHELL_TOKS = frozenset({
    "名字", "什么", "叫什", "哪里", "哪个", "多少", "几岁", "谁呀",
})


def expand_query_tokens(
    query: str, aliases: Optional[Mapping[str, Sequence[str]]] = None,
) -> set:
    """``query_tokens`` + 别名整词扩展（解决「前夫≠丈夫」类词汇错配）。

    ``aliases`` 缺省用全局静态表；检索链会传入 ``persona_query_aliases(pid)``
    ——全局表 ⊕ 该人设档案派生的实体名（见 M9 注释）。
    """
    toks = set(query_tokens(query))
    s = str(query or "")
    s_low = s.lower()
    for tip, alts in (aliases if aliases is not None else _QUERY_ALIASES).items():
        if tip in s or tip.lower() in s_low:
            for a in alts:
                toks.add(a.lower() if a.isascii() else a)
    return toks


def _keyword_score(q_tokens: set, text: str) -> int:
    """块文本对 query token 集的命中数（拉丁不区分大小写；CJK 不受 lower 影响）。"""
    if not q_tokens:
        return 0
    low = str(text or "").lower()
    return sum(1 for t in q_tokens if t in low)


def _keyword_score_weighted(
    raw_toks: set, all_toks: set, text: str, low: Optional[str] = None,
) -> int:
    """原词命中权重 2、仅别名命中权重 1；名片块（婚否/Marital Status）分×0.25。

    ``low`` 可由调用方传入预折的小写文本（热路每条消息都要对全库跑一遍，
    重复 ``lower()`` 是纯浪费）。
    """
    if not all_toks:
        return 0
    if low is None:
        low = str(text or "").lower()
    score = 0
    for t in all_toks:
        if not t or t not in low:
            continue
        score += 2 if t in raw_toks else 1
    if score and _PROFILE_CARD_RE.search(str(text or "")):
        score = max(1, int(round(score * 0.25))) if score >= 4 else 0
        # 短名片上的「离婚/名字」别名噪声：总分被压到 0，让位给叙事块
    return score


def _is_chitchat_query(raw_toks: set) -> bool:
    """查询 token 全是寒暄词 → 不进检索（「今天天气真好」曾误中秋季出游段）。"""
    if not raw_toks:
        return False
    return all(t in _CHITCHAT_TOKS for t in raw_toks)


def _cjk_ratio(text: str) -> float:
    """文本中 CJK 字符占比（空→0）。用于中文查询时软降权英文碎片块。"""
    s = str(text or "")
    if not s:
        return 0.0
    n = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    return n / max(1, len(s))


def excerpt_around_hits(
    text: str, q_tokens: set, max_chars: int,
) -> str:
    """围绕关键词命中截取窗口（真机：大学块前端是日语中小学时间线，
    从块首截会丢掉同块后部的「都灵大学」——先定位命中再取窗）。

    无命中 / 无 token → 退回原文前 ``max_chars`` 字。
    """
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    limit = max(0, int(max_chars))
    if not t or limit <= 0:
        return ""
    if not q_tokens:
        return t[:limit]
    low = t.lower()
    best = -1
    for tok in q_tokens:
        if not tok:
            continue
        pos = low.find(str(tok).lower())
        if pos >= 0 and (best < 0 or pos < best):
            best = pos
    if best < 0:
        return t[:limit]
    # 聚焦命中窗（真机：budget≥块长时 focus==len → start 被钳成 0，
    # 大学块仍从日语中小学起截）。命中靠后时强制缩小窗口锚过去。
    want = min(limit, 280, len(t))
    if best > want // 4 and want > 120:
        # 给命中点留前 1/5 + 后文，但不要吞掉整块前缀噪声
        want = max(120, min(want, (len(t) - best) + want // 4 + 60))
        want = min(want, len(t), limit)
    start = max(0, min(best - want // 5, len(t) - want))
    if not (start <= best < start + want):
        start = max(0, min(best, max(0, len(t) - want)))
    out = t[start:start + want]
    if start > 0:
        out = "…" + out[1:]
    return out


def rank_sentences(
    unit_text: str,
    query_tokens: Any,
    query_emb: Optional[List[float]] = None,
    sim_fn: Any = None,
) -> List[tuple]:
    """单元内句级重排 → ``[(句, 分)]`` 按分降序（同分保原文序）。

    **默认只用关键词**：句级嵌入要按句数放大热路延迟，除非
    ``personas.bio_retrieval.sentence_semantic: true`` 才由调用方传 ``sim_fn``
    （给 ``query_emb`` 时按 ``sim_fn(query_emb, 句)`` 调，否则 ``sim_fn(句)``）。
    """
    toks = set(query_tokens or ())
    ranked: List[tuple] = []
    for i, sent in enumerate(split_sentences(unit_text)):
        score = float(_keyword_score(toks, sent))
        if sim_fn is not None:
            try:
                sem = sim_fn(query_emb, sent) if query_emb is not None else sim_fn(sent)
                score += _SENT_SEM_WEIGHT * float(sem or 0.0)
            except Exception:
                logger.debug("[persona_bio] 句级语义打分失败", exc_info=True)
        ranked.append((sent, score, i))
    ranked.sort(key=lambda x: (-x[1], x[2]))
    return [(s, sc) for s, sc, _ in ranked]


def pick_unit_sentences(
    unit_text: str,
    query_tokens: Any,
    max_sents: int = _MAX_SENTS_PER_UNIT,
    query_emb: Optional[List[float]] = None,
    sim_fn: Any = None,
) -> List[str]:
    """单元 → 最相关的 1~2 句，**按原文顺序**输出（叙事顺序不打乱）。

    一句都没得分（单元靠语义准入）→ 返回空，由调用方走命中窗摘录兜底。
    """
    return pick_span_sentences(
        [unit_text], query_tokens, max_sents, query_emb, sim_fn)


def pick_span_sentences(
    unit_texts: Sequence[str],
    query_tokens: Any,
    max_sents: int = _MAX_SENTS_PER_UNIT,
    query_emb: Optional[List[float]] = None,
    sim_fn: Any = None,
) -> List[str]:
    """跨单元选最相关的 1~2 句，按文档序输出。

    **逐单元各自分句**，绝不先把单元拼成一整段再切——句向量是入库期按**单元内**
    分句算好的（键＝句文本），拼接会让跨界句变成一个新字符串、查不到向量、语义分
    恒 0。实测代价具体：真实文档里一句 240 字的答案句（sem 0.467）被拼成 408 字后
    降到 0 分，`sc > 0` 直接过滤掉——扩展句池反而弄丢了本来有的信号。

    单元重叠会让同一句在相邻单元各出现一次 → 按句文本去重，保最早出现位置。
    """
    scored: Dict[str, tuple] = {}          # 句 → (分, 文档序)
    for ui, raw in enumerate(unit_texts):
        text = str(raw or "")
        if not text:
            continue
        order = {}
        for si, s in enumerate(split_sentences(text)):
            order.setdefault(s, si)
        for s, sc in rank_sentences(text, query_tokens, query_emb, sim_fn):
            pos = (ui, order.get(s, 0))
            prev = scored.get(s)
            if prev is None or sc > prev[0]:
                scored[s] = (sc, prev[1] if prev else pos)
    items = [(sc, pos, s) for s, (sc, pos) in scored.items() if sc > 0]
    items.sort(key=lambda t: (-t[0], t[1]))       # 分降序；同分保文档序
    keep = items[: max(1, int(max_sents))]
    keep.sort(key=lambda t: t[1])
    return [s for _sc, _pos, s in keep]


# ── 注入观测（模块级轻量计数器；metrics 路由接线留给下一波）──────────────────

_STATS_LOCK = threading.Lock()
_STATS: Dict[str, int] = {
    "queries": 0, "hits": 0, "empty": 0, "embed_fail": 0, "hits_total": 0,
}


def _bump(field: str, n: int = 1) -> None:
    with _STATS_LOCK:
        _STATS[field] = int(_STATS.get(field, 0)) + int(n)


def retrieval_stats_snapshot() -> Dict[str, Any]:
    """检索观测快照：``{queries, hits, empty, embed_fail, hit_rate, avg_hits}``。"""
    with _STATS_LOCK:
        q = int(_STATS.get("queries", 0))
        h = int(_STATS.get("hits", 0))
        e = int(_STATS.get("empty", 0))
        f = int(_STATS.get("embed_fail", 0))
        tot = int(_STATS.get("hits_total", 0))
    return {
        "queries": q, "hits": h, "empty": e, "embed_fail": f,
        "hit_rate": round(h / q, 4) if q else 0.0,
        "avg_hits": round(tot / q, 4) if q else 0.0,
    }


def reset_retrieval_stats() -> None:
    """归零观测计数（测试钩子；``reset_persona_bio_store`` 会一并调用）。"""
    with _STATS_LOCK:
        for k in list(_STATS):
            _STATS[k] = 0


def _sent_key(sent: str) -> str:
    """句 → 句向量表主键（按去空白后的原文取 sha1，跨单元重叠句天然去重）。"""
    return hashlib.sha1(
        "".join(str(sent or "").split()).encode("utf-8")).hexdigest()


def _embeddable_sentences(chunks: Any) -> List[str]:
    """待入库句向量的句集合（保序去重 + 短句剔除）。"""
    out: List[str] = []
    seen: set = set()
    for c in chunks or ():
        for sent in split_sentences(str(c or "")):
            s = sent.strip()
            if len(s) < _SENT_EMB_MIN_CHARS:
                continue
            k = _sent_key(s)
            if k in seen:
                continue
            seen.add(k)
            out.append(s)
    return out


def _dumps_embedding(vec: Optional[List[float]]) -> Optional[str]:
    if not vec:
        return None
    try:
        return json.dumps([float(x) for x in vec], separators=(",", ":"))
    except Exception:
        return None


def _loads_embedding(raw: Any) -> Optional[List[float]]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        data = json.loads(s)
    except Exception:
        return None
    if not isinstance(data, list) or not data:
        return None
    try:
        return [float(x) for x in data]
    except Exception:
        return None


def _normalize_weights(w_kw: float, w_sem: float) -> tuple:
    """两者归一化到和为 1；非法/全 0 回落默认。"""
    try:
        a = float(w_kw)
        b = float(w_sem)
    except (TypeError, ValueError):
        return _DEFAULT_KW_WEIGHT, _DEFAULT_SEM_WEIGHT
    s = a + b
    if s <= 0:
        return _DEFAULT_KW_WEIGHT, _DEFAULT_SEM_WEIGHT
    return a / s, b / s


# ── 嵌入 / 检索配置（模块级可注入；测试钉死 stub，生产懒取）────────────────

_EMBED_FN: Any = _UNSET
_RETRIEVAL_CFG_OVERRIDE: Optional[Dict[str, Any]] = None
_LAZY_EMBED_CACHE: Any = _UNSET
_CFG_LOCK = threading.Lock()

# 查询嵌入 LRU（键=嵌入函数身份 + 归一化 query）：热路每条入站消息都要嵌一次
# query，同一问法反复出现（「你大学在哪读的」）时不该反复打网络。进程级即可，
# 不做 TTL——传记库换了也只是查询向量复用，语义不变。
_QUERY_EMB_CACHE: "OrderedDict[tuple, List[float]]" = OrderedDict()
_QUERY_EMB_CACHE_MAX = 256
# 嵌入端点抖动时，每条消息都卡一次超时 → 记一次失败后冷却窗内直接走纯关键词。
_EMBED_FAIL_COOLDOWN_SEC = 60
_EMBED_FAIL_UNTIL = 0.0
_EMB_LOCK = threading.Lock()


def _query_cache_key(fn: Any, text: str) -> tuple:
    norm = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return (id(fn), getattr(fn, "__qualname__", ""), norm)


def _note_embed_failure() -> None:
    global _EMBED_FAIL_UNTIL
    with _EMB_LOCK:
        _EMBED_FAIL_UNTIL = time.time() + _EMBED_FAIL_COOLDOWN_SEC
    _bump("embed_fail")


def clear_query_embedding_cache() -> None:
    """清查询向量缓存 + 解除嵌入失败冷却（换 embed_fn / 测试隔离）。"""
    global _EMBED_FAIL_UNTIL
    with _EMB_LOCK:
        _QUERY_EMB_CACHE.clear()
        _EMBED_FAIL_UNTIL = 0.0


def set_embed_fn(fn: Optional[EmbedFn]) -> None:
    """注入/清空嵌入函数。显式 ``None`` = 禁用嵌入（不懒取网络端点）。"""
    global _EMBED_FN, _LAZY_EMBED_CACHE
    with _CFG_LOCK:
        _EMBED_FN = fn
        _LAZY_EMBED_CACHE = _UNSET
    clear_query_embedding_cache()   # 换了嵌入函数，旧查询向量不可复用


def set_retrieval_cfg(cfg: Optional[Dict[str, Any]]) -> None:
    """测试钩子：覆写 ``personas.bio_retrieval`` 混合检索参数（``None`` 清覆写）。"""
    global _RETRIEVAL_CFG_OVERRIDE
    with _CFG_LOCK:
        _RETRIEVAL_CFG_OVERRIDE = dict(cfg) if isinstance(cfg, dict) else None


# ── 人设实体别名（M9）：全局静态表 ⊕ 按人设从档案派生 ──────────────────────
# ``_QUERY_ALIASES`` 是全局静态表，只能修「口语 vs 书面语」这类通用同义
# （妈妈→母亲）。它救不了**人设特有的实体名**：真实档案里父亲一律写全名
# ``José Leandro Navarro``，「爸爸」在叙事单元中一次都没出现 → 关键词分恒 0
# → 空注入。``persona_entity_alias`` 从档案派生「爸爸 → josé leandro」这类
# 别名补一跳。56 例校准集实测 92.9% → 94.6%（+1 修好，0 打坏）。
#
# **刻意不从传记正文正文派生**（原 M9 设想，实测否决）：正文里「老公」出现
# 0 次、「前夫」0 次、「丈夫」仅 1 次且指的是**她父亲**（「身为一个丈夫和
# 父亲的所有优点」）；配偶每次登场都写作「西班牙人（José Luis Jerónimo）」。
# 锚点式（称谓＋邻近人名）在这种语料上不但抽不到配偶，还会把父亲抽成丈夫
# ——错别名比没别名更糟（它会**稳定地**注入错段）。配偶这类要跨句推理
# （相恋→求婚→结婚→离婚）的关系，只有 LLM 抽取轨能给，见 backlog。
_ALIAS_PROVIDER: Any = _UNSET
# pid -> (档案签名, 合并后的别名表)。档案改了签名就变 → 自动重算。
_ALIAS_CACHE: "OrderedDict[str, tuple]" = OrderedDict()
_ALIAS_CACHE_MAX = 32
_ALIAS_LOCK = threading.Lock()


def set_alias_provider(fn: Any) -> None:
    """注入 ``persona_id -> profile dict`` 的档案取用函数（``None`` = 禁用派生）。

    默认懒取 ``PersonaManager``；测试可钉死 stub，避免依赖全局单例。
    """
    global _ALIAS_PROVIDER
    with _ALIAS_LOCK:
        _ALIAS_PROVIDER = fn
        _ALIAS_CACHE.clear()


def clear_alias_cache() -> None:
    with _ALIAS_LOCK:
        _ALIAS_CACHE.clear()


def _default_alias_profile(pid: str) -> Optional[Dict[str, Any]]:
    """默认档案源：PersonaManager 进程内单例（未就绪/查无 → None，静默降级）。"""
    try:
        from src.utils.persona_manager import PersonaManager
        return PersonaManager.get_instance().get_persona_by_id(pid)
    except Exception:
        return None


def _alias_profile_sig(profile: Mapping[str, Any]) -> str:
    """只对 ``build_entity_aliases`` 真正读的字段取内容 sha1。

    取内容而非长度：同长度改写（换个同字数的名字）在长度签名下静默失效——
    与 ``_CFG_FILE_CACHE`` 放弃 (mtime,size) 是同一个教训。8KB 级 sha1 约
    10µs，相对整条检索可忽略。
    """
    ctx = profile.get("context")
    ctx = ctx if isinstance(ctx, Mapping) else {}
    parts = [
        str(profile.get("background") or ""),
        str(profile.get("gender") or ""),
        repr(ctx.get("family")),
        repr(ctx.get("specific_memories")),
    ]
    return hashlib.sha1("\x00".join(parts).encode("utf-8", "ignore")).hexdigest()


def persona_query_aliases(pid: str) -> Dict[str, tuple]:
    """该人设生效的别名表＝全局静态表 ⊕ 档案派生实体名。

    任何异常一律回落全局表——检索少一跳只是少注入，抛异常会打断整条聊天链。
    """
    pid = str(pid or "").strip()
    if not pid:
        return _QUERY_ALIASES
    try:
        with _ALIAS_LOCK:
            provider = _ALIAS_PROVIDER
        if provider is None:
            return _QUERY_ALIASES
        fetch = _default_alias_profile if provider is _UNSET else provider
        profile = fetch(pid)
        if not isinstance(profile, Mapping) or not profile:
            return _QUERY_ALIASES
        sig = _alias_profile_sig(profile)
        with _ALIAS_LOCK:
            cached = _ALIAS_CACHE.get(pid)
            if cached is not None and cached[0] == sig:
                _ALIAS_CACHE.move_to_end(pid)
                return cached[1]
        from src.companion.persona_entity_alias import (
            build_entity_aliases, merge_query_aliases,
        )
        derived = build_entity_aliases(profile)
        merged = (merge_query_aliases(_QUERY_ALIASES, derived)
                  if derived else _QUERY_ALIASES)
        with _ALIAS_LOCK:
            _ALIAS_CACHE[pid] = (sig, merged)
            _ALIAS_CACHE.move_to_end(pid)
            while len(_ALIAS_CACHE) > _ALIAS_CACHE_MAX:
                _ALIAS_CACHE.popitem(last=False)
        return merged
    except Exception:
        logger.debug("[persona_bio] 实体别名派生失败，回落全局表", exc_info=True)
        return _QUERY_ALIASES


def _extract_doc_aliases_json(text: str) -> str:
    """入库期：传记正文 → 文档派生别名 JSON（M9 文档轨）。

    profile 轨救不了「档案里没写、名字只在 33k 文档里」的人设（Mizuki 连
    profile 都没有）——共现票选的提取逻辑在 ``persona_entity_alias.
    build_doc_entity_aliases``，这里只管序列化与软失败（提取挂了照常入库，
    只是少一跳别名）。
    """
    try:
        from src.companion.persona_entity_alias import build_doc_entity_aliases
        derived = build_doc_entity_aliases(text)
        return json.dumps(
            {k: list(v) for k, v in derived.items()},
            ensure_ascii=False, sort_keys=True) if derived else "{}"
    except Exception:
        logger.debug("[persona_bio] 文档别名派生失败", exc_info=True)
        return "{}"


_CFG_FILES = ("config/config.yaml", "config/config.local.yaml")
# 配置文件解析缓存：`_retrieval_settings` 每次检索都调，而检索跑在**每条入站
# 消息**上——原实现等于每条消息 yaml.safe_load 两个千行配置（实测 ~200ms，且
# build_bio_block 再来一次）。
# 签名取**文件内容 sha1** 而不是 (mtime, size)：Windows 的文件时间戳只按
# ~15.6ms 步进，同尺寸的改动（`top_k: 5` → `top_k: 7`）连 mtime 带 size 都
# 一模一样，热更新会静默失灵（persona_manager 的 global_rules 踩过 mtime 那半边）。
# 读+哈希 ~130KB 约 0.3ms，相对被省掉的 yaml 解析可以忽略，且语义精确。
_CFG_FILE_CACHE: Dict[str, Any] = {"sig": None, "data": {}}


def _cfg_files_signature() -> tuple:
    sig = []
    for path in _CFG_FILES:
        try:
            with open(path, "rb") as f:
                sig.append((path, hashlib.sha1(f.read()).hexdigest()))
        except OSError:
            sig.append((path, ""))
    return tuple(sig)


def _load_bio_retrieval_cfg_from_files() -> Dict[str, Any]:
    sig = _cfg_files_signature()
    with _CFG_LOCK:
        if _CFG_FILE_CACHE["sig"] == sig:
            return dict(_CFG_FILE_CACHE["data"])
    out: Dict[str, Any] = {}
    try:
        import yaml  # 局部导入：缺依赖时仍可纯关键词运行
    except Exception:
        return out
    for path in _CFG_FILES:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            br = ((data.get("personas") or {}).get("bio_retrieval") or {})
            if isinstance(br, dict):
                out.update(br)
        except Exception:
            continue
    with _CFG_LOCK:
        _CFG_FILE_CACHE["sig"] = sig
        _CFG_FILE_CACHE["data"] = dict(out)
    return out


def clear_retrieval_cfg_file_cache() -> None:
    """丢弃配置文件解析缓存（测试/运维手动改盘后强制重读）。"""
    with _CFG_LOCK:
        _CFG_FILE_CACHE["sig"] = None
        _CFG_FILE_CACHE["data"] = {}


def default_retrieval_settings() -> Dict[str, Any]:
    """代码内置默认档（不读 config、不读覆写）。

    评测（``src/eval/persona_bio_eval.py``）要在**任意机器上复现同一个数字**，
    不能被本机 config.local overlay 带偏；抄一份常量副本又必然漂移，故这里做
    单一事实源：``_retrieval_settings`` 与评测都从它起步。
    """
    return {
        "semantic": _DEFAULT_SEMANTIC,
        "keyword_weight": _DEFAULT_KW_WEIGHT,
        "semantic_weight": _DEFAULT_SEM_WEIGHT,
        "sem_floor": _DEFAULT_SEM_FLOOR,
        "sem_floor_kw0": _DEFAULT_SEM_FLOOR_KW0,
        "sem_min_chars": _SEM_MIN_CHARS,
        "chunk_mode": "unit",
        "unit_target_chars": UNIT_TARGET_CHARS,
        "unit_min_chars": UNIT_MIN_CHARS,
        "unit_max_chars": UNIT_MAX_CHARS,
        "unit_overlap_chars": UNIT_OVERLAP_CHARS,
        "top_k": DEFAULT_TOP_K,
        "sentence_semantic": _DEFAULT_SENTENCE_SEMANTIC,
        "neighbor_expand": _DEFAULT_NEIGHBOR_EXPAND,
        "sem_veto": _DEFAULT_SEM_VETO,
        "doc_aliases": _DEFAULT_DOC_ALIASES,
    }


def _retrieval_settings() -> Dict[str, Any]:
    """读混合检索配置（覆写 > yaml 合并 > 默认）。"""
    base = default_retrieval_settings()
    file_cfg = _load_bio_retrieval_cfg_from_files()
    if file_cfg:
        base.update(file_cfg)
    with _CFG_LOCK:
        ov = _RETRIEVAL_CFG_OVERRIDE
    if ov:
        base.update(ov)
    w_kw, w_sem = _normalize_weights(
        base.get("keyword_weight", _DEFAULT_KW_WEIGHT),
        base.get("semantic_weight", _DEFAULT_SEM_WEIGHT),
    )
    try:
        sem_floor = float(base.get("sem_floor", _DEFAULT_SEM_FLOOR))
    except (TypeError, ValueError):
        sem_floor = _DEFAULT_SEM_FLOOR
    try:
        sem_floor_kw0 = float(base.get("sem_floor_kw0", _DEFAULT_SEM_FLOOR_KW0))
    except (TypeError, ValueError):
        sem_floor_kw0 = _DEFAULT_SEM_FLOOR_KW0

    def _int(name: str, fallback: int) -> int:
        try:
            return int(base.get(name, fallback))
        except (TypeError, ValueError):
            return fallback

    def _float(name: str, fallback: float) -> float:
        try:
            return float(base.get(name, fallback))
        except (TypeError, ValueError):
            return fallback

    mode = str(base.get("chunk_mode") or "unit").strip().lower()
    return {
        "semantic": bool(base.get("semantic", _DEFAULT_SEMANTIC)),
        "keyword_weight": w_kw,
        "semantic_weight": w_sem,
        "sem_floor": sem_floor,
        "sem_floor_kw0": sem_floor_kw0,
        "sem_min_chars": max(0, _int("sem_min_chars", _SEM_MIN_CHARS)),
        "chunk_mode": mode if mode in ("unit", "legacy") else "unit",
        "unit_target_chars": _int("unit_target_chars", UNIT_TARGET_CHARS),
        "unit_min_chars": _int("unit_min_chars", UNIT_MIN_CHARS),
        "unit_max_chars": _int("unit_max_chars", UNIT_MAX_CHARS),
        "unit_overlap_chars": _int("unit_overlap_chars", UNIT_OVERLAP_CHARS),
        "top_k": max(1, min(10, _int("top_k", DEFAULT_TOP_K))),
        "sentence_semantic": bool(
            base.get("sentence_semantic", _DEFAULT_SENTENCE_SEMANTIC)),
        "neighbor_expand": bool(
            base.get("neighbor_expand", _DEFAULT_NEIGHBOR_EXPAND)),
        "sem_veto": max(0.0, min(1.0, _float("sem_veto", _DEFAULT_SEM_VETO))),
        "doc_aliases": bool(base.get("doc_aliases", _DEFAULT_DOC_ALIASES)),
    }


def _lazy_build_embed_fn() -> Optional[EmbedFn]:
    """生产默认：``build_embed_fn``；失败 → None（软降级关键词）。测试勿触发网络。"""
    global _LAZY_EMBED_CACHE
    with _CFG_LOCK:
        if _LAZY_EMBED_CACHE is not _UNSET:
            return _LAZY_EMBED_CACHE
    fn: Optional[EmbedFn] = None
    try:
        from src.eval.embedding_providers import build_embed_fn
        fn = build_embed_fn()
    except Exception:
        logger.debug("[persona_bio] build_embed_fn 失败，降级关键词", exc_info=True)
        fn = None
    with _CFG_LOCK:
        _LAZY_EMBED_CACHE = fn
    return fn


def _resolve_embed_fn(instance_fn: Any = _UNSET) -> Optional[EmbedFn]:
    """实例构造参数 > 模块 set_embed_fn > 懒取 build_embed_fn。"""
    if instance_fn is not _UNSET:
        return instance_fn  # 含显式 None
    with _CFG_LOCK:
        mod = _EMBED_FN
    if mod is not _UNSET:
        return mod
    return _lazy_build_embed_fn()


# ── 存储 ─────────────────────────────────────────────────────────────────────

class PersonaBioStore:
    """人设长传记分块库（线程安全 SQLite）。"""

    def __init__(
        self,
        db_path: Any = ":memory:",
        embed_fn: Any = _UNSET,
    ) -> None:
        self._is_mem = str(db_path) == ":memory:"
        if not self._is_mem:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._embed_fn = embed_fn  # _UNSET / None / callable
        # 解析后的单元视图缓存（见 `_chunk_view`）；任何写入路径必须失效
        # persona_id → (meta.updated_ts, 单元视图)；ts 用于跨进程校验（见 _chunk_view）
        self._chunk_cache: "OrderedDict[str, tuple]" = OrderedDict()
        # persona_id → (meta.updated_ts, 文档派生别名表)；同一套 ts 校验（M9）
        self._doc_alias_cache: "OrderedDict[str, tuple]" = OrderedDict()
        with self._lock:
            if not self._is_mem:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._ensure_embedding_column()
            self._conn.commit()

    def _ensure_embedding_column(self) -> None:
        """幂等 ALTER：旧库补 ``embedding TEXT``（可空；空=未嵌入）。"""
        try:
            cols = [
                r[1] for r in self._conn.execute(
                    "PRAGMA table_info(persona_bio_chunks)"
                ).fetchall()
            ]
            if "embedding" not in cols:
                self._conn.execute(
                    "ALTER TABLE persona_bio_chunks ADD COLUMN embedding TEXT"
                )
        except Exception:
            logger.debug("[persona_bio] ALTER embedding 列失败", exc_info=True)

    def set_embed_fn(self, fn: Optional[EmbedFn]) -> None:
        """实例级注入嵌入函数（覆盖模块级 / 懒取）。"""
        self._embed_fn = fn
        clear_query_embedding_cache()

    def _embed_query(self, text: str) -> Optional[List[float]]:
        """查询向量（模块级 LRU + 失败冷却）——热路每条入站消息都会走这里。

        入库嵌入（``_embed_text``）刻意不共用这条链：批量入库偶发失败不该
        污染聊天热路的冷却窗，反之亦然。
        """
        fn = _resolve_embed_fn(self._embed_fn)
        if not fn:
            return None
        key = _query_cache_key(fn, text)
        with _EMB_LOCK:
            hit = _QUERY_EMB_CACHE.get(key)
            if hit is not None:
                _QUERY_EMB_CACHE.move_to_end(key)
                return list(hit)
            if time.time() < _EMBED_FAIL_UNTIL:
                return None          # 端点抖动冷却窗内不再打网络，直接走纯关键词
        try:
            raw = fn(str(text or ""))
            vec = [float(x) for x in raw] if raw else None
        except Exception:
            logger.debug("[persona_bio] 查询嵌入异常", exc_info=True)
            vec = None
        if not vec:
            _note_embed_failure()
            return None
        with _EMB_LOCK:
            _QUERY_EMB_CACHE[key] = list(vec)
            _QUERY_EMB_CACHE.move_to_end(key)
            while len(_QUERY_EMB_CACHE) > _QUERY_EMB_CACHE_MAX:
                _QUERY_EMB_CACHE.popitem(last=False)
        return vec

    def _embed_text(self, text: str) -> Optional[List[float]]:
        fn = _resolve_embed_fn(self._embed_fn)
        if not fn:
            return None
        try:
            vec = fn(str(text or ""))
        except Exception:
            logger.debug("[persona_bio] embed_fn 异常", exc_info=True)
            return None
        if not vec:
            return None
        try:
            return [float(x) for x in vec]
        except Exception:
            return None

    def _build_sentence_rows(self, pid: str, chunks: Any) -> List[tuple]:
        """入库期预算句向量 → ``[(pid, sent_key, embedding_json)]``。

        句级重排的语义轨若在查询时现算，top_k×每单元句数 ≈ 每条消息十余次嵌入
        往返（聊天热路不可接受）。入库是一次性后台作业，把这笔账挪到这里付：
        查询侧退化成对缓存向量做余弦，零网络。任一句失败只是少一条，不阻断入库。
        """
        if _resolve_embed_fn(self._embed_fn) is None:
            return []
        rows: List[tuple] = []
        for sent in _embeddable_sentences(chunks):
            emb_s = _dumps_embedding(self._embed_text(sent))
            if emb_s:
                rows.append((pid, _sent_key(sent), emb_s))
        return rows

    def _fetch_sentence_vectors(
        self, persona_id: str, texts: Any,
    ) -> Dict[str, List[float]]:
        """取给定单元文本涉及的句向量（只捞命中单元的句，不整表拉）。"""
        keys = [_sent_key(s) for s in _embeddable_sentences(texts)]
        if not keys:
            return {}
        out: Dict[str, List[float]] = {}
        with self._lock:
            for i in range(0, len(keys), 400):     # SQLite 变量数上限保护
                part = keys[i:i + 400]
                qs = ",".join("?" * len(part))
                for r in self._conn.execute(
                    "SELECT sent_key, embedding FROM persona_bio_sents"
                    f" WHERE persona_id = ? AND sent_key IN ({qs})",
                    (persona_id, *part),
                ).fetchall():
                    vec = _loads_embedding(r["embedding"])
                    if vec is not None:
                        out[str(r["sent_key"])] = vec
        return out

    def replace_bio_doc(self, persona_id: str, text: str) -> Optional[Dict[str, Any]]:
        """全文分块入库（整体替换该人设旧块）。返回 ``{"chunks", "chars"}``。

        若有 embed_fn：每块预计算 embedding（失败该块留空仍入库文本）。
        """
        pid = str(persona_id or "").strip()
        clean = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not pid or not clean:
            return None
        if len(clean) > MAX_BIO_CHARS:
            clean = clean[:MAX_BIO_CHARS]
        chunks = split_bio_chunks(clean)
        if not chunks:
            return None
        rows = []
        for i, c in enumerate(chunks):
            emb_s = _dumps_embedding(self._embed_text(c))
            rows.append((pid, i, c, emb_s))
        sent_rows = self._build_sentence_rows(pid, chunks)
        alias_json = _extract_doc_aliases_json(clean)
        now = time.time()
        with self._lock:
            self._conn.execute(
                "DELETE FROM persona_bio_chunks WHERE persona_id = ?", (pid,))
            self._conn.execute(
                "DELETE FROM persona_bio_sents WHERE persona_id = ?", (pid,))
            self._conn.executemany(
                "INSERT INTO persona_bio_chunks (persona_id, idx, text, embedding)"
                " VALUES (?,?,?,?)",
                rows)
            if sent_rows:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO persona_bio_sents"
                    " (persona_id, sent_key, embedding) VALUES (?,?,?)",
                    sent_rows)
            self._conn.execute(
                "INSERT OR REPLACE INTO persona_bio_aliases (persona_id, aliases)"
                " VALUES (?,?)", (pid, alias_json))
            self._conn.execute(
                "INSERT INTO persona_bio_meta (persona_id, chars, chunks, updated_ts)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(persona_id) DO UPDATE SET chars=excluded.chars,"
                " chunks=excluded.chunks, updated_ts=excluded.updated_ts",
                (pid, len(clean), len(chunks), now))
            self._conn.commit()
        self._invalidate_chunk_view(pid)
        return {"chunks": len(chunks), "chars": len(clean)}

    def reembed_bio_doc(self, persona_id: str) -> Optional[Dict[str, Any]]:
        """给缺向量的块**与句**补嵌（运维/测试）；无 embed_fn 或无人设 → None。

        句向量表是后加的（2026-07-28），老库整表为空 → 句级语义轨静默失效。
        这里顺手回填，免得非要重新导一遍文档。
        """
        pid = str(persona_id or "").strip()
        if not pid:
            return None
        fn = _resolve_embed_fn(self._embed_fn)
        if not fn:
            return None
        with self._lock:
            rows = self._conn.execute(
                "SELECT idx, text, embedding FROM persona_bio_chunks"
                " WHERE persona_id = ? ORDER BY idx", (pid,),
            ).fetchall()
        if not rows:
            return None
        updated = 0
        for r in rows:
            if _loads_embedding(r["embedding"]) is not None:
                continue
            vec = self._embed_text(str(r["text"] or ""))
            emb_s = _dumps_embedding(vec)
            if emb_s is None:
                continue
            with self._lock:
                self._conn.execute(
                    "UPDATE persona_bio_chunks SET embedding = ?"
                    " WHERE persona_id = ? AND idx = ?",
                    (emb_s, pid, int(r["idx"])))
                self._conn.commit()
            updated += 1
        sents_added = self._backfill_sentence_vectors(
            pid, [str(r["text"] or "") for r in rows])
        aliases_added = self._backfill_doc_aliases(
            pid, [str(r["text"] or "") for r in rows])
        if updated or sents_added or aliases_added:
            # 升 meta.updated_ts：视图/别名缓存按 ts 跨进程校验（M6），不升的话
            # 「A 实例点补齐向量，B 实例的缓存视图到重启都不知道」——回填路径
            # 曾漏了这一步，属 M6 同款静默失效。
            with self._lock:
                self._conn.execute(
                    "UPDATE persona_bio_meta SET updated_ts = ?"
                    " WHERE persona_id = ?", (time.time(), pid))
                self._conn.commit()
            self._invalidate_chunk_view(pid)
        return {"chunks": len(rows), "updated": updated,
                "sents_added": sents_added, "aliases_added": aliases_added}

    def _backfill_doc_aliases(self, pid: str, texts: Any) -> int:
        """老库存补文档别名（别名表是 M9 后加的）。已有非空行不重算。

        没有原文档，就从已入库的块拼回近似全文——块首的 overlap 前缀会造成
        行级重复，但投票按（名字×角色）聚合，重复行只是重复投同一票，无害。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT aliases FROM persona_bio_aliases WHERE persona_id = ?",
                (pid,)).fetchone()
        if row is not None and str(row["aliases"] or "").strip() not in ("", "{}"):
            return 0
        alias_json = _extract_doc_aliases_json("\n".join(texts))
        if alias_json == "{}" and row is not None:
            return 0
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO persona_bio_aliases (persona_id, aliases)"
                " VALUES (?,?)", (pid, alias_json))
            self._conn.commit()
        return 1 if alias_json != "{}" else 0

    def _backfill_sentence_vectors(self, pid: str, texts: Any) -> int:
        """补齐句向量表里缺的句（已有的不重算）。"""
        have = set(self._fetch_sentence_vectors(pid, texts).keys())
        rows: List[tuple] = []
        for sent in _embeddable_sentences(texts):
            key = _sent_key(sent)
            if key in have:
                continue
            emb_s = _dumps_embedding(self._embed_text(sent))
            if emb_s:
                rows.append((pid, key, emb_s))
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO persona_bio_sents"
                " (persona_id, sent_key, embedding) VALUES (?,?,?)", rows)
            self._conn.commit()
        return len(rows)

    def get_bio_meta(self, persona_id: str) -> Optional[Dict[str, Any]]:
        """库存元数据；无库存 None。

        除 ``{"chunks","chars","updated_ts","preview"}`` 外带三个覆盖度计数
        （M9 收尾 2026-07-28）：``emb_chunks``（有块向量的单元数——低于 chunks
        说明该跑 reembed）、``sents``（句向量条数——0 说明句级语义轨没在工作）、
        ``alias_keys``（文档派生别名键数）。此前这三样只能裸 SQL 才看得到，
        「库存看着在、向量/别名其实缺」在运维面完全不可见。三个都是单行
        COUNT/SELECT，走索引，meta 路由每次调也无感。
        """
        pid = str(persona_id or "").strip()
        if not pid:
            return None
        with self._lock:
            m = self._conn.execute(
                "SELECT chars, chunks, updated_ts FROM persona_bio_meta"
                " WHERE persona_id = ?", (pid,)).fetchone()
            first = self._conn.execute(
                "SELECT text FROM persona_bio_chunks WHERE persona_id = ?"
                " ORDER BY idx LIMIT 1", (pid,)).fetchone()
            n_emb = self._conn.execute(
                "SELECT COUNT(*) FROM persona_bio_chunks WHERE persona_id = ?"
                " AND embedding IS NOT NULL AND embedding != ''",
                (pid,)).fetchone()[0]
            n_sents = self._conn.execute(
                "SELECT COUNT(*) FROM persona_bio_sents WHERE persona_id = ?",
                (pid,)).fetchone()[0]
        if not m:
            return None
        preview = str(first["text"] if first else "")[:PREVIEW_CHARS]
        return {
            "chunks": int(m["chunks"] or 0),
            "chars": int(m["chars"] or 0),
            "updated_ts": float(m["updated_ts"] or 0.0),
            "preview": preview,
            "emb_chunks": int(n_emb or 0),
            "sents": int(n_sents or 0),
            "alias_keys": len(self._doc_aliases(pid)),
        }

    def delete_bio_doc(self, persona_id: str) -> bool:
        """删除该人设全部分块+元数据；返回是否真删了东西。"""
        pid = str(persona_id or "").strip()
        if not pid:
            return False
        with self._lock:
            c1 = self._conn.execute(
                "DELETE FROM persona_bio_chunks WHERE persona_id = ?", (pid,))
            c2 = self._conn.execute(
                "DELETE FROM persona_bio_meta WHERE persona_id = ?", (pid,))
            self._conn.execute(
                "DELETE FROM persona_bio_sents WHERE persona_id = ?", (pid,))
            self._conn.execute(
                "DELETE FROM persona_bio_aliases WHERE persona_id = ?", (pid,))
            self._conn.commit()
        self._invalidate_chunk_view(pid)
        return bool((c1.rowcount or 0) > 0 or (c2.rowcount or 0) > 0)

    def list_bio_personas(self) -> List[str]:
        """有传记库存的人设 id（运维批量补向量用）。"""
        with self._lock:
            return [str(r["persona_id"]) for r in self._conn.execute(
                "SELECT persona_id FROM persona_bio_meta ORDER BY persona_id"
            ).fetchall()]

    def _fetch_chunks(self, persona_id: str) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT idx, text, embedding FROM persona_bio_chunks"
                " WHERE persona_id = ? ORDER BY idx", (persona_id,),
            ).fetchall()

    def _chunk_view(self, persona_id: str) -> List[Dict[str, Any]]:
        """解析好的单元视图，进程内按人设缓存（写入时失效）。

        检索跑在**每条入站消息**上，而每次都重做的三件事全是纯派生量：
        1024 维向量的 ``json.loads``、``text.lower()``、``_cjk_ratio``。
        真实 183 单元的库上仅 JSON 解析就 ~116ms/次。向量存 ``array('f')``
        （4 字节/维，约为 Python float list 的 1/8），整库常驻不到 1MB。

        缓存**按 meta.updated_ts 校验**，不只靠同进程写入失效：本机是双实例部署
        （智聊/通译共用 `config/`），且回填走独立 CLI 进程——纯进程内失效会让
        「A 实例导入传记、B 实例永远查不到」，以及「导入前查过一次的人设把空视图
        缓存到重启」。多一次单行索引查询（≈0.05ms）换掉这两类静默失效。
        """
        ts = self._fetch_meta_ts(persona_id)
        with self._lock:
            cached = self._chunk_cache.get(persona_id)
            if cached is not None and cached[0] == ts:
                self._chunk_cache.move_to_end(persona_id)
                return cached[1]
        view: List[Dict[str, Any]] = []
        for r in self._fetch_chunks(persona_id):
            text = str(r["text"] or "")
            vec = _loads_embedding(r["embedding"])
            view.append({
                "idx": int(r["idx"]),
                "text": text,
                "low": text.lower(),
                "emb": array("f", vec) if vec else None,
                "cjk": _cjk_ratio(text),
                "len": len(text.strip()),
            })
        with self._lock:
            self._chunk_cache[persona_id] = (ts, view)
            self._chunk_cache.move_to_end(persona_id)
            while len(self._chunk_cache) > _CHUNK_CACHE_MAX_PERSONAS:
                self._chunk_cache.popitem(last=False)
        return view

    def _fetch_meta_ts(self, persona_id: str) -> float:
        """该人设传记的 ``updated_ts``（无库存/异常→0.0，当作「空视图」的版本号）。"""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT updated_ts FROM persona_bio_meta WHERE persona_id = ?",
                    (persona_id,),
                ).fetchone()
            return float(row["updated_ts"]) if row else 0.0
        except Exception:
            return 0.0

    def _invalidate_chunk_view(self, persona_id: Optional[str] = None) -> None:
        with self._lock:
            if persona_id is None:
                self._chunk_cache.clear()
                self._doc_alias_cache.clear()
            else:
                self._chunk_cache.pop(persona_id, None)
                self._doc_alias_cache.pop(persona_id, None)

    def _doc_aliases(self, persona_id: str) -> Dict[str, tuple]:
        """该人设入库时派生的文档别名表（无库存/异常 → ``{}``）。

        与 ``_chunk_view`` 同一套 ``meta.updated_ts`` 校验：双实例部署下
        A 实例重新导入文档，B 实例下一条消息就能拿到新别名。
        """
        ts = self._fetch_meta_ts(persona_id)
        with self._lock:
            cached = self._doc_alias_cache.get(persona_id)
            if cached is not None and cached[0] == ts:
                self._doc_alias_cache.move_to_end(persona_id)
                return cached[1]
        parsed: Dict[str, tuple] = {}
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT aliases FROM persona_bio_aliases WHERE persona_id = ?",
                    (persona_id,)).fetchone()
            if row is not None:
                data = json.loads(str(row["aliases"] or "{}"))
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, (list, tuple)) and v:
                            parsed[str(k)] = tuple(str(x) for x in v)
        except Exception:
            logger.debug("[persona_bio] 文档别名读取失败", exc_info=True)
            parsed = {}
        with self._lock:
            self._doc_alias_cache[persona_id] = (ts, parsed)
            self._doc_alias_cache.move_to_end(persona_id)
            while len(self._doc_alias_cache) > _CHUNK_CACHE_MAX_PERSONAS:
                self._doc_alias_cache.popitem(last=False)
        return parsed

    def _effective_aliases(
        self, persona_id: str, settings: Dict[str, Any],
    ) -> Mapping[str, Sequence[str]]:
        """本次查询生效的别名表 = 全局静态 ⊕ profile 派生 ⊕ 文档派生（M9）。"""
        base: Mapping[str, Sequence[str]] = persona_query_aliases(persona_id)
        if not settings.get("doc_aliases", True):
            return base
        doc = self._doc_aliases(persona_id)
        if not doc:
            return base
        try:
            from src.companion.persona_entity_alias import merge_query_aliases
            return merge_query_aliases(base, doc)
        except Exception:
            logger.debug("[persona_bio] 文档别名合并失败，回落", exc_info=True)
            return base

    def search_bio(
        self, persona_id: str, query: str, top_k: int = 2, min_hits: int = 2,
    ) -> List[Dict[str, Any]]:
        """混合检索：返回 ``[{"idx","text","score", ...}]`` 按相关度降序。

        - 无 embed / 全无向量 / ``semantic:false`` → **纯关键词旧行为**
          （``score`` = keyword 命中数）。
        - 有向量时：``final = w_kw * kw_norm + w_sem * sem``；
          ``score = int(round(final*100))``（兼容字段改语义），另附 ``kw``/``sem``/``rank``。
        准入：``(kw >= eff_min) OR (sem >= sem_floor)``。

        每次调用累计注入观测（``retrieval_stats_snapshot``）。
        """
        pid = str(persona_id or "").strip()
        if not pid:
            return []
        _bump("queries")
        out = self._search_bio_inner(pid, query, top_k, min_hits)
        if out:
            _bump("hits")
            _bump("hits_total", len(out))
        else:
            _bump("empty")
        return out

    def _search_bio_inner(
        self, pid: str, query: str, top_k: int, min_hits: int,
    ) -> List[Dict[str, Any]]:
        raw_toks = query_tokens(query)
        if _is_chitchat_query(raw_toks):
            return []
        settings = _retrieval_settings()
        # 打分用：去掉问句壳（名字/什么…）；别名仍基于原始 query 扩展
        score_raw = {t for t in raw_toks if t not in _QUESTION_SHELL_TOKS}
        toks = (expand_query_tokens(query, self._effective_aliases(pid, settings))
                - _QUESTION_SHELL_TOKS)
        if not score_raw and not toks:
            return []
        rows = self._chunk_view(pid)
        if not rows:
            return []

        semantic_on = bool(settings.get("semantic", True))
        query_emb: Optional[List[float]] = None
        if semantic_on:
            query_emb = self._embed_query(str(query or ""))

        use_hybrid = bool(
            semantic_on
            and query_emb is not None
            and any(r["emb"] is not None for r in rows)
        )
        query_is_cjk = any(
            isinstance(t, str) and any("\u4e00" <= ch <= "\u9fff" for ch in t)
            for t in raw_toks
        )

        if not use_hybrid:
            # ── 纯关键词路径（含别名扩展；score = 加权命中数）──────────────
            if not toks:
                return []
            # 有别名扩展时门槛降为 1（否则「前夫」只靠「离婚」别名时
            # eff_min=2 会把叙事块挡在门外——真机冒烟实锤）
            pure_alias = bool(toks - score_raw)
            base_n = len(score_raw) or len(toks)
            eff_min = 1 if pure_alias else max(1, min(int(min_hits), base_n))
            scored: List[Dict[str, Any]] = []
            for r in rows:
                sc = _keyword_score_weighted(
                    score_raw, toks, r["text"], low=r["low"])
                if sc >= eff_min:
                    scored.append({
                        "idx": r["idx"],
                        "text": r["text"],
                        "score": sc,
                    })
            scored.sort(key=lambda h: (-h["score"], h["idx"]))
            return scored[: max(1, int(top_k))]

        # ── 混合路径 ──────────────────────────────────────────────────────
        w_kw = float(settings["keyword_weight"])
        w_sem = float(settings["semantic_weight"])
        sem_floor = float(settings["sem_floor"])
        sem_floor_kw0 = float(settings["sem_floor_kw0"])
        sem_min_chars = int(settings["sem_min_chars"])
        pure_alias = bool(toks - score_raw)
        base_n = len(score_raw) or len(toks)
        if not toks:
            eff_min = int(min_hits)
        elif pure_alias:
            eff_min = 1
        else:
            eff_min = max(1, min(int(min_hits), base_n))
        sem_veto = float(settings["sem_veto"])

        kw_scores: List[int] = []
        sem_scores: List[float] = []
        for r in rows:
            kw_scores.append(
                _keyword_score_weighted(score_raw, toks, r["text"], low=r["low"])
                if toks else 0
            )
            emb = r["emb"]
            sem = cosine_similarity(query_emb, emb) if emb is not None else 0.0
            # 中文查询 × 英文碎片块：软降权（真机英文 marriage 碎片误中）
            if query_is_cjk and r["cjk"] < 0.12:
                sem *= 0.85
            sem_scores.append(sem)
        max_kw = max(kw_scores) if kw_scores else 0
        # 嵌入轨对本条查询整体失灵时不给否决权（见 _SEM_VETO_MIN_SIGNAL）
        veto_on = (
            sem_veto > 0
            and bool(sem_scores)
            and max(sem_scores) >= _SEM_VETO_MIN_SIGNAL
        )

        scored = []
        for r, kw, sem in zip(rows, kw_scores, sem_scores):
            floor = sem_floor if kw > 0 else sem_floor_kw0
            # kw==0 的纯语义准入另加长度门槛：短碎片向量方向不稳（见 _SEM_MIN_CHARS）
            long_enough = kw > 0 or r["len"] >= sem_min_chars
            kw_ok = bool(toks) and kw >= eff_min
            if kw_ok and veto_on and sem < sem_veto and r["emb"] is not None:
                kw_ok = False   # 语义明确反对 → 词面巧合不足以注入
            admit = kw_ok or (sem >= floor and long_enough)
            if not admit:
                continue
            kw_norm = (kw / max_kw) if max_kw > 0 else 0.0
            final = w_kw * kw_norm + w_sem * float(sem)
            scored.append({
                "idx": r["idx"],
                "text": r["text"],
                # score：有向量时 = final*100 的 int；观测另带 kw/sem/rank
                "score": int(round(final * 100)),
                "kw": int(kw),
                "sem": float(sem),
                "rank": float(final),
            })
        scored.sort(key=lambda h: (-h["rank"], h["idx"]))
        return scored[: max(1, int(top_k))]

    def _sentence_pools(
        self, persona_id: str, hits: Sequence[Dict[str, Any]],
        settings: Dict[str, Any],
    ) -> List[List[str]]:
        """每个命中的**句候选池**＝单元文本列表（与 hits 等长同序）。

        默认＝只有命中单元自己。开 ``neighbor_expand`` 后追加同段邻居，让「答案
        正好落在隔壁单元」的一类有机会被句级重排捞出来。**刻意返回列表而不是拼好的
        整段**——句向量按单元内分句入库，拼接会让跨界句查不到向量（见
        ``pick_span_sentences``）。取不到单元视图（老库/异常）退回单元原文，
        绝不阻断注入。
        """
        raws = [[str(h.get("text") or "")] for h in hits]
        if not settings.get("neighbor_expand") or not raws:
            return raws
        try:
            rows = self._chunk_view(persona_id)
            if not rows:
                return raws
            texts = [r["text"] for r in rows]
            at = {int(r["idx"]): i for i, r in enumerate(rows)}
            overlap = int(settings.get("unit_overlap_chars", UNIT_OVERLAP_CHARS))
            out: List[List[str]] = []
            for h, raw in zip(hits, raws):
                pos = at.get(int(h.get("idx", -1)), -1)
                if pos < 0:
                    out.append(raw)
                    continue
                start, end = neighbor_span(texts, pos, overlap)
                out.append(list(texts[start:end]) or raw)
            return out
        except Exception:
            logger.debug("[persona_bio] 邻接扩展失败，退回单元原文", exc_info=True)
            return raws

    def build_bio_block(
        self, persona_id: str, query: str,
        budget_chars: int = DEFAULT_BLOCK_BUDGET,
        hits: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[str]:
        """检索命中 → 组装注入块（总长硬夹 budget）；无命中/无库存 None。

        单元更小之后不再整单元照搬：每个命中单元用 ``pick_unit_sentences``
        取最相关的 1~2 句（保原文序），跨单元**按句去重**（消化单元重叠，
        重复内容不重复占预算）。整单元一句都不得分或仍超预算 → 退回
        ``excerpt_around_hits`` 命中窗摘录兜底。

        ``hits`` 可由调用方预先检索后传入（自测路由用，避免重复计一次观测）。
        """
        settings = _retrieval_settings()
        if hits is None:
            hits = self.search_bio(
                persona_id, query, top_k=int(settings.get("top_k", DEFAULT_TOP_K)))
        if not hits:
            return None
        budget = max(0, int(budget_chars))
        toks = (expand_query_tokens(
                    query, self._effective_aliases(persona_id, settings))
                - _QUESTION_SHELL_TOKS)
        pools = self._sentence_pools(persona_id, hits, settings)

        sim_fn = None
        if settings.get("sentence_semantic"):
            q_emb = self._embed_query(str(query or ""))
            if q_emb is not None:
                # 只读入库期预算好的句向量：缺向量（老库/短句）就退化成纯关键词
                # 排序，**绝不**在聊天热路补打嵌入（那正是这条开关原先的代价）。
                vecs = self._fetch_sentence_vectors(
                    persona_id, [t for pool in pools for t in pool])
                if vecs:
                    def sim_fn(sent: str, _q=q_emb, _v=vecs) -> float:  # noqa: F811
                        vec = _v.get(_sent_key(sent))
                        return cosine_similarity(_q, vec) if vec is not None else 0.0

        parts = [_BLOCK_HEADER]
        used = len(_BLOCK_HEADER)
        emitted: List[str] = []
        added = 0
        for h, pool in zip(hits, pools):
            raw = str(h.get("text") or "")
            if not raw.strip():
                continue
            prefix = "\n- "
            room = budget - used - len(prefix)
            if room < 12:      # 剩余预算连半句都放不下 → 停
                break
            seen = "".join(emitted)
            picked = pick_span_sentences(
                pool, toks, sim_fn=sim_fn,
                max_sents=min(_MAX_SENTS_PER_UNIT * len(pool),
                              _MAX_SENTS_PER_SPAN))
            if picked:
                text = ""
                for sent in picked:
                    s = sent.strip()
                    if s and s not in seen and s not in text:
                        text += s
                if not text:
                    continue      # 选中的句子已由前面单元注入（重叠/重复）→ 整单元跳过
                if len(text) > room:
                    text = excerpt_around_hits(text, toks, room)
            else:
                # 一句都没得分（纯语义准入的单元）→ 命中窗摘录兜底
                text = excerpt_around_hits(raw, toks, room)
            if not text or text in seen:
                continue
            parts.append(prefix + text)
            used += len(prefix) + len(text)
            emitted.append(text)
            added += 1
        if not added:
            return None
        block = "".join(parts)
        return block[:budget] if len(block) > budget else block


# ── 模块级单例（懒建；main.py 可用 configure_* 显式指定库路径）───────────────

_STORE: Optional[PersonaBioStore] = None
_DB_PATH: str = DEFAULT_DB_PATH


def _resolved_db_path(db_path: Any) -> str:
    from src.licensing.data_paths import resolve_legacy_config_path
    return resolve_legacy_config_path(db_path)


def configure_persona_bio_store(
    db_path: Any = DEFAULT_DB_PATH,
    embed_fn: Any = _UNSET,
) -> Optional[PersonaBioStore]:
    """启动期/测试装配（幂等）：指定库路径并建库；可选注入 embed_fn。"""
    global _STORE, _DB_PATH
    with _CFG_LOCK:
        _DB_PATH = _resolved_db_path(db_path)
        try:
            kwargs: Dict[str, Any] = {}
            if embed_fn is not _UNSET:
                kwargs["embed_fn"] = embed_fn
            _STORE = PersonaBioStore(_DB_PATH, **kwargs)
        except Exception:
            logger.warning("[persona_bio] 建库失败", exc_info=True)
            _STORE = None
        return _STORE


def get_persona_bio_store() -> Optional[PersonaBioStore]:
    """取 store 单例（未配置则按默认路径懒建）。建库失败返回 None（调用方需容错）。"""
    global _STORE, _DB_PATH
    if _STORE is None:
        with _CFG_LOCK:
            if _STORE is None:
                _DB_PATH = _resolved_db_path(_DB_PATH)
                try:
                    _STORE = PersonaBioStore(_DB_PATH)
                except Exception:
                    logger.warning("[persona_bio] 懒建库失败", exc_info=True)
                    _STORE = None
    return _STORE


def reset_persona_bio_store() -> None:
    """测试钩子：清空单例 + 嵌入/检索覆写 + 查询向量缓存 + 观测计数（防串测）。"""
    global _STORE, _EMBED_FN, _LAZY_EMBED_CACHE, _RETRIEVAL_CFG_OVERRIDE
    with _CFG_LOCK:
        _STORE = None
        _EMBED_FN = _UNSET
        _LAZY_EMBED_CACHE = _UNSET
        _RETRIEVAL_CFG_OVERRIDE = None
    clear_query_embedding_cache()
    clear_retrieval_cfg_file_cache()
    reset_retrieval_stats()


# ── 模块级软失败 API（路由 + 聊天链的唯一入口，DB 异常绝不外抛）──────────────

def replace_bio_doc(persona_id: str, text: str) -> Optional[Dict[str, Any]]:
    try:
        store = get_persona_bio_store()
        return store.replace_bio_doc(persona_id, text) if store else None
    except Exception:
        logger.warning("[persona_bio] replace_bio_doc 失败", exc_info=True)
        return None


def get_bio_meta(persona_id: str) -> Optional[Dict[str, Any]]:
    try:
        store = get_persona_bio_store()
        return store.get_bio_meta(persona_id) if store else None
    except Exception:
        logger.warning("[persona_bio] get_bio_meta 失败", exc_info=True)
        return None


def delete_bio_doc(persona_id: str) -> bool:
    try:
        store = get_persona_bio_store()
        return store.delete_bio_doc(persona_id) if store else False
    except Exception:
        logger.warning("[persona_bio] delete_bio_doc 失败", exc_info=True)
        return False


def explain_query_aliases(
    persona_id: str, query: str, hit_texts: Any = (),
) -> List[Dict[str, Any]]:
    """自测/调参用：这条 query 触发了哪些别名键，值里哪些真的命中了返回块。

    运营调传记最常问的是「为什么这句没搜到 / 搜到的凭什么」——答案藏在
    别名扩展里（``老公`` 靠文档派生的 ``josé luis`` 才够到婚史段），此前只有
    翻代码或跑脚本才看得到。返回 ``[{"key", "values", "hits"}, …]``（按键排序、
    上限 8 键防噪）；任何异常返回 []（自测面板少一行，不碍检索本身）。
    """
    try:
        store = get_persona_bio_store()
        if store is None:
            return []
        aliases = store._effective_aliases(
            str(persona_id or "").strip(), _retrieval_settings())
        s = str(query or "")
        s_low = s.lower()
        lows = [str(t or "").lower() for t in (hit_texts or ())]
        out: List[Dict[str, Any]] = []
        for tip in sorted(aliases.keys()):
            if tip not in s and tip.lower() not in s_low:
                continue
            vals = [str(v) for v in aliases[tip]]
            out.append({
                "key": tip,
                "values": vals,
                "hits": [v for v in vals
                         if any(v.lower() in L for L in lows)],
            })
            if len(out) >= 8:
                break
        return out
    except Exception:
        logger.debug("[persona_bio] explain_query_aliases 失败", exc_info=True)
        return []


def search_bio(
    persona_id: str, query: str, top_k: int = 2, min_hits: int = 2,
) -> List[Dict[str, Any]]:
    try:
        store = get_persona_bio_store()
        return store.search_bio(persona_id, query, top_k=top_k, min_hits=min_hits) if store else []
    except Exception:
        logger.debug("[persona_bio] search_bio 失败", exc_info=True)
        return []


def build_bio_block(
    persona_id: str, query: str, budget_chars: int = DEFAULT_BLOCK_BUDGET,
    hits: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    try:
        store = get_persona_bio_store()
        return store.build_bio_block(
            persona_id, query, budget_chars=budget_chars, hits=hits) if store else None
    except Exception:
        logger.debug("[persona_bio] build_bio_block 失败", exc_info=True)
        return None


def reembed_bio_doc(persona_id: str) -> Optional[Dict[str, Any]]:
    try:
        store = get_persona_bio_store()
        return store.reembed_bio_doc(persona_id) if store else None
    except Exception:
        logger.warning("[persona_bio] reembed_bio_doc 失败", exc_info=True)
        return None


def list_bio_personas() -> List[str]:
    try:
        store = get_persona_bio_store()
        return store.list_bio_personas() if store else []
    except Exception:
        logger.warning("[persona_bio] list_bio_personas 失败", exc_info=True)
        return []


__all__ = [
    "DEFAULT_DB_PATH", "MAX_BIO_CHARS", "CHUNK_MAX_CHARS", "DEFAULT_BLOCK_BUDGET",
    "UNIT_TARGET_CHARS", "UNIT_MIN_CHARS", "UNIT_MAX_CHARS", "UNIT_OVERLAP_CHARS",
    "DEFAULT_TOP_K",
    "PersonaBioStore", "split_bio_chunks", "query_tokens", "expand_query_tokens",
    "excerpt_around_hits", "script_segments", "split_sentences",
    "rank_sentences", "pick_unit_sentences", "pick_span_sentences",
    "units_are_contiguous", "stitch_units", "neighbor_span",
    "set_embed_fn", "set_retrieval_cfg", "clear_query_embedding_cache",
    "set_alias_provider", "clear_alias_cache", "persona_query_aliases",
    "clear_retrieval_cfg_file_cache", "default_retrieval_settings",
    "retrieval_stats_snapshot", "reset_retrieval_stats",
    "configure_persona_bio_store", "get_persona_bio_store", "reset_persona_bio_store",
    "replace_bio_doc", "get_bio_meta", "delete_bio_doc", "search_bio", "build_bio_block",
    "reembed_bio_doc", "list_bio_personas", "explain_query_aliases",
]
