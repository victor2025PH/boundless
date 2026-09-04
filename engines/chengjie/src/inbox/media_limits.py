"""出站媒体体积上限——**按平台**的单一事实源（#169，2026-09-05）。

事故：钧机 AMKK5Z/WQYPPX「line 发送视频超过 25mb 的视频无法发送」——上限来自
``unified_inbox_send_routes._media_cap_mb``：``inbox.media.limits_mb.{platform}`` →
``.default`` → **25**。``config.example.yaml`` 里虽然写着 ``line: 100``，但装机包与
生产实例的 ``config.yaml`` / ``config.local.yaml`` 都**没有** ``limits_mb`` 段，于是四个
平台一律 25MB；LINE 客户端自己的视频上限是 200MB，坐席手机随手拍的一分钟视频就超。

修法＝**内建平台默认表**（``limits_mb`` 缺该平台键时生效），数字来源：

- ``line``：2026-09-05 117 号真机三档实测（``tools/probe_line_media.py --send-self
  --kind video --file … --max-mb 120 --confirm``，发给自己的 Keep memo 并回读 magic）。
  详见 ``docs/发版对账_v1.0.74_J3.md`` C 段实测表。
- ``telegram`` 200 / ``whatsapp`` 64：平台公开上限（TG Bot API 文档 / WA 官方媒体上限）。
- 其它平台仍 25（旧行为，``messenger`` 网页链路没实测过，不放开）。

解析顺序（``platform_media_cap_mb``）::

    limits_mb.{platform}（运营显式配） → 内建平台表 → limits_mb.default → 25

刻意让**内建平台表优先于 ``limits_mb.default``**：``default`` 的语义是「没有更具体
信息时的兜底」，而内建表就是更具体的信息（真机实测/官方文档）。运营要压某平台
就显式写 ``limits_mb.line: 20``，写 ``default`` 只影响表外平台。

两处消费方必须同源：``unified_inbox_send_routes``（413 + ``send-caps.media_max_mb``
给前端预检）与 ``line_media.resolve_line_media_cfg``（LINE worker 的
``outbound_max_bytes`` 缺省）。修前后者独立硬编码 20MB，比路由的 25 还低——路由放行的
20~25MB 文件到了 worker 才被 ``media_too_large`` 拒掉，坐席看到的是一句
「未送达（media_too_large）」而不是 413 的上限文案。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

MB = 1024 * 1024

#: 未配置、且平台不在内建表里时的兜底（旧行为）。
DEFAULT_MEDIA_CAP_MB = 25

#: 内建平台默认（MB）。``line`` 按真机实测（见模块头）。
PLATFORM_MEDIA_CAP_MB: Dict[str, int] = {
    "line": 100,
    "telegram": 200,
    "whatsapp": 64,
}

#: 夹紧区间——防 ``limits_mb`` 误配成 0 / 负数 / 几十 GB。
_CAP_MIN_MB = 1
_CAP_MAX_MB = 2048


def _limits_section(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        lim = (((config or {}).get("inbox") or {}).get("media") or {}).get("limits_mb")
    except Exception:
        return {}
    return lim if isinstance(lim, dict) else {}


def _clamp(v: Any, fallback: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return fallback
    return max(_CAP_MIN_MB, min(_CAP_MAX_MB, n))


def platform_media_cap_mb(config: Optional[Dict[str, Any]], platform: str) -> int:
    """该平台的出站媒体体积上限（MB）。见模块头的解析顺序。绝不抛。"""
    plat = str(platform or "").strip().lower()
    lim = _limits_section(config)
    v = lim.get(plat)
    if v is not None:
        return _clamp(v, DEFAULT_MEDIA_CAP_MB)
    builtin = PLATFORM_MEDIA_CAP_MB.get(plat)
    if builtin is not None:
        return builtin
    v = lim.get("default")
    if v is not None:
        return _clamp(v, DEFAULT_MEDIA_CAP_MB)
    return DEFAULT_MEDIA_CAP_MB


def platform_media_cap_bytes(config: Optional[Dict[str, Any]], platform: str) -> int:
    return platform_media_cap_mb(config, platform) * MB


def describe_over_cap(size_bytes: int, cap_mb: int) -> Dict[str, Any]:
    """413 文案参数：``{size, cap, over}``（MB，一位小数；``size`` 未知回 0）。

    路由是**流式落盘、超限即停**（峰值内存恒定），停下时只知道「超了」不知道
    实际总大小；调用方拿 ``Content-Length`` 请求头当近似值（multipart 包裹开销
    对几十 MB 的文件是千分位噪音）。``over`` 是「还差多少」——坐席看一眼就知道
    该压多少，而不是对着一个上限数字猜。
    """
    try:
        size_mb = round(max(0, int(size_bytes or 0)) / MB, 1)
    except (TypeError, ValueError):
        size_mb = 0.0
    cap = int(cap_mb or DEFAULT_MEDIA_CAP_MB)
    over = round(max(0.0, size_mb - cap), 1) if size_mb > 0 else 0.0
    return {"size": size_mb, "cap": cap, "over": over}
