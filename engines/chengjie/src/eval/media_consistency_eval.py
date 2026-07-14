# -*- coding: utf-8 -*-
"""图文一致性评测（Phase20，平移 ``bazi_reading_eval`` 模式：确定性校验器 +
内置事故语料回归网 + 可选真流量样本）。

守的是「发媒体链的端到端诚实」——生成侧已有五层防线（拟稿 hint/photo_directive/
承诺守卫/异步兑现/llm_caption），本模块是**验收网**：任何一层回归漂移（prompt 改坏/
守卫误放行/配文指令失效），这里的确定性断言先红。

四类硬违规（宁漏勿误——只断言确定性可检的自相矛盾，模糊表述一律放过）：

- ``deny_with_photo``   附图却否认/拖延（"等我去拍/发不了照片" + photo_sent=True）
  ——llm_caption「照片已发出」指令失效的实锤。
- ``claim_without_photo`` 无图却断言已发（"照片来啦/刚拍的给你/here's a pic" +
  photo_sent=False）——承诺守卫「assertion lie」漏网的实锤。
- ``scene_mismatch``    配文**强断言**自己身处 X 场景（"我在健身房/正在海边"），
  而照片实际场景是**另一类**——图文同源（Phase18）失效的实锤。
  只认「我在/正在/我现在在 + 他类场景词」的强断言；聊愿望（"改天陪你去海边"）、
  提问（"你在海边吗"）不算。
- ``time_mismatch``     场景短语的时间词与发送时刻硬冲突（凌晨发 afternoon light）
  ——复用 Phase19 ``scene_conflicts_with_hour`` 词表做端到端验收。

CLI：``python -m scripts.run_eval --media-consistency [--json]``；
门禁：``tests/test_media_consistency_eval.py``（含探测器有效性自证——
篡改金标必 FAIL，评测不是摆设）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

# ── 违规检测词表（zh + en，宣告形/断言形；与 outbound_promise_guard 同族但更窄——
# 这里按 photo_sent 分支判断，无需 promise/offer 的完整排除面）────────────────────

# 附图时的否认/拖延（图都发出去了还说"等我去拍"）
_DENY_WITH_PHOTO = [re.compile(p, re.IGNORECASE) for p in (
    r"等\s*(?:我|人家)\s*(?:去)?\s*拍",
    r"(?:我|人家)\s*(?:这就|馬上|马上|现在|現在)\s*去\s*拍",
    r"发\s*不\s*了\s*(?:照片|图|圖)|發\s*不\s*了",
    r"不\s*能\s*(?:发|發)\s*(?:照片|图|圖)",
    r"没\s*法\s*(?:发|發)|沒\s*法\s*(?:发|發)",
    r"\blet\s+me\s+(?:go\s+)?take\s+(?:a\s+)?(?:photo|pic|selfie)",
    r"\bi(?:'|’)?ll\s+(?:go\s+)?take\s+(?:a\s+)?(?:photo|pic|selfie)",
    r"\bcan(?:'|’)?t\s+send\s+(?:photos?|pics?)",
    r"\bwait\s+(?:for\s+me\s+)?to\s+take\b",
)]

# 无图时的"已发"断言（图根本没到还说"来啦"）。过去指涉（"上次刚拍的那张"）在
# check 里统一排除，正则本身保持宣告形窄口径。
_CLAIM_WITHOUT_PHOTO = [re.compile(p, re.IGNORECASE) for p in (
    r"(?:照片|自拍)\s*(?:来|來)\s*(?:啦|了|咯|喽)",
    r"[刚剛]\s*拍\s*(?:的|好)[^\n，,]{0,4}[，,]?\s*(?:发|發|给|給)\s*你",
    r"(?:发|發)\s*(?:给|給)?\s*你\s*(?:啦|了)\s*[～~!！。]?\s*$",
    r"看\s*(?:一)?\s*下\s*(?:我[刚剛]拍)",
    r"\bhere(?:'|’)?s\s+(?:a|an|my|the|one)?\s*(?:photo|pic(?:ture)?|selfie)",
    r"\bjust\s+(?:took|sent)\s+(?:this|it|one|a\s+(?:photo|pic|selfie))",
    r"\b(?:photo|pic|selfie)\s+(?:is\s+)?(?:sent|on\s+(?:the|its)\s+way)",
)]

# 过去指涉排除（谈论以前发过的照片 ≠ 断言本条附了图）
_PAST_REF_RE = re.compile(
    r"上次|之前|昨天|前几天|前幾天|那[张張]|\blast\s+time\b|\bearlier\b")

# 强断言"我在 X"（配文声称身处某场景类；与照片实际场景类冲突才违规）
_SELF_LOCATION_RE = re.compile(
    r"(?:我|人家)\s*(?:现在|現在|正)?\s*在\s*([^\s，。！？,!?~～]{1,8})"
)

# 场景类词表（zh 断言词 → 类 key；en 场景短语关键词 → 类 key）。
# 与 companion_selfie._REQUESTED_SCENE_MAP 语义对齐但独立维护——评测器不 import
# 生产词表（防"词表改坏导致评测跟着瞎"，评测要做独立事实源）。
_SCENE_CLASSES: Dict[str, Dict[str, Sequence[str]]] = {
    "beach":   {"zh": ("海边", "海邊", "沙滩", "沙灘"),
                "en": ("beach", "seaside", "sea in the background")},
    "cafe":    {"zh": ("咖啡厅", "咖啡廳", "咖啡店", "咖啡馆", "咖啡館"),
                "en": ("cafe", "coffee shop", "coffee cup")},
    "office":  {"zh": ("办公室", "辦公室", "公司"),
                "en": ("office",)},
    "gym":     {"zh": ("健身房",),
                "en": ("gym", "workout")},
    "kitchen": {"zh": ("厨房", "廚房"),
                "en": ("kitchen", "cooking")},
    "park":    {"zh": ("公园", "公園"),
                "en": ("park", "outdoors in a park")},
    "home":    {"zh": ("家里", "家裡", "沙发", "沙發"),
                "en": ("at home", "on the couch", "dorm", "cozy room")},
    "bedroom": {"zh": ("卧室", "臥室", "床上"),
                "en": ("bedroom",)},
    "library": {"zh": ("图书馆", "圖書館", "书店", "書店"),
                "en": ("library", "bookstore")},
    "street":  {"zh": ("街上", "街头", "街頭"),
                "en": ("city street", "street style")},
}

# home 域内互容（"在家" vs bedroom/couch 不算冲突——卧室也是家）
_SCENE_COMPAT = {
    ("home", "bedroom"), ("bedroom", "home"),
}


def _scene_class_of_phrase(scene: str) -> str:
    """英文场景短语 → 场景类 key（识别不出返回 ''=不参与冲突判定）。"""
    s = str(scene or "").lower()
    if not s:
        return ""
    for key, table in _SCENE_CLASSES.items():
        for w in table["en"]:
            if w in s:
                return key
    return ""


def _claimed_scene_classes(text: str) -> List[str]:
    """文本里「我在 X」强断言指向的场景类列表（判不出为空；愿望/提问不命中）。"""
    out: List[str] = []
    for m in _SELF_LOCATION_RE.finditer(str(text or "")):
        frag = m.group(1)
        for key, table in _SCENE_CLASSES.items():
            if any(w in frag for w in table["zh"]) and key not in out:
                out.append(key)
    return out


def check_media_consistency(
    text: str,
    *,
    photo_sent: bool,
    scene: str = "",
    hour: Optional[int] = None,
) -> Dict[str, Any]:
    """单样本图文一致性判定（纯函数）。返回 ``{"ok": bool, "violations": [...]}``。

    ``text``＝随图配文或无图时的出站文本；``photo_sent``＝该消息是否真附了照片；
    ``scene``＝照片实际生成场景（英文短语，可空=相册现成图）；``hour``＝发送时刻
    （0-23，可空=跳过时间冲突检查）。
    """
    violations: List[str] = []
    t = str(text or "")
    if photo_sent:
        for rx in _DENY_WITH_PHOTO:
            if rx.search(t):
                violations.append("deny_with_photo")
                break
    elif not _PAST_REF_RE.search(t):  # 谈论以前的照片 ≠ 断言本条附图
        for rx in _CLAIM_WITHOUT_PHOTO:
            if rx.search(t):
                violations.append("claim_without_photo")
                break
    if photo_sent and scene:
        actual = _scene_class_of_phrase(scene)
        if actual:
            for claimed in _claimed_scene_classes(t):
                if claimed != actual and (claimed, actual) not in _SCENE_COMPAT:
                    violations.append("scene_mismatch")
                    break
    if photo_sent and scene and hour is not None:
        try:
            from src.ai.companion_selfie import scene_conflicts_with_hour
            if scene_conflicts_with_hour(scene, int(hour)):
                violations.append("time_mismatch")
        except Exception:
            pass
    return {"ok": not violations, "violations": violations}


# ── 内置金标语料（真实事故 + 边界反例；评测器的常驻回归网）─────────────────────
# expect_ok=False 的都是实录/实锤级违规；expect_ok=True 的是易误伤的合法表达。
_GOLDEN_SAMPLES: List[Dict[str, Any]] = [
    # —— 违规正例（必须抓住）——
    {"id": "deny1", "text": "等我现在去拍一张？等我一下～", "photo_sent": True,
     "scene": "in a cozy cafe", "expect_ok": False},          # 实录事故原文
    {"id": "deny2", "text": "I can't send photos here sorry", "photo_sent": True,
     "scene": "", "expect_ok": False},
    {"id": "claim1", "text": "自拍来啦！好看吗", "photo_sent": False,
     "scene": "", "expect_ok": False},                        # 无图硬说到了
    {"id": "claim2", "text": "here's a selfie for you babe", "photo_sent": False,
     "scene": "", "expect_ok": False},
    {"id": "scene1", "text": "我在健身房呢，刚练完好累～", "photo_sent": True,
     "scene": "at the beach, sea in the background", "expect_ok": False},
    {"id": "time1", "text": "刚拍的～", "photo_sent": True,
     "scene": "campus walkway, afternoon light", "hour": 3, "expect_ok": False},
    # —— 合法反例（不许误伤）——
    {"id": "ok1", "text": "这是刚拍的，给你看～喜欢吗？", "photo_sent": True,
     "scene": "in a cozy cafe", "expect_ok": True},           # 附图说刚拍=真话
    {"id": "ok2", "text": "嘿嘿，先卖个关子～多陪我聊聊嘛", "photo_sent": False,
     "scene": "", "expect_ok": True},                         # 撤回后的兜底话术
    {"id": "ok3", "text": "我在咖啡厅呢，拍给你看～", "photo_sent": True,
     "scene": "in a cozy cafe, holding a coffee cup", "expect_ok": True},  # 场景一致
    {"id": "ok4", "text": "改天陪你去海边呀，今天先宅家", "photo_sent": True,
     "scene": "at home on the couch, cozy and relaxed",
     "expect_ok": True},                                      # 愿望≠断言身处
    {"id": "ok5", "text": "在家躺着呢～", "photo_sent": True,
     "scene": "in the bedroom, soft warm light", "expect_ok": True},  # home/bedroom 互容
    {"id": "ok6", "text": "夜跑完啦，city walk～", "photo_sent": True,
     "scene": "city night lights in the background", "hour": 22,
     "expect_ok": True},                                      # 夜景+深夜=一致
]


def evaluate_media_consistency(
    samples: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """跑金标语料（或调用方样本）出报告：validator 判定 vs expect_ok 全对才 PASS。

    金标含「必须抓住的违规」与「不许误伤的合法表达」两面——探测器松了或紧了都红。
    """
    rows = samples if samples is not None else _GOLDEN_SAMPLES
    results = []
    n_ok = 0
    for s in rows:
        verdict = check_media_consistency(
            str(s.get("text") or ""),
            photo_sent=bool(s.get("photo_sent")),
            scene=str(s.get("scene") or ""),
            hour=s.get("hour"),
        )
        expected = bool(s.get("expect_ok", True))
        hit = (verdict["ok"] == expected)
        n_ok += 1 if hit else 0
        results.append({
            "id": str(s.get("id") or ""),
            "expect_ok": expected,
            "got_ok": verdict["ok"],
            "violations": verdict["violations"],
            "pass": hit,
        })
    total = len(rows)
    return {
        "available": True,
        "total": total,
        "matched": n_ok,
        "passed": (n_ok == total),
        "results": results,
    }


def format_media_consistency_report(report: Dict[str, Any]) -> str:
    lines = ["=== 图文一致性评测（确定性校验器 + 事故语料回归网）==="]
    for r in report.get("results", []):
        mark = "PASS" if r.get("pass") else "FAIL"
        vio = ",".join(r.get("violations") or []) or "-"
        lines.append(
            f"[{mark}] {r.get('id')}: expect_ok={r.get('expect_ok')} "
            f"got_ok={r.get('got_ok')} violations={vio}")
    lines.append(
        f"总计 {report.get('matched')}/{report.get('total')} 对齐 → "
        + ("PASS" if report.get("passed") else "FAIL"))
    return "\n".join(lines)


__all__ = [
    "check_media_consistency", "evaluate_media_consistency",
    "format_media_consistency_report",
]
