# -*- coding: utf-8 -*-
"""入站图片「是谁 / 是什么」——视觉身份层（#333 EREM2H · #332 KEWMB6，2026-09-17）。

事故（skuio 坐席机 1.0.86，WhatsApp Nori × Cameron 00:56 / 00:59）：客户连发自拍，识图
caption 只有「一位红发、有胡须的男性…并非聊天截图」，系统里没有任何「这是客户本人」
的判断，也没有跨轮的照片记忆；LLM 顺着历史占位符演成「我也发了一张」。三个缺口：

1. **识图 prompt 面向截图/OCR**（``vision_client`` 旧默认「描述与聊天/文字相关的内容」），
   人物照只落一句外貌，没有主体/人数/是否自拍这些能被下游消费的字段。
2. **人的身份从未被判断**：人脸嵌入（``src/ai/face_fidelity``，insightface ArcFace）只服务
   人设 LoRA 保真；相册 ``face_ref`` 只给 PuLID 生图锁脸；客户侧零视觉实体。
3. **记忆只有 24h 文字便签**（``image_observation`` KV，最多 3 条），caption 又被
   ``strip_media_desc`` 刻意排除在 episodic 抽取之外。

本模块是这条线的**单一事实源**：

- :data:`INBOUND_IMAGE_PROMPT`：识图 prompt v3。保留 v2 的首行「类型=A|B|C」契约
  （``media_enrich.parse_desc_type`` / 前端徽标 / stats 都认它），C 类改为**人物导向**：
  「主体：」一个词 + 「人物：」人数/性别年龄段/外貌/是否自拍 + 「场景：」，并钉死
  「不要猜姓名、身份、与聊天对象的关系」。
- :func:`parse_caption_fields`：从 caption 里抠 ``subject / people / scene / kind`` 字段，
  容忍 v1/v2 旧产出（缺字段回空串，不抛）。
- :func:`classify_identity` / :func:`identity_note`：P1 身份判定与注入措辞（人脸相似度 →
  ``persona / customer_self / known / unknown`` 四类）。相似度阈值沿用 ``face_fidelity``
  的 ArcFace 刻度（同人 ≥0.40 稳、<0.28 基本不同人）。**AI 推断只是 observation**，
  客户亲口确认才升事实（``source=user_confirmed`` 永远压过 ``ai_inferred``）。

纯函数、stdlib-only；任何异常回空 / 回 ``unknown``，绝不阻断识图或拟稿。
门禁 ``tests/test_visual_identity.py``。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ── 识图 prompt v3（单一事实源：vision_client 默认 / desktop 种子 / 实例 overlay 同文）──
INBOUND_IMAGE_PROMPT = (
    "请先判断图片类型，再按对应规则输出，不要复述规则。输出的第一行固定为「类型=A」「类型=B」"
    "或「类型=C」（按你判断的类型），从第二行起输出内容。"
    "A类＝票据/订单/证件/表单/转账或支付凭证：把画面里的文字字段逐字抄录（尤其姓名/Name、日期、"
    "单号、金额、座位、电话等小字，不要总结省略；看不清的字段标注「不清晰」，不要猜），末尾用一句话"
    "说明单据类型（以画面文字为准，不要臆测交通工具或商家）。"
    "B类＝聊天记录截图：不要逐字抄录气泡，只概括「谁在和谁聊、主题是什么、最后一条说了什么」，"
    "最多逐字引用最关键的一两句（如金额、账号、时间承诺）。"
    "C类＝其他所有图片（人物/自拍/宠物/食物/房屋/车辆/风景/物品/界面等）：先写「主体：」后接一个词"
    "（自拍/单人/多人/宠物/食物/房屋/车辆/风景/物品/其他）。画面有人时写「人物：」——人数；每人的"
    "大致性别与年龄段；显著外貌特征（发色、发型、胡须、眼镜、明显服饰）；表情与姿态；是否对着镜头"
    "自拍。再写「场景：」——地点类型、主要物件与数量（按看到的写，不夸大）、光线或时段线索。"
    "不要猜测人物的姓名、身份或与聊天对象的关系，不要把画面里的地物当成对方的住处或所在地；"
    "画面上零散文字（键帽、包装印字、界面按钮、菜单项）不抄录，除非文字本身就是主体（横幅、告示牌）。"
    "回答用中文（抄录的字段保留原文语言），B/C 类总长不超过 160 字；不确定就写「不确定」，只写看到的。"
)

#: prompt 必含的关键子句（门禁与「overlay 是否已同步 v3」自检共用）
PROMPT_REQUIRED_CLAUSES: Tuple[str, ...] = (
    "类型=A", "类型=B", "类型=C", "主体：", "人物：", "场景：",
    "是否对着镜头", "不要猜测人物的姓名、身份或与聊天对象的关系",
)

# ── caption 字段解析 ────────────────────────────────────────────────────────

_SUBJECT_WORDS = ("自拍", "单人", "多人", "宠物", "食物", "房屋", "车辆", "风景", "物品", "其他")
_FIELD_RE = re.compile(
    r"(?P<key>主体|人物|场景|文字)\s*[:：]\s*(?P<val>.*?)(?=(?:主体|人物|场景|文字)\s*[:：]|$)",
    re.S,
)
_TYPE_RE = re.compile(r"^\s*(?:类型|type)\s*[=＝:：]\s*([ABCabc])\s*[。．.、]?\s*", re.I)
_SELFIE_RE = re.compile(r"自拍|selfie", re.I)
_NOT_SELFIE_RE = re.compile(r"(?:不是|非|并非|並非|不像|没有|沒有|未)\s*(?:在)?\s*(?:对着镜头)?\s*自拍|selfie\s*[:：]?\s*(?:no|否)", re.I)
_PERSON_COUNT_RE = re.compile(r"(?:人数|人數)\s*[:：]?\s*(\d+|[一二两兩三四五六七八九十]+)|(\d+|[一二两兩三四五六七八九十]+)\s*(?:个|位|名)?\s*(?:人|男|女|男性|女性|男子|女子|男人|女人|孩子|小孩|儿童|老人)")
_NO_PERSON_RE = re.compile(r"(?:人物|人)\s*[:：]?\s*(?:无|無|没有|沒有|不可见|不可見|0)|画面(?:中|里)?(?:没有|沒有|无|無)\s*人")
_CN_NUM = {"一": 1, "二": 2, "两": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _to_int(tok: str) -> int:
    tok = str(tok or "").strip()
    if tok.isdigit():
        return int(tok)
    return _CN_NUM.get(tok, 0)


def parse_caption_fields(desc: Any) -> Dict[str, Any]:
    """识图 caption → ``{kind, subject, people, scene, text, person_count, is_selfie, has_person}``。

    容忍：v3「主体：/人物：/场景：」；v2 首行「类型=X」+ 自由文本；v1 纯自由文本（字段回空，
    ``has_person`` 靠关键词兜底）。纯函数，绝不抛。
    """
    out: Dict[str, Any] = {
        "kind": "", "subject": "", "people": "", "scene": "", "text": "",
        "person_count": 0, "is_selfie": False, "has_person": False,
    }
    try:
        t = " ".join(str(desc or "").replace("\r", "\n").split("\n")).strip()
        if not t:
            return out
        m = _TYPE_RE.match(t)
        if m:
            out["kind"] = m.group(1).upper()
            t = t[m.end():].strip()
        for fm in _FIELD_RE.finditer(t):
            key, val = fm.group("key"), " ".join(fm.group("val").split()).strip(" ;；，,。")
            if key == "主体" and not out["subject"]:
                out["subject"] = val
            elif key == "人物" and not out["people"]:
                out["people"] = val
            elif key == "场景" and not out["scene"]:
                out["scene"] = val
            elif key == "文字" and not out["text"]:
                out["text"] = val
        subj = out["subject"]
        for w in _SUBJECT_WORDS:
            if w in subj:
                out["subject"] = w
                break
        people = out["people"] or ("" if out["subject"] else t)
        if out["subject"] in ("自拍", "单人", "多人"):
            out["has_person"] = True
        if people and not _NO_PERSON_RE.search(people):
            pm = _PERSON_COUNT_RE.search(people)
            if pm:
                out["person_count"] = _to_int(pm.group(1) or pm.group(2))
                out["has_person"] = True
            elif re.search(r"男|女|人物|自拍|selfie|man|woman|person|people", people, re.I):
                out["has_person"] = True
        if out["has_person"] and not out["person_count"]:
            out["person_count"] = 2 if out["subject"] == "多人" else 1
        selfie_src = f"{out['subject']} {out['people']}" if (out["subject"] or out["people"]) else t
        if _SELFIE_RE.search(selfie_src) and not _NOT_SELFIE_RE.search(selfie_src):
            out["is_selfie"] = True
            out["has_person"] = True
            out["person_count"] = out["person_count"] or 1
        if out["subject"] == "自拍":
            out["is_selfie"] = True
    except Exception:
        return out
    return out


# ── 身份判定（P1；相似度由人脸嵌入层给，这里只做刻度与措辞）────────────────────
#: ArcFace（antelopev2 glintr100）余弦刻度——2026-09-17 176 边车实测校准：
#: 同图增广（翻转/JPEG60/裁 80%/缩 480）0.935~0.971；同人设 PuLID 相册两两 0.51~0.65；
#: **不同人设生成脸两两最高 0.455**（生成「大众美颜脸」互相偏像，比真人 impostor 高）。
#: 故判同一人取 0.50（face_fidelity 注「真实同人照对常 ≥0.5」），0.35~0.50 只算疑似。
#: 刻度 provisional：攒够真实入站样本后用 face_fidelity.calibrate_fidelity_floor 同款纪律收紧。
MATCH_THRESHOLD = 0.50      #: ≥ 判同一人
AMBIGUOUS_THRESHOLD = 0.35  #: [0.35, 0.50) 疑似，不下结论（可据此追问「是不是你」）
LABELS = ("persona", "customer_self", "known", "unknown", "no_face")


def classify_identity(
    scores: Dict[str, float], *, has_face: bool = True,
    match: float = MATCH_THRESHOLD, ambiguous: float = AMBIGUOUS_THRESHOLD,
) -> Tuple[str, str, float]:
    """``scores``＝{候选身份: 余弦}（键：``persona`` / ``customer_self`` / 其它=已确认关系人 label）
    → ``(label, matched_key, score)``。无脸 → ``no_face``；最高分 ≥match → 该键；
    在 [ambiguous, match) → ``unknown``（疑似但不下结论）；否则 ``unknown``。纯函数。"""
    if not has_face:
        return "no_face", "", 0.0
    best_k, best_s = "", -1.0
    for k, v in (scores or {}).items():
        try:
            s = float(v)
        except (TypeError, ValueError):
            continue
        if s > best_s:
            best_k, best_s = str(k), s
    if not best_k or best_s < match:
        return "unknown", best_k if best_s >= ambiguous else "", max(best_s, 0.0)
    if best_k == "persona":
        return "persona", best_k, best_s
    if best_k == "customer_self":
        return "customer_self", best_k, best_s
    return "known", best_k, best_s


def identity_note(label: str, *, matched: str = "", confirmed: bool = False,
                  relation: str = "", who: str = "TA") -> str:
    """注入回复 prompt 的一句身份说明（带外、无方括号标签）。空标签 → ''。"""
    lab = str(label or "")
    if lab == "persona":
        return ("图中人物与你（人设）的相貌一致——这是你自己的照片（多半是我方此前发过的），"
                "不要当成对方发来的自拍去评价。")
    if lab == "customer_self":
        if confirmed:
            return (f"图中人物与{who}此前确认过的自拍是同一个人（已确认）"
                    f"——这是{who}本人，可以自然评价，不要问「这是谁」。")
        return (f"图中大概率是{who}本人的自拍（推断，{who}还没亲口确认）——可以按本人自然接话、"
                f"评价照片，但别把「是你」说成定论；如果拿不准就自然问一句「这是你吗」。")
    if lab == "known":
        rel = relation or matched or "已知关系人"
        return (f"图中人物是{who}此前确认过的「{rel}」（已确认）——按这个关系接话，不要重新猜。")
    if lab == "unknown":
        return ("图中有人，但系统无法确定是谁（不是你，也没有与已知的人匹配）——"
                "**不要猜**这是谁或与对方是什么关系；可以自然地问一句「这是你吗 / 这是谁呀」，"
                "对方确认后再记。")
    if lab == "no_face":
        return ""
    return ""


def observation_summary(fields: Dict[str, Any], *, max_chars: int = 60) -> str:
    """caption 字段 → 一行短摘要（写入观察记录 / 日志用）。"""
    subj = str(fields.get("subject") or "")
    ppl = str(fields.get("people") or "")
    scn = str(fields.get("scene") or "")
    parts = [p for p in (subj, ppl, scn) if p]
    s = "；".join(parts)
    if len(s) > max_chars:
        s = s[: max_chars - 1] + "…"
    return s


__all__ = [
    "INBOUND_IMAGE_PROMPT", "PROMPT_REQUIRED_CLAUSES",
    "parse_caption_fields", "classify_identity", "identity_note", "observation_summary",
    "MATCH_THRESHOLD", "AMBIGUOUS_THRESHOLD", "LABELS",
]
