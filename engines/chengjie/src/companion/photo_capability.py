# -*- coding: utf-8 -*-
"""人设发图能力单一事实源（photo capability SSOT，2026-07-31）。

背景（人设工作室试聊实录）：人设背景里有「画过猫猫明信片」这类素材，客户一句
「有图嘛」，LLM 就角色扮演「我翻翻手机相册哈」「等我找找看有没有存图」——承诺
反复出现、图永远不来。根因是能力约束与真实能力**倒挂**：旧逻辑只在开了 selfie
的部署注入【媒体能力边界】（`ai_client._media_capability_hint_enabled` 的
docstring 原话「没有发图能力时这段说明只是噪声」），而最需要「别承诺发图」
约束的恰是**发不了图**的形态。

本模块把「这个人设此刻能不能发图」收成单一判定，四类消费方同一口径：
- prompt 层：persona_manager 人设块（``capabilities.photos`` 反向消费，与
  ``capabilities.video_call`` 同款先例，全域覆盖含试聊）+ ai_client 陪聊域注入
  （能力关 → 硬约束；能力开 → 原 [PHOTO] 协议，行为不变）；
- 执行层：A 线 Stage 0（注册相册）/A（自拍生成）/B（物体图）、photo_directive
  执行、承诺异步兑现预检——即使提示层漏了，执行层也一张图都发不出去；
- B 线：``image_autosend.run_autosend_image``（相册+生成同一入口）；
- 试聊：``chat_test_routes`` 出站消毒与能力状态回显（与生产同轨）。

语义（默认关是刻意的产品决策，2026-07-31 需求「相册增加关闭功能，默认为关闭」）：
- 人设级开关 = ``persona["capabilities"]["photos"]``，**显式 true 才开**；
  缺字段/无人设/域默认人设 → 一律关。
- 开关关闭 = 该人设**整条发图能力**关闭（注册相册 + AI 生成一起），不是只关
  相册库——否则生成链仍会发图/仍会承诺，与「对话中不要承诺发送照片」矛盾。
- 有效能力 = 全局 ``companion.selfie.enabled`` 且 人设开关开（两级都开才发）。
- 关闭 ≠ 禁止谈论照片：人设谈论照片**回忆**（「我画过一套明信片」）是人味，
  承诺**发送**（「我找找发你」）才是事故。约束文案据此措辞，勿改成一刀切
  禁提「照片」二字（那会让人设躲闪、不自然）。

纯函数 + 尾部两个薄解析 helper（import PersonaManager，软失败回 None/False），
门禁 tests/test_photo_capability.py。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

# ── 进程级观测（P1：能力关仍被消毒的次数 → 提示层约束够不够用）──────────────
_STATS_LOCK = threading.Lock()
_STATS: Dict[str, int] = {
    "sanitize_calls": 0,
    "sanitize_stripped": 0,       # 真剥掉了承诺/标记
    "chat_test_stripped": 0,
    "inbox_draft_stripped": 0,
}


def record_sanitize(*, stripped: bool, source: str = "") -> None:
    """记录一次出站消毒。``source`` ∈ chat_test|inbox_draft|其他。"""
    with _STATS_LOCK:
        _STATS["sanitize_calls"] += 1
        if stripped:
            _STATS["sanitize_stripped"] += 1
            key = f"{source}_stripped" if source else ""
            if key in _STATS:
                _STATS[key] += 1


def dump_sanitize_stats() -> Dict[str, Any]:
    """供 /api/workspace/metrics 消费；零流量也有结构。"""
    with _STATS_LOCK:
        d = dict(_STATS)
    calls = int(d.get("sanitize_calls") or 0)
    stripped = int(d.get("sanitize_stripped") or 0)
    d["strip_rate"] = round(stripped / calls, 3) if calls else 0.0
    d["active"] = calls > 0
    return d


# ── 人设块短约束（persona_manager compact/full 反向消费；与 video_call 并排）──
# 一句话版：人设块寸土寸金，细则由 ai_client 的详细版承担（陪聊域两者可并存，
# 重复是刻意的防御纵深，不是缺陷）。
NO_PHOTO_PERSONA_LINE = (
    "你没有发照片/图片的功能：绝不说「我找找/翻翻相册/拍一张发你/回头发你」"
    "这类承诺发图的话，也绝不假装图已发出（「这张是…」「你看看这张」）；"
    "被要照片时轻巧带过、继续聊天，不答应稍后补图。"
)

# ── 陪聊域详细约束（ai_client 注入；能力关闭时替代旧的「什么都不注入」）──────
_NO_PHOTO_CONSTRAINT = (
    "【发图能力·硬边界】你在这个聊天里**没有发照片/图片的功能**（一张也发不出）。"
    "可以自然聊照片相关的回忆和话题（比如聊起你拍过/画过什么），但绝对不要：\n"
    "① 承诺发图——「我找找看」「我翻翻相册」「等我拍一张」「回头发你」"
    "「发你看看」都不许说；\n"
    "② 假装正在发图或已发图——不说「这张是…」「你看这张」「发过去了」；\n"
    "③ 长篇解释自己为什么发不了图（一句自然带过即可）。\n"
    "对方要照片时：轻巧地把话题聊下去（如「照片就先不发啦，先陪你聊会儿」），"
    "绝不答应稍后补图，也不要编造「手机坏了」之类的具体借口。"
)


def no_photo_constraint() -> str:
    """能力关闭时注入 system prompt 的硬约束（详细版，陪聊域用）。"""
    return _NO_PHOTO_CONSTRAINT


def persona_photos_enabled(persona: Optional[Dict[str, Any]]) -> bool:
    """人设级发图开关：``capabilities.photos`` 显式真值才开，**默认关**。

    无人设/非 dict/缺 capabilities/缺 photos → False。域默认人设与兜底人设
    不会带该字段 → 天然关闭（「默认为关闭相册」的产品语义）。
    """
    if not isinstance(persona, dict):
        return False
    caps = persona.get("capabilities")
    if not isinstance(caps, dict):
        return False
    return bool(caps.get("photos"))


def selfie_globally_enabled(cfg: Optional[Dict[str, Any]]) -> bool:
    """全局发图总闸 ``companion.selfie.enabled``（缺省/异常 → False）。"""
    try:
        if not isinstance(cfg, dict):
            return False
        comp = cfg.get("companion") or {}
        if not isinstance(comp, dict):
            return False
        sc = comp.get("selfie") or {}
        return bool(isinstance(sc, dict) and sc.get("enabled", False))
    except Exception:
        return False


def photos_effective(cfg: Optional[Dict[str, Any]],
                     persona: Optional[Dict[str, Any]]) -> bool:
    """有效发图能力 = 全局总闸开 且 人设开关开。任何一级关 → 关。"""
    return selfie_globally_enabled(cfg) and persona_photos_enabled(persona)


def sanitize_no_photo_reply(
    text: str, *, media_context: bool = False, source: str = "",
) -> str:
    """能力关闭形态的出站消毒（试聊与轻量出站口用；A/B 生产线走各自守卫）。

    动作：剥 [PHOTO] 标记（协议若被上游误注入，标记绝不能漏给用户）→ 剥
    「将发」承诺句 → 剥「已发」断言句（``media_context``=对方在索要媒体时才判，
    防误伤评论对方发来的图）→ 全剥空则换语言对齐的婉转兜底话术。
    任何一步异常都回退原文（消毒是增强，绝不吞掉回复）。
    ``source`` 供观测分桶（``chat_test`` / ``inbox_draft``）。
    """
    raw = str(text or "")
    if not raw:
        return raw
    try:
        from src.ai.outbound_promise_guard import (
            deflection_line,
            detect_media_claim,
            detect_media_promise,
            strip_media_claims,
            strip_media_promises,
        )
        from src.ai.photo_directive import strip_photo_directives

        out = strip_photo_directives(raw)
        kind = detect_media_promise(out) or detect_media_claim(
            out, media_context=media_context)
        if not kind:
            # 可能只剥了 [PHOTO] 标记
            try:
                record_sanitize(stripped=(out != raw), source=source)
            except Exception:
                pass
            return out
        stripped = strip_media_promises(out)
        stripped = strip_media_claims(stripped, media_context=media_context)
        # 剥后残留（跨句拼接漏网）→ 整条换兜底，宁可少说不说谎。
        if stripped.strip() and (
                detect_media_promise(stripped)
                or detect_media_claim(stripped, media_context=media_context)):
            stripped = ""
        if not stripped.strip():
            stripped = deflection_line(out, kind)
        try:
            record_sanitize(stripped=(stripped != raw), source=source)
        except Exception:
            pass
        return stripped
    except Exception:
        return raw


# ── 薄解析 helper（唯一的非纯部分：查 PersonaManager；软失败=关）────────────────

def resolve_prompt_persona(context: Optional[Dict[str, Any]]
                           ) -> Optional[Dict[str, Any]]:
    """按 prompt 层同一优先级解析「当前生效人设」。

    与 ``ai_client._build_system_instruction`` 拼人设块的口径一致
    （``get_persona_with_tier(chat_id, account_persona_id)``：账号人设 >
    chat 绑定 > 域 > 兜底），保证「提示层说的能力」与「能力判定」永远同一人设。
    异常/无 PersonaManager → None（按无人设=关处理）。
    """
    try:
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        persona, _tier = pm.get_persona_with_tier(
            str((context or {}).get("chat_id") or ""),
            str((context or {}).get("account_persona_id") or ""),
        )
        return persona if isinstance(persona, dict) else None
    except Exception:
        return None


def persona_photos_enabled_by_id(persona_id: str) -> bool:
    """B 线口径：按显式 persona_id 查人设开关（查不到/异常 = 关）。"""
    pid = str(persona_id or "").strip()
    if not pid:
        return False
    try:
        from src.utils.persona_manager import PersonaManager
        p = PersonaManager.get_instance().get_persona_by_id(pid)
        return persona_photos_enabled(p)
    except Exception:
        return False


def prompt_photos_allowed(user_context: Optional[Dict[str, Any]]) -> bool:
    """A 线执行层统一门：当前会话生效人设的发图开关（默认关，异常按关）。

    刻意做成**模块级函数**而非 SkillManager 方法：A 线五个媒体入口
    （Stage 0/A/B、photo_directive、异步兑现预检）直接调用，测试桩类
    （只绑定所需方法的 _SM 模式）零改动即可工作，monkeypatch 本模块
    即可统一控制门态。与 prompt 层同一人设解析口径（resolve_prompt_persona），
    保证「提示词声明的能力」与「执行层真发不发」永远一致。
    """
    try:
        return persona_photos_enabled(resolve_prompt_persona(user_context))
    except Exception:
        return False


__all__ = [
    "NO_PHOTO_PERSONA_LINE",
    "no_photo_constraint",
    "persona_photos_enabled",
    "selfie_globally_enabled",
    "photos_effective",
    "sanitize_no_photo_reply",
    "resolve_prompt_persona",
    "persona_photos_enabled_by_id",
    "prompt_photos_allowed",
    "record_sanitize",
    "dump_sanitize_stats",
]
