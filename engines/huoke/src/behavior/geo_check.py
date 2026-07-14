# -*- coding: utf-8 -*-
"""
Geo-IP Checker — verifies device IP matches target country.

Checks the device's public IP address via ADB and validates it against
the expected country from geo_strategy configuration.

Detection methods:
  1. ADB shell → curl ifconfig.me → public IP
  2. IP → country lookup via free APIs (ip-api.com, ipapi.co)
  3. Compare against target country → emit alert if mismatch

Integration:
  - Called before warmup_session starts
  - Emits EventBus alert on mismatch
  - Returns check result for logging/dashboard
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

from src.host.device_registry import DEFAULT_DEVICES_YAML

log = logging.getLogger(__name__)

# Country code → expected country names (for fuzzy matching)
COUNTRY_ALIASES: Dict[str, list] = {
    "italy": ["italy", "italia", "it"],
    "germany": ["germany", "deutschland", "de"],
    "france": ["france", "fr"],
    "spain": ["spain", "españa", "es"],
    "brazil": ["brazil", "brasil", "br"],
    "japan": ["japan", "jp"],
    "philippines": ["philippines", "philippine", "ph", "filipino"],
    "united states": ["united states", "usa", "us"],
    "united kingdom": ["united kingdom", "uk", "gb"],
}


@dataclass
class GeoCheckResult:
    device_id: str
    public_ip: str
    detected_country: str
    detected_country_code: str
    expected_country: str
    matches: bool
    vpn_detected: bool = False
    check_method: str = ""
    error: str = ""
    # Sprint 4 P2: 双源/三源交叉校验字段
    cross_checked: bool = False
    source_conflict: bool = False
    sources: Optional[list] = None


def check_device_geo(device_id: str, expected_country: str,
                     device_manager=None) -> GeoCheckResult:
    """
    Check if a device's IP matches the expected country.

    Uses ADB to get the device's public IP, then queries a free geo-IP API.
    """
    result = GeoCheckResult(
        device_id=device_id,
        public_ip="",
        detected_country="",
        detected_country_code="",
        expected_country=expected_country,
        matches=False,
    )

    if not device_manager:
        try:
            from ..device_control.device_manager import get_device_manager
            device_manager = get_device_manager(DEFAULT_DEVICES_YAML)
        except Exception as e:
            result.error = f"No device manager: {e}"
            return result

    # Step 1: Get public IP via ADB
    public_ip = _get_public_ip(device_id, device_manager)
    if not public_ip:
        result.error = "Could not determine public IP"
        return result

    result.public_ip = public_ip

    # Step 2: Geo-lookup the IP
    geo = _lookup_ip(public_ip)
    if not geo:
        result.error = "Geo-lookup failed"
        return result

    result.detected_country = geo.get("country", "")
    result.detected_country_code = geo.get("country_code", "")
    result.check_method = geo.get("method", "")
    result.cross_checked = bool(geo.get("_cross_checked"))
    result.source_conflict = bool(geo.get("_conflict"))
    result.sources = geo.get("sources") or []

    # Step 3: Check VPN indicators
    if geo.get("hosting", False) or geo.get("proxy", False):
        result.vpn_detected = True

    # Step 4: Compare
    expected_lower = expected_country.lower()
    detected_lower = result.detected_country.lower()
    code_lower = result.detected_country_code.lower()

    aliases = COUNTRY_ALIASES.get(expected_lower, [expected_lower])
    result.matches = (
        detected_lower in aliases or
        code_lower in aliases or
        expected_lower in detected_lower or
        detected_lower in expected_lower
    )

    cross_tag = (" [cross=✓]" if result.cross_checked
                 else " [cross=×]") + (" [conflict]" if result.source_conflict else "")
    if result.matches:
        log.info("[GeoCheck] %s: IP=%s → %s (%s) ✓ 匹配 %s%s",
                 device_id, public_ip, result.detected_country,
                 result.detected_country_code, expected_country, cross_tag)
    else:
        log.warning("[GeoCheck] %s: IP=%s → %s (%s) ✗ 不匹配 %s%s",
                    device_id, public_ip, result.detected_country,
                    result.detected_country_code, expected_country, cross_tag)
        _emit_geo_alert(device_id, result)

    return result


def _get_public_ip(device_id: str, dm) -> str:
    """Get the device's public IP address via ADB."""
    commands = [
        "shell curl -s --max-time 5 ifconfig.me",
        "shell curl -s --max-time 5 api.ipify.org",
        "shell curl -s --max-time 5 icanhazip.com",
    ]

    for cmd in commands:
        try:
            ok, output = dm.execute_adb_command(cmd, device_id)
            if ok and output:
                ip = output.strip()
                if _is_valid_ip(ip):
                    return ip
        except Exception:
            continue

    return ""


