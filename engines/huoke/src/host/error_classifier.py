"""任务失败原因归类 — Phase 2 P1 后端 normalize.

把 raw `last_error` 文本映射到稳定的 (layer, code, msg, tone, emoji) 五元组,
让前端不再用脆弱的 regex 匹配, 跨边界 (sibling 改业务方法时) 也不会破.

## 业务方约定 (跨边界 contract — 修改本文件需 @对方 review)

业务方法 raise / 写入 last_error 时, **请优先用以下文本片段** 让分类引擎能识别:

| Layer | Code | 文本片段示例 (regex 见下方 _RULES) |
|-------|------|-----------------------------------|
| infra | vpn_no_ip | "无法访问外网" / "[gate] 预检未通过 (network)" |
| infra | adb_offline | "adb offline" / "device not found" |
| quota | rate_limited | "quota exceeded" / "in hourly window" |
| business | vision_join_button_miss | "加入群组失败" / "join_group ... fail" / "无法找到 ... Join" |
| business | vision_search_bar_miss | "找不到搜索框" / "search bar miss" |
| business | group_already_joined | "已加入" / "already in group" |
| business | group_not_found | "群不存在" / "group not found" |
| timing | sla_timeout | "SLA timeout" / "30min 无业务" |
| timing | task_timeout | "timeout" / "超时" |
| safety | circuit_breaker | "circuit ... open" / "熔断" |

新加 layer/code 时:
1. 在 `_RULES` 顶部加新规则 (顺序: 最具体 → 最一般)
2. 更新本 docstring 表格
3. 更新前端 fallback `tasks-chat.js::_attributeError` (本类挂时的兜底)
4. 加 unit test 覆盖

## API

```python
from src.host.error_classifier import classify_task_error

cls = classify_task_error("facebook_join_group quota exceeded for []: 3/3")
# {'layer': 'quota', 'code': 'rate_limited',
#  'msg': '配额到 (等下个 window)', 'tone': 'amber', 'emoji': '⏰'}

classify_task_error("")     # → None
classify_task_error(None)   # → None
```
"""
from __future__ import annotations

import re
from typing import Optional


# 规则顺序: 最具体 → 最一般. 第一个匹配胜出.
# 每条 (regex, layer, code, msg, tone, emoji, fix_action)
# fix_action: 前端 UI 据此渲染「一键修复」按钮，可选值见 _FIX_ACTIONS。
_RULES: list[tuple[re.Pattern, str, str, str, str, str, str]] = [
    # ── quota / rate limit (最具体先) ──
    (re.compile(r"quota exceeded|in hourly window", re.I),
     "quota", "rate_limited", "配额到 (等下个 window)", "amber", "⏰", "wait_window"),

    # ── infra: 代理 / 网络细分 ──
    # P0-2: 拆 vpn_no_ip 为三态 — proxy_hijack / network_zero / network_timeout
    # 顺序：具体的 hijack/zero/timeout 必须在通用 "无法访问外网" 之前
    (re.compile(r"代理路径异常|代理.*被劫持|HTTP=.*'?(301|302|403|418|451)'?", re.I),
     "infra", "proxy_hijack",
     "代理 IP 被劫持/封禁 (建议换 IP)", "red", "🚫", "rotate_ip"),
    (re.compile(r"完全无外网|三路全失败|HTTP/ICMP/IP", re.I),
     "infra", "network_zero",
     "彻底断网 (检查 SIM/Wi‑Fi/USB)", "red", "📡", "reconnect_usb"),
    (re.compile(r"网络检查超时|USB 稳定性|adb.*timeout", re.I),
     "infra", "adb_timeout",
     "USB/adb 抖动 (重新插拔)", "red", "🔌", "reconnect_usb"),
    # 兜底：旧版日志 + 业务方还在用 "无法访问外网" 的报错
    (re.compile(r"无法访问外网|\[gate\]\s*预检未通过.*network", re.I),
     "infra", "vpn_no_ip", "VPN 不通 (修网络再派)", "red", "🔌", "rotate_ip"),
    (re.compile(r"adb\s+offline|device not found|adb.*not.*online", re.I),
     "infra", "adb_offline", "设备 adb 离线", "red", "📵", "reconnect_usb"),

    # ── business (具体 vision/group 错误优先) ──
    (re.compile(r"已加入|already in group|already a member", re.I),
     "business", "group_already_joined", "群已加入 (跳过)", "amber", "✅", ""),
    (re.compile(r"群不存在|group not found|no such group", re.I),
     "business", "group_not_found", "群不存在 / 已被封", "red", "🚫", ""),
    (re.compile(r"加入群组失败|join_group.*fail|无法找到.*Join", re.I),
     "business", "vision_join_button_miss",
     "Vision 找不到 Join 按钮 (重试可能修复)", "red", "❌", "smart_retry"),
    (re.compile(r"找不到搜索框|search bar miss|search bar not found", re.I),
     "business", "vision_search_bar_miss",
     "Vision 找不到搜索框 (FB 可能误入 Messenger)", "red", "🔎", "smart_retry"),

    # ── safety (熔断) ──
    (re.compile(r"circuit.*open|熔断", re.I),
     "safety", "circuit_breaker", "熔断保护 (设备/路由器异常)", "amber", "🚧", "diagnose"),

    # ── timing (SLA 比 plain timeout 具体) ──
    (re.compile(r"SLA.*(timeout|abort)|30min.*无业务", re.I),
     "timing", "sla_timeout", "SLA 超时 (30min 无业务事件)", "amber", "⏱️", "smart_retry"),
    (re.compile(r"timeout|超时", re.I),
     "timing", "task_timeout", "超时", "amber", "⏳", "smart_retry"),
]


