# -*- coding: utf-8 -*-
"""人设卡（persona）schema 校验与 system prompt 组装（真人感文本层）。

来源：engines/avatarhub avatar_hub.py 的 _persona_sanitize/_persona_prompt
（2026-08-10 快照）；HTTPException 改为 PersonaError（ValueError 子类），
其余逻辑逐字一致。样例卡见 data/personas/Mizuki.json。

schema（白名单，未列字段一律丢弃）：
  identity / style / greeting_habit : str，≤200 字
  catchphrases / boundaries / tone_words : list[str]，≤10 条、每条 ≤50 字
  tone_words 供出口注入池用：仅单字且 ∈ TONE_WORD_POOL 的会生效。

实战要点（源仓 P1 修复的教训）：硬规则（boundaries + 听不清澄清）优先且不被
截断——旧版整段 [:300] 会把 boundaries 裁掉，模型随即无视澄清规则。
"""
from __future__ import annotations

import json
from pathlib import Path

STR_FIELDS = ("identity", "style", "greeting_habit")
LIST_FIELDS = ("catchphrases", "boundaries", "tone_words")
# 句尾语气词安全白名单（出口注入池只认这些单字）
TONE_WORD_POOL = set("呢啊呀哟嘞诶咯")


class PersonaError(ValueError):
    """人设卡字段不合法。"""


def persona_sanitize(raw: dict) -> dict:
    """人设卡字段校验：白名单字段；字符串≤200字；列表≤10条、每条≤50字。"""
    if not isinstance(raw, dict):
        raise PersonaError("persona 必须是 JSON 对象")
    out: dict = {}
    for k in STR_FIELDS:
        v = raw.get(k) or ""
        if not isinstance(v, str):
            raise PersonaError(f"字段 {k} 必须是字符串")
        v = v.strip()[:200]
        if v:
            out[k] = v
    for k in LIST_FIELDS:
        v = raw.get(k)
        if v in (None, ""):
            v = []
        if isinstance(v, str):           # 宽容：单个字符串视作单元素列表
            v = [v]
        if not isinstance(v, list):
            raise PersonaError(f"字段 {k} 必须是字符串列表")
        items = []
        for it in v[:10]:
            if not isinstance(it, str):
                raise PersonaError(f"字段 {k} 的每一项必须是字符串")
            s = it.strip()[:50]
            if s:
                items.append(s)
        if items:
            out[k] = items
    return out


def persona_prompt(p: dict) -> str:
    """人设卡 → 注入 system prompt 的中文人设段（稳定 system 层，KV 友好）。

    硬规则（boundaries + 写死的「听不清」澄清）优先预算不被截断；
    软描述（身份/风格/口头禅/打招呼）填剩余预算；总预算 720 字。
    """
    hard: list[str] = []
    soft: list[str] = []
    bds = [b for b in (p.get("boundaries") or []) if isinstance(b, str) and b.strip()]
    if bds:
        hard.append("【必须遵守】" + "；".join(bds[:8]))
    # 听不清澄清：产品硬规则，写死注入（不依赖 boundaries 是否被运营删掉）
    hard.append(
        "【听不清·必须】对方说「说的什么」「没听清」「听不清」「啥意思」时："
        "先用更短的话重说上一句意思；回复里必须出现「没听清」或「重说」或「意思」之一；"
        "最多两句、禁止开新话题或推销"
    )
    if p.get("identity"):
        soft.append(f"身份：{p['identity']}")
    if p.get("style"):
        soft.append(f"风格：{p['style']}")
    cps = [c for c in (p.get("catchphrases") or []) if isinstance(c, str) and c.strip()]
    if cps:
        soft.append("口头禅：" + "、".join(cps[:6]) + "（自然带入，别堆砌）")
    if p.get("greeting_habit"):
        soft.append(f"打招呼：{p['greeting_habit']}")
    if not hard and not soft:
        return ""
    hard_txt = "。".join(hard)
    soft_txt = "。".join(soft)
    # 硬规则预算约 320 字，整体上限 720（给 LLM 足够人设，又不至于刷屏）
    budget = 720
    hard_budget = min(len(hard_txt) + 8, 360)
    remain = max(80, budget - hard_budget - 12)
    if len(soft_txt) > remain:
        soft_txt = soft_txt[: remain - 1] + "…"
    body = hard_txt + ("。" + soft_txt if soft_txt else "")
    return ("【人设卡】" + body + "。请始终以这个人设的口吻说话。")[:budget]


def load_persona(path: str | Path) -> dict:
    """读盘上的人设卡 JSON 并做 schema 校验。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return persona_sanitize(raw)


def tone_words(p: dict) -> list[str]:
    """取该人设的安全句尾语气词池（供出口注入用，可空）。"""
    return [w for w in (p.get("tone_words") or [])
            if isinstance(w, str) and len(w) == 1 and w in TONE_WORD_POOL]
