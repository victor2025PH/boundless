# -*- coding: utf-8 -*-
"""
Intent Executor — 将解析出的意图转为 OpenClaw API 调用并执行。

接收 ChatAI 输出的 {intent, devices, params, targeting, goals}，
调用本机 OpenClaw API（默认端口见 src/openclaw_env.py）。

P0-1 改进:
  - 新增 _do_live_engage（直播间互动任务）
  - 新增 _do_comment_engage（评论区互动任务）
  - 新增 _do_plan_followers（涨粉目标规划）
  - 新增 _do_campaign_playbook（完整获客剧本串行编排）
  - _do_follow / _do_warmup 新增 targeting 参数透传（gender / min_age / max_age / min_followers）
  - _merge_targeting 工具：把 targeting 块合并到 params
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

import yaml

from src.host.device_registry import config_file
from src.host.task_ui_enrich import device_label_for_display

log = logging.getLogger(__name__)

_CONFIG_PATH = config_file("chat.yaml")

_INTENT_ALIASES = {
    "tiktok_follow": "follow",
    "tiktok_warmup": "warmup",
    "tiktok_chat": "send_dm",
    "tiktok_check_inbox": "check_inbox",
    "tiktok_live_engage": "live_engage",
    "tiktok_comment_engage": "comment_engage",
    # ★ P3-3: 评论监控别名
    "comment_monitor": "comment_monitor_on",
    "tiktok_comment_monitor": "comment_monitor_on",
    "comment_monitor_enable": "comment_monitor_on",
    "comment_monitor_disable": "comment_monitor_off",
}


def _load_api_url() -> str:
    from src.openclaw_env import local_api_base

    _def = local_api_base("localhost")
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("api", {}).get("base_url", _def)
    return _def


def _merge_targeting(params: dict, targeting: dict) -> dict:
    """将 targeting 块字段合并到任务 params（不覆盖已有值）。"""
    if not targeting:
        return params
    p = dict(params)
    field_map = {
        "gender":        "gender",
        "age_min":       "min_age",
        "age_max":       "max_age",
        "min_followers": "min_followers",
        "max_followers": "max_followers",
        "interests":     "interests",
    }
    for src, dst in field_map.items():
        v = targeting.get(src)
        if v is not None and v != "" and dst not in p:
            p[dst] = v
    # 把 interests 数组转成逗号字符串（executor 期望 string）
    if isinstance(p.get("interests"), list):
        p["interests"] = ",".join(p["interests"])
    return p


class IntentExecutor:
    """Execute parsed intents against the OpenClaw API."""

    def __init__(self, api_url: Optional[str] = None):
        self._api_url = (api_url or _load_api_url()).rstrip("/")

    def _auth_headers(self) -> dict:
        key = os.environ.get("OPENCLAW_API_KEY", "").strip()
        if not key:
            return {}
        return {"X-API-Key": key}

    def execute(self, intent: str, devices: List[str],
                params: Dict[str, Any],
                targeting: Optional[Dict[str, Any]] = None,
                goals: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """
        Execute an intent and return a list of action results.
        targeting / goals 会合并到对应的 API 调用参数中。
        """
        intent = _INTENT_ALIASES.get(intent, intent)
        handler = getattr(self, f"_do_{intent}", None)
        if handler is None:
            return [{"action": intent, "error": f"未知操作: {intent}"}]
        # 把 targeting 注入到 params（下游 handler 统一读取）
        merged_params = _merge_targeting(params, targeting or {})
        if goals:
            merged_params["_goals"] = goals
        try:
            return handler(devices, merged_params)
        except Exception as e:
            log.error("[Executor] %s failed: %s", intent, e)
            return [{"action": intent, "error": str(e)}]

    # ── Task creation helpers ──

    def _create_task(self, task_type: str, device_id: Optional[str],
                     params: dict) -> dict:
        p = dict(params or {})
        if "_created_via" not in p:
            p["_created_via"] = "ai_chat"
        body = {"type": task_type, "params": p, "created_via": "ai_chat"}
        if device_id and device_id != "all":
            body["device_id"] = device_id
        return self._post("/tasks", body)

    def _get_online_devices(self) -> List[str]:
        """Fetch currently online device IDs from the API."""
        devs = self._get("/devices")
        if isinstance(devs, list):
            return [d["device_id"] for d in devs
                    if d.get("status") in ("connected", "online")
                    and not d.get("busy")]
        return []

    def _create_tasks_for_devices(self, task_type: str, devices: List[str],
                                  params: dict) -> List[dict]:
        if not devices or devices == ["all"]:
            target_ids = self._get_online_devices()
            if not target_ids:
                return [{"action": task_type, "device": "all",
                         "error": "没有在线设备"}]
        else:
            target_ids = devices

        if len(target_ids) > 1:
            bp = dict(params or {})
            if "_created_via" not in bp:
                bp["_created_via"] = "ai_chat"
            r = self._post("/tasks/batch", {
                "type": task_type, "device_ids": target_ids, "params": bp,
                "created_via": "ai_chat",
            })
            full_ids = r.get("task_ids") or []
            labels = [device_label_for_display(d) for d in target_ids]
            return [{
                "action": task_type,
                "batch_id": r.get("batch_id", ""),
                "device": "all" if not devices or devices == ["all"] else ",".join(d[:6] for d in devices),
                "task_id": ",".join(t[:8] for t in full_ids),
                "task_ids": full_ids,
                "device_ids": list(target_ids),
                "device_labels": labels,
                "count": r.get("count", len(full_ids)),
            }]
        else:
            did = target_ids[0]
            r = self._create_task(task_type, did, params)
            return [{
                "action": task_type, "device": did[:12],
                "device_serial": did,
                "device_label": device_label_for_display(did),
                "task_id": r.get("task_id", ""), "error": r.get("detail", ""),
            }]

    # ── Intent handlers ──

    def _do_warmup(self, devices, params):
        p = {
            "duration_minutes": params.get("duration_minutes") or 30,
            "target_country": params.get("target_country") or "italy",
            "phase": params.get("phase") or "auto",
        }
        # targeting 透传（warmup 阶段可利用 gender/age 做内容偏好）
        for k in ("gender", "min_age", "max_age", "interests"):
            if params.get(k):
                p[k] = params[k]
        return self._create_tasks_for_devices("tiktok_warmup", devices, p)

    def _do_follow(self, devices, params):
        tc = params.get("target_country") or params.get("country") or "italy"
        p = {
            "max_follows": params.get("max_follows", 20),
            "country": tc,
            "target_country": tc,
            "language": {
                "italy": "italian", "germany": "german",
                "france": "french", "spain": "spanish",
                "philippines": "filipino", "usa": "english",
                "brazil": "portuguese", "japan": "japanese",
            }.get(tc, tc),
        }
        # 精准定向参数透传
        for k in ("gender", "min_age", "max_age", "min_followers", "max_followers",
                  "interests", "keyword", "seed_accounts"):
            v = params.get(k)
            if v is not None and v != "" and v != 0:
                p[k] = v
        return self._create_tasks_for_devices("tiktok_follow", devices, p)

    def _do_live_engage(self, devices, params):
        """直播间互动：进直播间 → 评论 → 关注活跃观众。"""
        tc = params.get("target_country") or "italy"
        # 将 target_country → target_countries 数组（executor 期望数组）
        tc_list = params.get("target_countries") or [tc]
        if isinstance(tc_list, str):
            tc_list = [x.strip() for x in tc_list.split(",") if x.strip()]
        p = {
            "target_country": tc,
            "target_countries": tc_list,
            "max_live_rooms": params.get("max_live_rooms", 3),
            "comments_per_room": params.get("comments_per_room", 2),
            "follow_active_viewers": params.get("follow_active_viewers", True),
        }
        # targeting 透传
        for k in ("gender", "min_age", "max_age", "min_followers"):
            if params.get(k):
                p[k] = params[k]
        return self._create_tasks_for_devices("tiktok_live_engage", devices, p)

    def _do_comment_engage(self, devices, params):
        """评论区互动：热门视频评论 → 关注评论者（支持 targeting 过滤）。"""
        tc = params.get("target_country") or "italy"
        p = {
            "target_country": tc,
            "max_videos": params.get("max_videos", 5),
            "comments_per_video": params.get("comments_per_video", 2),
            "follow_commenters": params.get("follow_commenters", True),
            "keyword": params.get("keyword", ""),
        }
        for k in ("gender", "min_age", "max_age"):
            if params.get(k) is not None and params.get(k) != 0:
                p[k] = params[k]
        return self._create_tasks_for_devices("tiktok_comment_engage", devices, p)

    def _do_campaign_playbook(self, devices, params):
        """
        完整获客剧本 — ★ P2-4 串行执行。
        使用 tiktok_campaign_run 任务类型（单任务内串行执行各步骤），
        比创建多个独立任务更可靠：保证顺序 + 中途失败可记录 + 不并发占用资源。
        """
        tc = params.get("target_country") or "italy"
        steps = params.get("steps") or ["warmup", "live_engage", "follow", "check_inbox"]

        targeting_extra = {}
        for k in ("gender", "min_age", "max_age", "min_followers"):
            if params.get(k):
                targeting_extra[k] = params[k]

        # 构建单个 campaign_run 任务的参数
        campaign_params = {
            "target_country": tc,
            "steps": steps,
            "warmup_minutes": params.get("warmup_minutes", 20),
            "max_live_rooms": params.get("max_live_rooms", 3),
            "max_videos": params.get("max_videos", 5),
            "max_follows": params.get("max_follows", 30),
            "max_conversations": params.get("max_conversations", 20),
            **targeting_extra,
        }

        # 每台设备分别创建一个 tiktok_campaign_run 任务（各自串行执行）
        results = self._create_tasks_for_devices("tiktok_campaign_run", devices, campaign_params)
        return results or [{"action": "campaign_playbook", "error": "无设备可用"}]

    def _do_plan_followers(self, devices, params):
        """涨粉目标规划 — 基于设备数/日上限/回关率估算。"""
        target = params.get("target_followers") or params.get("target_messages", 10000)
        country = params.get("target_country", "italy")
        gender = params.get("gender", "")
        age_min = params.get("min_age", 0)
        age_max = params.get("max_age", 0)
        min_f = params.get("min_followers", 0)

        try:
            readiness = self._get("/tiktok/readiness")
            ready = readiness.get("summary", {}).get("ready", 0)
        except Exception:
            ready = 2

        devices_available = max(ready, 1)
        daily_follow_per_device = 200
        followback_rate = 0.15  # 预估15%回关
        daily_total_follow = devices_available * daily_follow_per_device
        daily_new_followers = int(daily_total_follow * followback_rate)
        days_needed = max(1, target // max(daily_new_followers, 1))
        if target % max(daily_new_followers, 1) > 0:
            days_needed += 1

        # 人群描述
        audience_desc = ""
        if gender == "female":
            audience_desc += "女性"
        elif gender == "male":
            audience_desc += "男性"
        if age_min and age_max:
            audience_desc += f" {age_min}-{age_max}岁"
        elif age_min:
            audience_desc += f" {age_min}岁以上"
        if min_f:
            if min_f >= 10000:
                audience_desc += f" {min_f // 10000}万粉以上"
            else:
                audience_desc += f" {min_f}粉以上"
        if audience_desc:
            audience_desc = audience_desc.strip() + " "

        plan = {
            "target": target,
            "country": country,
            "audience": audience_desc.strip() or "通用",
            "resources": {
                "devices_ready": devices_available,
            },
            "calculation": {
                "daily_follow_per_device": daily_follow_per_device,
                "daily_total_follow": daily_total_follow,
                "followback_rate": f"{followback_rate * 100:.0f}%",
                "daily_new_followers": daily_new_followers,
            },
            "timeline": {
                "days_needed": days_needed,
            },
            "message": (
                f"📊 涨粉规划：{country} {audience_desc}目标 {target:,} 粉\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"在线设备：{devices_available} 台\n"
                f"每台日关注上限：{daily_follow_per_device} 人\n"
                f"预估回关率：{followback_rate * 100:.0f}%\n"
                f"预计每日新增粉丝：{daily_new_followers} 人\n"
                f"预计完成：{days_needed} 天\n\n"
                f"💡 建议执行策略：\n"
                f"  1. 养号 30 分钟（冷启/兴趣建立）\n"
                f"  2. 进 3 个{country}直播间评论（曝光+获关）\n"
                f"  3. 精准关注目标人群 {daily_total_follow // days_needed} 人/日\n"
                f"  4. 检查收件箱并AI自动回复\n\n"
                f"回复「开始」即可创建今日任务"
            ),
        }
        return [{"action": "plan_followers", "data": plan}]

    def _do_test_follow(self, devices, params):
        return self._create_tasks_for_devices("tiktok_test_follow", devices, {})

    def _do_check_inbox(self, devices, params):
        p = {
            "auto_reply": params.get("auto_reply", True),
            "max_conversations": params.get("max_conversations", 50),
        }
        return self._create_tasks_for_devices("tiktok_check_inbox", devices, p)

    def _do_send_dm(self, devices, params):
        uid = (params.get("username") or params.get("recipient") or "").strip()
        if not uid:
            return [{
                "action": "tiktok_send_dm",
                "error": "发私信需要填写收件人（recipient/username）；泛化聊天请用「检查收件箱」。",
            }]
        return self._create_tasks_for_devices("tiktok_send_dm", devices, params)

    def _do_vpn_setup(self, devices, params):
        uri = params.get("uri_or_qr", "")
        body = {"uri_or_qr": uri}
        if devices and devices != ["all"]:
            body["device_id"] = devices[0]
        else:
            body["all"] = True
        r = self._post("/vpn/setup", body)
        return [{"action": "vpn_setup", "data": r}]

    def _do_vpn_status(self, devices, params):
        if devices and devices != ["all"]:
            r = self._get(f"/vpn/status/{devices[0]}")
        else:
            r = self._get("/vpn/status")
        return [{"action": "vpn_status", "data": r}]

    def _do_vpn_stop(self, devices, params):
        results = []
        targets = devices if devices and devices != ["all"] else []
        for did in targets:
            r = self._post(f"/vpn/stop/{did}", {})
            results.append({"action": "vpn_stop", "device": did[:12], "data": r})
        return results or [{"action": "vpn_stop", "error": "请指定设备"}]

    def _do_vpn_reconnect(self, devices, params):
        results = []
        targets = devices if devices and devices != ["all"] else []
        for did in targets:
            r = self._post(f"/vpn/reconnect/{did}", {})
            results.append({"action": "vpn_reconnect", "device": did[:12], "data": r})
        return results or [{"action": "vpn_reconnect", "error": "请指定设备"}]

    def _do_device_list(self, devices, params):
        r = self._get("/devices")
        device_list = r if isinstance(r, list) else r.get("devices", r)
        return [{"action": "device_list", "data": device_list}]

    def _do_set_wallpaper(self, devices, params):
        if not devices or devices == ["all"]:
            r = self._post("/devices/wallpaper/all", {})
            return [{"action": "set_wallpaper", "device": "all", "data": r}]
        results = []
        for did in devices:
            r = self._post(f"/devices/{did}/wallpaper", {})
            results.append({"action": "set_wallpaper", "device": did[:12], "data": r})
        return results

    def _do_stats(self, devices, params):
        r = self._get("/funnel")
        return [{"action": "stats", "data": r}]

    def _do_health(self, devices, params):
        r = self._get("/health")
        return [{"action": "health", "data": r}]

    def _do_risk(self, devices, params):
        results = []
        targets = devices if devices and devices != ["all"] else []
        for did in targets:
            r = self._get(f"/risk/{did}")
            results.append({"action": "risk", "device": did[:12], "data": r})
        if not results:
            r = self._get("/risk")
            results.append({"action": "risk", "data": r})
        return results

    def _do_schedule_list(self, devices, params):
        r = self._get("/schedules")
        return [{"action": "schedule_list", "data": r}]

    def _do_schedule_create(self, devices, params):
        r = self._post("/schedules", params)
        return [{"action": "schedule_create", "data": r}]

    def _do_geo_check(self, devices, params):
        results = []
        targets = devices if devices and devices != ["all"] else []
        for did in targets:
            r = self._get(f"/geo/check/{did}")
            results.append({"action": "geo_check", "device": did[:12], "data": r})
        if not results:
            r = self._get("/geo/check-all")
            results.append({"action": "geo_check", "data": r})
        return results

    def _do_leads(self, devices, params):
        r = self._get("/leads/stats")
        return [{"action": "leads", "data": r}]

    def _do_stop_all(self, devices, params):
        r = self._post("/tasks/cancel-all", {})
        count = r.get("cancelled", 0)
        return [{"action": "stop_all",
                 "data": {"message": f"已取消 {count} 个任务", "cancelled": count}}]

    def _do_help(self, devices, params):
        return [{"action": "help"}]

    def _do_set_referral(self, devices, params):
        body = {}
        if params.get("telegram"):
            body["telegram"] = params["telegram"]
        if params.get("whatsapp"):
            body["whatsapp"] = params["whatsapp"]
        if not body:
            return [{"action": "set_referral", "error": "需要指定 telegram 或 whatsapp 账号"}]
        if devices and devices != ["all"]:
            body["device_id"] = devices[0]
        else:
            body["all"] = True
        r = self._post("/tiktok/referral-config", body)
        return [{"action": "set_referral", "data": r}]

    def _do_switch_country(self, devices, params):
        country = params.get("target_country", "italy")
        r = self._post("/vpn/pool/deploy", {"country": country, "verify_geo": False})
        return [{"action": "switch_country", "data": {
            "country": country,
            "deployed": r.get("connected", 0),
            "total": r.get("total", 0),
        }}]

    def _do_create_campaign(self, devices, params):
        country = params.get("target_country", "italy")
        results = []
        jobs = [
            {"name": f"{country} morning", "cron": "0 8 * * *",
             "action": "tiktok_auto",
             "params": {"target_country": country, "max_follows": 20, "max_chats": 5}},
            {"name": f"{country} afternoon", "cron": "0 13 * * *",
             "action": "tiktok_auto",
             "params": {"target_country": country, "max_follows": 15, "max_chats": 5}},
            {"name": f"{country} evening", "cron": "0 18 * * *",
             "action": "tiktok_auto",
             "params": {"target_country": country, "max_follows": 15, "max_chats": 5}},
        ]
        for job in jobs:
            r = self._post("/scheduled-jobs", job)
            results.append({"job_id": r.get("id", ""), "name": job["name"]})
        return [{"action": "create_campaign", "data": {
            "country": country, "jobs_created": len(results), "jobs": results
        }}]

    def _do_daily_report(self, devices, params):
        r = self._get("/tiktok/daily-report")
        return [{"action": "daily_report", "data": r}]

    def _do_multi_task(self, devices, params):
        return [{"action": "multi_task", "data": {"message": "多任务已拆分执行"}}]

    def _do_comment_monitor_on(self, devices, params):
        """★ P3-3: 启动评论回复→DM 定时监控（每3小时轮询）。"""
        cron = params.get("cron", "0 */3 * * *")
        max_replies = int(params.get("max_replies", 20))
        r = self._post("/tiktok/comment_reply_monitor/enable",
                       {"cron": cron, "max_replies": max_replies})
        return [{"action": "comment_monitor_on", "data": r}]

    def _do_comment_monitor_off(self, devices, params):
        """★ P3-3: 关闭评论回复→DM 定时监控。"""
        r = self._post("/tiktok/comment_reply_monitor/disable", {})
        return [{"action": "comment_monitor_off", "data": r}]

    def _do_plan_referral(self, devices, params):
        """智能引流规划 — 根据目标数量和当前资源生成执行计划。"""
        target = params.get("target_messages", 500)
        country = params.get("target_country", "italy")
        try:
            readiness = self._get("/tiktok/readiness")
            online = readiness.get("summary", {}).get("online", 0)
            ready = readiness.get("summary", {}).get("ready", 0)
        except Exception:
            online, ready = 2, 2
        devices_available = max(ready, 1)
        daily_follow_per_device = 200
        followback_rate = 0.15
        daily_total_follow = devices_available * daily_follow_per_device
        daily_followbacks = int(daily_total_follow * followback_rate)
        daily_messages = daily_followbacks
        days_needed = max(1, target // max(daily_messages, 1))
        if target % max(daily_messages, 1) > 0:
            days_needed += 1
        plan = {
            "target": target, "country": country,
            "resources": {"devices_online": online, "devices_ready": devices_available},
            "calculation": {
                "daily_follow_per_device": daily_follow_per_device,
                "daily_total_follow": daily_total_follow,
                "followback_rate": f"{followback_rate * 100:.0f}%",
                "daily_followbacks": daily_followbacks,
                "daily_messages": daily_messages,
            },
            "timeline": {"days_needed": days_needed},
            "message": (
                f"引流目标: {target} 条消息\n"
                f"在线设备: {devices_available} 台\n"
                f"每台日关注: {daily_follow_per_device} 人\n"
                f"预估回关率: {followback_rate * 100:.0f}%\n"
                f"预计每日引流: {daily_messages} 条\n"
                f"预计完成: {days_needed} 天\n\n"
                f"回复 '确认' 开始执行今日计划"
            ),
        }
        return [{"action": "plan_referral", "data": plan}]

    # ── HTTP helpers ──

    def http_get(self, path: str) -> Any:
        """对外只读 GET（与 _get 相同鉴权），供查询分流等调用。"""
        return self._get(path)

    def _get(self, path: str) -> Any:
        url = f"{self._api_url}{path}"
        req = urllib.request.Request(url, method="GET", headers=self._auth_headers())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            log.error("[API] GET %s → %s: %s", path, e.code, body)
            return {"error": f"HTTP {e.code}", "detail": body}
        except Exception as e:
            return {"error": str(e)}

    def _post(self, path: str, body: dict) -> Any:
        url = f"{self._api_url}{path}"
        data = json.dumps(body).encode()
        h = {"Content-Type": "application/json", **self._auth_headers()}
        req = urllib.request.Request(url, data=data, method="POST", headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode() if e.fp else ""
            log.error("[API] POST %s → %s: %s", path, e.code, body_text)
            return {"error": f"HTTP {e.code}", "detail": body_text}
        except Exception as e:
            return {"error": str(e)}
