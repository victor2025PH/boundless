"""客户端 ←→ 官网试用台账的桥接（P2 最后一段）。

整条链里本模块负责客户这一端：

    首启向导「注册领 7 天」→ claim(联系方式) → 官网按机器码建单
    → poll() 轮询 → 厂商机签好后取回 license → 本地验签落盘激活
    → 「加客服领 10 万字符」→ bind_code() 出深链 → 客服核销 → poll() 取回加量凭证入账

设计取舍：

* **机器指纹在本地算、只把结果发出去**。指纹本身是不可逆摘要（见
  platform/licensing/machine_id.py），官网只拿它做去重键，不反推硬件信息。
* **状态落 ``config/trial_claim.json``**——删掉它不会让人白拿第二份：官网按指纹
  去重，同一台机器再领拿回的是同一条单子。所以这个文件是「便利」不是「防线」。
* **HTTP 全部可注入**（``fetch`` 参数）：路由层能在无网络的测试里跑完整状态机。
* **任何一步失败都不抛**，统一返回 ``{ok: False, error: <code>}``——领试用失败不该
  让首启向导崩掉，用户还有「粘贴授权码」这条老路。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_SITE = "https://bd2026.cc"
STATE_FILENAME = "trial_claim.json"
HTTP_TIMEOUT = 12

#: 注入点：(url, method, body) -> dict。测试与离线环境替换此函数即可。
Fetch = Callable[[str, str, Optional[dict]], Dict[str, Any]]


def _http(url: str, method: str = "GET", body: Optional[dict] = None) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("accept", "application/json")
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8") or "{}") or {"ok": False, "error": f"http_{e.code}"}
        except Exception:
            return {"ok": False, "error": f"http_{e.code}"}
    except Exception as e:  # noqa: BLE001 - 网络层任何异常都收敛成错误码
        logger.debug("[trial-claim] 请求失败 %s: %s", url, e)
        return {"ok": False, "error": "network"}


# ── 配置 / 状态 ────────────────────────────────────────────────────────────

def site_url(config: Optional[dict] = None) -> str:
    cfg = ((config or {}).get("licensing") or {}).get("trial") or {}
    return str(cfg.get("site_url") or DEFAULT_SITE).rstrip("/")


def state_path(config: Optional[dict] = None) -> Path:
    """状态文件路径。跟随 license.key 所在目录，安装版即用户可写的数据目录。"""
    try:
        from src.licensing import get_license_manager
        p = get_license_manager().license_path
        if p:
            return Path(p).parent / STATE_FILENAME
    except Exception:
        pass
    return Path("config") / STATE_FILENAME


def load_state(config: Optional[dict] = None) -> Dict[str, Any]:
    try:
        raw = state_path(config).read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state: Dict[str, Any], config: Optional[dict] = None) -> bool:
    try:
        p = state_path(config)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[trial-claim] 状态写盘失败：%s", e)
        return False


# ── 纯逻辑 ─────────────────────────────────────────────────────────────────

def normalize_contact(raw: str) -> str:
    """联系方式规整：Telegram 补 @、去掉链接前缀、砍长度。空串=未填（允许）。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    low = s.lower()
    for pre in ("https://t.me/", "http://t.me/", "t.me/"):
        if low.startswith(pre):
            return "@" + s[len(pre):].lstrip("@")[:60]
    # 纯数字/带 + 视为手机号（WhatsApp），其余按 Telegram 用户名补 @
    core = s.replace(" ", "").replace("-", "")
    if core.startswith("+") and core[1:].isdigit():
        return core[:24]
    if core.isdigit() and len(core) >= 7:
        return core[:24]
    return "@" + s.lstrip("@")[:60]


def summarize(state: Dict[str, Any]) -> Dict[str, Any]:
    """给前端的状态摘要——**不含 license/voucher 明文**（它们已落盘生效，无需回传）。"""
    return {
        "claimed": bool(state.get("claim_id")),
        "claim_id": str(state.get("claim_id") or ""),
        "contact": str(state.get("contact") or ""),
        "status": str(state.get("status") or ""),
        "bind_code": str(state.get("bind_code") or ""),
        "activated": bool(state.get("activated_at")),
        "activated_at": int(state.get("activated_at") or 0),
        "exhausted": bool(state.get("exhausted")),
        "gift_redeemed": bool(state.get("topup_redeemed_at")),
        "gift_chars": int(state.get("topup_chars") or 0),
        "last_error": str(state.get("last_error") or ""),
    }


# ── 三个动作 ───────────────────────────────────────────────────────────────

def claim(contact: str, *, config: Optional[dict] = None, source: str = "desktop",
          fetch: Optional[Fetch] = None) -> Dict[str, Any]:
    """向官网领取试用。已领过（本地有单号）则直接回既有单，不重复建单。"""
    f = fetch or _http
    state = load_state(config)
    if state.get("claim_id"):
        return {"ok": True, "already": True, **summarize(state)}

    from src.licensing.machine_bridge import machine_fingerprint
    fp = machine_fingerprint()
    if not fp:
        # 拿不到指纹就没法绑机，签出来的授权在本机也验不过——不如当场说清楚。
        return {"ok": False, "error": "no_fingerprint"}

    c = normalize_contact(contact)
    resp = f(f"{site_url(config)}/api/trial/claim", "POST",
             {"fingerprint": fp, "contact": c, "source": source, "product": "chatx"})
    if not resp.get("ok"):
        return {"ok": False, "error": str(resp.get("error") or "claim_failed")}

    state = {
        "claim_id": str(resp.get("claim_id") or ""),
        "fingerprint": fp,
        "contact": c,
        "bind_code": str(resp.get("bind_code") or ""),
        "status": str(resp.get("status") or "pending"),
        "created_at": int(time.time()),
    }
    save_state(state, config)
    logger.info("[trial-claim] 已建单 %s（指纹 %s，去重=%s）",
                state["claim_id"], fp, bool(resp.get("deduped")))
    out = {"ok": True, "deduped": bool(resp.get("deduped")), **summarize(state)}
    # 去重命中一条早已签发的单子 → 授权当场就在响应里，不必再等一轮轮询。
    if resp.get("license"):
        out["license"] = str(resp["license"])
    return out


