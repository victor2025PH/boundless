# -*- coding: utf-8 -*-
"""Facebook 平台路由 — 与 routers/tiktok.py 同构,提供:

  * 设备级一键启动 (POST /facebook/device/{id}/launch)
  * 5 套执行方案预设查询 (GET /facebook/presets)
  * 引流账号配置 (GET/POST /facebook/referral-config) — 默认 WA 优先
  * 设备网格聚合数据 (GET /facebook/device-grid)
  * 漏斗统计 (GET /facebook/funnel)

Sprint 1 实现 launch + presets + referral-config 骨架;
Sprint 2 补 device-grid + funnel + qualified-leads。
"""
from __future__ import annotations

import json as _json
import logging
import os
import urllib.request as _ur
from pathlib import Path
import copy as _copy
from typing import Any, Dict, List, Optional, Tuple

import yaml
from fastapi import APIRouter, Body, Depends, HTTPException, Request

from .auth import verify_api_key
from src.openclaw_env import local_api_base
from src.host.device_registry import (
    DEFAULT_DEVICES_YAML,
    config_file,
    is_device_in_local_registry,
)
# P2-UI Sprint 新增：persona 展示层 & 引流优先级
# 把原先写在 /referral-config 里的 priority_order 硬编码迁到 persona 节点，
# 实现"改客群即自动切换引流渠道顺序"的单一事实源。
from src.host.fb_target_personas import (
    clear_active_persona_override,
    get_default_persona_key,
    get_persona_display,
    get_referral_priority,
    get_yaml_default_persona_key,
    list_persona_displays,
    read_active_persona_override,
    set_active_persona_override,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/facebook", tags=["facebook"],
                   dependencies=[Depends(verify_api_key)])

# 设为 1/true：POST /active-persona 仅接受 X-API-Key==OPENCLAW_API_KEY（不接受仅浏览器会话），用于公网暴露面收缩。
_STRICT_FB_ACTIVE_PERSONA_POST = os.environ.get(
    "OPENCLAW_FB_ACTIVE_PERSONA_REQUIRE_KEY", ""
).strip().lower() in ("1", "true", "yes")

_W03_BASE = "http://192.168.0.103:8000"
_DEVICES_YAML = Path(DEFAULT_DEVICES_YAML)
_CHAT_MSG_YAML = config_file("chat_messages.yaml")
_CUSTOM_PRESETS_YAML = config_file("fb_custom_presets.yaml")


# ─────────────────────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────────────────────

def _is_local_device(device_id: str) -> bool:
    """判断设备是否在本地 device manager 中。"""
    return is_device_in_local_registry(device_id, devices_yaml=str(_DEVICES_YAML))


class FacebookTaskEnqueueError(RuntimeError):
    """子节点 ``POST /tasks`` 失败（含 400 gate / 校验错误），携带 HTTP 状态与 FastAPI detail。"""

    __slots__ = ("status", "detail")

    def __init__(self, message: str, *, status: int = 0, detail: Any = None):
        super().__init__(message)
        self.status = int(status or 0)
        self.detail = detail


def _probe_worker_health_capabilities(base: str, timeout: float = 3.0) -> Tuple[Dict[str, Any], bool]:
    """对远端 ``GET {base}/health`` 轻量探测；返回 ``(capabilities 字典, 是否成功拿到 HTTP 响应体)``。"""
    b = (base or "").rstrip("/")
    if not b:
        return {}, False
    try:
        req = _ur.Request(f"{b}/health", method="GET")
        resp = _ur.urlopen(req, timeout=timeout)
        try:
            raw = resp.read().decode("utf-8", errors="replace")
            data = _json.loads(raw) if raw.strip().startswith("{") else {}
            return dict((data or {}).get("capabilities") or {}), True
        finally:
            resp.close()
    except Exception:
        return {}, False


def _remote_worker_capabilities_warning(
    *,
    is_local: bool,
    worker_caps: Dict[str, Any],
    probe_ok: bool = True,
) -> Optional[str]:
    """非本机入队时，若无法确认 Worker 与主控一致的预检门能力，则给出运维可读告警文案。"""
    if is_local:
        return None
    if (worker_caps or {}).get("facebook_task_precreate_gate") is True:
        return None
    core = (
        "无法确认远程 Worker 与主控一致的加好友预检门（facebook_task_precreate_gate）；"
        "加好友类 POST /tasks 可能与主控不一致，请将 Worker 升级到与主控同版本。"
    )
    if not probe_ok:
        return core + "（GET /health 探测失败：请检查网络、防火墙与 Worker 是否在线。）"
    return core + "（GET /health 已连通但未声明该能力，常见于旧版 Worker。）"


