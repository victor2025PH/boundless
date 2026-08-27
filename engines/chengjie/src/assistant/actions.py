# -*- coding: utf-8 -*-
"""小智「动作注册表」纯函数核心（实施58 P1，2026-08-23）。

设计铁律（docs/实施58 §3.4；改前先读）：
1. **白名单即全部**：智能体/前端只能提名本表登记的动作；执行器只写表内
   声明的 config 路径（与 reply_pacing_settings.FIELDS / capability_toggle
   同哲学——结构性防线，不靠 prompt 自觉）。**不造新写端点**：全部经
   ``cm.set_overlay_flag``（ruamel 保注释）落 overlay。
2. **权限分级**：L0 只读查询 / L1 教学导航 / L2 可逆设置（必须确认 +
   可撤销）。给客户发消息等 L3 刻意不进本批（走既有人审投递链，P2 再议）。
3. **先看后做**：L2 动作两段式——``plan_action`` 出 diff（现值→新值），
   ``issue_confirm`` 发一次性确认 token（TTL 10min），带 token 才
   ``apply_plan``；应用登记 undo 快照（进程内 24h + 审计 JSONL 落盘）。
4. **审计**：每次写/撤销追加 ``assistant_actions_audit.jsonl``（config 目录，
   风格对齐 companion_capability_audit.jsonl）。

本模块保持零 web 依赖（纯函数 + 进程级小状态），路由薄包装在
``src/web/routes/assistant_action_routes.py``。门禁
``tests/test_assistant_actions.py``。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

CONFIRM_TTL_SEC = 600.0
UNDO_TTL_SEC = 24 * 3600.0
_MAX_PENDING = 50

_time = time.time  # 测试可 monkeypatch

_LOCK = threading.Lock()
_CONFIRMS: Dict[str, Dict[str, Any]] = {}
_UNDOS: Dict[str, Dict[str, Any]] = {}

# goto 白名单兜底（nav_schema 提取失败时的保守核心集）
CORE_NAV_PATHS = (
    "/workspace",
    "/reply-settings",
    "/personas",
    "/knowledge",
    "/ops-overview",
    "/dashboard",
    "/membership",
    "/settings",
)

# ── 值→人话标签表（确认卡 diff 的 old_h/new_h；老板令 2026-08-23：界面
#    绝不出现 L2/config 路径这类黑话，值也要说人话）─────────────────────
_VL_MODE = {
    "manual": ("纯手动", "manual"),
    "review": ("AI拟稿·人审后发", "AI drafts, human approves"),
    "multi_choice": ("AI出多稿·人来挑", "AI multi-draft, human picks"),
    "auto_ai": ("全自动", "fully automatic"),
}
_VL_TRIGGER = {
    "never": ("从不发语音", "never"),
    "always": ("每条都语音", "every reply"),
    "when_peer_voice": ("对方发语音才回语音", "when peer sends voice"),
    "smart": ("智能判断", "smart"),
}
_VL_LENGTH = {
    "": ("跟随人设", "follow persona"),
    "concise": ("简短", "concise"),
    "moderate": ("适中", "moderate"),
    "detailed": ("详细", "detailed"),
}
_VL_EMOJI = {
    "": ("跟随人设", "follow persona"),
    "none": ("不用表情", "none"),
    "minimal": ("偶尔一个", "minimal"),
    "moderate": ("适度", "moderate"),
    "rich": ("丰富", "rich"),
}

# 平台键域与 reply_pacing_settings.PLATFORMS 同源（本模块保持零依赖，
# 一致性由 tests/test_assistant_actions.py 钉住）。
ACTION_PLATFORMS = ("telegram", "whatsapp", "line", "messenger", "zalo",
                    "instagram")

# ── 注册表 ────────────────────────────────────────────────────────────────
# level: L0 查询 / L1 导航 / L2 可逆设置。
# fields: L2 允许写的 config 点分路径（default=快照缺键时的真实缺省，
#         与 reply_pacing_settings.FIELDS 对齐）；hot 语义同该表——
#         True=消费方活读 config 写完即生效；False/缺省=重启窗后全量生效
#         （前端如实展示，绝不谎报）。path 含 {platform} 的字段须带
#         platforms 白名单，参数里必须给 platform。
ACTIONS: Dict[str, Dict[str, Any]] = {
    "diagnose_autoreply": {
        "level": "L0",
        "kind": "query",
        "label_zh": "诊断：为什么不自动回复",
        "label_en": "Diagnose: why is auto-reply off",
        "desc_zh": "汇总影响全自动回复的因素清单（因素注册表，只读）",
        "desc_en": "Read-only factor checklist affecting auto-reply",
    },
    "query_ai_status": {
        "level": "L0",
        "kind": "query",
        "label_zh": "查询：AI 现在正常吗",
        "label_en": "Query: is the AI healthy",
        "desc_zh": "主链/备用/本地兜底当前谁在岗（只读）",
        "desc_en": "Read-only: primary / pool / local fallback duty status",
    },
    "query_reply_budget": {
        "level": "L0",
        "kind": "query",
        "label_zh": "查询：今日回复额度",
        "label_en": "Query: today's reply budget",
        "desc_zh": "回复额度保险丝今天的用量与触顶情况（只读）",
        "desc_en": "Read-only: today's per-chat reply budget usage",
    },
    "query_platform_health": {
        "level": "L0",
        "kind": "query",
        "label_zh": "查询：平台账号在线吗",
        "label_en": "Query: platform sessions health",
        "desc_zh": "各平台会话（messenger/whatsapp 等）当前健康状况（只读）",
        "desc_en": "Read-only: current platform session health",
    },
    "goto_page": {
        "level": "L1",
        "kind": "nav",
        "label_zh": "带我去指定页面",
        "label_en": "Take me to a page",
        "desc_zh": "跳转到导航白名单内的页面（params.path）",
        "desc_en": "Navigate to a whitelisted page (params.path)",
    },
    "set_reply_delay": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "调整回复速度（拟人延迟区间）",
        "label_en": "Adjust reply speed (humanized delay range)",
        "desc_zh": "min_sec/max_sec 秒级区间，0-600；写 overlay + 活体 worker 热更",
        "desc_en": "min_sec/max_sec in 0-600s; overlay write + live worker hot-apply",
        "hot": "worker",
        "fields": [
            {"path": "inbox.l2_autosend.deliver_delay.min_sec",
             "param": "min_sec", "type": "number", "lo": 0, "hi": 600,
             "default": 0,
             "label_zh": "回复延迟下限（秒）", "label_en": "Min delay (s)"},
            {"path": "inbox.l2_autosend.deliver_delay.max_sec",
             "param": "max_sec", "type": "number", "lo": 0, "hi": 600,
             "default": 0,
             "label_zh": "回复延迟上限（秒）", "label_en": "Max delay (s)"},
        ],
    },
    "toggle_voice_reply": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关自动语音回复",
        "label_en": "Toggle automatic voice replies",
        "desc_zh": "AI 自动回复时是否用语音条回（热生效）",
        "desc_en": "Whether auto replies go out as voice notes (hot)",
        "hot": True,
        "fields": [
            {"path": "inbox.l2_autosend.voice.enabled",
             "param": "enabled", "type": "bool", "default": False,
             "label_zh": "自动语音回复", "label_en": "Auto voice replies"},
        ],
    },
    # ── 2026-08-23 动作池扩容（老板令「能做多少做多少」）。全部复用既有
    #    overlay 写面与 reply_pacing_settings.FIELDS 登记过的路径，零新端点。
    "set_automation_mode": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "切换回复档位（全自动/人审/手动）",
        "label_en": "Switch reply mode (auto / review / manual)",
        "desc_zh": "全局默认档位：全自动直接回、AI拟稿人审后发、或纯手动（活读热生效）",
        "desc_en": "Global default: fully automatic, AI-draft with human "
                   "approval, or manual (hot)",
        "hot": True,
        "fields": [
            {"path": "inbox.auto_draft.automation_mode", "param": "mode",
             "type": "enum",
             "choices": ("manual", "review", "multi_choice", "auto_ai"),
             "default": "auto_ai", "vl": _VL_MODE,
             "label_zh": "回复档位", "label_en": "Reply mode"},
        ],
    },
    "set_platform_automation_cap": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "限某平台的自动化档位",
        "label_en": "Cap automation on one platform",
        "desc_zh": "对单个平台限档（如 messenger 只许 AI 拟稿人审）；"
                   "设回「全自动」即不限（活读热生效）",
        "desc_en": "Cap one platform (e.g. messenger review-only); set back "
                   "to fully-automatic to lift the cap (hot)",
        "hot": True,
        "fields": [
            {"path": "inbox.auto_draft.platform_modes.{platform}",
             "param": "mode", "type": "enum",
             "choices": ("manual", "review", "multi_choice", "auto_ai"),
             "default": None, "vl": _VL_MODE,
             "platforms": ACTION_PLATFORMS,
             "label_zh": "该平台档位上限", "label_en": "Platform cap"},
        ],
    },
    "set_reply_length": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "调整回复长度（简短/适中/详细）",
        "label_en": "Set reply length (concise / moderate / detailed)",
        "desc_zh": "全局默认回复长度；人设里显式设置的优先（热生效）",
        "desc_en": "Global default reply length; persona overrides win (hot)",
        "hot": True,
        "fields": [
            {"path": "ai.reply_defaults.length", "param": "length",
             "type": "enum",
             "choices": ("", "concise", "moderate", "detailed"),
             "default": "", "vl": _VL_LENGTH,
             "label_zh": "回复长度", "label_en": "Reply length"},
        ],
    },
    "set_emoji_level": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "调整表情用量",
        "label_en": "Set emoji usage",
        "desc_zh": "回复里 emoji 的密度：不用/偶尔/适度/丰富（热生效）",
        "desc_en": "Emoji density in replies: none / minimal / moderate / "
                   "rich (hot)",
        "hot": True,
        "fields": [
            {"path": "ai.reply_defaults.emoji_level", "param": "level",
             "type": "enum",
             "choices": ("", "none", "minimal", "moderate", "rich"),
             "default": "", "vl": _VL_EMOJI,
             "label_zh": "表情用量", "label_en": "Emoji usage"},
        ],
    },
    "set_max_sentences": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "限制每条回复的句数",
        "label_en": "Cap sentences per reply",
        "desc_zh": "每条回复最多几句话，0=不限制（热生效）",
        "desc_en": "Max sentences per reply, 0 = unlimited (hot)",
        "hot": True,
        "fields": [
            {"path": "ai.reply_defaults.max_sentences", "param": "n",
             "type": "number", "lo": 0, "hi": 10, "default": 0,
             "label_zh": "每条最多句数", "label_en": "Max sentences"},
        ],
    },
    "set_tone_hint": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "设置全局语气提示",
        "label_en": "Set global tone hint",
        "desc_zh": "一句话语气要求（如「多关心对方，少用感叹号」），"
                   "留空=清除（热生效）",
        "desc_en": "One-line tone request (e.g. 'be caring, fewer "
                   "exclamations'); empty = clear (hot)",
        "hot": True,
        "fields": [
            {"path": "ai.reply_defaults.tone_hint", "param": "hint",
             "type": "text", "maxlen": 120, "default": "",
             "label_zh": "语气提示", "label_en": "Tone hint"},
        ],
    },
    "set_voice_trigger": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "设定什么时候用语音回",
        "label_en": "When to reply with voice",
        "desc_zh": "从不/每条都语音/对方发语音才回语音/智能判断（热生效）",
        "desc_en": "never / always / when peer sends voice / smart (hot)",
        "hot": True,
        "fields": [
            {"path": "inbox.l2_autosend.voice.trigger", "param": "trigger",
             "type": "enum",
             "choices": ("never", "always", "when_peer_voice", "smart"),
             "default": "when_peer_voice", "vl": _VL_TRIGGER,
             "label_zh": "语音回复时机", "label_en": "Voice trigger"},
        ],
    },
    "toggle_work_schedule": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关上下班班表",
        "label_en": "Toggle work-hours schedule",
        "desc_zh": "开=按班表时间回复，关=全天可回（热生效）",
        "desc_en": "On = reply only in work hours; off = around the clock "
                   "(hot)",
        "hot": True,
        "fields": [
            {"path": "inbox.work_schedule.enabled", "param": "enabled",
             "type": "bool", "default": False,
             "label_zh": "上下班班表", "label_en": "Work schedule"},
        ],
    },
    "toggle_reply_budget_guard": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关每日回复额度保险丝",
        "label_en": "Toggle daily reply budget guard",
        "desc_zh": "防对面也是机器人互刷：单个聊天每天回复条数保险丝（热生效）",
        "desc_en": "Anti bot-loop fuse: caps replies per chat per day (hot)",
        "hot": True,
        "fields": [
            {"path": "inbox.peer_bot_guard.enabled", "param": "enabled",
             "type": "bool", "default": False,
             "label_zh": "回复额度保险丝", "label_en": "Reply budget guard"},
        ],
    },
    "toggle_outbound_translate": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关出站自动翻译",
        "label_en": "Toggle outbound auto-translation",
        "desc_zh": "发出前自动译成客户语言；开关重启窗后全量生效",
        "desc_en": "Translate outgoing messages into the customer's "
                   "language; takes full effect after the restart window",
        "hot": False,
        "fields": [
            {"path": "inbox.l2_autosend.translate.enabled",
             "param": "enabled", "type": "bool", "default": False,
             "label_zh": "出站自动翻译", "label_en": "Outbound translation"},
        ],
    },
    "toggle_proactive_greeting": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关主动打招呼",
        "label_en": "Toggle proactive greetings",
        "desc_zh": "客户沉默几小时后 AI 主动问候；开关重启窗后全量生效",
        "desc_en": "AI reaches out after hours of silence; takes full "
                   "effect after the restart window",
        "hot": False,
        "fields": [
            {"path": "companion.proactive_topic.enabled", "param": "enabled",
             "type": "bool", "default": False,
             "label_zh": "主动打招呼", "label_en": "Proactive greetings"},
        ],
    },
    "toggle_daily_ritual": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关早晚安问候",
        "label_en": "Toggle morning/night greetings",
        "desc_zh": "按客户活跃时间点发早安晚安；开关重启窗后全量生效",
        "desc_en": "Personalized good-morning / good-night pings; takes "
                   "full effect after the restart window",
        "hot": False,
        "fields": [
            {"path": "companion.proactive_topic.daily_ritual.enabled",
             "param": "enabled", "type": "bool", "default": False,
             "label_zh": "早晚安问候", "label_en": "Daily ritual"},
        ],
    },
    "toggle_selfie": {
        "level": "L2",
        "kind": "settings",
        "label_zh": "开/关聊天发生活照",
        "label_en": "Toggle photo sharing in chat",
        "desc_zh": "人设相册/自拍链总开关（客户要图时才发，护栏照旧）；"
                   "开关重启窗后全量生效",
        "desc_en": "Master switch for persona photo sharing (guardrails "
                   "unchanged); takes full effect after the restart window",
        "hot": False,
        "fields": [
            {"path": "companion.selfie.enabled", "param": "enabled",
             "type": "bool", "default": False,
             "label_zh": "聊天发生活照", "label_en": "Photo sharing"},
        ],
    },
}


def allowed_levels_for_role(role: str) -> tuple:
    """坐席只许查询+导航；管理侧角色才许 L2 设置写。"""
    if str(role or "").strip().lower() == "agent":
        return ("L0", "L1")
    return ("L0", "L1", "L2")


def catalog(role: str, lang: str = "zh") -> List[Dict[str, Any]]:
    en = str(lang or "").lower().startswith("en")
    levels = allowed_levels_for_role(role)
    out: List[Dict[str, Any]] = []
    for aid, spec in ACTIONS.items():
        if spec["level"] not in levels:
            continue
        out.append({
            "id": aid,
            "level": spec["level"],
            "kind": spec["kind"],
            "label": spec["label_en" if en else "label_zh"],
            "desc": spec["desc_en" if en else "desc_zh"],
            "hot": spec.get("hot"),
        })
    return out


def _dig(config: Any, path: str, default: Any = None) -> Any:
    cur = config
    for part in str(path).split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _coerce(spec: Dict[str, Any], raw: Any) -> tuple:
    """(ok, value|reason)。宁拒不猜：类型/边界不对一律 bad_params。"""
    t = spec.get("type")
    if t == "bool":
        if isinstance(raw, bool):
            return True, raw
        return False, f"{spec.get('param')} must be bool"
    if t == "number":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return False, f"{spec.get('param')} must be number"
        v = float(raw)
        lo, hi = spec.get("lo"), spec.get("hi")
        if lo is not None and v < lo:
            return False, f"{spec.get('param')} < {lo}"
        if hi is not None and v > hi:
            return False, f"{spec.get('param')} > {hi}"
        return True, (int(v) if float(v).is_integer() else v)
    if t == "enum":
        if not isinstance(raw, str):
            return False, f"{spec.get('param')} must be string"
        if raw not in (spec.get("choices") or ()):
            return False, f"{spec.get('param')} not in choices"
        return True, raw
    if t == "text":
        if not isinstance(raw, str):
            return False, f"{spec.get('param')} must be string"
        s = raw.strip()
        maxlen = int(spec.get("maxlen") or 0)
        if maxlen and len(s) > maxlen:
            return False, f"{spec.get('param')} > {maxlen} chars"
        return True, s
    return False, "unsupported type"


def _humanize(spec: Dict[str, Any], val: Any, zh: bool) -> str:
    """值→人话（确认卡展示；老板令：不给用户看 true/false/auto_ai 黑话）。"""
    if isinstance(val, bool):
        return ("开" if val else "关") if zh else ("on" if val else "off")
    vl = spec.get("vl")
    if isinstance(vl, dict) and isinstance(val, str) and val in vl:
        return vl[val][0 if zh else 1]
    if val is None:
        return "未设置" if zh else "unset"
    if val == "":
        return "（空）" if zh else "(empty)"
    return str(val)


def plan_action(
    action_id: str,
    params: Optional[Mapping[str, Any]],
    config: Any,
    *,
    nav_paths: Optional[set] = None,
    lang: str = "zh",
) -> Dict[str, Any]:
    """校验 + 出计划（纯函数，不写任何东西）。

    返回：{ok, action, level, kind, ...}；L2 附
    diff=[{path, old, new, label, old_h, new_h}]（label/old_h/new_h 是
    确认卡人话素材，按 lang 出）+ clean_params（复验用干净参数，含
    platform 这类路径模板参数）；L1 goto 附 goto=path。
    失败：{ok:False, error, detail}。
    """
    spec = ACTIONS.get(str(action_id or ""))
    if not spec:
        return {"ok": False, "error": "unknown_action", "detail": str(action_id)}
    params = params or {}
    zh = not str(lang or "").lower().startswith("en")
    base = {"ok": True, "action": action_id, "level": spec["level"],
            "kind": spec["kind"], "hot": spec.get("hot")}

    if spec["kind"] == "query":
        return base

    if spec["kind"] == "nav":
        path = str(params.get("path") or "").strip()
        allowed = nav_paths if nav_paths else set(CORE_NAV_PATHS)
        if not path or path not in allowed:
            return {"ok": False, "error": "path_not_allowed", "detail": path}
        base["goto"] = path
        return base

    # settings（L2）：逐字段校验 + 现值快照 + 人话标签
    diff: List[Dict[str, Any]] = []
    clean: Dict[str, Any] = {}
    for f in spec.get("fields", []):
        if f["param"] not in params:
            return {"ok": False, "error": "bad_params",
                    "detail": f"missing {f['param']}"}
        ok, v = _coerce(f, params[f["param"]])
        if not ok:
            return {"ok": False, "error": "bad_params", "detail": str(v)}
        path = str(f["path"])
        if "{platform}" in path:
            plat = str(params.get("platform") or "").strip().lower()
            allowed_p = f.get("platforms") or ()
            if plat not in allowed_p:
                return {"ok": False, "error": "bad_params",
                        "detail": f"platform not in {list(allowed_p)}"}
            path = path.replace("{platform}", plat)
            clean["platform"] = plat
        old = _dig(config, path, f.get("default"))
        clean[f["param"]] = v
        diff.append({
            "path": path, "old": old, "new": v,
            "label": str(f.get("label_zh" if zh else "label_en") or ""),
            "old_h": _humanize(f, old, zh),
            "new_h": _humanize(f, v, zh),
        })
    # 动作级横向校验
    if action_id == "set_reply_delay":
        vals = {d["path"].rsplit(".", 1)[-1]: d["new"] for d in diff}
        if vals.get("min_sec", 0) > vals.get("max_sec", 0):
            return {"ok": False, "error": "bad_params",
                    "detail": "min_sec > max_sec"}
    base["diff"] = diff
    base["clean_params"] = clean
    return base


# ── 确认 token（单次核销 + TTL） ─────────────────────────────────────────
def _gc(store: Dict[str, Dict[str, Any]], ttl: float) -> None:
    now = _time()
    dead = [k for k, v in store.items() if now - v.get("ts", 0) > ttl]
    for k in dead:
        store.pop(k, None)


def issue_confirm(plan: Mapping[str, Any], uid: str) -> str:
    token = "xza-" + secrets.token_urlsafe(18)
    with _LOCK:
        _gc(_CONFIRMS, CONFIRM_TTL_SEC)
        if len(_CONFIRMS) >= _MAX_PENDING:
            oldest = min(_CONFIRMS, key=lambda k: _CONFIRMS[k]["ts"])
            _CONFIRMS.pop(oldest, None)
        _CONFIRMS[token] = {"plan": dict(plan), "uid": str(uid),
                            "ts": _time()}
    return token


def pop_confirm(token: str, uid: str) -> Optional[Dict[str, Any]]:
    """取走即删（单次核销）；过期/uid 不符=None。"""
    with _LOCK:
        _gc(_CONFIRMS, CONFIRM_TTL_SEC)
        ent = _CONFIRMS.pop(str(token or ""), None)
    if not ent:
        return None
    if ent.get("uid") != str(uid):
        return None
    return ent.get("plan")


# ── 应用 + 撤销 + 审计 ───────────────────────────────────────────────────
def _audit_path(cm) -> Optional[Path]:
    base = getattr(cm, "config_path", None)
    return (Path(base).parent / "assistant_actions_audit.jsonl") if base else None


def _audit(cm, rec: Dict[str, Any]) -> None:
    try:
        p = _audit_path(cm)
        if p is None:
            return
        rec = dict(rec)
        rec["ts"] = round(_time(), 3)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("写小智动作审计失败（忽略）", exc_info=True)


def apply_plan(plan: Mapping[str, Any], cm, actor: str) -> Dict[str, Any]:
    """把 L2 计划写 overlay；成功登记 undo 快照并审计。

    返回 {ok, applied:[path], failed:[{path,msg}], undo_id}；ok=至少一条成功。
    审计行携带 undo_id（P1 任务历史：历史列表据此标注「仍可撤销」）——
    故先写盘收集成功项、登记 undo 快照拿到 id，再统一落审计。
    """
    diff = list(plan.get("diff") or [])
    applied: List[str] = []
    failed: List[Dict[str, str]] = []
    olds: List[Dict[str, Any]] = []
    ok_rows: List[Dict[str, Any]] = []
    for d in diff:
        try:
            ok, msg = cm.set_overlay_flag(d["path"], d["new"])
        except Exception as ex:  # cm 异常按失败记，绝不半途抛
            ok, msg = False, str(ex)
        if ok:
            applied.append(d["path"])
            olds.append({"path": d["path"], "old": d["old"]})
            ok_rows.append(d)
        else:
            failed.append({"path": d["path"], "msg": str(msg or "")})
    undo_id = ""
    if olds:
        undo_id = "xzu-" + secrets.token_urlsafe(12)
        with _LOCK:
            _gc(_UNDOS, UNDO_TTL_SEC)
            if len(_UNDOS) >= _MAX_PENDING:
                oldest = min(_UNDOS, key=lambda k: _UNDOS[k]["ts"])
                _UNDOS.pop(oldest, None)
            _UNDOS[undo_id] = {"action": plan.get("action"), "olds": olds,
                               "actor": actor, "ts": _time()}
    for d in ok_rows:
        _audit(cm, {"op": "apply", "actor": actor,
                    "action": plan.get("action"), "path": d["path"],
                    "old": d["old"], "new": d["new"], "undo_id": undo_id})
    return {"ok": bool(applied), "applied": applied, "failed": failed,
            "undo_id": undo_id}


# ── L0 查询的人话摘要器（P2）：路由取快照、这里出一句话——纯函数可测，
#    前端 execStep 的 query 分支只认 result.verdict，零前端改动。─────────
def summarize_ai_status(snap: Optional[Mapping[str, Any]],
                        lang: str = "zh") -> str:
    zh = not str(lang or "").lower().startswith("en")
    if not isinstance(snap, Mapping):
        return ("AI 状态读不到（观测口未装载）" if zh
                else "AI status unavailable (probe not loaded)")
    degraded = bool(snap.get("degraded"))
    mode = str(snap.get("mode") or "primary")
    names = {
        "primary": ("主链在岗", "primary on duty"),
        "pool": ("备用 Key 顶班", "backup key on duty"),
        "local": ("本地兜底顶班", "local fallback on duty"),
        "canned": ("兜底话术顶班", "canned replies on duty"),
    }
    label = names.get(mode, (mode, mode))[0 if zh else 1]
    if degraded:
        return (f"AI 降级中：{label}——回复仍在出，建议看运营总览定位主链问题"
                if zh else
                f"AI degraded: {label} — replies still flowing; check ops "
                "overview for the primary-chain issue")
    return (f"AI 正常：{label}" if zh else f"AI healthy: {label}")


def summarize_reply_budget(n_rows: int, n_exhausted: int, n_relieved: int,
                           guard: Optional[Mapping[str, Any]],
                           lang: str = "zh") -> str:
    zh = not str(lang or "").lower().startswith("en")
    g = guard if isinstance(guard, Mapping) else {}
    enabled = bool(g.get("enabled"))
    budget = int(g.get("daily_reply_budget") or 0)
    if not enabled or budget <= 0:
        return ("回复额度保险丝未开启（今天不限量）" if zh
                else "Reply budget guard is off (no cap today)")
    if n_rows <= 0:
        return (f"额度保险丝开着（每聊每天 {budget} 条），今天还没有会话用到额度"
                if zh else
                f"Guard on ({budget}/chat/day); no chats used budget today")
    if zh:
        out = f"今天 {n_rows} 个会话在用额度（每聊上限 {budget} 条）"
        out += f"，其中 {n_exhausted} 个触顶" if n_exhausted else "，无触顶"
        if n_relieved:
            out += f"，{n_relieved} 个已豁免继续"
        return out
    out = f"{n_rows} chats used budget today (cap {budget}/chat)"
    out += (f"; {n_exhausted} hit the cap" if n_exhausted else "; none capped")
    if n_relieved:
        out += f"; {n_relieved} relieved"
    return out


def summarize_platform_sessions(dump: Optional[Mapping[str, Any]],
                                lang: str = "zh") -> str:
    zh = not str(lang or "").lower().startswith("en")
    d = dump if isinstance(dump, Mapping) else {}
    sessions = d.get("sessions") if isinstance(d.get("sessions"), Mapping) \
        else {}
    unhealthy = d.get("unhealthy") if isinstance(d.get("unhealthy"), list) \
        else []
    if not sessions:
        return ("没有外部平台会话在登记（telegram 协议号不走这个口）" if zh
                else "No external platform sessions registered (protocol "
                     "accounts are not tracked here)")
    if not unhealthy:
        return (f"全部平台会话健康（{len(sessions)} 条在册）" if zh
                else f"All platform sessions healthy ({len(sessions)} "
                     "registered)")
    heads = ", ".join(str(k) for k in unhealthy[:4])
    more = len(unhealthy) - 4
    tail = (f" 等{len(unhealthy)}条" if zh else f" (+{more} more)") \
        if more > 0 else ""
    return ((f"{len(unhealthy)} 条会话不健康：{heads}{tail}——"
             "可去运营总览「平台会话健康」卡重新登录") if zh else
            f"{len(unhealthy)} unhealthy: {heads}{tail} — relogin from the "
            "ops overview card")


def has_undo(undo_id: str) -> bool:
    """该撤销快照仍在有效期内（任务历史标注「可撤销」用）。"""
    if not undo_id:
        return False
    with _LOCK:
        _gc(_UNDOS, UNDO_TTL_SEC)
        return str(undo_id) in _UNDOS


_FIELD_BY_PATH: Optional[Dict[str, Dict[str, Any]]] = None
_FIELD_TPL_PREFIXES: List[tuple] = []


def _field_maps() -> tuple:
    """path→字段 spec 惰性索引（含模板路径前缀表），历史行人话化用。"""
    global _FIELD_BY_PATH, _FIELD_TPL_PREFIXES
    if _FIELD_BY_PATH is None:
        exact: Dict[str, Dict[str, Any]] = {}
        prefixes: List[tuple] = []
        for spec in ACTIONS.values():
            for f in spec.get("fields", []):
                p = str(f["path"])
                if "{platform}" in p:
                    prefixes.append((p.split("{platform}", 1)[0], f))
                else:
                    exact[p] = f
        _FIELD_BY_PATH = exact
        _FIELD_TPL_PREFIXES = prefixes
    return _FIELD_BY_PATH, _FIELD_TPL_PREFIXES


def describe_path(path: str, lang: str = "zh") -> tuple:
    """(字段人话标签, spec|None)。模板路径按前缀归位并附平台名。"""
    zh = not str(lang or "").lower().startswith("en")
    exact, prefixes = _field_maps()
    p = str(path or "")
    f = exact.get(p)
    if f is not None:
        return str(f.get("label_zh" if zh else "label_en") or p), f
    for pre, f2 in prefixes:
        if p.startswith(pre):
            plat = p[len(pre):]
            base = str(f2.get("label_zh" if zh else "label_en") or p)
            return f"{base}（{plat}）" if zh else f"{base} ({plat})", f2
    return p.rsplit(".", 2)[-1], None


def humanize_value(path: str, val: Any, lang: str = "zh") -> str:
    """按字段 spec 出人话值；未知路径回落 str。"""
    zh = not str(lang or "").lower().startswith("en")
    _label, spec = describe_path(path, lang)
    return _humanize(spec or {}, val, zh)


def read_history(cm, limit: int = 40) -> List[Dict[str, Any]]:
    """审计 JSONL 尾部 N 行（新→旧）。文件缺席/坏行一律跳过不抛。"""
    p = _audit_path(cm)
    if p is None or not p.is_file():
        return []
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 131072))
            tail = f.read().decode("utf-8", errors="replace")
    except Exception:
        logger.debug("读小智动作审计失败", exc_info=True)
        return []
    out: List[Dict[str, Any]] = []
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict) and rec.get("op") in ("apply", "undo"):
            out.append(rec)
    out.reverse()
    return out[: max(1, int(limit or 40))]


def apply_undo(undo_id: str, cm, actor: str) -> Optional[Dict[str, Any]]:
    """按快照回写旧值；不存在/过期=None（路由回 404 文案）。"""
    with _LOCK:
        _gc(_UNDOS, UNDO_TTL_SEC)
        ent = _UNDOS.pop(str(undo_id or ""), None)
    if not ent:
        return None
    restored: List[str] = []
    failed: List[Dict[str, str]] = []
    for item in ent.get("olds", []):
        try:
            ok, msg = cm.set_overlay_flag(item["path"], item["old"])
        except Exception as ex:
            ok, msg = False, str(ex)
        if ok:
            restored.append(item["path"])
            _audit(cm, {"op": "undo", "actor": actor,
                        "action": ent.get("action"), "path": item["path"],
                        "restored": item["old"]})
        else:
            failed.append({"path": item["path"], "msg": str(msg or "")})
    return {"ok": bool(restored), "restored": restored, "failed": failed,
            "action": ent.get("action")}
