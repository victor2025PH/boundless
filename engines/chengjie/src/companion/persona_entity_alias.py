# -*- coding: utf-8 -*-
"""按人设动态派生的「口语称谓 → 该人设文档里的实体名」查询别名（长传记检索补丁）。

``persona_bio_store._QUERY_ALIASES`` 是**全局静态**词表，只能修「口语 vs 书面语」这类
放之四海皆准的同义（妈妈→母亲）。它救不了**人设特有的实体名**：真实 33k 文档里丈夫
一律写全名 ``José Luis Jerónimo``，「老公/丈夫/husband」在叙事单元中一次都没出现（只
出现在人设名片行，那是另一个块且被 ``_PROFILE_CARD_RE`` 降权）——客户问「你老公对你
好吗」时关键词分恒 0，而该问句对全库最高语义分只有 0.540、够不到 ``sem_floor_kw0``
（0.62）→ **整体空注入**，AI 只能编。本模块从人设档案里把「老公 → josé luis」这类
别名派生出来，与全局表合并后喂给 ``expand_query_tokens``，让关键词轨先扩一跳。

设计权衡（错的别名比没有别名更糟——它会让检索**稳定地**注入错误段落）：
- **宁可漏不可错**。所有产出都要求「位置信号 + 形态信号」双高置信；任何一层不确定
  就不产出。覆盖率不足由全局表和语义轨兜底，误产出没有兜底。
- **CJK 人名只从结构化 ``context.family`` 取**（角色 key 已经给了位置信号），自由文本
  里**一律不抽 CJK**：真实档案有「父亲因此病倒瘫痪」「妹妹还在读大学」，任何
  「称谓 + 后续汉字」的正则都会抽出 ``父亲→因此病倒``，而百家姓兜底同样会被
  「父亲高血压」骗过。自由文本只抽**拉丁专名**（Titlecase 形态在中文语料里几乎不会
  被非专名占用），这恰好正中 ``José Luis Jerónimo`` 这个真实缺陷。
- **值一律小写**：``expand_query_tokens`` 对**非 ASCII**（``José``）不做 lower，而
  ``_keyword_score_weighted`` 是在 ``text.lower()`` 上做子串匹配——大写开头的重音名
  进了 token 集也永远命不中。这里在产出侧就折好。
- 片段有**最短长度**：拉丁独立片段 ≥4 字符（``lin`` 会命中 ``online``——打分是子串
  匹配不是分词），CJK 片段 ≥2 字（与全局表「单字键只做触发、不进 token」同一教训）。
- 值只放**专名本身**，不引状态词/泛化词（全局表 2026-07-27 踩过：值里放「离婚」会
  命中人设名片的「婚姻状况：离婚」）。
- 每键值数与总键数都有上限，防畸形档案把 token 集撑爆拖慢全库打分。
- 纯函数、无 IO、无全局可变状态、结果确定性（上游按 persona 缓存）。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

MAX_VALUES_PER_KEY = 4        # 单键最多几个实体片段（同一角色可能有多人）
MAX_KEYS = 40                 # 总键数上限（expand_query_tokens 每条消息全表扫一遍）
MIN_LATIN_FRAGMENT = 4        # 拉丁**独立**片段最短长度（打分是子串匹配，短词必误命中）
_MAX_NAME_TOKENS = 4          # 一个人名最多几个词（含 de/van 这类虚词）
_MAX_LATIN_RUNS_PER_HEAD = 2  # 结构化字段首段最多认几个拉丁名
_MAX_FAMILY_ENTRIES = 24      # family 字典最多处理几条
_MAX_HITS_PER_TERM = 4        # 自由文本里同一称谓最多认几次
_MAX_SCAN_CHARS = 8_000       # 自由文本扫描预算
_FREE_TEXT_WINDOW = 14        # 称谓与人名之间允许隔多少字符

# ── 角色 → 查询键 ───────────────────────────────────────────────────────────
# 顺序即产出优先级（键额度用尽时后面的角色出局）。键是「触发子串」，与全局表同构；
# 单字键（爸/妈）沿用全局表的既有取舍：只做触发，本身不进 token。
_ROLE_QUERY_KEYS: Dict[str, Tuple[str, ...]] = {
    "husband": ("老公", "丈夫", "husband"),
    "wife": ("老婆", "妻子", "wife"),
    "ex_husband": ("前夫", "ex-husband"),
    "ex_wife": ("前妻", "ex-wife"),
    "father": ("爸爸", "爸", "父亲", "老爸", "father"),
    "mother": ("妈妈", "妈", "母亲", "老妈", "mother"),
    "daughter": ("女儿", "daughter"),
    "son": ("儿子", "son"),
    "brother": ("哥哥", "弟弟", "兄弟", "brother"),
    "sister": ("姐姐", "妹妹", "姊妹", "sister"),
    "grandfather": ("爷爷", "外公", "grandpa", "grandfather"),
    "grandmother": ("奶奶", "外婆", "grandma", "grandmother"),
    "aunt": ("姑姑", "阿姨", "姨妈", "aunt"),
    "uncle": ("叔叔", "舅舅", "uncle"),
    "partner": ("伴侣", "partner"),
    "child": ("孩子", "child"),
    "parents": ("父母", "parents"),
}

# ``context.family`` 的 key 五花八门（真实档案有 father/mother/brother/daughter/
# ex_wife/grandfather；Studio 手填还可能是中文）。**未登记的 key 一律跳过**——
# 同处 context 的 ``german_cultural_heritage.words_he_uses`` 那类子键不是亲属。
_ROLE_SYNONYMS: Dict[str, str] = {
    "father": "father", "dad": "father", "papa": "father", "父亲": "father",
    "爸爸": "father", "爸": "father", "老爸": "father",
    "mother": "mother", "mom": "mother", "mama": "mother", "母亲": "mother",
    "妈妈": "mother", "妈": "mother", "老妈": "mother",
    "husband": "husband", "丈夫": "husband", "老公": "husband",
    "wife": "wife", "妻子": "wife", "老婆": "wife",
    "ex_husband": "ex_husband", "exhusband": "ex_husband",
    "former_husband": "ex_husband", "前夫": "ex_husband",
    "ex_wife": "ex_wife", "exwife": "ex_wife", "former_wife": "ex_wife",
    "前妻": "ex_wife",
    "son": "son", "儿子": "son",
    "daughter": "daughter", "女儿": "daughter",
    "child": "child", "children": "child", "kid": "child", "kids": "child",
    "孩子": "child",
    "brother": "brother", "brothers": "brother", "elder_brother": "brother",
    "younger_brother": "brother", "哥哥": "brother", "弟弟": "brother",
    "兄弟": "brother",
    "sister": "sister", "sisters": "sister", "elder_sister": "sister",
    "younger_sister": "sister", "姐姐": "sister", "妹妹": "sister",
    "姊妹": "sister",
    "grandfather": "grandfather", "grandpa": "grandfather",
    "爷爷": "grandfather", "外公": "grandfather", "祖父": "grandfather",
    "grandmother": "grandmother", "grandma": "grandmother",
    "奶奶": "grandmother", "外婆": "grandmother", "祖母": "grandmother",
    "partner": "partner", "spouse": "partner", "伴侣": "partner",
    "配偶": "partner",
    "parents": "parents", "父母": "parents",
    "aunt": "aunt", "auntie": "aunt", "姑姑": "aunt", "阿姨": "aunt",
    "姨妈": "aunt",
    "uncle": "uncle", "叔叔": "uncle", "舅舅": "uncle",
}

# 自由文本里的称谓锚点（只用于**拉丁**专名，见模块 docstring 的取舍）。
_TEXT_KINSHIP_TERMS: Tuple[Tuple[str, str], ...] = (
    ("前夫", "ex_husband"), ("前妻", "ex_wife"),
    ("老公", "husband"), ("丈夫", "husband"),
    ("老婆", "wife"), ("妻子", "wife"),
    ("父亲", "father"), ("爸爸", "father"),
    ("母亲", "mother"), ("妈妈", "mother"),
    ("女儿", "daughter"), ("儿子", "son"),
    ("哥哥", "brother"), ("弟弟", "brother"), ("兄弟", "brother"),
    ("姐姐", "sister"), ("妹妹", "sister"),
    ("爷爷", "grandfather"), ("外公", "grandfather"),
    ("奶奶", "grandmother"), ("外婆", "grandmother"),
    ("husband", "husband"), ("wife", "wife"),
)

# 称谓与人名之间只允许「引名」性质的连接。**刻意不收 ``是``**：「父亲是 Vancouver
# 大学的教授」会把地名/机构名当人名抽走，而漏掉「母亲是 Elena Lin」由结构化轨兜底。
_NAME_MARKERS: Tuple[str, ...] = (
    "英文名字", "英文名", "中文名", "名字是", "名字叫", "名叫", "本名", "叫", "：", ":",
)
# 中间出现这些字 = 已经拐进另一个语义单元（「哥哥在 Google 工作」的 ``在``）。
_TEXT_BLOCK_CHARS = frozenset("。！？!?；;、，,\n\r…—在去到从往和与跟及给对被把")

_LATIN_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]*")
_CJK_HEAD_RE = re.compile(r"^[\u4e00-\u9fff]+")
_HEAD_SPLIT_RE = re.compile(r"[，,。;；\n\r]")

# 姓氏虚词：可以留在全名串里（``maría de la cruz``），但绝不单独成片段。
_NAME_PARTICLES = frozenset({
    "de", "del", "della", "la", "las", "le", "les", "los", "van", "von",
    "der", "den", "da", "di", "do", "dos", "bin", "ibn", "al", "el", "st",
    "mac", "mc", "y", "e",
})
# Titlecase 但不是人名：代词/冠词开头的句子、常见机构与平台名。宁可多列几个，
# 也不要产出「哥哥 → google」这种会稳定注入错段的别名。
_LATIN_NAME_BLOCKLIST = frozenset({
    "i", "he", "she", "they", "we", "you", "my", "his", "her", "our", "their",
    "the", "a", "an", "and", "but", "or", "in", "on", "at", "of", "for",
    "is", "was", "are", "were", "it", "this", "that", "there", "then",
    "mr", "mrs", "ms", "dr", "prof", "sir", "madam",
    "google", "facebook", "meta", "apple", "amazon", "microsoft", "tesla",
    "uber", "netflix", "twitter", "instagram", "telegram", "whatsapp",
    "youtube", "tiktok", "sony", "toyota", "samsung", "huawei", "nike",
    "harvard", "stanford", "oxford", "cambridge", "yale", "princeton",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "january", "february", "march", "april", "june", "july",
    "august", "september", "october", "november", "december",
    # 双语文档的表头/节标题词（真实语料实锤：``Aunt's Name:`` 整个被当人名串、
    # ``Christmas Day``/``Turner Syndrome``/``Marital Status`` 攒够票就进表）。
    # 亲属称谓的英文词本身也不是名字——挡掉后 ``Aunt Maria`` 只留 Maria，更准。
    "name", "names", "story", "stories", "day", "date", "job", "company",
    "position", "status", "marital", "syndrome", "race", "mixed", "journey",
    "earning", "childhood", "chapter", "part", "section", "gender", "age",
    "christmas", "easter", "birthday", "holiday", "festival", "fiesta",
    "father", "mother", "sister", "brother", "son", "daughter", "aunt",
    "uncle", "husband", "wife", "grandfather", "grandmother", "grandma",
    "grandpa", "cousin", "family", "spanish", "japanese", "chinese",
    "english", "italian", "french", "german", "korean",
})

# CJK 候选里出现这些字 = 是句子片段不是名字（「一只叫」「我最敬佩」）。
_CJK_STOP_CHARS = frozenset(
    "的是在和与我她他你们个位名岁只叫有很多没不了就都也还要会能过最以及或者"
    "很非常都被把给对从去到那这什么么吗呢啊吧呀已经曾经现在后来因此所以但"
    "一二三四五六七八九十几每"
)
# 2~3 字但明显不是人名的常见填写（运营在 family 里写状态/职业而非名字）。
_CJK_NOT_NAME = frozenset({
    "在世", "已故", "去世", "过世", "离世", "健在", "早逝", "不详", "未知",
    "保密", "无", "已婚", "离婚", "单身", "丧偶", "退休", "高管", "老师",
    "教师", "医生", "护士", "工人", "农民", "律师", "主妇", "商人", "司机",
    "厨师", "警察", "军人", "会计", "经理", "总监", "老板", "教授", "博士",
    "硕士", "学生", "公务", "自由", "无业", "家庭", "本人", "自己", "同上",
})
# 百家姓（常见 ~110）：CJK 人名的**必要**条件之一。日文汉字名（美月）因此会被
# 漏掉——可接受：漏了还有语义轨，错了会稳定注入错段。
_CJK_SURNAMES = frozenset(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董"
    "袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟"
    "熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤温常康施文"
    "牛樊葛洪季倪聂庄辛翟殷詹申欧耿关兰焦俞左柳甘祝包宁尚符舒阮柯纪梅童凌毕"
)

_GENDER_FEMALE = frozenset({"female", "f", "woman", "女", "女性"})
_GENDER_MALE = frozenset({"male", "m", "man", "男", "男性"})


# ── 形态判定 ────────────────────────────────────────────────────────────────

def _is_name_token(tok: str) -> bool:
    """Titlecase 且非停用词：``Lin``/``José`` 真、``IT``/``MIT``/``the`` 假。

    全大写一律拒（``IT 高管``/``MIT``），首字母小写一律拒（普通英文单词）。
    """
    if len(tok) < 2 or not tok[0].isupper():
        return False
    if tok[1:].isupper():
        return False
    return tok.lower().strip("'’-") not in _LATIN_NAME_BLOCKLIST


def _latin_name_at(text: str, pos: int) -> Tuple[List[str], int]:
    """从 ``pos``（须正好是拉丁词首）起吃一个人名串，返回 (词列表, 结束偏移)。

    只吃**紧邻**（单空格或连字符相连）的 Titlecase 词，中间允许 de/van 这类虚词；
    括号/逗号/CJK 一律断开。
    """
    tokens: List[str] = []
    end = pos
    cursor = pos
    while len(tokens) < _MAX_NAME_TOKENS:
        m = _LATIN_TOKEN_RE.match(text, cursor)
        if not m:
            break
        tok = m.group(0)
        if _is_name_token(tok):
            tokens.append(tok)
            end = m.end()          # 名字只能**以实名词收尾**，虚词不推进 end
        elif tokens and tok.lower() in _NAME_PARTICLES:
            tokens.append(tok)
        else:
            break
        nxt = _LATIN_TOKEN_RE.search(text, m.end())
        if not nxt:
            break
        gap = text[m.end():nxt.start()]
        if len(gap) > 1 or gap.strip(" \u00a0") != "":
            break
        cursor = nxt.start()
    while tokens and tokens[-1].lower() in _NAME_PARTICLES:
        tokens.pop()
    return tokens, end


def _latin_fragments(tokens: Sequence[str]) -> List[str]:
    """全名 + 有用前缀 + 名（given name）。姓氏单独成片段**不产出**——
    ``Lin``/``Wei`` 这类短姓在子串匹配下会命中 ``online``/``weird``。"""
    if not tokens:
        return []
    core = [t for t in tokens if t.lower() not in _NAME_PARTICLES]
    if not core:
        return []
    full = " ".join(tokens).lower()
    if len(full) < MIN_LATIN_FRAGMENT:
        return []
    if len(core) == 1 and len(core[0]) < MIN_LATIN_FRAGMENT:
        return []
    out = [full]
    if len(core) >= 3:
        out.append(" ".join(core[:2]).lower())
    if len(core) >= 2 and len(core[0]) >= MIN_LATIN_FRAGMENT:
        out.append(core[0].lower())
    return out


def _cjk_fragments(name: str) -> List[str]:
    """全名 + 去姓的名（≥2 字）。单字永不产出。"""
    out = [name]
    if len(name) >= 3:
        out.append(name[1:])
    return out


def _cjk_name_from_head(head: str) -> Optional[str]:
    """结构化字段首段开头的 2~3 字 CJK 人名（姓氏白名单 + 停用字 + 非名词表三重闸）。

    位置信号（角色 key 的值、且在首段最前）本身很强，但真实档案里也有
    ``母亲：在世``、``父亲：高中老师`` 这类填法，形态闸不能省。
    """
    m = _CJK_HEAD_RE.match(head)
    if not m:
        return None
    run = m.group(0)
    # 首段开头的连续汉字串**整体**就是判据：4 字以上按词组处理（「高中老师」
    # 「白天上班」），不做「取前 3 字」的贪心切分——那会把词组切成假名字。
    if not 2 <= len(run) <= 3:
        return None
    if run in _CJK_NOT_NAME or run[0] not in _CJK_SURNAMES:
        return None
    if any(ch in _CJK_STOP_CHARS for ch in run):
        return None
    return run


# ── 抽取轨 ──────────────────────────────────────────────────────────────────

def _head_segment(text: str) -> str:
    """``林志强（Michael Lin），加拿大华裔IT高管`` → ``林志强（Michael Lin）``。

    结构化值的写法惯例是「名字在最前、逗号后是描述」，只认首段就把描述里的
    机构名/地名全挡在外面。
    """
    return _HEAD_SPLIT_RE.split(str(text).strip(), 1)[0].strip(" \t\"'“”‘’")


def _names_from_head(head: str) -> List[str]:
    out: List[str] = []
    cjk = _cjk_name_from_head(head)
    if cjk:
        out.extend(_cjk_fragments(cjk))
    runs = 0
    pos = 0
    while runs < _MAX_LATIN_RUNS_PER_HEAD:
        m = _LATIN_TOKEN_RE.search(head, pos)
        if not m:
            break
        tokens, end = _latin_name_at(head, m.start())
        pos = max(end, m.end())
        if tokens:
            frags = _latin_fragments(tokens)
            if frags:
                out.extend(frags)
                runs += 1
    return out


def _collect_from_family(fam: Mapping[str, Any], role_names: Dict[str, List[str]]) -> None:
    for i, (raw_key, raw_val) in enumerate(fam.items()):
        if i >= _MAX_FAMILY_ENTRIES:
            break
        key = str(raw_key).strip().lower().replace("-", "_").replace(" ", "_")
        role = _ROLE_SYNONYMS.get(key)
        if not role:
            continue
        if isinstance(raw_val, str):
            values: Sequence[Any] = (raw_val,)
        elif isinstance(raw_val, (list, tuple)):
            values = [v for v in raw_val if isinstance(v, str)][:2]
        else:
            continue
        for val in values:
            head = _head_segment(val)
            if not head:
                continue
            for name in _names_from_head(head):
                _push(role_names, role, name)


def _name_after_term(text: str, pos: int) -> List[str]:
    """自由文本里称谓之后的拉丁专名（紧邻 或 有显式引名标记，且中间没拐弯）。"""
    window = text[pos:pos + _FREE_TEXT_WINDOW]
    m = _LATIN_TOKEN_RE.search(window)
    if not m:
        return []
    between = window[:m.start()]
    if between.strip(" \u00a0") != "":
        if any(ch in _TEXT_BLOCK_CHARS for ch in between):
            return []
        if not any(mk in between for mk in _NAME_MARKERS):
            return []
    tokens, _end = _latin_name_at(text, pos + m.start())
    return _latin_fragments(tokens)


def _collect_from_text(text: str, role_names: Dict[str, List[str]]) -> None:
    low = text.lower()
    if len(low) != len(text):
        low = text          # 少数字符 lower 后长度会变（İ）→ 索引对不齐，宁可少认英文称谓
    for term, role in _TEXT_KINSHIP_TERMS:
        hay = low if term.isascii() else text
        start = 0
        hits = 0
        while hits < _MAX_HITS_PER_TERM:
            i = hay.find(term, start)
            if i < 0:
                break
            start = i + len(term)
            hits += 1
            for name in _name_after_term(text, start):
                _push(role_names, role, name)


def _push(role_names: Dict[str, List[str]], role: str, name: str) -> None:
    bucket = role_names.setdefault(role, [])
    if name and name not in bucket:
        bucket.append(name)


# ── 组装 ────────────────────────────────────────────────────────────────────

def _normalized_gender(profile: Mapping[str, Any]) -> str:
    g = str(profile.get("gender") or "").strip().lower()
    if g in _GENDER_FEMALE:
        return "female"
    if g in _GENDER_MALE:
        return "male"
    return ""


def _apply_spillover(role_names: Dict[str, List[str]], gender: str) -> None:
    """客户问「老公」时，档案里只有 ``ex_husband``/``partner`` 也该能召回。

    检索别名不是事实断言——注入的是原文段落，离婚与否由段落本身讲清楚；
    真正的事故是**空注入**（AI 只能编）。当前配偶存在时永远优先，绝不被前任覆盖。
    """
    if not role_names.get("husband"):
        cand = list(role_names.get("ex_husband", ()))
        if gender == "female":
            cand += [n for n in role_names.get("partner", ()) if n not in cand]
        if cand:
            role_names["husband"] = cand
    if not role_names.get("wife"):
        cand = list(role_names.get("ex_wife", ()))
        if gender == "male":
            cand += [n for n in role_names.get("partner", ()) if n not in cand]
        if cand:
            role_names["wife"] = cand
    # 合并键（孩子/父母）按人**轮转**取值而不是拼接：单键额度只有几个，直接拼接
    # 会让第一个人的片段占满，「父母」变成只有父亲——union 键就白设了。
    if not role_names.get("child"):
        kids = _round_robin(role_names.get("son", ()), role_names.get("daughter", ()))
        if kids:
            role_names["child"] = kids
    if not role_names.get("parents"):
        ps = _round_robin(role_names.get("father", ()), role_names.get("mother", ()))
        if ps:
            role_names["parents"] = ps


def _round_robin(*buckets: Sequence[str]) -> List[str]:
    out: List[str] = []
    for i in range(max((len(b) for b in buckets), default=0)):
        for b in buckets:
            if i < len(b):
                out.append(b[i])
    return _dedup(out)


def _dedup(names: Sequence[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for n in names:
        k = n.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(n)
    return out


def build_entity_aliases(
    profile: Any,
    *,
    max_keys: int = MAX_KEYS,
    max_values_per_key: int = MAX_VALUES_PER_KEY,
) -> Dict[str, tuple]:
    """从人设档案派生「口语称谓 → 该人设文档里实际使用的实体名」别名表。

    返回值与 ``persona_bio_store._QUERY_ALIASES`` 同构（key=触发子串，value=要加入
    token 集的整词 tuple，已折小写），供调用方与全局表合并后喂给
    ``expand_query_tokens``。任何异常/脏输入一律返回 ``{}``——检索降级只是少注入，
    抛异常会打断整条聊天链。
    """
    try:
        return _build(profile, max(0, int(max_keys)), max(1, int(max_values_per_key)))
    except Exception as e:  # noqa: BLE001 — 软失败：宁可没有别名，不可打断聊天
        logger.debug("build_entity_aliases failed: %s", e)
        return {}


def _build(profile: Any, max_keys: int, max_values_per_key: int) -> Dict[str, tuple]:
    if not isinstance(profile, Mapping) or not profile:
        return {}
    ctx = profile.get("context")
    ctx = ctx if isinstance(ctx, Mapping) else {}
    fam = ctx.get("family")

    role_names: Dict[str, List[str]] = {}
    if isinstance(fam, Mapping):
        _collect_from_family(fam, role_names)

    texts: List[str] = []
    if isinstance(fam, str):
        texts.append(fam)
    elif isinstance(fam, (list, tuple)):
        texts.extend(x for x in fam if isinstance(x, str))
    for src in (profile.get("background"), ctx.get("specific_memories")):
        if isinstance(src, str):
            texts.append(src)
        elif isinstance(src, (list, tuple)):
            texts.extend(x for x in src if isinstance(x, str))
    if texts:
        # 换行是断句字符 → 拼接不会让相邻条目互相串味
        _collect_from_text("\n".join(texts)[:_MAX_SCAN_CHARS], role_names)

    if not role_names:
        return {}
    _apply_spillover(role_names, _normalized_gender(profile))

    out: Dict[str, tuple] = {}
    for role, keys in _ROLE_QUERY_KEYS.items():
        names = role_names.get(role)
        if not names:
            continue
        for key in keys:
            if key not in out and len(out) >= max_keys:
                continue
            merged = _dedup(list(out.get(key, ())) + names)
            out[key] = tuple(merged[:max_values_per_key])
    return out


def merge_query_aliases(
    base: Mapping[str, Sequence[str]],
    extra: Mapping[str, Sequence[str]],
    *,
    max_values_per_key: int = MAX_VALUES_PER_KEY + 4,
) -> Dict[str, tuple]:
    """全局静态表 ⊕ 人设实体表。**同键不覆盖而是拼接**（base 在前）。

    直接 ``dict.update`` 会让 ``老公`` 丢掉全局的 ``丈夫/husband``——那两个词才是
    文档名片行的写法，实体名只是补一跳，两者必须共存。
    """
    out: Dict[str, tuple] = {}
    try:
        for src in (base, extra):
            if not isinstance(src, Mapping):
                continue
            for k, vals in src.items():
                key = str(k)
                if isinstance(vals, str):
                    vals = (vals,)
                items = [str(v) for v in vals if str(v).strip()]
                merged = _dedup(list(out.get(key, ())) + items)
                out[key] = tuple(merged[:max_values_per_key])
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("merge_query_aliases failed: %s", e)
        return {str(k): tuple(v) for k, v in base.items()} if isinstance(base, Mapping) else {}


# ════════════════════════════════════════════════════════════════════════════
# 文档轨（M9）：从**已入库的传记正文**派生实体别名
# ════════════════════════════════════════════════════════════════════════════
# profile 轨对「档案里根本没写亲属名」的人设无能为力（真实 9 个人设里 8 个 family
# 为空；Mizuki 甚至没有 profile）——而名字明明就在 33k 文档里：
# 「和一个西班牙人相恋，西班牙人（José Luis Jerónimo）25岁…」。
# 这形态没有「丈夫」二字，靠不上 profile 轨的「称谓紧邻引名」语义，要的是
# **行级共现投票**：一行里既有亲属/婚恋事件词、又有多词拉丁专名，且两者距离够近
# → 该专名给对应角色投一票；全文攒完票后按「每个名字归票数最高的那个角色」定归属。
#
# 与 profile 轨同一条安全哲学（错别名会**稳定地**注入错段，比没有更糟）：
# - 只认 **≥2 个实词的拉丁名**（单词 Titlecase 是城市/品牌重灾区：Barcelona）；
# - 尾词是场所词（Palace/Hotel/…）的不算人（求婚地点 Crystal Palace 不是老公）；
# - 一票制：每次名字出现只投给**最近**的那个标记，等距歧义弃权；
# - 全文票数 < min_votes 不产出（本文档中英双语，真亲属天然 ≥2 票）；
# - 名字归属歧义（两角色同票）整个名字弃权；
# - **跨人碰撞过滤**：同一片段被两个不同的人产出（父亲 José Leandro ×
#   前夫 José Luis → 都会派生 "josé"）→ 该片段两边都不发（发了=查爸爸中前夫段）。

DOC_SCAN_MAX_CHARS = 64_000   # 提取跑在**入库期**不是聊天热路，可以扫全文
DOC_NAME_WINDOW = 40          # 名字与标记允许的最大间距（字符）
DOC_MIN_VOTES = 2             # 全文共现票数下限（宁可漏不可错）
_DOC_MAX_PERSONS_PER_ROLE = 2

# 直接称谓：行内出现即可投票（比 profile 轨的 _TEXT_KINSHIP_TERMS 宽——那边要求
# 紧邻引名，这边只要求同行近距共现，所以英文称谓也能用）。
_DOC_DIRECT_TERMS: Tuple[Tuple[str, str], ...] = _TEXT_KINSHIP_TERMS + (
    ("father", "father"), ("mother", "mother"), ("daughter", "daughter"),
    ("son", "son"), ("brother", "brother"), ("sister", "sister"),
    ("grandfather", "grandfather"), ("grandmother", "grandmother"),
    ("姑姑", "aunt"), ("姨妈", "aunt"), ("aunt", "aunt"),
    ("叔叔", "uncle"), ("舅舅", "uncle"), ("uncle", "uncle"),
)
# 婚恋事件词：本身不指明「丈夫还是妻子」，投给中间角色 partner_event，
# 组装期按文档自述性别落到 husband/wife（未知则两头都发 + partner 兜底）。
_DOC_EVENT_TERMS: Tuple[str, ...] = (
    "相恋", "恋爱", "结婚", "求婚", "订婚", "离婚", "婚礼", "蜜月",
    "男朋友", "女朋友", "未婚夫", "未婚妻",
    "married", "marry", "wedding", "divorce", "divorced", "engaged",
    "engagement", "proposal", "proposed", "honeymoon", "relationship",
    "boyfriend", "girlfriend", "fiancé", "fiancée", "fiance", "fiancee",
)
_DOC_PARTNER_EVENT = "partner_event"
# 场所尾词/头词：多词 Titlecase 但明显是地点设施（求婚地点、任职酒店），
# 出现在名字串的首或尾 → 整串不算人名。西英双语（本产品语料两者都常见）。
_PLACE_NAME_TOKENS = frozenset({
    "palace", "palacio", "hotel", "hostal", "hospital", "museum", "museo",
    "park", "parque", "plaza", "plaça", "square", "church", "iglesia",
    "cathedral", "catedral", "castle", "castillo", "tower", "torre",
    "beach", "playa", "island", "isla", "street", "calle", "avenue",
    "avenida", "road", "bridge", "puente", "station", "estación",
    "airport", "aeropuerto", "university", "universidad", "college",
    "school", "escuela", "academy", "restaurant", "restaurante", "café",
    "cafe", "bar", "club", "mall", "market", "mercado", "garden",
    "jardín", "city", "ciudad", "town", "village", "temple", "shrine",
    "bay", "port", "puerto", "valley", "mountain", "montaña", "lake",
    "lago", "river", "río", "basilica", "basílica", "politecnico",
    "polytechnic", "instituto", "institute", "san", "santa", "saint",
})
# 性别自述行（性别：女 / Gender: Female）——文档轨唯一允许的「事实推断」，
# 只用于决定 partner_event 落 老公 还是 老婆。
_DOC_GENDER_RE = re.compile(
    r"(?:性别|gender)\s*[：:]\s*(女|男|female|male|woman|man|f|m)\b",
    re.I,
)
_DOC_GENDER_MAP = {"女": "female", "female": "female", "woman": "female",
                   "f": "female", "男": "male", "male": "male",
                   "man": "male", "m": "male"}
# partner_event 的出键：查询侧「老公/前夫」都该召回同一段婚史（离没离由原文段落
# 自己讲清楚；错的是空注入）。性别未知时两头都发，宁多一跳不误人。
_PARTNER_KEYS = {
    "female": ("老公", "丈夫", "前夫", "前任", "husband", "ex-husband"),
    "male": ("老婆", "妻子", "前妻", "前任", "wife", "ex-wife"),
    "": ("老公", "丈夫", "前夫", "老婆", "妻子", "前妻", "前任",
         "husband", "wife", "伴侣", "partner"),
}


def _looks_like_place(core: Sequence[str]) -> bool:
    if not core:
        return True
    return (core[0].lower() in _PLACE_NAME_TOKENS
            or core[-1].lower() in _PLACE_NAME_TOKENS)


def _line_name_runs(line: str) -> List[Tuple[List[str], int, int]]:
    """行内全部**多词**拉丁人名串 → ``[(词列表, 起, 止)]``（单词串一律不算）。"""
    runs: List[Tuple[List[str], int, int]] = []
    pos = 0
    while True:
        m = _LATIN_TOKEN_RE.search(line, pos)
        if not m:
            break
        tokens, end = _latin_name_at(line, m.start())
        pos = max(end, m.end())
        if not tokens:
            continue
        core = [t for t in tokens if t.lower() not in _NAME_PARTICLES]
        if len(core) >= 2 and not _looks_like_place(core):
            runs.append((tokens, m.start(), end))
    return runs


def _term_spans(term: str, line: str, low: str) -> List[Tuple[int, int]]:
    """标记词在行内的所有出现。ASCII 词**整词**匹配——``son`` 是 ``person``/
    ``season``/``reason`` 的子串，按子串匹配会给行里每个 "person" 投一票儿子
    （真实语料实锤：把前夫的票拉成 partner/son 平票 → 整个人被歧义弃权）。"""
    if term.isascii():
        return [(m.start(), m.end())
                for m in re.finditer(rf"\b{re.escape(term)}\b", low)]
    out = []
    start = 0
    while True:
        i = line.find(term, start)
        if i < 0:
            break
        out.append((i, i + len(term)))
        start = i + 1
    return out


_POSSESSIVE_BEFORE_RE = re.compile(r"\b(his|her|their)\s*$")


def _third_person_owned(line: str, low: str, start: int, term: str) -> bool:
    """称谓前面是「她/他（的）/his/her/their」= **别人的**亲属，不给人设投票。

    真实语料实锤：「她把她儿子介绍给我，西班牙人（José Luis Jerónimo）」——
    José Luis 是财务总监的儿子、人设的丈夫；不挡第三人称领属，son 票会把
    partner 票拉平 → 整个人被歧义弃权，丈夫再次丢失（与 bazi_profile 的
    「我男朋友1993年…不落本人画像」同一护栏哲学）。
    """
    if term.isascii():
        return bool(_POSSESSIVE_BEFORE_RE.search(low[:start]))
    before = line[:start]
    return before.endswith(("她", "他")) or before.endswith(
        ("她的", "他的", "她们的", "他们的"))


def _line_markers(line: str) -> List[Tuple[int, int, str]]:
    """行内全部亲属/事件标记 → ``[(起, 止, 角色)]``，被更长标记包住的丢弃。

    包含过滤挡「grandfather 里含 father」这类子串误判：只留最长者。
    第三人称领属的**直接称谓**（她儿子/his aunt）整个不进标记表。
    """
    low = line.lower()
    found: List[Tuple[int, int, str]] = []
    for term, role in _DOC_DIRECT_TERMS:
        for s, e in _term_spans(term, line, low):
            if _third_person_owned(line, low, s, term):
                continue
            found.append((s, e, role))
    for term in _DOC_EVENT_TERMS:
        for s, e in _term_spans(term, line, low):
            found.append((s, e, _DOC_PARTNER_EVENT))
    out = []
    for s, e, role in found:
        contained = any(
            (s2 <= s and e <= e2 and (e2 - s2) > (e - s))
            for s2, e2, _r2 in found)
        if not contained:
            out.append((s, e, role))
    return out


def _nearest_role(markers: Sequence[Tuple[int, int, str]],
                  n_start: int, n_end: int, window: int) -> Optional[str]:
    """离名字最近的标记角色；超窗 None；**等距两个不同角色 = 弃权**。"""
    best_gap: Optional[int] = None
    best_roles: set = set()
    for s, e, role in markers:
        gap = (n_start - e) if e <= n_start else ((s - n_end) if s >= n_end else 0)
        if gap > window or gap < 0:
            continue
        if best_gap is None or gap < best_gap:
            best_gap, best_roles = gap, {role}
        elif gap == best_gap:
            best_roles.add(role)
    return best_roles.pop() if len(best_roles) == 1 else None


def _doc_gender(text: str) -> str:
    m = _DOC_GENDER_RE.search(text)
    return _DOC_GENDER_MAP.get(m.group(1).lower(), "") if m else ""


def build_doc_entity_aliases(
    text: Any,
    *,
    min_votes: int = DOC_MIN_VOTES,
    window: int = DOC_NAME_WINDOW,
    max_keys: int = MAX_KEYS,
    max_values_per_key: int = MAX_VALUES_PER_KEY,
) -> Dict[str, tuple]:
    """从传记正文派生「口语称谓 → 文档里的实体名」别名表（入库期调用）。

    与 ``build_entity_aliases``（profile 轨）同构同哲学，输入换成正文全文。
    A/B 实测（真实 33k 文档 56 例）：把「你老公对你好吗」从空注入救成正确
    注入婚史段。任何异常/脏输入返回 ``{}``。
    """
    try:
        return _build_doc(str(text or "")[:DOC_SCAN_MAX_CHARS],
                          max(1, int(min_votes)), max(0, int(window)),
                          max(0, int(max_keys)), max(1, int(max_values_per_key)))
    except Exception as e:  # noqa: BLE001 — 软失败：宁可没有别名
        logger.debug("build_doc_entity_aliases failed: %s", e)
        return {}


def _build_doc(text: str, min_votes: int, window: int,
               max_keys: int, max_values_per_key: int) -> Dict[str, tuple]:
    if not text.strip():
        return {}
    # full_lower → {role → 票数}；full_lower → 词列表（取首次出现的形态）
    votes: Dict[str, Dict[str, int]] = {}
    tokens_of: Dict[str, List[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        runs = _line_name_runs(line)
        if not runs:
            continue
        markers = _line_markers(line)
        if not markers:
            continue
        for tokens, n_start, n_end in runs:
            role = _nearest_role(markers, n_start, n_end, window)
            if not role:
                continue
            full = " ".join(tokens).lower()
            votes.setdefault(full, {})
            votes[full][role] = votes[full].get(role, 0) + 1
            tokens_of.setdefault(full, list(tokens))

    # 归属：每个名字归票数最高的角色；并列 = 歧义弃权。
    role_persons: Dict[str, List[Tuple[str, int]]] = {}
    for full, by_role in votes.items():
        total = sum(by_role.values())
        if total < min_votes:
            continue
        top = max(by_role.values())
        winners = [r for r, c in by_role.items() if c == top]
        if len(winners) != 1:
            continue
        role_persons.setdefault(winners[0], []).append((full, total))

    if not role_persons:
        return {}

    # 片段展开（每人：全名/前二词/名），并记「片段 → 是哪些人产出的」。
    frag_owner: Dict[str, set] = {}
    role_frags: Dict[str, List[str]] = {}
    for role, persons in role_persons.items():
        persons.sort(key=lambda p: (-p[1], p[0]))
        for full, _cnt in persons[:_DOC_MAX_PERSONS_PER_ROLE]:
            for frag in _latin_fragments(tokens_of[full]):
                frag_owner.setdefault(frag, set()).add(full)
                _push(role_frags, role, frag)
    banned = {f for f, owners in frag_owner.items() if len(owners) > 1}

    gender = _doc_gender(text)
    out: Dict[str, tuple] = {}

    def _emit(keys: Sequence[str], frags: Sequence[str]) -> None:
        vals = [f for f in frags if f not in banned]
        if not vals:
            return
        for key in keys:
            if key not in out and len(out) >= max_keys:
                continue
            merged = _dedup(list(out.get(key, ())) + list(vals))
            out[key] = tuple(merged[:max_values_per_key])

    for role, frags in role_frags.items():
        if role == _DOC_PARTNER_EVENT:
            _emit(_PARTNER_KEYS[gender], frags)
        elif role in ("husband", "ex_husband"):
            _emit(_PARTNER_KEYS["female"], frags)
        elif role in ("wife", "ex_wife"):
            _emit(_PARTNER_KEYS["male"], frags)
        else:
            _emit(_ROLE_QUERY_KEYS.get(role, ()), frags)
    return out
