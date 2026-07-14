# -*- coding: utf-8 -*-
"""
代理账号自动轮换模块。

解决的问题:
  当前熔断触发后只能停止任务 + 告警，需要人工干预替换代理账号。
  本模块实现：检测到 IP 泄漏时自动从代理池选取备用账号，重新部署
  Clash 配置，恢复正常流量，最小化人工介入。

设计原则:
  - 只在 state='leak'（IP不匹配）时轮换，'no_ip' 是设备问题不是代理问题
  - 轮换前做 TCP 连通性预检（避免换了一个同样不可用的）
  - 速率限制：同一路由器 30 分钟内最多轮换 1 次（防止无限循环）
  - 黑名单机制：失败的代理账号记录，轮换时不重复使用
  - 健康评分：优先选用历史成功率高的备用代理
  - 轮换历史持久化（JSON），支持运维回溯

轮换流程:
  1. 检查速率限制
  2. 获取当前路由器的代理账号（作为"失败账号"加入黑名单）
  3. 从代理池中选未用过、未黑名单、连通性测试通过的账号
  4. 按健康评分排序，选最优
  5. 更新路由器代理分配
  6. 生成并推送新 Clash 配置
  7. 等待 30s 后验证新出口 IP
  8. 验证成功→恢复任务，失败→升级告警

路由器的 proxy_rotator.json:
  {
    "router-01": {
      "last_rotation": 1713000000.0,
      "rotation_count": 3,
      "blacklist": ["proxy_abc", "proxy_def"],
      "history": [
        {"ts": ..., "from": ["proxy_abc"], "to": ["proxy_xyz"],
         "reason": "IP泄漏", "success": true, "exit_ip": "1.2.3.4"}
      ]
    }
  }
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.host.device_registry import config_file

log = logging.getLogger(__name__)

# 轮换速率限制（同一路由器30分钟内最多1次）
ROTATION_RATE_LIMIT = 1800  # 秒

# 最多尝试几个备用代理
MAX_CANDIDATES = 5

# 状态文件
_STATE_FILE = config_file("proxy_rotator.json")
_state_lock = threading.Lock()


# ═══════════════════════════════════════════════
# 持久化状态管理
# ═══════════════════════════════════════════════

def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state: dict):
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                           encoding="utf-8")


def _get_router_state(router_id: str) -> dict:
    with _state_lock:
        state = _load_state()
        return state.get(router_id, {
            "last_rotation": 0.0,
            "rotation_count": 0,
            "blacklist": [],
            "history": [],
        })


def _update_router_state(router_id: str, updates: dict):
    with _state_lock:
        state = _load_state()
        if router_id not in state:
            state[router_id] = {
                "last_rotation": 0.0,
                "rotation_count": 0,
                "blacklist": [],
                "history": [],
            }
        state[router_id].update(updates)
        _save_state(state)


# ═══════════════════════════════════════════════
# 代理健康评分
# ═══════════════════════════════════════════════

_SCORES_FILE = config_file("proxy_scores.json")
_scores_lock = threading.Lock()

# ═══════════════════════════════════════════════
# Phase 8 P2: 跨路由器代理亲和力评分
# ═══════════════════════════════════════════════

# 亲和力文件：记录每个 (路由器, 代理) 对的历史成功/失败
_AFFINITY_FILE = config_file("proxy_affinity.json")
_affinity_lock = threading.Lock()

# 亲和力权重（综合评分 = 健康评分 × HEALTH_W + 亲和力评分 × AFFINITY_W）
HEALTH_W = 0.6
AFFINITY_W = 0.4


def _load_scores() -> dict:
    if _SCORES_FILE.exists():
        try:
            return json.loads(_SCORES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_scores(scores: dict):
    _SCORES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SCORES_FILE.write_text(json.dumps(scores, indent=2, ensure_ascii=False),
                            encoding="utf-8")


def record_proxy_test(proxy_id: str, success: bool, latency_ms: float = 0):
    """记录代理账号的连通性测试结果（用于健康评分）。"""
    with _scores_lock:
        scores = _load_scores()
        if proxy_id not in scores:
            scores[proxy_id] = {"success": 0, "fail": 0,
                                "total_latency_ms": 0.0, "last_test": 0.0}
        s = scores[proxy_id]
        if success:
            s["success"] += 1
            s["total_latency_ms"] += latency_ms
        else:
            s["fail"] += 1
        s["last_test"] = time.time()
        _save_scores(scores)


def get_proxy_score(proxy_id: str) -> float:
    """获取代理账号的健康评分（0.0-1.0）。

    计算公式:
      score = success_rate × recency_factor
      success_rate = success / (success + fail) 若测试次数>=3, 否则按0.8计（未知=中等）
      recency_factor = 最近测试越新权重越高（超过7天视为陈旧，0.5衰减）
    """
    with _scores_lock:
        scores = _load_scores()
    s = scores.get(proxy_id)
    if not s:
        return 0.75  # 未测试的账号给中等偏高评分（未知但可能正常）

    total = s["success"] + s["fail"]
    if total < 3:
        success_rate = 0.75  # 样本太少，给中等评分
    else:
        success_rate = s["success"] / total

    # 时效性衰减：测试时间越老，权重越低
    age_days = (time.time() - s.get("last_test", 0)) / 86400
    if age_days < 1:
        recency = 1.0
    elif age_days < 3:
        recency = 0.9
    elif age_days < 7:
        recency = 0.75
    else:
        recency = 0.5  # 超过7天未测试，评分衰减

    return round(success_rate * recency, 3)


def _load_affinity() -> dict:
    if _AFFINITY_FILE.exists():
        try:
            return json.loads(_AFFINITY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_affinity(data: dict):
    _AFFINITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _AFFINITY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                              encoding="utf-8")


def get_affinity_score(router_id: str, proxy_id: str) -> float:
    """获取代理对特定路由器的亲和力评分（0.0-1.0）。

    逻辑:
      - 该 (router, proxy) 对没有历史 → 0.5（中性）
      - 只有成功 → 0.9（高置信度）
      - 只有失败 → 0.2（避免重用）
      - 混合历史 → success_rate × 0.8（上限0.85）
    """
    with _affinity_lock:
        data = _load_affinity()
    router_data = data.get(router_id, {})
    entry = router_data.get(proxy_id)
    if not entry:
        return 0.5  # 未知组合，中性评分
    success = entry.get("success", 0)
    fail = entry.get("fail", 0)
    total = success + fail
    if total == 0:
        return 0.5
    if fail == 0:
        return 0.9  # 全部成功，高亲和力
    if success == 0:
        return 0.2  # 全部失败，低亲和力
    return round(min((success / total) * 0.8, 0.85), 3)


def record_affinity(router_id: str, proxy_id: str, success: bool):
    """记录代理在特定路由器上的使用结果。"""
    with _affinity_lock:
        data = _load_affinity()
        if router_id not in data:
            data[router_id] = {}
        if proxy_id not in data[router_id]:
            data[router_id][proxy_id] = {"success": 0, "fail": 0, "last_used": 0.0}
        entry = data[router_id][proxy_id]
        if success:
            entry["success"] += 1
        else:
            entry["fail"] += 1
        entry["last_used"] = time.time()
        _save_affinity(data)


def get_combined_score(router_id: str, proxy_id: str) -> float:
    """获取代理的综合评分（健康评分 × 0.6 + 亲和力评分 × 0.4）。"""
    health = get_proxy_score(proxy_id)
    affinity = get_affinity_score(router_id, proxy_id)
    return round(health * HEALTH_W + affinity * AFFINITY_W, 3)


def get_all_proxy_scores() -> Dict[str, dict]:
    """获取所有代理账号的评分详情（用于前端展示）。"""
    with _scores_lock:
        scores = _load_scores()
    result = {}
    for proxy_id, s in scores.items():
        total = s["success"] + s["fail"]
        result[proxy_id] = {
            "proxy_id": proxy_id,
            "score": get_proxy_score(proxy_id),
            "success": s["success"],
            "fail": s["fail"],
            "total": total,
            "success_rate": round(s["success"] / total, 3) if total else None,
            "avg_latency_ms": round(s["total_latency_ms"] / max(s["success"], 1), 1),
            "last_test": s.get("last_test", 0),
            "last_test_str": time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(s.get("last_test", 0))
            ) if s.get("last_test") else "从未测试",
        }
    return result


# ═══════════════════════════════════════════════
# 轮换核心逻辑
# ═══════════════════════════════════════════════

def rotate_proxy(router_id: str, reason: str = "IP泄漏熔断") -> dict:
    """为指定路由器自动选取并部署备用代理账号。

    Args:
        router_id: 路由器 ID
        reason: 触发轮换的原因（用于记录）

    Returns:
        {
            "ok": bool,
            "router_id": str,
            "reason": str,           # 轮换原因
            "skipped": str,          # 跳过原因（速率限制等）
            "old_proxy_ids": [...],  # 被替换的代理
            "new_proxy_ids": [...],  # 新分配的代理
            "exit_ip": str,          # 轮换后的出口IP
            "exit_verified": bool,
        }
    """
    from src.device_control.router_manager import (
        get_router_manager, _load_pool,
        test_proxy_connection, generate_clash_config, push_clash_config,
        get_router_exit_ip,
    )

    result = {"ok": False, "router_id": router_id, "reason": reason}

    # ── Step 1: 获取路由器信息 ──
    mgr = get_router_manager()
    router = mgr.get_router(router_id)
    if not router:
        result["error"] = f"路由器 {router_id} 不存在"
        return result

    # ── Step 2: 速率限制检查 ──
    router_state = _get_router_state(router_id)
    last_rotation = router_state.get("last_rotation", 0)
    elapsed = time.time() - last_rotation
    if elapsed < ROTATION_RATE_LIMIT:
        remaining = int(ROTATION_RATE_LIMIT - elapsed)
        result["skipped"] = f"速率限制：上次轮换距今 {int(elapsed)}s，需等待 {remaining}s"
        log.info("[Rotator] %s %s", router_id, result["skipped"])
        return result

    old_proxy_ids = list(router.proxy_ids)
    result["old_proxy_ids"] = old_proxy_ids

    # ── Step 3: 获取黑名单 ──
    blacklist = set(router_state.get("blacklist", []))
    # 将当前失败的代理加入黑名单
    for pid in old_proxy_ids:
        blacklist.add(pid)

    # ── Step 4: 从代理池找候选账号（vpn_pool + proxy_pool_manager 双源合并）──
    pool = _load_pool()
    all_proxies = pool.get("configs", [])

    # Phase 8 P2: 合并 proxy_pool_manager 的 922S5 代理池
    try:
        from src.device_control.proxy_pool_manager import get_available_proxies as _get_pool_proxies
        pool_mgr_proxies = _get_pool_proxies(country=router.country if router.country else None)
        existing_ids = {p["id"] for p in all_proxies}
        for pm_p in pool_mgr_proxies:
            # 标准化字段格式以兼容 vpn_pool 结构
            normalized = {
                "id": pm_p.get("id") or pm_p.get("proxy_id", ""),
                "label": pm_p.get("label") or pm_p.get("id", ""),
                "protocol": "socks5",
                "server": pm_p.get("server", ""),
                "port": pm_p.get("port", 0),
                "username": pm_p.get("username", ""),
                "password": pm_p.get("password", ""),
                "country": pm_p.get("country", ""),
                "source": pm_p.get("source", "922s5"),
            }
            if normalized["id"] and normalized["id"] not in existing_ids and normalized["server"]:
                all_proxies.append(normalized)
                existing_ids.add(normalized["id"])
        log.debug("[Rotator] 合并后代理池共 %d 个（含922S5）", len(all_proxies))
    except Exception as _pool_e:
        log.debug("[Rotator] proxy_pool_manager 合并失败（降级）: %s", _pool_e)

    # 所有路由器已使用的代理（避免一个代理同时分配给多个路由器）
    all_used_proxy_ids: set = set()
    for r in mgr.list_routers():
        if r.router_id != router_id:
            all_used_proxy_ids.update(r.proxy_ids)

    # 过滤候选：不在黑名单、不被其他路由器使用
    candidates = [
        p for p in all_proxies
        if p["id"] not in blacklist
        and p["id"] not in all_used_proxy_ids
    ]

    # Phase 8 P2: 按综合评分（健康×0.6 + 亲和力×0.4）降序排列
    candidates.sort(key=lambda p: get_combined_score(router_id, p["id"]), reverse=True)
    candidates = candidates[:MAX_CANDIDATES]  # 最多测试5个

    if not candidates:
        err_msg = (
            f"代理池耗尽：所有代理均已被使用或在黑名单中"
            f"（黑名单={len(blacklist)}个，其他路由器={len(all_used_proxy_ids)}个）"
        )
        log.warning("[Rotator] %s %s", router_id, err_msg)

        # Phase 6 P0: 尝试通过 922S5 API 自动补货
        replenish_result = _try_922s5_replenish(list(blacklist), err_msg, router)
        if replenish_result.get("purchased", 0) > 0:
            # 补货成功，重新获取候选
            pool2 = _load_pool()
            all_proxies2 = pool2.get("configs", [])
            candidates = [
                p for p in all_proxies2
                if p["id"] not in blacklist
                and p["id"] not in all_used_proxy_ids
            ]
            candidates.sort(key=lambda p: get_proxy_score(p["id"]), reverse=True)
            candidates = candidates[:MAX_CANDIDATES]
            log.info("[Rotator] 922S5补货后获得 %d 个候选代理", len(candidates))

        if not candidates:
            result["error"] = err_msg
            _send_rotator_alert(
                f"⚠️ 代理轮换失败\n路由器: {router.name}\n原因: {err_msg}\n"
                f"需要手动添加新代理账号到配置池"
            )
            return result

    log.info("[Rotator] %s 找到 %d 个候选代理，开始连通性测试",
             router_id, len(candidates))

    # ── Step 5: 逐个测试候选，选第一个通过的 ──
    chosen = None
    for candidate in candidates:
        test_result = test_proxy_connection(candidate)
        score = get_proxy_score(candidate["id"])
        record_proxy_test(candidate["id"], test_result["ok"], test_result.get("latency_ms", 0))

        log.debug("[Rotator] 候选 %s 测试: %s (评分=%.2f, 延迟=%.0fms)",
                  candidate.get("label", candidate["id"]),
                  "通过" if test_result["ok"] else "失败",
                  score, test_result.get("latency_ms", 0))

        if test_result["ok"]:
            chosen = candidate
            break

    if not chosen:
        result["error"] = f"所有 {len(candidates)} 个候选代理连通性测试均失败"
        log.warning("[Rotator] %s %s", router_id, result["error"])
        _send_rotator_alert(
            f"🚨 代理轮换失败（无可用备用）\n"
            f"路由器: {router.name}\n"
            f"测试了 {len(candidates)} 个候选，全部无法连接\n"
            f"需要手动处理！"
        )
        return result

    new_proxy_ids = [chosen["id"]]
    result["new_proxy_ids"] = new_proxy_ids
    result["chosen_proxy"] = chosen.get("label", chosen["id"])

    # ── Step 6: 更新路由器代理分配 ──
    mgr.assign_proxies(router_id, new_proxy_ids)
    log.info("[Rotator] %s 代理轮换: %s → %s",
             router_id, old_proxy_ids, new_proxy_ids)

    # ── Step 7: 生成并推送新配置 ──
    clash_yaml = generate_clash_config(router, [chosen])
    if not clash_yaml:
        result["error"] = "Clash 配置生成失败"
        mgr.assign_proxies(router_id, old_proxy_ids)  # 回滚分配
        return result

    pushed, backup_path = push_clash_config(router, clash_yaml)
    result["pushed"] = pushed

    if not pushed:
        result["error"] = "Clash 配置推送失败"
        mgr.assign_proxies(router_id, old_proxy_ids)  # 回滚分配
        return result

    # ── Step 8: 等待 Clash 重启，验证新出口 IP ──
    log.info("[Rotator] %s 新配置已推送，等待 Clash 重启（30s）...", router_id)
    time.sleep(30)

    new_exit_ip = get_router_exit_ip(router)
    result["exit_ip"] = new_exit_ip

    if new_exit_ip:
        # 验证地理位置
        if router.country:
            from src.device_control.ip_geolocation import verify_ip_for_country
            geo_match, geo_info = verify_ip_for_country(new_exit_ip, router.country)
            result["geo_match"] = geo_match
            result["geo_info"] = geo_info
            if not geo_match:
                log.warning("[Rotator] %s 新出口IP %s 在 %s，但期望 %s",
                            router_id, new_exit_ip,
                            geo_info.get("actual", "?"), router.country)

        mgr.update_router(router_id, {
            "current_exit_ip": new_exit_ip,
            "last_check": time.time(),
            "online": True,
        })
        result["exit_verified"] = True
        result["ok"] = True

        log.info("[Rotator] %s 代理轮换成功！新出口IP: %s", router_id, new_exit_ip)
        _send_rotator_alert(
            f"✅ 代理自动轮换成功\n"
            f"路由器: {router.name}\n"
            f"旧代理: {old_proxy_ids}\n"
            f"新代理: {chosen.get('label', chosen['id'])}\n"
            f"新出口IP: {new_exit_ip}\n"
            f"原因: {reason}"
        )
    else:
        result["exit_verified"] = False
        result["ok"] = False  # 轮换了但出口IP验证失败
        result["error"] = "新代理已部署，但出口IP验证超时（Clash可能仍在重启中）"
        log.warning("[Rotator] %s %s", router_id, result["error"])
        _send_rotator_alert(
            f"⚠️ 代理轮换完成但IP验证超时\n"
            f"路由器: {router.name}\n"
            f"新代理: {chosen.get('label', chosen['id'])}\n"
            f"请稍后手动检查出口IP"
        )

    # ── Step 9: 记录亲和力结果 + 更新持久化状态 ──
    # Phase 8 P2: 记录本次轮换的亲和力（成功/失败）
    if chosen:
        record_affinity(router_id, chosen["id"], result["ok"])

    new_blacklist = sorted(blacklist)
    history_entry = {
        "ts": time.time(),
        "ts_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "from": old_proxy_ids,
        "to": new_proxy_ids,
        "reason": reason,
        "success": result["ok"],
        "exit_ip": new_exit_ip,
    }
    existing_history = router_state.get("history", [])
    existing_history.append(history_entry)
    existing_history = existing_history[-50:]  # 只保留最近50条

    _update_router_state(router_id, {
        "last_rotation": time.time(),
        "rotation_count": router_state.get("rotation_count", 0) + 1,
        "blacklist": new_blacklist,
        "history": existing_history,
    })

    return result


def get_rotation_history(router_id: str) -> dict:
    """获取路由器的轮换历史和黑名单。"""
    state = _get_router_state(router_id)
    return {
        "router_id": router_id,
        "last_rotation": state.get("last_rotation", 0),
        "rotation_count": state.get("rotation_count", 0),
        "blacklist": state.get("blacklist", []),
        "history": list(reversed(state.get("history", []))),  # 最新的在前
    }


def clear_blacklist(router_id: str) -> bool:
    """清除路由器的代理黑名单（运维操作，允许重新使用曾失败的代理）。"""
    _update_router_state(router_id, {"blacklist": []})
    log.info("[Rotator] %s 黑名单已清除", router_id)
    return True


def _send_rotator_alert(message: str):
    """发送 Telegram 告警（降级处理，不抛异常）。"""
    try:
        from src.host.routers.notifications import send_telegram_message
        send_telegram_message(message)
    except Exception:
        log.warning("[Rotator] 告警发送失败: %s", message[:80])


def _try_922s5_replenish(blacklist: list, err_msg: str, router) -> dict:
    """在代理池耗尽时，尝试通过 922S5 API 自动补货（Phase 6 P0）。

    Args:
        blacklist: 当前黑名单列表（用于传入 check_and_replenish）
        err_msg: 耗尽错误描述（用于告警）
        router: 路由器对象（用于获取国家信息）

    Returns:
        {purchased: int, action: str, error: str}
    """
    try:
        from src.device_control.proxy_922s5 import check_and_replenish, get_922s5_status

        # 检查是否已配置 922S5
        status = get_922s5_status()
        if not status.get("configured"):
            log.info("[Rotator] 922S5 未配置，跳过自动补货")
            return {"purchased": 0, "action": "skipped", "error": "922S5未配置"}

        # 确定目标国家（优先用路由器的国家）
        target_countries = None
        if hasattr(router, "country") and router.country:
            target_countries = [router.country.upper()]

        log.info("[Rotator] 代理池耗尽，尝试 922S5 自动补货 (country=%s)...",
                 target_countries)

        result = check_and_replenish(
            blacklist=blacklist,
            target_countries=target_countries,
        )

        if result.get("purchased", 0) > 0:
            _send_rotator_alert(
                f"🔄 922S5 自动补货成功\n"
                f"购买了 {result['purchased']} 个新代理\n"
                f"触发原因: {err_msg[:100]}"
            )
        else:
            log.warning("[Rotator] 922S5 自动补货失败: %s", result.get("error", "未知"))

        return result

    except ImportError:
        log.debug("[Rotator] proxy_922s5 模块不可用，跳过自动补货")
        return {"purchased": 0, "action": "skipped", "error": "模块不可用"}
    except Exception as e:
        log.error("[Rotator] 922S5 补货异常: %s", e)
        return {"purchased": 0, "action": "failed", "error": str(e)}
