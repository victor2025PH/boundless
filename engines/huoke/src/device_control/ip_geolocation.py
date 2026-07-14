# -*- coding: utf-8 -*-
"""
IP 地理位置验证模块。

解决的问题:
  当前系统只验证「手机IP == 路由器出口IP」，但从未验证
  路由器出口IP是否真的在目标国家。一个指向意大利的代理
  实际可能从法国出口，TikTok会检测到这种不一致。

设计:
  - 使用 ip-api.com 免费接口（45次/分钟，足够用）
  - 支持 ipinfo.io / ipwho.is 作为备用
  - 24小时本地缓存（IP很少更换归属国）
  - 批量查询支持（减少API调用次数）
  - 线程安全，可从后台监控线程调用

使用方式:
  from src.device_control.ip_geolocation import lookup_ip_country, verify_ip_for_country

  # 查询IP所属国家
  info = lookup_ip_country("1.2.3.4")
  print(info)  # {"ip": "1.2.3.4", "country_code": "US", "country": "United States", ...}

  # 验证IP是否在目标国家
  ok, info = verify_ip_for_country("1.2.3.4", "us")
  if not ok:
      print(f"IP在 {info['country_code']} 但期望在 US")
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Tuple

from src.host.device_registry import config_file

log = logging.getLogger(__name__)

# 缓存 TTL（秒）— IP 24小时内不会换国家
_CACHE_TTL = 86400

# API 超时（秒）
_API_TIMEOUT = 8

# 缓存文件路径（持久化缓存，跨进程共享）
_CACHE_FILE = config_file("ip_geo_cache.json")

# 内存缓存
_ip_cache: Dict[str, dict] = {}
_cache_lock = threading.Lock()

# 查询 API（按优先级）
_GEO_APIS = [
    # ip-api.com 免费版：45次/分钟，无需密钥
    {
        "url_tpl": "http://ip-api.com/json/{ip}?fields=status,country,countryCode,city,isp,org,as",
        "parser": lambda d: {
            "ip": d.get("query", ""),
            "country_code": d.get("countryCode", "").upper(),
            "country": d.get("country", ""),
            "city": d.get("city", ""),
            "isp": d.get("isp", ""),
            "org": d.get("org", ""),
            "ok": d.get("status") == "success",
        },
    },
    # ipwho.is 免费版：无限制（但速度慢）
    {
        "url_tpl": "https://ipwho.is/{ip}",
        "parser": lambda d: {
            "ip": d.get("ip", ""),
            "country_code": d.get("country_code", "").upper(),
            "country": d.get("country", ""),
            "city": d.get("city", ""),
            "isp": d.get("connection", {}).get("isp", ""),
            "org": d.get("connection", {}).get("org", ""),
            "ok": d.get("success", False),
        },
    },
    # ipinfo.io 免费版：50000次/月
    {
        "url_tpl": "https://ipinfo.io/{ip}/json",
        "parser": lambda d: {
            "ip": d.get("ip", ""),
            "country_code": d.get("country", "").upper(),
            "country": d.get("country", ""),
            "city": d.get("city", ""),
            "isp": d.get("org", ""),
            "org": d.get("org", ""),
            "ok": bool(d.get("country")),
        },
    },
]

# 国家别名映射（统一到 iso2 code）
_COUNTRY_ALIASES: Dict[str, str] = {
    "gb": "GB",   # ISO 3166-1 alpha-2 标准码
    "uk": "GB",   # uk 不是标准 ISO 码，统一映射到 GB
    "us": "US",
    "usa": "US",
    "cn": "CN",
    "korea": "KR",
    "kr": "KR",
    "jp": "JP",
    "japan": "JP",
    "br": "BR",
    "brazil": "BR",
    "de": "DE",
    "germany": "DE",
    "fr": "FR",
    "france": "FR",
    "it": "IT",
    "italy": "IT",
    "es": "ES",
    "spain": "ES",
    "nl": "NL",
    "netherlands": "NL",
    "au": "AU",
    "australia": "AU",
    "ca": "CA",
    "canada": "CA",
    "in": "IN",
    "india": "IN",
    "sg": "SG",
    "singapore": "SG",
    "id": "ID",
    "indonesia": "ID",
}


def _normalize_country(country: str) -> str:
    """将国家名/别名统一为 ISO 2 字母大写码。"""
    s = country.strip().lower()
    return _COUNTRY_ALIASES.get(s, s.upper())


def _load_cache():
    """从磁盘加载缓存（启动时调用一次）。"""
    global _ip_cache
    if _CACHE_FILE.exists():
        try:
            raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            now = time.time()
            # 过滤过期条目
            _ip_cache = {ip: info for ip, info in raw.items()
                         if now - info.get("cached_at", 0) < _CACHE_TTL}
            log.debug("[GeoIP] 从磁盘加载 %d 条缓存", len(_ip_cache))
        except Exception as e:
            log.debug("[GeoIP] 缓存加载失败（将重建）: %s", e)
            _ip_cache = {}


def _save_cache():
    """将内存缓存写入磁盘。"""
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _cache_lock:
            data = dict(_ip_cache)
        _CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception as e:
        log.debug("[GeoIP] 缓存保存失败: %s", e)


def _fetch_ip_geo(ip: str) -> Optional[dict]:
    """通过外部 API 查询 IP 地理信息（带备用切换）。"""
    for api in _GEO_APIS:
        url = api["url_tpl"].format(ip=ip)
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "OpenClaw/1.0"},
                method="GET",
            )
            resp = urllib.request.urlopen(req, timeout=_API_TIMEOUT)
            raw = json.loads(resp.read().decode())
            parsed = api["parser"](raw)

            if parsed.get("ok") and parsed.get("country_code"):
                parsed["cached_at"] = time.time()
                if not parsed.get("ip"):
                    parsed["ip"] = ip
                log.debug("[GeoIP] %s → %s %s (via %s)",
                          ip, parsed["country_code"], parsed.get("city", ""),
                          url.split("/")[2])
                return parsed
        except (urllib.error.URLError, OSError, Exception) as e:
            log.debug("[GeoIP] API %s 失败: %s", url.split("/")[2], e)
            continue

    log.warning("[GeoIP] 所有 API 均无法查询 %s", ip)
    return None


def lookup_ip_country(ip: str, force_refresh: bool = False) -> Optional[dict]:
    """查询 IP 的地理位置信息（优先读缓存，过期自动刷新）。

    Args:
        ip: IPv4 地址字符串
        force_refresh: 忽略缓存，强制重新查询

    Returns:
        {ip, country_code, country, city, isp, org, cached_at}
        或 None（查询完全失败）
    """
    if not ip:
        return None

    # 尝试从内存缓存读取
    if not force_refresh:
        with _cache_lock:
            cached = _ip_cache.get(ip)
        if cached:
            age = time.time() - cached.get("cached_at", 0)
            if age < _CACHE_TTL:
                return cached
            # 缓存过期，继续向下走

    # 调用外部 API
    result = _fetch_ip_geo(ip)
    if result:
        with _cache_lock:
            _ip_cache[ip] = result
        # 异步保存磁盘缓存（不阻塞主流程）
        threading.Thread(target=_save_cache, daemon=True).start()

    return result


def verify_ip_for_country(ip: str, expected_country: str) -> Tuple[bool, dict]:
    """验证 IP 是否在目标国家。

    Args:
        ip: 要验证的 IP 地址
        expected_country: 期望国家（支持: 'us', 'italy', 'uk', 'US', 'IT', 'GB' 等）

    Returns:
        (match: bool, geo_info: dict)
        match=True  表示 IP 确实在目标国家
        match=False 表示 IP 在其他国家，或查询失败
    """
    expected_code = _normalize_country(expected_country)
    info = lookup_ip_country(ip)

    if not info:
        return False, {
            "ip": ip,
            "error": "无法查询IP地理位置",
            "expected": expected_code,
            "actual": "unknown",
            "match": False,
        }

    actual_code = info.get("country_code", "").upper()
    # 标准化实际返回码（ip-api.com 返回 GB，但 ipinfo.io 有时返回 UK）
    if actual_code == "UK":
        actual_code = "GB"
    match = (actual_code == expected_code)

    return match, {
        "ip": ip,
        "expected": expected_code,
        "actual": actual_code,
        "country": info.get("country", ""),
        "city": info.get("city", ""),
        "isp": info.get("isp", ""),
        "match": match,
    }


def batch_verify_ips(ip_country_pairs: list) -> list:
    """批量验证多个 IP 的地理位置（并发）。

    Args:
        ip_country_pairs: [(ip, country), ...] 列表

    Returns:
        [(match, geo_info), ...] 列表，顺序与输入一致
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = [None] * len(ip_country_pairs)

    with ThreadPoolExecutor(max_workers=min(10, len(ip_country_pairs))) as pool:
        futs = {
            pool.submit(verify_ip_for_country, ip, country): idx
            for idx, (ip, country) in enumerate(ip_country_pairs)
        }
        for fut in as_completed(futs):
            idx = futs[fut]
            try:
                results[idx] = fut.result()
            except Exception as e:
                ip, country = ip_country_pairs[idx]
                results[idx] = (False, {"ip": ip, "error": str(e), "match": False})

    return results


# 启动时加载磁盘缓存
_load_cache()
