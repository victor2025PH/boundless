# -*- coding: utf-8 -*-
"""AI 功能路由 — LLM、意图分类、自然语言控制、聊天。"""
import re
import logging

from fastapi import APIRouter, HTTPException

from src.host.device_registry import DEFAULT_DEVICES_YAML
from src.host.task_ui_enrich import enrich_chat_response

logger = logging.getLogger(__name__)

router = APIRouter(tags=["AI"])

# ── 意图映射表 ──

_AI_INTENT_MAP = {
    "打开tiktok": "am start -n com.zhiliaoapp.musically/.app.core.activity.MainTabActivity",
    "打开telegram": "am start -n org.telegram.messenger/.DefaultIcon",
    "打开whatsapp": "am start -n com.whatsapp/.Main",
    "打开facebook": "am start -n com.facebook.katana/.LoginActivity",
    "打开instagram": "am start -n com.instagram.android/.activity.MainTabActivity",
    "打开youtube": "am start -n com.google.android.youtube/.HomeActivity",
    "打开浏览器": "am start -a android.intent.action.VIEW -d https://www.google.com",
    "打开设置": "am start -n com.android.settings/.Settings",
    "打开相机": "am start -a android.media.action.IMAGE_CAPTURE",
    "返回桌面": "input keyevent KEYCODE_HOME",
    "返回": "input keyevent KEYCODE_BACK",
    "截屏": "screencap -p /sdcard/ai_screenshot.png",
    "清除tiktok缓存": "pm clear com.zhiliaoapp.musically",
    "清除telegram缓存": "pm clear org.telegram.messenger",
    "清除whatsapp缓存": "pm clear com.whatsapp",
    "上滑": "input swipe 540 1600 540 400 300",
    "下滑": "input swipe 540 400 540 1600 300",
    "左滑": "input swipe 900 100 100 800 300",
    "右滑": "input swipe 100 800 900 800 300",
    "锁屏": "input keyevent KEYCODE_POWER",
    "音量加": "input keyevent KEYCODE_VOLUME_UP",
    "音量减": "input keyevent KEYCODE_VOLUME_DOWN",
    "静音": "input keyevent KEYCODE_VOLUME_MUTE",
    "重启": "reboot",
    "飞行模式": "cmd connectivity airplane-mode enable",
    "关闭飞行模式": "cmd connectivity airplane-mode disable",
    "wifi开": "svc wifi enable",
    "wifi关": "svc wifi disable",
    "查看电量": "dumpsys battery | grep level",
    "查看ip": "ip addr show wlan0 | grep inet",
    "查看存储": "df -h /sdcard",
    "刷5分钟视频": "LOOP",
    "刷10分钟视频": "LOOP",
}

_AI_TASK_INTENTS = {
    "养号": "warmup",
    "刷视频": "watch_videos",
    "关注": "follow_users",
    "自动回复": "auto_reply",
    "全流程": "tiktok_acquisition",
}


def _resolve_target_devices(instruction: str, explicit_device_id: str = "") -> tuple:
    """Parse instruction to determine target devices: single, group, or all."""
    from src.device_control.device_manager import get_device_manager

    manager = get_device_manager(DEFAULT_DEVICES_YAML)
    lower = instruction.lower()

    group_match = re.search(r'第?(\d+)组|分组\s*["\']?(\w+)', instruction)
    if group_match:
        group_ref = group_match.group(1) or group_match.group(2)
        try:
            from ..database import get_conn
            with get_conn() as conn:
                groups = conn.execute("SELECT group_id, name FROM device_groups").fetchall()
                target_group = None
                for g in groups:
                    if group_ref.isdigit():
                        idx = int(group_ref) - 1
                        if groups.index(g) == idx:
                            target_group = g
                            break
                    elif group_ref in g["name"]:
                        target_group = g
                        break
                if target_group:
                    members = conn.execute(
                        "SELECT device_id FROM device_group_members WHERE group_id=?",
                        (target_group["group_id"],)).fetchall()
                    return [m["device_id"] for m in members], f"分组[{target_group['name']}]"
        except Exception:
            pass

    if "全部" in lower or "所有" in lower or "all" in lower:
        devices = manager.get_all_devices()
        online = [d.device_id for d in devices if d.is_online]
        return online, "全部在线设备"

    device_match = re.search(r'第?(\d+)号|编号\s*(\d+)', instruction)
    if device_match:
        num = int(device_match.group(1) or device_match.group(2))
        devices = manager.get_all_devices()
        online = [d for d in devices if d.is_online]
        if 0 < num <= len(online):
            return [online[num - 1].device_id], f"设备#{num}"

    if explicit_device_id:
        return [explicit_device_id], "指定设备"
    return [], "未指定"


