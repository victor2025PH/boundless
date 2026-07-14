# -*- coding: utf-8 -*-
"""
代理池管理器 — Phase 7 P0

功能:
  1. 专用代理池文件（proxy_pool.json），独立于 vpn_config.json（VLESS 格式）
  2. 922S5 定时巡检：每日同步 + 清理过期代理
  3. 余额检查 + 低余额告警
  4. 代理池健康摘要 API

架构说明（优化思考后的设计决策）:
  旧方案: 直接读写 vpn_config.json（单 VLESS 条目，格式不兼容）
  新方案: 独立 proxy_pool.json（数组格式），vpn_config.json 保持不变
  好处：
    - 不破坏现有 VLESS 代理（给 Clash 使用的主代理）
    - 922S5 代理池独立管理，可按国家/状态过滤
    - router_manager.py 的 pool 接口可扩展为「主池 + 922S5 池」合并视图

定时任务调度:
  任务类型: "proxy_pool_sync"
  默认 cron: "0 6 * * *"（每天早上6点）
  参数: {sync: true, cleanup: true, check_balance: true}
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from src.host.device_registry import config_file

log = logging.getLogger(__name__)

# ── 配置 ──
_POOL_FILE = config_file("proxy_pool.json")
_pool_lock = threading.Lock()

# 低池告警阈值（可用代理少于此值时发送告警）
_MIN_POOL_WARN = 3

# ── 格式 (proxy_pool.json) ──
# [
#   {
#     "proxy_id": "922s5_abc123",
#     "label": "922s5_us_c123",
#     "type": "socks5",
#     "server": "1.2.3.4",
#     "port": 1080,
#     "username": "user",
#     "password": "pass",
#     "country": "us",
#     "source": "922s5",
#     "expire_time": "2026-12-31T00:00:00Z",
#     "synced_at": "2026-04-11T06:00:00Z",
#     "active": true
#   },
#   ...
# ]


def load_pool() -> List[dict]:
    """加载本地代理池（proxy_pool.json）。"""
    with _pool_lock:
        if not _POOL_FILE.exists():
            return []
        try:
            data = json.loads(_POOL_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception as e:
            log.error("[PoolMgr] 加载代理池失败: %s", e)
            return []


def save_pool(pool: List[dict]):
    """保存代理池到磁盘。"""
    with _pool_lock:
        try:
            _POOL_FILE.parent.mkdir(parents=True, exist_ok=True)
            _POOL_FILE.write_text(
                json.dumps(pool, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            log.error("[PoolMgr] 保存代理池失败: %s", e)


def get_available_proxies(country: Optional[str] = None,
                          exclude_ids: Optional[List[str]] = None) -> List[dict]:
    """获取可用代理列表（未过期 + 活跃 + 可选国家过滤）。

    Args:
        country: 过滤国家代码（None=全部）
        exclude_ids: 排除的 proxy_id 列表（黑名单）

    Returns:
        可用代理列表，按 synced_at 倒序排列（最新的优先）
    """
    pool = load_pool()
    now = time.time()
    exclude = set(exclude_ids or [])

    available = []
    for p in pool:
        if p.get("proxy_id", "") in exclude:
            continue
        if not p.get("active", True):
            continue
        # 检查过期时间（注意：continue 在内层循环无法跳过外层，使用 is_expired 标志）
        is_expired = False
        exp_str = p.get("expire_time", "")
        if exp_str:
            try:
                from datetime import datetime
                for fmt in ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
                    try:
                        dt = datetime.strptime(exp_str, fmt)
                        if dt.timestamp() < now:
                            is_expired = True
                        break
                    except ValueError:
                        continue
            except Exception:
                pass
        if is_expired:
            continue
        # 国家过滤
        if country:
            p_country = p.get("country", "").lower().strip()
            c_norm = country.lower().strip()
            if p_country != c_norm:
                continue
        available.append(p)

    # 最新同步的排前面
    available.sort(key=lambda x: x.get("synced_at", ""), reverse=True)
    return available


def get_pool_stats() -> dict:
    """获取代理池统计信息。"""
    pool = load_pool()
    now = time.time()

    total = len(pool)
    active = 0
    expired = 0
    by_country: Dict[str, int] = {}
    by_source: Dict[str, int] = {}

    for p in pool:
        source = p.get("source", "manual")
        by_source[source] = by_source.get(source, 0) + 1
        c = p.get("country", "unknown")
        by_country[c] = by_country.get(c, 0) + 1

        exp_str = p.get("expire_time", "")
        is_expired = False
        if exp_str:
            try:
                from datetime import datetime
                for fmt in ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
                    try:
                        dt = datetime.strptime(exp_str, fmt)
                        is_expired = dt.timestamp() < now
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

        if is_expired:
            expired += 1
        else:
            active += 1

    return {
        "total": total,
        "active": active,
        "expired": expired,
        "by_country": by_country,
        "by_source": by_source,
        "pool_file": str(_POOL_FILE),
        "needs_attention": active < _MIN_POOL_WARN,
    }


# ─────────────────────────────────────────────
# 922S5 同步逻辑
# ─────────────────────────────────────────────

def sync_from_922s5(country: Optional[str] = None) -> dict:
    """从 922S5 API 同步代理到本地池。

    Returns:
        {fetched, added, skipped, error}
    """
    try:
        from src.device_control.proxy_922s5 import get_922s5_client, Proxy922S5Info
    except ImportError:
        return {"fetched": 0, "added": 0, "skipped": 0, "error": "922s5 模块不可用"}

    client = get_922s5_client()
    if not client:
        return {"fetched": 0, "added": 0, "skipped": 0, "error": "922S5 未配置"}

    proxies = client.list_proxies(country=country, status="active")
    if not proxies:
        return {"fetched": 0, "added": 0, "skipped": 0, "error": "获取代理列表为空"}

    pool = load_pool()
    existing_ids = {p.get("proxy_id", "") for p in pool}
    existing_servers = {(p.get("server", ""), p.get("port", 0)) for p in pool}

    added = 0
    skipped = 0
    for proxy in proxies:
        pid = proxy.proxy_id
        srv = (proxy.server, proxy.port)
        if pid in existing_ids or srv in existing_servers:
            skipped += 1
            continue
        entry = proxy.to_pool_entry()
        entry["active"] = True
        pool.append(entry)
        existing_ids.add(pid)
        existing_servers.add(srv)
        added += 1

    if added > 0:
        save_pool(pool)
        log.info("[PoolMgr] 从 922S5 新增 %d 个代理，跳过 %d 个重复", added, skipped)

    return {"fetched": len(proxies), "added": added, "skipped": skipped, "error": None}


def cleanup_expired() -> int:
    """清理过期代理（标记 active=False 而非直接删除，便于运维回溯）。

    Returns:
        标记过期的数量
    """
    pool = load_pool()
    now = time.time()
    marked = 0

    for p in pool:
        if not p.get("active", True):
            continue
        exp_str = p.get("expire_time", "")
        if not exp_str:
            continue
        try:
            from datetime import datetime
            for fmt in ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
                try:
                    dt = datetime.strptime(exp_str, fmt)
                    if dt.timestamp() < now:
                        p["active"] = False
                        marked += 1
                    break
                except ValueError:
                    continue
        except Exception:
            pass

    if marked > 0:
        save_pool(pool)
        log.info("[PoolMgr] 标记 %d 个过期代理为 inactive", marked)

    return marked


def check_balance_and_alert() -> Optional[dict]:
    """检查 922S5 余额，不足时发 Telegram 告警。

    Returns:
        {balance, currency, warning} 或 None
    """
    try:
        from src.device_control.proxy_922s5 import get_922s5_client, load_922s5_config
    except ImportError:
        return None

    config = load_922s5_config() or {}
    threshold = config.get("low_balance_threshold", 5.0)

    client = get_922s5_client()
    if not client:
        return None

    balance_info = client.get_balance()
    if not balance_info:
        return None

    balance = balance_info.get("balance", 0)
    warning = balance < threshold

    if warning:
        log.warning("[PoolMgr] 922S5 余额不足！当前 $%.2f，阈值 $%.2f", balance, threshold)
        try:
            from src.device_control.proxy_922s5 import send_balance_alert
            send_balance_alert(balance, threshold)
        except Exception as e:
            log.debug("[PoolMgr] 余额告警发送失败: %s", e)

    return {
        "balance": balance,
        "currency": balance_info.get("currency", "USD"),
        "threshold": threshold,
        "warning": warning,
    }


# ─────────────────────────────────────────────
# 主巡检任务（供 scheduler 调用）
# ─────────────────────────────────────────────

def run_proxy_pool_sync(params: Optional[dict] = None) -> dict:
    """代理池完整巡检任务。

    执行步骤:
      1. 清理过期代理
      2. 从 922S5 同步最新代理列表
      3. 检查余额
      4. 如果可用池不足 _MIN_POOL_WARN 个，触发自动补货

    Args:
        params: {sync, cleanup, check_balance, country, min_pool_size}

    Returns:
        {ok, sync_result, cleanup_count, balance, pool_stats, replenish}
    """
    params = params or {}
    do_sync = params.get("sync", True)
    do_cleanup = params.get("cleanup", True)
    do_balance = params.get("check_balance", True)
    country = params.get("country")
    min_size = params.get("min_pool_size", _MIN_POOL_WARN)

    result: dict = {
        "ok": True,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Step 1: 清理过期
    if do_cleanup:
        expired_count = cleanup_expired()
        result["cleanup_count"] = expired_count

    # Step 2: 从 922S5 同步
    if do_sync:
        sync_res = sync_from_922s5(country=country)
        result["sync_result"] = sync_res
        if sync_res.get("error"):
            log.warning("[PoolMgr] 同步失败: %s", sync_res["error"])

    # Step 3: 检查余额
    if do_balance:
        balance_info = check_balance_and_alert()
        result["balance"] = balance_info

    # Step 4: 池不足则自动补货
    stats = get_pool_stats()
    result["pool_stats"] = stats

    if stats["active"] < min_size:
        log.warning("[PoolMgr] 可用代理池不足 %d 个（当前 %d），触发自动补货",
                    min_size, stats["active"])
        try:
            from src.device_control.proxy_922s5 import replenish_proxy_pool
            rep = replenish_proxy_pool(target_count=min_size + 2)
            result["replenish"] = rep
        except Exception as e:
            result["replenish"] = {"ok": False, "error": str(e)}
            log.error("[PoolMgr] 自动补货失败: %s", e)

    log.info("[PoolMgr] 巡检完成: 活跃代理 %d 个，新增 %d 个",
             stats["active"], result.get("sync_result", {}).get("added", 0))
    return result


# ─────────────────────────────────────────────
# 合并代理视图（给 router_manager 用）
# ─────────────────────────────────────────────

def get_merged_proxy_pool() -> List[dict]:
    """获取合并代理视图（922S5池 + 手动配置池）。

    优先返回 922S5 代理（有自动刷新能力），
    手动代理作为补充。

    Returns:
        合并后的代理列表，每项包含 {id, label, type, server, port, username, password}
    """
    # 922S5 池
    s5_pool = get_available_proxies()

    # 手动配置（从现有 vpn_config.json 读取，兼容 VLESS 和传统格式）
    manual_pool = _load_manual_pool()

    # 合并（922S5 优先）
    merged = []
    seen_servers = set()

    for p in s5_pool:
        key = (p.get("server", ""), p.get("port", 0))
        if key not in seen_servers:
            merged.append(_normalize_pool_entry(p))
            seen_servers.add(key)

    for p in manual_pool:
        key = (p.get("server", ""), p.get("port", 0))
        if key not in seen_servers:
            merged.append(_normalize_pool_entry(p))
            seen_servers.add(key)

    return merged


def _load_manual_pool() -> List[dict]:
    """加载手动配置的代理（vpn_config.json，兼容多种格式）。"""
    vpn_file = config_file("vpn_config.json")
    if not vpn_file.exists():
        return []
    try:
        data = json.loads(vpn_file.read_text(encoding="utf-8"))
        # 数组格式
        if isinstance(data, list):
            return data
        # 带 configs 键
        if isinstance(data, dict) and "configs" in data:
            return data["configs"]
        # 单条目（VLESS/SOCKS等）
        if isinstance(data, dict) and data.get("server"):
            return [data]
        return []
    except Exception as e:
        log.debug("[PoolMgr] 手动代理加载失败: %s", e)
        return []


def _normalize_pool_entry(p: dict) -> dict:
    """标准化代理条目格式（统一字段名）。"""
    proxy_id = p.get("proxy_id", p.get("id", p.get("label", "")))
    return {
        "id": proxy_id,
        "label": p.get("label", proxy_id),
        "type": p.get("type", p.get("protocol", "socks5")),
        "server": p.get("server", p.get("host", "")),
        "port": int(p.get("port", 0)),
        "username": p.get("username", p.get("user", "")),
        "password": p.get("password", p.get("pass", "")),
        "country": p.get("country", ""),
        "source": p.get("source", "manual"),
        "proxy_id": proxy_id,
    }


# ─────────────────────────────────────────────
# 首次运行：注册定时任务
# ─────────────────────────────────────────────

def ensure_sync_schedule(cron_expr: str = "0 6 * * *") -> Optional[str]:
    """确保代理池定时同步任务已注册（幂等，重复调用安全）。

    Args:
        cron_expr: cron 表达式（默认每天 06:00）

    Returns:
        schedule_id 或 None（创建失败）
    """
    try:
        from src.host.scheduler import list_schedules, create_schedule
        # 检查是否已有同名任务
        existing = [s for s in list_schedules() if s.get("name") == "proxy_pool_sync_daily"]
        if existing:
            log.debug("[PoolMgr] 定时任务已存在: %s", existing[0]["schedule_id"][:8])
            return existing[0]["schedule_id"]

        sid = create_schedule(
            name="proxy_pool_sync_daily",
            cron_expr=cron_expr,
            task_type="proxy_pool_sync",
            params={
                "sync": True,
                "cleanup": True,
                "check_balance": True,
            }
        )
        log.info("[PoolMgr] 已注册定时同步任务: %s cron=%s", sid[:8], cron_expr)
        return sid
    except Exception as e:
        log.warning("[PoolMgr] 注册定时任务失败: %s（调度器可能未启动）", e)
        return None
