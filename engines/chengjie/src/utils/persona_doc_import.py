# -*- coding: utf-8 -*-
"""「从文档创建人设」LLM 结构化抽取管线（B 线，2026-07-27）。

运营拿到客户的「人设包装文档」（.docx，1-4 万字，常中英双语重复叙述：身份卡/工作/
学历/家人档案/人生时间线/感情史/名字来历……），本模块把它变成 canonical 人设 JSON
（schema 与 ``persona_manager._format_persona_instructions`` 的消费字段一一对应，
富人设范例见 ``config/profiles_runtime.yaml`` 的 su_wan）。

流水线：``extract_docx_text``（python-docx 优先，zipfile+regex 兜底）→ ``prepare_text``
（清洗+截断）→ ``run_extraction``（三次 LLM：①身份与性格 ②传记与记忆 → clamp →
③独立轻量溯源；prompt 硬规则=只用文档事实、绝不编造、严格 JSON）→
coverage/completeness 汇总。LLM 经 ``chat_fn(system, user, timeout) -> str`` 注入
（路由层从生产 AIClient 构建、测试注入假函数、CLI 自建），本模块自身零网络依赖。

溯源标注（H2 线，2026-07-27）：Call1/Call2 **不**要求 sources（保简洁 JSON、降重试）；
Call3 独立轻量调用，输出**只** ``{"sources": {...}}``。键支持字段级点路径，以及逐条记忆
``context.specific_memories.N``（0-based，与 memories 下标对齐；越界丢弃）；
字段级 ``context.specific_memories`` 作整组回落。编排层校验编号（int 且 1..N）后
出 ``sources`` / ``source_excerpts`` / ``paragraphs_total``。Call3 失败/空 →
``sources={}`` + warning「溯源标注未生成」，**绝不因溯源让整次抽取失败**。

Call3 分批 + 输入预算（I2 线，2026-07-28，治「输出 JSON 太大 → 空响应/截断」）：
一次给「全部字段 + 16 条记忆」的大 JSON 空响应率高，改为**分批**——批 1=身份与概览
字段，批 2..N=``specific_memories`` 每 6 条一批（记忆 index 全局 0-based 不重编号）。
每批独立解析、独立失败容忍（某批空只丢该批，warnings 记「第 N 批溯源未生成」），
各批 sources 按键去重合并（``_filter_batch_sources`` 保证键域不重叠）。每批输入不再
硬塞全部段落：``preselect_paragraphs``（纯函数，CJK bigram / 拉丁词 / 数字关键词粗筛）
按相关度取 top 60 段（**编号保持 1-based 原值**），单段截 200 字，user 预算 6k 字符。
重试分两种：坏 JSON → 附 ``_RETRY_SUFFIX``；**空响应**（len==0/纯空白）→ 段落减半 +
摘要再截短后重试，并在 warnings 记录。``run_extraction`` 另出 ``extract_meta``
（逐次调用耗时/是否重试/是否空响应/调用次数/是否截断修复、批次成败、记忆溯源覆盖、
总耗时），CLI 直接打印。

Call1/Call2 抖动韧性（I2 线续，2026-07-28，真实 33k 文档 CLI 冒烟事故）：Call2 偶发
**空响应**（len=0）被当成「坏 JSON」附 ``_RETRY_SUFFIX`` 重试 → 误导模型输出解释性
文字 → 整轮抽取硬失败（用户白等两分钟看到红叉）；同一 prompt 单独复跑两次均成功，
证明是云端抖动而非 prompt 缺陷。``_call_llm_json`` 遂改**分类重试状态机**：空响应
**原样重试**（不加话术）最多 ``_EMPTY_RETRIES`` 次、退避 ``_EMPTY_BACKOFF_SEC``；
坏 JSON 才附 ``_RETRY_SUFFIX`` 重试 ``_BAD_JSON_RETRIES`` 次；两类可混合，总调用
≤ ``_MAX_LLM_ATTEMPTS``。另加 ``_repair_truncated_json``——输出撞 ``LLM_MAX_TOKENS``
被硬切时，在最后一个完整值边界截断补齐闭合符再解析（扫描跳过字符串与转义，正文里的
括号不参与配平），修复成功记 warning 提示该字段可能不全，修复后仍坏则照旧失败。

进程级任务注册表（``create_job``/``get_job``）供路由做「提交→后台线程抽取→前端轮询」；
TTL 30 分钟、并发上限 3（满载 ``create_job`` 返回 None → 调用方转 429）。
``create_job_runner(runner)`` 为通用后台任务入口（同一注册表/TTL/状态机
queued|running|done|error），供兄弟线（如人设一致性考题）复用；``create_job``
基于它实现（对外签名/行为不变）。

CLI 离线冒烟（集成负责人跑，勿在测试里真调 LLM）::

    python -m src.utils.persona_doc_import --file 人设包装.docx [--out tmp_extract.json]
"""
from __future__ import annotations

import copy
import inspect
import json
import logging
import re
import threading
import time
import uuid
from html import unescape
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("ai_chat_assistant.persona_doc_import")

# ── 预算常量 ──────────────────────────────────────────────────────────────────
MAX_TEXT_CHARS = 120_000          # 抽取输入文本上限（超出截断）
LLM_TIMEOUT_SEC = 120.0           # 单次 LLM 调用超时
LLM_MAX_TOKENS = 4000             # 单次 LLM 输出预算（人设 JSON 可观）
LLM_TEMPERATURE = 0.2             # 抽取任务要保真，低温

_BG_MAX_CHARS = 1500              # background 浓缩履历
_MEMORY_MAX_ITEMS = 16            # specific_memories 条数
_MEMORY_MAX_CHARS = 120           # 每条记忆长度
_HOBBY_MAX_ITEMS = 12
_TAG_MAX_ITEMS = 10
_TRAIT_MAX_ITEMS = 8
_SCENE_MAX_ITEMS = 6
_TASTE_MAX_ITEMS = 8              # tastes.likes/dislikes/opinions 各自
_OPENER_MAX_ITEMS = 8
_AVOID_MAX_ITEMS = 6
_FAMILY_MAX_ITEMS = 12            # context.family 亲属条数
_FAMILY_KEY_MAX_CHARS = 24        # 角色 key（father / ex_husband / 姑姑…）
_FAMILY_VALUE_MAX_CHARS = 120     # 「姓名，一句话说明」

_EXCERPT_MAX_CHARS = 500          # source_excerpts 单段落收录上限（超长截断加 …）
_SOURCES_USER_BUDGET = 6_000      # Call3 每批 user 字符预算（摘要 + 编号段落）
_SOURCES_PARA_CLIP = 200          # 编号段落每段截断长度
_SOURCES_PRESELECT_LIMIT = 60     # 每批粗筛保留的段落数
_SOURCES_MEMORY_BATCH = 6         # specific_memories 每批条数
_SOURCES_RETRY_PARA_CLIP = 120    # 空响应重试时段落截得更短
_SOURCES_RETRY_TEXT_CLIP = 60     # 空响应重试时摘要文本截得更短
_SOURCES_RETRY_LIST_ITEMS = 4     # 空响应重试时摘要列表只留前几项
_MEMORY_SOURCE_KEY_RE = re.compile(r"^context\.specific_memories\.(\d+)$")
_MEMORY_GROUP_SOURCE_KEY = "context.specific_memories"


# ── docx 文本提取 ─────────────────────────────────────────────────────────────

def _docx_text_via_zipfile(data: bytes) -> str:
    """zipfile+regex 兜底解析：按 ``</w:p>`` 切段、抓 ``<w:t>`` 文本、unescape 实体。"""
    import zipfile

    with zipfile.ZipFile(BytesIO(data)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    paras: List[str] = []
    for chunk in xml.split("</w:p>"):
        texts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", chunk, flags=re.S)
        line = unescape("".join(texts)).strip()
        if line:
            paras.append(line)
    return "\n".join(paras)


def extract_docx_text(data: bytes) -> str:
    """从 .docx 字节提取纯文本（按段落换行）。

    优先 python-docx（requirements 已有，宝箱文档翻译同款用法）；ImportError/解析失败/
    提取为空 → 回落 zipfile+regex 解析 ``word/document.xml``。两路都失败返回 ""。
    """
    try:
        import docx  # noqa: F401  （懒导入：环境缺库时走兜底）

        document = docx.Document(BytesIO(data))
        paras = [(p.text or "").strip() for p in document.paragraphs]
        text = "\n".join(p for p in paras if p)
        if text.strip():
            return text
    except Exception:
        logger.debug("python-docx 解析失败，回落 zipfile 解析", exc_info=True)
    try:
        return _docx_text_via_zipfile(data)
    except Exception:
        logger.debug("zipfile 兜底解析也失败", exc_info=True)
        return ""


def prepare_text(text: str) -> Tuple[str, bool]:
    """清洗 + 截断：规整换行、去行尾空白、压缩连续空行；超 ``MAX_TEXT_CHARS`` 截断。

    返回 ``(text, truncated)``。
    """
    t = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in t.split("\n")]
    t = "\n".join(lines)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    truncated = False
    if len(t) > MAX_TEXT_CHARS:
        t = t[:MAX_TEXT_CHARS]
        truncated = True
    return t, truncated