def _post_create_task(base: str, payload: dict, timeout: int = 10) -> dict:
    import urllib.error as _ue

    rb = _json.dumps(payload).encode()
    req = _ur.Request(f"{base.rstrip('/')}/tasks", data=rb,
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = _ur.urlopen(req, timeout=timeout)
        try:
            return _json.loads(resp.read())
        finally:
            resp.close()
    except _ue.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        detail: Any = None
        msg = ""
        try:
            if body.strip().startswith("{"):
                j = _json.loads(body)
                detail = j.get("detail")
                if isinstance(detail, dict):
                    msg = str(detail.get("error") or detail.get("message") or "").strip()
                elif isinstance(detail, str):
                    msg = detail.strip()
        except Exception:
            pass
        if not msg:
            msg = f"HTTP {e.code}: {body[:400]}"
        raise FacebookTaskEnqueueError(msg, status=e.code, detail=detail) from e


# ─────────────────────────────────────────────────────────────────────────
# 5 套执行方案预设(服务端权威定义,前端拉取后渲染卡片)
# ─────────────────────────────────────────────────────────────────────────
#
# 设计原则:
#   - 与 TikTok 的 _TT_FLOW_PRESETS 同构,但步骤适配 FB 业务
#   - "激进"风控档默认值:每号每日 30 加好友 / 60 DM 上限
#   - steps 里的 type 必须与 executor.py 中 facebook_* 任务类型对齐
# ─────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────
# 预设参数设计笔记（P2-UI Sprint · 日本 37-60 女性客群）
# ─────────────────────────────────────────────────────────────────────────
# 以下 5 套预设的"显性参数"（像 like_probability、max_friends_per_run、
# max_members）来自 3 个维度的综合权衡：
#   1) 日本 FB 中年女性行为常模（本地敬语文化 + 举报阈值更低 → 克制）
#   2) 风控相位（cold_start/growth/mature/cooldown，由 fb_playbook.yaml 热加载）
#   3) 引流产出 KPI（日本 LINE 渗透更高 → 侧重深聊而非广撒网）
#
# 主要调整（相比旧版通用激进档）：
#   * like_probability ↑  0.18 → 0.35（日本女性点赞文化活跃，低了反不自然）
#   * comment_probability ↓ 0.25 → 0.15（日本社区评论克制，广告态度会被秒投诉）
#   * max_friends_per_run ↓ 8 → 5（日本女性号被秒举报即永封）
#   * group_hunter.max_members ↓ 30 → 20（一次提取太多 → 后续 add_friend 必超限）
#   * full_funnel.max_friends ↓ 5 → 4（保守档下每日 16-20 好友，月稳定累计）
#
# 所有 step 默认 persona_key=jp_female_midlife；launch 阶段如未传入
# target_country/language 会由 fb_device_launch 自动从 persona 补齐。
# ─────────────────────────────────────────────────────────────────────────

# 客群硬编码值：从 fb_target_personas.yaml 读出默认 persona 的 key。
# 放在 module 级常量是为了让 FB_FLOW_PRESETS 在模块载入时就能固化，
# 切换客群时只需改 YAML 的 default_persona 字段并调 /facebook/presets/reload。
_DEFAULT_PERSONA = "jp_female_midlife"


_JP_MIDLIFE_FAMILY_NAMES = [
    "佐藤", "鈴木", "高橋", "田中", "渡辺", "伊藤", "山本", "中村",
    "小林", "加藤", "吉田", "山田", "佐々木", "山口", "松本", "井上",
    "木村", "林", "清水", "山崎", "森", "池田", "橋本", "阿部",
]

_JP_MIDLIFE_GIVEN_NAMES = [
    "恵子", "裕子", "陽子", "直子", "智子", "美穂", "由美", "久美子",
    "真由美", "香織", "美香", "由美子", "恵美", "真理子", "千恵", "紀子",
    "幸子", "明美", "典子", "里美", "佳代", "和美", "順子", "雅子",
    "玲子", "尚子", "美智子", "良子", "昭子", "洋子", "京子", "早苗",
    "花子", "美咲",
]

_JP_MIDLIFE_NAME_PACKS: Dict[str, List[str]] = {
    "37_45": ["美香", "香織", "美穂", "恵美", "由美", "千恵", "直子", "佳代"],
    "46_55": ["真由美", "由美子", "智子", "陽子", "里美", "紀子", "和美", "尚子"],
    "56_60": ["恵子", "裕子", "幸子", "典子", "雅子", "玲子", "京子", "洋子"],
}


def _split_name_targets(raw: Any) -> List[dict]:
    """Normalize name-hunter input into ``[{"name": "..."}]`` with stable dedupe."""
    import re as _re

    rows: List[dict] = []

    def _push(name: str, meta: Optional[dict] = None) -> None:
        nm = str(name or "").strip()
        if not nm:
            return
        item = {"name": nm}
        if meta:
            for k, v in meta.items():
                if k != "name" and v not in (None, ""):
                    item[k] = v
        rows.append(item)

    if isinstance(raw, str):
        for line in _re.split(r"[,\n;、，；\t]+", raw):
            _push(line)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                _push(item)
            elif isinstance(item, dict):
                _push(str(item.get("name") or ""), item)

    seen = set()
    out: List[dict] = []
    for item in rows:
        key = str(item.get("name") or "").replace(" ", "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _generate_jp_midlife_names(count: int = 30, age_pack: str = "mixed") -> List[dict]:
    """Return deterministic Japanese female-name seeds suitable for manual review."""
    count = max(1, min(int(count or 30), 200))
    pack = str(age_pack or "mixed")
    if pack in _JP_MIDLIFE_NAME_PACKS:
        given = list(_JP_MIDLIFE_NAME_PACKS[pack])
    else:
        given = list(_JP_MIDLIFE_GIVEN_NAMES)
    names: List[dict] = []
    for i in range(count):
        family = _JP_MIDLIFE_FAMILY_NAMES[i % len(_JP_MIDLIFE_FAMILY_NAMES)]
        g = given[(i * 7 + i // len(_JP_MIDLIFE_FAMILY_NAMES)) % len(given)]
        names.append({
            "name": f"{family}{g}",
            "source": f"jp_female_common_name:{pack}",
        })
    return _split_name_targets(names)


def _score_name_hunter_target(target: dict, persona_ctx: dict) -> dict:
    """Static pre-search score; profile/VLM stages still decide final customer match."""
    name = str((target or {}).get("name") or "").strip()
    compact = name.replace(" ", "")
    reasons: List[str] = []
    score = 0

    if any(ch in compact for ch in _JP_MIDLIFE_FAMILY_NAMES):
        score += 25
        reasons.append("日本常见姓氏")
    if any(ch in compact for ch in _JP_MIDLIFE_GIVEN_NAMES):
        score += 35
        reasons.append("37岁以上女性常见名")
    if any("\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff" for ch in compact):
        score += 15
        reasons.append("日文姓名字符")
    if (persona_ctx or {}).get("country_code") == "JP":
        score += 10
        reasons.append("目标客群为日本")
    if (persona_ctx or {}).get("gender") == "female":
        score += 10
        reasons.append("目标客群为女性")

    age_min = (persona_ctx or {}).get("age_min")
    age_max = (persona_ctx or {}).get("age_max")
    if age_min and int(age_min) >= 35:
        score += 5
        reasons.append(f"年龄画像 {age_min}-{age_max or '?'}")

    score = min(score, 100)
    if score >= 80:
        stage = "high_confidence_seed"
        action = "preview_then_confirm"
    elif score >= 50:
        stage = "review_required"
        action = "manual_review"
    else:
        stage = "weak_seed"
        action = "skip_or_enrich"
    return {
        **target,
        "score": score,
        "stage": stage,
        "recommended_action": action,
        "reasons": reasons or ["仅作为姓名种子，需搜索资料确认"],
    }


def _validate_preset_inputs(preset: dict, provided: dict, persona_ctx: dict) -> List[dict]:
    """根据 preset.needs_input + input_schema 校验已规范化的入参。

    ``provided`` 是经过字符串拆分、persona 兜底之后的实际值字典。
    返回缺失字段列表（每项 {field, label, schema}）；为空表示通过。

    支持 ``input_schema[field].fallback_from`` 声明回退源：
      * ``persona.seed_group_keywords`` — 当 persona_ctx 该键非空时视为已填
    """
    needs = preset.get("needs_input") or []
    schema = preset.get("input_schema") or {}
    missing: List[dict] = []
    for field in needs:
        spec = schema.get(field) or {}
        if not spec.get("required", True):
            continue
        val = provided.get(field)
        # "已填"语义：list/str 非空 / int 非 None / 其它真值
        filled = False
        if isinstance(val, (list, tuple)):
            filled = len(val) > 0
        elif isinstance(val, str):
            filled = bool(val.strip())
        elif val is not None:
            filled = bool(val)
        if filled:
            continue
        # fallback 路径
        fb_path = spec.get("fallback_from") or ""
        if fb_path == "persona.seed_group_keywords":
            seeds = persona_ctx.get("seed_group_keywords") or []
            if seeds:
                continue
        missing.append({
            "field": field,
            "label": spec.get("label") or field,
            "schema": spec,
        })
    return missing


# ═══════════════════════════════════════════════════════════════
# 共享 input_schema 字段定义 (DRY — 多个预设复用同一份规格)
# ═══════════════════════════════════════════════════════════════
_SHARED_FIELDS: dict = {
    "target_groups": {
        "type": "list_str", "label": "目标群组关键词",
        "required": True, "min": 1, "max": 3,
        "placeholder": "ペット\nカレー\nママ友",
        "help": "至少 1 个宽关键词或群名；系统会发现相关群并入群。留空时用客群默认种子群。",
        "fallback_from": "persona.seed_group_keywords",
    },
    "verification_note": {
        "type": "text", "label": "好友请求验证语",
        "required": True, "max_chars": 60,
        "placeholder": "您好🌸看到我们都在 XX 群，想认识下志同道合的朋友 ☺️",
        "help": "FB 好友请求的「打招呼」字段，60 字内。强烈建议带情境（同群/同兴趣）。",
        "ai_assist": True,
    },
    "greeting": {
        "type": "text", "label": "通过后首条打招呼话术",
        "required": True, "max_chars": 200,
        "placeholder": "您好～我也是关注美食的，请多多指教 🌸",
        "help": "好友通过后通过 Profile→Message 发送的首条 DM。AI 可基于 persona 生成。",
        "ai_assist": True,
    },
    "max_friends_per_run": {
        "type": "int", "label": "本次最多好友请求数（节流上限）",
        "required": False, "default": 5, "min": 1, "max": 15,
        "help": "单次任务硬节流上限，达到即停。保守档建议 ≤5；日本中年女性硬上限 8。",
    },
}


FB_FLOW_PRESETS: List[dict] = [
    {
        "key": "warmup",
        "name": "🌱 账号培育",
        "color": "#22c55e",
        "label": "养号机",
        "category": "maintain",
        "risk": "safe",
        "phase_gate": ["cold_start", "growth", "mature", "cooldown"],
        "desc": "Feed 浏览 + 兴趣点赞,降低风控权重",
        "detail": "适合新号或被限号恢复期(日本中年女性号安全档)",
        "estimated_minutes": 30,
        "estimated_output": "0 线索(基础养号)",
        "input_schema": {},
        "steps": [
            {"type": "facebook_browse_feed_by_interest",
             "params": {"duration": 10, "persona_key": _DEFAULT_PERSONA,
                        "interest_hours": 168, "max_topics": 4, "like_boost": 0.12}},
            {"type": "facebook_browse_feed",
             "params": {"scroll_count": 20, "like_probability": 0.35, "duration": 15,
                        "persona_key": _DEFAULT_PERSONA}},
        ],
    },
    {
        "key": "group_scout",
        "name": "🔍 社群探查",
        "color": "#10b981",
        "label": "探查机",
        "category": "maintain",
        "risk": "safe",
        "phase_gate": ["cold_start", "growth", "mature", "cooldown"],
        "desc": "入群 + 浏览帖子 + 点赞互动，建立群内存在感",
        "detail": "不发好友请求、不发 DM，纯互动养号。为后续拓展铺垫群内可见度",
        "estimated_minutes": 40,
        "estimated_output": "0 线索(群内存在感)",
        "next_preset": "friend_growth",
        "next_hint": "探查完成后，推荐进入「好友打招呼」发起拓展",
        "needs_input": ["target_groups"],
        "input_schema": {
            "target_groups": _SHARED_FIELDS["target_groups"],
        },
        "steps": [
            {"type": "facebook_campaign_run",
             "params": {
                 "steps": ["warmup", "group_engage"],
                 "warmup_scrolls": 15,
                 "group_max_posts": 6,
                 "like_probability": 0.4,
                 "persona_key": _DEFAULT_PERSONA,
             }},
        ],
    },
    # [已移除] group_hunter("社群客服拓展") — 功能与 friend_growth 重复，合并入好友打招呼
    {
        "key": "friend_growth",
        "name": "👥 好友打招呼",
        "color": "#60a5fa",
        "label": "招呼机",
        "category": "outreach",
        "risk": "moderate",
        "phase_gate": ["growth", "mature"],
        "desc": "群成员 → 进主页筛选 → 带验证语好友请求 → 打招呼 DM",
        "detail": "通过率 25-35%;日本女性保守档每号每日 15 请求 + 上限 8 条打招呼;"
                  "打招呼走 profile 页 Message 按钮（方案 A2，全程不换 app）",
        "estimated_minutes": 75,
        "estimated_output": "4-6 通过好友 + 3-5 条打招呼曝光",
        "needs_input": ["target_groups", "verification_note", "greeting"],
        "input_schema": {
            **_SHARED_FIELDS,
            "outreach_goal": {
                "type": "int", "label": "本次目标完成数（业务目标）",
                "required": False, "default": 5, "min": 1, "max": 15,
                "help": "任务完成判定线：成功发出好友请求数 ≥ 该值才记为「业务达成」，"
                        "否则任务以「outreach_goal_not_met」结束。"
                        "通常等于 max_friends_per_run；调小可让任务更早判达成。",
            },
        },
        "steps": [
            {"type": "facebook_group_member_greet",
             "params": {"steps": ["extract_members", "add_friends"],
                        # 2026-05-03 v27: 真机第 31 轮 sent=1/5 因候选池太小
                        # (16 候选只 1 个 search_people+add_friend 全程成功).
                        # 扩到 40 候选给 add_friend gate / quota / 找不到目标
                        # 等多重过滤后留够基数让 sent 接近 outreach_goal=5.
                        "max_members": 50, "extract_max_members": 50,
                        "persona_key": _DEFAULT_PERSONA,
                        "broad_keyword": True, "discover_groups": True,
                        "max_groups": 4, "max_groups_to_extract": 4,
                        "max_members_per_group": 50,
                        # 2026-05-03 v16: 真机 21 轮迭代发现新版 FB 已关闭非
                        # 管理员视角的 Members 列表入口 (Members tab 移除 +
                        # Member tools 菜单无 Members 选项). mutual_members /
                        # contributors 池长期产出 0. feed_authors 路径从帖子流
                        # 提取作者作为候选 — 绕过权限限制, 候选都是活跃发帖
                        # 用户. 必要时可加回 ["mutual_members", "contributors"]
                        # 让旧路径作 fallback.
                        "member_sources": ["general", "feed_authors"],
                        # 2026-05-10 v29: 18→25 扩大候选池, 解决 quota_exhausted
                        "feed_max_scrolls": 25,
                        # 2026-05-11 v30: fb_lead_scorer 已增强日本名/性别识别,
                        # 重新开启预筛. 20 分阈值: 日本女性名 45+, 日本中性名 30,
                        # 男性名 15 或负分 → 被过滤. 既去噪又不误杀.
                        "min_l1_score": 20,
                        "auto_join_groups": True, "join_if_needed": True,
                        "skip_visited": True,
                        "max_friends_per_run": 5,
                        "outreach_goal": 5,
                        "verification_note": "",
                        "greeting": "",
                        "send_greeting_inline": True,
                        "require_verification_note": True,
                        "require_outreach_goal": True,
                        # 2026-05-03 v28: 切换到智谱 GLM-4V-flash 云端视觉
                        # 模型, 不占本地内存, 延迟 ~4s. L2 双层判别.
                        # 2026-05-10 v29: l2_gate_shots 2→1 减少误拒,
                        # max_l2_calls 5→8 让更多候选进入评估
                        "do_l2_gate": True,
                        "strict_persona_gate": True,
                        "l2_gate_shots": 1,
                        "walk_candidates": True,
                        "max_l2_calls": 8}},
        ],
    },
    {
        "key": "name_hunter",
        "name": "🔎 点名添加",
        "color": "#0ea5e9",
        "label": "点名机",
        "category": "outreach",
        "risk": "moderate",
        "phase_gate": ["growth", "mature"],
        "desc": "名字列表 → 搜索 → 看资料 → 加好友 → 打招呼 DM",
        "detail": "适合已有高质量名字线索(Excel 导入 / 运营手动列)。"
                  "phase=cold_start / cooldown 会自动跳过,"
                  "每号每日硬上限由 playbook 控制。",
        "estimated_minutes": 45,
        "estimated_output": "3-5 个好友 + 同数量打招呼",
        "needs_input": ["add_friend_targets"],   # 前端必须提供名字列表
        "steps": [
            {"type": "facebook_campaign_run",
             "params": {"steps": ["add_friends"],
                        "max_friends_per_run": 5,
                        "verification_note": "",
                        "greeting": "",
                        "send_greeting_inline": True,
                        "require_verification_note": True,
                        "require_high_match": True,
                        "min_seed_score": 80,
                        "do_l2_gate": True,
                        "strict_persona_gate": True,
                        "l2_gate_shots": 2,
                        "walk_candidates": True,
                        "max_l2_calls": 5,
                        "add_friend_targets": [],   # 运行时由前端填充: [{"name": "山田花子"}, ...]
                        "persona_key": _DEFAULT_PERSONA}},
        ],
    },
    {
        "key": "inbox_pro",
        "name": "💬 沟通管家",
        "color": "#a78bfa",
        "label": "管家机",
        "category": "maintain",
        "risk": "safe",
        "phase_gate": ["cold_start", "growth", "mature", "cooldown"],
        "desc": "Messenger + Message Requests + Friend Requests 三件套",
        "detail": "AI 按敬语文化自动回复,LINE 优先引流",
        "estimated_minutes": 40,
        "estimated_output": "处理 20-40 条对话",
        "input_schema": {},
        "steps": [
            {"type": "facebook_check_inbox",
             "params": {"auto_reply": True, "max_conversations": 15,
                        "persona_key": _DEFAULT_PERSONA}},
            {"type": "facebook_check_message_requests",
             "params": {"max_requests": 10, "persona_key": _DEFAULT_PERSONA}},
            {"type": "facebook_check_friend_requests",
             "params": {"accept_all": False, "max_requests": 15,
                        "persona_key": _DEFAULT_PERSONA}},
        ],
    },
    {
        "key": "full_funnel",
        "name": "⚡ 全链路客服拓展",
        "color": "#ef4444",
        "label": "全程机",
        "category": "full_auto",
        "risk": "high",
        "phase_gate": ["growth", "mature"],
        "desc": "养号 → 进群 → 群成员打招呼 → 加好友 → 收件,完整闭环",
        "detail": "Phase C1 升级：链式 6 步执行, 支持断点续跑和单步重试。"
                  "日常首选,日本女性保守档每号每日 3-6 个 LINE 引流",
        "estimated_minutes": 150,
        "estimated_output": "3-6 个 LINE 引流",
        "needs_input": ["target_groups", "verification_note", "greeting"],
        "input_schema": {
            **{k: _SHARED_FIELDS[k] for k in
               ["target_groups", "verification_note", "greeting"]},
            "max_friends_per_run": {
                **_SHARED_FIELDS["max_friends_per_run"],
                "default": 4,
                "help": "全链路保守档默认 4；单次跑完全流程风险较高，建议 ≤5。",
            },
        },
        # Phase C1: 优先走 chain 编排 (支持断点续跑 + 单步重试)
        "chain_id": "fb_full_funnel",
        # 向后兼容: chain 不可用时回退到 campaign_run 单步
        "steps": [
            {"type": "facebook_campaign_run",
             "params": {
                 "steps": ["warmup", "group_engage", "extract_members",
                           "add_friends", "check_inbox"],
                 "warmup_scrolls": 20,
                 "group_max_posts": 4,
                 "extract_max_members": 30,
                 "max_friends_per_run": 4,
                 "max_conversations": 12,
                 "auto_reply": True,
                 "require_verification_note": True,
                 "persona_key": _DEFAULT_PERSONA,
             }},
        ],
    },
    {
        "key": "smart_growth",
        "name": "🧠 智能社群互动",
        "color": "#10b981",
        "label": "智能机",
        "category": "outreach",
        "risk": "moderate",
        "phase_gate": ["growth", "mature"],
        "desc": "浏览群帖子→分析用户→点赞→进 profile→加好友,模拟真人社交",
        "detail": "v1: 基于帖子内容和名字评分的双层分析,只对 B 类以上用户"
                  "(日本女性+正面帖子)执行点赞→进 profile→加好友。"
                  "比批量加友通过率更高,风控风险更低。",
        "estimated_minutes": 40,
        "estimated_output": "4-6 个高质量好友请求",
        "needs_input": ["target_groups"],
        "input_schema": {
            **{k: _SHARED_FIELDS[k] for k in ["target_groups"]},
        },
        "steps": [
            {"type": "facebook_campaign_run",
             "params": {
                 "steps": ["warmup", "smart_engage", "check_inbox"],
                 "warmup_scrolls": 15,
                 "persona_key": _DEFAULT_PERSONA,
                 "max_friends_per_run": 6,
                 "auto_reply": True,
                 "max_conversations": 10,
                 "broad_keyword": True,
                 "discover_groups": True,
                 "max_groups": 3,
                 "auto_join_groups": True,
                 "join_if_needed": True,
                 "require_verification_note": False,
             }},
        ],
    },
]


@router.get("/presets")
def get_facebook_presets():
    """返回执行方案预设(供前端流程模态渲染) + 今日各 preset 运行次数。

    各 step 的 ``params.persona_key`` 按当前**生效**默认客群重写（含运行时 override），
    与 GET ``/active-persona`` 的 ``default_key`` 一致。
    BB3b: 合并用户自定义预设,标记 _custom=true 并置顶。
    """
    from ..api import task_store
    eff = get_default_persona_key()
    out = _copy.deepcopy(FB_FLOW_PRESETS)
    # BB3b: 合并自定义预设
    customs = _load_custom_presets()
    for cp in customs:
        cp["_custom"] = True
        cp.setdefault("category", "custom")
        out.append(cp)
    # 今日运行次数
    try:
        today_counts = task_store.count_today_by_preset()
    except Exception:
        today_counts = {}
    for p in out:
        p["today_runs"] = today_counts.get(p["key"], 0)
        for st in p.get("steps") or []:
            if not isinstance(st, dict):
                continue
            prm = dict(st.get("params") or {})
            if "persona_key" in prm:
                prm["persona_key"] = eff
                st["params"] = prm
    return {"presets": out, "custom_count": len(customs)}


# ─────────────────────────────────────────────────────────────────────────
# BB3a: 自定义预设 CRUD — YAML 存储 + 基于系统预设克隆
# ─────────────────────────────────────────────────────────────────────────

def _load_custom_presets() -> List[dict]:
    """从 fb_custom_presets.yaml 加载自定义预设列表。"""
    if not _CUSTOM_PRESETS_YAML.exists():
        return []
    try:
        with open(_CUSTOM_PRESETS_YAML, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("presets") or []
    except Exception:
        return []


def _save_custom_presets(presets: List[dict]):
    """将自定义预设列表写入 YAML。"""
    _CUSTOM_PRESETS_YAML.parent.mkdir(parents=True, exist_ok=True)
    with open(_CUSTOM_PRESETS_YAML, "w", encoding="utf-8") as f:
        yaml.safe_dump({"version": 1, "presets": presets},
                       f, allow_unicode=True, sort_keys=False)


_SYSTEM_KEYS = frozenset(p["key"] for p in FB_FLOW_PRESETS)


@router.post("/presets/save-as")
def save_preset_as(body: dict = Body(default={})):
    """从系统预设克隆 + 参数覆盖,创建自定义预设。

    body 参数:
      - source_key: 系统预设 key (必填)
      - name: 自定义名称 (必填)
      - param_overrides: dict — 覆盖 steps[*].params 中的值
      - desc: 自定义描述 (可选)
    """
    source_key = (body.get("source_key") or "").strip()
    custom_name = (body.get("name") or "").strip()
    if not source_key or not custom_name:
        raise HTTPException(400, "source_key 和 name 必填")

    # 查找系统预设
    source = next((p for p in FB_FLOW_PRESETS if p["key"] == source_key), None)
    if not source:
        raise HTTPException(404, f"系统预设 {source_key} 不存在")

    import time
    custom_key = f"custom_{source_key}_{int(time.time())}"

    # 深拷贝并覆盖参数
    new_preset = _copy.deepcopy(source)
    new_preset["key"] = custom_key
    new_preset["name"] = custom_name
    new_preset["_custom"] = True
    new_preset["_source_key"] = source_key
    new_preset["_created"] = time.time()
    new_preset["category"] = "custom"
    if body.get("desc"):
        new_preset["desc"] = body["desc"]

    # 参数覆盖
    overrides = body.get("param_overrides") or {}
    if overrides:
        for step in new_preset.get("steps") or []:
            params = step.get("params") or {}
            for k, v in overrides.items():
                if k in params or k in (
                    "max_friends_per_run", "outreach_goal", "max_members",
                    "verification_note", "greeting", "target_groups",
                    "warmup_scrolls", "group_max_posts", "max_conversations",
                ):
                    params[k] = v
            step["params"] = params

    presets = _load_custom_presets()
    presets.append(new_preset)
    _save_custom_presets(presets)

    return {
        "ok": True,
        "key": custom_key,
        "name": custom_name,
        "source_key": source_key,
        "total_custom": len(presets),
    }


@router.put("/presets/{preset_key}")
def update_custom_preset(preset_key: str, body: dict = Body(default={})):
    """更新自定义预设的名称/描述/参数。仅允许修改 _custom 预设。"""
    if preset_key in _SYSTEM_KEYS:
        raise HTTPException(403, "系统预设不可修改")

    presets = _load_custom_presets()
    target = next((p for p in presets if p["key"] == preset_key), None)
    if not target:
        raise HTTPException(404, f"自定义预设 {preset_key} 不存在")

    if body.get("name"):
        target["name"] = body["name"]
    if body.get("desc"):
        target["desc"] = body["desc"]
    overrides = body.get("param_overrides") or {}
    if overrides:
        for step in target.get("steps") or []:
            params = step.get("params") or {}
            for k, v in overrides.items():
                params[k] = v
            step["params"] = params

    _save_custom_presets(presets)
    return {"ok": True, "key": preset_key, "name": target.get("name")}


@router.delete("/presets/{preset_key}")
def delete_custom_preset(preset_key: str):
    """删除自定义预设。仅允许删除 _custom 预设。"""
    if preset_key in _SYSTEM_KEYS:
        raise HTTPException(403, "系统预设不可删除")

    presets = _load_custom_presets()
    before = len(presets)
    presets = [p for p in presets if p.get("key") != preset_key]
    if len(presets) == before:
        raise HTTPException(404, f"自定义预设 {preset_key} 不存在")

    _save_custom_presets(presets)
    return {"ok": True, "deleted": preset_key, "remaining": len(presets)}


# ─────────────────────────────────────────────────────────────────────────
# 引流账号配置 — 与 TikTok 同结构,但默认排序 WA 优先
# ─────────────────────────────────────────────────────────────────────────

@router.get("/referral-config")
def get_facebook_referral_config(persona_key: Optional[str] = None):
    """获取所有设备的 Facebook 引流账号配置。

    数据源与 TikTok 共用 chat_messages.yaml.device_referrals。
    ``priority_order`` 不再硬编码 WA 优先——P2-UI Sprint 起改为按 persona
    节点读取(来源 fb_target_personas.yaml 的 ``referral_priority`` 字段)，
    实现"改目标客群即自动切换引流渠道顺序"的单一事实源。

    查询参数 ``persona_key`` 可显式覆盖(用于前端预览非默认客群的排序)；
    不传则用默认客群(jp_female_midlife → LINE/IG/WA/TG)。
    """
    data = {}
    if _CHAT_MSG_YAML.exists():
        with open(_CHAT_MSG_YAML, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    persona = get_persona_display(persona_key)
    return {
        "referrals": data.get("device_referrals", {}),
        "priority_order": persona["referral_priority"],
        "persona": persona,  # 前端用它拿 display_label/display_flag 生成文案
        "_platform": "facebook",
    }


@router.get("/active-persona")
def get_active_persona(persona_key: Optional[str] = None):
    """返回当前目标客群的展示包 + 可选客群列表(供前端下拉渲染)。

    * ``active`` = 当前 persona(默认或 query 指定)的完整 display 字段
    * ``available`` = 全部 active=True 的 persona 的精简卡片列表
    * ``default_key`` = 当前**生效**默认（含 ``data/fb_active_persona_override.json``）
    * ``yaml_default_key`` = YAML 文件内 ``default_persona``（不含 override）
    * ``override_key`` = 仅当存在 override 文件时返回
    前端在 ⚡ 配置执行流程 和 🔗 引流账号 两个弹窗里统一调这一个接口。
    """
    return {
        "active": get_persona_display(persona_key),
        "available": list_persona_displays(),
        "default_key": get_default_persona_key(),
        "yaml_default_key": get_yaml_default_persona_key(),
        "override_key": read_active_persona_override(),
    }


# ════════════════════════════════════════════════════════
# 客群 Profile CRUD（用户自定义客群，SQLite 持久化）
# ════════════════════════════════════════════════════════

def _ensure_persona_profiles_table():
    from src.host.database import get_conn
    with get_conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS fb_persona_profiles (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            persona_key     TEXT NOT NULL UNIQUE,
            name            TEXT NOT NULL,
            display_flag    TEXT NOT NULL DEFAULT '🌐',
            display_label   TEXT NOT NULL,
            short_label     TEXT NOT NULL DEFAULT '',
            country_code    TEXT NOT NULL DEFAULT '',
            country_zh      TEXT NOT NULL DEFAULT '',
            language        TEXT NOT NULL DEFAULT 'en',
            gender          TEXT NOT NULL DEFAULT '',
            age_min         INTEGER DEFAULT NULL,
            age_max         INTEGER DEFAULT NULL,
            interest_topics TEXT NOT NULL DEFAULT '[]',
            seed_group_keywords TEXT NOT NULL DEFAULT '[]',
            referral_priority TEXT NOT NULL DEFAULT '["whatsapp","telegram","instagram","line"]',
            is_system       INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )""")


@router.get("/persona-profiles")
def fb_list_persona_profiles():
    """列出所有客群 profile（YAML 系统预设 + 用户自建）。"""
    _ensure_persona_profiles_table()
    # YAML 系统预设
    system_profiles = []
    for p in list_persona_displays():
        p["is_system"] = True
        p["source"] = "yaml"
        system_profiles.append(p)
    # 用户自建 (SQLite)
    from src.host.database import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM fb_persona_profiles ORDER BY updated_at DESC"
        ).fetchall()
    user_profiles = []
    for r in rows:
        user_profiles.append({
            "id": r["id"],
            "persona_key": r["persona_key"],
            "name": r["name"],
            "display_flag": r["display_flag"],
            "display_label": r["display_label"],
            "short_label": r["short_label"],
            "country_code": r["country_code"],
            "country_zh": r["country_zh"],
            "language": r["language"],
            "gender": r["gender"],
            "age_min": r["age_min"],
            "age_max": r["age_max"],
            "interest_topics": _json.loads(r["interest_topics"] or "[]"),
            "seed_group_keywords": _json.loads(r["seed_group_keywords"] or "[]"),
            "referral_priority": _json.loads(r["referral_priority"] or "[]"),
            "is_system": False,
            "source": "user",
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return {"profiles": system_profiles + user_profiles}


@router.post("/persona-profiles")
def fb_create_persona_profile(body: dict = Body(default={})):
    """创建自定义客群 profile。"""
    _ensure_persona_profiles_table()
    import time as _t
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "name is required")
    country_code = (body.get("country_code") or "").upper().strip()
    language = (body.get("language") or "").strip() or "en"
    gender = (body.get("gender") or "").strip()
    age_min = body.get("age_min")
    age_max = body.get("age_max")
    interest_topics = body.get("interest_topics") or []
    seed_group_keywords = body.get("seed_group_keywords") or []
    referral_priority = body.get("referral_priority") or ["whatsapp", "telegram", "instagram", "line"]
    # 生成 persona_key
    slug = name.lower().replace(" ", "_").replace("·", "").replace("　", "_")
    import re
    slug = re.sub(r'[^a-z0-9_\u4e00-\u9fff]', '', slug) or f"custom_{int(_t.time())}"
    persona_key = body.get("persona_key") or slug
    # display 字段
    flag = body.get("display_flag") or _country_to_flag(country_code)
    display_label = body.get("display_label") or f"{flag} {name}"
    short_label = body.get("short_label") or name
    country_zh = body.get("country_zh") or ""
    from src.host.database import get_conn
    now = _t.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_conn() as conn:
            conn.execute("""INSERT INTO fb_persona_profiles
                (persona_key, name, display_flag, display_label, short_label,
                 country_code, country_zh, language, gender, age_min, age_max,
                 interest_topics, seed_group_keywords, referral_priority, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (persona_key, name, flag, display_label, short_label,
                 country_code, country_zh, language, gender, age_min, age_max,
                 _json.dumps(interest_topics, ensure_ascii=False),
                 _json.dumps(seed_group_keywords, ensure_ascii=False),
                 _json.dumps(referral_priority, ensure_ascii=False),
                 now, now))
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"persona_key '{persona_key}' already exists")
        raise
    return {"ok": True, "persona_key": persona_key, "display_label": display_label}


@router.post("/persona-profiles/suggest")
def fb_suggest_persona_content(body: dict = Body(default={})):
    """根据国家/性别/已选兴趣，推荐相关的兴趣标签和群组关键词。

    请求体:
      country_code: str (如 "JP")
      gender: str ("female"|"male"|"")
      current_topics: list[str] (已选兴趣，用于推荐群组关键词)
      type: str ("topics"|"keywords"|"both")
    """
    country = (body.get("country_code") or "").upper()
    gender = body.get("gender") or ""
    current_topics = body.get("current_topics") or []
    suggest_type = body.get("type") or "both"

    result: Dict[str, Any] = {}

    # ── 推荐兴趣标签 ──
    if suggest_type in ("topics", "both"):
        pool = _INTEREST_KNOWLEDGE.get(country) or _INTEREST_KNOWLEDGE["_default"]
        candidates = list(pool.get(gender) or pool.get("") or [])
        # 排除已选
        suggestions = [t for t in candidates if t not in current_topics]
        result["topics"] = suggestions[:12]

    # ── 推荐群组关键词 ──
    if suggest_type in ("keywords", "both"):
        kw_pool = _GROUP_KEYWORD_KNOWLEDGE.get(country) or _GROUP_KEYWORD_KNOWLEDGE["_default"]
        kw_list = list(kw_pool.get("_base") or [])
        # 根据已选兴趣追加专属关键词
        for topic in current_topics:
            extras = kw_pool.get(topic) or []
            kw_list.extend(extras)
        # 去重保序
        seen = set()
        unique_kw = []
        for k in kw_list:
            if k not in seen:
                seen.add(k)
                unique_kw.append(k)
        result["keywords"] = unique_kw[:15]

    result["source"] = "knowledge_base"
    return result


@router.get("/persona-profiles/compare")
def fb_compare_personas():
    """横向对比所有客群的核心 KPI（候选人数/好友/回复率）— 单次聚合查询。"""
    from src.host.database import get_conn
    from src.host.fb_targets_store import ensure_schema
    ensure_schema()
    all_personas = list_persona_displays()
    _ensure_persona_profiles_table()
    with get_conn() as conn:
        user_rows = conn.execute("SELECT persona_key, name, display_flag FROM fb_persona_profiles WHERE is_system=0").fetchall()
        # 单次 GROUP BY 聚合 targets
        t_agg = conn.execute(
            "SELECT persona_key, COUNT(*) AS total, "
            "SUM(CASE WHEN status='friended' THEN 1 ELSE 0 END) AS friended, "
            "SUM(CASE WHEN qualified=1 THEN 1 ELSE 0 END) AS qualified "
            "FROM fb_targets_global GROUP BY persona_key"
        ).fetchall()
        # 单次 GROUP BY 聚合 greetings
        g_agg = conn.execute(
            "SELECT persona_key, SUM(sent_count) AS sent, SUM(replied_count) AS replied "
            "FROM fb_greeting_library GROUP BY persona_key"
        ).fetchall()
    t_map = {r["persona_key"]: dict(r) for r in t_agg}
    g_map = {r["persona_key"]: dict(r) for r in g_agg}
    user_keys = {r["persona_key"] for r in user_rows}
    # 合并 persona_key 列表
    all_keys = [p["persona_key"] for p in all_personas]
    for r in user_rows:
        if r["persona_key"] not in all_keys:
            all_keys.append(r["persona_key"])
    # 组装结果
    display_map = {p["persona_key"]: p for p in all_personas}
    for r in user_rows:
        if r["persona_key"] not in display_map:
            display_map[r["persona_key"]] = {"persona_key": r["persona_key"], "display_flag": r["display_flag"], "name": r["name"]}
    results = []
    for key in all_keys:
        t = t_map.get(key) or {}
        g = g_map.get(key) or {}
        total = t.get("total") or 0
        sent = int(g.get("sent") or 0)
        replied = int(g.get("replied") or 0)
        display = display_map.get(key) or {}
        results.append({
            "persona_key": key,
            "flag": display.get("display_flag", "🌐"),
            "label": display.get("short_label") or display.get("name") or key,
            "source": "user" if key in user_keys else "system",
            "targets": total,
            "friended": t.get("friended") or 0,
            "qualified": t.get("qualified") or 0,
            "sent": sent,
            "replied": replied,
            "reply_rate": round(replied / sent, 3) if sent > 0 else 0,
        })
    results.sort(key=lambda x: x["targets"], reverse=True)
    return {"personas": results}


@router.put("/persona-profiles/{persona_key}")
def fb_update_persona_profile(persona_key: str, body: dict = Body(default={})):
    """更新用户自建客群 profile。"""
    _ensure_persona_profiles_table()
    import time as _t
    from src.host.database import get_conn
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM fb_persona_profiles WHERE persona_key=?", (persona_key,)
        ).fetchone()
        if not existing:
            raise HTTPException(404, f"profile '{persona_key}' not found (system profiles are read-only)")
        sets = []
        params = []
        for col in ("name", "display_flag", "display_label", "short_label",
                    "country_code", "country_zh", "language", "gender"):
            if col in body:
                sets.append(f"{col}=?")
                params.append(body[col])
        for col in ("age_min", "age_max"):
            if col in body:
                sets.append(f"{col}=?")
                params.append(body[col])
        for col in ("interest_topics", "seed_group_keywords", "referral_priority"):
            if col in body:
                sets.append(f"{col}=?")
                params.append(_json.dumps(body[col], ensure_ascii=False))
        if not sets:
            return {"ok": True, "unchanged": True}
        sets.append("updated_at=?")
        params.append(_t.strftime("%Y-%m-%d %H:%M:%S"))
        params.append(persona_key)
        conn.execute(f"UPDATE fb_persona_profiles SET {','.join(sets)} WHERE persona_key=?", params)
    return {"ok": True, "persona_key": persona_key}


@router.delete("/persona-profiles/{persona_key}")
def fb_delete_persona_profile(persona_key: str):
    """删除用户自建客群 profile。"""
    _ensure_persona_profiles_table()
    from src.host.database import get_conn
    with get_conn() as conn:
        r = conn.execute("DELETE FROM fb_persona_profiles WHERE persona_key=? AND is_system=0", (persona_key,))
        if r.rowcount == 0:
            raise HTTPException(404, "profile not found or is system-protected")
    return {"ok": True, "deleted": persona_key}


@router.get("/persona-profiles/{persona_key}/stats")
def fb_persona_profile_stats(persona_key: str):
    """返回指定客群的效果预览统计（候选数、话术统计、转化漏斗）。"""
    from src.host.database import get_conn
    from src.host.fb_targets_store import get_greeting_stats, ensure_schema
    ensure_schema()
    with get_conn() as conn:
        # 候选人统计
        target_row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status='discovered' THEN 1 ELSE 0 END) AS discovered, "
            "SUM(CASE WHEN status='friended' THEN 1 ELSE 0 END) AS friended, "
            "SUM(CASE WHEN status='greeted' THEN 1 ELSE 0 END) AS greeted, "
            "SUM(CASE WHEN qualified=1 THEN 1 ELSE 0 END) AS qualified "
            "FROM fb_targets_global WHERE persona_key=?", (persona_key,)
        ).fetchone()
        # 话术库统计
        greeting_row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(sent_count) AS total_sent, "
            "SUM(replied_count) AS total_replied "
            "FROM fb_greeting_library WHERE persona_key=?", (persona_key,)
        ).fetchone()
        # 最佳话术
        best = conn.execute(
            """SELECT text_ja, reply_rate, sent_count
               FROM fb_greeting_library
               WHERE persona_key=? AND sent_count >= 2
               ORDER BY reply_rate DESC LIMIT 1""",
            (persona_key,),
        ).fetchone()
    targets = dict(target_row) if target_row else {}
    greetings = dict(greeting_row) if greeting_row else {}
    total_sent = int(greetings.get("total_sent") or 0)
    total_replied = int(greetings.get("total_replied") or 0)
    return {
        "persona_key": persona_key,
        "targets": {
            "total": targets.get("total") or 0,
            "discovered": targets.get("discovered") or 0,
            "friended": targets.get("friended") or 0,
            "greeted": targets.get("greeted") or 0,
            "qualified": targets.get("qualified") or 0,
        },
        "greetings": {
            "library_count": int(greetings.get("total") or 0),
            "total_sent": total_sent,
            "total_replied": total_replied,
            "avg_reply_rate": round(total_replied / total_sent, 3) if total_sent > 0 else 0,
        },
        "best_greeting": {
            "text": (best["text_ja"][:40] + "…") if best and len(best["text_ja"]) > 40 else (best["text_ja"] if best else ""),
            "reply_rate": best["reply_rate"] if best else 0,
            "sent_count": best["sent_count"] if best else 0,
        } if best else None,
    }


@router.post("/persona-profiles/{persona_key}/clone")
def fb_clone_persona_profile(persona_key: str, body: dict = Body(default={})):
    """克隆一个客群 profile（系统/用户均可克隆为新的用户 profile）。"""
    _ensure_persona_profiles_table()
    import time as _t
    # 先获取源 profile
    source = None
    # 尝试从系统预设获取
    for p in list_persona_displays():
        if p.get("persona_key") == persona_key:
            source = p
            break
    # 尝试从用户自建获取
    if not source:
        from src.host.database import get_conn
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM fb_persona_profiles WHERE persona_key=?", (persona_key,)
            ).fetchone()
            if row:
                source = {
                    "name": row["name"],
                    "display_flag": row["display_flag"],
                    "country_code": row["country_code"],
                    "country_zh": row["country_zh"],
                    "language": row["language"],
                    "gender": row["gender"],
                    "age_min": row["age_min"],
                    "age_max": row["age_max"],
                    "interest_topics": _json.loads(row["interest_topics"] or "[]"),
                    "seed_group_keywords": _json.loads(row["seed_group_keywords"] or "[]"),
                    "referral_priority": _json.loads(row["referral_priority"] or "[]"),
                }
    if not source:
        raise HTTPException(404, f"source persona '{persona_key}' not found")
    # 新名称
    new_name = body.get("name") or (source.get("name", persona_key) + " (副本)")
    clone_body = {
        "name": new_name,
        "country_code": source.get("country_code", ""),
        "language": source.get("language", "en"),
        "gender": source.get("gender", ""),
        "age_min": source.get("age_min"),
        "age_max": source.get("age_max"),
        "interest_topics": source.get("interest_topics", []),
        "seed_group_keywords": source.get("seed_group_keywords", []),
        "referral_priority": source.get("referral_priority", ["whatsapp", "telegram", "instagram", "line"]),
        "display_flag": source.get("display_flag", "🌐"),
    }
    # 复用 create 逻辑
    return fb_create_persona_profile(clone_body)


