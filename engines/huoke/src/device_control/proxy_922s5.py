# -*- coding: utf-8 -*-
"""
922S5 代理 API 集成 + 代理池自动补货 — Phase 6 P0

解决的问题:
  当代理池耗尽（rotate_proxy 找不到候选）时，系统只能报警等人工处理。
  接入 922S5 的 REST API，可以：
    1. 查询账户余额
    2. 列出/刷新当前代理
    3. 将新代理同步到 pool
    4. 黑名单中的代理自动刷新（换IP）

922S5 API 文档:
  Base URL: https://api.922s5.com/api
  认证方式: ?appKey=KEY&appSecret=SECRET（Query 参数）

使用:
  from src.device_control.proxy_922s5 import get_922s5_client, replenish_proxy_pool

  client = get_922s5_client()
  proxies = client.list_proxies()
  client.refresh_proxy(proxy_id)
  replenish_proxy_pool(target_count=5)
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from src.host.device_registry import config_file

log = logging.getLogger(__name__)

# ─────────────────────── 常量 ───────────────────────

_BASE_URL = "https://api.922s5.com/api"
_API_TIMEOUT = 15       # 秒
_MAX_RETRIES = 3
_RETRY_DELAY = 1.5      # 秒（指数退避基数）

# 余额不足告警阈值（美元）
_LOW_BALANCE_THRESHOLD = 5.0

# 代理池最小数量（低于此值触发补货）
_MIN_POOL_SIZE = 3

# 配置文件路径
_CONFIG_FILE = config_file("922s5_config.json")

# 代理池路径（与 router_manager.py 共享的代理账号列表）
_POOL_CONFIG = config_file("vpn_config.json")

# 全局单例
_client_instance: Optional["Proxy922S5Client"] = None
_client_lock = threading.Lock()


# ─────────────────────── 数据结构 ───────────────────────

class Proxy922S5Info:
    """922S5 代理信息结构。"""
    def __init__(self, data: dict):
        self.proxy_id: str = str(data.get("proxyId", data.get("id", "")))
        self.server: str = data.get("host", data.get("server", ""))
        self.port: int = int(data.get("port", 0))
        self.username: str = data.get("username", data.get("user", ""))
        self.password: str = data.get("password", data.get("pass", ""))
        self.protocol: str = data.get("protocol", "socks5").lower()
        self.country: str = data.get("country", data.get("countryCode", "")).upper()
        self.city: str = data.get("city", "")
        self.expire_time: str = data.get("expireTime", data.get("expireAt", ""))
        self.status: str = data.get("status", "active")
        self.raw = data

    def to_pool_entry(self) -> dict:
        """转换为 vpn_config.json 中的代理条目格式。"""
        label = f"922s5_{self.proxy_id}"
        if self.country:
            label = f"922s5_{self.country.lower()}_{self.proxy_id[-4:]}"
        return {
            "label": label,
            "type": self.protocol,
            "server": self.server,
            "port": self.port,
            "username": self.username,
            "password": self.password,
            "country": self.country.lower(),
            "source": "922s5",
            "proxy_id": self.proxy_id,
            "expire_time": self.expire_time,
            "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def to_clash_proxy(self) -> dict:
        """转换为 Clash 代理配置格式。"""
        return {
            "name": f"922s5-{self.proxy_id[-6:]}",
            "type": self.protocol,
            "server": self.server,
            "port": self.port,
            "username": self.username,
            "password": self.password,
        }

    def __repr__(self):
        return (f"Proxy922S5Info(id={self.proxy_id}, "
                f"{self.protocol}://{self.server}:{self.port}, "
                f"{self.country}/{self.city})")


# ─────────────────────── API 客户端 ───────────────────────

class Proxy922S5Client:
    """922S5 代理服务 REST API 客户端。

    认证方式: Query 参数 appKey + appSecret
    """

    def __init__(self, app_key: str, app_secret: str):
        self.app_key = app_key
        self.app_secret = app_secret
        self._lock = threading.Lock()

    def _build_url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        """构建带认证的完整 URL。"""
        base_params = {"appKey": self.app_key, "appSecret": self.app_secret}
        if params:
            base_params.update(params)
        query = urllib.parse.urlencode(base_params)
        return f"{_BASE_URL}/{path.lstrip('/')}?{query}"

    def _call(self, method: str, path: str,
              params: Optional[dict] = None,
              body: Optional[dict] = None) -> Optional[dict]:
        """发送 HTTP 请求（带重试 + 指数退避）。

        Returns:
            解析后的 JSON 响应 dict，或 None（失败）
        """
        url = self._build_url(path, params)
        data = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json", "User-Agent": "OpenClaw/1.0"}

        for attempt in range(_MAX_RETRIES):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                resp = urllib.request.urlopen(req, timeout=_API_TIMEOUT)
                raw = json.loads(resp.read().decode())

                # 922S5 标准响应格式: {code: 200, msg: "success", data: {...}}
                code = raw.get("code", raw.get("status", 0))
                if code == 200 or code == "200" or raw.get("success"):
                    return raw.get("data", raw)
                else:
                    msg = raw.get("msg", raw.get("message", "unknown error"))
                    log.warning("[922S5] API 业务错误: code=%s msg=%s", code, msg)
                    return None

            except (urllib.error.URLError, OSError, TimeoutError) as e:
                if attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_DELAY * (2 ** attempt)
                    log.debug("[922S5] 网络错误(尝试%d/%d): %s, 等待%.1fs",
                              attempt + 1, _MAX_RETRIES, e, delay)
                    time.sleep(delay)
                else:
                    log.error("[922S5] 请求失败(已重试%d次): %s %s → %s",
                              _MAX_RETRIES, method, path, e)
            except (json.JSONDecodeError, KeyError) as e:
                log.error("[922S5] 响应解析失败: %s", e)
                return None  # 业务错误不重试

        return None

    # ── 账户相关 ──

    def get_balance(self) -> Optional[dict]:
        """查询账户余额。

        Returns:
            {balance: float, currency: str, expire_time: str} 或 None
        """
        result = self._call("GET", "/user/balance")
        if result:
            # 标准化字段名
            return {
                "balance": float(result.get("balance", result.get("amount", 0))),
                "currency": result.get("currency", "USD"),
                "expire_time": result.get("expireTime", result.get("expireAt", "")),
                "plan": result.get("plan", result.get("product", "")),
            }
        return None

    def get_account_info(self) -> Optional[dict]:
        """获取账户基本信息。"""
        return self._call("GET", "/user/info")

    # ── 代理管理 ──

    def list_proxies(self, country: Optional[str] = None,
                     status: str = "active", page: int = 1,
                     page_size: int = 100) -> List[Proxy922S5Info]:
        """列出账户下的代理列表。

        Args:
            country: 过滤国家（'US', 'JP' 等，None=全部）
            status: 'active' / 'expired' / 'all'
            page, page_size: 分页参数

        Returns:
            Proxy922S5Info 列表
        """
        params: dict = {"page": page, "pageSize": page_size, "status": status}
        if country:
            params["country"] = country.upper()

        result = self._call("GET", "/proxy/list", params=params)
        if not result:
            return []

        # 处理分页结构
        items = result if isinstance(result, list) else result.get("list", result.get("proxies", []))
        proxies = []
        for item in items:
            try:
                proxies.append(Proxy922S5Info(item))
            except Exception as e:
                log.debug("[922S5] 解析代理条目失败: %s — %s", e, item)

        log.info("[922S5] 获取代理列表: %d 个", len(proxies))
        return proxies

    def refresh_proxy(self, proxy_id: str) -> Optional[Proxy922S5Info]:
        """刷新代理 IP（换一个新出口 IP，保持协议/端口不变）。

        Returns:
            更新后的 Proxy922S5Info，或 None（失败）
        """
        result = self._call("POST", "/proxy/refresh", body={"proxyId": proxy_id})
        if result:
            log.info("[922S5] 代理 %s 刷新成功", proxy_id)
            return Proxy922S5Info(result if isinstance(result, dict) else {"proxyId": proxy_id})
        log.warning("[922S5] 代理 %s 刷新失败", proxy_id)
        return None

    def batch_refresh_proxies(self, proxy_ids: List[str]) -> dict:
        """批量刷新多个代理 IP。

        Returns:
            {success: [id,...], failed: [id,...]}
        """
        result = self._call("POST", "/proxy/batch-refresh", body={"proxyIds": proxy_ids})
        if result:
            return {
                "success": result.get("success", result.get("successIds", [])),
                "failed": result.get("failed", result.get("failedIds", [])),
            }
        return {"success": [], "failed": proxy_ids}

    def buy_proxies(self, country: str, count: int = 1,
                    duration_days: int = 30, protocol: str = "socks5") -> List[Proxy922S5Info]:
        """购买新代理。

        Args:
            country: 国家代码 ('US', 'JP' 等)
            count: 购买数量
            duration_days: 有效期（天）
            protocol: 'socks5' 或 'http'

        Returns:
            新购买的 Proxy922S5Info 列表
        """
        body = {
            "country": country.upper(),
            "count": count,
            "duration": duration_days,
            "protocol": protocol,
        }
        result = self._call("POST", "/proxy/buy", body=body)
        if not result:
            log.error("[922S5] 购买代理失败: %s x%d %dd", country, count, duration_days)
            return []

        items = result if isinstance(result, list) else result.get("proxies", result.get("list", []))
        proxies = [Proxy922S5Info(item) for item in items]
        log.info("[922S5] 购买成功: %d 个 %s 代理", len(proxies), country)
        return proxies

    def get_proxy_countries(self) -> List[dict]:
        """获取支持的国家列表和库存。"""
        result = self._call("GET", "/proxy/countries")
        if result:
            return result if isinstance(result, list) else result.get("countries", [])
        return []


# ─────────────────────── 单例管理 ───────────────────────

def load_922s5_config() -> Optional[dict]:
    """加载 922S5 配置。"""
    if _CONFIG_FILE.exists():
        try:
            return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.error("[922S5] 配置加载失败: %s", e)
    return None


def save_922s5_config(config: dict):
    """保存 922S5 配置。"""
    try:
        _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("[922S5] 配置已保存到 %s", _CONFIG_FILE)
    except Exception as e:
        log.error("[922S5] 配置保存失败: %s", e)


def get_922s5_client() -> Optional["Proxy922S5Client"]:
    """获取全局 922S5 客户端单例（需先配置）。

    Returns:
        Proxy922S5Client 实例，或 None（未配置）
    """
    global _client_instance
    with _client_lock:
        if _client_instance is not None:
            return _client_instance

        config = load_922s5_config()
        if not config:
            log.warning("[922S5] 未找到配置文件: %s", _CONFIG_FILE)
            log.warning("[922S5] 请调用 configure_922s5(app_key, app_secret) 初始化")
            return None

        app_key = config.get("app_key", "")
        app_secret = config.get("app_secret", "")
        if not app_key or not app_secret:
            log.error("[922S5] 配置不完整（缺少 app_key 或 app_secret）")
            return None

        _client_instance = Proxy922S5Client(app_key, app_secret)
        log.info("[922S5] 客户端初始化完成 (key=%s...)", app_key[:8])
        return _client_instance


def configure_922s5(app_key: str, app_secret: str,
                    auto_replenish: bool = True,
                    min_pool_size: int = _MIN_POOL_SIZE,
                    low_balance_threshold: float = _LOW_BALANCE_THRESHOLD):
    """初始化 922S5 配置并保存。

    Args:
        app_key: 922S5 API Key
        app_secret: 922S5 API Secret
        auto_replenish: 是否开启自动补货
        min_pool_size: 代理池最小数量
        low_balance_threshold: 低余额告警阈值（美元）
    """
    global _client_instance
    config = {
        "app_key": app_key,
        "app_secret": app_secret,
        "auto_replenish": auto_replenish,
        "min_pool_size": min_pool_size,
        "low_balance_threshold": low_balance_threshold,
        "configured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    save_922s5_config(config)
    with _client_lock:
        _client_instance = Proxy922S5Client(app_key, app_secret)
    log.info("[922S5] 配置完成: auto_replenish=%s, min_pool=%d", auto_replenish, min_pool_size)


# ─────────────────────── 代理池同步 ───────────────────────

def _load_pool() -> List[dict]:
    """加载当前代理池（vpn_config.json）。"""
    if _POOL_CONFIG.exists():
        try:
            data = json.loads(_POOL_CONFIG.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else data.get("proxies", [])
        except Exception as e:
            log.error("[922S5] 代理池加载失败: %s", e)
    return []


def _save_pool(pool: List[dict]):
    """保存代理池。"""
    try:
        _POOL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        _POOL_CONFIG.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.error("[922S5] 代理池保存失败: %s", e)


def sync_proxies_to_pool(proxies: List[Proxy922S5Info]) -> int:
    """将 922S5 代理列表同步到本地代理池（不重复添加）。

    Returns:
        新增的代理数量
    """
    pool = _load_pool()
    existing_ids = {p.get("proxy_id", "") for p in pool if p.get("source") == "922s5"}
    existing_servers = {(p.get("server", ""), p.get("port", 0)) for p in pool}

    added = 0
    for proxy in proxies:
        if proxy.proxy_id in existing_ids:
            continue
        if (proxy.server, proxy.port) in existing_servers:
            continue
        entry = proxy.to_pool_entry()
        pool.append(entry)
        existing_ids.add(proxy.proxy_id)
        existing_servers.add((proxy.server, proxy.port))
        added += 1
        log.info("[922S5] 新增代理到池: %s (%s:%d)", proxy.proxy_id, proxy.server, proxy.port)

    if added > 0:
        _save_pool(pool)

    log.info("[922S5] 同步完成: 新增 %d 个，总计 %d 个", added, len(pool))
    return added


def remove_expired_from_pool() -> int:
    """从代理池中移除已过期的 922S5 代理。

    Returns:
        移除数量
    """
    pool = _load_pool()
    now = time.time()
    to_remove = []

    for entry in pool:
        if entry.get("source") != "922s5":
            continue
        expire_str = entry.get("expire_time", "")
        if expire_str:
            try:
                # 解析多种时间格式
                import datetime
                for fmt in ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
                    try:
                        dt = datetime.datetime.strptime(expire_str, fmt)
                        if dt.timestamp() < now:
                            to_remove.append(entry)
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

    for entry in to_remove:
        pool.remove(entry)
        log.info("[922S5] 移除过期代理: %s", entry.get("proxy_id", entry.get("server")))

    if to_remove:
        _save_pool(pool)

    return len(to_remove)


def get_pool_stats() -> dict:
    """获取代理池统计信息。"""
    pool = _load_pool()
    s5_proxies = [p for p in pool if p.get("source") == "922s5"]
    manual_proxies = [p for p in pool if p.get("source") != "922s5"]

    countries: Dict[str, int] = {}
    for p in pool:
        c = p.get("country", "unknown")
        countries[c] = countries.get(c, 0) + 1

    return {
        "total": len(pool),
        "922s5_count": len(s5_proxies),
        "manual_count": len(manual_proxies),
        "by_country": countries,
        "needs_replenish": len(pool) < _MIN_POOL_SIZE,
    }


# ─────────────────────── 自动补货 ───────────────────────

def check_and_replenish(blacklist: Optional[List[str]] = None,
                        target_countries: Optional[List[str]] = None,
                        dry_run: bool = False) -> dict:
    """检查代理池是否需要补货，需要则自动购买并同步。

    Args:
        blacklist: 已黑名单的代理 ID 列表（不计入可用数量）
        target_countries: 目标购买国家列表（None=使用配置默认值）
        dry_run: 只检查不实际购买

    Returns:
        {
          needs_replenish: bool,
          pool_size: int,
          available: int,
          blacklist_count: int,
          action: 'skipped' | 'dry_run' | 'purchased' | 'failed',
          purchased: int,
          error: str (可选)
        }
    """
    config = load_922s5_config() or {}
    min_size = config.get("min_pool_size", _MIN_POOL_SIZE)
    auto_replenish = config.get("auto_replenish", True)

    pool = _load_pool()
    blacklist = blacklist or []

    # 可用代理 = 总数 - 黑名单中的数量
    bl_set = set(blacklist)
    available_pool = [
        p for p in pool
        if p.get("proxy_id", p.get("label", "")) not in bl_set
    ]
    available = len(available_pool)
    needs = available < min_size

    result = {
        "needs_replenish": needs,
        "pool_size": len(pool),
        "available": available,
        "blacklist_count": len(blacklist),
        "action": "skipped",
        "purchased": 0,
    }

    if not needs:
        log.debug("[922S5] 代理池充足: %d 个可用（最低要求 %d）", available, min_size)
        return result

    log.warning("[922S5] 代理池不足！可用 %d 个，最低要求 %d 个", available, min_size)

    if not auto_replenish:
        result["action"] = "skipped"
        result["error"] = f"auto_replenish=False，需手动补货（当前可用: {available}）"
        return result

    if dry_run:
        result["action"] = "dry_run"
        result["error"] = "dry_run 模式，未实际购买"
        return result

    # 确定需要购买的数量
    need_count = min_size - available + 2  # 多买2个作为缓冲

    client = get_922s5_client()
    if not client:
        result["action"] = "failed"
        result["error"] = "922S5 客户端未配置"
        return result

    # 检查余额
    balance_info = client.get_balance()
    if balance_info:
        balance = balance_info.get("balance", 0)
        threshold = config.get("low_balance_threshold", _LOW_BALANCE_THRESHOLD)
        if balance < threshold:
            log.warning("[922S5] 账户余额不足: $%.2f (阈值 $%.2f)", balance, threshold)
            # 仍然尝试购买（让 API 返回具体错误）

    # 确定购买国家（优先选 pool 中已有国家，补充均衡性）
    if not target_countries:
        target_countries = config.get("preferred_countries", ["US"])

    purchased_total = 0
    for country in target_countries:
        per_country = max(1, need_count // len(target_countries))
        log.info("[922S5] 购买 %d 个 %s 代理...", per_country, country)
        new_proxies = client.buy_proxies(country, count=per_country)
        if new_proxies:
            added = sync_proxies_to_pool(new_proxies)
            purchased_total += added
            log.info("[922S5] 已新增 %d 个 %s 代理到池", added, country)
        else:
            log.error("[922S5] 购买 %s 代理失败", country)

    result["action"] = "purchased" if purchased_total > 0 else "failed"
    result["purchased"] = purchased_total
    if purchased_total == 0:
        result["error"] = "所有购买尝试均失败，请检查账户余额和配置"

    return result


def replenish_proxy_pool(target_count: int = 5,
                         countries: Optional[List[str]] = None) -> dict:
    """主动补货：直接购买指定数量的代理并同步到池。

    Args:
        target_count: 目标总代理数（不够才购买）
        countries: 购买国家列表

    Returns:
        {ok: bool, purchased: int, pool_size: int, error: str}
    """
    client = get_922s5_client()
    if not client:
        return {"ok": False, "purchased": 0, "pool_size": 0,
                "error": "922S5 未配置，请先调用 configure_922s5()"}

    stats = get_pool_stats()
    current = stats["total"]

    if current >= target_count:
        return {"ok": True, "purchased": 0, "pool_size": current,
                "message": f"代理池已有 {current} 个，无需补货"}

    need = target_count - current
    countries = countries or ["US"]
    purchased = 0

    for country in countries:
        per_country = max(1, need // len(countries))
        new_proxies = client.buy_proxies(country, count=per_country)
        if new_proxies:
            added = sync_proxies_to_pool(new_proxies)
            purchased += added

    new_stats = get_pool_stats()
    return {
        "ok": purchased > 0,
        "purchased": purchased,
        "pool_size": new_stats["total"],
        "error": None if purchased > 0 else "购买失败，请检查 922S5 账户",
    }


def refresh_blacklisted_proxies(blacklist: List[str]) -> dict:
    """刷新黑名单中的 922S5 代理 IP（换出口不换账号）。

    Args:
        blacklist: 黑名单代理 ID 列表

    Returns:
        {refreshed: [id,...], failed: [id,...]}
    """
    client = get_922s5_client()
    if not client:
        return {"refreshed": [], "failed": blacklist,
                "error": "922S5 未配置"}

    # 只刷新来自 922S5 的代理
    pool = _load_pool()
    s5_ids = {p.get("proxy_id", "") for p in pool if p.get("source") == "922s5"}
    to_refresh = [pid for pid in blacklist if pid in s5_ids]

    if not to_refresh:
        log.info("[922S5] 黑名单中无 922S5 代理需要刷新")
        return {"refreshed": [], "failed": [], "skipped": len(blacklist)}

    log.info("[922S5] 刷新黑名单代理: %s", to_refresh)
    if len(to_refresh) == 1:
        result = client.refresh_proxy(to_refresh[0])
        if result:
            return {"refreshed": to_refresh, "failed": []}
        return {"refreshed": [], "failed": to_refresh}
    else:
        return client.batch_refresh_proxies(to_refresh)


# ─────────────────────── Telegram 告警集成 ───────────────────────

def send_low_pool_alert(available: int, min_size: int):
    """当代理池不足时发送 Telegram 告警（复用现有通知机制）。"""
    try:
        from src.behavior.notifier import send_telegram_alert
        msg = (
            f"⚠️ 代理池不足告警\n"
            f"可用代理: {available} 个\n"
            f"最低要求: {min_size} 个\n"
            f"请检查 922S5 账户余额并手动补货，或确保 auto_replenish=True"
        )
        send_telegram_alert(msg)
    except ImportError:
        log.warning("[922S5] 未找到 Telegram 通知模块，跳过告警")
    except Exception as e:
        log.error("[922S5] 发送告警失败: %s", e)


def send_balance_alert(balance: float, threshold: float):
    """余额不足告警。"""
    try:
        from src.behavior.notifier import send_telegram_alert
        msg = (
            f"💰 922S5 余额不足告警\n"
            f"当前余额: ${balance:.2f}\n"
            f"告警阈值: ${threshold:.2f}\n"
            f"请及时充值，否则代理到期后无法自动续费"
        )
        send_telegram_alert(msg)
    except ImportError:
        log.warning("[922S5] 未找到 Telegram 通知模块，跳过告警")
    except Exception as e:
        log.error("[922S5] 发送余额告警失败: %s", e)


# ─────────────────────── 状态查询 ───────────────────────

def get_922s5_status() -> dict:
    """获取 922S5 集成状态（用于 API 端点和诊断）。"""
    config = load_922s5_config()
    if not config:
        return {
            "configured": False,
            "error": "未配置，请调用 configure_922s5() 或手动创建配置文件",
            "config_path": str(_CONFIG_FILE),
        }

    client = get_922s5_client()
    status: dict = {
        "configured": True,
        "auto_replenish": config.get("auto_replenish", True),
        "min_pool_size": config.get("min_pool_size", _MIN_POOL_SIZE),
        "pool_stats": get_pool_stats(),
    }

    if client:
        balance = client.get_balance()
        status["balance"] = balance
        if balance:
            threshold = config.get("low_balance_threshold", _LOW_BALANCE_THRESHOLD)
            status["balance_warning"] = balance.get("balance", 0) < threshold
    else:
        status["balance"] = None
        status["balance_warning"] = False

    return status