def count_paragraphs(text: str) -> int:
    """非空行数（=段落数口径，与 extract_docx_text 的段落换行一致）。"""
    return sum(1 for ln in str(text or "").split("\n") if ln.strip())


def split_numbered_paragraphs(text: str) -> List[str]:
    """与 ``count_paragraphs`` 同口径切段（非空行=一段），供溯源编号引用。

    不变量：``len(split_numbered_paragraphs(t)) == count_paragraphs(t)``——
    prompt 里的 ``[n]`` 编号与本列表下标+1 一一对应，``source_excerpts``
    按同一列表取段落原文，保证编号全程一致。
    """
    return [ln.strip() for ln in str(text or "").split("\n") if ln.strip()]


# ── LLM 输出容错解析 ──────────────────────────────────────────────────────────

def _repair_truncated_json(text: str) -> Optional[str]:
    """截断修复：在**最后一个完整值边界**截断并补齐闭合符，返回候选 JSON 串。

    典型场景＝输出撞上 ``LLM_MAX_TOKENS`` 被硬切，尾部缺 ``}``/``]`` 或只剩半个
    键值对。扫描器跳过字符串字面量与 ``\\`` 转义——内容里的 ``{``/``}`` 不参与
    配平（否则「他说了句 { 括号」这种正文会把栈算错）。

    只在「完整值刚结束」处下刀（闭合的 ``}``/``]``、作为**值**的字符串右引号、
    被分隔符终止的字面量）；**结尾未被终止的字面量不算边界**（``123`` 可能是
    ``1234`` 被截半）。结构错乱（括号类型不匹配）/ 找不到任何边界 → None，
    由调用方照旧当失败处理——宁可失败也不返回半成品乱数据。
    """
    s = str(text or "")
    i = s.find("{")
    if i < 0:
        return None
    s = s[i:]

    stack: List[str] = []        # 容器栈：'{' / '['
    cut = -1                     # 最后一个完整值边界（exclusive）
    cut_stack: List[str] = []
    in_str = False
    str_is_key = False
    esc = False
    expect_value = False         # 对象里刚见过 ':'（下一个字符串是值不是键）
    tok_start = -1               # 字面量（数字 / true / false / null）起点

    for idx, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
                if not str_is_key:
                    cut, cut_stack = idx + 1, list(stack)
            continue
        if tok_start >= 0 and (ch.isspace() or ch in ",}]"):
            cut, cut_stack = idx, list(stack)      # 字面量被分隔符终止=完整
            tok_start = -1
        if ch == '"':
            in_str = True
            str_is_key = bool(stack) and stack[-1] == "{" and not expect_value
        elif ch in "{[":
            stack.append(ch)
            expect_value = False
        elif ch in "}]":
            if not stack or stack[-1] != ("{" if ch == "}" else "["):
                return None                        # 结构错乱，不猜
            stack.pop()
            expect_value = False
            cut, cut_stack = idx + 1, list(stack)
        elif ch == ":":
            expect_value = True
        elif ch == ",":
            expect_value = False
        elif not ch.isspace() and tok_start < 0:
            tok_start = idx
    if cut < 0:
        return None
    return s[:cut] + "".join(
        "}" if c == "{" else "]" for c in reversed(cut_stack))


def _parse_llm_json_ex(raw: str) -> Tuple[Optional[dict], bool]:
    """``_parse_llm_json`` 的带观测版：返回 ``(dict|None, 是否走了截断修复)``。"""
    if not isinstance(raw, str) or not raw.strip():
        return None, False
    t = raw.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    i = t.find("{")
    if i < 0:
        return None, False
    j = t.rfind("}")
    if j > i:
        body = t[i:j + 1]
        for candidate in (body, re.sub(r",\s*([}\]])", r"\1", body)):
            try:
                obj = json.loads(candidate)
            except Exception:
                continue
            return (obj, False) if isinstance(obj, dict) else (None, False)
    fixed = _repair_truncated_json(t[i:])
    if fixed:
        try:
            obj = json.loads(fixed)
        except Exception:
            return None, False
        if isinstance(obj, dict):
            return obj, True
    return None, False


def _parse_llm_json(raw: str) -> Optional[dict]:
    """容错解析 LLM 输出为 dict：剥 ```json 围栏、取首 ``{`` 到末 ``}``、
    json.loads，失败再试去尾逗号轻修复、再试 ``_repair_truncated_json``
    截断修复；彻底坏 / 非 dict → None。"""
    return _parse_llm_json_ex(raw)[0]


# ── 预算护栏（clamp）──────────────────────────────────────────────────────────

def _clean_str(v: Any, max_chars: int = 0) -> str:
    """字符串字段：非 str 丢弃（返 ""）；strip；超长截断。"""
    if not isinstance(v, str):
        return ""
    t = v.strip()
    if max_chars and len(t) > max_chars:
        t = t[:max_chars].strip()
    return t


def _clean_str_list(v: Any, max_items: int, max_chars: int = 0) -> List[str]:
    """字符串列表字段：非 list/tuple 丢弃；条目非 str 丢弃；strip 剔空；截条数/长度。"""
    if not isinstance(v, (list, tuple)):
        return []
    out: List[str] = []
    for item in v:
        s = _clean_str(item, max_chars)
        if s:
            out.append(s)
        if len(out) >= max_items:
            break
    return out


def _clean_str_dict(v: Any, keys: Tuple[str, ...], max_chars: int = 0) -> Dict[str, str]:
    """子 dict 里挑指定键的字符串值：宿主非 dict / 值非 str / 空 → 剔除。"""
    if not isinstance(v, dict):
        return {}
    out: Dict[str, str] = {}
    for k in keys:
        s = _clean_str(v.get(k), max_chars)
        if s:
            out[k] = s
    return out


def _clean_flat_dict(
    v: Any, max_items: int, key_max_chars: int, val_max_chars: int,
) -> Dict[str, str]:
    """键不预先枚举的扁平 str→str 字典（``context.family`` 的角色 key 是开放集）。

    与 ``_clean_str_dict`` 的区别：那个按白名单挑键，这个保留 LLM 给的任意键但限量。
    值里的换行折成空格——下游 ``persona_entity_alias._head_segment`` 用换行断句，
    多行值会让「姓名在首段」的约定失效。
    """
    if not isinstance(v, dict):
        return {}
    out: Dict[str, str] = {}
    for raw_k, raw_v in v.items():
        if len(out) >= max_items:
            break
        k = _clean_str(raw_k, key_max_chars)
        s = _clean_str(raw_v, val_max_chars)
        if k and s and k not in out:
            out[k] = " ".join(s.split())
    return out