def _country_to_flag(code: str) -> str:
    """Convert 2-letter country code to flag emoji."""
    if not code or len(code) != 2:
        return "🌐"
    return chr(0x1F1E6 + ord(code[0]) - ord('A')) + chr(0x1F1E6 + ord(code[1]) - ord('A'))


# ════════════════════════════════════════════════════════
# 智能推荐（规则库 + 可选 AI 增强）
# ════════════════════════════════════════════════════════

# 从 YAML 文件加载知识库（支持热编辑，重启即生效）
def _load_persona_knowledge():
    """从 config/persona_knowledge.yaml 加载兴趣/关键词知识库。"""
    import yaml
    from pathlib import Path
    yaml_path = Path(__file__).resolve().parent.parent.parent.parent / "config" / "persona_knowledge.yaml"
    if not yaml_path.exists():
        logger.warning("persona_knowledge.yaml not found at %s, using empty knowledge base", yaml_path)
        return {}, {}
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    # 转换 interest_topics: YAML 中用 "all" 表示不限性别，代码中用 ""
    raw_interests = data.get("interest_topics") or {}
    interests = {}
    for country, genders in raw_interests.items():
        interests[country] = {}
        for g_key, tags in genders.items():
            mapped_key = "" if g_key == "all" else g_key
            interests[country][mapped_key] = tags
    keywords = data.get("group_keywords") or {}
    return interests, keywords

_INTEREST_KNOWLEDGE, _GROUP_KEYWORD_KNOWLEDGE = _load_persona_knowledge()


@router.post("/name-hunter/suggest")
def post_name_hunter_suggest(body: dict = Body(default={})):
    """生成日本女性常用名种子。

    这里只生成“搜索入口”，不是客户判定结果；最终是否 37 岁以上女性仍由
    profile 资料与 L1/L2 画像识别确认。
    """
    persona_key = body.get("persona_key") or get_default_persona_key()
    persona = get_persona_display(persona_key)
    names = _generate_jp_midlife_names(
        count=int(body.get("count") or 30),
        age_pack=str(body.get("age_pack") or "mixed"),
    )
    rows = [_score_name_hunter_target(n, persona) for n in names]
    return {
        "ok": True,
        "persona": persona,
        "count": len(rows),
        "names": rows,
        "note": "姓名仅用于搜索入口；需搜索资料并确认画像后再触达。",
    }


@router.post("/name-hunter/preview")
def post_name_hunter_preview(body: dict = Body(default={})):
    """预处理点名添加名单：拆分、去重、静态评分、给出执行建议。"""
    persona_key = body.get("persona_key") or get_default_persona_key()
    persona = get_persona_display(persona_key)
    raw = body.get("names")
    if raw is None:
        raw = body.get("add_friend_targets") or []
    targets = _split_name_targets(raw)
    scored = [_score_name_hunter_target(t, persona) for t in targets]
    source_ref = body.get("source_ref") or "name_hunter"
    source_quality = {
        "source_ref": source_ref,
        "source_health": "new",
        "qualified_rate": 0.0,
        "recommended_action": "collect_more_prescreen",
    }
    try:
        from src.host.fb_targets_store import name_hunter_source_quality
        source_quality = name_hunter_source_quality(persona_key=persona_key, source_ref=source_ref)
    except Exception:
        pass
    if source_quality.get("source_health") == "degraded":
        for row in scored:
            row["score"] = max(0, int(row.get("score") or 0) - 15)
            row["stage"] = (
                "high_confidence_seed" if row["score"] >= 80
                else ("review_required" if row["score"] >= 50 else "weak_seed")
            )
            row["recommended_action"] = "manual_review"
            row.setdefault("reasons", []).append("来源名字包历史 qualified 率偏低，自动降权")
    if body.get("persist", True):
        try:
            from src.host.fb_targets_store import upsert_name_hunter_candidate
            for row in scored:
                s = float(row.get("score") or 0)
                st = "seeded" if s >= 80 else ("review_required" if s >= 50 else "weak_seed")
                row["candidate_id"] = upsert_name_hunter_candidate(
                    name=row.get("name") or "",
                    persona_key=persona_key,
                    seed_score=s,
                    seed_stage=row.get("stage") or "",
                    status=st,
                    source_ref=source_ref or row.get("source") or "name_hunter",
                    insights={
                        "reasons": row.get("reasons") or [],
                        "recommended_action": row.get("recommended_action") or "",
                        "source": source_ref or row.get("source") or "name_hunter",
                        "source_quality": source_quality,
                    },
                )
        except Exception as e:
            logger.debug("[name_hunter_preview] candidate persist skipped: %s", e)
    high = [r for r in scored if r["score"] >= 80]
    review = [r for r in scored if 50 <= r["score"] < 80]
    weak = [r for r in scored if r["score"] < 50]
    return {
        "ok": True,
        "persona": persona,
        "input_count": len(targets),
        "unique_count": len(scored),
        "high_confidence_count": len(high),
        "review_required_count": len(review),
        "weak_count": len(weak),
        "launch_targets": high,
        "rows": scored,
        "source_quality": source_quality,
        "policy": {
            "default_launch_mode": "confirm_then_launch",
            "auto_touch_allowed": False,
            "minimum_score_for_launch": 80,
            "strict_profile_l2_required": True,
            "qualified_requires_age_37plus_evidence": True,
            "age_band_30s_requires_manual_review": True,
        },
    }


@router.get("/name-hunter/candidates")
def get_name_hunter_candidates(persona_key: Optional[str] = None,
                               status: str = "",
                               q: str = "",
                               min_seed_score: int = 0,
                               limit: int = 100):
    """候选资料池：展示点名添加产生并持续更新的候选。"""
    pk = persona_key or get_default_persona_key()
    try:
        from src.host.fb_targets_store import list_name_hunter_candidates
        rows = list_name_hunter_candidates(
            persona_key=pk,
            status=status.strip(),
            q=q.strip(),
            min_seed_score=float(min_seed_score or 0),
            limit=limit,
        )
    except Exception as e:
        raise HTTPException(500, f"query candidates failed: {e}") from e
    return {
        "ok": True,
        "persona": get_persona_display(pk),
        "items": rows,
        "count": len(rows),
        "policy": {
            "touch_requires_status": "qualified",
            "touch_requires_profile_l2": True,
            "minimum_seed_score": 80,
        },
    }


