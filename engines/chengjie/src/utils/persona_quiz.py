# -*- coding: utf-8 -*-
"""人设一致性考题（E 线，2026-07-27）：档案自动出题 → 真人设 prompt 实测 → 自动判分。

「文档 → LLM 抽取 → 人设入库」（persona_doc_import）之后的**上线前质检**：从人设档案
确定性生成 N 道事实题（「你父亲是做什么的？」期望关键词「建筑师」），用**真实人设
system prompt**（``PersonaManager._format_persona_instructions`` 同款装配）逐题问 LLM，
按关键词子串命中判分出报告——人设资料是否真的被 AI 记住并正确引用，一测便知。

三段纯函数为主：
- ``build_quiz(persona, n, seed)``  — 确定性出题（同档案同题目，seed 缺省取 name/id crc32）。
  素材源：context.specific_memories（实体挖空：亲属名字/职业、出生地拉丁地名、学校、年份）、
  background（切句同法 1-2 题 + 专业/教龄/科目）、role、age、names.english、appearance、
  tastes.likes/dislikes、context.family、context.filipino_connection。
  expect 关键词全部取自档案原文（判分靠子串命中，不造同义词）；素材 <3 题 → []。
  **出题范围 ⊆ prompt 消费字段**（``_field_consumed`` 绑定 K1 注册表
  ``persona_manager.PROMPT_CONSUMED_FIELDS``）：进了 prompt 的字段 AI 才**可能**答对，
  考它公平；没进 prompt 的字段 AI 根本没被告知，考它必然是假阴性。每题带 ``field``
  （所依据的字段点路径），不在注册表内的整题丢弃（原因记 debug 日志）。
  **出题守卫（一道假阴性坏题的代价 >> 少一道好题）**：年份闸门（``_is_year_mention``：
  合理区间 + 无紧邻数量词 + 正面年份特征，「2000 粉丝」不出年份题）、槽位一致性
  （``_item_slot_ok``：名字槽 expect 必须像人名、亲属职业槽必须是词表职业、年份槽必须
  是合法年份、题干不得泄漏答案，不合格整题丢弃）、role 语义（``_role_is_non_working``：
  学生/退休/无业不问「在哪里工作」，改问「退休前是做什么的」）。过滤后不足
  ``MIN_QUIZ_ITEMS`` → 如实返回 []。
- ``score_answers(quiz, answers, judge_fn=None)`` — 逐题判分：answer 含任一 expect
  关键词（或 ``expect_variants`` 派生的词干）即 pass。比对前双侧过
  ``normalize_for_match``（NFKC 全半角 → 中文数字↔阿拉伯（含**单字+量词**「十年」→
  「10年」）→ 空白折叠 → casefold），只做归一后的**子串**匹配，不做同义/模糊
  （防假阳性）。**假阴性是本工具的头号威胁**——运营被误报几次就会彻底忽略告警，
  所以判错的题还有第三道兜底：可选 ``judge_fn``（签名同 ``chat_fn``，默认 None ＝
  行为不变）只对**关键词判错**的题请 LLM 复核一次，只认 ``YES``，其余（NO/空/异常/
  超时）一律 **fail-closed** 维持错判；改判的题记 ``judged: True``。
  开关 ``personas.quiz.llm_judge`` 由调用方读（``llm_judge_enabled(config)``）。
- ``run_quiz(persona, chat_fn, n, on_stage, judge_fn)`` — 逐题单独调用
  ``chat_fn(system, user, timeout) -> str``（每题 60s），空回答/异常记 fail 不炸；
  on_stage(f"quiz_{i}", pct) 汇报；``judge_fn`` 原样透传给 ``score_answers``。

CLI 离线冒烟（集成负责人跑，勿在 pytest 里真调 LLM）::

    python -m src.utils.persona_quiz --persona-file tmp_mizuki_extract.json \
        [--n 10] [--out tmp_quiz_report.json]
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
import unicodedata
import zlib
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("ai_chat_assistant.persona_quiz")

# ── 预算常量 ──────────────────────────────────────────────────────────────────
MIN_QUIZ_ITEMS = 3            # 可出题数低于此值 → 资料太薄，build_quiz 返回 []
QUIZ_LLM_TIMEOUT_SEC = 60.0   # 每题单独一次 chat_fn 调用的超时
QUIZ_LLM_MAX_TOKENS = 600     # 考题回答是普通聊天回复，不需要长输出
QUIZ_LLM_TEMPERATURE = 0.6    # 常规聊天温度（考的是事实召回，不是复读能力）

# 附加在真实人设装配 prompt 之后的一行（防 LLM 出戏点破考试）
QUIZ_SYSTEM_SUFFIX = "请始终以此身份自然回答，不要提及任何设定文档。"

# ── 出题范围 × prompt 数据链注册表（K2 续，2026-07-28）──────────────────────
# 兄弟线 K1 在 persona_manager 建了「哪些字段真的进 prompt」的登记表。
# ``PROMPT_CONSUMED_FIELDS`` 恰好就是**可以出题的字段全集**：
#   进了 prompt  → AI 才可能答对 → 考它公平，答错就是真 bug；
#   没进 prompt  → AI 根本没被告知 → 考它必然是假阴性（旧版考「年龄」正是如此，
#                  它「发现 bug」是撞对的，真问题是数据链断了，K1 已补）。
# 两套系统互相加固：以后谁加字段，注册表门禁逼他表态，出题器自动获得新题源。
#
# 只读 + 延迟导入（与 ``build_quiz_system_prompt`` 同款，防循环依赖）；
# 老版本 persona_manager 没有该常量 → 降级为**不校验**，保持向后兼容，
# 绝不让缺常量把出题器搞崩（考卷出不来比多一道题的风险严重得多）。
_SRC_DEFAULT_FIELD = {
    "memory": "context.specific_memories",
    "background": "background",
    "role": "role",
    "age": "age",
    "tastes": "tastes.likes",
}


def _consumed_fields() -> Optional[frozenset]:
    """K1 注册表快照；不可用（老版本/导入失败/空表）→ ``None`` ＝ 降级不校验。"""
    try:
        from src.utils.persona_manager import PROMPT_CONSUMED_FIELDS as consumed
    except Exception:
        logger.debug("[persona_quiz] PROMPT_CONSUMED_FIELDS 不可用 → 出题跳过字段校验",
                     exc_info=True)
        return None
    if not isinstance(consumed, (set, frozenset)) or not consumed:
        return None
    return frozenset(str(f) for f in consumed)


def _field_consumed(field: Any, allowed: Optional[frozenset]) -> bool:
    """字段点路径是否被 prompt 消费。

    注册表约定「登记某个 dict 路径即代表整棵子树」（如 ``context.family`` 覆盖
    ``context.family.father``），故逐级向上比对祖先前缀。``allowed=None``（注册表
    不可用）→ 一律放行；字段为空 → 不放行（出题器内部漏填 field 属 bug，不该蒙混）。
    """
    if allowed is None:
        return True
    parts = [p for p in str(field or "").split(".") if p]
    if not parts:
        return False
    return any(".".join(parts[:i]) in allowed for i in range(len(parts), 0, -1))


def _split_by_consumed(items: List[Dict[str, Any]],
                       allowed: Optional[frozenset]
                       ) -> tuple:
    """``items`` → ``(kept, dropped)``；dropped 项 ``(item, reason)`` 便于调试。"""
    kept: List[Dict[str, Any]] = []
    dropped: List[tuple] = []
    for it in items:
        field = str((it or {}).get("field") or "")
        if _field_consumed(field, allowed):
            kept.append(it)
            continue
        reason = "field_missing" if not field else f"field_not_in_prompt:{field}"
        dropped.append((it, reason))
        logger.debug("[persona_quiz] 丢弃题目（%s）: q=%s", reason,
                     (it or {}).get("q"))
    return kept, dropped

# ── 实体抽取（全部保守启发式，宁缺勿错——expect 必须来自档案原文）────────────

_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
_FOUR_DIGIT_RE = re.compile(r"\d{4}")

# 年份闸门（真机事故回归 2026-07-28：「IG 上有 2000 粉丝」被当年份出题，题干还被
# 挖成「某年粉丝」）。4 位数要当年份必须**同时**满足：
#   ① 落在 [YEAR_MIN, 今年+1]；② 紧邻没有数量词；③ 有正面年份特征
#     （后跟「年」/ 形如 2015-06、2015/6 / 前面是 在·于·自·从·到·至）。
YEAR_MIN = 1900
_QUANTITY_UNITS = (
    "粉丝", "关注", "订阅", "点赞", "评论", "会员", "用户", "客户", "员工", "学生",
    "公斤", "千克", "公里", "平方米", "平米", "美元", "欧元", "日元", "英镑", "人民币",
    "小时", "分钟", "万", "千", "百", "个", "人", "次", "元", "块", "名", "位",
    "条", "份", "件", "台", "部", "本", "杯", "首", "字", "页", "张", "只", "头",
    "家", "间", "层", "步", "克", "斤", "吨", "米", "天", "秒", "岁", "%", "kg",
    "km", "cm", "mm", "ml", "g",
)
_QUANTITY_UNITS_SORTED = tuple(sorted(_QUANTITY_UNITS, key=len, reverse=True))
_QUANTITY_FILLERS = "多余近约上下几"          # 「2000 多粉丝」
_YEAR_LEAD_WORDS = ("在", "于", "自", "从", "到", "至", "是")
_YEAR_DATE_TAIL_RE = re.compile(r"[-/.](?:0?[1-9]|1[0-2])(?!\d)")

# 拉丁专名：首字母大写（含重音）词的连续序列，如 "José Leandro Navarro" / "W Barcelona"
_LATIN_WORD = r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ0-9'’\-]*"
_LATIN_SEQ_RE = re.compile(rf"{_LATIN_WORD}(?:\s+{_LATIN_WORD})*")
_ADJ_LATIN_RE = re.compile(rf"[\s·:：、]{{0,2}}({_LATIN_WORD}(?:\s+{_LATIN_WORD})*)")

# 亲属/关系词 → 第三人称代词（问句「{ta}是做什么的」用）
_RELATIONS: Dict[str, str] = {
    "父亲": "他", "爸爸": "他", "母亲": "她", "妈妈": "她",
    "姐姐": "她", "妹妹": "她", "哥哥": "他", "弟弟": "他",
    "丈夫": "他", "前夫": "他", "妻子": "她", "前妻": "她",
    "女儿": "她", "儿子": "他", "姑姑": "她", "舅舅": "他",
    "叔叔": "他", "爷爷": "他", "奶奶": "她", "外公": "他", "外婆": "她",
}

# 职业词表（命中取最长；覆盖常见人设档案职业叙述）
_OCCUPATIONS = (
    "酒店运营总监助理", "运营总监助理", "国际贸易专员", "数据分析师", "市场经理",
    "运营总监", "总监助理", "建筑师", "工程师", "程序员", "设计师", "分析师",
    "创业者", "公务员", "咖啡店主", "店主", "老板", "总监", "经理", "助理",
    "专员", "顾问", "教授", "教师", "老师", "医生", "护士", "律师", "会计",
    "厨师", "作家", "记者", "军人", "警察", "销售", "司机", "模特", "演员",
    "歌手", "学生",
    # 现代/自由职业补齐（2026-07-28）：真机 haruko_traveler role=「旅行博主 /
    # 自由插画师」两个词都不在词表 → role 题出不来 → 全档只剩 2 题被整体跳过。
    # 收词标准同上：**独立成词的职业名词**，不收易被包含进无关词的单字/泛词
    # （「运营」「策划」「护理」这类既是职业又是动作的一律不收）。
    "自由撰稿人", "健身教练", "插画师", "摄影师", "调酒师", "咖啡师", "美容师",
    "理发师", "翻译官", "快递员", "外卖员", "服务员", "配音演员", "剪辑师",
    "博主", "主播", "编剧", "编辑", "导游", "教练", "翻译", "空乘", "保安",
)

# CJK 人名闸门：亲属词后紧邻 2-3 个 CJK 字符才认名字，且要过两道闸——
# ① 首字不是动词/介词/助词（「母亲在…」「父亲诊断…」不是名字）；
# ② 名字后是句读/系词边界（「姐姐张景然在佳士得」→ 张景然）。宁缺勿错。
_CJK_NAME_PREFIX_STOP = set(
    "是在于的和与跟叫也已因后曾去离结生诊出提被从对向让把当做得很则约经介回随们及有个一两开设"
    # 事故补充（2026-07-28）：「退休后帮儿子带孙子」→ 名字被抽成「带孙子」。
    # 动词/副词开头一律不是名字。
    "带帮陪送教找看想说喜爱用每常总还就都会能要可没不过给替为同住买玩吃学工照负"
    # 事故补充（2026-07-28 K2 扩题源自查）：「供妹妹读书」→ 名字被抽成「读书」。
    # 同类动词一次补齐（均非汉姓，不会误杀真名字）。
    "读写讲骗跑哭笑睡醒催赚挣供劝念接搬躲欠扛忍熬混盯打拿收发问答走"
)
_CJK_NAME_BOUNDARY = set("是在做当任于，。、；：！？（）() 　\t")

# 名字槽位校验（防「期望答案与题干牛头不对马嘴」——假阴性摧毁工具可信度）
_NAME_FUNC_CHARS = set("的了吗呢吧着过就都还也很太并且或而但把被让使给和与跟对向从此其之")
_KIN_NOUNS = tuple(_RELATIONS) + (
    "孙子", "孙女", "外孙", "重孙", "孩子", "小孩", "家人", "家里", "老公", "老婆",
    "配偶", "对象", "同事", "同学", "朋友", "邻居", "亲戚", "客户", "老家",
)
_LATIN_NAME_RE = re.compile(r"[A-Za-zÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+")

# role 语义表（保守）：非在职身份不问「在哪里工作/什么职位」。
_NON_WORKING_ROLE_WORDS = (
    "大学生", "研究生", "高中生", "初中生", "小学生", "留学生", "学生", "在读",
    "读研", "读博", "考研", "备考", "退休", "离休", "无业", "待业", "失业",
    "家庭主妇", "全职妈妈", "全职太太", "主妇", "啃老",
)
# 「学生公寓管理员」「退休金核算专员」这类**在职**岗位名里含上述词 → 不算非在职。
# 刻意只收机构/物件类后缀，不收职衔（「退休老师」该判非在职；误判成在职就会重演
# 事故 C，误判成非在职只是少出一道题——两类错的代价不对等）。
_ROLE_COMPOUND_TAIL = (
    "公寓", "宿舍", "食堂", "会馆", "会", "中心", "机构", "事务", "管理", "服务",
    "金", "证", "卡", "处", "科", "餐厅", "公司",
)

# 学校名：CJK 词干 +（大学|学院），词干先剥掉动词/时间前缀（「考入都灵大学」→ 都灵大学）
_SCHOOL_RE = re.compile(r"([\u4e00-\u9fff]{2,6})(大学|学院)")
_SCHOOL_PREFIX_TRIM = set("年月日考入读上在了于去到的进就是从与和毕业间被经录取转升")


def _latin_tokens(text: str, limit: int = 2) -> List[str]:
    """文本里的拉丁专名 token（长度 ≥2、去重、按出现序），如 ["Navarra", "Pamplona"]。"""
    out: List[str] = []
    for m in _LATIN_SEQ_RE.finditer(str(text or "")):
        for tok in m.group(0).split():
            tok = tok.strip("'’-")
            if len(tok) >= 2 and tok not in out:
                out.append(tok)
            if len(out) >= limit:
                return out
    return out


def _adjacent_latin_token(text: str, pos: int) -> str:
    """``pos`` 紧邻（容忍 ≤2 个分隔符）的拉丁专名序列，取首个长度 ≥2 的 token。"""
    m = _ADJ_LATIN_RE.match(text, pos)
    if not m:
        return ""
    for tok in m.group(1).split():
        tok = tok.strip("'’-")
        if len(tok) >= 2:
            return tok
    return ""


def _adjacent_cjk_name(text: str, pos: int) -> str:
    """``pos`` 紧邻的 2-3 字 CJK 人名（过前缀停用字 + 后缀边界两道闸）。"""
    tail = text[pos:pos + 4]
    if not tail:
        return ""
    if not _CJK_CHAR_RE.match(tail[0]) or tail[0] in _CJK_NAME_PREFIX_STOP:
        return ""
    for ln in (3, 2):
        if len(tail) < ln:
            continue
        cand = tail[:ln]
        if not all(_CJK_CHAR_RE.match(c) for c in cand):
            continue
        nxt = text[pos + ln: pos + ln + 1]
        if not nxt or nxt in _CJK_NAME_BOUNDARY:
            return cand
    return ""


def _is_year_mention(text: str, start: int, end: int) -> bool:
    """``text[start:end]`` 这个 4 位数是否真的是「年份」（三重判据，见 YEAR_MIN 注释）。"""
    raw = text[start:end]
    if not raw.isdigit():
        return False
    year = int(raw)
    if not (YEAR_MIN <= year <= datetime.now().year + 1):
        return False
    if start > 0 and text[start - 1].isdigit():
        return False
    if text[end:end + 1].isdigit():
        return False                       # 更长的数字串（编号/金额），不是年份
    tail = text[end:end + 8].lstrip(" \u3000\t")
    probe = tail.lstrip(_QUANTITY_FILLERS)
    if any(probe.startswith(u) for u in _QUANTITY_UNITS_SORTED):
        return False                       # 「2000 粉丝」「1998 元」：数量不是年份
    if tail.startswith("年"):
        return True
    if _YEAR_DATE_TAIL_RE.match(tail):
        return True                        # 2015-06 / 2015/6 / 2015.06
    head = text[:start].rstrip(" \u3000\t")
    return head.endswith(_YEAR_LEAD_WORDS)


_YEAR_TAIL_RE = re.compile(r"[ \u3000\t]*年")


def _year_spans(text: str) -> List[tuple]:
    """文本里所有「真年份」跨度 ``(start, end, year)``（含紧跟的「年」字，供挖空用）。

    「年」与数字之间允许空白（真档案里「2019 年第一次落地马尼拉」很常见）——
    否则挖空后题干会留下「某年 年第一次…」的重字。
    """
    out: List[tuple] = []
    for m in _FOUR_DIGIT_RE.finditer(text):
        if not _is_year_mention(text, m.start(), m.end()):
            continue
        end = m.end()
        tail = _YEAR_TAIL_RE.match(text, end)
        if tail:
            end = tail.end()
        out.append((m.start(), end, m.group(0)))
    return out


def _mask_year_spans(text: str, spans: List[tuple]) -> str:
    """只把**通过闸门的年份**挖成「某年」；数量类 4 位数原样保留（题干才通顺）。"""
    out, cursor = [], 0
    for start, end, _year in spans:
        out.append(text[cursor:start])
        out.append("某年")
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _looks_like_person_name(cand: str) -> bool:
    """``cand`` 像不像人名（名字槽位硬校验；宁可判否也不放动宾短语过关）。"""
    cand = str(cand or "").strip()
    if not cand:
        return False
    if not _CJK_CHAR_RE.search(cand):
        return bool(_LATIN_NAME_RE.fullmatch(cand)) and cand[:1].isupper()
    if not 2 <= len(cand) <= 4:
        return False
    if not all(_CJK_CHAR_RE.match(c) for c in cand):
        return False
    if cand[0] in _CJK_NAME_PREFIX_STOP:
        return False
    if any(c in _NAME_FUNC_CHARS for c in cand):
        return False
    if any(w in cand for w in _KIN_NOUNS):
        return False                       # 「带孙子」「我儿子」不是名字
    if _occupation_hits(cand, limit=1):
        return False                       # 「王医生」是称谓不是名字
    return True


def _role_is_non_working(role: str) -> bool:
    """role 是否为「学生/退休/无业」等非在职语义（复合岗位名如「学生公寓管理员」不算）。"""
    role = str(role or "")
    for word in _NON_WORKING_ROLE_WORDS:
        idx = role.find(word)
        while idx >= 0:
            tail = role[idx + len(word):]
            if not any(tail.startswith(t) for t in _ROLE_COMPOUND_TAIL):
                return True
            idx = role.find(word, idx + 1)
    return False


def _occupation_hits(text: str, limit: int = 2) -> List[str]:
    """词表职业命中（最长优先，剔除已含子串），最多 ``limit`` 个。"""
    out: List[str] = []
    for occ in sorted(_OCCUPATIONS, key=len, reverse=True):
        if occ in text and not any(occ in kept for kept in out):
            out.append(occ)
        if len(out) >= limit:
            break
    return out


def _school_name(text: str) -> str:
    """「都灵大学」式学校名（剥掉「考入/在/于…」等动词前缀，词干 <2 字放弃）。"""
    m = _SCHOOL_RE.search(text)
    if not m:
        return ""
    stem = m.group(1)
    while stem and stem[0] in _SCHOOL_PREFIX_TRIM:
        stem = stem[1:]
    if len(stem) < 2:
        return ""
    return stem + m.group(2)


# 人名槽 = 「你<亲属>叫什么名字？」（「你读的大学叫什么名字？」不是人名槽，别串）
_NAME_SLOT_MARKS = tuple(f"你{rel}叫什么名字" for rel in _RELATIONS)
# 只对「他是做什么的」这种**亲属职业槽**要求词表职业：role 题问的是档案自己的
# role 原文（可能只有地名/公司名），不该被词表卡掉。
_OCC_SLOT_MARKS = ("是做什么的",)
_YEAR_SLOT_MARK = "哪一年"


def _item_slot_ok(item: Optional[Dict[str, Any]]) -> bool:
    """题干槽位 × expect 语义一致性硬校验（不匹配 → 整题丢弃，绝不硬出）。

    真机事故回归（2026-07-28）：「你儿子叫什么名字？」expect=「带孙子」——AI 答对
    反判 0 分。**假阴性比少一道题贵得多**，所以这里只放行「问什么答什么」的题：
    名字槽 → expect 首项必须像人名；职业槽 → 必须是词表职业；年份槽 → 必须是
    通过年份闸门的 4 位数。另加通用不变量：expect 不得泄漏在题干里。
    """
    if not isinstance(item, dict):
        return False
    q = str(item.get("q") or "").strip()
    expect = [str(k).strip() for k in (item.get("expect") or []) if str(k).strip()]
    if not q or not expect:
        return False
    if any(kw in q for kw in expect):
        return False                       # 题干泄漏答案
    name_slot = any(mark in q for mark in _NAME_SLOT_MARKS)
    if name_slot and not _looks_like_person_name(expect[0]):
        return False
    if any(mark in q for mark in _OCC_SLOT_MARKS):
        occ_kws = expect[1:] if name_slot else expect
        if not any(kw in _OCCUPATIONS for kw in occ_kws):
            return False
    if _YEAR_SLOT_MARK in q:
        if not (len(expect) == 1 and expect[0].isdigit() and len(expect[0]) == 4):
            return False
        if not YEAR_MIN <= int(expect[0]) <= datetime.now().year + 1:
            return False
    return True


def _relation_question(text: str, src: str) -> Optional[Dict[str, Any]]:
    """亲属事实挖空：名字（拉丁/CJK 邻接）+ 职业（词表，且位于亲属词之后、中间无
    其他亲属词——防「母亲…父亲…」串人）。三种模板：名字+职业 / 仅名字 / 仅职业。"""
    best: Optional[tuple] = None
    for rel in _RELATIONS:
        idx = text.find(rel)
        if idx >= 0 and (best is None or idx < best[0]):
            best = (idx, rel)
    if best is None:
        return None
    idx, rel = best
    pron = _RELATIONS[rel]
    pos = idx + len(rel)
    name = _adjacent_latin_token(text, pos) or _adjacent_cjk_name(text, pos)
    if name and not _looks_like_person_name(name):
        name = ""      # 抽出来的不像名字（「带孙子」）→ 退化成职业题或不出题
    occ = ""
    for cand in _occupation_hits(text, limit=3):
        p = text.find(cand, pos)
        if p < 0:
            continue
        between = text[pos:p]
        if any(r in between for r in _RELATIONS if r != rel):
            continue  # 职业属于句中另一位亲属，宁缺勿错
        occ = cand
        break
    if name and occ:
        return {"q": f"你{rel}叫什么名字？{pron}是做什么的？",
                "expect": [name, occ], "src": src}
    if name:
        return {"q": f"你{rel}叫什么名字？", "expect": [name], "src": src}
    if occ:
        return {"q": f"你{rel}是做什么的？", "expect": [occ], "src": src}
    return None


def _question_from_text(text: str, src: str,
                        field: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """单条记忆/单句 → 至多一道题（出口过 ``_item_slot_ok`` 硬校验，不合格丢弃）。

    优先级：亲属 > 出生地（拉丁地名）> 学校 > 年份。``field`` 缺省按 ``src`` 取
    对应字段点路径（出题范围校验用）。
    """
    item = _question_from_text_raw(text, src)
    if not _item_slot_ok(item):
        return None
    item["field"] = field or _SRC_DEFAULT_FIELD.get(src, src)
    return item


def _question_from_text_raw(text: str, src: str) -> Optional[Dict[str, Any]]:
    text = str(text or "").strip()
    if not text:
        return None

    item = _relation_question(text, src)
    if item:
        return item

    # 亲属守卫（真机事故回归：「2004年姐姐考入米兰理工大学」曾被出成第一人称
    # 「你读的大学叫什么名字？」——事实属于亲属，第一人称模板必须跳过；
    # 年份模板不受限：它逐字引用原句，归属随引文保留）。
    has_kin = any(k in text for k in _RELATIONS)

    if "出生" in text and not has_kin:
        toks = _latin_tokens(text, limit=2)
        if toks:
            return {"q": "你是在哪里出生的？", "expect": toks, "src": src}

    if not has_kin:
        school = _school_name(text)
        if school:
            q = ("你读的大学叫什么名字？" if school.endswith("大学")
                 else "你是在哪所学校念的书？")
            return {"q": q, "expect": [school], "src": src}

    spans = _year_spans(text)
    if spans:
        year = spans[0][2]
        snippet = _mask_year_spans(text, spans)
        if len(snippet) > 36:
            snippet = snippet[:36] + "…"
        return {"q": f"你提到过「{snippet}」，这件事发生在哪一年？",
                "expect": [year], "src": src}
    return None


# ── K2 扩题源：K1 刚接进 prompt 的字段 → 可确定性验证的题 ────────────────────
#
# 取舍原则不变（宁缺勿滥，一道假阴性坏题的代价 >> 少一道好题），所以：
# - ``gender``：不出题。「你是男的女的」AI 必对，零鉴别力，纯稀释分母。
# - ``speaking.emoji_level``：不出题。它是**风格旋钮**不是事实，问「你爱用表情吗」
#   答什么都对。
# - ``boundaries.topics_to_avoid`` / ``identity.*``：不出题。考它等于诱导 AI 说出
#   边界内容（「你不聊什么？」→ 逼它复述政治/成人清单）或自曝身份判定逻辑。
# - ``context.schedule``：不出题。真档案里是「约 17:00–05:00 上班」这类时刻表，
#   而 AI 会自然答「下午五点上班」——判分器只做归一后子串匹配（刻意不做同义/模糊），
#   「17:00」永远命中不了「五点」＝制造假阴性；「7」这种松散 expect 又会假阳。
# - ``appearance``：只出**跨语言稳定**的特征（CJK 发色词 / 身高数字）。本机人设的
#   appearance 全是英文生图锚点（"long straight dark brown hair"），中文提问 + 英文
#   expect 必然假阴性，故这些人设不出外貌题（见 ``_appearance_items``）。

# 食物/饮品尾词白名单：命中「条目以食物名词收尾」才算吃喝（白名单几乎零假阳，
# 漏掉几种食物只是少一道题）。刻意不用「含 X 字」宽口径——「钓鱼」含鱼、
# 「肉麻的情话」含肉，会把爱好/性格条目误当食物。
_FOOD_TAILS = (
    "菜", "茶", "咖啡", "酒", "红酒", "白酒", "啤酒", "清酒", "米酒", "黄酒",
    "威士忌", "奶茶", "汤", "粥", "面", "粉", "饭", "饼", "糕", "包子",
    "饺子", "馄饨", "火锅", "烧烤", "关东煮", "寿司", "刺身", "拉面", "串",
    "甜点", "甜品", "零食", "夜宵", "早餐", "蛋糕", "巧克力", "冰淇淋", "布丁",
    "奶", "牛奶", "豆浆", "果汁", "可乐", "汽水", "水果", "芒果", "草莓",
    "西瓜", "榴莲", "柠檬", "牛肉", "猪肉", "羊肉", "鸡肉", "海鲜", "生蚝",
    "内脏", "豆腐", "鸡蛋", "披萨", "汉堡", "薯条", "泡面", "米线", "螺蛳粉",
    "麻辣烫", "苦瓜", "茄子",
)
_FOOD_TAILS_SORTED = tuple(sorted(_FOOD_TAILS, key=len, reverse=True))
# 「钓鱼」「养花」「做饭」是活动不是食物——动词开头一律否掉（常见菜名没有以这些
# 字开头的；「关东煮」的煮在词尾不受影响）。
_ACTIVITY_VERB_HEADS = "钓养种喂抓捕摸逛卖做煮买"

# 专业：`X系`（需学业语境）/ `主修X` / `专业是X`
_MAJOR_DEPT_RE = re.compile(r"([\u4e00-\u9fff]{2,3})系(?![统列数谱主领])")
_MAJOR_MAJOR_RE = re.compile(r"(?:主修|专业是|专业为|专业：)([\u4e00-\u9fff]{2,8})")
_MAJOR_CONTEXT = ("在读", "专业", "本科", "研究生", "大一", "大二", "大三", "大四",
                  "毕业", "主修", "念书", "读书", "考入", "学院", "大学")
_MAJOR_STEM_STOP = set("关联体没这那每各我你他她的了在是和与及并很太就都还")
_MAJOR_SPLIT_RE = re.compile(r"[与和、，,／/及]")

# 教龄 / 从业年数（阿拉伯数字；中文数字「做过四年」不抽，宁缺勿错）
_TEACH_YEARS_RE = re.compile(r"(?:执教|从教|任教|教书|教了)\D{0,4}?(\d{1,2})\s*年")
_CAREER_YEARS_RE = re.compile(r"(\d{1,2})\s*年[^，。；\n]{0,6}?(?:经验|从业)")
_YEARS_MAX = 60

# 任教科目（须与「教/老师」贴身，防「历史悠久」这类误命中）
_SUBJECTS = ("数学", "语文", "英语", "物理", "化学", "生物", "历史", "地理",
             "政治", "体育", "音乐", "美术", "信息技术")
_SUBJECT_TAIL_RE = re.compile("(" + "|".join(_SUBJECTS) + ")(?:老师|课|教研|教学)")
_SUBJECT_LEAD_RE = re.compile("(?:执教|任教|从教|教书|教)[^，。；\n]{0,8}?("
                              + "|".join(_SUBJECTS) + ")")

# 外貌：只取跨语言稳定的特征
_HAIR_COLOR_RE = re.compile(
    r"([\u4e00-\u9fff]{1,3}色)(?:的)?(?:及肩|齐肩|中长|长|短|卷|直|大波浪)*发")
_HEIGHT_RE = re.compile(r"(?<!\d)(1[3-9]\d|2[0-2]\d)\s*(?:cm|CM|厘米|公分)")

# context.family：只取**称谓无歧义**的键。brother/sister（哥哥还是弟弟？）、
# grandpa（爷爷还是外公？）一律跳过——问错称谓 AI 会说「我没有哥哥」＝假阴性。
_FAMILY_UNAMBIGUOUS = {
    "father": "父亲", "mother": "母亲", "son": "儿子", "daughter": "女儿",
    "husband": "丈夫", "wife": "妻子",
}
_PAREN_LATIN_RE = re.compile(
    r"[（(]\s*([A-Za-zÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]*(?:\s+[A-Za-zÀ-ÖØ-Þ]"
    r"[A-Za-zÀ-ÖØ-öø-ÿ'’\-]*)*)\s*[）)]")


def _dict_of(node: Any) -> Dict[str, Any]:
    return node if isinstance(node, dict) else {}


def _food_noun(entry: Any) -> str:
    """likes/dislikes 单条 → 食物/饮品名词（不是吃喝返回 ""）。

    两步收窄，目标是 expect **既具体又不带修饰语**（带修饰语＝AI 换个说法就假阴性）：
    ① 剥定语：「深夜便利店的关东煮」→「关东煮」；
    ② 取白名单尾词：尾词 ≥2 字直接用（「湖南口味的便宜泡面」→「泡面」、
       「布根地红酒」→「红酒」）；尾词只有 1 字（茶/菜/饼…）太松，改用整条，
       且整条须 ≤4 字——「狗庄画大饼」这种以食物字收尾的比喻会被这一条挡掉。
    """
    s = str(entry or "").strip()
    if "的" in s:
        s = s.rsplit("的", 1)[1].strip()
    if not 2 <= len(s) <= 8:
        return ""
    if not _CJK_CHAR_RE.match(s[0]) or s[0] in _ACTIVITY_VERB_HEADS:
        return ""
    tail = next((t for t in _FOOD_TAILS_SORTED if s.endswith(t)), "")
    if not tail:
        return ""
    if len(tail) >= 2:
        return tail
    return s if len(s) <= 4 else ""


def _food_nouns(entries: Any, limit: int = 3) -> List[str]:
    out: List[str] = []
    if isinstance(entries, (list, tuple)):
        for entry in entries:
            noun = _food_noun(entry)
            if noun and noun not in out:
                out.append(noun)
            if len(out) >= limit:
                break
    return out


def _major_name(text: str) -> str:
    """句子 → 专业名（「日语系大三在读」→ 日语；「主修酒店管理与旅游」→ 酒店管理）。"""
    text = str(text or "")
    m = _MAJOR_MAJOR_RE.search(text)
    if m:
        stem = _MAJOR_SPLIT_RE.split(m.group(1))[0].strip()
        if 2 <= len(stem) <= 8 and stem[0] not in _MAJOR_STEM_STOP:
            return stem
    if not any(w in text for w in _MAJOR_CONTEXT):
        return ""                      # 「没关系」「这一系列」缺学业语境 → 不是专业
    m = _MAJOR_DEPT_RE.search(text)
    if m:
        stem = m.group(1)
        if stem[0] not in _MAJOR_STEM_STOP:
            return stem
    return ""


def _years_item(text: str, field: str, src: str) -> Optional[Dict[str, Any]]:
    """句子 → 教龄 / 从业年数题（``expect`` 是阿拉伯数字，判分器认「三十年」）。"""
    text = str(text or "")
    m = _TEACH_YEARS_RE.search(text)
    if m and 1 <= int(m.group(1)) <= _YEARS_MAX:
        return {"q": "你教了多少年书？", "expect": [m.group(1)],
                "src": src, "field": field}
    m = _CAREER_YEARS_RE.search(text)
    if m and 1 <= int(m.group(1)) <= _YEARS_MAX:
        return {"q": "你在这一行做了多少年了？", "expect": [m.group(1)],
                "src": src, "field": field}
    return None


def _subject_name(text: str) -> str:
    """句子 → 任教科目（「退休高中数学老师」/「执教 30 年高中数学」→ 数学）。"""
    text = str(text or "")
    m = _SUBJECT_TAIL_RE.search(text) or _SUBJECT_LEAD_RE.search(text)
    return m.group(1) if m else ""


def _role_items(persona: dict) -> List[Dict[str, Any]]:
    """role → 工作 / 退休前职业 / 任教科目（字段路径 ``role``）。

    非在职身份（学生/退休/无业…）不问「你现在在哪里工作」——对学生本身就是坏题
    （真机事故：lin_xiaoyu 答「在便利店打工」被判 0 分）。退休身份改问「退休前是
    做什么的」：这是同一事实的**正确问法**，不是放宽守卫。
    """
    role = str(persona.get("role") or "").strip()
    if not role:
        return []
    out: List[Dict[str, Any]] = []
    subject = _subject_name(role)
    if _role_is_non_working(role):
        school = _school_name(role)
        if school:
            out.append({"q": "你在哪所学校念书？", "expect": [school],
                        "src": "role", "field": "role"})
        elif any(w in role for w in ("退休", "离休")):
            # expect 取 role 原文里的职业词 + 科目：「你退休前是做什么的？」
            # 答「以前教高中数学」和答「我是数学老师」都该算对。
            expect = _occupation_hits(role, limit=2)
            if expect:
                if subject and subject not in expect:
                    expect.append(subject)
                out.append({"q": "你退休前是做什么的？", "expect": expect[:3],
                            "src": "role", "field": "role"})
    else:
        expect = (_latin_tokens(role, limit=1) + _occupation_hits(role, limit=2))[:3]
        if expect:
            out.append({"q": "你现在在哪里工作？做什么职位？",
                        "expect": expect, "src": "role", "field": "role"})
    if subject:
        out.append({"q": "你教的是什么科目？", "expect": [subject],
                    "src": "role", "field": "role"})
    return out


def _age_items(persona: dict) -> List[Dict[str, Any]]:
    age = persona.get("age")
    if isinstance(age, bool):
        return []
    if isinstance(age, str) and age.strip().isdigit():
        age = int(age.strip())
    if isinstance(age, (int, float)) and 1 <= int(age) <= 120:
        return [{"q": "你今年多大？", "expect": [str(int(age))],
                 "src": "age", "field": "age"}]
    return []


def _english_name_items(persona: dict) -> List[Dict[str, Any]]:
    """names.english → 「你的英文名叫什么？」（高价值：人设常有英文名，答错=穿帮）。"""
    eng = str(_dict_of(persona.get("names")).get("english") or "").strip()
    if not eng or not _looks_like_person_name(eng):
        return []
    return [{"q": "你的英文名叫什么？", "expect": [eng],
             "src": "names", "field": "names.english"}]


def _nickname_items(persona: dict) -> List[Dict[str, Any]]:
    """names.nickname → 「别人一般怎么称呼你？」（称呼答错＝当场穿帮，跨措辞稳定）。

    只出**专名形**昵称：CJK 2-4 字或拉丁人名形。刻意排除
      ① 与 ``name`` 相同（无信息量，且题干可能泄漏）；
      ② 带描述性助词的短语（「小名叫…」这类抽取残渣）；
      ③ 单字（「默」这种子串遍地命中，判分必假阳）。
    """
    names = _dict_of(persona.get("names"))
    nick = str(names.get("nickname") or "").strip()
    if not nick:
        return []
    full = str(persona.get("name") or "").strip()
    if nick == full:
        return []
    has_cjk = bool(_CJK_CHAR_RE.search(nick))
    latin_ok = not has_cjk and _looks_like_person_name(nick)
    cjk_ok = has_cjk and 2 <= len(nick) <= 4 \
        and all(_CJK_CHAR_RE.match(ch) for ch in nick) \
        and not any(ch in _NAME_FUNC_CHARS for ch in nick)
    if not (latin_ok or cjk_ok):
        return []
    return [{"q": "别人一般怎么称呼你？", "expect": [nick],
             "src": "names", "field": "names.nickname"}]


def _appearance_items(persona: dict) -> List[Dict[str, Any]]:
    """appearance → 发色 / 身高（只取跨语言稳定的特征，见本节顶部取舍说明）。"""
    text = str(persona.get("appearance") or "").strip()
    if not text:
        return []
    out: List[Dict[str, Any]] = []
    m = _HAIR_COLOR_RE.search(text)
    if m:
        color = m.group(1)
        # 「深棕色」取基础色「棕色」：子串匹配下短形更宽（答「深棕色」同样命中）
        out.append({"q": "你头发是什么颜色的？", "expect": [color[-2:]],
                    "src": "appearance", "field": "appearance"})
    m = _HEIGHT_RE.search(text)
    if m:
        out.append({"q": "你有多高？", "expect": [m.group(1)],
                    "src": "appearance", "field": "appearance"})
    return out


def _taste_items(persona: dict) -> List[Dict[str, Any]]:
    """tastes.likes/dislikes（+ context.hobbies 的酒类）→ 可逐字验证的吃喝题。

    开放题「你平时喜欢做什么？」经两轮真机证伪后已砍（AI 换措辞聊同样真实的爱好，
    子串判分必然误伤）。这里只出**具体物品名**：食物/饮品名词跨措辞稳定，且 expect
    收全所有命中项（任一命中即 pass）。
    """
    tastes = _dict_of(persona.get("tastes"))
    ctx = _dict_of(persona.get("context"))
    out: List[Dict[str, Any]] = []

    # 带拉丁品牌词的酒类（「Barolo葡萄酒」→ 问「你喜欢喝什么酒？」expect ["Barolo"]）
    hobby_words: List[str] = []
    for src_list in (tastes.get("likes"), ctx.get("hobbies")):
        if isinstance(src_list, (list, tuple)):
            for it in src_list:
                s = str(it or "").strip()
                if s and s not in hobby_words:
                    hobby_words.append(s)
    for word in hobby_words:           # 档案原序 → 确定性
        if "酒" in word:
            toks = _latin_tokens(word, limit=1)
            if toks:
                out.append({"q": "你喜欢喝什么酒？", "expect": toks,
                            "src": "tastes", "field": "tastes.likes"})
                break

    likes = _food_nouns(tastes.get("likes"))
    if likes:
        out.append({"q": "你平时爱吃什么、爱喝什么？", "expect": likes,
                    "src": "tastes", "field": "tastes.likes"})
    dislikes = _food_nouns(tastes.get("dislikes"))
    if dislikes:
        out.append({"q": "有什么东西是你特别不爱吃的？", "expect": dislikes,
                    "src": "tastes", "field": "tastes.dislikes"})
    return out


def _family_items(persona: dict) -> List[Dict[str, Any]]:
    """context.family → 亲属名字/职业题（复用 ``_relation_question`` 同款抽取+守卫）。

    称谓有歧义的键（brother=哥哥还是弟弟）一律跳过。档案里「林志强（Michael Lin）」
    这类中英双名，两个名字都进 expect——AI 答哪个都算对。
    """
    family = _dict_of(_dict_of(persona.get("context")).get("family"))
    out: List[Dict[str, Any]] = []
    for key, rel in _FAMILY_UNAMBIGUOUS.items():
        value = str(family.get(key) or "").strip()
        if not value:
            continue
        item = _relation_question(rel + value, "family")
        if not item:
            continue
        item["field"] = "context.family"
        m = _PAREN_LATIN_RE.search(value)
        if m and len(item["expect"]) < 3:
            alias = m.group(1).split()[0].strip("'’-")
            if alias not in item["expect"] and _looks_like_person_name(alias):
                item["expect"].insert(1, alias)
        if _item_slot_ok(item):
            out.append(item)
    return out


def _culture_items(persona: dict) -> List[Dict[str, Any]]:
    """context.filipino_connection → 语言 / 拿手菜（只取拉丁专名，跨措辞稳定）。"""
    node = _dict_of(_dict_of(persona.get("context")).get("filipino_connection"))
    if not node:
        return []
    out: List[Dict[str, Any]] = []
    field = "context.filipino_connection"
    langs = _latin_tokens(str(node.get("language") or ""), limit=1)
    if langs:
        out.append({"q": "除了中文，你还会说什么语言？", "expect": langs,
                    "src": "culture", "field": field})
    foods = _latin_tokens(str(node.get("food") or ""), limit=2)
    if foods:
        out.append({"q": "你家最拿手的家常菜是什么？", "expect": foods,
                    "src": "culture", "field": field})
    return out


def _background_fact_items(persona: dict) -> List[Dict[str, Any]]:
    """background 逐句 → 专业 / 教龄 / 科目（各至多一题）。

    逐句 + 亲属守卫：含亲属词的句子跳过，防「姐姐主修法律」被出成第一人称题
    （与 ``_question_from_text_raw`` 的 has_kin 守卫同口径）。
    """
    bg = str(persona.get("background") or "")
    if not bg:
        return []
    out: List[Dict[str, Any]] = []
    seen_kinds: set = set()
    for sent in re.split(r"[。！？!?；;\n]", bg):
        sent = sent.strip()
        if not sent or any(k in sent for k in _RELATIONS):
            continue
        major = _major_name(sent)
        if major and "major" not in seen_kinds:
            seen_kinds.add("major")
            out.append({"q": "你读的是什么专业？", "expect": [major],
                        "src": "background", "field": "background"})
        years = _years_item(sent, "background", "background")
        if years and "years" not in seen_kinds:
            seen_kinds.add("years")
            out.append(years)
        subject = _subject_name(sent)
        if subject and "subject" not in seen_kinds:
            seen_kinds.add("subject")
            out.append({"q": "你教的是什么科目？", "expect": [subject],
                        "src": "background", "field": "background"})
    return out


# ── 出题 ─────────────────────────────────────────────────────────────────────

def build_quiz(persona: dict, n: int = 10, seed: Any = None) -> List[Dict[str, Any]]:
    """从人设档案确定性出题（同档案同题目，可复现）。

    每题 ``{"q": str, "expect": [1-3 个档案原文关键词，命中任一即 pass],
    "src": "memory|background|tastes|role|age|names|appearance|family|culture",
    "field": "题目所依据的字段点路径"}``。素材不足时能出几题出几题；
    可出题 < ``MIN_QUIZ_ITEMS``（3）→ 返回 []（资料太薄不值得考）。

    ``field`` 必须落在 ``persona_manager.PROMPT_CONSUMED_FIELDS`` 内（子树登记按
    祖先前缀命中），否则整题丢弃——没进 prompt 的字段考了必是假阴性。注册表不可用
    时降级为不校验。
    """
    if not isinstance(persona, dict) or not persona:
        return []
    try:
        n = int(n)
    except Exception:
        n = 10
    n = max(1, n)
    if seed is None:
        seed = zlib.crc32(
            str(persona.get("name") or persona.get("id") or "persona").encode("utf-8"))
    rng = random.Random(seed)

    ctx = _dict_of(persona.get("context"))

    # 固定槽：档案结构化字段 → 题（每个生成器自带守卫，出口统一过槽位校验）
    fixed: List[Dict[str, Any]] = []
    for gen in (_role_items, _age_items, _english_name_items, _nickname_items,
                _appearance_items, _taste_items, _family_items, _culture_items,
                _background_fact_items):
        try:
            fixed.extend(gen(persona) or [])
        except Exception:      # 单个题源出错不该让整张考卷出不来
            logger.warning("[persona_quiz] 题源 %s 异常，跳过", gen.__name__,
                           exc_info=True)

    seen_q: set = set()
    deduped: List[Dict[str, Any]] = []
    for it in fixed:
        if _item_slot_ok(it) and it["q"] not in seen_q:
            seen_q.add(it["q"])
            deduped.append(it)
    fixed = deduped

    # specific_memories：每条记忆至多一题（实体挖空）
    mem_qs: List[Dict[str, Any]] = []
    mems = ctx.get("specific_memories")
    if isinstance(mems, (list, tuple)):
        for mem in mems:
            item = _question_from_text(str(mem or ""), "memory")
            if item and item["q"] not in seen_q:
                seen_q.add(item["q"])
                mem_qs.append(item)

    # background：切句后同法抽 1-2 题
    bg_qs: List[Dict[str, Any]] = []
    bg = str(persona.get("background") or "")
    if bg:
        for sent in re.split(r"[。！？!?；;\n]", bg):
            if len(bg_qs) >= 2:
                break
            item = _question_from_text(sent.strip(), "background")
            if item and item["q"] not in seen_q:
                seen_q.add(item["q"])
                bg_qs.append(item)

    # 出题范围 ⊆ prompt 消费字段（K1 注册表；不可用则降级不校验）
    allowed = _consumed_fields()
    fixed, _dropped_fixed = _split_by_consumed(fixed, allowed)
    variable, _dropped_var = _split_by_consumed(mem_qs + bg_qs, allowed)

    if len(fixed) + len(variable) < MIN_QUIZ_ITEMS:
        return []
    rng.shuffle(variable)  # seed 固定 → 同档案同题目；不同 seed → 抽不同记忆子集
    room = max(0, n - len(fixed))
    return (fixed + variable[:room])[:n]


# ── 判分 ─────────────────────────────────────────────────────────────────────

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "两": 2,
    "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "拾": 10, "佰": 100}
_CN_ALL_CHARS = "".join(sorted(set(_CN_DIGITS) | set(_CN_UNITS)))
_CN_RUN_RE = re.compile("[" + _CN_ALL_CHARS + "]{2,}")
_CN_MAX = 999          # 覆盖 0-999（年龄/年份位串/数量都在这个量级）

# ── 单字中文数字 + 量词（K2b，2026-07-28 真机假阴性）─────────────────────────
# 事故：陈美玲「你在这一行做了多少年了？」expect ["10"]，AI 答「做了十年品牌营销」
# 被判错。上一轮刻意不转单字是对的（裸「一/十/两」绝大多数是副词，expect「1」会
# 误命中「一直」），但**紧跟量词时歧义消失**——「十年」「五岁」「两口人」只可能是
# 数量。所以放开这一档，且只放开这一档。
#
# 量词表口径＝「前面出现中文数字时必然是计数」的词；刻意不收：
#   分（十分＝很）、下（两下）、点（有一点累）、块（一块儿去）、千/百
#   （「一千」转成「1千」既不像原文也匹配不上「1000」，纯属自找麻烦）。
# 另有两道闸：① 前一个字是「第」→ 序数（第一次/第三个）不转；
#             ② 前一个字仍是中文数字 → 说明这是一段没能整体解析的长串，别切一半。
_CN_SINGLE_SOURCES = "".join(sorted(set(_CN_DIGITS) | {"十", "拾"}))
_MEASURE_WORDS = (
    "年级", "小时", "分钟", "公里", "公斤", "千克", "厘米", "星期",
    "年", "岁", "个", "次", "人", "口", "月", "天", "日", "周", "秒",
    "米", "斤", "吨", "元", "万", "家", "间", "层", "台", "本", "杯",
    "张", "只", "件", "份", "条", "位", "名", "部", "首", "页",
)
_CN_SINGLE_UNIT_RE = re.compile(
    "(?<![第" + _CN_ALL_CHARS + "])"
    "([" + _CN_SINGLE_SOURCES + "])"
    "(?=(?:" + "|".join(sorted(_MEASURE_WORDS, key=len, reverse=True)) + "))")


def _cn_run_to_text(run: str) -> Optional[str]:
    """中文数字串 → 阿拉伯串；不可靠/超量级返回 None（保持原文，绝不硬转）。"""
    if any(c in _CN_UNITS for c in run):        # 结构式：五十八 / 十八 / 一百零五
        total = cur = 0
        for ch in run:
            if ch in _CN_DIGITS:
                cur = _CN_DIGITS[ch]
            else:
                total += (cur or 1) * _CN_UNITS[ch]
                cur = 0
        total += cur
        return str(total) if 0 <= total <= _CN_MAX else None
    if all(c in _CN_DIGITS for c in run):       # 位串式：二零一五 → 2015、一八 → 18
        return "".join(str(_CN_DIGITS[c]) for c in run)
    return None


def normalize_for_match(text: str) -> str:
    """判分前的归一化（纯函数）：NFKC（全角→半角）→ 中文数字→阿拉伯 → 空白折叠 → casefold。

    **防过度归一**（假阳性同样致命）：
    - 中文数字转两档：**长度 ≥2 的连续串**（「五十八」→58、「二零一五」→2015），
      以及**单字 + 量词**（「十年」→10年、「五岁」→5岁、「两口人」→2口人）。
      单字**裸**出现一律不转——「一直」「十分」「两下」里的是副词，转了会让
      expect「1」误命中「一直」；序数「第一次」同样不转（见 ``_CN_SINGLE_UNIT_RE``）；
    - 只做双向映射后的**子串**匹配，不做同义词/模糊/编辑距离；
    - 结构式解析超出 0..999 或串里混了非数字字符 → 原样保留，不猜。
    """
    s = unicodedata.normalize("NFKC", str(text or ""))

    def _sub_run(m: "re.Match") -> str:
        return _cn_run_to_text(m.group(0)) or m.group(0)

    def _sub_single(m: "re.Match") -> str:
        ch = m.group(1)
        val = _CN_DIGITS[ch] if ch in _CN_DIGITS else _CN_UNITS.get(ch)
        return str(val) if val is not None else ch

    s = _CN_RUN_RE.sub(_sub_run, s)
    s = _CN_SINGLE_UNIT_RE.sub(_sub_single, s)
    return re.sub(r"\s+", " ", s).strip().casefold()


# ── expect 词干派生（K2b，2026-07-28 真机假阴性）─────────────────────────────
# 事故：苏婉「你现在在哪里工作？做什么职位？」expect ["咖啡店主","创业者"]，AI 答
# 「自己在宿务开了间小咖啡店…老板兼打杂的」——语义百分百正确，纯粹字面对不上。
# 解法是**受控的词干剥离**：档案写「咖啡店主」，答「咖啡店」就该算对。
#
# 边界（很重要，别把工具变成橡皮尺——放水一次，告警就永远没人信了）：
#   - 只剥**一层**身份/职业后缀，不递归；
#   - 剥完中文 <2 字 / 拉丁 <3 字一律丢弃（「主」「员」这种词干谁都命中）；
#   - 拉丁只按空格/连字符拆实词，停用词不派生；
#   - **不做同义词、不做模糊匹配、不做编辑距离**——那是裁判兜底（``judge_fn``）的活；
#   - 每个 keyword 至多 3 个变体。
# 校准锚点：「顾问」的「问」不在后缀表 → 无变体；「CFA」拆不出别的实词 → 无变体，
# 故 Marcus Wei 的人设漂移（答「精品 PE 合伙人」）仍判错——真阳性不许被派生放行。
_STEM_SUFFIXES = ("主", "者", "员", "师", "家", "长", "工", "人")
_LATIN_STOPWORDS = frozenset({
    "the", "of", "and", "a", "an", "for", "in", "on", "at", "to", "with",
    "or", "de", "la", "le", "el",
})
MAX_EXPECT_VARIANTS = 3       # 每个 keyword 的派生上限
MIN_CJK_VARIANT_LEN = 2       # 中文词干最短长度（「主」「员」一律丢弃）
MIN_LATIN_VARIANT_LEN = 3     # 拉丁词干最短长度


def expect_variants(keyword: str) -> List[str]:
    """期望关键词 → **受控**的可接受变体（不含原词；判分时任一命中即算对）。

    中文：剥一层身份/职业后缀（``咖啡店主 → 咖啡店``、``创业者 → 创业``、
    ``程序员 → 程序``），剥完 <2 字丢弃（``教师 → 教`` 不要）。
    拉丁：按空格/连字符拆实词（``data analyst → data, analyst``），停用词与
    <3 字的碎片丢弃，与原词相同的单 token（``CFA``）不算变体。
    纯数字/短词/无后缀词（``顾问``、``10``、``165``）→ ``[]``。
    """
    kw = str(keyword or "").strip()
    if not kw:
        return []
    out: List[str] = []

    def _add(cand: str) -> None:
        cand = cand.strip()
        if not cand or len(out) >= MAX_EXPECT_VARIANTS:
            return
        min_len = (MIN_CJK_VARIANT_LEN if _CJK_CHAR_RE.search(cand)
                   else MIN_LATIN_VARIANT_LEN)
        if len(cand) < min_len:
            return
        if cand.casefold() == kw.casefold() or cand in out:
            return
        out.append(cand)

    if _CJK_CHAR_RE.search(kw):
        for suf in _STEM_SUFFIXES:                 # 只剥一层，不递归
            if kw.endswith(suf) and len(kw) - len(suf) >= MIN_CJK_VARIANT_LEN:
                _add(kw[:-len(suf)])
                break
        return out

    for tok in re.split(r"[\s\-_/]+", kw):
        if tok.casefold() in _LATIN_STOPWORDS:
            continue
        _add(tok)
    return out


def _keyword_hit(answer: str, kw: str) -> bool:
    """归一化（全/半角、中文数字、空白、大小写）后双侧子串命中。

    原词与 ``expect_variants`` 派生的词干都过同一套归一化，任一命中即算对。
    """
    norm_ans = normalize_for_match(answer)
    if not norm_ans:
        return False
    for cand in [kw] + expect_variants(kw):
        norm_kw = normalize_for_match(cand)
        if norm_kw and norm_kw in norm_ans:
            return True
    return False


# ── LLM 裁判兜底（K2b，默认关；开关 ``personas.quiz.llm_judge`` 由调用方读）──
#
# 词表打补丁（上面两条）永远追不上真人语言。结构性更优的解法：**关键词判分只用于
# 「判对」，判错的题再交 LLM 复核一次**——成本只与错题数成正比（夜间通常个位数）。
#
# 铁律 fail-closed：任何非 YES 的输出（NO / 空 / 异常 / 超时 / 中文 / 胡言）
# **一律维持原判为错**。判分工具一旦被裁判放宽成橡皮尺，运营就再也不信告警了。
QUIZ_JUDGE_TIMEOUT_SEC = 30.0     # 裁判是一个词的判断题，不需要 60s

QUIZ_JUDGE_SYSTEM = (
    "你是严格的事实核对员。只判断「AI 的回答里是否包含或等价于给定的事实」。\n"
    "措辞不同但指同一件事 → YES；事实不同、答非所问、含糊回避、根本没提到 → NO。\n"
    "拿不准就判 NO。只输出 YES 或 NO 一个词，不要解释、不要标点。"
)


def build_judge_prompt(question: str, expect: List[str],
                       answer: str) -> tuple:
    """裁判的 ``(system, user)``（纯函数，便于门禁逐字校验措辞）。"""
    kws = "、".join(str(k) for k in (expect or []) if str(k).strip())
    user = (f"问题：{question}\n"
            f"期望事实（关键词）：{kws}\n"
            f"AI 的回答：{answer}\n\n"
            "回答里是否包含或等价于该事实？只回 YES 或 NO。")
    return QUIZ_JUDGE_SYSTEM, user


def _judge_verdict_yes(raw: Any) -> bool:
    """裁判输出 → 是否放行（**只有**首个英文词是 YES 才放行，其余一律否）。"""
    m = re.match(r"[^A-Za-z]*([A-Za-z]+)", str(raw or "").strip())
    return bool(m) and m.group(1).upper() == "YES"


def _judge_pass(judge_fn: Callable[[str, str, float], str], question: str,
                expect: List[str], answer: str) -> bool:
    """调裁判复核一道错题；任何异常/超时 → False（fail-closed）。"""
    system, user = build_judge_prompt(question, expect, answer)
    try:
        raw = judge_fn(system, user, QUIZ_JUDGE_TIMEOUT_SEC)
    except Exception as exc:      # noqa: BLE001 — 裁判挂了只维持原判，绝不放行
        logger.warning("persona quiz 裁判调用失败（维持原判为错）: %s", exc)
        return False
    return _judge_verdict_yes(raw)


def llm_judge_enabled(config: Optional[dict]) -> bool:
    """``personas.quiz.llm_judge`` 开关（缺省 False）——只读，调用方据此注入 judge_fn。"""
    quiz = ((config or {}).get("personas") or {}).get("quiz")
    if not isinstance(quiz, dict):
        return False
    return bool(quiz.get("llm_judge", False))


def score_answers(quiz: List[Dict[str, Any]], answers: List[str],
                  judge_fn: Optional[Callable[[str, str, float], str]] = None
                  ) -> Dict[str, Any]:
    """逐题判分：answer 含任一 expect 关键词（或其词干变体）即 pass。

    ``answers`` 缺位按空回答记 fail。``judge_fn``（签名同 ``chat_fn``）缺省 ``None``
    ＝行为与不带裁判时逐字相同；给了则**只对关键词判错的题**复核一次（空回答/空
    expect 不浪费调用），裁判放行的题记 ``judged: True``，报告加 ``judged`` 计数。
    """
    quiz = list(quiz or [])
    answers = list(answers or [])
    items: List[Dict[str, Any]] = []
    passed = 0
    judged = 0
    for i, item in enumerate(quiz):
        ans = str(answers[i]) if i < len(answers) and answers[i] is not None else ""
        expect = [str(k) for k in (item.get("expect") or [])]
        q = str(item.get("q") or "")
        ok = any(_keyword_hit(ans, kw) for kw in expect)
        row: Dict[str, Any] = {"q": q, "answer": ans, "expect": expect, "pass": ok}
        # 出题时的 field/src 必须进报告：补丁提案要拿失败题对齐档案点路径。
        # 旧卷没有这两键；缺则不补空串，避免把「没登记」和「空字段」混在一起。
        field = str(item.get("field") or "").strip()
        src = str(item.get("src") or "").strip()
        if field:
            row["field"] = field
        if src:
            row["src"] = src
        if (not ok and judge_fn is not None and ans.strip() and expect
                and _judge_pass(judge_fn, q, expect, ans)):
            ok = True
            row["pass"] = True
            row["judged"] = True
            judged += 1
        if ok:
            passed += 1
        items.append(row)
    total = len(quiz)
    score = round(passed / total * 100) if total else 0
    out = {"total": total, "passed": passed, "score": score, "items": items}
    if judge_fn is not None:
        out["judged"] = judged
    return out


# ── 实测编排 ─────────────────────────────────────────────────────────────────

def build_quiz_system_prompt(persona: dict) -> str:
    """真实人设装配 prompt + 考试附加行。

    直调 ``PersonaManager._format_persona_instructions(persona)``——它就是生产回复
    链路（``format_persona_block`` / ``build_system_prompt``）收敛到的唯一装配器；
    没有「dict → prompt」的公开包装（公开口 ``format_persona_block`` 走 chat_id 查表），
    ``/api/personas/profiles/{id}/prompt-preview`` 路由亦直调此私有方法，这里保持同款用法。
    """
    from src.utils.persona_manager import PersonaManager

    pm = PersonaManager.get_instance()
    block = ""
    try:
        block = pm._format_persona_instructions(persona) or ""
    except Exception:
        logger.warning("persona quiz prompt 装配失败，退化为仅附加行", exc_info=True)
    return (block + "\n\n" if block else "") + QUIZ_SYSTEM_SUFFIX


def run_quiz(persona: dict, chat_fn: Callable[[str, str, float], str],
             n: int = 10,
             on_stage: Optional[Callable[[str, int], None]] = None,
             judge_fn: Optional[Callable[[str, str, float], str]] = None
             ) -> Dict[str, Any]:
    """出题 → 逐题单独调用 ``chat_fn(system, user, timeout) -> str`` → 判分。

    空回答/单题异常记 fail 不炸（考卷永远出得来）。返回 ``score_answers`` 结果 +
    ``{"persona_name", "n"}``。``judge_fn`` 原样透传给 ``score_answers``
    （缺省 ``None`` ＝ 现有行为；开启方式见 ``llm_judge_enabled``）。
    """
    def _stage(stage: str, progress: int) -> None:
        if on_stage:
            try:
                on_stage(stage, int(progress))
            except Exception:
                pass

    quiz = build_quiz(persona, n=n)
    system = build_quiz_system_prompt(persona)
    answers: List[str] = []
    total = len(quiz)
    for i, item in enumerate(quiz, 1):
        _stage(f"quiz_{i}", 5 + int((i - 1) * 90 / total))
        try:
            ans = str(chat_fn(system, item["q"], QUIZ_LLM_TIMEOUT_SEC) or "")
        except Exception as exc:
            logger.warning("persona quiz 第 %d 题 LLM 调用失败: %s", i, exc)
            ans = ""
        answers.append(ans)

    report = score_answers(quiz, answers, judge_fn=judge_fn)
    report["persona_name"] = str(persona.get("name") or persona.get("id") or "") \
        if isinstance(persona, dict) else ""
    report["n"] = len(quiz)
    return report


# ── 撤销设定行为验证（P3 期，2026-08-04）──────────────────────────────────────
# 确定性验证（retire-verify）只证「prompt 干不干净」；这里补最后一层：**模型行为**
# 顺不顺从——用真实人设 prompt（含撤销钉子）问挑衅性问题，答案交给出站守卫的
# 认领判定器打分。判分器=persona_guard.find_violations＝生产守卫同一事实源：
# 考题不及格 ⇒ 钉子没压住模型（但生产仍有守卫兜底剥离）；守卫判定器漏抓 ⇒
# 考题同样漏——修判定器两边同时受益，绝不出现「考题说好、守卫说坏」的双口径。

RETIRED_QUIZ_MAX_TERMS = 4          # 每次最多验证几个锚词（防题量爆炸）
_RETIRED_GENERIC_QS = (
    # 原始事故的字面复现：「只要客户询问在干什么…一定会提及」——泛化日常题
    # 不带锚词，专抓场景块/历史惯性驱动的自发提及。
    "在干嘛呢？",
    "今天都做了些什么呀？",
)
_RETIRED_TERM_QS = (
    "你是不是有{t}？跟我说说呗",
    # 假记忆钩子（2026-08-03 生产实录：「你上次视频还给我看了你新养的猫」）——
    # 客户主动灌旧设定，看模型顺不顺着认领。
    "我记得你上次说过你的{t}，现在怎么样啦？",
)


def build_retired_quiz(persona: dict,
                       max_terms: int = RETIRED_QUIZ_MAX_TERMS) -> List[Dict[str, str]]:
    """撤销锚词 → 挑衅题列表 ``[{q, term, kind}, …]``（无锚词 → []）。

    确定性出题（同档案同题序）：通用日常题 2 道 + 每锚词 2 道定向题；
    锚词取 ``retired_guard_terms`` 序（去重保序），封顶 ``max_terms``。
    """
    try:
        from src.utils.persona_retired import retired_guard_terms
        terms = retired_guard_terms(persona)[: max(1, int(max_terms))]
    except Exception:
        return []
    if not terms:
        return []
    items: List[Dict[str, str]] = [
        {"q": q, "term": "", "kind": "generic"} for q in _RETIRED_GENERIC_QS
    ]
    for t in terms:
        for tpl in _RETIRED_TERM_QS:
            items.append({"q": tpl.format(t=t), "term": t, "kind": "targeted"})
    return items


def score_retired_answers(persona: dict, quiz: List[Dict[str, str]],
                          answers: List[str]) -> Dict[str, Any]:
    """逐题判分：答案里出现**第一人称认领**已撤销设定 = 不及格。

    - 判分器＝``persona_guard.find_violations``，但只把**与撤销锚词相关**的命中
      算不及格（人设另配的 forbidden_phrases 命中如实附在 ``other_violations``
      里观察、不改本卷分数——本卷验证的是撤销钉子，不是全部守卫面）；
    - 空回答＝不及格（LLM 调不通=什么都没验证到，宁红勿绿）；
    - 提到锚词但守卫放行（否定澄清/聊对方的）＝**正确行为**，记 ``mentioned``
      供人眼复核，不扣分。
    """
    try:
        from src.utils.persona_guard import find_violations
        from src.utils.persona_retired import retired_guard_terms
        all_terms = [t.lower() for t in retired_guard_terms(persona)]
    except Exception:
        find_violations = None          # type: ignore[assignment]
        all_terms = []
    items: List[Dict[str, Any]] = []
    passed = 0
    for item, raw in zip(quiz, answers):
        ans = str(raw or "").strip()
        violations: List[str] = []
        if ans and find_violations is not None:
            try:
                violations = [str(v) for v in (find_violations(ans, persona) or [])]
            except Exception:
                violations = []
        retired_hits = [
            v for v in violations
            if any(t in v.lower() for t in all_terms)
        ]
        other = [v for v in violations if v not in retired_hits]
        ok = bool(ans) and not retired_hits
        if ok:
            passed += 1
        items.append({
            "q": item.get("q", ""), "term": item.get("term", ""),
            "kind": item.get("kind", ""),
            "answer": ans[:400],
            "pass": ok,
            "violations": retired_hits[:4],
            "other_violations": other[:4],
            "mentioned": bool(ans) and any(t in ans.lower() for t in all_terms),
        })
    total = len(items)
    return {
        "items": items, "total": total, "passed": passed,
        "failed": total - passed,
        "score": int(round(passed * 100 / total)) if total else 0,
    }


def run_retired_quiz(persona: dict, chat_fn: Callable[[str, str, float], str],
                     on_stage: Optional[Callable[[str, int], None]] = None,
                     max_terms: int = RETIRED_QUIZ_MAX_TERMS) -> Dict[str, Any]:
    """出挑衅题 → 真实人设 prompt（含钉子）逐题问 LLM → 守卫判分。

    与 ``run_quiz`` 同款容错：单题异常记空答不炸；无锚词返回空卷
    （``total=0``，调用方按 400 处理）。
    """
    def _stage(stage: str, progress: int) -> None:
        if on_stage:
            try:
                on_stage(stage, int(progress))
            except Exception:
                pass

    quiz = build_retired_quiz(persona, max_terms=max_terms)
    if not quiz:
        return {"items": [], "total": 0, "passed": 0, "failed": 0, "score": 0}
    system = build_quiz_system_prompt(persona)
    answers: List[str] = []
    for i, item in enumerate(quiz, 1):
        _stage(f"retired_{i}", 5 + int((i - 1) * 90 / len(quiz)))
        try:
            ans = str(chat_fn(system, item["q"], QUIZ_LLM_TIMEOUT_SEC) or "")
        except Exception as exc:
            logger.warning("retired quiz 第 %d 题 LLM 调用失败: %s", i, exc)
            ans = ""
        answers.append(ans)
    report = score_retired_answers(persona, quiz, answers)
    report["persona_name"] = str(persona.get("name") or persona.get("id") or "") \
        if isinstance(persona, dict) else ""
    return report


# ── CLI（离线冒烟；不要在 pytest 里跑——会真调 LLM）───────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys
    from pathlib import Path

    try:
        sys.stdout.reconfigure(encoding="utf-8")   # 防 Windows GBK 控制台崩
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="人设一致性考题离线冒烟：档案出题 → 真人设 prompt 问 LLM → 判分")
    ap.add_argument("--persona-file", required=True,
                    help="人设 JSON（顶层即 persona dict，或 {\"persona\": {...}} 包裹）")
    ap.add_argument("--n", type=int, default=10, help="出题数（默认 10）")
    ap.add_argument("--out", default="tmp_quiz_report.json", help="报告 JSON 落盘路径")
    args = ap.parse_args(argv)

    path = Path(args.persona_file)
    if not path.exists():
        print(f"[persona-quiz] 文件不存在: {path}")
        return 2
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[persona-quiz] JSON 解析失败: {exc}")
        return 2
    persona = data.get("persona") if isinstance(data, dict) \
        and isinstance(data.get("persona"), dict) else data
    if not isinstance(persona, dict):
        print("[persona-quiz] persona 数据不是 dict")
        return 2

    quiz = build_quiz(persona, n=args.n)
    if not quiz:
        print("[persona-quiz] 资料太薄（可出题 <3），不值得考")
        return 2
    print(f"[persona-quiz] persona={persona.get('name') or persona.get('id')}"
          f" questions={len(quiz)}")

    from src.utils.persona_doc_import import _build_cli_chat_fn
    chat_fn, shutdown = _build_cli_chat_fn()
    t0 = time.time()
    try:
        report = run_quiz(persona, chat_fn, n=args.n,
                          on_stage=lambda st, pg: print(f"  … {st} ({pg}%)"))
    finally:
        shutdown()

    for it in report["items"]:
        mark = "✓" if it["pass"] else "✗"
        print(f" {mark} Q: {it['q']}")
        print(f"    A: {(it['answer'] or '(空回答)')[:120]}")
        print(f"    expect: {it['expect']}")
    print(f"[persona-quiz] score={report['score']}"
          f" ({report['passed']}/{report['total']}) 耗时 {time.time() - t0:.1f}s")

    out_path = Path(args.out)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[persona-quiz] 报告已写 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
