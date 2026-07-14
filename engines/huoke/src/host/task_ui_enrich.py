# -*- coding: utf-8 -*-
"""
任务 API 展示层：设备标签、来源、Worker、养号 phase 说明（与业务 params 分离，减少误解）。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any, Dict, List, Optional

from src.host.device_registry import config_file

logger = logging.getLogger(__name__)

_ORIGIN_ZH = {
    "ai_chat": "AI 指令",
    "api": "API / 控制台",
    "batch_api": "批量任务",
    "scheduler": "定时调度",
    "recovery": "掉线恢复",
    "tiktok_ops": "TikTok 运维",
    "platform": "平台任务",
    "tiktok_device_route": "TikTok 设备路由",
    "tiktok_onboarding": "TikTok 批量上线",
    "tiktok_daily_campaign": "TikTok 日常战役",
    "tiktok_scan_all": "TikTok 扫描用户名",
    "tiktok_cross_follow": "TikTok 互关",
    "tiktok_cross_interact": "TikTok 互互动",
    "platform_console": "平台控制台",
    "platform_batch": "平台批量",
    "campaign": "营销活动",
    "device_group": "设备分组",
    "ai_quick": "AI 快捷指令",
    "group_grid": "网格批量",
    "executor_followup_inbox": "执行器收件箱跟进",
    "tiktok_escalation": "TikTok 升级转化",
    "unknown": "未标注",
}


@lru_cache(maxsize=1)
def _chat_alias_reverse() -> Dict[str, str]:
    """序列号 -> 两位编号字符串（不含「号」）。"""
    p = config_file("chat.yaml")
    if not p.exists():
        return {}
    try:
        import yaml

        with open(p, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        aliases = raw.get("device_aliases") or {}
        rev: Dict[str, str] = {}
        for num, serial in aliases.items():
            if serial:
                rev[str(serial).strip()] = str(num).strip()
        return rev
    except Exception as e:
        logger.debug("task_ui_enrich: chat.yaml 读取失败: %s", e)
        return {}


def _short_serial(device_id: Optional[str]) -> str:
    if not device_id:
        return ""
    s = str(device_id).strip()
    if len(s) <= 10:
        return s
    return f"{s[:4]}…{s[-4:]}"


def _device_label(device_id: Optional[str]) -> str:
    """人类可读：优先 chat.yaml 别名 → 「NN号」+ 短序列号。"""
    if not device_id:
        return ""
    rev = _chat_alias_reverse()
    num = rev.get(device_id.strip())
    short = _short_serial(device_id)
    if num:
        return f"{num.zfill(2)}号 · {short}"
    return short or device_id[:12]


def device_label_for_display(device_id: Optional[str]) -> str:
    """对外导出：与任务详情 device_label 同源（供聊天执行器、API 等复用）。"""
    return _device_label(device_id)


def _infer_origin(t: Dict[str, Any]) -> str:
    p = t.get("params")
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except (json.JSONDecodeError, TypeError):
            p = {}
    if not isinstance(p, dict):
        p = {}
    ex = (p.get("_created_via") or "").strip()
    if ex:
        return ex
    if (t.get("batch_id") or "").strip():
        return "batch_api"
    return "api"


def _phase_caption(task_type: str, params: Dict[str, Any]) -> Optional[str]:
    if not task_type.startswith("tiktok_warmup"):
        return None
    ph = params.get("phase")
    if ph == "auto":
        return (
            "养号参数 phase=auto 表示由账号状态自动选择冷启动/活跃等阶段，"
            "与「定时调度自动执行任务」不是同一概念。"
        )
    if ph and ph != "auto":
        return f"当前养号阶段固定为：{ph}（由任务参数指定）。"
    return None


@lru_cache(maxsize=1)
def _policy_hint_cached() -> Optional[str]:
    """只读摘要（进程内缓存）。"""
    try:
        from src.host.task_policy import load_task_execution_policy

        pol = load_task_execution_policy()
        if pol.get("manual_execution_only"):
            parts = ["已开启 manual_execution_only：定时调度与无人恢复派发默认关闭（以各节点配置为准）。"]
            if pol.get("disable_auto_tiktok_check_inbox"):
                parts.append("收件箱类无人自动派发已禁用。")
            return " ".join(parts)
    except Exception:
        pass
    return None


def _stuck_reason_zh(task_row: Dict[str, Any], params: Dict[str, Any]) -> Optional[str]:
    """为长时间 pending 的任务给出一句中文的"为什么没跑"提示。

    覆盖四类：
        - 等待 agent（run_on_host=False 但手机上没 agent）
        - 等待重试（next_retry_at 在未来）
        - 等待救援重派（updated_at 已老旧，且不在运行中）
        - 门禁/冷却（result 中 gate 标志位）
    其他状态（running / completed / failed / cancelled）一律返回 None。
    """
    status = task_row.get("status") or ""
    if status != "pending":
        return None
    import time

    if params.get("run_on_host") is False:
        return "等待手机端 agent 拉取（run_on_host=false）"

    nra = (task_row.get("next_retry_at") or "").strip()
    if nra:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(nra.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if dt > now:
                secs = int((dt - now).total_seconds())
                if secs >= 60:
                    return f"等待重试（还有约 {secs // 60} 分钟）"
                return "等待重试（即将到期）"
            return "等待救援补派（重试已到期）"
        except Exception:
            pass

    res = task_row.get("result")
    if isinstance(res, dict):
        gate = (res.get("error") or res.get("message") or "")
        if isinstance(gate, str) and gate.startswith("[gate]"):
            return f"被派发门禁拦住：{gate[:60]}"

    updated = (task_row.get("updated_at") or task_row.get("created_at") or "").strip()
    if updated:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            if age > 120:
                return f"排队超过 {int(age // 60)} 分钟仍未执行（等待 pending 救援补派）"
        except Exception:
            pass
    return None


def build_task_ui_enrichment(task_row: Dict[str, Any]) -> Dict[str, Any]:
    """
    从原始 task 行构造 UI 附加字段（不含敏感信息）。
    用于 TaskResponse 与集群合并后的 dict。
    """
    p = task_row.get("params")
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except (json.JSONDecodeError, TypeError):
            p = {}
    if not isinstance(p, dict):
        p = {}

    device_id = task_row.get("device_id")
    task_type = task_row.get("type") or ""

    origin = _infer_origin({**task_row, "params": p})
    origin_zh = _ORIGIN_ZH.get(origin, origin or "未标注")

    result = task_row.get("result")
    if not isinstance(result, dict):
        result = {}

    worker_host = task_row.get("_worker") or result.get("dispatched_to") or result.get("dispatched_to_host")

    out: Dict[str, Any] = {
        "device_label": _device_label(device_id),
        "task_origin": origin,
        "task_origin_label_zh": origin_zh,
    }
    if worker_host:
        out["worker_host"] = str(worker_host)
    pc = _phase_caption(task_type, p)
    if pc:
        out["phase_caption"] = pc
    ph = _policy_hint_cached()
    if ph:
        out["execution_policy_hint"] = ph
    sr = _stuck_reason_zh(task_row, p)
    if sr:
        out["stuck_reason_zh"] = sr
    return out


def params_for_display(params: Any) -> Dict[str, Any]:
    """详情页 JSON：隐藏 _ 前缀元数据键，减少与业务参数混淆。"""
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except (json.JSONDecodeError, TypeError):
            return {}
    if not isinstance(params, dict):
        return {}
    return {k: v for k, v in params.items() if not str(k).startswith("_")}


_INTENT_ZH = {
    # 短名（DeepSeek API 返回）
    "warmup": "养号", "follow": "精准关注", "live_engage": "直播间互动",
    "comment_engage": "评论区互动", "campaign_playbook": "完整获客剧本",
    "plan_followers": "涨粉规划", "check_inbox": "收件箱",
    "send_dm": "发私信", "vpn_setup": "VPN配置", "stop_all": "紧急停止",
    "plan_referral": "引流规划", "multi_task": "组合任务",
    "test_follow": "测试关注", "health": "健康检查",
    "stats": "统计数据", "device_list": "设备列表",
    "geo_check": "IP检查", "risk": "风控检查",
    # 长名（_multi_intent_parse / executor 返回）— ★ P0 Fix: 补充 tiktok_ 前缀格式
    "tiktok_warmup": "养号", "tiktok_follow": "精准关注",
    "tiktok_live_engage": "直播间互动", "tiktok_comment_engage": "评论区互动",
    "tiktok_campaign_run": "完整获客剧本", "tiktok_check_inbox": "收件箱",
    "tiktok_send_dm": "发私信", "tiktok_chat": "私信引流",
    "tiktok_keyword_follow": "关键词关注",
    # ★ P3-3
    "comment_monitor_on": "开启评论监控", "comment_monitor_off": "关闭评论监控",
    "tiktok_comment_monitor": "评论回复监控", "comment_monitor": "评论回复监控",
    "tiktok_check_comment_replies": "评论回复扫描",
    "converse": "说明 / 闲聊",
    "query": "只读查询",
    "task_insights": "任务错误分析",
    "ambiguous": "意图待澄清",
}


def _build_targeting_desc(targeting: Dict[str, Any]) -> str:
    """构造人群描述字符串，如 '菲律宾 · 女性 · 20-25岁'。"""
    if not targeting:
        return ""
    parts = []
    gender_map = {"male": "♂男性", "female": "♀女性"}
    g = targeting.get("gender", "")
    if g in gender_map:
        parts.append(gender_map[g])
    age_min = targeting.get("age_min", 0)
    age_max = targeting.get("age_max", 0)
    if age_min and age_max:
        parts.append(f"{age_min}-{age_max}岁")
    elif age_min:
        parts.append(f"{age_min}岁+")
    elif age_max:
        parts.append(f"≤{age_max}岁")
    mf = targeting.get("min_followers", 0)
    if mf:
        parts.append(f"{mf // 10000}万粉+" if mf >= 10000 else f"{mf}粉+")
    interests = targeting.get("interests", [])
    if isinstance(interests, list) and interests:
        parts.append("/".join(interests[:2]))
    return " · ".join(parts) if parts else ""


def _check_device_warnings(device_ids: List[str]) -> List[str]:
    """检查目标设备是否有已知健康问题，返回警告列表。"""
    warnings = []
    try:
        from src.host.health_monitor import metrics
        for did in device_ids[:8]:
            risk = metrics.predict_disconnect_risk(did)
            if risk.get("risk") == "high":
                label = device_label_for_display(did)
                reasons = "; ".join(risk.get("reasons", []))
                warnings.append(f"⚠ {label} 掉线风险高 ({reasons[:40]})")
            elif risk.get("risk") == "medium":
                label = device_label_for_display(did)
                warnings.append(f"△ {label} 掉线风险中等，建议检查连接")
    except Exception:
        pass
    return warnings


def enrich_chat_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    为 POST /chat 的 JSON 增加:
    - task_hints: 设备可读标签 + 完整 task id（可点击打开详情）
    - intent_display: 意图中文名（用于 chip 回显）
    - targeting_desc: 人群描述字符串（用于 chip 回显）
    - device_warnings: 设备健康警告列表
    ★ P0 Fix: intent_display 和 targeting_desc 不再依赖 actions_taken，确保始终回显
    """
    # ★ P0 Fix: 先无条件设置 intent_display / targeting_desc / country_desc
    # 不能等 actions_taken 判断，否则 early return 会跳过这些 chip 数据
    intent = data.get("intent", "")
    if intent:
        data["intent_display"] = _INTENT_ZH.get(intent, intent)

    # 人群描述 chip — 从 targeting / params 两处读取
    targeting = data.get("targeting") or data.get("params", {}).get("targeting") or {}
    td = _build_targeting_desc(targeting)
    if not td:
        params = data.get("params", {})
        mini_targeting: Dict[str, Any] = {}
        for k in ("gender", "min_age", "max_age", "min_followers"):
            if params.get(k):
                mini_targeting[k] = params[k]
        td = _build_targeting_desc(mini_targeting)
    if td:
        data["targeting_desc"] = td

    # 国家 chip
    params_top = data.get("params", {})
    country = params_top.get("target_country", "")
    if country:
        data["country_desc"] = country

    actions = data.get("actions_taken")
    if not isinstance(actions, list) or not actions:
        return data

    hints: List[Dict[str, Any]] = []
    for r in actions:
        if not isinstance(r, dict):
            continue
        if r.get("error"):
            hints.append({"action": r.get("action"), "error": r.get("error")})
            continue

        act = r.get("action")
        tid_list = r.get("task_ids")
        if isinstance(tid_list, list) and tid_list:
            dids = r.get("device_ids")
            lbls = r.get("device_labels")
            tasks: List[Dict[str, Any]] = []
            for i, tid in enumerate(tid_list):
                tid_s = str(tid)
                ds = ""
                if isinstance(dids, list) and i < len(dids):
                    ds = str(dids[i])
                dl = ""
                if isinstance(lbls, list) and i < len(lbls):
                    dl = str(lbls[i])
                if not dl and ds:
                    dl = _device_label(ds)
                tasks.append(
                    {
                        "task_id": tid_s,
                        "task_id_short": tid_s[:8] + "…" if len(tid_s) > 8 else tid_s,
                        "device_serial": ds,
                        "device_label": dl,
                    }
                )
            h: Dict[str, Any] = {
                "action": act,
                "batch_id": r.get("batch_id") or "",
                "count": r.get("count", len(tasks)),
                "tasks": tasks,
            }
            hints.append(h)
            continue

        tid = r.get("task_id")
        if tid:
            tid_s = str(tid)
            ds = (r.get("device_serial") or "").strip() or None
            dl = (r.get("device_label") or "").strip() or _device_label(ds)
            hints.append(
                {
                    "action": act,
                    "task_id": tid_s,
                    "task_id_short": tid_s[:8] + "…" if len(tid_s) > 8 else tid_s,
                    "device_serial": ds or "",
                    "device_label": dl,
                }
            )

    if hints:
        data["task_hints"] = hints

    # ★ intent_display / targeting_desc / country_desc 已在函数顶部无条件设置
    # 此处只补充设备健康警告（需要 actions_taken 才能读设备列表）

    # 设备健康警告
    all_device_ids: List[str] = []
    for r in (data.get("actions_taken") or []):
        if isinstance(r.get("device_ids"), list):
            all_device_ids.extend(r["device_ids"])
        elif r.get("device_serial"):
            all_device_ids.append(r["device_serial"])
    if all_device_ids:
        warnings = _check_device_warnings(list(dict.fromkeys(all_device_ids)))
        if warnings:
            data["device_warnings"] = warnings

    return data