def clamp_persona(persona: dict) -> dict:
    """预算护栏：长度/条数上限 + 全量 strip 剔空 + 类型防御。**不就地改入参**。

    上限：background≤1500 字符、specific_memories≤16×120、hobbies≤12、tags≤10、
    traits≤8、selfie_scenes≤6、tastes 各≤8、openers≤8、topics_to_avoid≤6。
    清洗后为空的键直接剔除（保持「文档没有就省略」语义）。
    """
    src = persona if isinstance(persona, dict) else {}
    out: Dict[str, Any] = {}

    name = _clean_str(src.get("name"), 80)
    if name:
        out["name"] = name

    names = _clean_str_dict(
        src.get("names"),
        ("full_western", "english", "german", "french", "nickname", "usage_notes"),
        200)
    if names:
        out["names"] = names

    role = _clean_str(src.get("role"), 200)
    if role:
        out["role"] = role

    age = src.get("age")
    if isinstance(age, bool):
        age = None
    if isinstance(age, (int, float)):
        out["age"] = int(age)
    elif isinstance(age, str) and age.strip().isdigit():
        out["age"] = int(age.strip())

    gender = _clean_str(src.get("gender"), 20).lower()
    gender = {"女": "female", "男": "male"}.get(gender, gender)
    if gender:
        out["gender"] = gender

    tags = _clean_str_list(src.get("tags"), _TAG_MAX_ITEMS, 24)
    if tags:
        out["tags"] = tags

    background = _clean_str(src.get("background"), _BG_MAX_CHARS)
    if background:
        out["background"] = background

    p_src = src.get("personality")
    if isinstance(p_src, dict):
        p_out: Dict[str, Any] = {}
        traits = _clean_str_list(p_src.get("traits"), _TRAIT_MAX_ITEMS, 40)
        if traits:
            p_out["traits"] = traits
        for k in ("style", "quirks", "humor", "temperament"):
            s = _clean_str(p_src.get(k), 400)
            if s:
                p_out[k] = s
        if p_out:
            out["personality"] = p_out

    s_src = src.get("speaking")
    if isinstance(s_src, dict):
        openers = _clean_str_list(s_src.get("openers"), _OPENER_MAX_ITEMS, 60)
        if openers:
            out["speaking"] = {"openers": openers}

    c_src = src.get("context")
    if isinstance(c_src, dict):
        c_out: Dict[str, Any] = {}
        hobbies = _clean_str_list(c_src.get("hobbies"), _HOBBY_MAX_ITEMS, 30)
        if hobbies:
            c_out["hobbies"] = hobbies
        mems = _clean_str_list(
            c_src.get("specific_memories"), _MEMORY_MAX_ITEMS, _MEMORY_MAX_CHARS)
        if mems:
            c_out["specific_memories"] = mems
        family = _clean_flat_dict(
            c_src.get("family"), _FAMILY_MAX_ITEMS,
            _FAMILY_KEY_MAX_CHARS, _FAMILY_VALUE_MAX_CHARS)
        if family:
            c_out["family"] = family
        triggers = _clean_str_dict(
            c_src.get("emotional_triggers"),
            ("positive", "negative", "deep_empathy"), 300)
        if triggers:
            c_out["emotional_triggers"] = triggers
        if c_out:
            out["context"] = c_out

    b_src = src.get("boundaries")
    if isinstance(b_src, dict):
        avoid = _clean_str_list(b_src.get("topics_to_avoid"), _AVOID_MAX_ITEMS, 30)
        if avoid:
            out["boundaries"] = {"topics_to_avoid": avoid}

    appearance = _clean_str(src.get("appearance"), 600)
    if appearance:
        out["appearance"] = appearance

    scenes = _clean_str_list(src.get("selfie_scenes"), _SCENE_MAX_ITEMS, 120)
    if scenes:
        out["selfie_scenes"] = scenes

    t_src = src.get("tastes")
    if isinstance(t_src, dict):
        t_out: Dict[str, Any] = {}
        for k in ("likes", "dislikes", "opinions"):
            items = _clean_str_list(t_src.get(k), _TASTE_MAX_ITEMS, 60)
            if items:
                t_out[k] = items
        if t_out:
            out["tastes"] = t_out

    identity = src.get("identity")
    if isinstance(identity, dict) and identity:
        out["identity"] = copy.deepcopy(identity)

    return out


# ── 建议 profile_id ───────────────────────────────────────────────────────────

def _slugify_ascii(s: str) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z0-9]*", str(s or ""))
    slug = "_".join(w.lower() for w in words)
    return slug[:48].strip("_")


def suggested_profile_id(persona: dict) -> str:
    """英文名 → 小写下划线 slug；无英文名从 name 提 ascii；再兜底日期占位 id。"""
    src = persona if isinstance(persona, dict) else {}
    names = src.get("names") if isinstance(src.get("names"), dict) else {}
    for cand in (names.get("english"), names.get("full_western"),
                 src.get("name"), src.get("role")):
        slug = _slugify_ascii(cand if isinstance(cand, str) else "")
        if slug:
            return slug
    return "imported_persona_" + time.strftime("%Y%m%d")


# ── 抽取 prompt（Call1/Call2 人设字段；Call3 独立溯源）────────────────────────

_COMMON_RULES = (
    "你是「人设文档结构化抽取器」。输入是一份人设包装文档（常为中英双语，同一事实会用"
    "两种语言各叙述一遍），你从中抽取角色设定，输出**一个严格 JSON 对象**。\n"
    "硬规则（违反即失败）：\n"
    "1. 只用文档里明确出现的事实；文档没有提到的字段直接省略该键——**绝不编造**。\n"
    "2. 中英重复叙述的是同一事实，只抽取一次，以中文为主；人名/地名/机构名/品牌保留"
    "文档原文写法（含外文原文）。\n"
    "3. 输出必须是严格 JSON：无 markdown 围栏、无注释、无 JSON 以外的解释文字。"
    "**只输出人设字段**（加 warnings），不要输出 sources / 溯源相关键。\n"
    "4. 除字段说明里明确要求英文的字段外，值用中文书写（专有名词保留原文）。\n"
    "5. 同时输出 \"warnings\" 字符串数组：凡是你做了归纳/组合/推断（而非文档原文直接"
    "支持）的地方，用一句话说明；没有则输出 []。\n"
)

_IDENTITY_SYSTEM = _COMMON_RULES + (
    "\n本次抽取【身份与性格】。输出字段（文档没有的键省略）：\n"
    "{\n"
    '  "name": "对外自称的主名字（如日文名/中文名）",\n'
    '  "names": {"full_western": "西方完整姓名", "english": "英文名",'
    ' "nickname": "昵称", "usage_notes": "名字使用说明（谁怎么叫、什么场合用哪个名）"},\n'
    '  "role": "一句话身份：职业+城市+年龄，如「在西班牙巴塞罗那的酒店运营总监助理，32岁」",\n'
    '  "age": 32,\n'
    '  "gender": "female 或 male",\n'
    '  "tags": ["3-10 个标签：性别/年龄/城市/职业/性格关键词，每个不超过 8 字"],\n'
    '  "personality": {\n'
    '    "traits": ["性格特点短语，最多 8 条"],\n'
    '    "style": "TA 平时怎么说话的口语化描述（从性格/语言能力/职业归纳，属归纳需在 warnings 说明）",\n'
    '    "quirks": "口头禅/说话习惯（文档有才写）",\n'
    '    "humor": "幽默感描述（文档有才写）",\n'
    '    "temperament": "脾气/性情（文档有才写）"\n'
    "  },\n"
    '  "speaking": {"openers": ["日常开场白示例，最多 8 条（文档有才写）"]},\n'
    '  "boundaries": {"topics_to_avoid": ["应回避的话题，最多 6 条；可从创伤/敏感经历合理得出，'
    '属推断需在 warnings 说明"]},\n'
    '  "appearance": "英文外貌锚点一段（仅当文档有外貌/穿搭/身材描述才输出；用英文，'
    '格式如 a 32-year-old ... woman, ... hair, ... style）",\n'
    '  "warnings": []\n'
    "}\n"
)

_BIOGRAPHY_SYSTEM = _COMMON_RULES + (
    "\n本次抽取【传记与记忆】。补充规则：\n"
    "6. \"specific_memories\" 每条必须是**一句话、可被客户追问验证的具体事实**"
    "（含人名/时间/地点/事件），如「父亲 José Leandro Navarro 是建筑师，2019 年 5 月"
    "因肺癌去世，享年 68 岁」。优先覆盖：家人档案（名字/职业/所在地）、学历与工作时间线、"
    "感情史要点、名字来历。每条不超过 100 字。\n"
    "7. \"background\" 用第三人称浓缩人生履历（出生→成长→学历→工作→现状），800 字以内；"
    "不逐字复制原文，但时间/地名/机构等事实必须与文档一致。\n"
    "输出字段（文档没有的键省略）：\n"
    "{\n"
    '  "background": "第三人称人生履历浓缩",\n'
    '  "context": {\n'
    '    "hobbies": ["兴趣爱好，最多 12 条"],\n'
    '    "specific_memories": ["最多 16 条具体事实短句"],\n'
    '    "emotional_triggers": {\n'
    '      "positive": "什么话题/行为让 TA 来精神（从文档性格与经历得出，属归纳需在 warnings 说明）",\n'
    '      "negative": "什么话题让 TA 防御/疏远（如背叛/创伤相关）",\n'
    '      "deep_empathy": "什么话题触发 TA 深度共情"\n'
    "    }\n"
    "  },\n"
    '  "tastes": {\n'
    '    "likes": ["喜欢的东西，最多 8 条"],\n'
    '    "dislikes": ["不喜欢的东西，最多 8 条（文档有才写）"],\n'
    '    "opinions": ["TA 会说的观点句，最多 8 条（基于文档价值观，属归纳需在 warnings 说明）"]\n'
    "  },\n"
    '  "selfie_scenes": ["英文场景短语，最多 6 条，依据文档中的生活方式合理组合'
    '（如 hotel lobby / beach promenade）——属推断需在 warnings 说明"],\n'
    '  "warnings": []\n'
    "}\n"
)

