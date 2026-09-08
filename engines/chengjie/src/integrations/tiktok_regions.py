# -*- coding: utf-8 -*-
"""TikTok 账号地区模型（指令 TK-1 决策 D-TK-2，2026-09-08）——注册地决定官方能力可用性。

TikTok 与抖音的根本差别之一：TikTok 的官方私信 API（Business Messaging）**按 Business Account 的注册地**分区
开放，图片发送另有一张更长的禁用地区表，店铺客服按国家站点授权。不建模这一维度，就会在客户面前假装能接。

数据来源（核实日期 2026-09-08，见 docs/指令_TK-1 §2）：
- 私信 API 不可用：欧洲经济区 30 国 + 瑞士 + 英国（所有服务商一致）；美国（Infobip / respond.io / Wati / SleekFlow
  均标不可用，USDS 合资后数据隔离）；印度（无 TikTok）。
- 媒体（图片）发送不可用：澳大利亚、哥伦比亚、欧盟成员国、印度、伊朗、日本、尼日利亚、朝鲜、菲律宾、俄罗斯、
  韩国、土耳其、乌克兰、英国、美国（respond.io 渠道限制页）。
- 店铺站点：TikTok Shop 已开放卖家站点（Partner Center）。

纯数据 + 纯函数；地区码统一为 ISO-3166-1 alpha-2 大写。**未知地区返回 None（渲染「未知，需核实」），不默认可用。**
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, Optional

VERIFIED_ON = "2026-09-08"

EEA: FrozenSet[str] = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT",
    "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",   # 欧盟 27
    "IS", "LI", "NO",                                            # 欧洲经济区 +3
})
EU: FrozenSet[str] = frozenset(EEA - {"IS", "LI", "NO"})

#: 私信 API（Business Messaging）按注册地不可用
DM_API_UNAVAILABLE: FrozenSet[str] = frozenset(EEA | {"CH", "GB", "US", "IN"})

#: Open Beta 已明确覆盖的区域（用于「可用」判定：不在不可用表 且 在已知覆盖区）
DM_API_KNOWN_AVAILABLE: FrozenSet[str] = frozenset({
    # APAC
    "SG", "MY", "TH", "VN", "ID", "PH", "KH", "LA", "MM", "BN", "TW", "HK", "MO", "JP", "KR", "AU", "NZ", "PK", "BD",
    "LK", "NP",
    # LATAM
    "MX", "BR", "AR", "CL", "CO", "PE", "EC", "UY", "PY", "BO", "CR", "PA", "DO", "GT", "HN", "SV", "NI",
    # METAP（中东 / 土耳其 / 非洲 / 巴基斯坦）
    "AE", "SA", "QA", "KW", "BH", "OM", "JO", "LB", "IQ", "EG", "MA", "TR", "ZA", "NG", "KE", "GH", "TZ", "UG", "ET",
    "DZ", "TN",
    # 北美（美国除外）
    "CA",
})

#: 图片发送不可用（文本仍可）
MEDIA_SEND_UNAVAILABLE: FrozenSet[str] = frozenset(
    EU | {"AU", "CO", "IN", "IR", "JP", "NG", "KP", "PH", "RU", "KR", "TR", "UA", "GB", "US"}
)

#: TikTok Shop 卖家站点（店铺客服 API 按站点授权）
SHOP_SITES: Dict[str, str] = {
    "US": "US", "GB": "UK", "ID": "ID", "TH": "TH", "VN": "VN", "MY": "MY", "PH": "PH", "SG": "SG",
    "ES": "ES", "IE": "IE", "DE": "DE", "FR": "FR", "IT": "IT", "MX": "MX", "BR": "BR", "JP": "JP",
}

_ALIASES = {"UK": "GB", "USA": "US", "UAE": "AE", "KSA": "SA", "EL": "GR"}


def normalize_region(raw: Any) -> str:
    r = str(raw or "").strip().upper().replace(" ", "")
    if not r:
        return ""
    r = _ALIASES.get(r, r)
    return r if len(r) == 2 and r.isalpha() else ""


def dm_api_available(region: Any) -> Optional[bool]:
    """Business Messaging 私信 API 是否可用：True / False / None（未知地区，需核实）。"""
    r = normalize_region(region)
    if not r:
        return None
    if r in DM_API_UNAVAILABLE:
        return False
    if r in DM_API_KNOWN_AVAILABLE:
        return True
    return None


def media_send_allowed(region: Any) -> Optional[bool]:
    """图片消息能否发送（文本不受此限）。未知地区 None。"""
    r = normalize_region(region)
    if not r:
        return None
    if r in MEDIA_SEND_UNAVAILABLE:
        return False
    # 私信 API 都不可用的地区，媒体自然也谈不上；已知可用区默认可发图
    if r in DM_API_KNOWN_AVAILABLE:
        return True
    return None


def shop_site(region: Any) -> str:
    """店铺站点代码（无站点返回空串）。"""
    return SHOP_SITES.get(normalize_region(region), "")


def dm_block_reason(region: Any) -> str:
    """给接入向导 / worker 的原因码：``region_unsupported`` / ``region_unknown`` / 空串（可用）。"""
    ok = dm_api_available(region)
    if ok is True:
        return ""
    return "region_unsupported" if ok is False else "region_unknown"


def capabilities(region: Any) -> Dict[str, Any]:
    """一屏能力：给向导卡 / 账号卡渲染（含替代路径提示）。"""
    r = normalize_region(region)
    dm = dm_api_available(r)
    return {
        "region": r, "verified_on": VERIFIED_ON,
        "dm_api": dm, "media_send": media_send_allowed(r), "shop_site": shop_site(r),
        "reason": dm_block_reason(r),
        "alternatives": ([] if dm else ["messaging_ads", "shop_customer_service", "hosted_personal"]),
    }


__all__ = ["VERIFIED_ON", "EEA", "EU", "DM_API_UNAVAILABLE", "DM_API_KNOWN_AVAILABLE", "MEDIA_SEND_UNAVAILABLE",
           "SHOP_SITES", "normalize_region", "dm_api_available", "media_send_allowed", "shop_site",
           "dm_block_reason", "capabilities"]