@router.get("/name-hunter/stats")
def get_name_hunter_stats(persona_key: Optional[str] = None):
    """名字包/候选池运营复盘统计。"""
    pk = persona_key or get_default_persona_key()
    try:
        from src.host.fb_targets_store import name_hunter_stats
        stats = name_hunter_stats(persona_key=pk)
    except Exception as e:
        raise HTTPException(500, f"query name hunter stats failed: {e}") from e
    return {"ok": True, "persona": get_persona_display(pk), **stats}


@router.post("/name-hunter/candidates/{candidate_id}/action")
def post_name_hunter_candidate_action(candidate_id: int, body: dict = Body(default={})):
    """候选池人工操作：拉黑、重筛、标记 qualified/rejected。"""
    action = (body.get("action") or "").strip().lower()
    if action not in {"blocklist", "requeue", "qualify", "reject"}:
        raise HTTPException(400, "action 必须是 blocklist/requeue/qualify/reject")
    try:
        from src.host.fb_targets_store import add_to_blocklist, get_target, mark_status
        target = get_target(candidate_id)
        if not target:
            raise HTTPException(404, "candidate not found")
        name = target.get("display_name") or target.get("identity_key") or ""
        if action == "blocklist":
            add_to_blocklist(name, reason=body.get("reason") or "name_hunter_manual")
            status = "opt_out"
            mark_status(candidate_id, status, device_id=body.get("device_id") or "",
                        extra_fields={"qualified": 0})
        elif action == "requeue":
            status = "seeded"
            mark_status(candidate_id, status, device_id=body.get("device_id") or "",
                        extra_fields={"qualified": 0})
        elif action == "qualify":
            status = "qualified"
            mark_status(candidate_id, status, device_id=body.get("device_id") or "",
                        extra_fields={"qualified": 1})
        else:
            status = "rejected"
            mark_status(candidate_id, status, device_id=body.get("device_id") or "",
                        extra_fields={"qualified": 0})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"candidate action failed: {e}") from e
    return {"ok": True, "candidate_id": candidate_id, "action": action, "status": status}


@router.post("/name-hunter/prescreen")
def post_name_hunter_prescreen(body: dict = Body(default={})):
    """创建“只搜索资料+画像识别”的点名预筛任务，不触达。"""
    device_id = (body.get("device_id") or "").strip()
    if not device_id:
        raise HTTPException(400, "device_id 必填")
    persona_key = body.get("persona_key") or get_default_persona_key()
    params = {
        "persona_key": persona_key,
        "max_targets": int(body.get("max_targets") or 20),
        "min_seed_score": float(body.get("min_seed_score") or 80),
        "status": body.get("status") or "seeded",
        "shot_count": int(body.get("shot_count") or 3),
        "inter_target_min_sec": float(body.get("inter_target_min_sec") or 20),
        "inter_target_max_sec": float(body.get("inter_target_max_sec") or 34),
    }
    if body.get("candidates"):
        params["candidates"] = body.get("candidates")
    try:
        from src.host.task_store import create_task
        tid = create_task("facebook_name_hunter_prescreen", device_id, params)
    except Exception as e:
        raise HTTPException(500, f"create prescreen task failed: {e}") from e
    return {"ok": True, "task_id": tid, "type": "facebook_name_hunter_prescreen"}


@router.post("/name-hunter/touch-qualified")
def post_name_hunter_touch_qualified(body: dict = Body(default={})):
    """创建“只触达 qualified 候选”的任务。"""
    device_id = (body.get("device_id") or "").strip()
    if not device_id:
        raise HTTPException(400, "device_id 必填")
    persona_key = body.get("persona_key") or get_default_persona_key()
    max_targets = int(body.get("max_targets") or 5)
    min_ready = int(body.get("min_qualified_ready") or 1)
    try:
        from src.host.fb_targets_store import name_hunter_touch_targets
        ready = name_hunter_touch_targets(persona_key=persona_key, limit=max(max_targets, min_ready, 1))
    except Exception as e:
        raise HTTPException(500, f"query qualified candidates failed: {e}") from e
    if len(ready) < min_ready:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "qualified 候选不足，先执行资料预筛或人工复核",
                "qualified_ready": len(ready),
                "min_qualified_ready": min_ready,
            },
        )
    params = {
        "persona_key": persona_key,
        "max_targets": max_targets,
        "max_friends_per_run": int(body.get("max_friends_per_run") or 5),
        "min_seed_score": float(body.get("min_seed_score") or 80),
        "send_greeting_inline": bool(body.get("send_greeting_inline", True)),
        "verification_note": body.get("verification_note") or "",
        "greeting": body.get("greeting") or "",
        "_preset_key": "name_hunter",
    }
    try:
        from src.host.task_store import create_task
        tid = create_task("facebook_name_hunter_touch_qualified", device_id, params)
    except Exception as e:
        raise HTTPException(500, f"create touch task failed: {e}") from e
    return {"ok": True, "task_id": tid, "type": "facebook_name_hunter_touch_qualified"}


@router.post("/active-persona")
def post_active_persona(request: Request, body: dict = Body(default={})):
    """切换运行时默认目标客群（不写 YAML；持久化到 ``data/fb_active_persona_override.json``）。

    Body:
      * ``{"persona_key": "jp_female_midlife"}`` — 设为默认
      * ``{"clear": true}`` — 删除 override，恢复仅 YAML ``default_persona``

    可选环境变量 ``OPENCLAW_FB_ACTIVE_PERSONA_REQUIRE_KEY=1``：此 POST 仅接受 ``X-API-Key`` 与
    ``OPENCLAW_API_KEY`` 完全一致（不接受仅登录会话），用于公网主控缩小写盘攻击面。
    """
    if _STRICT_FB_ACTIVE_PERSONA_POST:
        expected = (os.environ.get("OPENCLAW_API_KEY") or "").strip()
        if not expected:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "已启用 OPENCLAW_FB_ACTIVE_PERSONA_REQUIRE_KEY 但未配置 OPENCLAW_API_KEY",
                    "hint": "设置 OPENCLAW_API_KEY，或将该开关置 0。",
                },
            )
        key = (request.headers.get("X-API-Key") or "").strip()
        if key != expected:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "active-persona 严格模式：须携带与 OPENCLAW_API_KEY 一致的 X-API-Key",
                    "hint": "浏览器仅会话无效；关闭 OPENCLAW_FB_ACTIVE_PERSONA_REQUIRE_KEY 可恢复与会话共用鉴权。",
                },
            )
    if body.get("clear"):
        clear_active_persona_override()
        return {"ok": True, "cleared": True, **get_active_persona()}
    pk = (body.get("persona_key") or body.get("key") or "").strip()
    if not pk:
        raise HTTPException(400, "persona_key 必填，或传 clear=true")
    try:
        set_active_persona_override(pk)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "persona_key": pk, **get_active_persona()}


