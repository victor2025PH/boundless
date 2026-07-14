"""云端凭证健康探针 —— 首期只盯 DeepSeek 账户余额水位（防「扣完才知道」）。

背景：主对话/翻译走 DeepSeek 云（预付费）。余额耗尽后 API 直接拒绝调用，
虽有本地兜底顶班出话，但机主若不知情会一直烧兜底、云端能力全失。
本模块把「余额还剩多少」变成可周期巡检的信号：低于阈值 → HealthWatchdog
弹主机告警（host_alert，仅算力机弹窗 + EventBus 远程镜像）。

设计：纯函数（target/classify）与网络探针（probe，TTL 缓存）分离，全部软失败。
配置 ``ops.cloud_credentials``（新子系统默认 enabled:false，本机 overlay 打开）::

    ops:
      cloud_credentials:
        enabled: true
        balance_warn_cny: 20        # 余额低于该值（CNY）告警
        probe_interval_sec: 3600    # 余额巡检间隔（水位变化慢，1h 足够）
        remind_sec: 21600           # 低水位重提冷却（6h 一条，直到充值恢复）

扩展预留：未来多云 Key 备用池（ai.key_pool）时，本模块按凭证列表逐个探活，
``summarize`` 输出天然是列表口径。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_PLACEHOLDER_PREFIX = "YOUR_"


def credentials_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """取 ``ops.cloud_credentials`` 段（缺省安全默认）。"""
    ops = (config.get("ops") or {}) if isinstance(config, dict) else {}
    cc = ops.get("cloud_credentials") or {}
    return {
        "enabled": bool(cc.get("enabled", False)),
        "balance_warn_cny": float(cc.get("balance_warn_cny", 20) or 20),
        "probe_interval_sec": max(300.0, float(cc.get("probe_interval_sec", 3600) or 3600)),
        "remind_sec": max(600.0, float(cc.get("remind_sec", 21600) or 21600)),
    }


def deepseek_balance_target(config: Dict[str, Any]) -> Dict[str, str]:
    """决策：该不该探**主 Key** 的 DeepSeek 余额、探哪个 URL（纯函数）。

    仅当主对话 provider=openai_compatible、base_url 指向 DeepSeek、且 key 已真实
    配置（非空/非 YOUR_* 占位）时返回 ``{url, api_key}``；其余返回空 dict 不探
    （本地 Ollama / 其他云无此契约，探了必误报）。
    """
    ai = (config.get("ai") or {}) if isinstance(config, dict) else {}
    if str(ai.get("provider") or "").strip().lower() != "openai_compatible":
        return {}
    base = str(ai.get("base_url") or "").strip().rstrip("/")
    if "deepseek" not in base.lower() or "://" not in base:
        return {}
    key = str(ai.get("api_key") or "").strip()
    if not key or key.upper().startswith(_PLACEHOLDER_PREFIX):
        return {}
    root = base[:-3] if base.endswith("/v1") else base
    return {"url": root + "/user/balance", "api_key": key}


def balance_targets(config: Dict[str, Any]) -> list:
    """全部可探余额的凭证（纯函数）：主 Key + ``ai.key_pool`` 里指向 DeepSeek 的备用 Key。

    备用 Key 悄悄欠费是最阴的坑——等主 Key 挂了才发现备用也是空的。池条目缺省
    继承主链 base_url（与 AIClient 解析一致）；只认 DeepSeek 契约端点；
    按 (url, api_key) 去重（主/池重复配置只探一次）。
    返回 ``[{name, url, api_key}]``，主 Key 恒为 name="DeepSeek"、排首位。
    """
    out: list = []
    seen: set = set()
    primary = deepseek_balance_target(config)
    if primary:
        out.append({"name": "DeepSeek", **primary})
        seen.add((primary["url"], primary["api_key"]))
    ai = (config.get("ai") or {}) if isinstance(config, dict) else {}
    kp = ai.get("key_pool") or {}
    if not (isinstance(kp, dict) and kp.get("enabled", True)):
        return out
    for i, item in enumerate(kp.get("keys") or []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("api_key") or "").strip()
        if not key or key.upper().startswith(_PLACEHOLDER_PREFIX):
            continue
        base = str(item.get("base_url") or ai.get("base_url") or "").strip().rstrip("/")
        if "deepseek" not in base.lower() or "://" not in base:
            continue
        root = base[:-3] if base.endswith("/v1") else base
        url = root + "/user/balance"
        if (url, key) in seen:
            continue
        seen.add((url, key))
        name = str(item.get("name") or f"key{i + 1}").strip()
        out.append({"name": f"备用:{name}", "url": url, "api_key": key})
    return out


def probe_deepseek_balance(url: str, api_key: str, *, timeout_sec: float = 8.0) -> Dict[str, Any]:
    """打 DeepSeek 余额接口。永不抛异常；结果里区分「网络不可达」与「通了但被拒」。"""
    result: Dict[str, Any] = {"reachable": False, "http_status": 0, "balances": {}}
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        result["reachable"] = True
        result["http_status"] = 200
        result["available"] = bool(data.get("is_available", True))
        for b in (data.get("balance_infos") or []):
            if not isinstance(b, dict):
                continue
            cur = str(b.get("currency") or "").strip().upper()
            try:
                result["balances"][cur] = float(b.get("total_balance") or 0.0)
            except (TypeError, ValueError):
                continue
    except urllib.error.HTTPError as e:
        # 通了但被拒：401/403 = key 坏（与网络故障是两种病，分开报）
        result["reachable"] = True
        result["http_status"] = int(e.code or 0)
        result["error"] = f"HTTP {e.code}"
    except Exception as e:
        result["error"] = str(e)[:120]
    return result


def classify_balance(probe: Optional[Dict[str, Any]], warn_threshold: float,
                     *, currency: str = "CNY") -> Dict[str, Any]:
    """把探针结果分级（纯函数）：ok / low / auth_failed / unreachable / unknown。

    余额取阈值同币种（默认 CNY）；该币种缺失时回退任意非零币种（并随结果
    标注实际币种），全零/无数据 → unknown（不装绿也不误报 low）。
    """
    out: Dict[str, Any] = {"status": "unknown", "balance": None, "currency": currency,
                           "threshold": float(warn_threshold)}
    if not probe:
        return out
    if probe.get("error") and not probe.get("reachable"):
        out["status"] = "unreachable"
        out["error"] = probe.get("error")
        return out
    if int(probe.get("http_status") or 0) in (401, 403):
        out["status"] = "auth_failed"
        out["error"] = probe.get("error") or f"HTTP {probe.get('http_status')}"
        return out
    balances = probe.get("balances") or {}
    bal = balances.get(currency)
    if bal is None:
        for cur, v in balances.items():
            if v:
                bal, out["currency"] = float(v), cur
                break
    if bal is None:
        return out
    out["balance"] = float(bal)
    out["status"] = "low" if float(bal) < float(warn_threshold) else "ok"
    return out


# 探针 TTL 缓存：余额变化慢且接口计入厂商限流，看板/巡检共用同一次探测结果。
# result 存「全部凭证的 summary 列表」（主 Key + 备用池），单 Key 部署即单元素列表。
_BALANCE_CACHE: Dict[str, Any] = {"ts": 0.0, "sig": "", "result": None}


def collect_cloud_balances(config: Dict[str, Any], *, force: bool = False,
                           ttl_sec: float = 600.0) -> list:
    """探测 + 分级一站式（主 Key + 备用池全部 DeepSeek 凭证，带 TTL 缓存）。

    返回 summary 列表（每项含 provider/status/balance/...）；未启用/无可探凭证
    返回空列表（不该告警）。
    """
    cc = credentials_config(config)
    if not cc["enabled"]:
        return []
    targets = balance_targets(config)
    if not targets:
        return []
    sig = "|".join(f"{t['url']}#{t['api_key'][:8]}" for t in targets)
    now = time.time()
    if (not force and _BALANCE_CACHE["result"] is not None
            and _BALANCE_CACHE["sig"] == sig
            and now - _BALANCE_CACHE["ts"] < max(60.0, ttl_sec)):
        return _BALANCE_CACHE["result"]
    out = []
    for t in targets:
        probe = probe_deepseek_balance(t["url"], t["api_key"])
        summary = classify_balance(probe, cc["balance_warn_cny"])
        summary["provider"] = t["name"]
        summary["remind_sec"] = cc["remind_sec"]
        out.append(summary)
    _BALANCE_CACHE.update({"ts": now, "sig": sig, "result": out})
    return out


def collect_deepseek_balance(config: Dict[str, Any], *, force: bool = False,
                             ttl_sec: float = 600.0) -> Optional[Dict[str, Any]]:
    """兼容入口：只取主 Key 的余额 summary。返回 None = 未启用/不适用。"""
    for summary in collect_cloud_balances(config, force=force, ttl_sec=ttl_sec):
        if summary.get("provider") == "DeepSeek":
            return summary
    return None


# ── 备用 Key 主动探活（chat ping）────────────────────────────────
#
# 余额巡检只覆盖 DeepSeek 系 key 的「欠费」维度；备用 key 还可能「被封/装错端点/
# 模型名失效」——这些只有真打一次 /chat/completions 才暴露。池 key 平时无流量，
# 「主 Key 挂了、切过去才发现备用也坏」的窗口靠每日一次 1-token ping 关掉。
# 主 Key 不 ping：生产流量 + 启动连接测试 + 余额巡检已全覆盖，别多花一次调用。

_PING_STATE: Dict[str, Dict[str, Any]] = {}   # name -> {ts, ok, http_status, error, latency_ms}
_PING_LAST_RUN: Dict[str, float] = {"ts": 0.0}


def chat_ping_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """``ops.cloud_credentials.chat_ping`` 段（缺省安全默认：随父开关，日探一次）。"""
    ops = (config.get("ops") or {}) if isinstance(config, dict) else {}
    cp = ((ops.get("cloud_credentials") or {}).get("chat_ping")) or {}
    return {
        "enabled": bool(cp.get("enabled", True)),
        "interval_sec": max(3600.0, float(cp.get("interval_sec", 86400) or 86400)),
    }


def chat_ping_targets(config: Dict[str, Any]) -> list:
    """池内全部备用 key（不限厂商——任意 OpenAI 兼容端点都答 /chat/completions）。

    返回 ``[{name, base_url, api_key, model}]``；池未启用/为空 → []。
    条目缺省继承主链 base_url/model（与 AIClient 解析一致），按 (base,key) 去重。
    """
    ai = (config.get("ai") or {}) if isinstance(config, dict) else {}
    kp = ai.get("key_pool") or {}
    if not (isinstance(kp, dict) and kp.get("enabled", True)):
        return []
    out: list = []
    seen: set = set()
    primary_key = str(ai.get("api_key") or "").strip()
    primary_base = str(ai.get("base_url") or "").strip().rstrip("/")
    for i, item in enumerate(kp.get("keys") or []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("api_key") or "").strip()
        if not key or key.upper().startswith(_PLACEHOLDER_PREFIX):
            continue
        base = str(item.get("base_url") or ai.get("base_url") or "").strip().rstrip("/")
        if not base or "://" not in base:
            continue
        if not base.endswith("/v1") and "deepseek" in base.lower():
            base = base + "/v1"
        if (base, key) in seen or (key == primary_key and base.rstrip("/") in
                                   (primary_base, primary_base + "/v1")):
            continue
        seen.add((base, key))
        model = str(item.get("model") or ai.get("model") or "").strip()
        name = str(item.get("name") or f"key{i + 1}").strip()
        out.append({"name": name, "base_url": base, "api_key": key, "model": model})
    return out


def probe_chat_key(base_url: str, api_key: str, model: str,
                   *, timeout_sec: float = 15.0) -> Dict[str, Any]:
    """对单个备用 key 打一次 1-token chat 探活。永不抛异常。

    结果语义与余额探针对齐：``reachable``（TCP/HTTP 层通了）+ ``ok``（真出了 200）；
    401/402/403 = key 坏（reachable 但 auth 层拒绝）。成本：1 token 输出，忽略不计。
    """
    result: Dict[str, Any] = {"ok": False, "reachable": False, "http_status": 0}
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }).encode("utf-8")
    t0 = time.time()
    try:
        req = urllib.request.Request(url, data=payload, method="POST", headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            resp.read()
            result.update({"ok": True, "reachable": True,
                           "http_status": int(resp.status or 200)})
    except urllib.error.HTTPError as e:
        result.update({"reachable": True, "http_status": int(e.code or 0),
                       "error": f"HTTP {e.code}"})
        try:
            body = e.read().decode("utf-8", "replace")[:200]
            if body:
                result["error"] = f"HTTP {e.code}: {body}"
        except Exception:
            pass
    except Exception as e:
        result["error"] = str(e)[:120]
    result["latency_ms"] = int((time.time() - t0) * 1000)
    return result


def run_chat_pings(config: Dict[str, Any], *, force: bool = False,
                   now: Optional[float] = None) -> list:
    """按节流跑一轮池 key 探活，返回 ``[{name, ok, http_status, error, ...}]``。

    间隔内重复调用返回缓存结果（不重复打端点）；未启用/空池 → []。
    结果同时存进模块级 ``_PING_STATE`` 供看板读取。
    """
    cc = credentials_config(config)
    cp = chat_ping_config(config)
    if not (cc["enabled"] and cp["enabled"]):
        return []
    targets = chat_ping_targets(config)
    if not targets:
        return []
    ts = float(now if now is not None else time.time())
    if not force and _PING_LAST_RUN["ts"] and (ts - _PING_LAST_RUN["ts"]) < cp["interval_sec"]:
        return [dict(v, name=k) for k, v in _PING_STATE.items()]
    _PING_LAST_RUN["ts"] = ts
    out = []
    for t in targets:
        r = probe_chat_key(t["base_url"], t["api_key"], t["model"])
        r["ts"] = ts
        _PING_STATE[t["name"]] = r
        out.append(dict(r, name=t["name"]))
    return out


def ping_state_snapshot() -> Dict[str, Dict[str, Any]]:
    """看板读取用：最近一次探活结果（按 key 名）。"""
    return {k: dict(v) for k, v in _PING_STATE.items()}
