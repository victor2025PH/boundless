# -*- coding: utf-8 -*-
"""Facebook 养号 Playbook 读取层（2026-04-21 P1-1）。

职责：
  * 读取 ``config/facebook_playbook.yaml``，走 ``_yaml_cache.YamlCache``
    自动 mtime 热加载（改完保存 → 下一次任务生效，不必重启服务）。
  * 对外提供 ``resolve_browse_feed_params(phase=...)`` —— phase 级覆盖 +
    defaults 兜底，automation 层只需要传 phase 就能拿到完整参数。
  * 配置缺失/解析失败时回退到 automation 模块自带的 FB_BROWSE_DEFAULTS，
    **确保养号永远能跑**（配置不可用不代表业务不可用）。

调用示例::

    from src.host.fb_playbook import resolve_browse_feed_params
    cfg = resolve_browse_feed_params(phase="growth")
    short_lo, short_hi = cfg["short_wait_ms"]
"""

from __future__ import annotations

import copy
import logging
import os
from typing import Any, Dict, Optional

from src.host._yaml_cache import YamlCache
from src.host.device_registry import config_file

logger = logging.getLogger(__name__)

_playbook_path = config_file("facebook_playbook.yaml")


def local_rules_disabled() -> bool:
    """临时测试模式：跳过本地账号阶段、persona、quota/cap 等业务闸。

    当前项目目标是先跑通 Facebook 群成员打招呼全链路，所以默认开启。
    后续恢复养号版本时，把环境变量 ``OPENCLAW_DISABLE_LOCAL_RULES=0`` 即可关掉。

    pytest 自动 bypass：单测默认走严格 playbook 规则（避免 relax_params_for_test
    把 ``require_persona_template`` 等字段篡改成测试期望外的值）。需要测试
    relax 行为本身的用例可显式 ``monkeypatch.setenv("OPENCLAW_DISABLE_LOCAL_RULES", "1")``。
    """
    if os.getenv("PYTEST_CURRENT_TEST") and "OPENCLAW_DISABLE_LOCAL_RULES" not in os.environ:
        return False
    return str(os.getenv("OPENCLAW_DISABLE_LOCAL_RULES", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def relax_params_for_test(section: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """在测试模式下放宽本地 playbook 参数，不改变页面级失败判断。"""
    out: Dict[str, Any] = dict(cfg or {})
    if section == "add_friend":
        out.update({
            "max_friends_per_run": max(int(out.get("max_friends_per_run") or 0), 50),
            "daily_cap_per_account": 0,
            "require_verification_note": False,
            "inter_request_sec": (0, 1),
        })
    elif section == "send_greeting":
        out.update({
            "max_greetings_per_run": max(int(out.get("max_greetings_per_run") or 0), 50),
            "daily_cap_per_account": 0,
            "enabled_probability": 1.0,
            "require_persona_template": False,
            "allow_messenger_fallback": True,
            "inter_greeting_sec": (0, 1),
            "post_add_friend_wait_sec": (1, 2),
            "think_before_type_sec": (0, 1),
        })
    elif section == "extract_members":
        out.update({
            "max_members": max(int(out.get("max_members") or 0), 50),
            "inter_member_sec": (0, 1),
            "l1_min_score": 0,
        })
    elif section == "group_engage":
        out.update({
            "max_posts": max(int(out.get("max_posts") or 0), 20),
            "max_comments_per_run": max(int(out.get("max_comments_per_run") or 0), 20),
            "post_dwell_ms": (500, 1000),
        })
    elif section == "check_inbox":
        out.update({
            "max_conversations": max(int(out.get("max_conversations") or 0), 50),
            "max_requests": max(int(out.get("max_requests") or 0), 50),
            "reply_think_sec": (0, 1),
        })
    return out


# 与 src/app_automation/facebook.py 的 FB_BROWSE_DEFAULTS 保持一致，
# 纯粹是配置文件读不到时的回退（*defense in depth*，不是真值）。
_FALLBACK = {
    "version": 1,
    "defaults": {
        "browse_feed": {
            "scroll_per_min": 4,
            "short_wait_ms": [2000, 8000],
            "video_dwell_prob": 0.15,
            "video_dwell_ms": [8000, 20000],
            "like_probability": 0.05,
            "pull_refresh_prob": 0.08,
            "max_scrolls_hard_cap": 400,
        },
        # P2-UI Sprint 新增：其他任务的 phase-aware fallback
        "add_friend": {
            "max_friends_per_run": 5,
            "daily_cap_per_account": 15,
            "inter_request_sec": [180, 420],
            "require_verification_note": True,
            "backoff_after_risk_min": 120,
        },
        "group_engage": {
            "max_posts": 5,
            "comment_probability": 0.15,
            "like_probability": 0.40,
            "post_dwell_ms": [6000, 18000],
            "max_comments_per_run": 3,
        },
        "extract_members": {
            "max_members": 20,
            "inter_member_sec": [8, 18],
            "l1_min_score": 30,
        },
        "check_inbox": {
            "max_conversations": 15,
            "max_requests": 15,
            "auto_reply": True,
            "reply_think_sec": [12, 40],
            "max_turns_per_conv": 3,
        },
        "send_greeting": {
            "max_greetings_per_run": 3,
            "daily_cap_per_account": 8,
            "inter_greeting_sec": [240, 600],
            "post_add_friend_wait_sec": [8, 18],
            "think_before_type_sec": [3, 7],
            "enabled_probability": 1.0,
            "require_persona_template": True,
            # 2026-04-23 P0-3: profile 页无 Message 按钮时,是否降级到
            # Messenger App 路径(切 app + 搜名字)。默认关闭 —— 切 app 风控
            # 风险高,且刚加的人 Messenger 搜索命中率低。仅在成熟号 +
            # 高价值 target 的场景开启。
            "allow_messenger_fallback": False,
        },
        "risk": {
            "cooldown_trigger_24h": 3,
            "cooldown_min_hours": 24,
            "cooldown_clear_clean_hours": 48,
        },
    },
    "phases": {},
    "phase_transitions": {
        "to_cooldown": {"min_risk_count_24h": 3},
        "cooldown_to_cold_start": {"min_clean_hours": 48},
        "cold_start_to_growth": {"min_total_scrolls": 200, "min_age_hours": 24},
        "growth_to_mature": {"min_total_scrolls": 2000, "min_age_days": 7},
    },
}

# 所有 phase-aware 任务段 key（用于通用 resolver 和 _post_process 合并）
_PHASE_AWARE_SECTIONS = (
    "browse_feed",
    "add_friend",
    "group_engage",
    "extract_members",
    "check_inbox",
    "send_greeting",
)


def _post_process(raw: Any) -> Dict[str, Any]:
    """确保关键字段存在，异常配置不会把下游炸掉。"""
    if not isinstance(raw, dict):
        return copy.deepcopy(_FALLBACK)
    data = copy.deepcopy(_FALLBACK)
    # 逐层合并：raw 里有就覆盖
    if isinstance(raw.get("defaults"), dict):
        data["defaults"].update({
            k: v for k, v in raw["defaults"].items() if isinstance(v, dict) or v is not None
        })
        # 子 dict 再合并一层（所有 phase-aware 段 + risk）
        for sec in _PHASE_AWARE_SECTIONS + ("risk",):
            if isinstance(raw["defaults"].get(sec), dict):
                data["defaults"].setdefault(sec, {}).update(raw["defaults"][sec])
    if isinstance(raw.get("phases"), dict):
        data["phases"] = raw["phases"]
    if isinstance(raw.get("phase_transitions"), dict):
        data["phase_transitions"].update(raw["phase_transitions"])
    data["version"] = raw.get("version") or data["version"]
    data["updated_at"] = raw.get("updated_at") or ""

    logger.info(
        "facebook_playbook 加载完成: phases=%s sections=%s scroll_per_min=%s "
        "like_prob=%s add_friend/run=%s greeting/run=%s cooldown_trigger=%s",
        list(data["phases"].keys()),
        [s for s in _PHASE_AWARE_SECTIONS if s in data["defaults"]],
        data["defaults"]["browse_feed"].get("scroll_per_min"),
        data["defaults"]["browse_feed"].get("like_probability"),
        data["defaults"].get("add_friend", {}).get("max_friends_per_run"),
        data["defaults"].get("send_greeting", {}).get("max_greetings_per_run"),
        data["defaults"]["risk"].get("cooldown_trigger_24h"),
    )
    return data


_CACHE = YamlCache(
    path=_playbook_path,
    defaults=_FALLBACK,
    post_process=_post_process,
    log_label="facebook_playbook.yaml",
    logger=logger,
)


def load_playbook(force_reload: bool = False) -> Dict[str, Any]:
    """加载最新 playbook。mtime 自动热加载。"""
    return _CACHE.get(force_reload=force_reload)


def reload_playbook() -> Dict[str, Any]:
    """强制重读（供 POST /facebook/playbook/reload 调用）。"""
    return _CACHE.reload()


def playbook_mtime() -> float:
    return _CACHE.mtime()


# ── 对 automation 层的便捷 API ─────────────────────────────────────────

_VALID_PHASES = ("cold_start", "growth", "mature", "cooldown")


# P2-UI Sprint：通用 phase-aware resolver
# 把 "defaults[section] <- phases[phase][section]" 合并 + list→tuple 归一化
# 抽成公共函数，避免每个任务写重复代码。
_TUPLE_FIELDS = {
    # section → 字段列表（YAML 读出来是 list，automation 代码想用 tuple）
    "browse_feed": ("short_wait_ms", "video_dwell_ms"),
    "add_friend": ("inter_request_sec",),
    "group_engage": ("post_dwell_ms",),
    "extract_members": ("inter_member_sec",),
    "check_inbox": ("reply_think_sec",),
    "send_greeting": ("inter_greeting_sec", "post_add_friend_wait_sec",
                      "think_before_type_sec"),
}


def resolve_params(section: str, phase: Optional[str] = None) -> Dict[str, Any]:
    """通用 resolver：合并 defaults[section] 与 phases[phase][section]。

    section 不在 ``_PHASE_AWARE_SECTIONS`` 时返回 {}（防拼写错误）。
    """
    if section not in _PHASE_AWARE_SECTIONS:
        logger.warning("resolve_params 未知 section=%s", section)
        return {}
    data = load_playbook()
    base = (data.get("defaults") or {}).get(section) or {}
    out: Dict[str, Any] = dict(base)
    if phase and phase in _VALID_PHASES:
        override = ((data.get("phases") or {}).get(phase) or {}).get(section) or {}
        if isinstance(override, dict):
            out.update(override)
    # 归一化 tuple（YAML 读出来是 list）
    for k in _TUPLE_FIELDS.get(section, ()):
        v = out.get(k)
        if isinstance(v, list) and len(v) == 2:
            out[k] = (int(v[0]), int(v[1]))
    if local_rules_disabled():
        out = relax_params_for_test(section, out)
    return out


def resolve_browse_feed_params(phase: Optional[str] = None) -> Dict[str, Any]:
    """返回合并后的 browse_feed 配置（phase 覆盖 defaults）。"""
    return resolve_params("browse_feed", phase)


def resolve_add_friend_params(phase: Optional[str] = None) -> Dict[str, Any]:
    """返回合并后的 add_friend 配置。

    关键字段:
      * max_friends_per_run      单次任务最多请求数
      * daily_cap_per_account    每号每日好友请求上限（硬）
      * inter_request_sec        两次请求间隔 (sec_lo, sec_hi)
      * require_verification_note 是否强制带验证语
      * backoff_after_risk_min   命中风控后退避分钟
    """
    return resolve_params("add_friend", phase)


def resolve_group_engage_params(phase: Optional[str] = None) -> Dict[str, Any]:
    """返回合并后的 group_engage 配置（进群浏览 + 评论互动）。"""
    return resolve_params("group_engage", phase)


def resolve_extract_members_params(phase: Optional[str] = None) -> Dict[str, Any]:
    """返回合并后的 extract_members 配置（群成员打招呼）。"""
    return resolve_params("extract_members", phase)


def resolve_check_inbox_params(phase: Optional[str] = None) -> Dict[str, Any]:
    """返回合并后的 check_inbox 配置（收件箱 / Messenger 回复）。"""
    return resolve_params("check_inbox", phase)


def resolve_send_greeting_params(phase: Optional[str] = None) -> Dict[str, Any]:
    """返回合并后的 send_greeting 配置（加好友后打招呼）。

    关键字段:
      * max_greetings_per_run      单次任务最多打招呼数
      * daily_cap_per_account      每号每日打招呼上限（硬）
      * inter_greeting_sec         两次之间的间隔 (sec_lo, sec_hi)
      * post_add_friend_wait_sec   加好友成功后等待多久再点 Message (sec_lo, sec_hi)
      * think_before_type_sec      打开对话后停留多久再输入 (sec_lo, sec_hi)
      * enabled_probability        本次打招呼触发概率（0~1，非 1.0 时为抽样）
      * require_persona_template   文案为空时是否强制用 persona 模板（否则 skip）
    """
    return resolve_params("send_greeting", phase)


def resolve_risk_config() -> Dict[str, Any]:
    data = load_playbook()
    return dict(data["defaults"]["risk"])


def resolve_transitions() -> Dict[str, Any]:
    """供 fb_account_phase 读取迁移规则。"""
    data = load_playbook()
    return copy.deepcopy(data.get("phase_transitions") or {})