def _is_valid_ip(s: str) -> bool:
    """Basic check if string looks like an IPv4 address."""
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


_GEO_SOURCES = [
    {
        "name": "ip-api.com",
        "url_tpl": "http://ip-api.com/json/{ip}?fields=country,countryCode,hosting,proxy",
        "parser": lambda d: {
            "country": d.get("country", ""),
            "country_code": (d.get("countryCode", "") or "").upper(),
            "hosting": bool(d.get("hosting", False)),
            "proxy": bool(d.get("proxy", False)),
        },
    },
    {
        "name": "ipapi.co",
        "url_tpl": "https://ipapi.co/{ip}/json/",
        "parser": lambda d: {
            "country": d.get("country_name", ""),
            "country_code": (d.get("country_code", "") or "").upper(),
            "hosting": False,
            "proxy": False,
        },
    },
    {
        "name": "ipwhois.io",
        "url_tpl": "http://ipwho.is/{ip}",
        "parser": lambda d: {
            "country": d.get("country", ""),
            "country_code": (d.get("country_code", "") or "").upper(),
            "hosting": False,
            "proxy": False,
        },
    },
]


def _query_one_source(ip: str, source: dict, timeout: int = 6) -> Optional[dict]:
    """查一个 geo 源,返回 {country, country_code, ..., method} 或 None。"""
    import urllib.request
    url = source["url_tpl"].format(ip=ip)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "OpenClaw-GeoCheck/1.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            result = source["parser"](data)
            if result.get("country") or result.get("country_code"):
                result["method"] = source["name"]
                return result
    except Exception as e:
        log.debug("[GeoCheck] %s failed: %s", source["name"], e)
    return None