# ── 端点 ──

@router.get("/ai/stats")
def ai_stats():
    """LLM usage stats + rewriter pool status + vision budget."""
    result = {}
    try:
        from src.ai.llm_client import get_llm_client
        client = get_llm_client()
        snap = client.stats.snapshot()
        snap["circuit_breaker"] = client.cb_status()
        result["llm"] = snap
        # 2026-05-13: 暴露 fallback provider 的独立统计（含 p95 + CB 状态）
        fb = client._get_fallback_client()
        if fb is not None:
            fb_snap = fb.stats.snapshot()
            fb_snap["provider"] = fb.config.provider
            fb_snap["circuit_breaker"] = fb.cb_status()
            result["llm_fallback"] = fb_snap
        # 双路 CB 开路告警
        primary_open = result["llm"]["circuit_breaker"].get("state") == "open"
        fallback_open = (
            fb is not None
            and result.get("llm_fallback", {}).get("circuit_breaker", {}).get("state") == "open"
        )
        result["llm_available"] = not (primary_open and (fb is None or fallback_open))
    except Exception:
        result["llm"] = {"status": "not_initialized"}

    try:
        from src.ai.message_rewriter import get_rewriter
        rw = get_rewriter()
        result["rewriter"] = {"pool": rw.pool_status()}
    except Exception:
        result["rewriter"] = {"status": "not_initialized"}

    try:
        from src.ai.vision_fallback import VisionFallback
        vf = VisionFallback()
        result["vision"] = vf.stats()
    except Exception:
        result["vision"] = {"status": "not_initialized"}

    return result


@router.get("/ai/vision/health")
def ai_vision_health():
    """Vision engine health check — backend availability, selector cache health."""
    result = {"status": "ok", "backends": {}, "selectors": {}}

    # Check LLM vision backend
    try:
        from src.ai.llm_client import get_llm_client
        client = get_llm_client()
        result["backends"]["llm_vision"] = {
            "available": True,
            "provider": getattr(client, '_provider', 'unknown'),
            "vision_model": getattr(client, '_vision_model', 'unknown'),
        }
    except Exception as e:
        result["backends"]["llm_vision"] = {"available": False, "error": str(e)}

    # Check OmniParser backend
    try:
        from src.vision.backends import OmniParserBackend
        omni = OmniParserBackend()
        available = omni._is_available()
        result["backends"]["omniparser"] = {"available": available}
    except Exception as e:
        result["backends"]["omniparser"] = {"available": False, "error": str(e)}

    # Check vision budget
    try:
        from src.ai.vision_fallback import VisionFallback
        vf = VisionFallback()
        result["vision_budget"] = vf.stats()
    except Exception:
        result["vision_budget"] = {"status": "not_initialized"}

    # Selector cache health
    try:
        from src.vision.auto_selector import SelectorStore
        store = SelectorStore()
        result["selectors"] = store.health_report()
    except Exception as e:
        result["selectors"] = {"status": "error", "error": str(e)}

    # Overall status
    has_any_backend = any(
        b.get("available") for b in result["backends"].values()
    )
    result["status"] = "ok" if has_any_backend else "degraded"
    return result


