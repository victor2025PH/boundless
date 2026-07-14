# -*- coding: utf-8 -*-
"""
Chat Controller — 核心调度器，连接 AI 解析 + API 执行 + 回复生成。

流程: 用户消息 → AI 解析意图 → 执行 API 调用 → 生成中文回复
支持 dry_run（仅预览）与待确认计划 execute_pending_plan。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

import yaml

from src.host.device_registry import config_file

from .ai_client import ChatAI, get_chat_ai
from .intent_executor import IntentExecutor

log = logging.getLogger(__name__)

_chat_preflight_yaml_cache: Optional[Dict[str, Any]] = None
_unified_cfg_cache: Optional[Dict[str, Any]] = None


def _has_real_api_key(ai: ChatAI) -> bool:
    """避免 unittest.mock 的非 str _api_key 被当成真值。"""
    k = getattr(ai, "_api_key", "")
    return isinstance(k, str) and len(k.strip()) > 0


def _unified_parse_enabled(ai: ChatAI) -> bool:
    """config unified_parse.enabled + OPENCLAW_UNIFIED_PARSE=1|0 + 必须有真实 API Key。"""
    global _unified_cfg_cache
    env = os.environ.get("OPENCLAW_UNIFIED_PARSE", "").strip()
    if env == "0":
        return False
    if env == "1":
        return _has_real_api_key(ai)
    if _unified_cfg_cache is None:
        try:
            p = config_file("chat.yaml")
            if p.exists():
                with open(p, encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
                _unified_cfg_cache = raw.get("unified_parse") or {}
            else:
                _unified_cfg_cache = {}
        except Exception:
            _unified_cfg_cache = {}
    en = bool((_unified_cfg_cache or {}).get("enabled", False))
    return en and _has_real_api_key(ai)


def _collect_task_ids(action_results: List[dict]) -> List[str]:
    """从执行结果汇总完整 task id 列表（优先 batch 的 task_ids 数组）。"""
    out: List[str] = []
    for r in action_results:
        lst = r.get("task_ids")
        if isinstance(lst, list) and lst:
            out.extend(str(x) for x in lst if x)
            continue
        tid = r.get("task_id")
        if not tid:
            continue
        s = str(tid)
        if "," in s:
            out.extend(p.strip() for p in s.split(",") if p.strip())
        else:
            out.append(s)
    return out


def _load_chat_preflight_section() -> Dict[str, Any]:
    """读取 config/chat.yaml 中 preflight_before_execute。"""
    global _chat_preflight_yaml_cache
    if _chat_preflight_yaml_cache is not None:
        return _chat_preflight_yaml_cache
    p = config_file("chat.yaml")
    if not p.exists():
        _chat_preflight_yaml_cache = {}
        return _chat_preflight_yaml_cache
    try:
        with open(p, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        _chat_preflight_yaml_cache = raw.get("preflight_before_execute") or {}
    except Exception:
        _chat_preflight_yaml_cache = {}
    return _chat_preflight_yaml_cache


def _chat_preflight_should_run() -> bool:
    """OPENCLAW_CHAT_PREFLIGHT=0 关；=1 开；未设时读 chat.yaml enabled（默认关）。"""
    v = os.environ.get("OPENCLAW_CHAT_PREFLIGHT", "").strip()
    if v == "0":
        return False
    if v == "1":
        return True
    sec = _load_chat_preflight_section()
    return bool(sec.get("enabled", False))


def _chat_preflight_mode() -> str:
    """OPENCLAW_CHAT_PREFLIGHT_MODE 可覆盖 yaml（full / network_only / none）。"""
    env_m = os.environ.get("OPENCLAW_CHAT_PREFLIGHT_MODE", "").strip().lower()
    if env_m in ("full", "network_only", "none"):
        return env_m
    sec = _load_chat_preflight_section()
    m = (sec.get("mode") or "network_only").strip().lower()
    if m in ("full", "network_only", "none"):
        return m
    return "network_only"


def _chat_preflight_max_devices() -> int:
    sec = _load_chat_preflight_section()
    try:
        return max(1, min(64, int(sec.get("max_devices", 12))))
    except (TypeError, ValueError):
        return 12


def _chat_default_target_country() -> str:
    """与 chat.yaml defaults.target_country 对齐，供 full 预检做出口国比对。"""
    p = config_file("chat.yaml")
    try:
        with open(p, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        d = (raw.get("defaults") or {}).get("target_country")
        if isinstance(d, str) and d.strip():
            return d.strip().lower()
    except Exception:
        pass
    return "italy"


def _resolve_preflight_task_country(params: Optional[Dict[str, Any]]) -> Optional[str]:
    if not params:
        return None
    c = params.get("target_country") or params.get("country")
    if isinstance(c, str) and c.strip():
        return c.strip().lower()
    return None


def _preflight_tips_for_steps(steps: set) -> str:
    """按失败步骤组合建议，避免「网络失败却先提 VPN」。"""
    lines = [
        "\n\n说明：未通过预检时不会创建任务，可减少任务中心中的必败记录。",
        "针对性建议：",
    ]
    has_specific = False
    if "network" in steps:
        has_specific = True
        lines.append(
            "• 网络：确认 Wi‑Fi/蜂窝可用、关闭飞行模式；若提示 HTTP 码为空，常见于 DNS/ROM 限制，请在设备页「诊断」。"
        )
    if "vpn" in steps:
        has_specific = True
        lines.append(
            "• VPN：需要代理出口时，请打开 v2rayNG 并连接与任务地区一致的节点。"
            "若本地 SIM/WiFi 已在目标国，可将预检设为 full 并确保解析到目标国家。"
        )
    if "account" in steps:
        has_specific = True
        lines.append("• TikTok：确认应用已安装，可先手动打开一次。")
    if not has_specific:
        lines.append("• 请在设备页运行「诊断」或查看主控日志。")
    return "\n".join(lines)


class ChatController:
    """
    Stateful chat controller for a single session.

    Usage:
        ctrl = ChatController()
        reply = ctrl.handle("01号手机养号30分钟")
        print(reply["reply"])
    """

    def __init__(self, ai: Optional[ChatAI] = None,
                 executor: Optional[IntentExecutor] = None):
        self._ai = ai or get_chat_ai()
        self._executor = executor or IntentExecutor()
        self._history: List[Dict[str, str]] = []

    def _resolve_preflight_devices(self, devices: List[str]) -> List[str]:
        if not devices or devices == ["all"]:
            return self._executor._get_online_devices()
        return list(devices)

    def _preflight_block_message(
        self, devices: List[str], params: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """执行前预检，不通过则返回中文说明（不创建任务）。"""
        if not _chat_preflight_should_run():
            return None
        mode = _chat_preflight_mode()
        if mode == "none":
            return None
        max_d = _chat_preflight_max_devices()
        try:
            from src.host.preflight import run_preflight
        except Exception as e:
            log.warning("[Chat] 无法加载预检模块，跳过: %s", e)
            return None
        target = self._resolve_preflight_devices(devices)
        if not target:
            return (
                "当前没有在线设备可通过预检，本次未创建任务。\n"
                "请确认设备已连接、状态为在线，或稍后在「设备管理」重试。"
            )
        task_country: Optional[str] = _resolve_preflight_task_country(params)
        if mode == "full":
            task_country = task_country or _chat_default_target_country()

        bad: List[str] = []
        steps_seen: Set[str] = set()
        for did in target[:max_d]:
            try:
                pr = run_preflight(
                    did,
                    skip_cache=False,
                    mode=mode,
                    task_target_country=task_country if mode == "full" else None,
                )
            except Exception as e:
                log.debug("[Chat] preflight %s: %s", did[:8], e)
                continue
            if not pr.passed:
                step = pr.blocked_step or "?"
                reason = pr.blocked_reason or "未通过预检"
                steps_seen.add((step or "").strip().lower())
                bad.append(f"• {did[:14]}：{step} — {reason}")
        if not bad:
            return None
        tips = _preflight_tips_for_steps(steps_seen)
        return "以下设备预检未通过，本次未创建任务：\n" + "\n".join(bad[:10]) + tips

    def _append_history(self, user_message: str, reply: str) -> None:
        self._history.append({
            "role": "user", "content": user_message,
            "timestamp": time.strftime("%H:%M:%S"),
        })
        self._history.append({
            "role": "assistant", "content": reply,
            "timestamp": time.strftime("%H:%M:%S"),
        })

    def _build_dry_run_reply(
        self,
        intent: str,
        devices: list,
        params: dict,
        intent_result: dict,
        is_multi: bool,
        intents_count: int,
    ) -> str:
        lines = ["【预览】未创建任务、未调用设备。"]
        lines.append(f"意图: {intent}")
        if devices:
            lines.append(f"目标设备: {devices}")
        if params:
            lines.append(f"参数: {params}")
        if is_multi:
            lines.append(f"多步任务数: {intents_count}")
        lines.append("—")
        lines.append("下一步：在请求体中传 confirm:true 与同一 session_id 以执行；或修改指令后再次 dry_run。")
        if intent == "help":
            return self._ai._help_text()
        return "\n".join(lines)

    def execute_pending_plan(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """执行 dry_run 阶段保存的 pending_plan（由路由在 confirm 时调用）。"""
        t0 = time.time()
        user_message = (plan.get("user_message") or "").strip()
        intent_result = plan.get("intent_result") or {}
        action_results: List[dict] = []

        early_intent = plan.get("intent", "help")
        if plan.get("multi") and plan.get("intents"):
            early_intent = "multi_task"
        if early_intent != "help":
            block_msg = self._preflight_block_message(
                plan.get("devices") or [], params=plan.get("params")
            )
            if block_msg:
                self._append_history(user_message or "[确认执行]", block_msg)
                elapsed = int((time.time() - t0) * 1000)
                return {
                    "reply": block_msg,
                    "intent": early_intent,
                    "devices": plan.get("devices", []),
                    "params": plan.get("params", {}),
                    "actions_taken": [],
                    "task_ids": [],
                    "elapsed_ms": elapsed,
                    "dry_run": False,
                    "confirmed": False,
                    "preflight_blocked": True,
                    "chat_mode": "execute",
                }

        _targeting = plan.get("targeting", {})
        _goals = plan.get("goals", {})

        if plan.get("multi") and plan.get("intents"):
            devices = plan.get("devices", [])
            params = plan.get("params", {})
            for sub in plan["intents"]:
                sub_action = sub["action"]
                sub_params = {**params, **sub.get("params", {})}
                results = self._executor.execute(
                    sub_action, devices, sub_params,
                    targeting=_targeting, goals=_goals)
                action_results.extend(results)
            intent = "multi_task"
            devices = plan.get("devices", [])
            params = plan.get("params", {})
        else:
            intent = plan.get("intent", "help")
            devices = plan.get("devices", [])
            params = plan.get("params", {})
            action_results = self._executor.execute(
                intent, devices, params,
                targeting=_targeting, goals=_goals)

        reply = self._ai.generate_reply(intent_result, action_results, user_message)
        task_ids = _collect_task_ids(action_results)
        self._append_history(user_message or "[确认执行]", reply)
        elapsed = int((time.time() - t0) * 1000)
        return {
            "reply": reply,
            "intent": intent,
            "devices": devices,
            "params": params,
            "actions_taken": action_results,
            "task_ids": task_ids,
            "elapsed_ms": elapsed,
            "dry_run": False,
            "confirmed": True,
            "chat_mode": "execute",
        }

    def _build_execute_state_unified(
        self, u: Dict[str, Any], user_message: str
    ) -> Dict[str, Any]:
        """将 parse_unified 的 execute 分支转为与旧链路一致的变量。"""
        multi_local = self._ai._multi_intent_parse(user_message)
        is_multi = bool(u.get("multi_task")) and u.get("intents")
        intents_list: List = []
        if is_multi:
            intents_list = list(u.get("intents") or [])
            intent = "multi_task"
            devices = u.get("devices") or []
            params = u.get("params") or {}
            intent_result: Dict[str, Any] = {
                "intent": "multi_task",
                "devices": devices,
                "params": params,
                "intents": intents_list,
                "targeting": u.get("targeting") or {},
                "goals": u.get("goals") or {},
            }
            multi = intent_result
        else:
            intent = u.get("intent", "help")
            devices = u.get("devices") or []
            params = u.get("params") or {}
            intent_result = {
                "intent": intent,
                "devices": devices,
                "params": params,
                "targeting": u.get("targeting") or {},
                "goals": u.get("goals") or {},
            }
            _ml = user_message.lower()
            local_intent = multi_local.get("intent", "")
            if intent in ("warmup", "help", "") and local_intent not in (
                "", "multi_task", "warmup", "help"):
                log.info("[Chat] unified+本地覆盖: %s → %s", intent, local_intent)
                intent = local_intent
                intent_result = multi_local
                params = multi_local.get("params", {})
                devices = multi_local.get("devices", devices)
            elif intent == "warmup" and any(
                    w in _ml for w in
                    ["直播", "live", "直播间", "进直播", "评论区", "comment_engage"]):
                override = ("live_engage" if any(w in _ml for w in ["直播", "live", "直播间", "进直播"])
                              else "comment_engage")
                log.info("[Chat] unified: warmup → %s", override)
                intent = override
                params.setdefault(
                    "target_country",
                    multi_local.get("params", {}).get("target_country") or "italy")
                params.setdefault("max_live_rooms", 3)
                intent_result["intent"] = intent
                intent_result["params"] = params

            if intent_result.get("intent") == "plan_referral" or (
                    intent == "warmup" and "引流" in user_message
                    and self._ai._extract_count(user_message) >= 100):
                count = self._ai._extract_count(user_message) or 500
                intent = "plan_referral"
                params["target_messages"] = count
                intent_result["intent"] = intent
                intent_result["params"] = params

            if not intent_result.get("targeting") and multi_local.get("targeting"):
                intent_result["targeting"] = multi_local["targeting"]

            devices = intent_result.get("devices", devices)
            params = intent_result.get("params", params)
            intent = intent_result.get("intent", intent)
            multi = multi_local

        log.info("[Chat] unified execute → intent=%s multi=%s", intent, is_multi)
        return {
            "intent": intent,
            "devices": devices,
            "params": params,
            "intent_result": intent_result,
            "is_multi": is_multi,
            "intents_list": intents_list,
            "multi": multi,
        }

    def handle(self, user_message: str, dry_run: bool = False) -> Dict[str, Any]:
        """
        Process a user message and return structured response.

        dry_run=True：只解析与生成预览，不调用 /tasks。
        """
        t0 = time.time()
        user_message = user_message.strip()

        if not user_message:
            return {"reply": "请输入指令。", "intent": "", "devices": [],
                    "params": {}, "actions_taken": [], "task_ids": [],
                    "elapsed_ms": 0, "dry_run": dry_run}

        from .triage import ChatRoute, triage_message
        from .query_answer import run_query
        from .converse_reply import generate_converse_reply

        parse_mode = "legacy"
        used_unified_execute = False
        u: Optional[Dict[str, Any]] = None
        tr = None

        if _unified_parse_enabled(self._ai):
            u = self._ai.parse_unified(user_message)
            if u is not None:
                parse_mode = "unified"
                rt = u.get("routing")
                if rt == "converse":
                    reply = generate_converse_reply(user_message, self._history)
                    self._append_history(user_message, reply)
                    elapsed = int((time.time() - t0) * 1000)
                    return {
                        "reply": reply,
                        "intent": "converse",
                        "devices": [],
                        "params": {},
                        "targeting": {},
                        "goals": {},
                        "actions_taken": [],
                        "task_ids": [],
                        "elapsed_ms": elapsed,
                        "dry_run": dry_run,
                        "chat_mode": "converse",
                        "parse_mode": "unified",
                        "triage": {"reason": "unified_llm", "llm": True},
                    }
                if rt == "query":
                    qsub = u.get("query_subtype", "general") or "general"
                    reply, logical_intent, actions = run_query(
                        qsub, self._executor, user_message)
                    self._append_history(user_message, reply)
                    elapsed = int((time.time() - t0) * 1000)
                    return {
                        "reply": reply,
                        "intent": logical_intent,
                        "devices": [],
                        "params": {},
                        "targeting": {},
                        "goals": {},
                        "actions_taken": actions,
                        "task_ids": [],
                        "elapsed_ms": elapsed,
                        "dry_run": dry_run,
                        "chat_mode": "query",
                        "query_subtype": qsub,
                        "parse_mode": "unified",
                        "triage": {"reason": "unified_llm"},
                    }
                if rt == "ambiguous":
                    reply = (
                        "我不太确定你是想**查数据**、**闲聊**，还是要**下任务**。\n"
                        "可以试：「哪些手机在线」「在吗」「01号养号30分钟」分别对应查询 / 闲聊 / 执行。"
                    )
                    self._append_history(user_message, reply)
                    elapsed = int((time.time() - t0) * 1000)
                    return {
                        "reply": reply,
                        "intent": "ambiguous",
                        "devices": [],
                        "params": {},
                        "targeting": {},
                        "goals": {},
                        "actions_taken": [],
                        "task_ids": [],
                        "elapsed_ms": elapsed,
                        "dry_run": dry_run,
                        "chat_mode": "ambiguous",
                        "parse_mode": "unified",
                        "triage": {"reason": "unified_llm", "llm": True},
                    }
                if rt == "execute":
                    st = self._build_execute_state_unified(u, user_message)
                    intent = st["intent"]
                    devices = st["devices"]
                    params = st["params"]
                    intent_result = st["intent_result"]
                    is_multi = st["is_multi"]
                    intents_list = st["intents_list"]
                    multi = st["multi"]
                    used_unified_execute = True
                    action_results = []

        if not used_unified_execute:
            tr = triage_message(user_message)

            if tr.route == ChatRoute.CONVERSE:
                reply = generate_converse_reply(user_message, self._history)
                self._append_history(user_message, reply)
                elapsed = int((time.time() - t0) * 1000)
                return {
                    "reply": reply,
                    "intent": "converse",
                    "devices": [],
                    "params": {},
                    "targeting": {},
                    "goals": {},
                    "actions_taken": [],
                    "task_ids": [],
                    "elapsed_ms": elapsed,
                    "dry_run": dry_run,
                    "chat_mode": "converse",
                    "parse_mode": "legacy",
                    "triage": {"reason": tr.reason, "llm": tr.llm_used},
                }

            if tr.route == ChatRoute.QUERY:
                reply, logical_intent, actions = run_query(
                    tr.query_subtype, self._executor, user_message)
                self._append_history(user_message, reply)
                elapsed = int((time.time() - t0) * 1000)
                return {
                    "reply": reply,
                    "intent": logical_intent,
                    "devices": [],
                    "params": {},
                    "targeting": {},
                    "goals": {},
                    "actions_taken": actions,
                    "task_ids": [],
                    "elapsed_ms": elapsed,
                    "dry_run": dry_run,
                    "chat_mode": "query",
                    "query_subtype": tr.query_subtype,
                    "parse_mode": "legacy",
                    "triage": {"reason": tr.reason},
                }

            if tr.route == ChatRoute.AMBIGUOUS:
                reply = (
                    "我不太确定你是想**查数据**、**闲聊**，还是要**下任务**。\n"
                    "可以试：「哪些手机在线」「在吗」「01号养号30分钟」分别对应查询 / 闲聊 / 执行。"
                )
                self._append_history(user_message, reply)
                elapsed = int((time.time() - t0) * 1000)
                return {
                    "reply": reply,
                    "intent": "ambiguous",
                    "devices": [],
                    "params": {},
                    "targeting": {},
                    "goals": {},
                    "actions_taken": [],
                    "task_ids": [],
                    "elapsed_ms": elapsed,
                    "dry_run": dry_run,
                    "chat_mode": "ambiguous",
                    "parse_mode": "legacy",
                    "triage": {"reason": tr.reason, "llm": tr.llm_used},
                }

            intent = ""
            devices = []
            params = {}
            intent_result = {}
            action_results = []
            is_multi = False
            intents_list = []

            multi = self._ai._multi_intent_parse(user_message)
            if multi.get("intent") == "multi_task" and multi.get("intents"):
                is_multi = True
                intent = "multi_task"
                devices = multi.get("devices", [])
                params = multi.get("params", {})
                intents_list = multi["intents"]
                intent_result = multi
                log.info("[Chat] Multi-intent: %d tasks | Devices: %s", len(intents_list), devices)
            else:
                intent_result = self._ai.parse_intent(user_message)
                intent = intent_result.get("intent", "help")
                devices = intent_result.get("devices", [])
                params = intent_result.get("params", {})

                local_intent = multi.get("intent", "")
                _ml = user_message.lower()
                if intent in ("warmup", "help", "") and local_intent not in (
                        "", "multi_task", "warmup", "help"):
                    log.info("[Chat] ★ 本地解析覆盖 AI 解析: %s → %s", intent, local_intent)
                    intent = local_intent
                    intent_result = multi
                    params = multi.get("params", {})
                elif intent == "warmup" and any(w in _ml for w in
                                                ["直播", "live", "直播间", "进直播", "评论区", "comment_engage"]):
                    override = "live_engage" if any(w in _ml for w in ["直播", "live", "直播间", "进直播"]) \
                               else "comment_engage"
                    log.info("[Chat] ★ 关键词强制覆盖: warmup → %s（用户消息含直播/评论区词）", override)
                    intent = override
                    params.setdefault("target_country", multi.get("params", {}).get("target_country") or "italy")
                    params.setdefault("max_live_rooms", 3)
                    intent_result["intent"] = intent
                    intent_result["params"] = params

                if intent_result.get("intent") == "plan_referral" or \
                   (intent == "warmup" and "引流" in user_message and self._ai._extract_count(user_message) >= 100):
                    count = self._ai._extract_count(user_message) or 500
                    intent = "plan_referral"
                    params["target_messages"] = count

                log.info("[Chat] Intent: %s | Devices: %s | Params: %s",
                         intent, devices, params)

                if not intent_result.get("targeting") and multi.get("targeting"):
                    intent_result["targeting"] = multi["targeting"]
                    log.info("[Chat] ★ targeting 从本地解析补充: %s", multi["targeting"])

        if not dry_run and intent != "help":
            block_msg = self._preflight_block_message(devices, params=params)
            if block_msg:
                self._append_history(user_message, block_msg)
                elapsed = int((time.time() - t0) * 1000)
                return {
                    "reply": block_msg,
                    "intent": intent,
                    "devices": devices,
                    "params": params,
                    "actions_taken": [],
                    "task_ids": [],
                    "elapsed_ms": elapsed,
                    "dry_run": dry_run,
                    "preflight_blocked": True,
                    "chat_mode": "execute",
                    "parse_mode": parse_mode,
                    "triage": {
                        "reason": "unified_single_llm" if used_unified_execute else (tr.reason if tr else ""),
                    },
                }

        # 提取 targeting / goals（新增字段，向后兼容）
        targeting = intent_result.get("targeting", {}) if not is_multi else multi.get("targeting", {})
        goals = intent_result.get("goals", {}) if not is_multi else multi.get("goals", {})

        if not dry_run:
            if is_multi:
                for sub in intents_list:
                    sub_action = sub["action"]
                    sub_params = {**params, **sub.get("params", {})}
                    results = self._executor.execute(
                        sub_action, devices, sub_params,
                        targeting=targeting, goals=goals)
                    action_results.extend(results)
            else:
                action_results = self._executor.execute(
                    intent, devices, params,
                    targeting=targeting, goals=goals)

        if dry_run:
            reply = self._build_dry_run_reply(
                intent, devices, params, intent_result, is_multi, len(intents_list),
            )
            pending_plan = {
                "intent": intent,
                "devices": devices,
                "params": params,
                "targeting": targeting,
                "goals": goals,
                "user_message": user_message,
                "multi": is_multi,
                "intents": intents_list if is_multi else [],
                "intent_result": intent_result,
            }
            if intent == "help":
                pending_plan = None
            task_ids: List[str] = []
        else:
            reply = self._ai.generate_reply(
                intent_result if not is_multi else multi,
                action_results,
                user_message,
            )
            task_ids = _collect_task_ids(action_results)
            pending_plan = None

        self._append_history(user_message, reply)

        elapsed = int((time.time() - t0) * 1000)

        tri_reason = (
            "unified_single_llm" if used_unified_execute else (tr.reason if tr else "")
        )
        tri_llm = used_unified_execute or (getattr(tr, "llm_used", False) if tr else False)
        out: Dict[str, Any] = {
            "reply": reply,
            "intent": intent,
            "devices": devices,
            "params": params,
            "targeting": targeting,
            "goals": goals,
            "actions_taken": action_results,
            "task_ids": task_ids,
            "elapsed_ms": elapsed,
            "dry_run": dry_run,
            "chat_mode": "execute",
            "parse_mode": parse_mode,
            "triage": {"reason": tri_reason, "llm": tri_llm},
        }
        if dry_run and pending_plan is not None:
            out["pending_plan"] = pending_plan
            out["pending_confirmation"] = True
        return out

    @property
    def history(self) -> List[Dict[str, str]]:
        return list(self._history)

    def clear(self):
        self._history.clear()
        self._ai.clear_history()
