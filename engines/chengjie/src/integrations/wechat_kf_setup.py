# -*- coding: utf-8 -*-
"""微信客服（企业微信）接入向导的**纯逻辑层**（实施97 线 A · 引导页）：

- :func:`describe_kf_errcode`：企微错误码 → 坐席看得懂的「问题 + 怎么办」；
- :func:`fetch_egress_ip` / :func:`parse_ip_text`：本机出站公网 IP（企微自建应用要求填进「可信 IP」）；
- :func:`assemble_checks`：把 gettoken / kf/account/list 两步的结果装配成向导第 ② 步的检查项列表；
- :func:`normalize_kf_accounts`：客服账号列表 → 卡片数据。

全部无 I/O（``fetch_egress_ip`` 的 HTTP 可注入），路由层只做鉴权与拼装。
"""
from __future__ import annotations

import ipaddress
import json
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

#: 企微错误码 → (问题类别, 原因说明, 怎么办)。类别与 wechat_kf.classify_errcode 的口径对齐。
_ERRCODE_HELP: Dict[int, Tuple[str, str, str]] = {
    40013: ("invalid_corpid", "CorpID 不正确", "到企微管理后台「我的企业 → 企业信息」复制企业 ID（以 ww 开头）"),
    40001: ("invalid_secret", "Secret 不正确或不属于该企业", "到「应用管理 → 自建应用」重新查看 Secret（注意不要复制到空格）"),
    40014: ("invalid_secret", "凭证无效（access_token 不合法）", "核对 CorpID 与 Secret 是否配对；Secret 重置过要重新填"),
    41001: ("invalid_secret", "缺少 access_token", "凭证未能换到令牌，先核对 CorpID / Secret"),
    42001: ("token_expired", "令牌过期", "系统会自动刷新；若持续出现请重新测试"),
    60020: ("ip_not_allowed", "本机出站 IP 不在应用的「可信 IP」里",
            "复制下方检测到的出站 IP，到企微后台「自建应用 → 企业可信 IP」添加后重测；宽带 IP 会变，建议固定 IP"),
    60011: ("no_privilege", "应用没有该客服账号的管理权限",
            "到「微信客服 → 通过 API 管理微信客服账号」把该应用加入可调用列表并勾选可管理的客服账号"),
    95014: ("servicer_not_active", "客服账号未被该应用管理或接待人员未激活",
            "到「微信客服 → 可调用接口的应用」确认本应用已加入，且客服账号在其可管理范围内"),
    95013: ("no_privilege", "应用未被授权微信客服接口", "到「微信客服 → 通过 API 管理微信客服账号」授权本应用"),
    45009: ("rate_limited", "接口调用频率超限", "稍等一分钟再测"),
    701008: ("license_required", "接待人员未开通互通账号许可", "在企微管理后台为接待人员购买/分配互通账号许可后重试"),
}


def describe_kf_errcode(errcode: Any, *, egress_ip: str = "") -> Dict[str, str]:
    """错误码 → ``{kind, problem, fix}``；未知码给通用兜底文案（带原始码）。"""
    try:
        code = int(errcode)
    except Exception:
        code = -1
    hit = _ERRCODE_HELP.get(code)
    if hit is None:
        if code == 0:
            return {"kind": "ok", "problem": "", "fix": ""}
        if code < 0:
            return {"kind": "network", "problem": "连不上企业微信接口（网络/代理/DNS）",
                    "fix": "检查本机能否访问 qyapi.weixin.qq.com；有代理时确认已放行"}
        return {"kind": "api_error", "problem": f"企业微信返回错误码 {code}",
                "fix": "到企微开发者中心「全局错误码」查询该码；多为权限或参数问题"}
    kind, problem, fix = hit
    if kind == "ip_not_allowed" and egress_ip:
        fix = f"把 {egress_ip} 添加到企微后台「自建应用 → 企业可信 IP」后重测；宽带 IP 会变，建议固定 IP 或使用智聊中继"
    return {"kind": kind, "problem": problem, "fix": fix}


# ── 出站 IP ──────────────────────────────────────────────────────────────────

#: 公网 IP 回显服务（多家轮询，任一成功即可；全部只回纯文本/JSON，不带我们的任何信息）
EGRESS_IP_PROVIDERS: Tuple[Tuple[str, str], ...] = (
    ("https://api.ipify.org?format=json", "json:ip"),
    ("https://ifconfig.me/ip", "text"),
    ("https://api.ip.sb/ip", "text"),
    ("https://icanhazip.com", "text"),
)
_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def parse_ip_text(body: str, fmt: str = "text") -> str:
    """回显服务响应 → 合法 IPv4 字符串；解析不出返回空串。"""
    s = str(body or "").strip()
    if not s:
        return ""
    if fmt.startswith("json"):
        key = fmt.split(":", 1)[1] if ":" in fmt else "ip"
        try:
            d = json.loads(s)
            s = str((d or {}).get(key) or "")
        except Exception:
            return ""
    m = _IPV4_RE.search(s)
    if not m:
        return ""
    try:
        ip = ipaddress.ip_address(m.group(1))
    except Exception:
        return ""
    if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
        return ""
    return str(ip)