@router.post("/ai/rewrite")
def ai_rewrite(body: dict):
    """Rewrite a message template for uniqueness."""
    template = body.get("template", "")
    platform = body.get("platform", "telegram")
    context = body.get("context", {})
    if not template:
        raise HTTPException(status_code=400, detail="template required")
    try:
        from src.ai.message_rewriter import get_rewriter
        rw = get_rewriter()
        result = rw.rewrite(template, context, platform)
        return {"original": template, "rewritten": result, "platform": platform}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ai/pregenerate")
def ai_pregenerate(body: dict):
    """Pre-generate message variants for a template."""
    template = body.get("template", "")
    platform = body.get("platform", "telegram")
    count = body.get("count", 5)
    if not template:
        raise HTTPException(status_code=400, detail="template required")
    try:
        from src.ai.message_rewriter import get_rewriter
        rw = get_rewriter()
        generated = rw.pregenerate(template, count, platform)
        return {"template": template, "variants_generated": generated,
                "pool_status": rw.pool_status()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ai/execute-intent")
def ai_execute_intent(body: dict):
    """Parse natural language and execute on single or multiple devices with group support."""
    from src.device_control.device_manager import get_device_manager
    from ..executor import _get_device_id

    _config_path = DEFAULT_DEVICES_YAML

    def _resolve_device(device_id: str) -> str:
        manager = get_device_manager(_config_path)
        manager.discover_devices()
        resolved = _get_device_id(manager, device_id, _config_path)
        if not resolved:
            raise HTTPException(status_code=404, detail="无可用设备")
        return resolved

    instruction = body.get("instruction", "").strip()
    device_id = body.get("device_id", "")
    if not instruction:
        raise HTTPException(400, "instruction is required")

    lower = instruction.lower()
    target_devices, target_desc = _resolve_target_devices(instruction, device_id)

    manager = get_device_manager(_config_path)

    for task_keyword, task_type in _AI_TASK_INTENTS.items():
        if task_keyword in lower:
            if not target_devices:
                target_devices = [d.device_id for d in manager.get_all_devices() if d.is_online]
                target_desc = "全部在线设备"
            created = 0
            from .. import task_store
            from src.host.task_origin import with_origin
            from ..task_dispatcher import dispatch_after_create

            _params_with_origin = with_origin({}, "ai_quick")
            for did in target_devices:
                try:
                    tid = task_store.create_task(
                        task_type=task_type,
                        device_id=did,
                        params=_params_with_origin,
                    )
                    dispatch_after_create(
                        task_id=tid,
                        device_id=did,
                        task_type=task_type,
                        params=_params_with_origin,
                    )
                    created += 1
                except Exception:
                    pass
            return {
                "reply": f"已为{target_desc}({len(target_devices)}台)创建{task_keyword}任务，共{created}个",
                "commands": [], "instruction": instruction,
                "target_count": len(target_devices),
            }

    matched_cmd = None
    for key, cmd in _AI_INTENT_MAP.items():
        if key in lower:
            matched_cmd = (key, cmd)
            break

    if matched_cmd:
        key, cmd = matched_cmd
        if not target_devices and device_id:
            target_devices = [_resolve_device(device_id)]
            target_desc = "当前设备"
        if not target_devices:
            online = [d for d in manager.get_all_devices() if d.is_online]
            if online:
                target_devices = [online[0].device_id]
                target_desc = "首台在线设备"

        if cmd == "LOOP":
            dur_match = re.search(r'(\d+)\s*分钟', instruction)
            dur = int(dur_match.group(1)) if dur_match else 5
            loops = dur * 6
            cmd = f"for i in $(seq 1 {loops}); do input swipe 540 1600 540 400 300; sleep 10; done"

        results = []
        from concurrent.futures import ThreadPoolExecutor
        def _exec(did):
            try:
                ok, out = manager.execute_adb_command(f"shell {cmd}", did)
                return {"device": did, "success": ok, "output": (out or "")[:300]}
            except Exception as e:
                return {"device": did, "success": False, "output": str(e)}
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(_exec, target_devices))

        ok_count = sum(1 for r in results if r["success"])
        return {
            "reply": f"已在{target_desc}({len(target_devices)}台)执行: {key} — 成功{ok_count}台",
            "commands": results, "instruction": instruction,
            "target_count": len(target_devices),
        }

    search_match = re.match(r".*(?:搜索|search)\s+(.+)", instruction, re.I)
    if search_match:
        query = search_match.group(1).strip()
        app_open = None
        if "tiktok" in lower:
            app_open = "am start -n com.zhiliaoapp.musically/.app.core.activity.MainTabActivity"
        elif "telegram" in lower:
            app_open = "am start -n org.telegram.messenger/.DefaultIcon"
        if not target_devices and device_id:
            target_devices = [_resolve_device(device_id)]
        if target_devices and app_open:
            for did in target_devices[:5]:
                try:
                    manager.execute_adb_command(f"shell {app_open}", did)
                except Exception:
                    pass
        return {"reply": f"已打开应用并准备搜索: {query}", "commands": [], "instruction": instruction}

    try:
        from src.ai.llm_client import get_llm_client
        client = get_llm_client()
        prompt = (
            f"用户想要在Android手机上执行以下操作: '{instruction}'\n"
            "请返回需要执行的ADB shell命令列表，每行一条。\n"
            "只返回命令，不要解释。如果无法转换为ADB命令，回复'无法执行'。"
        )
        llm_reply = client.chat(prompt, max_tokens=200, temperature=0.2) or ""
        if "无法执行" in llm_reply:
            return {"reply": f"AI 无法将指令转换为设备操作: {instruction}",
                    "commands": [], "instruction": instruction}
        cmds = [ln.strip() for ln in llm_reply.strip().split("\n")
                if ln.strip() and not ln.startswith("#")]
        if not target_devices and device_id:
            target_devices = [_resolve_device(device_id)]
        elif not target_devices:
            online = [d for d in manager.get_all_devices() if d.is_online]
            if online:
                target_devices = [online[0].device_id]
        results = []
        for did in target_devices:
            for cmd in cmds[:5]:
                try:
                    ok, out = manager.execute_adb_command(f"shell {cmd}", did)
                    results.append({"device": did, "command": cmd, "output": (out or "")[:300]})
                except Exception as ce:
                    results.append({"device": did, "command": cmd, "output": f"error: {ce}"})
        return {"reply": f"AI 解析并执行了 {len(cmds)} 条命令到 {len(target_devices)} 台设备",
                "commands": results, "instruction": instruction}
    except Exception as e:
        return {"reply": f"AI 处理失败: {e}", "commands": [], "instruction": instruction}