def poll(*, config: Optional[dict] = None, fetch: Optional[Fetch] = None) -> Dict[str, Any]:
    """查询履约进度。返回摘要 + 可选的 ``license`` / ``topup_voucher`` 明文供路由入账。"""
    f = fetch or _http
    state = load_state(config)
    cid = str(state.get("claim_id") or "")
    if not cid:
        return {"ok": True, "claimed": False}

    url = f"{site_url(config)}/api/trial/claim-status?id={cid}"
    if state.get("fingerprint"):
        url += f"&fingerprint={state['fingerprint']}"
    resp = f(url, "GET", None)
    if not resp.get("ok"):
        state["last_error"] = str(resp.get("error") or "network")
        save_state(state, config)
        return {"ok": False, "error": state["last_error"], **summarize(state)}

    state["status"] = str(resp.get("status") or state.get("status") or "pending")
    state["last_error"] = ""
    if resp.get("bind_code"):
        state["bind_code"] = str(resp["bind_code"])
    save_state(state, config)

    out: Dict[str, Any] = {"ok": True, **summarize(state)}
    # 已激活的就不再回传 license，省得每次轮询都在网络上搬运一份可用授权。
    if resp.get("license") and not state.get("activated_at"):
        out["license"] = str(resp["license"])
    if resp.get("topup_voucher") and not state.get("topup_redeemed_at"):
        out["topup_voucher"] = str(resp["topup_voucher"])
    return out


def bind_code(*, config: Optional[dict] = None, fetch: Optional[Fetch] = None) -> Dict[str, Any]:
    """取「加客服领字符」的一次性绑定码 + 深链。"""
    f = fetch or _http
    state = load_state(config)
    cid = str(state.get("claim_id") or "")
    if not cid:
        return {"ok": False, "error": "not_claimed"}
    resp = f(f"{site_url(config)}/api/trial/bind-code", "POST", {"claim_id": cid})
    if not resp.get("ok"):
        return {"ok": False, "error": str(resp.get("error") or "bind_failed")}
    if resp.get("bind_code"):
        state["bind_code"] = str(resp["bind_code"])
        save_state(state, config)
    return {
        "ok": True,
        "bind_code": str(resp.get("bind_code") or ""),
        "telegram_url": str(resp.get("telegram_url") or ""),
        "whatsapp_url": str(resp.get("whatsapp_url") or ""),
        "redeemed": bool(resp.get("redeemed")),
    }


#: 首启向导漏斗事件白名单（与桌面壳 first-run-model.js 的 FR_FUNNEL_EVENTS 同口径）。
#: 收口在客户端这层：路由只透传，事件名不在表内直接拒——官网 /api/track 是全站
#: 通用事件流水，别让任意字符串顺着桌面壳灌进去。
FUNNEL_EVENTS = {"welcome", "claim_submit", "claim_ok", "claim_skip", "gift_open", "done"}


def funnel(event: str, *, config: Optional[dict] = None,
           fetch: Optional[Fetch] = None) -> Dict[str, Any]:
    """上报首启向导漏斗事件到官网 /api/track（fire-and-forget 语义）。

    * ``sid`` = 机器指纹 → 官网侧按机去重（与 intro 漏斗按会话去重同构）；
      拿不到指纹照样上报（匿名计数仍有意义），绝不因指纹失败丢事件。
    * /api/track 成功返回 204 无体 → ``_http`` 解析为 ``{}``；因此这里按
      「没有显式 error 即成功」判定，而不是要求 ``ok=True``。
    * 任何失败只回错误码，绝不抛——埋点是旁路，不许影响向导主流程。
    """
    ev = str(event or "").strip().lower()
    if ev not in FUNNEL_EVENTS:
        return {"ok": False, "error": "bad_event"}
    fp = ""
    try:
        from src.licensing.machine_bridge import machine_fingerprint
        fp = machine_fingerprint() or ""
    except Exception:
        pass
    f = fetch or _http
    resp = f(f"{site_url(config)}/api/track", "POST", {
        "event": f"trial_wizard_{ev}",
        "sid": fp,
        "props": {"product": "chatx"},
    })
    if isinstance(resp, dict) and resp.get("ok") is False:
        return {"ok": False, "error": str(resp.get("error") or "network")}
    return {"ok": True}


def mark_activated(config: Optional[dict] = None) -> None:
    state = load_state(config)
    if state:
        state["activated_at"] = int(time.time())
        state["status"] = "issued"
        save_state(state, config)


def mark_expired(config: Optional[dict] = None) -> None:
    """本机试用已用尽（取回的授权已过期）。记下来，向导不必每次轮询才知道。"""
    state = load_state(config)
    if state:
        state["status"] = "expired"
        state["exhausted"] = True
        save_state(state, config)


def mark_topup(chars: int, config: Optional[dict] = None) -> None:
    state = load_state(config)
    if state:
        state["topup_redeemed_at"] = int(time.time())
        state["topup_chars"] = int(chars or 0)
        save_state(state, config)