@router.post("/referral-config")
def set_facebook_referral_config(body: dict = Body(default={})):
    """配置某设备(或全部设备)的引流账号。

    Body:
      {"device_id": "...", "whatsapp": "+39...", "telegram": "@u"}
      {"all": true, "whatsapp": "+39..."}  # 全部设备
    """
    data = {}
    if _CHAT_MSG_YAML.exists():
        with open(_CHAT_MSG_YAML, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    data.setdefault("device_referrals", {})

    _RESERVED = {"device_id", "all"}
    contacts = {k: v for k, v in body.items()
                if k not in _RESERVED and not k.startswith("_")}

    if body.get("all"):
        from src.device_control.device_manager import get_device_manager
        mgr = get_device_manager(str(_DEVICES_YAML))
        devices = [d.device_id for d in mgr.get_all_devices()]
        for did in devices:
            ref = data["device_referrals"].get(did, {})
            for app, val in contacts.items():
                if val:
                    ref[app] = val
                elif app in ref:
                    del ref[app]
            data["device_referrals"][did] = ref
        updated = len(devices)
    else:
        device_id = body.get("device_id", "")
        if not device_id:
            raise HTTPException(400, "device_id 必填(或 all=true)")
        ref = data["device_referrals"].get(device_id, {})
        for app, val in contacts.items():
            if val:
                ref[app] = val
            elif app in ref:
                del ref[app]
        data["device_referrals"][device_id] = ref
        updated = 1

    with open(_CHAT_MSG_YAML, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    return {"ok": True, "updated": updated, "_platform": "facebook"}


# BB2a: 引流账号自动填充 — 从已配置设备克隆到未配置设备
@router.post("/referral-config/auto-fill")
def auto_fill_referral_config():
    """自动将首个已配置设备的引流账号克隆到所有未配置设备。

    规则:
      - 只填充**完全没有引流配置**的设备（不覆盖已有配置）
      - 模板来源 = 第一个有非空引流配置的设备
      - 返回填充数量 + 仍缺失的渠道提示
    """
    data = {}
    if _CHAT_MSG_YAML.exists():
        with open(_CHAT_MSG_YAML, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    data.setdefault("device_referrals", {})

    from src.device_control.device_manager import get_device_manager
    mgr = get_device_manager(str(_DEVICES_YAML))
    all_devs = [d.device_id for d in mgr.get_all_devices()]

    # 找模板设备（首个有非空引流配置的）
    template = {}
    template_device = ""
    for did in all_devs:
        ref = data["device_referrals"].get(did, {})
        has_any = any(v and str(v).strip() for v in ref.values())
        if has_any:
            template = ref
            template_device = did
            break

    if not template:
        return {
            "ok": False, "filled": 0, "total_devices": len(all_devs),
            "message": "没有找到已配置引流账号的设备，请先手动配置一台",
            "_platform": "facebook",
        }

    # 克隆到未配置设备
    filled = 0
    for did in all_devs:
        if did == template_device:
            continue
        ref = data["device_referrals"].get(did, {})
        has_any = any(v and str(v).strip() for v in ref.values())
        if has_any:
            continue  # 已有配置,跳过
        data["device_referrals"][did] = dict(template)
        filled += 1

    if filled > 0:
        with open(_CHAT_MSG_YAML, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    # 检查模板中主渠道是否配置
    persona = get_persona_display()
    priority = persona.get("referral_priority", [])
    main_ch = priority[0] if priority else "line"
    main_configured = bool(template.get(main_ch, "").strip())

    return {
        "ok": True, "filled": filled,
        "template_device": template_device[:12],
        "total_devices": len(all_devs),
        "already_configured": len(all_devs) - filled - 1,  # 减去模板自身
        "main_channel": main_ch,
        "main_channel_configured": main_configured,
        "channels_in_template": {k: bool(v and str(v).strip()) for k, v in template.items()},
        "_platform": "facebook",
    }


# ─────────────────────────────────────────────────────────────────────────
# 设备级一键启动 — 与 TikTok device/{id}/launch 同构
# ─────────────────────────────────────────────────────────────────────────

@router.post("/device/{device_id}/launch")
def fb_device_launch(device_id: str, body: dict = Body(default={})):
    """为指定设备启动 Facebook 工作流。

    支持两种模式:
      A. flow_steps=[{type, params}, ...]  自定义步骤序列(前端流程配置器传入)
      B. preset_key="full_funnel"          直接运行预设
    """
    flow_steps = body.get("flow_steps")
    preset_key = body.get("preset_key")

    # B 模式: 用预设展开为步骤 (BB3b: 同时搜索系统预设和自定义预设)
    if preset_key and not flow_steps:
        preset = next((p for p in FB_FLOW_PRESETS if p["key"] == preset_key), None)
        if not preset:
            preset = next((p for p in _load_custom_presets() if p.get("key") == preset_key), None)
        if not preset:
            raise HTTPException(404, f"未知预设 key: {preset_key}")

        # Phase C1: 优先走 chain 编排 (支持断点续跑 + 单步重试)
        _chain_id = preset.get("chain_id")
        if _chain_id and not body.get("force_legacy"):
            try:
                from src.host.task_chain import create_chain
                chain_params = {
                    k: body.get(k) for k in (
                        "target_groups", "verification_note", "greeting",
                        "max_friends_per_run", "persona_key",
                        "target_country", "language",
                    ) if body.get(k) is not None
                }
                chain_params["_preset_key"] = preset_key
                result = create_chain(_chain_id, device_id,
                                      params_override=chain_params)
                run_id = result.get("run_id", "")
                return {
                    "ok": True,
                    "device_id": device_id,
                    "preset_key": preset_key,
                    "chain_id": _chain_id,
                    "chain_run_id": run_id,
                    "mode": "chain",
                    "message": f"已通过链式编排启动 {preset.get('name', preset_key)}"
                               f" (支持断点续跑)",
                }
            except Exception as _ce:
                logger.warning(
                    "[fb_launch] chain=%s 启动失败, 回退到 legacy: %s",
                    _chain_id, _ce)
                # 回退到传统模式

        flow_steps = preset["steps"]

    if not flow_steps or not isinstance(flow_steps, list):
        raise HTTPException(400, "需要传入 flow_steps[] 或 preset_key")

    # 从 body 注入 GEO/语言/人群预设/目标群组 到每个 step
    geo_country = body.get("target_country") or ""
    geo_lang = body.get("language") or ""
    audience_preset = body.get("audience_preset") or ""
    target_groups = body.get("target_groups") or []
    if isinstance(target_groups, str):
        target_groups = [g.strip() for g in target_groups.split(",") if g.strip()]
    verification_note = body.get("verification_note") or ""
    # 2026-04-23: 点名添加 (name_hunter 预设) 的名字列表入口
    # body.add_friend_targets 支持三种形式:
    #   * List[Dict] : [{"name": "山田花子"}, ...]
    #   * List[str]  : ["山田花子", "佐藤美咲"]
    #   * str        : "山田花子\n佐藤美咲" / "山田花子, 佐藤美咲"
    raw_targets = body.get("add_friend_targets") or []
    add_friend_targets: List[dict] = _split_name_targets(raw_targets)
    # body.greeting 允许用户覆盖 persona 默认打招呼文案（name_hunter 常用）
    greeting_override = body.get("greeting") or ""

    # P2-UI Sprint：persona 自动补齐安全网
    # 前端可以在 body 里指定 persona_key 明确客群；否则回退默认（当前=日本中年女性）。
    # 如果前端忘了传 GEO/lang，从 persona 的 country_code/language 自动补齐，
    # 避免出现"target_country='' + persona=JP"这种错配送到 automation 层。
    persona_key_in = body.get("persona_key") or get_default_persona_key()
    persona_ctx = get_persona_display(persona_key_in)
    if not geo_country:
        geo_country = persona_ctx.get("country_code") or ""
    if not geo_lang:
        geo_lang = persona_ctx.get("language") or ""
    if not target_groups and persona_ctx.get("seed_group_keywords"):
        # 没显式指定群组时，用 persona 的默认种子词（日本女性 = ママ友等）
        target_groups = list(persona_ctx["seed_group_keywords"])[:2]
    if preset_key == "name_hunter" and add_friend_targets:
        enriched_targets: List[dict] = []
        for item in add_friend_targets:
            if isinstance(item, dict) and item.get("seed_score") is not None:
                enriched_targets.append(item)
            else:
                enriched_targets.append(_score_name_hunter_target(item, persona_ctx))
        add_friend_targets = enriched_targets

    # ── P0-3: 必填参数前置校验 ──────────────────────────────────
    # preset 声明 needs_input 的字段，在所有兜底之后仍为空 → 422 拒绝创建任务
    # 同时把 input_schema 回传，前端可据此动态生成填表对话框（避免用户瞎点"一键启动"
    # 然后任务失败的反馈循环）。
    if preset_key:
        _spec = next((p for p in FB_FLOW_PRESETS if p["key"] == preset_key), None)
        if _spec and _spec.get("needs_input"):
            _provided = {
                "target_groups": target_groups,
                "verification_note": verification_note,
                "greeting": greeting_override,
                "add_friend_targets": add_friend_targets,
                "max_friends_per_run": body.get("max_friends_per_run"),
                "max_members": body.get("max_members"),
            }
            _missing = _validate_preset_inputs(_spec, _provided, persona_ctx)
            if _missing:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "missing_required_inputs",
                        "preset_key": preset_key,
                        "preset_name": _spec.get("name"),
                        "missing": _missing,
                        "input_schema": _spec.get("input_schema") or {},
                        "message": (
                            f"方案「{_spec.get('name')}」缺少必填参数：" +
                            "、".join(m["field"] for m in _missing) +
                            "。请在启动对话框中填写后再试。"
                        ),
                    },
                )

    # ── 双任务防御层 ────────────────────────────────────────────
    # 历史问题: 用户选 friend_growth("好友打招呼")时同时产生
    # facebook_group_member_greet + facebook_campaign_run 两个任务,
    # 任务"完成"语义被串号. 守卫两条:
    #   A) 单步预设硬锁: friend_growth/name_hunter 只能产生约定的单步任务,
    #      即便 body.flow_steps 被外部篡改/旧版预设残留也强行收敛.
    #   B) 同 launch 内 step type+params 完全相同的重复条目去重.
    # 任何被丢弃的 step 写 warning 日志, 便于回查根因.
    _SINGLE_STEP_PRESETS: Dict[str, str] = {
        "friend_growth": "facebook_group_member_greet",
    }
    _expected_single = _SINGLE_STEP_PRESETS.get(preset_key or "")
    if _expected_single:
        _kept = [s for s in flow_steps if s.get("type") == _expected_single]
        if _kept and len(_kept) != len(flow_steps):
            _dropped = [s.get("type") for s in flow_steps
                        if s.get("type") != _expected_single]
            logger.warning(
                "[fb_launch] single-step lock: preset=%s kept=%s dropped=%s",
                preset_key, _expected_single, _dropped,
            )
            flow_steps = _kept
        elif not _kept:
            # 预设结构异常（约定的单步 type 都没有）→ 仍按原序列继续，
            # 让下游报错暴露问题而不是静默吞掉。
            logger.error(
                "[fb_launch] single-step lock: preset=%s 没有期望的 step type=%s, "
                "原始 steps=%s",
                preset_key, _expected_single,
                [s.get("type") for s in flow_steps],
            )

    _seen_step_keys: set = set()
    _deduped_steps: List[dict] = []
    for _st in flow_steps:
        _k = _json.dumps(
            {"t": _st.get("type", ""), "p": _st.get("params") or {}},
            sort_keys=True, ensure_ascii=False,
        )
        if _k in _seen_step_keys:
            logger.warning(
                "[fb_launch] dedupe: skip duplicate step type=%s preset=%s device=%s",
                _st.get("type"), preset_key, device_id,
            )
            continue
        _seen_step_keys.add(_k)
        _deduped_steps.append(_st)
    flow_steps = _deduped_steps

    is_local = _is_local_device(device_id)
    if is_local:
        base = local_api_base()
    else:
        # 通过集群路由找到设备所在 Worker 的实际地址，而非硬编码 _W03_BASE
        base = _W03_BASE  # 兜底默认
        try:
            from .cluster import _get_best_worker_url
            _worker = _get_best_worker_url(device_id)
            if _worker:
                base = f"http://{_worker['ip']}:{_worker.get('port', 8000)}"
        except Exception as _e:
            logger.debug("[fb_launch] 集群路由查询失败 device=%s, 回落 _W03_BASE: %s",
                         device_id, _e)
    worker_caps: Dict[str, Any] = {}
    worker_probe_ok = True
    if not is_local:
        worker_caps, worker_probe_ok = _probe_worker_health_capabilities(base)

    results = []
    for step in flow_steps:
        s_type = step.get("type", "")
        s_params = dict(step.get("params") or {})
        if geo_country and "target_country" not in s_params:
            s_params["target_country"] = geo_country
        if geo_lang and "language" not in s_params:
            s_params["language"] = geo_lang
        if audience_preset and "audience_preset" not in s_params:
            s_params["audience_preset"] = audience_preset
        # 群组类步骤注入目标群名(取首个,或全部传入)
        if target_groups:
            if s_type in ("facebook_group_engage", "facebook_extract_members") \
                    and "group_name" not in s_params:
                s_params["group_name"] = target_groups[0]
            if s_type == "facebook_extract_members" and preset_key in (
                    "friend_growth",):
                s_params.setdefault("broad_keyword", True)
                s_params.setdefault("discover_groups", True)
                s_params.setdefault("max_groups", min(max(len(target_groups), 3), 5))
                s_params.setdefault("max_groups_to_extract", s_params["max_groups"])
                s_params.setdefault("max_members_per_group",
                                    s_params.get("max_members", 20))
                s_params.setdefault("auto_join_groups", True)
                s_params.setdefault("join_if_needed", True)
                s_params.setdefault("skip_visited", True)
            if s_type in ("facebook_campaign_run", "facebook_group_member_greet") \
                    and "target_groups" not in s_params:
                s_params["target_groups"] = target_groups
        if verification_note and s_type in (
                "facebook_add_friend",
                "facebook_campaign_run",
                "facebook_group_member_greet",
        ):
            # 2026-05-03 真机第八轮 bug fix: 与 add_friend_targets / greeting 同
            # 源问题. friend_growth 预设里预填 "verification_note": "" 占位,
            # setdefault 因 key 已存在不写入 → body 传入的话术被吞掉, add_friends
            # step skip empty verification_note. 改为 "if not get" 才覆盖.
            if not (s_params.get("verification_note") or "").strip():
                s_params["verification_note"] = verification_note
            if not (s_params.get("note") or "").strip():
                s_params["note"] = verification_note

        # 2026-04-23: name_hunter 预设 — 注入名字列表 + 覆盖打招呼文案
        # 注意: 不能用 setdefault, 因为 name_hunter 预设里预填了 add_friend_targets=[],
        # 空列表虽然 falsy 但 key 已存在,setdefault 不会覆盖 → body 的名字会被吞掉
        if add_friend_targets and s_type in (
                "facebook_campaign_run",
                "facebook_group_member_greet",
                "facebook_add_friend",
                "facebook_add_friend_and_greet",
                "facebook_send_greeting"):
            if not s_params.get("add_friend_targets"):
                s_params["add_friend_targets"] = add_friend_targets
        if greeting_override and s_type in (
                "facebook_campaign_run",
                "facebook_group_member_greet",
                "facebook_add_friend_and_greet",
                "facebook_send_greeting"):
            if not s_params.get("greeting"):
                s_params["greeting"] = greeting_override

        if s_type in ("facebook_campaign_run", "facebook_group_member_greet"):
            if body.get("max_friends_per_run") is not None:
                s_params["max_friends_per_run"] = int(body.get("max_friends_per_run") or 0)
            if body.get("outreach_goal") is not None:
                s_params["outreach_goal"] = int(body.get("outreach_goal") or 0)
            if body.get("max_members") is not None:
                _max_members = int(body.get("max_members") or 0)
                s_params["max_members"] = _max_members
                s_params["extract_max_members"] = _max_members
                s_params["max_members_per_group"] = _max_members

        # Sprint 3 P0: 自动透传 preset_key,让漏斗能按预设切片
        if preset_key:
            s_params.setdefault("_preset_key", preset_key)

        # P2-UI Sprint：persona_key 兜底注入(preset 里可能漏填的 step 也会带上)
        s_params.setdefault("persona_key", persona_key_in)

        try:
            _task_payload = {
                "type": s_type,
                "device_id": device_id,
                "params": s_params,
            }
            if not is_local:
                # 主控已做过 gate 校验，Worker 无需重复跑 GEO/preflight
                _task_payload["params"] = {**s_params, "_coordinator_forwarded": True}
            r = _post_create_task(base, _task_payload)
            results.append({"type": s_type, "task_id": r.get("task_id", ""), "ok": True})
        except FacebookTaskEnqueueError as e:
            row: Dict[str, Any] = {
                "type": s_type,
                "ok": False,
                "error": str(e),
                "http_status": e.status,
            }
            if e.detail is not None:
                row["detail"] = e.detail
            results.append(row)
        except Exception as e:
            results.append({"type": s_type, "ok": False, "error": str(e)})

    ok_n = sum(1 for r in results if r.get("ok"))
    out: Dict[str, Any] = {
        "ok": ok_n > 0,
        "device_id": device_id,
        "preset_key": preset_key,
        "flow_tasks": results,
        "task_count": ok_n,
        "message": f"已创建 {ok_n}/{len(flow_steps)} 个 Facebook 步骤任务",
        "task_enqueue_base": base,
        "is_local_enqueue": is_local,
        "worker_capabilities_probe_ok": bool(is_local or worker_probe_ok),
    }
    if is_local:
        out["worker_capabilities"] = {"facebook_task_precreate_gate": True, "_source": "local"}
    else:
        out["worker_capabilities"] = dict(worker_caps)
    warn_msg = _remote_worker_capabilities_warning(
        is_local=is_local, worker_caps=worker_caps, probe_ok=worker_probe_ok
    )
    if warn_msg:
        out["worker_capabilities_warning"] = warn_msg
    fail_n = sum(1 for r in results if not r.get("ok"))
    try:
        from src.host.health_monitor import metrics as _fb_launch_metrics

        _fb_launch_metrics.record_fb_device_launch(
            is_local_enqueue=is_local,
            worker_capabilities_probe_ok=bool(out.get("worker_capabilities_probe_ok")),
            had_worker_capabilities_warning=bool(warn_msg),
            steps_ok=ok_n,
            steps_failed=fail_n,
        )
    except Exception:
        logger.debug("record_fb_device_launch skipped", exc_info=True)
    if warn_msg or fail_n > 0:
        logger.warning(
            "[fb_device_launch] device_id=%s steps_ok=%s steps_failed=%s worker_warn=%s probe_ok=%s base=%s",
            device_id,
            ok_n,
            fail_n,
            bool(warn_msg),
            out.get("worker_capabilities_probe_ok"),
            base,
        )
    # ── P1: 话术自动入库 ──
    # 只要有成功的步骤，将用户填写的验证语/打招呼文案注册进 greeting_library，
    # 以便后续跟踪 reply_rate 并形成 A/B 测试闭环。
    if ok_n > 0:
        try:
            from src.host.fb_targets_store import register_greeting_text as _reg_gt
            if verification_note:
                _reg_gt(verification_note, persona_key_in, field="verification_note")
            if greeting_override:
                _reg_gt(greeting_override, persona_key_in, field="greeting")
        except Exception:
            logger.debug("greeting auto-register skipped", exc_info=True)
    return out


# ─────────────────────────────────────────────────────────────────────────
# 设备网格聚合数据(Sprint 2 完善)
# ─────────────────────────────────────────────────────────────────────────

@router.get("/device-grid")
def fb_device_grid():
    """返回 FB 面板设备网格的聚合数据(Sprint 1 骨架,Sprint 2 补全)。

    与 /tiktok/device-grid 输出结构尽量对齐,便于公共组件复用。
    """
    devices_payload = []
    summary = {
        "online": 0,
        "logged_in": 0,
        "with_referral": 0,
        "total_friends_today": 0,
        "total_pending_replies": 0,
    }

    try:
        from src.device_control.device_manager import get_device_manager, DeviceStatus
        mgr = get_device_manager(str(_DEVICES_YAML))
        all_devs = mgr.get_all_devices()

        # 引流配置存量
        referrals = {}
        if _CHAT_MSG_YAML.exists():
            with open(_CHAT_MSG_YAML, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            referrals = (data.get("device_referrals") or {})

        # 读取账号健康/阶段数据
        from src.host.fb_targets_store import get_account_health as _get_ah
        _health_cache = {}
        try:
            for dev in all_devs:
                _health_cache[dev.device_id] = _get_ah(dev.device_id)
        except Exception:
            pass

        for dev in all_devs:
            online = dev.status in (DeviceStatus.CONNECTED, DeviceStatus.BUSY)
            ref = referrals.get(dev.device_id, {})
            has_ref = bool(ref.get("whatsapp") or ref.get("telegram"))
            ah = _health_cache.get(dev.device_id, {})

            if online:
                summary["online"] += 1
            if has_ref:
                summary["with_referral"] += 1

            devices_payload.append({
                "device_id": dev.device_id,
                "alias": getattr(dev, "alias", "") or "",
                "online": online,
                "logged_in": online,  # Sprint 2: 增加 FB 真实登录态检测
                "phase": ah.get("phase", "cold_start"),
                "health_score": ah.get("score", 100),
                "friends_today": 0,    # Sprint 2: 从 facebook_friend_requests 表聚合
                "groups_count": 0,     # Sprint 2: 从 facebook_groups 表聚合
                "pending_replies": 0,  # Sprint 2: 从 inbox 状态聚合
                "referral": {
                    "whatsapp": ref.get("whatsapp", ""),
                    "telegram": ref.get("telegram", ""),
                },
                "risk_status": "green" if ah.get("score", 100) >= 60 else ("yellow" if ah.get("score", 100) >= 40 else "red"),
            })
    except Exception as e:
        logger.warning("[fb_device_grid] 聚合失败: %s", e)

    return {
        "devices": devices_payload,
        "summary": summary,
        "_platform": "facebook",
    }


# ─────────────────────────────────────────────────────────────────────────
# BB5: Dashboard 健康度摘要（引流配置 + 漏斗进度）
# ─────────────────────────────────────────────────────────────────────────

@router.get("/health-summary")
def fb_health_summary():
    """一次性返回 Dashboard 卡片所需的所有 Facebook 健康度数据。

    合并: 设备数/引流配置状态/24h 漏斗进度/主渠道覆盖率
    """
    from src.device_control.device_manager import get_device_manager

    # 1. 设备数
    mgr = get_device_manager(str(_DEVICES_YAML))
    all_devs = [d.device_id for d in mgr.get_all_devices()]
    total_devices = len(all_devs)

    # 2. 引流配置状态
    data = {}
    if _CHAT_MSG_YAML.exists():
        with open(_CHAT_MSG_YAML, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    refs = data.get("device_referrals", {})

    configured = 0
    main_ch_configured = 0
    persona = get_persona_display()
    priority = persona.get("referral_priority", [])
    main_ch = priority[0] if priority else "line"

    for did in all_devs:
        ref = refs.get(did, {})
        has_any = any(v and str(v).strip() for v in ref.values())
        if has_any:
            configured += 1
        main_val = ref.get(main_ch, "")
        if main_val and str(main_val).strip():
            main_ch_configured += 1

    # 健康评分: 0-100
    if total_devices == 0:
        health_score = 0
    else:
        config_rate = configured / total_devices
        main_rate = main_ch_configured / total_devices
        health_score = int(config_rate * 60 + main_rate * 40)

    # 3. 24h 漏斗进度
    funnel_24h = {}
    try:
        from src.host.fb_store import get_funnel_metrics
        m = get_funnel_metrics(since_hours=24)
        funnel_24h = {
            "friend_requests_sent": m.get("stage_friend_request_sent", 0),
            "friends_accepted": m.get("stage_friend_accepted", 0),
            "greetings_sent": m.get("stage_greetings_sent", 0),
            "dm_conversations": m.get("stage_inbox_incoming", 0),
            "wa_referrals": m.get("stage_wa_referrals", 0),
            "rate_accept": m.get("rate_accept", 0),
        }
    except Exception:
        pass

    return {
        "total_devices": total_devices,
        "configured": configured,
        "unconfigured": total_devices - configured,
        "main_channel": main_ch,
        "main_channel_name": {"line": "LINE", "whatsapp": "WhatsApp",
                              "instagram": "Instagram", "telegram": "Telegram"}.get(main_ch, main_ch),
        "main_ch_configured": main_ch_configured,
        "health_score": health_score,
        "persona_label": persona.get("short_label", ""),
        "persona_flag": persona.get("display_flag", ""),
        "funnel_24h": funnel_24h,
        "_platform": "facebook",
    }


# ─────────────────────────────────────────────────────────────────────────
# 漏斗统计(Sprint 2 完整实现)
# ─────────────────────────────────────────────────────────────────────────

def _funnel_steps_from_metrics(m: dict, groups_count: int) -> list:
    """把 fb_store 漏斗 metrics 渲染成前端 steps 数组(可复用)。"""
    return [
        {"key": "groups_joined", "label": "已加入群组",
         "value": groups_count},
        {"key": "members_extracted", "label": "群成员打招呼",
         "value": m["stage_extracted_members"]},
        {"key": "friend_requests_sent", "label": "好友请求发送",
         "value": m["stage_friend_request_sent"]},
        {"key": "friends_accepted", "label": "好友通过",
         "value": m["stage_friend_accepted"],
         "rate": m["rate_accept"]},
        {"key": "dm_conversations", "label": "DM 对话",
         "value": m["stage_inbox_incoming"],
         "rate": m["rate_request_to_inbox"]},
        {"key": "wa_referrals", "label": "WA 引流成功",
         "value": m["stage_wa_referrals"],
         "rate": m["rate_inbox_to_referral"]},
    ]


@router.get("/funnel")
def fb_funnel(device_id: Optional[str] = None,
              since_hours: int = 168,
              preset_key: Optional[str] = None,
              group_by: Optional[str] = None):
    """FB 客服拓展漏斗:进群 → 群成员打招呼 → 加好友 → 通过 → DM → WA 引流。

    Sprint 3 P1: 支持 group_by=preset_key 切片对比 + preset_key 过滤。

    Args:
        device_id: 单设备过滤,空=全量
        since_hours: 时间窗(小时),默认 7 天
        preset_key: 单预设过滤(常规模式)
        group_by: 'preset_key' → 返回每预设一行的对比数组
    """
    try:
        import datetime as _dt
        since_iso = (
            _dt.datetime.utcnow() - _dt.timedelta(hours=max(1, since_hours))
        ).strftime("%Y-%m-%dT%H:%M:%SZ") if since_hours > 0 else None

        from src.host.fb_store import (
            get_funnel_metrics, list_groups,
            get_funnel_metrics_by_preset,
        )

        if group_by == "preset_key":
            slices = get_funnel_metrics_by_preset(device_id=device_id,
                                                  since_iso=since_iso)
            # 每个预设也需要 steps 数组,方便前端直接渲染
            for s in slices:
                # group_by 切片:groups 按 preset_key 过滤(简化为 0,主要看好友/DM)
                s["steps"] = _funnel_steps_from_metrics(s, 0)
            return {
                "_platform": "facebook",
                "_group_by": "preset_key",
                "_scope_device": device_id or "all",
                "_scope_since": since_iso or "all_time",
                "slices": slices,
                "slice_count": len(slices),
            }

        m = get_funnel_metrics(device_id=device_id, since_iso=since_iso,
                               preset_key=preset_key)
        groups = list_groups(device_id=device_id, status="joined", limit=500)
        steps = _funnel_steps_from_metrics(m, len(groups))
        return {
            "steps": steps,
            "rates": {
                "accept": m["rate_accept"],
                "extract_to_request": m["rate_extract_to_request"],
                "request_to_inbox": m["rate_request_to_inbox"],
                "inbox_to_referral": m["rate_inbox_to_referral"],
            },
            # P3-4 2026-04-23: 把 greeting 维度透传到响应根字段,前端 widget 直接读
            "stage_friend_request_sent": m.get("stage_friend_request_sent", 0),
            "stage_friend_accepted": m.get("stage_friend_accepted", 0),
            "stage_greetings_sent": m.get("stage_greetings_sent", 0),
            "stage_greetings_fallback": m.get("stage_greetings_fallback", 0),
            "rate_greet_after_add": m.get("rate_greet_after_add", 0.0),
            "greeting_template_distribution": m.get("greeting_template_distribution", []),
            "_platform": "facebook",
            "_scope_device": m["scope_device"],
            "_scope_since": m["scope_since"],
            "_scope_preset": m.get("scope_preset", "all"),
        }
    except Exception as e:
        logger.exception("fb_funnel failed")
        raise HTTPException(500, f"漏斗数据查询失败: {e}")


@router.get("/dedup-stats")
def fb_dedup_stats(device_id: Optional[str] = None,
                   since_hours: int = 168):
    """Phase D1*: 跨设备去重效果面板数据.

    包含:
      - 被拦截的跨设备重复好友请求数
      - 对话锁冲突次数 (同一 peer 被多设备 claim)
      - canonical_id 覆盖率 (contact_events + friend_requests)
      - 高频重复 peer top 10
      - 每日趋势 (blocked + claims)
    """
    try:
        from src.host.fb_store import get_dedup_stats, get_conv_lock_ttl_distribution
        stats = get_dedup_stats(since_hours=since_hours,
                                device_id=device_id or "")
        # G1: 附加 TTL 分布数据
        stats["ttl_distribution"] = get_conv_lock_ttl_distribution(
            since_hours=since_hours)
        return stats
    except Exception as e:
        logger.exception("fb_dedup_stats failed")
        raise HTTPException(500, f"去重统计查询失败: {e}")


def _do_backfill_one(resolve_fn, stable_acct: str, display_name: str,
                     persona_key: str, dry_run: bool, results: dict):
    """F2 helper: 处理单条回填 (dry_run 只计数, 否则调 resolve_identity)."""
    if dry_run:
        results["processed"] += 1
        if len(results.get("samples", [])) < 20:
            results.setdefault("samples", []).append({
                "stable_acct": stable_acct, "display_name": display_name,
            })
        return
    try:
        cid = resolve_fn(
            platform="facebook", account_id=stable_acct,
            display_name=display_name, persona_key=persona_key,
            discovered_via="backfill_f2",
            auto_merge=True,
        )
        if cid:
            results["new_identities"] = results.get("new_identities", 0) + 1
            if display_name:
                try:
                    resolve_fn(
                        platform="facebook",
                        account_id=f"fb:{display_name.strip()}",
                        display_name=display_name,
                        discovered_via="backfill_f2",
                        auto_merge=True,
                    )
                except Exception:
                    pass
        else:
            results["skipped"] = results.get("skipped", 0) + 1
    except Exception:
        results["errors"] = results.get("errors", 0) + 1
    results["processed"] = results.get("processed", 0) + 1


@router.post("/dedup-backfill")
def fb_dedup_backfill(
    body: Dict[str, Any] = Body(default={}),
):
    """Phase F2: 身份回填工具 — 从 fb_targets_global 的精确标识批量补充 lead_identities.

    Body 参数:
      dry_run: bool (默认 True, 只预览不写入)
      limit: int (默认 200, 单次最大处理数)
      persona_key: str (可选过滤)

    逻辑:
      1. 查 fb_targets_global 中 identity_type IN ('fb_user_id', 'username') 的记录
      2. 对每条: 用 resolve_identity 注册 fb_profile:uid:xxx / fb_profile:user:xxx
      3. 同时注册 display_name 为辅助 identity (fb:<name>)
    """
    dry_run = body.get("dry_run", True)
    batch_limit = min(int(body.get("limit") or 200), 500)
    persona_filter = (body.get("persona_key") or "").strip()

    try:
        from src.host.fb_targets_store import get_conn as _tgt_conn
        from src.host.lead_mesh import resolve_identity
        import sqlite3

        results = {"processed": 0, "new_identities": 0, "skipped": 0,
                   "errors": 0, "dry_run": dry_run, "samples": []}

        with _tgt_conn() as conn:
            conn.row_factory = sqlite3.Row
            sql = (
                "SELECT identity_key, identity_type, display_name, persona_key"
                " FROM fb_targets_global"
                " WHERE identity_type IN ('fb_user_id', 'username')"
                " AND status != 'opt_out'"
            )
            params: list = []
            if persona_filter:
                sql += " AND persona_key=?"
                params.append(persona_filter)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(batch_limit)
            rows = conn.execute(sql, params).fetchall()

        # Source 1: fb_targets_global (精确标识)
        for row in rows:
            ik = row["identity_key"]       # "uid:12345" or "user:john"
            dname = row["display_name"] or ""
            pk = row["persona_key"] or ""
            stable_acct = f"fb_profile:{ik}"
            _do_backfill_one(resolve_identity, stable_acct, dname, pk,
                             dry_run, results)

        # Source 2: facebook_friend_requests 的 target_profile_url (非空)
        from src.host.database import _connect as _fb_conn
        with _fb_conn() as conn2:
            conn2.row_factory = sqlite3.Row
            sql2 = (
                "SELECT target_name, target_profile_url FROM facebook_friend_requests"
                " WHERE target_profile_url != '' AND canonical_id = ''"
                " ORDER BY sent_at DESC LIMIT ?"
            )
            fr_rows = conn2.execute(sql2, [batch_limit]).fetchall()
        for fr in fr_rows:
            url = fr["target_profile_url"]
            tname = fr["target_name"] or ""
            from src.host.fb_targets_store import normalize_identity
            ik2, it2 = normalize_identity(url)
            if it2 == "weak":
                continue  # URL 无法解析为精确标识
            stable_acct2 = f"fb_profile:{ik2}"
            _do_backfill_one(resolve_identity, stable_acct2, tname, "",
                             dry_run, results)

        return results
    except Exception as e:
        logger.exception("fb_dedup_backfill failed")
        raise HTTPException(500, f"回填失败: {e}")


@router.get("/contact-events")
def fb_contact_events(device_id: Optional[str] = None,
                      peer_name: Optional[str] = None,
                      hours: int = 168,
                      event_type: Optional[str] = None,
                      limit: int = 100):
    """P3-3: 统一接触事件流水查询。

    三个使用场景:
      1. 查某人全部接触历史: ?device_id=X&peer_name=Y
      2. 查某类型事件近期总数: ?event_type=greeting_sent&hours=24
      3. 查 greeting 模板 A/B 回复率: 用 /facebook/greeting-reply-rate

    事件 schema 见 src/host/fb_store.py::VALID_CONTACT_EVENT_TYPES。
    """
    try:
        from src.host.fb_store import (list_contact_events_by_peer,
                                        count_contact_events)

        out: Dict[str, Any] = {
            "_platform": "facebook",
            "_scope_device": device_id or "all",
            "_scope_hours": hours,
        }

        if device_id and peer_name:
            # 场景 1: 查单人完整接触流水
            out["events"] = list_contact_events_by_peer(
                device_id=device_id, peer_name=peer_name, limit=limit)
            out["count"] = len(out["events"])
        else:
            # 场景 2: 仅统计计数
            out["count"] = count_contact_events(
                device_id=device_id,
                peer_name=peer_name,
                event_type=event_type,
                hours=hours,
            )
            out["event_type_filter"] = event_type or "all"
        return out
    except Exception as e:
        logger.exception("fb_contact_events failed")
        raise HTTPException(500, f"接触事件查询失败: {e}")


@router.get("/greeting-reply-rate")
def fb_greeting_reply_rate(device_id: Optional[str] = None,
                           hours: int = 168):
    """P3-3: 按 template_id 分组返回 greeting 回复率, 供 A/B 实验决策。

    依赖机器 B 的 Messenger 自动回复回写 ``greeting_replied`` 事件;
    在 B 未上线前此端点返回的 ``replied=0 / reply_rate=0``, 但 ``sent`` 数仍可信。
    """
    try:
        from src.host.fb_store import get_greeting_reply_rate_by_template
        rows = get_greeting_reply_rate_by_template(
            device_id=device_id, hours=hours)
        return {
            "_platform": "facebook",
            "_scope_device": device_id or "all",
            "_scope_hours": hours,
            "templates": rows,
            "total_templates": len(rows),
            "total_sent": sum(r["sent"] for r in rows),
            "total_replied": sum(r["replied"] for r in rows),
        }
    except Exception as e:
        logger.exception("fb_greeting_reply_rate failed")
        raise HTTPException(500, f"回复率查询失败: {e}")


# ─────────────────────────────────────────────────────────────────────────
# 话术库管理（P1 — 列表 + 统计 + 删除）
# ─────────────────────────────────────────────────────────────────────────

@router.get("/greeting-library")
def fb_greeting_library(persona_key: Optional[str] = None,
                        sort: str = "reply_rate",
                        limit: int = 50):
    """列出话术库条目 + 统计概览。

    sort: reply_rate（默认）| sent_count | created_at
    """
    from src.host.fb_targets_store import get_greeting_stats, ensure_schema
    from src.host.database import get_conn
    pk = persona_key or get_default_persona_key()
    ensure_schema()

    # 排序策略: reply_rate 优先但 sent_count>0 的排在 sent=0 前面（避免从未发送的排到顶部）
    sort_col = {
        "reply_rate": "CASE WHEN sent_count>0 THEN 0 ELSE 1 END, reply_rate DESC, sent_count DESC",
        "sent_count": "sent_count DESC, reply_rate DESC",
        "created_at": "created_at DESC",
    }.get(sort, "CASE WHEN sent_count>0 THEN 0 ELSE 1 END, reply_rate DESC, sent_count DESC")
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, persona_key, text_ja, style_tag, topic_id, char_count, "
            f"sent_count, replied_count, reply_rate, created_at "
            f"FROM fb_greeting_library WHERE persona_key=? "
            f"ORDER BY {sort_col} LIMIT ?",
            (pk, limit),
        ).fetchall()
    items = [dict(r) for r in rows]
    stats = get_greeting_stats(pk)
    return {
        "items": items,
        "stats": stats,
        "persona_key": pk,
        "_platform": "facebook",
    }


@router.post("/greeting-library")
def fb_add_greeting(body: dict = Body(default={})):
    """手动添加话术到库中。

    Body: text_ja(必填), persona_key, style_tag, topic_id
    """
    from src.host.database import get_conn
    from src.host.fb_targets_store import ensure_schema
    ensure_schema()
    text = (body.get("text_ja") or "").strip()
    if not text:
        raise HTTPException(400, "话术内容不能为空")
    pk = body.get("persona_key") or get_default_persona_key()
    style_tag = body.get("style_tag") or "manual"
    topic_id = body.get("topic_id") or "dm_greeting"
    with get_conn() as conn:
        try:
            conn.execute(
                """INSERT INTO fb_greeting_library
                   (persona_key, text_ja, reference_layer, style_tag, topic_id, char_count)
                   VALUES (?, ?, 'A', ?, ?, ?)""",
                (pk, text, style_tag, topic_id, len(text)),
            )
            conn.commit()
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, "此话术已存在")
            raise
        row = conn.execute(
            "SELECT id FROM fb_greeting_library WHERE text_ja=? AND persona_key=?",
            (text, pk),
        ).fetchone()
    return {"ok": True, "id": row[0] if row else None, "persona_key": pk}


@router.put("/greeting-library/{greeting_id}")
def fb_update_greeting(greeting_id: int, body: dict = Body(default={})):
    """编辑话术内容或标签。"""
    from src.host.database import get_conn
    from src.host.fb_targets_store import ensure_schema
    ensure_schema()
    updates = []
    params = []
    if "text_ja" in body:
        text = (body["text_ja"] or "").strip()
        if not text:
            raise HTTPException(400, "话术内容不能为空")
        updates.append("text_ja=?")
        params.append(text)
        updates.append("char_count=?")
        params.append(len(text))
    if "style_tag" in body:
        updates.append("style_tag=?")
        params.append(body["style_tag"])
    if "topic_id" in body:
        updates.append("topic_id=?")
        params.append(body["topic_id"])
    if not updates:
        raise HTTPException(400, "无有效更新字段")
    params.append(greeting_id)
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE fb_greeting_library SET {','.join(updates)} WHERE id=?", params
        )
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "话术不存在")
    return {"ok": True, "id": greeting_id}