@router.post("/ai/classify_intent")
def ai_classify_intent(body: dict):
    """Classify message intent (needs_reply / optional / no_reply)."""
    message = body.get("message", "")
    if not message:
        raise HTTPException(status_code=400, detail="message required")
    from src.ai.auto_reply import classify_intent
    intent = classify_intent(message)
    return {"message": message, "intent": intent}


@router.post("/ai/test_connection")
def ai_test_connection():
    """Test LLM API connectivity."""
    try:
        from src.ai.llm_client import get_llm_client
        client = get_llm_client()
        ok, msg = client.test_connection()
        return {"connected": ok, "message": msg}
    except Exception as e:
        return {"connected": False, "message": str(e)}


@router.post("/ai/generate-script")
def ai_generate_script(body: dict):
    """Generate ADB script from natural language description."""
    description = body.get("description", "")
    if not description:
        raise HTTPException(400, "description required")
    prompt_template = f"""你是一个Android ADB命令专家。根据用户的描述生成可执行的ADB shell命令脚本。
每行一条命令，只输出命令，不要解释。命令不需要adb shell前缀。

用户需求: {description}

生成脚本:"""
    try:
        from src.ai.ai_client import get_llm_client
        client = get_llm_client()
        result = client.chat([{"role": "user", "content": prompt_template}])
        script = result.strip()
        lines = [ln.strip() for ln in script.splitlines()
                 if ln.strip() and not ln.strip().startswith("#") and not ln.strip().startswith("```")]
        return {"ok": True, "script": "\n".join(lines), "description": description}
    except Exception as e:
        common_scripts = {
            "清理": "pm list packages -3\npm clear com.zhiliaoapp.musically",
            "截图": "screencap -p /sdcard/screenshot.png",
            "查看电池": "dumpsys battery",
            "查看内存": "cat /proc/meminfo",
            "查看存储": "df -h",
            "查看wifi": "dumpsys wifi | grep 'mWifiInfo'",
            "重启": "reboot",
            "查看进程": "ps -A | head -20",
        }
        for key, cmd in common_scripts.items():
            if key in description:
                return {"ok": True, "script": cmd, "description": description, "fallback": True}
        return {"ok": False, "error": str(e), "script": f"# AI不可用，请手动编写\n# 需求: {description}"}