# fix_action 枚举（前端按钮渲染据此 + i18n）
# 空字符串 = 不展示一键修复（如 group_already_joined / group_not_found 这类无意义重试）
#
# 端点契约（实装位置见 src/host/routers/）：
# - rotate_ip       → routers/devices_health.py::device_proxy_rotate (POST, 调 vpn_manager.reconnect_vpn_silent)
# - reconnect_usb   → routers/devices_core.py::reconnect_device     (POST, 已有: adb -s X reconnect)
# - smart_retry     → routers/tasks.py::retry_task_endpoint         (POST, 新增: 拿原 task 复制重派 + invalidate cache)
# - diagnose        → routers/devices_health.py::device_diagnose    (GET, 已有)
# - wait_window     → routers/tasks.py::DELETE /tasks/{task_id}     (软删=移入回收站)
_FIX_ACTIONS: dict = {
    "rotate_ip":     {"label": "🔄 换 IP 重试",   "endpoint": "/devices/{device_id}/proxy/rotate", "method": "POST", "needs": ("device_id",)},
    "reconnect_usb": {"label": "🔌 重连 USB",     "endpoint": "/devices/{device_id}/reconnect",     "method": "POST", "needs": ("device_id",)},
    "smart_retry":   {"label": "🔁 智能重试",     "endpoint": "/tasks/{task_id}/retry",             "method": "POST", "needs": ("task_id",)},
    "diagnose":      {"label": "🩺 诊断",         "endpoint": "/devices/{device_id}/diagnose",      "method": "GET",  "needs": ("device_id",)},
    "wait_window":   {"label": "⏰ 移入回收站",   "endpoint": "/tasks/{task_id}",                   "method": "DELETE", "needs": ("task_id",)},
}


def get_fix_action(action_key: str) -> Optional[dict]:
    """前端调用 / unit test 可用，返回 fix_action 元数据或 None。"""
    if not action_key:
        return None
    return _FIX_ACTIONS.get(action_key)


def classify_task_error(text: Optional[str]) -> Optional[dict]:
    """归类 last_error 文本到 (layer, code, msg, tone, emoji).

    返回 dict 字段:
    - layer: 'infra' | 'quota' | 'business' | 'safety' | 'timing' | 'unknown'
    - code: 稳定的标识符 (前端 i18n / 聚合分组 key)
    - msg: 用户可读的中文短句
    - tone: 'red' | 'amber' | 'green' (UI 色调)
    - emoji: 1 字符视觉锚 (跟前端 _attributeError 对齐)

    None / 空字符串 / 全空白 → 返 None (调用方按需省略字段).
    无规则匹配 → layer='unknown', code='unclassified', msg=截断 60 字.
    """
    if not text or not str(text).strip():
        return None
    t = str(text)
    for pat, layer, code, msg, tone, emoji, fix_action in _RULES:
        if pat.search(t):
            return {
                "layer": layer,
                "code": code,
                "msg": msg,
                "tone": tone,
                "emoji": emoji,
                "fix_action": fix_action,
            }
    return {
        "layer": "unknown",
        "code": "unclassified",
        "msg": t[:60] + ("…" if len(t) > 60 else ""),
        "tone": "red",
        "emoji": "⚠️",
        "fix_action": "diagnose",
    }