# 亲属名册单开一次调用而不是并进 Call2：Call2 的输出（800 字履历 + 16 条记忆 +
# 兴趣/口味/场景）本来就贴着 ``LLM_MAX_TOKENS`` 跑，实测 33k 文档会触发截断修复
# （``repaired: true``）——往一个已经在溢出的调用里再塞字段，只会让新旧字段一起丢。
# 独立调用输出很小、失败可单独降级，且不动既有 Call2 的稳定性。
_FAMILY_SYSTEM = _COMMON_RULES + (
    "\n本次只抽取【亲属名册】，输出一个键：\n"
    "{\n"
    '  "family": {\n'
    '    "father": "José Leandro Navarro，西班牙建筑师，2019 年因肺癌去世",\n'
    '    "ex_husband": "José Luis Jerónimo，西班牙人，2017 年 10 月结婚，2018 年 8 月离婚"\n'
    "  },\n"
    '  "warnings": []\n'
    "}\n"
    "补充规则：\n"
    "6. 值**必须以姓名开头**，姓名之后用中文逗号接一句话说明（职业/年龄/所在地/状态）。"
    "写成「我的丈夫叫 X」「建筑师 X」「已离婚」都算错——姓名必须在最前面。"
    "姓名保留文档原文写法（含外文原文）；文档只给了称谓没给名字的亲属直接省略该条。\n"
    "7. 角色 key 用英文小写下划线：father / mother / husband / ex_husband / wife /"
    " ex_wife / son / daughter / brother / sister / grandfather / grandmother；"
    "其余亲属（姑姑、舅舅、堂表亲等）用中文角色名作 key。同一角色有多人时，"
    "第二人起改用中文角色名作 key（如 sister 之外再写「妹妹」），**不要**加"
    " _2 这类数字后缀。\n"
    "8. **关系要跨句推断**：文档常常只在叙事里写「与 X 相恋…求婚…结婚…离婚」，"
    "全篇不出现「我丈夫 X」这种写法——这类也必须归到 husband / ex_husband"
    "（文档写明已离婚的归 ex_husband）。别因为原文没有出现称谓词就漏掉。\n"
    "9. 最多 12 条，每条值不超过 100 字。\n"
)

_SOURCES_COMMON_RULES = (
    "你是「人设字段溯源标注器」。给定编号段落原文与**本批**待溯源的内容，"
    "输出**只含 sources 的严格 JSON**。\n"
    "硬规则：\n"
    "1. 只输出 {\"sources\": {...}}，无其它键、无 markdown 围栏、无解释文字。\n"
    "2. 键=字段点路径，值=证据段落编号数组（int）；编号必须来自输入的 [n]，"
    "宁缺勿错，绝不虚构编号。\n"
    "3. 没把握的键直接省略；**本批范围之外的键一律不要输出**。\n"
)

_SOURCES_IDENTITY_SYSTEM = _SOURCES_COMMON_RULES + (
    "\n本批只标注【身份与概览字段】。可用键：name / names / role / age / gender / "
    "background / personality.style / context.hobbies / context.emotional_triggers / "
    "tastes.likes / tastes.dislikes / tastes.opinions / boundaries.topics_to_avoid / "
    "selfie_scenes。**本批不要输出任何 specific_memories 相关键。**\n"
    "示例：\n"
    '{"sources": {"role": [2], "background": [1, 3], "tastes.likes": [40]}}\n'
)

_SOURCES_MEMORIES_SYSTEM = _SOURCES_COMMON_RULES + (
    "\n本批只标注【具体记忆】。摘要里每条记忆带 index（全局 0-based），键必须写成 "
    "\"context.specific_memories.<index>\"——index **原样使用、不要重新编号**，"
    "且只输出本批出现过的 index。\n"
    "实在无法逐条对应时，才用整组回落键 \"context.specific_memories\"。\n"
    "示例（本批 index 为 0、2 时）：\n"
    '{"sources": {"context.specific_memories.0": [12, 13], '
    '"context.specific_memories.2": [15]}}\n'
)

_RETRY_SUFFIX = (
    "\n\n【注意】你上一次的输出不是合法 JSON，无法解析。"
    "这次请只输出一个合法 JSON 对象：不要 markdown 围栏、不要注释、不要任何解释文字。"
)

# 重试预算（I2 线，2026-07-28 实测事故）：33k 文档 Call2 偶发**空响应**（len=0，
# 云端抖动、几乎不耗时），同一 prompt 单独复跑两次均成功 → 空响应不是 prompt 缺陷。
# 空响应**原样重试**（附 `_RETRY_SUFFIX` 会让模型以为上次输出了坏 JSON，反而诱发
# 解释性文字），坏 JSON 才附提示；两类可混合，总调用不超过 `_MAX_LLM_ATTEMPTS`。
_EMPTY_RETRIES = 2                    # 空响应最多再试 2 次（成本极低，硬失败代价大）
_BAD_JSON_RETRIES = 1                 # 坏 JSON 仍只重试 1 次（重跑贵且多半是能力问题）
_MAX_LLM_ATTEMPTS = 1 + _EMPTY_RETRIES
_EMPTY_BACKOFF_SEC = (1.5, 3.0)       # 第 n 次空响应重试前的退避


def _sleep(seconds: float) -> None:
    """退避睡眠单一出口（测试 monkeypatch 此处，单测不真 sleep）。"""
    time.sleep(seconds)

# JSON mode（provider 侧 ``response_format={"type":"json_object"}``）：生产 chat_fn 由
# 路由/CLI 用 ``AIClient.rewrite_cloud`` 构建，其签名只有 timeout_sec/max_tokens/
# temperature，**不透传 response_format** → 这里按签名探测、探不到就用旧三参调用，
# 保证对既有 chat_fn 完全向后兼容（ai_client 将来加可选 kwarg 即自动启用）。
_JSON_MODE_KWARG = "response_format"
_JSON_MODE_VALUE = {"type": "json_object"}


def _chat_fn_accepts_json_mode(chat_fn: Callable[..., str]) -> bool:
    """chat_fn 是否吃 ``response_format`` 关键字（显式形参或 ``**kwargs``）。"""
    try:
        sig = inspect.signature(chat_fn)
    except (TypeError, ValueError):
        return False
    for p in sig.parameters.values():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if p.name == _JSON_MODE_KWARG and p.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY):
            return True
    return False


def _chat_raw(chat_fn: Callable[..., str], system: str, user: str,
              json_mode: bool = False) -> str:
    """调 chat_fn 取原始字符串；``json_mode`` 仅在签名支持时透传（TypeError 再降级）。"""
    if json_mode and _chat_fn_accepts_json_mode(chat_fn):
        try:
            return chat_fn(system, user, LLM_TIMEOUT_SEC,
                           **{_JSON_MODE_KWARG: _JSON_MODE_VALUE}) or ""
        except TypeError:
            logger.debug("chat_fn 不接受 %s，降级为三参调用", _JSON_MODE_KWARG)
    return chat_fn(system, user, LLM_TIMEOUT_SEC) or ""


def _record_call(calls_meta: Optional[List[Dict[str, Any]]], stage: str,
                 t0: float, retried: bool, empty: bool,
                 attempts: int = 1, repaired: bool = False) -> None:
    """观测：把一次 LLM 调用（含重试）记进 ``extract_meta.calls``。

    ``attempts``＝实际调用次数、``repaired``＝是否走了 JSON 截断修复。
    """
    if calls_meta is None:
        return
    calls_meta.append({
        "stage": stage,
        "ms": int((time.perf_counter() - t0) * 1000),
        "retried": bool(retried),
        "empty": bool(empty),
        "attempts": int(attempts),
        "repaired": bool(repaired),
    })


def _truncation_warning(label: str) -> str:
    return f"{label}：LLM 输出被截断，已修复尾部（该字段可能不全）"