@router.post("/ai/classify_lead_intent")
def classify_lead_intent(body: dict):
    """Classify a received message and update lead pipeline."""
    message = body.get("message", "")
    if not message:
        raise HTTPException(status_code=400, detail="message required")
    from src.ai.intent_classifier import get_intent_classifier
    lead_id = body.get("lead_id", 0)
    if lead_id:
        result = get_intent_classifier().classify_and_act(
            message, lead_id, body.get("platform", ""),
            body.get("context"),
        )
    else:
        result = get_intent_classifier().classify(message, body.get("context"))
    return result.to_dict()


@router.post("/ai/quick-command")
def ai_quick_command(body: dict):
    """解析口语化指令并创建 TikTok 任务。

    Body: {"command": "所有手机养号30分钟", "platform": "tiktok"}

    支持的指令格式:
      "所有手机养号" / "1-5号手机刷视频" / "全部关注意大利用户"
      "检查收件箱" / "全流程获客" / "连接VPN"
    """
    command = body.get("command", "").strip()
    if not command:
        raise HTTPException(400, "请输入指令")

    result = _parse_quick_command(command)
    if not result:
        return {"ok": False, "message": f"未识别的指令: {command}",
                "hint": "支持: 养号/刷视频/关注/发私信/查收件箱/全流程获客/连接VPN"}

    task_type = result["task_type"]
    params = result["params"]
    device_ids = result.get("device_ids", [])

    # 前端显式指定设备（如单台「本机收件箱」）；非空列表时覆盖解析结果
    req_ids = body.get("device_ids")
    if isinstance(req_ids, list) and len(req_ids) > 0:
        device_ids = [str(x).strip() for x in req_ids if x and str(x).strip()]

    # 取消全部任务的特殊处理
    if task_type == "_cancel_all":
        from .. import task_store
        from ..api import get_worker_pool
        tasks = task_store.list_tasks(status="running") + task_store.list_tasks(status="pending")
        pool = get_worker_pool()
        cancelled = 0
        for t in tasks:
            tid = t.get("task_id", "")
            pool.cancel_task(tid)
            task_store.set_task_cancelled(tid)
            cancelled += 1
        return {"ok": True, "command": command, "task_type": "_cancel_all",
                "devices": 0, "created": 0,
                "message": f"已取消 {cancelled} 个任务"}

    # 获取在线设备
    if not device_ids:
        from src.device_control.device_manager import get_device_manager
        config_path = DEFAULT_DEVICES_YAML
        mgr = get_device_manager(config_path)
        mgr.discover_devices()
        device_ids = [d.device_id for d in mgr.get_all_devices() if d.is_online]
        # 也包括集群设备
        try:
            from src.host.multi_host import get_cluster_coordinator
            coord = get_cluster_coordinator()
            if coord:
                cluster_devs = coord.get_all_devices()
                local_ids = set(device_ids)
                for d in cluster_devs:
                    did = d.get("device_id", "")
                    if did and did not in local_ids and d.get("status") == "connected":
                        device_ids.append(did)
        except Exception:
            pass

    if not device_ids:
        return {"ok": False, "message": "没有在线设备"}

    # 批量创建任务
    created = 0
    from .. import task_store
    from src.host.task_origin import with_origin
    from ..task_dispatcher import dispatch_after_create

    _p = with_origin(params, "ai_quick")
    for did in device_ids:
        try:
            tid = task_store.create_task(task_type=task_type, device_id=did, params=_p)
            dispatch_after_create(
                task_id=tid,
                device_id=did,
                task_type=task_type,
                params=_p,
            )
            created += 1
        except Exception:
            pass

    from src.host.task_labels_zh import task_label_zh
    return {
        "ok": True,
        "command": command,
        "task_type": task_type,
        "devices": len(device_ids),
        "created": created,
        "message": f"已为 {created} 台设备创建 {task_label_zh(task_type)} 任务",
    }