_egress_cache: Dict[str, Any] = {"ts": 0.0, "ip": "", "source": ""}
EGRESS_CACHE_SEC = 60.0


def fetch_egress_ip(http_get: Optional[Callable[[str, float], str]] = None, *, timeout: float = 4.0,
                    now: Optional[float] = None, use_cache: bool = True) -> Dict[str, Any]:
    """本机出站公网 IP：轮询多家回显服务，60 秒缓存。``http_get(url, timeout) -> text`` 可注入。"""
    t = float(now if now is not None else time.time())
    if use_cache and _egress_cache["ip"] and t - float(_egress_cache["ts"]) <= EGRESS_CACHE_SEC:
        return {"ok": True, "ip": _egress_cache["ip"], "source": _egress_cache["source"], "cached": True}
    if http_get is None:
        def http_get(url: str, tmo: float) -> str:  # pragma: no cover - 真实网络
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "chatx-setup/1.0"})
            with urllib.request.urlopen(req, timeout=tmo) as resp:  # noqa: S310
                return resp.read(256).decode("utf-8", errors="replace")
    errors: List[str] = []
    for url, fmt in EGRESS_IP_PROVIDERS:
        try:
            ip = parse_ip_text(http_get(url, timeout), fmt)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")
            continue
        if ip:
            _egress_cache.update({"ts": t, "ip": ip, "source": url})
            return {"ok": True, "ip": ip, "source": url, "cached": False}
        errors.append(f"{url}: unparsable")
    return {"ok": False, "ip": "", "source": "", "errors": errors[:4]}


def reset_egress_cache() -> None:
    _egress_cache.update({"ts": 0.0, "ip": "", "source": ""})


# ── 检查项装配 ────────────────────────────────────────────────────────────────

def normalize_kf_accounts(account_list: Any, *, bound_ids: Optional[set] = None) -> List[Dict[str, Any]]:
    """``kf/account/list`` 的 ``account_list`` → 卡片数据 ``[{open_kfid, name, avatar, manage_privilege, bound}]``。"""
    out: List[Dict[str, Any]] = []
    bound = set(bound_ids or ())
    for a in (account_list or []):
        if not isinstance(a, dict):
            continue
        kfid = str(a.get("open_kfid") or "").strip()
        if not kfid:
            continue
        out.append({
            "open_kfid": kfid,
            "name": str(a.get("name") or "").strip() or kfid,
            "avatar": str(a.get("avatar") or ""),
            "manage_privilege": bool(a.get("manage_privilege", False)),
            "bound": kfid in bound,
        })
    return out


def assemble_checks(token_res: Dict[str, Any], list_res: Optional[Dict[str, Any]], *,
                    egress_ip: str = "") -> Dict[str, Any]:
    """两步 API 结果 → 向导第 ② 步的检查项：``{ok, checks:[{id, ok, problem, fix, errcode}], accounts}``。

    - ``credentials``：gettoken 成功；
    - ``kf_permission``：kf/account/list 成功（说明应用已被授权微信客服接口、可信 IP 放行）；
    - ``manageable_accounts``：至少一个 manage_privilege 的客服账号。
    """
    checks: List[Dict[str, Any]] = []
    tok_ok = bool(token_res.get("ok"))
    tok_code = int(token_res.get("errcode") or 0) if not tok_ok else 0
    if tok_ok:
        checks.append({"id": "credentials", "ok": True, "problem": "", "fix": "", "errcode": 0})
    else:
        h = describe_kf_errcode(tok_code if tok_code else -1, egress_ip=egress_ip)
        checks.append({"id": "credentials", "ok": False, "problem": h["problem"], "fix": h["fix"],
                       "errcode": tok_code, "kind": h["kind"]})
    accounts: List[Dict[str, Any]] = []
    if tok_ok and list_res is not None:
        if list_res.get("ok"):
            accounts = normalize_kf_accounts((list_res.get("data") or {}).get("account_list"))
            checks.append({"id": "kf_permission", "ok": True, "problem": "", "fix": "", "errcode": 0})
            manageable = [a for a in accounts if a["manage_privilege"]]
            if manageable:
                checks.append({"id": "manageable_accounts", "ok": True, "problem": "", "fix": "",
                               "errcode": 0, "count": len(manageable)})
            else:
                checks.append({"id": "manageable_accounts", "ok": False,
                               "problem": "应用还没有任何可管理的客服账号" if accounts else "企业还没有客服账号",
                               "fix": ("到「微信客服 → 通过 API 管理微信客服账号」为本应用勾选客服账号"
                                       if accounts else "在下一步直接新建一个客服账号（由本应用管理）"),
                               "errcode": 0, "count": 0})
        else:
            code = int(list_res.get("errcode") or -1)
            h = describe_kf_errcode(code, egress_ip=egress_ip)
            checks.append({"id": "kf_permission", "ok": False, "problem": h["problem"], "fix": h["fix"],
                           "errcode": code, "kind": h["kind"]})
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "accounts": accounts}


__all__ = ["describe_kf_errcode", "parse_ip_text", "fetch_egress_ip", "reset_egress_cache",
           "EGRESS_IP_PROVIDERS", "normalize_kf_accounts", "assemble_checks"]
