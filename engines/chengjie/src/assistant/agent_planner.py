# -*- coding: utf-8 -*-
"""小智「替我做」规划器纯函数核心（实施58 P2，2026-08-23）。

刻意的架构选择（比原方案 v3 的「原生 function-calling」更稳，理由入档）：
1. **JSON 计划器 > 协议级 tool-calling**：LLM 只被要求输出一段严格 JSON
   （say + steps[]），随后**逐步经 actions.plan_action 结构性复验**——
   未知动作/坏参数/越权级别一律丢弃降级，LLM 输出永远不被直接执行。
   这让我们能复用主链 ``ai_client.generate_reply``（自带 key 池/本地兜底/
   熔断），零新 SDK 依赖；换 provider 也不挑「是否支持 tools 协议」。
2. **规划/执行分离**：本模块只出计划；执行统一走已上线的
   ``POST /api/assistant/act``（确认 token/撤销/审计三板斧在那一层，
   智能体不开第二条写通道）。
3. **宁缺勿滥**：解析失败/全步被丢 → ok=False + 诚实 say，不猜不凑。

门禁 ``tests/test_assistant_agent_planner.py``。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional

from src.assistant import actions as act

MAX_STEPS_DEFAULT = 5
MAX_SAY_CHARS = 300
MAX_GOAL_CHARS = 500


# ── prompt 构造 ──────────────────────────────────────────────────────────
def _params_doc(aid: str, spec: Mapping[str, Any], zh: bool) -> str:
    if aid == "goto_page":
        return "params={\"path\": <导航白名单内路径>}" if zh else \
            "params={\"path\": <one whitelisted nav path>}"
    fields = spec.get("fields") or []
    if not fields:
        return "params={}"
    parts = []
    for f in fields:
        if f.get("platforms"):
            parts.append("\"platform\": <" + "|".join(f["platforms"]) + ">")
        t = f.get("type")
        if t == "number":
            parts.append(f"\"{f['param']}\": <number {f.get('lo')}-{f.get('hi')}>")
        elif t == "bool":
            parts.append(f"\"{f['param']}\": <true|false>")
        elif t == "enum":
            ch = "|".join(str(c) or "''" for c in (f.get("choices") or ()))
            parts.append(f"\"{f['param']}\": <{ch}>")
        elif t == "text":
            parts.append(f"\"{f['param']}\": <text ≤{f.get('maxlen', 120)}>")
        else:
            parts.append(f"\"{f['param']}\": <value>")
    return "params={" + ", ".join(parts) + "}"


def build_settings_snapshot(config: Any, lang: str = "zh") -> str:
    """「当前设置现状」只读快照块（P1）：把 L2 动作字段的现值人话化喂给
    规划器——没有现状 LLM 是盲开处方（「调快点」不知道现在多快、
    「开启 X」不知道 X 本就开着）。模板路径（平台限档）汇总非空表。"""
    zh = not str(lang or "").lower().startswith("en")
    lines: List[str] = []
    seen: set = set()
    for spec in act.ACTIONS.values():
        for f in spec.get("fields", []):
            path = str(f["path"])
            if path in seen:
                continue
            seen.add(path)
            label = str(f.get("label_zh" if zh else "label_en") or "")
            if not label:
                continue
            if "{platform}" in path:
                base = path.split(".{platform}", 1)[0]
                cur = act._dig(config, base, {})
                if isinstance(cur, dict) and cur:
                    pairs = ", ".join(
                        f"{k}={act._humanize(f, v, zh)}"
                        for k, v in sorted(cur.items()))
                    lines.append(f"- {label}: {pairs}")
                continue
            val = act._dig(config, path, f.get("default"))
            lines.append(f"- {label}: {act._humanize(f, val, zh)}")
    if not lines:
        return ""
    head = ("当前设置现状（只读快照，规划时参考；用户没提的不要顺手改）："
            if zh else
            "Current settings snapshot (read-only; do not change what the "
            "user did not ask about):")
    return head + "\n" + "\n".join(lines)


def build_planner_prompt(
    goal: str,
    role: str,
    nav_paths: List[str],
    lang: str = "zh",
    page: str = "",
    max_steps: int = MAX_STEPS_DEFAULT,
    config: Any = None,
) -> str:
    zh = not str(lang or "").lower().startswith("en")
    lines: List[str] = []
    levels = act.allowed_levels_for_role(role)
    for aid, spec in act.ACTIONS.items():
        if spec["level"] not in levels:
            continue
        label = spec["label_zh" if zh else "label_en"]
        desc = spec["desc_zh" if zh else "desc_en"]
        lines.append(f"- {aid} [{spec['level']}] {label}：{desc}；"
                     f"{_params_doc(aid, spec, zh)}")
    nav = "、".join(sorted(nav_paths)[:40])
    goal = str(goal or "").strip()[:MAX_GOAL_CHARS]
    snap = build_settings_snapshot(config, lang) if config else ""
    snap_block = ("\n\n" + snap) if snap else ""
    if zh:
        return (
            "你是客服软件的操作规划器。根据用户目标，从下面的动作白名单里"
            "挑选 0 到 " + str(max_steps) + " 步组成计划。\n"
            "只能用白名单内的动作与参数；帮不上就 steps 留空并在 say 里"
            "说明。绝不编造动作。\n"
            "如果目标含糊到无法选参数（比如「调一下速度」没说快还是慢），"
            "可以 steps 留空并给 ask 一句追问（≤50 字），只许问一个问题。\n\n"
            "动作白名单：\n" + "\n".join(lines)
            + snap_block + "\n\n"
            "goto_page 允许的 path：" + nav + "\n"
            "用户当前页面：" + (page or "/") + "\n"
            "用户目标：" + goal + "\n\n"
            "只输出一段 JSON，不要任何其他文字，格式：\n"
            "{\"say\": \"一句话说明你打算怎么做\", "
            "\"ask\": \"可选：向用户追问的一句话\", "
            "\"steps\": [{\"action\": \"动作id\", \"params\": {…}}]}"
        )
    return (
        "You are the operation planner of a customer-service app. Pick 0 to "
        + str(max_steps) + " steps from the whitelist below to achieve the "
        "user goal.\nOnly whitelisted actions/params; if you cannot help, "
        "return empty steps and explain in say. Never invent actions.\n"
        "If the goal is too vague to pick params, leave steps empty and put "
        "ONE short clarifying question in ask.\n\n"
        "Action whitelist:\n" + "\n".join(lines)
        + snap_block + "\n\n"
        "Allowed goto_page paths: " + nav + "\n"
        "Current page: " + (page or "/") + "\n"
        "User goal: " + goal + "\n\n"
        "Output ONLY one JSON object:\n"
        "{\"say\": \"one sentence about your plan\", "
        "\"ask\": \"optional: one clarifying question\", "
        "\"steps\": [{\"action\": \"id\", \"params\": {…}}]}"
    )


# ── 解析（剥围栏 + 首个平衡 JSON 对象；字符串感知防内嵌花括号误配） ──────
def parse_plan_json(raw: str) -> Optional[Dict[str, Any]]:
    s = str(raw or "").strip()
    if not s:
        return None
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None
    return None


# ── 校验（逐步过 plan_action，宁缺勿滥） ─────────────────────────────────
def _step_params(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """从 plan_action 输出反推「干净参数」（供前端逐步转投 /act 复验）。"""
    aid = str(plan.get("action") or "")
    spec = act.ACTIONS.get(aid) or {}
    if plan.get("kind") == "nav":
        return {"path": plan.get("goto")}
    clean = plan.get("clean_params")
    if isinstance(clean, Mapping) and clean:
        return dict(clean)
    out: Dict[str, Any] = {}
    by_path = {d["path"]: d["new"] for d in (plan.get("diff") or [])}
    for f in spec.get("fields", []):
        if f["path"] in by_path:
            out[f["param"]] = by_path[f["path"]]
    return out


def validate_plan(
    parsed: Optional[Mapping[str, Any]],
    config: Any,
    role: str,
    nav_paths: Optional[set] = None,
    max_steps: int = MAX_STEPS_DEFAULT,
    lang: str = "zh",
) -> Dict[str, Any]:
    zh = not str(lang or "").lower().startswith("en")
    if not isinstance(parsed, Mapping):
        return {"ok": False, "reason": "parse_failed", "say": "", "steps": [],
                "dropped": [], "ask": ""}
    say = str(parsed.get("say") or "").strip()[:MAX_SAY_CHARS]
    ask = str(parsed.get("ask") or "").strip()[:120]
    raw_steps = parsed.get("steps")
    if not isinstance(raw_steps, list):
        raw_steps = []
    levels = act.allowed_levels_for_role(role)
    steps: List[Dict[str, Any]] = []
    dropped: List[Dict[str, str]] = []
    for item in raw_steps:
        if len(steps) >= max_steps:
            dropped.append({"action": str((item or {}).get("action", "")),
                            "reason": "max_steps"})
            continue
        if not isinstance(item, Mapping):
            dropped.append({"action": "", "reason": "bad_step"})
            continue
        aid = str(item.get("action") or "")
        params = item.get("params") if isinstance(item.get("params"),
                                                  Mapping) else {}
        plan = act.plan_action(aid, params, config, nav_paths=nav_paths,
                               lang=lang)
        if not plan.get("ok"):
            dropped.append({"action": aid,
                            "reason": str(plan.get("error") or "invalid")})
            continue
        if plan["level"] not in levels:
            dropped.append({"action": aid, "reason": "forbidden"})
            continue
        spec = act.ACTIONS[aid]
        steps.append({
            "action": aid,
            "level": plan["level"],
            "kind": plan["kind"],
            "hot": plan.get("hot"),
            "label": spec["label_zh" if zh else "label_en"],
            "params": _step_params(plan),
            "goto": plan.get("goto"),
            "diff": plan.get("diff"),
        })
    ok = bool(steps)
    # ask 只在「没有可执行步」时有意义（有步还追问＝拖泥带水，丢弃）
    return {"ok": ok, "reason": "" if ok else "no_valid_steps",
            "say": say, "steps": steps, "dropped": dropped,
            "ask": "" if ok else ask}