def _call_llm_json(chat_fn: Callable[[str, str, float], str],
                   system: str, user: str, label: str,
                   stage: str = "",
                   calls_meta: Optional[List[Dict[str, Any]]] = None,
                   warnings: Optional[List[str]] = None) -> dict:
    """单段抽取：调用 → 容错解析（含截断修复）→ **按失败类型分流**重试。

    - **空响应**（``raw`` 空/纯空白，云端偶发抖动）→ **原样重试**（不附
      ``_RETRY_SUFFIX``：模型什么都没输出时说「你上次输出的不是合法 JSON」是误导
      噪声），最多 ``_EMPTY_RETRIES`` 次，每次前按 ``_EMPTY_BACKOFF_SEC`` 退避；
    - **坏 JSON**（有内容但解析不了）→ 附 ``_RETRY_SUFFIX`` 重试 ``_BAD_JSON_RETRIES`` 次。

    两类可混合发生，总调用次数不超过 ``_MAX_LLM_ATTEMPTS``；用尽仍失败才抛异常。
    耗时/是否重试/是否见过空响应/调用次数/是否截断修复记进 ``calls_meta``。
    """
    t0 = time.perf_counter()
    attempts = 0
    empty_retries = 0
    bad_retries = 0
    saw_empty = False
    repaired = False
    lengths: List[int] = []
    cur_user = user
    try:
        while attempts < _MAX_LLM_ATTEMPTS:
            raw = chat_fn(system, cur_user, LLM_TIMEOUT_SEC) or ""
            attempts += 1
            lengths.append(len(raw))
            parsed, was_repaired = _parse_llm_json_ex(raw)
            if parsed is not None:
                repaired = was_repaired
                if was_repaired:
                    msg = _truncation_warning(label)
                    logger.warning("persona 抽取 %s", msg)
                    if warnings is not None:
                        warnings.append(msg)
                return parsed
            if not raw.strip():
                saw_empty = True
                if empty_retries >= _EMPTY_RETRIES:
                    break
                delay = _EMPTY_BACKOFF_SEC[
                    min(empty_retries, len(_EMPTY_BACKOFF_SEC) - 1)]
                empty_retries += 1
                logger.info(
                    "persona 抽取 %s 第 %d 次空响应（云端抖动），%.1fs 后原样重试",
                    label, attempts, delay)
                cur_user = user          # 原样：空响应 ≠ 坏 JSON，别给误导性话术
                _sleep(delay)
                continue
            if bad_retries >= _BAD_JSON_RETRIES:
                break
            bad_retries += 1
            logger.info("persona 抽取 %s 输出不可解析（len=%d），附提示重试",
                        label, len(raw))
            cur_user = user + _RETRY_SUFFIX
        raise ValueError(
            f"LLM 输出无法解析为 JSON（{label}，{attempts} 次调用后仍失败；"
            f"len={'/'.join(str(n) for n in lengths)}）")
    finally:
        _record_call(calls_meta, stage or label, t0, attempts > 1, saw_empty,
                     attempts=attempts, repaired=repaired)


def _merge_warnings(*parts: Any) -> List[str]:
    out: List[str] = []
    for p in parts:
        if isinstance(p, (list, tuple)):
            for item in p:
                s = _clean_str(item, 300)
                if s and s not in out:
                    out.append(s)
        elif isinstance(p, str) and p.strip():
            if p.strip() not in out:
                out.append(p.strip())
    return out


def _clean_sources(raw: Any, paragraphs_total: int,
                   memories_count: int = 0) -> Dict[str, List[int]]:
    """溯源标注护栏（宁缺勿错）：宿主非 dict → {}；键须非空 str、值须 list/tuple；
    编号须 int（bool 不算）且落在 ``1..paragraphs_total``——越界/非法剔除、
    去重保序；清洗后列表为空的键整个删除。

    逐条记忆键 ``context.specific_memories.N``（N 非负整数）仅当
    ``N < memories_count`` 时保留，越界丢弃；字段级 ``context.specific_memories``
    及其它点路径键仍接受。
    """
    if not isinstance(raw, dict):
        return {}
    mem_n = max(0, int(memories_count or 0))
    out: Dict[str, List[int]] = {}
    for key, nums in raw.items():
        field = key.strip() if isinstance(key, str) else ""
        if not field or not isinstance(nums, (list, tuple)):
            continue
        m = _MEMORY_SOURCE_KEY_RE.match(field)
        if m is not None:
            idx = int(m.group(1))
            if idx >= mem_n:
                continue
        clean: List[int] = []
        for n in nums:
            if isinstance(n, bool) or not isinstance(n, int):
                continue
            if 1 <= n <= paragraphs_total and n not in clean:
                clean.append(n)
        if clean:
            out[field] = clean
    return out


def _build_source_excerpts(sources: Dict[str, List[int]],
                           paragraphs: List[str]) -> Dict[str, str]:
    """被引用编号 → 段落原文（键=str(编号)，与 JSON 序列化口径一致）。

    仅收录 ``sources`` 里真实引用的编号；单段超 ``_EXCERPT_MAX_CHARS`` 截断加 …。
    调用方保证编号已经 ``_clean_sources`` 校验（1..len(paragraphs) 内）。
    """
    out: Dict[str, str] = {}
    for n in sorted({n for nums in sources.values() for n in nums}):
        para = paragraphs[n - 1]
        if len(para) > _EXCERPT_MAX_CHARS:
            para = para[:_EXCERPT_MAX_CHARS] + "…"
        out[str(n)] = para
    return out


def _persona_sources_digest(persona: dict, include_memories: bool = True) -> dict:
    """Call3 用人设摘要：只带需溯源的键（background 截 400 字，记忆带 index）。

    ``include_memories=False`` 供「身份与概览」批使用——记忆改由记忆批分批带上。
    """
    dig: Dict[str, Any] = {}
    for k in ("name", "names", "role", "age", "gender"):
        v = persona.get(k) if isinstance(persona, dict) else None
        if v not in (None, "", {}, []):
            dig[k] = v
    bg = _clean_str((persona or {}).get("background"), 400)
    if bg:
        dig["background"] = bg
    pers = (persona or {}).get("personality")
    if isinstance(pers, dict):
        style = _clean_str(pers.get("style"), 200)
        if style:
            dig["personality"] = {"style": style}
    ctx = (persona or {}).get("context")
    ctx_out: Dict[str, Any] = {}
    if isinstance(ctx, dict):
        mems = ctx.get("specific_memories") if include_memories else None
        if isinstance(mems, list) and mems:
            indexed = []
            for i, m in enumerate(mems):
                s = _clean_str(m, _MEMORY_MAX_CHARS)
                if s:
                    indexed.append({"index": i, "text": s})
            if indexed:
                ctx_out["specific_memories"] = indexed
        hobbies = _clean_str_list(ctx.get("hobbies"), _HOBBY_MAX_ITEMS, 40)
        if hobbies:
            ctx_out["hobbies"] = hobbies
        et = ctx.get("emotional_triggers")
        if isinstance(et, dict):
            et_clean = _clean_str_dict(
                et, ("positive", "negative", "deep_empathy"), 200)
            if et_clean:
                ctx_out["emotional_triggers"] = et_clean
    if ctx_out:
        dig["context"] = ctx_out
    tastes = (persona or {}).get("tastes")
    if isinstance(tastes, dict):
        t_out: Dict[str, List[str]] = {}
        for tk in ("likes", "dislikes", "opinions"):
            items = _clean_str_list(tastes.get(tk), _TASTE_MAX_ITEMS, 40)
            if items:
                t_out[tk] = items
        if t_out:
            dig["tastes"] = t_out
    bounds = (persona or {}).get("boundaries")
    if isinstance(bounds, dict):
        avoid = _clean_str_list(
            bounds.get("topics_to_avoid"), _AVOID_MAX_ITEMS, 40)
        if avoid:
            dig["boundaries"] = {"topics_to_avoid": avoid}
    scenes = _clean_str_list(
        (persona or {}).get("selfie_scenes"), _SCENE_MAX_ITEMS, 40)
    if scenes:
        dig["selfie_scenes"] = scenes
    return dig


# ── Call3 输入择优：关键词粗筛（纯函数）───────────────────────────────────────

_LATIN_TOKEN_RE = re.compile(r"[a-z][a-z0-9'’\-]+")
_NUM_TOKEN_RE = re.compile(r"\d{2,}")
_CJK_RUN_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7a3]+")


