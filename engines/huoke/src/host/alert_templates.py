# -*- coding: utf-8 -*-
"""
结构化告警文案（alert_code + 参数）→ 固定中英文模板，避免散落拼接与翻译漂移。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

# code → (zh_template, en_template)，占位符 {key}
_TEMPLATES: Dict[str, Tuple[str, str]] = {
    "DEVICE_OFFLINE_STREAK": (
        "设备掉线 (连续第{n}次)",
        "Device offline (streak {n})",
    ),
    "PHONE_OFFLINE": (
        "手机掉线 {host_tag}{display} (连续第{n}次)",
        "Phone offline {host_tag}{display} (streak {n})",
    ),
    "DEVICE_OFFLINE_CRITICAL": (
        "设备连续掉线 {n} 次，需人工检查",
        "Device offline {n} times in a row — manual check required",
    ),
    "DEVICE_PREDICTIVE_HIGH": (
        "预测性告警: {reasons} (风险分 {score})",
        "Predictive alert: {reasons_en} (risk score {score})",
    ),
    "DEVICE_ISOLATED": (
        "设备已隔离，不再分配新任务",
        "Device isolated; no new tasks will be assigned",
    ),
    "DEVICE_UNISOLATED": (
        "设备已解除隔离",
        "Device isolation cleared",
    ),
    "DEVICE_BACK_ONLINE": (
        "设备重新上线{tail_zh}",
        "Device back online{tail_en}",
    ),
    "DEVICE_FIRST_ONLINE": (
        "设备首次上线（本会话内首次发现）",
        "Device seen online for the first time in this session",
    ),
    "SCRCPY_STREAM_RESTORED": (
        "scrcpy 投屏流已自动恢复",
        "scrcpy stream auto-resumed",
    ),
    "TASK_STUCK_TERMINATED": (
        "任务 {task_id} 超时终止（约 {minutes} 分钟），已由健康监控结束",
        "Task {task_id} terminated as stuck (~{minutes} min) by health monitor",
    ),
    "SCREEN_ANOMALY_CRITICAL": (
        "屏幕严重异常: {atype} — {desc}",
        "Critical screen anomaly: {atype_en} — {desc_en}",
    ),
    "U2_DEEP_RECONNECT_FAILED": (
        "u2 深度重连失败，需人工处理",
        "u2 deep reconnect failed — manual intervention required",
    ),
    "ATX_AGENT_RESTART_OK": (
        "atx-agent 重启成功，u2 已恢复",
        "atx-agent restarted; u2 recovered",
    ),
    "APP_AUTO_RESTART_OK": (
        "应用自动重启成功: {pkg}",
        "App auto-restarted: {pkg}",
    ),
    "APP_AUTO_RESTART_FAIL": (
        "应用自动重启失败（任务在前台但无法拉起目标包）",
        "App auto-restart failed (task active but target package not foreground)",
    ),
    "TASKS_RECOVERED_AFTER_RECONNECT": (
        "设备重新上线，恢复 {count} 个中断任务",
        "Device back online; re-queued {count} interrupted task(s)",
    ),
    "ADB_RECOVERY_SUCCESS": (
        "自动恢复成功 (L{level}: {method})",
        "Auto-recovery succeeded (L{level}: {method})",
    ),
    "ADB_RECOVERY_EXHAUSTED": (
        "所有自动恢复级别已用尽，5 分钟后重试",
        "All auto-recovery stages exhausted; retry in 5 minutes",
    ),
    "VPN_HEALTH": (
        "[VPN] {text}",
        "[VPN] {text_en}",
    ),
    "CLUSTER_WORKER_ONLINE_DROP": (
        "Worker「{host}」在线 adb 数 {adb_o}→{adb_n}（心跳登记 {reg_o}→{reg_n}）",
        "Worker \"{host}\" ADB online count {adb_o}→{adb_n} (heartbeat registry {reg_o}→{reg_n})",
    ),
    # Phase F1: 去重防线告警
    "DEDUP_FRIEND_REQUEST_BLOCKED": (
        "🔒 跨设备去重拦截: {peer_name} (已被 {existing_device} 加过, 当前设备 {device_id})",
        "🔒 Cross-device dedup: {peer_name} blocked (already requested by {existing_device}, current {device_id})",
    ),
    "DEDUP_DAILY_SUMMARY": (
        "📊 去重日报: 今日拦截好友请求 {blocked_count} 次, 对话锁冲突 {lock_conflicts} 次",
        "📊 Dedup daily: {blocked_count} friend requests blocked, {lock_conflicts} conversation lock conflicts",
    ),
    "DEDUP_HIGH_FREQUENCY_PEER": (
        "⚠️ 高频重复目标: {peer_name} 被 {device_count} 台设备尝试, 可能目标池重叠",
        "⚠️ High-freq duplicate: {peer_name} attempted by {device_count} devices — possible pool overlap",
    ),
    "MERGE_QUALITY_SUSPECTS": (
        "🔍 发现 {suspect_count} 条可疑合并需要人工审核 (名字相似度低或身份数异常)",
        "🔍 Found {suspect_count} suspect merge(s) requiring manual review (name dissimilarity or high identity count)",
    ),
}


class _SafeParams(dict):
    """format_map 缺省键 → 空串，避免 KeyError。"""

    def __missing__(self, key: str) -> str:
        return ""


def render_alert_pair(code: str, params: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """
    返回 (中文正文, 英文正文)。未知 code 时回退为 params['message'] / params['message_en']。
    """
    raw = dict(params or {})
    if code not in _TEMPLATES:
        zh = str(raw.get("message", raw.get("zh", "")))
        en = str(raw.get("message_en", raw.get("en", zh)))
        return zh, en
    zh_t, en_t = _TEMPLATES[code]
    p = _SafeParams(raw)
    if code == "DEVICE_PREDICTIVE_HIGH":
        reasons = str(raw.get("reasons", ""))
        reasons_en = str(raw.get("reasons_en") or reasons)
        score = int(raw.get("score", 0))
        return (
            zh_t.format(reasons=reasons, score=score),
            en_t.format(reasons_en=reasons_en, score=score),
        )
    if code == "SCREEN_ANOMALY_CRITICAL":
        atype = str(raw.get("atype", ""))
        desc = str(raw.get("desc", ""))
        atype_en = str(raw.get("atype_en") or atype)
        desc_en = str(raw.get("desc_en") or desc)
        return (
            zh_t.format(atype=atype, desc=desc),
            en_t.format(atype_en=atype_en, desc_en=desc_en),
        )
    return zh_t.format_map(p), en_t.format_map(p)


def dedup_fingerprint(
    level: str,
    device_id: str,
    alert_code: Optional[str],
    params: Optional[Dict[str, Any]],
    message: str,
) -> str:
    """供去重：有 code 时用 code+规范化 params，避免同义改写导致重复推送。"""
    if alert_code:
        try:
            blob = json.dumps(
                {"level": level, "device_id": device_id, "c": alert_code, "p": params or {}},
                sort_keys=True,
                ensure_ascii=False,
            )
        except TypeError:
            blob = f"{level}:{device_id}:{alert_code}:{message}"
        return blob
    return f"{level}:{device_id}:{message}"


def list_known_codes() -> Tuple[str, ...]:
    return tuple(sorted(_TEMPLATES.keys()))