def _parse_quick_command(cmd: str) -> dict:
    """解析口语化指令为任务参数。"""
    cmd_lower = cmd.lower()

    # 解析设备范围 (暂留空，让调用方用全部设备)
    device_ids = []

    # 解析时长
    duration = None
    dur_match = re.search(r'(\d+)\s*分钟', cmd)
    if dur_match:
        duration = int(dur_match.group(1))

    # 解析目标国家
    country = "italy"  # 默认
    if "意大利" in cmd or "italy" in cmd_lower:
        country = "italy"
    elif "美国" in cmd or "usa" in cmd_lower:
        country = "usa"
    elif "德国" in cmd or "germany" in cmd_lower:
        country = "germany"
    elif "法国" in cmd or "france" in cmd_lower:
        country = "france"

    # 匹配意图
    if "全流程" in cmd or "获客" in cmd or "自动" in cmd:
        return {"task_type": "tiktok_auto", "params": {"target_country": country}, "device_ids": device_ids}
    if "养号" in cmd:
        p = {"duration_minutes": duration or 30, "target_country": country}
        return {"task_type": "tiktok_warmup", "params": p, "device_ids": device_ids}
    if "刷视频" in cmd or "浏览" in cmd:
        p = {"duration_minutes": duration or 15}
        return {"task_type": "tiktok_browse_feed", "params": p, "device_ids": device_ids}
    if "关注" in cmd:
        max_f = 20
        f_match = re.search(r'(\d+)\s*人', cmd)
        if f_match:
            max_f = int(f_match.group(1))
        return {"task_type": "tiktok_follow", "params": {"max_follows": max_f, "target_country": country}, "device_ids": device_ids}
    if "私信" in cmd or "dm" in cmd_lower:
        return {"task_type": "tiktok_send_dm", "params": {}, "device_ids": device_ids}
    if "收件箱" in cmd or "inbox" in cmd_lower:
        return {"task_type": "tiktok_check_inbox", "params": {"auto_reply": True, "max_conversations": 20}, "device_ids": device_ids}
    if "vpn" in cmd_lower:
        return {"task_type": "vpn_setup", "params": {}, "device_ids": device_ids}
    if "停止" in cmd or "暂停" in cmd or "取消" in cmd:
        return {"task_type": "_cancel_all", "params": {}, "device_ids": device_ids}

    return None