def _keyword_tokens(text: str) -> set:
    """粗筛用词袋：CJK bigram（单字成串时取该字）+ 拉丁词（小写）+ ≥2 位数字/年份。"""
    s = str(text or "")
    tokens: set = set()
    low = s.lower()
    tokens.update(_LATIN_TOKEN_RE.findall(low))
    tokens.update(_NUM_TOKEN_RE.findall(low))
    for run in _CJK_RUN_RE.findall(s):
        if len(run) == 1:
            tokens.add(run)
            continue
        for i in range(len(run) - 1):
            tokens.add(run[i:i + 2])
    return tokens


def preselect_paragraphs(paragraphs: Any, target_texts: Any,
                         limit: int = _SOURCES_PRESELECT_LIMIT
                         ) -> List[Tuple[int, str]]:
    """按与 ``target_texts`` 的关键词重合度挑段落（纯函数，供 Call3 每批择优输入）。

    返回 ``[(编号, 段落文本), ...]``——**编号是 1-based 原值**（粗筛不重编号，
    溯源引用与 ``source_excerpts`` 才对得上），按相关度降序、同分按原顺序；
    重合度为 0 的段落作为填充排在后面（仍受 ``limit`` 约束）。
    ``target_texts`` 为空/无有效词 → 直接返回前 ``limit`` 段（保持原顺序）。
    ``limit<=0`` 视为不限量。
    """
    items: List[Tuple[int, str]] = []
    for i, p in enumerate(paragraphs or []):
        s = str(p or "").strip()
        if s:
            items.append((i + 1, s))
    if not items:
        return []
    cap = len(items) if not limit or int(limit) <= 0 else int(limit)

    if isinstance(target_texts, str):
        target_texts = [target_texts]
    targets: set = set()
    for t in (target_texts or []):
        targets |= _keyword_tokens(t)
    if not targets:
        return items[:cap]

    scored = [(-len(targets & _keyword_tokens(text)), pos, num, text)
              for pos, (num, text) in enumerate(items)]
    scored.sort()
    return [(num, text) for _, _, num, text in scored[:cap]]


# ── Call3 分批溯源 ────────────────────────────────────────────────────────────

def _format_selected_paragraphs(selected: List[Tuple[int, str]], budget: int,
                                clip: int = _SOURCES_PARA_CLIP) -> str:
    """编号段落装入预算：每段截 ``clip``；超预算从**尾部**（最不相关）开始丢。"""
    lines: List[str] = []
    used = 0
    for num, text in selected:
        t = (text[:clip] + "…") if clip and len(text) > clip else text
        line = f"[{num}] {t}"
        add = len(line) + (1 if lines else 0)
        if budget > 0 and used + add > budget:
            break
        lines.append(line)
        used += add
    return "\n".join(lines)


def _build_sources_user(digest: dict, selected: List[Tuple[int, str]],
                        budget: int = _SOURCES_USER_BUDGET,
                        clip: int = _SOURCES_PARA_CLIP) -> str:
    """Call3 单批 user：本批待溯源摘要 JSON + 预算内编号段落（相关度高的在前）。"""
    header = (
        "【本批待溯源内容】\n"
        + json.dumps(digest or {}, ensure_ascii=False) + "\n\n"
        "【编号段落】（只用这些编号做溯源；相关度高的排在前面）\n"
    )
    body = _format_selected_paragraphs(
        selected, max(0, int(budget) - len(header)), clip)
    return header + body


def _digest_texts(obj: Any) -> List[str]:
    """摘要里的可检索文本（用于粗筛目标词）；跳过 ``index`` 这类结构性键。"""
    out: List[str] = []

    def _walk(v: Any) -> None:
        if isinstance(v, str):
            if v.strip():
                out.append(v)
        elif isinstance(v, dict):
            for k, x in v.items():
                if k == "index":
                    continue
                _walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                _walk(x)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(str(v))

    _walk(obj)
    return out