def _lookup_ip(ip: str, require_cross_check: bool = True) -> Optional[dict]:
    """多源并行查 IP,Sprint 4 P2 升级为交叉校验。

    策略:
      1. 并行查前两个源(ip-api + ipapi)
      2. 如果两源 country_code 相同 → 返回(high confidence)
      3. 如果两源不一致或只有一源返回 → 启用第三源(ipwho.is) 投票
      4. 三源里出现 ≥2 相同 country_code → 返回多数(high confidence)
      5. 全部不一致 → 返回最先响应的那个,并带 _conflict=True 警告
      6. 全部失败 → None

    返回 dict 额外字段:
      sources: [{"method","country","country_code"}]  — 所有成功源
      _cross_checked: bool
      _conflict: bool (三源互不一致时 True)
    """
    import concurrent.futures
    import time as _t

    # Sprint 5 P1: 总耗时硬封顶 10s(primary 最多 ~6s,第三源剩余预算)。
    _GEO_TOTAL_BUDGET_S = 10.0
    _GEO_MIN_THIRD_S = 1.5
    deadline = _t.time() + _GEO_TOTAL_BUDGET_S

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(_query_one_source, ip, s)
                   for s in _GEO_SOURCES[:2]]
        primary = []
        for f in futures:
            try:
                remaining = max(0.5, deadline - _t.time())
                primary.append(f.result(timeout=remaining))
            except concurrent.futures.TimeoutError:
                log.warning("[GeoCheck] primary source timeout after budget")
                primary.append(None)
            except Exception as e:
                log.debug("[GeoCheck] primary source error: %s", e)
                primary.append(None)

    valid = [r for r in primary if r and r.get("country_code")]
    sources_list = [r for r in primary if r]

    def _pack(winner: dict, cross: bool, conflict: bool = False) -> dict:
        return {
            **winner,
            "sources": [{"method": s["method"],
                         "country": s.get("country", ""),
                         "country_code": s.get("country_code", "")}
                        for s in sources_list],
            "_cross_checked": cross,
            "_conflict": conflict,
        }

    if len(valid) == 2 and valid[0]["country_code"] == valid[1]["country_code"]:
        # 两源一致 → 高置信
        return _pack(valid[0], cross=True)

    if require_cross_check and (len(valid) < 2 or
                                 valid[0]["country_code"] != valid[1]["country_code"]):
        # 两源矛盾或其一失败 → 启用第三源投票
        remaining_budget = deadline - _t.time()
        if remaining_budget < _GEO_MIN_THIRD_S:
            log.warning("[GeoCheck] 预算不足(剩 %.1fs < %.1fs),跳过第三源",
                        remaining_budget, _GEO_MIN_THIRD_S)
            if valid:
                return _pack(valid[0], cross=False,
                             conflict=(len(valid) >= 2 and
                                       valid[0]["country_code"] != valid[1]["country_code"]))
            return None
        third_to = max(2, int(min(6, remaining_budget)))
        third = _query_one_source(ip, _GEO_SOURCES[2], timeout=third_to)
        if third and third.get("country_code"):
            sources_list.append(third)
            all_valid = valid + [third]
            tally: Dict[str, int] = {}
            for r in all_valid:
                cc = r["country_code"]
                tally[cc] = tally.get(cc, 0) + 1
            winner_cc, winner_count = max(tally.items(), key=lambda kv: kv[1])
            if winner_count >= 2:
                winner = next(r for r in all_valid if r["country_code"] == winner_cc)
                return _pack(winner, cross=True,
                             conflict=(len(tally) > 1))
            # 三源各不同 → 3-way conflict,保守返回第一个但标记 conflict
            log.warning("[GeoCheck] 三源 geo 结果全不一致 ip=%s tally=%s",
                        ip, tally)
            return _pack(all_valid[0], cross=True, conflict=True)

    # 只一个源返回
    if valid:
        return _pack(valid[0], cross=False)

    return None


def _emit_geo_alert(device_id: str, result: GeoCheckResult):
    """Emit EventBus alert for country mismatch."""
    try:
        from ..workflow.event_bus import get_event_bus
        bus = get_event_bus()
        bus.emit_simple(
            "device.geo_mismatch",
            source="geo_check",
            device_id=device_id,
            public_ip=result.public_ip,
            detected_country=result.detected_country,
            expected_country=result.expected_country,
            vpn_detected=result.vpn_detected,
        )
    except Exception:
        pass


# ── Batch check ──

def check_all_devices(expected_country: str,
                      device_ids: Optional[list] = None) -> list:
    """Check geo for all connected devices."""
    try:
        from ..device_control.device_manager import get_device_manager
        dm = get_device_manager(DEFAULT_DEVICES_YAML)

        if not device_ids:
            device_ids = [d["device_id"] for d in dm.list_devices()
                          if d.get("status") == "online"]

        results = []
        for did in device_ids:
            r = check_device_geo(did, expected_country, dm)
            results.append({
                "device_id": r.device_id,
                "public_ip": r.public_ip,
                "detected_country": r.detected_country,
                "expected_country": r.expected_country,
                "matches": r.matches,
                "vpn_detected": r.vpn_detected,
                "error": r.error,
            })

        return results
    except Exception as e:
        log.error("[GeoCheck] Batch check failed: %s", e)
        return []