@router.post("/ai/fb/suggest-line")
def ai_fb_suggest_line(body: dict):
    """为 Facebook 好友请求/打招呼生成个性化文案（3 条候选）。

    Body: {"field": "verification_note"|"greeting", "persona_key": "...", "language": "ja"}
    """
    field = body.get("field", "verification_note")
    persona_key = body.get("persona_key", "jp_female_midlife")
    language = body.get("language", "ja")
    lang_names = {"ja": "日本語", "zh": "中文", "en": "English", "hi": "हिन्दी", "id": "Bahasa Indonesia"}
    lang_name = lang_names.get(language, language)

    # ── 从话术库取历史最佳（reply_rate > 0 且 sent_count >= 3）
    best_from_db = []      # [{"text": ..., "reply_rate": ...}]
    best_texts_set = set()
    try:
        from src.host.fb_targets_store import get_greeting_stats
        stats = get_greeting_stats(persona_key)
        for g in stats.get("best_greetings", []):
            if g.get("reply_rate", 0) > 0.1 and len(best_from_db) < 2:
                best_from_db.append({"text": g["text_ja"], "reply_rate": round(g["reply_rate"], 3)})
                best_texts_set.add(g["text_ja"])
    except Exception:
        pass

    if field == "verification_note":
        desc = "Facebook好友请求的验证语（60字以内）"
        style = "看到同群/同兴趣、礼貌温暖、带 emoji、适合中年女性社交圈"
    else:
        desc = "好友通过后首条打招呼DM（200字以内）"
        style = "感谢通过、期待交流、真诚自然、适合中年女性社交圈"

    _fallback_vn = {
        "ja": ["您好🌸看到我们都在同一个群，想认识下志同道合的朋友 ☺️",
               "はじめまして🌸同じグループで拝見しました。仲良くしていただけたら嬉しいです",
               "同じ趣味の方とつながりたく、フォロー失礼します😊"],
        "zh": ["您好，看到我们都在同一个群组，想认识一下 🌸",
               "你好呀，刚刚在群里看到你的留言，希望能交个朋友 ☺️",
               "同好相聚，希望能多多交流"],
        "en": ["Hello! Saw we are in the same group — would love to connect 🌸",
               "Hi there, I noticed we share similar interests. Nice to meet you!",
               "Namaste 🙏 saw your profile in our group, would be great to connect."],
    }
    _fallback_gr = {
        "ja": ["ご通過ありがとうございます😊これからよろしくお願いいたします🌸",
               "こんにちは🌸お友達になっていただけて嬉しいです。よろしくお願いします",
               "はじめまして！同じ趣味でつながれて嬉しいです、よろしくお願いします😊"],
        "zh": ["谢谢通过！很高兴认识你 🌸",
               "你好呀～我也是在群里看到你的，期待多多交流",
               "感谢通过好友请求，希望我们可以多多交流分享"],
        "en": ["Thanks for accepting! Looking forward to chatting 🌸",
               "Hi! So glad we connected — what brought you to the group?",
               "Hello there! Would love to know more about your interests 😊"],
    }
    fb_map = _fallback_vn if field == "verification_note" else _fallback_gr
    fallback = fb_map.get(language[:2], fb_map.get("ja", ["（暂无模板）"]))

    try:
        from src.ai.llm_client import get_llm_client
        client = get_llm_client()
        prompt = (
            f"你是 Facebook 社交运营专家。客群画像: {persona_key}。\n"
            f"任务: 生成3条{desc}。\n"
            f"语言: {lang_name}\n"
            f"风格: {style}\n"
            f"要求: 每行一条，不要编号，不要解释，直接输出可发送内容。"
        )
        resp = client.chat([{"role": "user", "content": prompt}])
        lines = [l.strip() for l in resp.strip().splitlines()
                 if l.strip() and len(l.strip()) > 5
                 and not l.strip().startswith('#')
                 and not l.strip().startswith('```')]
        suggestions = lines[:3]
        while len(suggestions) < 3:
            suggestions.append(fallback[len(suggestions)] if len(suggestions) < len(fallback) else fallback[0])
        # 混入历史最佳（去重后置顶），返回结构化数据
        result_items = []
        for b in best_from_db:
            if b["text"] not in set(suggestions):
                result_items.append({"text": b["text"], "proven": True, "reply_rate": b["reply_rate"]})
        for s in suggestions:
            result_items.append({"text": s, "proven": s in best_texts_set, "reply_rate": None})
        # 同时保持 suggestions 字段向后兼容
        flat = [i["text"] for i in result_items[:5]]
        return {"ok": True, "suggestions": flat, "items": result_items[:5], "field": field, "persona_key": persona_key}
    except Exception as e:
        logger.warning("fb suggest-line LLM error: %s", e)
        merged = []
        for b in best_from_db:
            merged.append({"text": b["text"], "proven": True, "reply_rate": b["reply_rate"]})
        for f in fallback:
            merged.append({"text": f, "proven": False, "reply_rate": None})
        merged = merged[:5]
        flat = [i["text"] for i in merged]
        return {"ok": True, "suggestions": flat, "items": merged, "field": field, "persona_key": persona_key, "fallback": True}