def _shrink_digest(obj: Any) -> Any:
    """空响应重试用：摘要文本再截短、列表只留前几项（保结构与 index 不变）。"""
    if isinstance(obj, str):
        return obj[:_SOURCES_RETRY_TEXT_CLIP]
    if isinstance(obj, dict):
        return {k: (v if k == "index" else _shrink_digest(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_shrink_digest(x) for x in obj[:_SOURCES_RETRY_LIST_ITEMS]]
    return obj


def _memories_digest(memories: List[str], indices: List[int]) -> dict:
    """记忆批摘要：只带本批几条，``index`` 为**全局 0-based 下标**（不重编号）。"""
    items = [{"index": i, "text": _clean_str(memories[i], _MEMORY_MAX_CHARS)}
             for i in indices
             if 0 <= i < len(memories) and _clean_str(memories[i])]
    return {"context": {"specific_memories": items}} if items else {}


def _plan_source_batches(persona: dict) -> List[Dict[str, Any]]:
    """切批：批 1=身份与概览字段，批 2..N=记忆每 ``_SOURCES_MEMORY_BATCH`` 条。

    摘要为空的批直接不建（省一次调用）。
    """
    batches: List[Dict[str, Any]] = []
    identity_digest = _persona_sources_digest(persona or {},
                                              include_memories=False)
    if identity_digest:
        batches.append({
            "kind": "identity",
            "label": "身份与概览",
            "stage": "sources_identity",
            "system": _SOURCES_IDENTITY_SYSTEM,
            "digest": identity_digest,
            "mem_indices": set(),
            "allow_group": False,
        })

    ctx = (persona or {}).get("context") if isinstance(persona, dict) else None
    mems = (ctx or {}).get("specific_memories") if isinstance(ctx, dict) else None
    mems = [m for m in mems] if isinstance(mems, list) else []
    for start in range(0, len(mems), _SOURCES_MEMORY_BATCH):
        indices = list(range(start, min(start + _SOURCES_MEMORY_BATCH, len(mems))))
        digest = _memories_digest(mems, indices)
        if not digest:
            continue
        batches.append({
            "kind": "memories",
            "label": f"记忆 {indices[0] + 1}-{indices[-1] + 1}",
            "stage": f"sources_memories_{start // _SOURCES_MEMORY_BATCH + 1}",
            "system": _SOURCES_MEMORIES_SYSTEM,
            "digest": digest,
            "mem_indices": set(indices),
            "allow_group": True,
        })
    return batches


def _filter_batch_sources(sources: Dict[str, List[int]],
                          batch: Dict[str, Any]) -> Dict[str, List[int]]:
    """把各批 sources 限回本批键域，保证合并时键不重叠（越界/串批的键丢弃）。"""
    allowed = batch.get("mem_indices") or set()
    out: Dict[str, List[int]] = {}
    for key, nums in sources.items():
        m = _MEMORY_SOURCE_KEY_RE.match(key)
        if m is not None:
            if int(m.group(1)) not in allowed:
                continue
        elif key == _MEMORY_GROUP_SOURCE_KEY:
            if not batch.get("allow_group"):
                continue
        elif batch.get("kind") == "memories":
            continue
        out[key] = nums
    return out


def _unwrap_sources(parsed: Any) -> Any:
    """取出批响应里的 sources 映射；容忍模型漏包一层（直接给 ``{"name": [1]}``）。"""
    if not isinstance(parsed, dict):
        return None
    inner = parsed.get("sources")
    if isinstance(inner, dict):
        return inner
    if "sources" in parsed:
        return None                      # 明确给了 sources 但类型不对 → 不猜
    if parsed and all(isinstance(v, (list, tuple)) for v in parsed.values()):
        return parsed
    return None


def _call_sources_batch(chat_fn: Callable[..., str], paragraphs: List[str],
                        batch: Dict[str, Any], index: int,
                        warnings: List[str],
                        calls_meta: Optional[List[Dict[str, Any]]] = None
                        ) -> Any:
    """单批溯源调用：粗筛输入 → 调用 → 解析；坏 JSON 重试、**空响应缩短输入**重试。

    任何失败都软降级返回 None（绝不抛），由调用方记「第 N 批溯源未生成」。
    """
    digest = batch["digest"]
    selected = preselect_paragraphs(
        paragraphs, _digest_texts(digest), _SOURCES_PRESELECT_LIMIT)
    user = _build_sources_user(digest, selected)
    t0 = time.perf_counter()
    retried = False
    empty = False
    attempts = 0
    repaired = False
    parsed: Optional[dict] = None
    try:
        raw = _chat_raw(chat_fn, batch["system"], user, json_mode=True)
        attempts += 1
        parsed, repaired = _parse_llm_json_ex(raw)
        if parsed is None:
            retried = True
            if not str(raw or "").strip():
                empty = True
                logger.info("persona 溯源第 %d 批（%s）首次空响应，缩短输入重试",
                            index, batch["label"])
                warnings.append(f"第 {index} 批溯源首次空响应，已缩短输入重试")
                short = selected[:max(1, len(selected) // 2)]
                user2 = _build_sources_user(
                    _shrink_digest(digest), short, clip=_SOURCES_RETRY_PARA_CLIP)
            else:
                logger.info(
                    "persona 溯源第 %d 批（%s）首次输出不可解析（len=%d），重试一次",
                    index, batch["label"], len(raw))
                user2 = user + _RETRY_SUFFIX
            raw2 = _chat_raw(chat_fn, batch["system"], user2, json_mode=True)
            attempts += 1
            parsed, repaired = _parse_llm_json_ex(raw2)
        if parsed is not None and repaired:
            msg = _truncation_warning(f"第 {index} 批溯源")
            logger.warning("persona 溯源 %s", msg)
            warnings.append(msg)
    except Exception:
        logger.info("persona 溯源第 %d 批调用失败，软降级为空", index, exc_info=True)
        parsed = None
    finally:
        _record_call(calls_meta, batch["stage"], t0, retried, empty,
                     attempts=attempts, repaired=repaired)
    return _unwrap_sources(parsed)


def _collect_sources(chat_fn: Callable[..., str], paragraphs: List[str],
                     persona: dict,
                     stage: Optional[Callable[[str, int], None]] = None,
                     calls_meta: Optional[List[Dict[str, Any]]] = None
                     ) -> Tuple[Dict[str, List[int]], List[str], Dict[str, int]]:
    """分批跑 Call3 并合并：某批空只丢该批，整体绝不失败。

    返回 ``(sources, warnings, {"total","ok","empty"})``；进度经 ``stage``
    汇报为 ``extract_sources`` 80→92。
    """
    ctx = (persona or {}).get("context") if isinstance(persona, dict) else None
    mems = (ctx or {}).get("specific_memories") if isinstance(ctx, dict) else None
    mem_count = len(mems) if isinstance(mems, list) else 0

    batches = _plan_source_batches(persona)
    total = len(batches)
    merged: Dict[str, List[int]] = {}
    warnings: List[str] = []
    ok = 0
    for i, batch in enumerate(batches):
        if stage:
            stage("extract_sources", 80 + int(round(12.0 * i / total)))
        raw_sources = _call_sources_batch(
            chat_fn, paragraphs, batch, i + 1, warnings, calls_meta)
        cleaned = _filter_batch_sources(
            _clean_sources(raw_sources, len(paragraphs),
                           memories_count=mem_count),
            batch)
        if not cleaned:
            warnings.append(f"第 {i + 1} 批溯源未生成（{batch['label']}）")
            continue
        ok += 1
        for key, nums in cleaned.items():
            merged.setdefault(key, nums)
    if stage and total:
        stage("extract_sources", 92)
    return merged, warnings, {"total": total, "ok": ok, "empty": total - ok}


def _extract_family(
    chat_fn: Callable[[str, str, float], str],
    user_doc: str,
    calls_meta: List[Dict[str, Any]],
) -> Tuple[Dict[str, str], List[str]]:
    """Call2b：亲属名册。失败只降级为 warning——名册是检索/prompt 的增益，不是主干。

    与 Call3 溯源同一容错口径：整次抽取绝不因为这一跳失败而失败。
    """
    call_warnings: List[str] = []
    try:
        part = _call_llm_json(chat_fn, _FAMILY_SYSTEM, user_doc, "亲属名册",
                              stage="family", calls_meta=calls_meta,
                              warnings=call_warnings)
    except Exception as e:  # noqa: BLE001
        logger.warning("persona 抽取 亲属名册失败，已跳过：%s", e)
        return {}, _merge_warnings(call_warnings, ["亲属名册抽取失败，已跳过"])
    fam = _clean_flat_dict(part.get("family"), _FAMILY_MAX_ITEMS,
                           _FAMILY_KEY_MAX_CHARS, _FAMILY_VALUE_MAX_CHARS)
    return fam, _merge_warnings(call_warnings, part.get("warnings"))


def run_extraction(text: str, chat_fn: Callable[[str, str, float], str],
                   on_stage: Optional[Callable[[str, int], None]] = None) -> dict:
    """多段式 LLM 抽取编排（同步阻塞；由任务线程/CLI 调用）。

    Call1 身份 → Call2 传记 → merge/clamp → Call3 **分批**轻量溯源 → finalize。
    ``chat_fn(system, user, timeout) -> str`` 由调用方注入（生产=AIClient.rewrite_cloud
    marshalling，测试=假函数）。``on_stage(stage, progress)`` 汇报进度。

    返回 ``{"persona", "suggested_id", "warnings", "coverage", "completeness",
    "sources", "source_excerpts", "paragraphs_total", "extract_meta"}``。
    Call1/Call2 按失败类型分流重试（空响应原样重试 ≤2 次带退避、坏 JSON 附提示
    重试 1 次，总调用 ≤3），用尽仍失败才抛异常（由任务注册表转 error 态）；
    Call3 每批独立容错，全批空 → ``sources={}`` + warning，
    **绝不因溯源让整次抽取失败**。
    """
    def _stage(stage: str, progress: int) -> None:
        if on_stage:
            try:
                on_stage(stage, progress)
            except Exception:
                pass

    t_start = time.perf_counter()
    calls_meta: List[Dict[str, Any]] = []
    call_warnings: List[str] = []      # 截断修复等「调用级」提示，汇入最终 warnings
    paragraphs = split_numbered_paragraphs(text)
    user_doc = "【人设包装文档全文】\n" + "\n".join(
        f"[{i}] {p}" for i, p in enumerate(paragraphs, 1))

    _stage("extract_identity", 10)
    identity_part = _call_llm_json(chat_fn, _IDENTITY_SYSTEM, user_doc, "身份与性格",
                                   stage="identity", calls_meta=calls_meta,
                                   warnings=call_warnings)

    _stage("extract_biography", 55)
    bio_part = _call_llm_json(chat_fn, _BIOGRAPHY_SYSTEM, user_doc, "传记与记忆",
                              stage="biography", calls_meta=calls_meta,
                              warnings=call_warnings)

    warnings = _merge_warnings(call_warnings,
                               identity_part.pop("warnings", None),
                               bio_part.pop("warnings", None))
    # Call1/Call2 若误带 sources，丢弃（溯源改由 Call3 独立产出）
    identity_part.pop("sources", None)
    bio_part.pop("sources", None)

    merged: Dict[str, Any] = dict(identity_part)
    merged.update(bio_part)          # 两段键不重叠，浅合并即可

    _stage("extract_family", 62)
    fam_part, fam_warnings = _extract_family(chat_fn, user_doc, calls_meta)
    warnings = _merge_warnings(warnings, fam_warnings)
    if fam_part:
        ctx_merged = merged.get("context")
        merged["context"] = dict(ctx_merged) if isinstance(ctx_merged, dict) else {}
        merged["context"]["family"] = fam_part

    # 产品默认（非文档来源）：真人身份硬锁
    merged["identity"] = {"deny_ai": True, "claim_human": True}
    warnings = _merge_warnings(
        warnings, ["已默认开启真人身份硬锁（identity.deny_ai / claim_human），非文档来源"])

    persona = clamp_persona(merged)

    sources, source_warnings, batch_stats = _collect_sources(
        chat_fn, paragraphs, persona, stage=_stage, calls_meta=calls_meta)
    warnings = _merge_warnings(warnings, source_warnings)
    if not sources:
        warnings = _merge_warnings(warnings, ["溯源标注未生成"])

    _stage("finalize", 95)
    ctx = persona.get("context") or {}
    coverage = {
        "identity": bool(persona.get("name") or persona.get("names")
                         or persona.get("role")),
        "personality": bool(persona.get("personality")),
        "background": bool(persona.get("background")),
        "memories": bool(ctx.get("specific_memories")),
        "tastes": bool(persona.get("tastes")),
        "scenes": bool(persona.get("selfie_scenes")),
    }

    try:
        from src.utils.persona_completeness import persona_completeness
        completeness = persona_completeness(persona)
    except Exception:
        completeness = {"score": -1}

    memories = ctx.get("specific_memories") or []
    mem_total = len(memories) if isinstance(memories, list) else 0
    mem_sourced = sum(1 for i in range(mem_total)
                      if f"context.specific_memories.{i}" in sources)

    return {
        "persona": persona,
        "suggested_id": suggested_profile_id(persona),
        "warnings": warnings,
        "coverage": coverage,
        "completeness": completeness,
        "sources": sources,
        "source_excerpts": _build_source_excerpts(sources, paragraphs),
        "paragraphs_total": len(paragraphs),
        "extract_meta": {
            "calls": calls_meta,
            "sources_batches": batch_stats,
            "memories_sourced": mem_sourced,
            "memories_total": mem_total,
            "total_ms": int((time.perf_counter() - t_start) * 1000),
        },
    }


# ── 进程级任务注册表（提交→后台线程→轮询）────────────────────────────────────

JOB_TTL_SEC = 30 * 60             # 完结/滞留任务 30 分钟后清理
MAX_RUNNING_JOBS = 3              # 同时在跑上限（LLM 长调用，防挤爆）

_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


def _purge_expired_locked(now: float) -> None:
    dead = [k for k, j in _JOBS.items()
            if now - float(j.get("created_ts") or 0.0) > JOB_TTL_SEC]
    for k in dead:
        _JOBS.pop(k, None)


def _set_job(job_id: str, **fields: Any) -> None:
    with _JOBS_LOCK:
        j = _JOBS.get(job_id)
        if j is not None:
            j.update(fields)


def _run_job(job_id: str,
             runner: Callable[[Callable[[str, int], None]], dict]) -> None:
    _set_job(job_id, status="running", stage="parsing", progress=5)

    def _stage(stage: str, progress: int) -> None:
        _set_job(job_id, stage=stage, progress=int(progress))

    try:
        result = runner(_stage)
        _set_job(job_id, status="done", stage="finalize", progress=100,
                 result=result)
    except Exception as exc:
        logger.warning("persona 后台任务 %s 失败: %s", job_id, exc)
        _set_job(job_id, status="error",
                 error=str(exc) or type(exc).__name__)


def create_job_runner(
        runner: Callable[[Callable[[str, int], None]], dict]) -> Optional[str]:
    """通用后台任务入口（供兄弟线复用，如人设一致性考题）。

    ``runner(on_stage) -> result dict`` 在后台守护线程执行；与 ``create_job``
    共用同一注册表/TTL/状态机（queued|running|done|error），``on_stage(stage,
    progress)`` 直写任务快照。满载（queued/running ≥ ``MAX_RUNNING_JOBS``）
    返回 None（调用方转 429）。
    """
    now = time.time()
    with _JOBS_LOCK:
        _purge_expired_locked(now)
        active = sum(1 for j in _JOBS.values()
                     if j.get("status") in ("queued", "running"))
        if active >= MAX_RUNNING_JOBS:
            return None
        job_id = uuid.uuid4().hex[:12]
        _JOBS[job_id] = {
            "id": job_id, "status": "queued", "stage": "parsing",
            "progress": 0, "result": None, "error": "", "created_ts": now,
        }
    threading.Thread(
        target=_run_job, args=(job_id, runner),
        name=f"persona-doc-import-{job_id}", daemon=True,
    ).start()
    return job_id


def create_job(text: str,
               chat_fn: Callable[[str, str, float], str]) -> Optional[str]:
    """建后台抽取任务（``create_job_runner`` 的抽取特化，对外签名/行为不变）。
    同时 queued/running ≥ ``MAX_RUNNING_JOBS`` → None（调用方转 429）。"""
    return create_job_runner(
        lambda on_stage: run_extraction(text, chat_fn, on_stage=on_stage))


def get_job(job_id: str) -> Optional[dict]:
    """任务快照（浅拷贝）；未知/已过期 → None。"""
    now = time.time()
    with _JOBS_LOCK:
        _purge_expired_locked(now)
        j = _JOBS.get(str(job_id or ""))
        return dict(j) if j is not None else None


def reset_jobs() -> None:
    """清空任务注册表（测试隔离用）。"""
    with _JOBS_LOCK:
        _JOBS.clear()


# ── CLI（离线冒烟；不要在 pytest 里跑——会真调 LLM）───────────────────────────

def _build_cli_chat_fn() -> Tuple[Callable[[str, str, float], str], Callable[[], None]]:
    """CLI 用：真实配置 + AIClient，专用后台事件循环线程承载 async 调用。

    底层复用 ``AIClient.rewrite_cloud``（system+user → 原始回复的低层工具入口：
    不跑语言守卫/QualityTracker、不碰主链熔断、成本单列 tier=tool——与
    ``voice_colloquial_llm`` 同一模式；勿用 generate_reply 全链）。
    """
    import asyncio

    from src.ai.ai_client import AIClient
    from src.utils.config_manager import ConfigManager

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True,
                     name="persona-doc-import-cli-loop").start()

    def _call(coro: Any, timeout: float) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    cm = ConfigManager()
    _call(cm.load(), 60)
    client = AIClient(cm)
    _call(client.initialize(), 120)

    def chat_fn(system: str, user: str, timeout: float) -> str:
        out = _call(
            client.rewrite_cloud(system, user, timeout_sec=timeout,
                                 max_tokens=LLM_MAX_TOKENS,
                                 temperature=LLM_TEMPERATURE),
            float(timeout) + 30.0)
        return out or ""

    def shutdown() -> None:
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass

    return chat_fn, shutdown


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys
    from pathlib import Path

    try:
        sys.stdout.reconfigure(encoding="utf-8")   # 防 Windows GBK 控制台崩
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="「从文档创建人设」离线冒烟：docx/txt → LLM 抽取 canonical 人设 JSON")
    ap.add_argument("--file", required=True, help="人设包装文档（.docx / .txt）")
    ap.add_argument("--out", default="tmp_extract.json", help="抽取结果 JSON 落盘路径")
    args = ap.parse_args(argv)

    path = Path(args.file)
    if not path.exists():
        print(f"[persona-doc-import] 文件不存在: {path}")
        return 2
    data = path.read_bytes()
    if path.suffix.lower() == ".docx":
        raw = extract_docx_text(data)
    else:
        raw = data.decode("utf-8", errors="replace")
    text, truncated = prepare_text(raw)
    if not text:
        print("[persona-doc-import] 未提取到任何文本")
        return 2
    print(f"[persona-doc-import] chars={len(text)} paragraphs={count_paragraphs(text)}"
          f" truncated={truncated}")

    chat_fn, shutdown = _build_cli_chat_fn()
    t0 = time.time()
    try:
        result = run_extraction(
            text, chat_fn,
            on_stage=lambda st, pg: print(f"  … {st} ({pg}%)"))
    finally:
        shutdown()

    persona = result["persona"]
    print(f"[persona-doc-import] 完成，耗时 {time.time() - t0:.1f}s")
    print(f"  suggested_id : {result['suggested_id']}")
    print(f"  completeness : {result['completeness'].get('score')}")
    print(f"  coverage     : {result['coverage']}")
    meta = result.get("extract_meta") or {}
    batches = meta.get("sources_batches") or {}
    memories = (persona.get("context") or {}).get("specific_memories") or []
    mem_n = len(memories) if isinstance(memories, list) else 0
    print(f"  memories     : {mem_n} 条")
    print(f"  sources      : {len(result['sources'])} 字段键（含逐条记忆）；"
          f"记忆逐条 {meta.get('memories_sourced', 0)}/"
          f"{meta.get('memories_total', mem_n)}；"
          f"引用 {len(result['source_excerpts'])} 段"
          f"（全文 {result['paragraphs_total']} 段）")
    print(f"  溯源批次     : {batches.get('ok', 0)}/{batches.get('total', 0)} 出结果"
          f"（空 {batches.get('empty', 0)}）")
    print(f"  LLM 调用     : {len(meta.get('calls') or [])} 次，"
          f"总耗时 {meta.get('total_ms', 0) / 1000.0:.1f}s")
    for call in meta.get("calls") or []:
        print(f"    - {str(call.get('stage')):<22}"
              f" {call.get('ms', 0) / 1000.0:6.1f}s"
              f" attempts={call.get('attempts')}"
              f" retried={call.get('retried')} empty={call.get('empty')}"
              f" repaired={call.get('repaired')}")
    if result["warnings"]:
        print("  warnings:")
        for w in result["warnings"]:
            print(f"    - {w}")
    out_path = Path(args.out)
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[persona-doc-import] 结果已写 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