@router.post("/greeting-library/batch-add")
def fb_batch_add_greetings(body: dict = Body(default={})):
    """批量添加话术（AI 生成后一键入库）。

    Body: items: [{text_ja, style_tag?}], persona_key
    """
    from src.host.database import get_conn
    from src.host.fb_targets_store import ensure_schema
    ensure_schema()
    items = body.get("items") or []
    if not items:
        raise HTTPException(400, "items 不能为空")
    pk = body.get("persona_key") or get_default_persona_key()
    added = 0
    with get_conn() as conn:
        for it in items:
            text = (it.get("text_ja") or it.get("text") or "").strip()
            if not text:
                continue
            style_tag = it.get("style_tag") or "ai_gen"
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO fb_greeting_library
                       (persona_key, text_ja, reference_layer, style_tag, topic_id, char_count)
                       VALUES (?, ?, 'A', ?, 'dm_greeting', ?)""",
                    (pk, text, style_tag, len(text)),
                )
                added += 1
            except Exception:
                pass
        conn.commit()
    return {"ok": True, "added": added, "persona_key": pk}


@router.delete("/greeting-library/{greeting_id}")
def fb_delete_greeting(greeting_id: int):
    """从话术库中移除一条话术。"""
    from src.host.database import get_conn
    from src.host.fb_targets_store import ensure_schema
    ensure_schema()
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM fb_greeting_library WHERE id=?", (greeting_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "话术不存在")
    return {"ok": True, "deleted_id": greeting_id}


@router.post("/health/daily-recover")
def fb_health_daily_recover():
    """每日健康分恢复 — 给所有非 cooldown 设备 +5 分。

    可由 cron/定时任务每天调一次，也可手动触发。
    返回有阶段变更的设备列表。
    """
    from src.host.fb_targets_store import update_account_health, ensure_schema
    from src.host.database import get_conn
    ensure_schema()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT device_id FROM fb_account_health WHERE phase != 'cooldown'"
        ).fetchall()
    device_ids = [r["device_id"] for r in rows]
    phase_changes = []
    for did in device_ids:
        result = update_account_health(did, "daily_recover")
        if result.get("phase_changed"):
            phase_changes.append({
                "device_id": did,
                "old_phase": result.get("old_phase"),
                "new_phase": result.get("phase"),
                "score": result.get("score"),
            })
    return {
        "ok": True,
        "devices_recovered": len(device_ids),
        "phase_changes": phase_changes,
    }


@router.get("/qualified-leads")
def fb_qualified_leads(limit: int = 20, min_score: int = 60):
    """高分线索列表(Sprint 3 P1 — 支持 min_score 参数,集成 leadList 公共组件)。

    返回字段:name / score / tier(S/A/B/C/D) / tags / reasons
    """
    from src.ai.fb_lead_scorer import _tier_for_score
    leads = []
    try:
        from src.leads.store import get_leads_store
        store = get_leads_store()
        if hasattr(store, "search_leads"):
            raw = store.search_leads(source_platform="facebook",
                                     min_score=min_score, limit=limit)
            for l in (raw or []):
                score = int(l.get("score", 0) or 0)
                # 解析 notes 中的 reasons(scorer 写入的格式 "r1;r2;r3")
                notes = l.get("notes") or ""
                reasons = [r.strip() for r in notes.split(";") if r.strip()]
                leads.append({
                    "name": l.get("name"),
                    "score": score,
                    "tier": _tier_for_score(score),
                    "tags": l.get("tags", []),
                    "reasons": reasons[:5],
                    "lead_id": l.get("lead_id") or l.get("id"),
                })
        leads.sort(key=lambda x: -x.get("score", 0))
    except Exception as e:
        logger.debug("[fb_qualified_leads] %s", e)
    return {"leads": leads, "count": len(leads),
            "min_score": min_score,
            "_status": "Sprint 3 P1 leadList"}


# ─────────────────────────────────────────────────────────────────────────
# 风控自愈相关接口(Sprint 2 P0)
# ─────────────────────────────────────────────────────────────────────────

@router.get("/risk/status")
def fb_risk_status():
    """全局风控状态:每台设备的当前 cooldown / 历史风控次数 / 最近事件。"""
    try:
        from src.host.fb_risk_listener import get_healer
        healer = get_healer()
        all_hist = healer.get_all_histories()
        result = []
        for did, hist in all_hist.items():
            cd = healer.get_cooldown_status(did) or 0
            result.append({
                "device_id": did,
                "risk_count": len(hist),
                "cooldown_remaining": int(cd),
                "last_event": hist[-1] if hist else None,
            })
        return {"devices": result, "config": healer._cfg}
    except Exception as e:
        return {"devices": [], "error": str(e)}


@router.get("/risk/history/{device_id}")
def fb_risk_history(device_id: str):
    """单台设备的完整风控自愈历史。"""
    try:
        from src.host.fb_risk_listener import get_healer
        return {"device_id": device_id,
                "history": get_healer().get_history(device_id)}
    except Exception as e:
        return {"device_id": device_id, "history": [], "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────
# P2.1 账号画像聚合（替代前端并发 4 API）+ P2.4 智能建议
# ─────────────────────────────────────────────────────────────────────────

@router.get("/devices/{device_id}/account-profile")
def fb_account_profile(device_id: str):
    """聚合账号画像 — 单次请求替代前端并发 4 API + 计算智能建议。

    返回结构（前端 _pgRenderProfile 直接消费）::

        {
          "device_id": "...",
          "account": {phase, friends_count, groups_count, friend_requests_sent_7d},
          "risk":    {level: 'low|medium|high', count_24h, last_blocked_at, cooldown_remaining},
          "recent_tasks": [{type, status, updated_at, error}, ...]  # 最多 3 条
          "suggestion":   {tone: 'ok|info|warning', text}           # 智能建议（基于 phase × 风控）
        }

    所有内部数据源单独 try/except — 任一数据源失败不影响其他字段。
    """
    import datetime as _dt
    out: Dict[str, Any] = {
        "device_id": device_id,
        "account": {"phase": "unknown", "friends_count": 0,
                    "groups_count": 0, "friend_requests_sent_7d": 0},
        "risk": {"level": "low", "count_24h": 0,
                 "last_blocked_at": None, "cooldown_remaining": 0},
        "recent_tasks": [],
        "suggestion": {"tone": "info", "text": "✓ 账号状态正常"},
    }

    # ---- 1. phase（来自 fb_account_phase） ----
    try:
        from src.host.fb_account_phase import get_phase as _get_phase
        ph = _get_phase(device_id) or {}
        if ph.get("phase"):
            out["account"]["phase"] = ph["phase"]
    except Exception as _e:
        logger.debug("[account-profile] phase fetch failed: %s", _e)

    # ---- 2. 风控历史 + cooldown ----
    try:
        from src.host.fb_risk_listener import get_healer
        healer = get_healer()
        hist = healer.get_history(device_id) or []
        cutoff_iso = (_dt.datetime.utcnow() - _dt.timedelta(hours=24)).isoformat() + "Z"
        recent_risk = [e for e in hist
                       if (e.get("detected_at") or e.get("at") or "") >= cutoff_iso]
        out["risk"]["count_24h"] = len(recent_risk)
        # 上次限流（最近一条 rate_limit/checkpoint/banned/policy/block）
        for e in reversed(hist):
            kind = str(e.get("kind") or e.get("type") or "").lower()
            if any(k in kind for k in ("rate_limit", "checkpoint", "banned", "policy", "block")):
                out["risk"]["last_blocked_at"] = e.get("detected_at") or e.get("at")
                break
        out["risk"]["cooldown_remaining"] = int(healer.get_cooldown_status(device_id) or 0)
    except Exception as _e:
        logger.debug("[account-profile] risk fetch failed: %s", _e)

    # ---- 3. Funnel 数据（友数 / 群数 / 7天请求数）----
    try:
        from src.host.fb_store import get_funnel_metrics, list_groups
        since_iso = (_dt.datetime.utcnow() - _dt.timedelta(hours=168)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        m = get_funnel_metrics(device_id=device_id, since_iso=since_iso) or {}
        out["account"]["friends_count"] = int(m.get("stage_friend_accepted", 0) or 0)
        out["account"]["friend_requests_sent_7d"] = int(m.get("stage_friend_request_sent", 0) or 0)
        groups = list_groups(device_id=device_id, status="joined", limit=500) or []
        out["account"]["groups_count"] = len(groups)
    except Exception as _e:
        logger.debug("[account-profile] funnel fetch failed: %s", _e)

    # ---- 4. 最近 3 条任务（修复 P1.1 contact-events 误用 bug）----
    try:
        from src.host.task_store import list_tasks
        tasks = list_tasks(device_id=device_id, limit=3) or []
        out["recent_tasks"] = [
            {
                "type": t.get("type"),
                "status": t.get("status"),
                "updated_at": t.get("updated_at"),
                "error": (t.get("result") or {}).get("error")
                         if isinstance(t.get("result"), dict) else None,
            }
            for t in tasks
        ]
    except Exception as _e:
        logger.debug("[account-profile] tasks fetch failed: %s", _e)

    # ---- 4.5 今日任务统计（P2.5: today_failed 进规则引擎）----
    today_tasks_total = 0
    today_failed_total = 0
    try:
        import datetime as _dt2
        from src.host.task_store import list_tasks as _list_tasks
        today_start = _dt2.datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        for _t in (_list_tasks(device_id=device_id, limit=200) or []):
            ua = _t.get("updated_at") or ""
            if ua >= today_start:
                today_tasks_total += 1
                if _t.get("status") == "failed":
                    today_failed_total += 1
    except Exception as _e:
        logger.debug("[account-profile] today task counts failed: %s", _e)

    today_fail_rate = (today_failed_total / today_tasks_total) if today_tasks_total else 0.0
    out["today_stats"] = {
        "tasks": today_tasks_total,
        "failed": today_failed_total,
        "fail_rate": round(today_fail_rate, 2),
    }

    # ---- 5. 风控等级 + 智能建议 + CTA action（P2.5+P2.6 升级）----
    # 优先级（高→低）: cooldown > 今日失败率 > 24h 风控 > phase
    phase = out["account"]["phase"]
    risk_24h = out["risk"]["count_24h"]
    cd = out["risk"]["cooldown_remaining"]

    _open_fail = {"label": "查看失败原因", "type": "open_failure_modal"}

    if cd > 0:
        out["risk"]["level"] = "high"
        out["suggestion"] = {
            "tone": "warning",
            "text": f"⚠ 当前正在 cooldown（{cd}s 后解禁），请暂停所有外联动作",
            "action": None,
        }
    elif today_fail_rate >= 0.5 and today_tasks_total >= 3:
        # P2.5 关键修复：高失败率 override phase 推荐
        out["risk"]["level"] = "high"
        out["suggestion"] = {
            "tone": "warning",
            "text": f"🚨 今日失败率 {int(today_fail_rate*100)}% ({today_failed_total}/{today_tasks_total})，先停止外联 → 排查原因",
            "action": _open_fail,
        }
    elif today_fail_rate >= 0.3 and today_tasks_total >= 5:
        out["risk"]["level"] = "medium"
        out["suggestion"] = {
            "tone": "warning",
            "text": f"⚠ 今日失败率偏高 {int(today_fail_rate*100)}% ({today_failed_total}/{today_tasks_total})，建议降频 + 检查 selector",
            "action": _open_fail,
        }
    elif phase == "cooldown" or risk_24h >= 5:
        out["risk"]["level"] = "high"
        out["suggestion"] = {
            "tone": "warning",
            "text": "🔴 24h 风控信号 ≥5 次，建议立即停止外联 + 检查 selector 健康度",
            "action": _open_fail,
        }
    elif risk_24h >= 2:
        out["risk"]["level"] = "medium"
        out["suggestion"] = {
            "tone": "warning",
            "text": "🟡 风控信号偏多，建议降频运行 + 避免连续相同动作",
            "action": _open_fail,
        }
    elif phase == "cold_start":
        out["risk"]["level"] = "low"
        out["suggestion"] = {
            "tone": "info",
            "text": "🌱 冷启动期，建议先做 5-7 天内容浏览养号，再展开外联",
            "action": {"label": "立即开始养号", "type": "run_task",
                       "task_type": "facebook_browse_feed_by_interest"},
        }
    elif phase == "growth":
        out["risk"]["level"] = "low"
        out["suggestion"] = {
            "tone": "info",
            "text": "📈 健康成长期，可下发：加好友（中频）/ 群成员打招呼 / 群内互动",
            "action": {"label": "发起加好友", "type": "run_task",
                       "task_type": "facebook_add_friend"},
        }
    elif phase == "mature":
        out["risk"]["level"] = "low"
        out["suggestion"] = {
            "tone": "ok",
            "text": "🌳 账号成熟，可承担营销动作（私信、群发）— 保持节奏",
            "action": {"label": "全链路客服拓展", "type": "run_task",
                       "task_type": "facebook_campaign_run"},
        }
    else:
        out["risk"]["level"] = "low"
        out["suggestion"] = {
            "tone": "info",
            "text": "✓ 账号状态正常，可按 phase 推荐执行任务",
            "action": None,
        }

    return out


@router.get("/devices/{device_id}/quota-status")
def fb_quota_status(device_id: str):
    """单台设备的 facebook 各 action quota 余量 + 下次可派 ETA.

    2026-04-27 P4: 社群客服拓展真机重试时 quota 满任务 fail 但 dashboard 看不见
    quota 状态. 本 endpoint 给 dashboard chip / preflight UI 显示用.

    返回 schema:
      {
        "device_id": "...",
        "actions": {
          "join_group": {
            "hourly_used": 3, "hourly_limit": 3,
            "daily_used": 5, "daily_limit": 50,
            "hourly_remaining": 0, "daily_remaining": 45,
            "next_slot_eta_seconds": 1234,
            "next_slot_eta_minutes": 21,
            "available_now": false
          },
          ...
        }
      }

    注: join_group 执行链路已按 device_id 作为 quota account 记录，避免一台
    设备的重试耗尽其他设备额度；旧动作仍沿用默认空账号。
    """
    try:
        from src.behavior.compliance_guard import get_compliance_guard
        guard = get_compliance_guard()
        plimits = guard._limits.get("facebook")
        if not plimits:
            return {"device_id": device_id, "actions": {},
                    "error": "facebook platform 限制未配置"}
        actions: dict = {}
        for action_name in plimits.actions:
            account = device_id if action_name == "join_group" else ""
            remaining = guard.get_remaining("facebook", action_name, account)
            eta_sec = guard.get_next_slot_eta("facebook", action_name, account)
            eta_min = max(1, int((eta_sec + 30) // 60)) if eta_sec > 0 else 0
            actions[action_name] = {
                "quota_account": account,
                "hourly_used": remaining["hourly_used"],
                "hourly_limit": plimits.actions[action_name].hourly,
                "hourly_remaining": remaining["hourly_remaining"],
                "daily_used": remaining["daily_used"],
                "daily_limit": plimits.actions[action_name].daily,
                "daily_remaining": remaining["daily_remaining"],
                "next_slot_eta_seconds": int(eta_sec),
                "next_slot_eta_minutes": eta_min,
                "available_now": eta_sec <= 0,
            }
        return {"device_id": device_id, "actions": actions}
    except Exception as e:
        return {"device_id": device_id, "actions": {}, "error": str(e)}


@router.post("/risk/reload")
def fb_risk_reload():
    """重新加载 config/facebook_risk.yaml(无需重启服务)。"""
    try:
        from src.host.fb_risk_listener import get_healer
        get_healer().reload_config()
        return {"ok": True, "config": get_healer()._cfg}
    except Exception as e:
        raise HTTPException(500, f"重载失败: {e}")


@router.get("/selectors/health")
def fb_selectors_health():
    """Facebook + Messenger 学习库 selector 健康度(Sprint 3 P0)。

    返回:
      packages: [{name, total_selectors, healthy, stale, stale_details}]
      facebook_total / messenger_total / overall_health_pct
    """
    try:
        from src.vision.auto_selector import SelectorStore
        store = SelectorStore()
        packages = ["com.facebook.katana", "com.facebook.orca"]
        out = []
        total, healthy = 0, 0
        for pkg in packages:
            entries = store.load(pkg)
            pkg_total = len(entries)
            pkg_healthy = sum(1 for e in entries.values()
                              if not (e.confidence < 0.4 and e.misses >= 3))
            pkg_stale = pkg_total - pkg_healthy
            stale_detail = [
                {"target": t, "confidence": round(e.confidence, 2),
                 "hits": e.hits, "misses": e.misses}
                for t, e in entries.items()
                if e.confidence < 0.4 and e.misses >= 3
            ]
            out.append({
                "package": pkg,
                "label": "Facebook" if pkg.endswith("katana") else "Messenger",
                "total_selectors": pkg_total,
                "healthy": pkg_healthy,
                "stale": pkg_stale,
                "stale_details": stale_detail,
                "all_targets": [
                    {"target": t, "confidence": round(e.confidence, 2),
                     "hits": e.hits, "misses": e.misses,
                     "alts_count": len(e.alts)}
                    for t, e in entries.items()
                ],
            })
            total += pkg_total
            healthy += pkg_healthy
        return {
            "packages": out,
            "overall_total": total,
            "overall_healthy": healthy,
            "overall_health_pct": round(healthy / total * 100, 1) if total else 100.0,
        }
    except Exception as e:
        logger.exception("fb_selectors_health failed")
        raise HTTPException(500, str(e))


@router.post("/daily-brief/generate")
def fb_daily_brief_generate(device_id: Optional[str] = None,
                            hours: int = 24,
                            persist: bool = True):
    """生成 1 份新日报。"""
    try:
        from src.ai.fb_daily_brief import generate_brief
        brief = generate_brief(device_id=device_id, hours=hours, persist=persist)
        return brief
    except Exception as e:
        logger.exception("fb_daily_brief_generate failed")
        raise HTTPException(500, f"生成失败: {e}")


@router.get("/daily-brief/latest")
def fb_daily_brief_latest(device_id: Optional[str] = None, limit: int = 1):
    """取最近 N 份日报(默认 1)。"""
    from src.ai.fb_daily_brief import get_latest_brief
    return {"briefs": get_latest_brief(device_id=device_id, limit=limit)}


@router.post("/risk/clear/{device_id}")
def fb_risk_clear(device_id: str):
    """清除指定设备的 risk_status / cooldown(人工恢复设备)。"""
    try:
        from src.host.fb_risk_listener import get_healer
        from src.host.device_state import DeviceStateStore
        healer = get_healer()
        with healer._lock:
            healer._cooldown_until.pop(device_id, None)
        try:
            ds = DeviceStateStore(platform="facebook")
            ds.set(device_id, "risk_status", "green")
        except Exception:
            pass
        return {"ok": True, "device_id": device_id}
    except Exception as e:
        raise HTTPException(500, f"清除失败: {e}")


# ─────────────────────────────────────────────────────────────────────
# P1-1 / P1-2 / P1-3: 养号运营面板 + playbook 热加载 + phase 管理
# ─────────────────────────────────────────────────────────────────────


@router.get("/playbook")
def fb_get_playbook():
    """当前生效的 facebook_playbook.yaml（含 mtime 供前端展示）。"""
    try:
        from src.host.fb_playbook import load_playbook, playbook_mtime
        import datetime as _dt
        data = load_playbook()
        mt = playbook_mtime()
        return {
            "ok": True,
            "data": data,
            "mtime": mt,
            "mtime_iso": (_dt.datetime.fromtimestamp(mt).strftime("%Y-%m-%dT%H:%M:%S")
                          if mt else ""),
        }
    except Exception as e:
        raise HTTPException(500, f"load playbook failed: {e}")


@router.post("/playbook/reload")
def fb_reload_playbook():
    """强制重读 playbook（一般不必调；mtime 自动热加载）。"""
    try:
        from src.host.fb_playbook import reload_playbook
        data = reload_playbook()
        return {"ok": True, "reloaded": True,
                "phases": list((data.get("phases") or {}).keys())}
    except Exception as e:
        raise HTTPException(500, f"reload failed: {e}")


@router.get("/phase/{device_id}")
def fb_get_device_phase(device_id: str):
    """查单个设备当前 phase / 累计刷量 / 最近一次风控 / 距迁移差距。"""
    try:
        from src.host.fb_account_phase import get_phase, evaluate_transition
        from src.host.fb_store import count_risk_events_recent
        p = get_phase(device_id) or {}
        p["risk_count_24h"] = count_risk_events_recent(device_id, hours=24)
        # 顺手跑一次评估（不强制迁移，只是为了给前端展示最新 phase）
        snap = evaluate_transition(device_id) or {}
        p["last_evaluation"] = snap
        return {"ok": True, "device_id": device_id, "phase": p}
    except Exception as e:
        raise HTTPException(500, f"get phase failed: {e}")


@router.get("/phase")
def fb_list_phases(phase: Optional[str] = None):
    """面板: 所有设备当前 phase 清单。"""
    try:
        from src.host.fb_account_phase import list_phases
        rows = list_phases(phase=phase)
        return {"ok": True, "total": len(rows), "rows": rows}
    except Exception as e:
        raise HTTPException(500, f"list phases failed: {e}")


@router.get("/campaign-runs")
def fb_list_campaign_runs(device_id: Optional[str] = None,
                          state: Optional[str] = None,
                          limit: int = 50):
    """campaign_run 运行史（支持断点续跑 UI 的数据源）。"""
    try:
        from src.host.fb_campaign_store import list_runs
        rows = list_runs(device_id=device_id, state=state, limit=limit)
        return {"ok": True, "total": len(rows), "rows": rows}
    except Exception as e:
        raise HTTPException(500, f"list campaign runs failed: {e}")


@router.get("/dashboard/ops")
def fb_dashboard_ops(hours: int = 24):
    """养号运营面板 —— 一次返回市场/运营需要的核心指标（P1-3b 最小版）。

    返回字段设计得贴齐市场经理关心的口径：
      * phases.counts     — 各档位账号分布
      * risks.*           — 风控触发频次 / 影响设备
      * campaign_runs.*   — 过去 N 小时的运行/成功率/断点
      * funnel            — 好友请求/入群/回复的漏斗聚合（复用 fb_store.get_funnel_metrics）
      * top_devices_risk  — 最近风控最多的 5 台设备（重点盯防）
    """
    try:
        import datetime as _dt
        from src.host.fb_account_phase import list_phases
        from src.host.fb_store import (list_recent_risk_events,
                                        get_funnel_metrics)
        from src.host.fb_campaign_store import list_runs

        since_iso = (_dt.datetime.utcnow()
                     - _dt.timedelta(hours=int(hours))
                     ).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1. phases 分档
        all_phases = list_phases()
        phase_counts = {"cold_start": 0, "growth": 0, "mature": 0, "cooldown": 0}
        for row in all_phases:
            k = row.get("phase") or "cold_start"
            phase_counts[k] = phase_counts.get(k, 0) + 1

        # 2. 风控事件
        risks = list_recent_risk_events(hours=hours, limit=500)
        risk_by_device: dict[str, int] = {}
        risk_by_kind: dict[str, int] = {}
        for r in risks:
            did = r.get("device_id") or ""
            kd = r.get("kind") or "other"
            if did:
                risk_by_device[did] = risk_by_device.get(did, 0) + 1
            risk_by_kind[kd] = risk_by_kind.get(kd, 0) + 1
        top_devices_risk = sorted(
            [{"device_id": k, "count": v} for k, v in risk_by_device.items()],
            key=lambda x: -x["count"])[:5]

        # 3. campaign_runs
        runs = list_runs(limit=500)
        runs_recent = [r for r in runs if (r.get("started_at") or "") >= since_iso]
        cstate: dict[str, int] = {}
        for r in runs_recent:
            s = r.get("state") or "unknown"
            cstate[s] = cstate.get(s, 0) + 1
        total_recent = len(runs_recent) or 1
        completed_recent = cstate.get("completed", 0)

        # 4. 漏斗聚合
        funnel = get_funnel_metrics(since_iso=since_iso)

        return {
            "ok": True,
            "window_hours": int(hours),
            "generated_at": _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "phases": {
                "counts": phase_counts,
                "total_devices": len(all_phases),
            },
            "risks": {
                "events_total": len(risks),
                "affected_devices": len(risk_by_device),
                "by_kind": risk_by_kind,
            },
            "campaign_runs": {
                "total_recent": len(runs_recent),
                "by_state": cstate,
                "success_rate": round(completed_recent / total_recent, 3),
            },
            "funnel": funnel,
            "top_devices_risk": top_devices_risk,
        }
    except Exception as e:
        logger.exception("[dashboard/ops]")
        raise HTTPException(500, f"dashboard ops failed: {e}")


@router.post("/risk/inject")
def fb_risk_inject(body: dict):
    """测试用:手工注入一次 risk_detected 事件,验证跨平台联动降级。

    Body:
      { "device_id": "...", "message": "simulated", "platform": "facebook" }
    """
    did = (body.get("device_id") or "").strip()
    if not did:
        raise HTTPException(400, "device_id required")
    msg = body.get("message", "manual inject test")
    platform = body.get("platform", "facebook")
    try:
        from src.host.event_stream import push_event
        push_event(f"{platform}.risk_detected", {
            "device_id": did,
            "message": msg,
            "manual_inject": True,
        }, did)
        return {"ok": True, "pushed": f"{platform}.risk_detected",
                "device_id": did}
    except Exception as e:
        raise HTTPException(500, f"注入失败: {e}")


# ─────────────────────────────────────────────────────────────────────
# P2-4 Sprint A: 目标画像 / VLM 识别 API
# ─────────────────────────────────────────────────────────────────────

@router.get("/target-personas")
def fb_list_target_personas(only_active: bool = True):
    """返回所有启用中的目标画像（UI 下拉使用）。"""
    from src.host import fb_target_personas
    try:
        items = fb_target_personas.list_personas(only_active=only_active)
        cfg = fb_target_personas.load_config()
        return {
            "ok": True,
            "personas": items,
            "default_persona": cfg.get("default_persona"),
            "quotas": cfg.get("quotas"),
            "vlm": {k: v for k, v in (cfg.get("vlm") or {}).items() if k != "endpoint"},
            "mtime": fb_target_personas.config_mtime(),
        }
    except Exception as e:
        raise HTTPException(500, f"load personas failed: {e}")


@router.post("/target-personas/reload")
def fb_reload_target_personas():
    """热加载 config/fb_target_personas.yaml。"""
    from src.host import fb_target_personas
    try:
        cfg = fb_target_personas.reload_config()
        return {"ok": True, "default_persona": cfg.get("default_persona"),
                "personas": list((cfg.get("personas") or {}).keys()),
                "mtime": fb_target_personas.config_mtime()}
    except Exception as e:
        raise HTTPException(500, f"reload failed: {e}")


@router.get("/vlm/health")
def fb_vlm_health():
    """Ollama 在线 + 目标模型可用性探测。"""
    from src.host import ollama_vlm
    return ollama_vlm.check_health()


@router.get("/vlm/level4/status")
def fb_vlm_level4_status():
    """VLM Level 4 UI fallback 运行时状态 (P4 运维: B_OPERATIONS_GUIDE §12.5
    的 REPL 查询脚本的 HTTP 版本). 不同于 `/vlm/health` (Ollama 分类服务),
    本 endpoint 暴露 `_enter_messenger_search` / `_tap_first_search_result` /
    `_tap_messenger_send` 共用的 `VisionFallback` instance + P5b provider
    swap 状态 + 最后一次 HTTP error。

    字段:
      * provider: "gemini" | "ollama" | null (null=无 VLM provider)
      * vision_model: e.g. "gemini-2.5-flash" | "llava:7b"
      * swapped: P5b 是否已从 Gemini runtime 切到 Ollama (true 单向不回)
      * consecutive_failures: 当前连续 HTTP 失败 count (阈值 3 触发 swap)
      * swap_events_total: 累计 swap 触发次数 (P16; Prometheus 同源 counter)
      * latency: {count, sum_sec, avg_sec} — P18 累计 find_element latency
        (histogram 按 bucket 导出到 Prometheus, 这里 JSON 只给 count/sum/avg)
      * last_error_code: int | null (Gemini 503/429 等; null = 上次成功)
      * last_error_body: str (截 120 chars; "timeout" 字面值表 httpx timeout)
      * budget: {hourly_used, hourly_budget, budget_remaining, cache_size}
      * init_attempted: lazy init 是否跑过 (false = 还没 VLM call 需求)

    对 ops 用: 早期 429 集群 → 看 consecutive_failures 接近 3; swap 已触发 →
    看 swapped=true provider=ollama。
    """
    from src.app_automation import facebook as fb
    out = {
        "provider": None, "vision_model": None, "swapped": False,
        "consecutive_failures": 0, "swap_events_total": 0,
        "last_error_code": None, "last_error_body": "",
        "budget": {}, "init_attempted": False,
        "latency": {"count": 0, "sum_sec": 0.0, "avg_sec": 0.0},
    }
    out["swapped"] = bool(getattr(fb, "_vlm_provider_swapped", False))
    out["consecutive_failures"] = int(
        getattr(fb, "_vlm_consecutive_failures", 0))
    out["swap_events_total"] = int(
        getattr(fb, "_vlm_swap_events_total", 0))
    out["init_attempted"] = bool(
        getattr(fb, "_vision_fallback_init_attempted", False))
    lat_count = int(getattr(fb, "_vlm_latency_count", 0))
    lat_sum = float(getattr(fb, "_vlm_latency_sum", 0.0))
    out["latency"] = {
        "count": lat_count, "sum_sec": round(lat_sum, 3),
        "avg_sec": round(lat_sum / lat_count, 3) if lat_count > 0 else 0.0,
    }
    vf = getattr(fb, "_vision_fallback_instance", None)
    if vf is None:
        return out
    try:
        out["budget"] = vf.stats()
    except Exception:
        pass
    client = getattr(vf, "_client", None)
    if client is None:
        return out
    cfg = getattr(client, "config", None)
    if cfg is not None:
        out["provider"] = getattr(cfg, "provider", None)
        out["vision_model"] = getattr(cfg, "vision_model", None)
    out["last_error_code"] = getattr(client, "last_error_code", None)
    body = getattr(client, "last_error_body", "") or ""
    out["last_error_body"] = body[:120]
    return out


@router.post("/classify/single")
def fb_classify_single(body: dict = Body(default={})):
    """单样本分类接口（给前端/脚本调用）。

    Body::
        {
          "device_id": "...",      # required，做配额 & 审计
          "task_id": "",
          "persona_key": "jp_female_midlife",  # 可选，空则默认
          "target_key": "https://facebook.com/xxx",  # required，唯一标识用于去重
          "display_name": "山田花子",
          "bio": "...", "username": "...", "locale": "ja-JP",
          "image_paths": ["/abs/path/1.jpg", ...],
          "do_l2": true,
          "dry_run": false
        }
    """
    did = (body.get("device_id") or "").strip()
    target = (body.get("target_key") or "").strip()
    if not did or not target:
        raise HTTPException(400, "device_id 和 target_key 必填")
    try:
        from src.host.fb_profile_classifier import classify
        result = classify(
            device_id=did,
            task_id=body.get("task_id", ""),
            persona_key=body.get("persona_key"),
            target_key=target,
            display_name=body.get("display_name", ""),
            bio=body.get("bio", ""),
            username=body.get("username", ""),
            locale=body.get("locale", ""),
            image_paths=body.get("image_paths") or [],
            l2_image_paths=body.get("l2_image_paths") or body.get("image_paths") or [],
            do_l2=bool(body.get("do_l2", True)),
            dry_run=bool(body.get("dry_run", False)),
        )
        return {"ok": True, **result}
    except Exception as e:
        logger.exception("classify_single failed")
        raise HTTPException(500, f"classify failed: {e}")


@router.get("/insights")
def fb_list_insights(device_id: Optional[str] = None,
                     persona_key: Optional[str] = None,
                     stage: Optional[str] = None,
                     match: Optional[int] = None,
                     hours: int = 24,
                     limit: int = 100):
    """查询 fb_profile_insights。用于前端"今天命中了谁"看板。"""
    from src.host.database import get_conn
    clauses = ["classified_at >= datetime('now', ?)"]
    params: list = [f"-{max(1, int(hours))} hours"]
    if device_id:
        clauses.append("device_id = ?"); params.append(device_id)
    if persona_key:
        clauses.append("persona_key = ?"); params.append(persona_key)
    if stage:
        clauses.append("stage = ?"); params.append(stage)
    if match is not None:
        clauses.append("match = ?"); params.append(int(match))
    sql = ("SELECT id, device_id, task_id, persona_key, target_key, display_name, "
           "stage, match, score, confidence, insights_json, vlm_model, latency_ms, "
           "classified_at FROM fb_profile_insights "
           f"WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?")
    params.append(max(1, min(int(limit), 500)))
    out = []
    try:
        with get_conn() as conn:
            for row in conn.execute(sql, params).fetchall():
                out.append({
                    "id": row[0], "device_id": row[1], "task_id": row[2],
                    "persona_key": row[3], "target_key": row[4],
                    "display_name": row[5], "stage": row[6],
                    "match": bool(row[7]), "score": float(row[8] or 0),
                    "confidence": float(row[9] or 0),
                    "insights": _json.loads(row[10] or "{}"),
                    "vlm_model": row[11] or "",
                    "latency_ms": int(row[12] or 0),
                    "classified_at": row[13],
                })
        return {"ok": True, "items": out, "total": len(out)}
    except Exception as e:
        raise HTTPException(500, f"query failed: {e}")


@router.get("/insights/stats")
def fb_insights_stats(hours: int = 24):
    """画像识别指标汇总（Dashboard 用）。"""
    from src.host.database import get_conn
    with get_conn() as conn:
        def _q(sql, params):
            return conn.execute(sql, params).fetchone()

        since = f"-{max(1, int(hours))} hours"
        l1_total = _q("SELECT COUNT(*) FROM fb_profile_insights "
                      "WHERE stage='L1' AND classified_at >= datetime('now', ?)", (since,))[0]
        l2_total = _q("SELECT COUNT(*) FROM fb_profile_insights "
                      "WHERE stage='L2' AND classified_at >= datetime('now', ?)", (since,))[0]
        match_count = _q("SELECT COUNT(*) FROM fb_profile_insights "
                         "WHERE match=1 AND classified_at >= datetime('now', ?)", (since,))[0]
        avg_l2_ms = _q("SELECT AVG(latency_ms) FROM fb_profile_insights "
                       "WHERE stage='L2' AND classified_at >= datetime('now', ?)", (since,))[0] or 0

        persona_rows = conn.execute(
            """SELECT persona_key,
                      SUM(CASE WHEN stage='L1' THEN 1 ELSE 0 END) AS l1_n,
                      SUM(CASE WHEN stage='L2' THEN 1 ELSE 0 END) AS l2_n,
                      SUM(CASE WHEN match=1 THEN 1 ELSE 0 END) AS match_n
               FROM fb_profile_insights
               WHERE classified_at >= datetime('now', ?)
               GROUP BY persona_key""", (since,)).fetchall()

        device_rows = conn.execute(
            """SELECT device_id,
                      SUM(CASE WHEN stage='L1' THEN 1 ELSE 0 END) AS l1_n,
                      SUM(CASE WHEN stage='L2' THEN 1 ELSE 0 END) AS l2_n,
                      SUM(CASE WHEN match=1 THEN 1 ELSE 0 END) AS match_n
               FROM fb_profile_insights
               WHERE classified_at >= datetime('now', ?)
               GROUP BY device_id ORDER BY l2_n DESC LIMIT 20""", (since,)).fetchall()

        # Sprint C-1: 新列 queue_wait_ms / device_id 可能在老库上缺失，加 try/except fallback
        try:
            cost_rows = conn.execute(
                """SELECT provider, model, scene, COUNT(*) AS n,
                          AVG(latency_ms) AS avg_ms, SUM(cost_usd) AS usd,
                          AVG(COALESCE(queue_wait_ms,0)) AS avg_wait,
                          MAX(COALESCE(queue_wait_ms,0)) AS peak_wait
                   FROM ai_cost_events
                   WHERE at >= datetime('now', ?) GROUP BY provider, model, scene""",
                (since,)).fetchall()
            has_wait_col = True
        except Exception:
            cost_rows = conn.execute(
                """SELECT provider, model, scene, COUNT(*) AS n,
                          AVG(latency_ms) AS avg_ms, SUM(cost_usd) AS usd
                   FROM ai_cost_events
                   WHERE at >= datetime('now', ?) GROUP BY provider, model, scene""",
                (since,)).fetchall()
            has_wait_col = False

    conv = l2_total / l1_total if l1_total else 0.0
    match_rate = match_count / l2_total if l2_total else 0.0

    # Sprint C-1: 进程级 VLM 并发指标（当前活跃统计，不受时间窗口限制）
    try:
        from src.host.ollama_vlm import get_concurrency_stats, get_warmup_state
        concurrency = get_concurrency_stats()
        warmup_st = get_warmup_state()
    except Exception:
        concurrency = {"peak_wait_ms": 0, "total_calls": 0, "total_wait_ms": 0}
        warmup_st = {"fresh": False, "age_sec": None, "last_error": "", "in_progress": False}

    ai_cost_list = []
    for r in cost_rows:
        item = {"provider": r[0], "model": r[1], "scene": r[2],
                "count": int(r[3]), "avg_latency_ms": int(r[4] or 0),
                "total_usd": float(r[5] or 0)}
        if has_wait_col and len(r) >= 8:
            item["avg_queue_wait_ms"] = int(r[6] or 0)
            item["peak_queue_wait_ms"] = int(r[7] or 0)
        ai_cost_list.append(item)

    return {
        "ok": True,
        "hours": hours,
        "totals": {
            "l1": int(l1_total), "l2": int(l2_total), "matched": int(match_count),
            "l1_to_l2_rate": round(conv, 3),
            "l2_match_rate": round(match_rate, 3),
            "avg_l2_latency_ms": int(avg_l2_ms),
        },
        "by_persona": [
            {"persona_key": r[0], "l1": int(r[1] or 0), "l2": int(r[2] or 0),
             "matched": int(r[3] or 0)} for r in persona_rows
        ],
        "top_devices": [
            {"device_id": r[0], "l1": int(r[1] or 0), "l2": int(r[2] or 0),
             "matched": int(r[3] or 0)} for r in device_rows
        ],
        "ai_cost": ai_cost_list,
        "vlm_concurrency": concurrency,  # Sprint C-1: {peak_wait_ms, total_calls, total_wait_ms}
        "vlm_warmup": warmup_st,         # Sprint E-0.1: {fresh, age_sec, last_error, in_progress}
    }


# ═══════════════════════════════════════════════════════════════════════
# Sprint D-1: L1 规则真阳性分析（只读报表，不自动改 yaml）
# ───────────────────────────────────────────────────────────────────────
# 目标：对历史 fb_profile_insights 里的每条 L1 reason 统计其 precision：
#     precision(reason) = (该 reason 命中 & 最终 L2 match=1 的 target 数)
#                         / (该 reason 命中的 target 总数)
# 用途：人类据此 tuning yaml 里的 weight 或增删 rule，不做自动写回。
# ═══════════════════════════════════════════════════════════════════════

@router.get("/l1-rule-analytics")
def fb_l1_rule_analytics(hours: int = 168, persona_key: Optional[str] = None):
    """统计近 N 小时内，每条 L1 reason 的命中数 / 最终 L2 match 数 / precision。

    依赖 fb_profile_insights.insights_json.l1_reasons（写入时已带）。
    """
    try:
        from src.host.database import get_conn
    except Exception:
        raise HTTPException(500, "db unavailable")
    hours = max(1, min(int(hours or 168), 24 * 90))
    since = f"-{hours} hours"

    # 聚合到 target_key 级别：一个 user 在窗口期有多次 L1/L2 记录时只看最新
    # 简化：直接扫所有 insights，若同 target 有 L2=match=1 则视为"此 target 是真阳性"。
    sql = """SELECT target_key, stage, match, insights_json
             FROM fb_profile_insights
             WHERE classified_at >= datetime('now', ?)"""
    params: list = [since]
    if persona_key:
        sql += " AND persona_key = ?"
        params.append(persona_key)

    try:
        with get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as e:
        raise HTTPException(500, f"query failed: {e}")

    # per-target: reasons 集合 + 是否最终 L2 匹配
    per_target: dict = {}
    for r in rows:
        tk = r[0]
        stage = r[1]
        match = int(r[2] or 0)
        try:
            ij = _json.loads(r[3] or "{}")
        except Exception:
            ij = {}
        d = per_target.setdefault(tk, {"reasons": set(), "l2_matched": False, "n_records": 0})
        d["n_records"] += 1
        for rz in (ij.get("l1_reasons") or []):
            d["reasons"].add(str(rz))
        if stage == "L2" and match == 1:
            d["l2_matched"] = True

    # reason 级聚合
    reason_agg: dict = {}
    for tk, info in per_target.items():
        for rz in info["reasons"]:
            a = reason_agg.setdefault(rz, {"hits": 0, "l2_match": 0})
            a["hits"] += 1
            if info["l2_matched"]:
                a["l2_match"] += 1

    rows_out = []
    for rz, a in reason_agg.items():
        precision = (a["l2_match"] / a["hits"]) if a["hits"] else 0
        rows_out.append({
            "reason": rz,
            "hits": a["hits"],
            "l2_match": a["l2_match"],
            "precision": round(precision, 3),
        })
    rows_out.sort(key=lambda x: (-x["hits"], -x["precision"]))

    # 整体 overview
    total_targets = len(per_target)
    total_l2_match = sum(1 for d in per_target.values() if d["l2_matched"])
    return {
        "ok": True,
        "hours": hours,
        "persona_key": persona_key,
        "total_targets": total_targets,
        "total_l2_matched": total_l2_match,
        "overall_l2_match_rate": round(total_l2_match / total_targets, 3) if total_targets else 0,
        "rules": rows_out,
        "recommendations": _derive_l1_recommendations(rows_out),
    }


def _derive_l1_recommendations(rows: list) -> list:
    """纯启发式建议，供人类决策，不自动改配置。"""
    out = []
    for r in rows:
        # 需要至少 5 次命中才给建议，否则样本太小
        if r["hits"] < 5:
            continue
        p = r["precision"]
        if p >= 0.75:
            out.append({"reason": r["reason"], "action": "boost_weight",
                        "hint": f"precision={p} (>=0.75)，可考虑提升该 rule 权重"})
        elif p <= 0.15:
            out.append({"reason": r["reason"], "action": "demote_or_remove",
                        "hint": f"precision={p} (<=0.15)，建议降权/移除，避免误通过"})
    return out


# ═══════════════════════════════════════════════════════════════════════
# Sprint E-0.1: VLM warmup API（供 Dashboard/SysTray 按钮触发）
# ═══════════════════════════════════════════════════════════════════════

@router.post("/vlm/warmup", operation_id="facebook_vlm_warmup")
def fb_vlm_warmup(force: bool = False, block: bool = False):
    """预热 qwen2.5vl:7b 到 GPU 显存，消掉首张 profile_hunt 的 ~56s 冷启动。

    block=False (默认)：fire-and-forget，立即返回
    block=True        ：等待 warmup 完成再返回
    force=True        ：忽略 10min TTL，强制重跑
    """
    from src.host import ollama_vlm
    if block:
        r = ollama_vlm.warmup(force=force)
        return {"ok": bool(r.get("ok")), "blocking": True, **r,
                "state": ollama_vlm.get_warmup_state()}
    queued = ollama_vlm.warmup_async(force=force)
    return {"ok": True, "blocking": False, "queued": queued,
            "state": ollama_vlm.get_warmup_state()}


# ═══════════════════════════════════════════════════════════════════════
# Sprint D-2: content_exposure 的兴趣热榜（用于后续"相似帖点赞"）
# ═══════════════════════════════════════════════════════════════════════

@router.get("/content-exposure/top-interests")
def fb_content_top_interests(hours: int = 168, persona_key: Optional[str] = None, limit: int = 30):
    """返回最近 N 小时命中用户的兴趣 topic 热榜，后续 feed_browse 可据此筛帖。"""
    try:
        from src.host.database import get_conn
    except Exception:
        raise HTTPException(500, "db unavailable")
    hours = max(1, min(int(hours or 168), 24 * 90))
    since = f"-{hours} hours"

    # meta_json 里存 persona_key；用 LIKE 过滤（避免全量解析 JSON）
    sql = """SELECT topic, COUNT(*) AS n, COUNT(DISTINCT device_id) AS devs
             FROM fb_content_exposure
             WHERE seen_at >= datetime('now', ?)"""
    params: list = [since]
    if persona_key:
        sql += " AND meta_json LIKE ?"
        params.append(f'%"persona_key": "{persona_key}"%')
    sql += " GROUP BY topic ORDER BY n DESC LIMIT ?"
    params.append(max(1, min(int(limit or 30), 200)))

    try:
        with get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as e:
        raise HTTPException(500, f"query failed: {e}")

    return {
        "ok": True,
        "hours": hours,
        "persona_key": persona_key,
        "topics": [{"topic": r[0], "count": int(r[1]), "devices": int(r[2])} for r in rows],
    }