@router.post("/ai/suggest-reply")
def ai_suggest_reply(body: dict):
    """Generate 3 AI reply suggestions to convert a TikTok lead."""
    message = body.get("message", "")
    if not message:
        raise HTTPException(400, "message required")
    contact = body.get("contact", "用户")
    context = body.get("context", "TikTok私信营销，目标是引导用户了解产品并促进成交。")
    _fallback = [
        "你好，感谢你的消息！很高兴认识你，我们可以聊聊吗？",
        "您好！我来为您详细介绍一下，有什么我能帮到您的？",
        "亲，现在有优惠活动，心动不如行动，加我详聊～"
    ]
    try:
        from src.ai.llm_client import get_llm_client
        client = get_llm_client()
        prompt = f"""你是TikTok私信销售专家，帮助运营人员快速回复客户消息以促进成交。

客户消息: {message}
营销背景: {context}

请生成3条不同风格的回复，要求:
- 每条回复20-60字，自然口语化，适合TikTok私信风格
- 三条分别对应: 1.热情拉近距离型 2.专业顾问型 3.促单型
- 每行一条，不要编号，不要任何解释，直接输出可发送内容

回复:"""
        resp = client.chat([{"role": "user", "content": prompt}])
        lines = [l.strip() for l in resp.strip().splitlines()
                 if l.strip() and len(l.strip()) > 5 and not l.strip().startswith('#') and not l.strip().startswith('```')]
        suggestions = lines[:3]
        while len(suggestions) < 3:
            suggestions.append(_fallback[len(suggestions)])
        return {"ok": True, "suggestions": suggestions, "contact": contact}
    except Exception as e:
        logger.warning("suggest-reply LLM error: %s", e)
        return {"ok": True, "suggestions": _fallback, "contact": contact, "fallback": True}


@router.post("/chat")
def chat_message(body: dict):
    """
    Natural language chat control.
    Body:
      {"message": "01号手机养号30分钟"}
      {"message": "...", "session_id": "uuid", "dry_run": true}  — 预览不建任务
      {"confirm": true, "session_id": "uuid"}  — 执行上一轮 dry_run 的 pending_plan
    Returns: 含 session_id；dry_run 时含 pending_plan / pending_confirmation
    """
    from src.host.chat_sessions import get_session_store

    confirm = bool(body.get("confirm", False))
    dry_run = bool(body.get("dry_run", False))
    message = (body.get("message") or "").strip()
    session_in = (body.get("session_id") or "").strip()

    store = get_session_store()

    if confirm:
        if not session_in:
            raise HTTPException(status_code=400, detail="confirm 需要有效的 session_id")
        ctrl = store.touch_controller(session_in)
        if ctrl is None:
            return {
                "reply": "会话不存在或已过期，请重新发送指令并获取新的 session_id。",
                "session_id": session_in,
                "intent": "",
                "devices": [],
                "params": {},
                "actions_taken": [],
                "task_ids": [],
                "elapsed_ms": 0,
                "confirmed": False,
            }
        plan = store.pop_pending_plan(session_in)
        if not plan:
            return {
                "reply": "没有待确认的操作。请先使用 dry_run:true 发送预览请求。",
                "session_id": session_in,
                "intent": "",
                "devices": [],
                "params": {},
                "actions_taken": [],
                "task_ids": [],
                "elapsed_ms": 0,
                "confirmed": False,
            }
        result = ctrl.execute_pending_plan(plan)
        result["session_id"] = session_in
        return enrich_chat_response(result)

    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    session_id, ctrl = store.get_or_create(session_in or None)
    result = ctrl.handle(message, dry_run=dry_run)
    result["session_id"] = session_id
    if dry_run and result.get("pending_plan"):
        store.set_pending_plan(session_id, result["pending_plan"])
    return enrich_chat_response(result)


@router.delete("/chat/session/{session_id}")
def delete_chat_session(session_id: str):
    """清除服务端多轮会话状态（待确认计划一并丢弃）。"""
    from src.host.chat_sessions import get_session_store

    ok = get_session_store().clear_session(session_id)
    return {"ok": ok, "session_id": session_id}
