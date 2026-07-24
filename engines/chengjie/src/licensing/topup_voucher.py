"""字符加量凭证（topup voucher）：签发 / 验签 / 兑换（P4c，charpack 自动履约闭环）。

背景：charpack（lingox-charpack，$59/150 万字符）此前在 ``MANUAL_SKUS``——加购型
用量包发 plan license 要么变相永久 basic、要么覆掉客户已有订阅档。本模块引入与
授权码**同一把 Ed25519 厂商钥匙**签名的第二种 token：

    payload = {"typ": "topup", "chars": N, "ref": 订单号, "sub"/"lic": 绑定, "iat": ts}
    token   = b64url(json) . b64url(sig)          ← 与 license.key 完全同构

链路（全自动，零厂商人工）：
  官网 paid 订单(lingox-charpack) → fulfill_chatx_watch 用离线私钥签凭证 →
  回填 order.code（与授权码同一交付通道，官网自动私信客户）→
  客户在 会员中心 → 兑换加量包 粘贴 → 本模块验签+绑定校验 →
  ``quota_store.add_license_topup``（ref=订单号幂等，同单绝不重复入账）。

绑定语义（防串号）：
  - ``lic``  精确绑定 lic_id（厂商 CLI 手工签发时首选，一号一凭证）；
  - ``sub``  客户级绑定（自动履约用：订单只有 contact，而授权 payload.sub=contact
    ——同一客户名下即可兑换）。已知宽松点：同一 contact 买过多份授权部署多实例时，
    一张凭证可在**每个实例各兑一次**（ref 幂等是实例本地的）；同客户自售后语义可接受。
  - 两者都带时 lic 优先；两者都缺 = 畸形凭证拒绝。

安全边界：
  - 验签失败/被篡改 → bad_signature；typ 不是 topup（比如把授权码贴进兑换框）→
    not_voucher；反向误贴（凭证当授权码）由 license_manager._compute 的 typ 护栏挡下；
  - 凭证不落库不缓存，兑换即消费（幂等由 ref 主键保证），无吊销面。
本模块只依赖 license_manager 的编码/密钥原语与 quota_store 入账口，绝不碰网络。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from src.licensing.license_manager import (
    LicenseError,
    _b64url_decode,
    _b64url_encode,
)

VOUCHER_TYP = "topup"


# ── 厂商侧：签发（scripts/license_tool.py topup / fulfill_chatx_watch.py 使用）──

def batch_refs(ref: str, count: int) -> list:
    """批量签发的 ref 派生（大客户一次买 N 包，license_tool ``--count``）。

    ``count==1`` → ``[ref]``（单张行为不变）；``N>1`` → ``ref-01..-NN``
    （序号零填充、宽度随 N 自适应）。每张 ref 独立 → 兑换幂等互不干扰，
    台账可按前缀归集回订单。
    """
    base = str(ref or "").strip()
    n = max(1, int(count or 1))
    if n == 1:
        return [base]
    width = max(2, len(str(n)))
    return [f"{base}-{i:0{width}d}" for i in range(1, n + 1)]


def issue_topup_voucher(
    private_hex: str,
    *,
    chars: int,
    ref: str,
    lic_id: str = "",
    customer: str = "",
    note: str = "",
    now: Optional[int] = None,
) -> str:
    """签发一张字符加量凭证。``lic_id`` / ``customer`` 至少给一个（绑定防串号）。"""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except Exception as e:  # pragma: no cover - 生产环境必装
        raise LicenseError(f"cryptography 未安装，无法签发凭证: {e}")
    n = int(chars or 0)
    r = str(ref or "").strip()
    lic = str(lic_id or "").strip()
    sub = str(customer or "").strip()
    if n <= 0:
        raise LicenseError("chars 必须为正整数")
    if not r:
        raise LicenseError("ref（订单号）不能为空——它是兑换幂等键")
    if not lic and not sub:
        raise LicenseError("必须绑定 lic_id 或 customer 之一（防凭证串号）")
    body: Dict[str, Any] = {
        "typ": VOUCHER_TYP,
        "chars": n,
        "ref": r,
        "iat": int(now if now is not None else time.time()),
    }
    if lic:
        body["lic"] = lic
    if sub:
        body["sub"] = sub
    if note:
        body["note"] = str(note)
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    return f"{_b64url_encode(raw)}.{_b64url_encode(priv.sign(raw))}"


# ── 产品侧：验签 + 兑换 ──────────────────────────────────────────────────────

def verify_topup_voucher(token: str, public_key_hex: str) -> Dict[str, Any]:
    """验签并做结构校验，返回 {ok, error?, payload?}。绝不抛。

    error 取值：bad_signature（格式/编码/签名/解析任何一步失败）、
    not_voucher（签名有效但不是 topup 凭证，如误贴授权码）、
    malformed（chars/ref/绑定字段缺失或非法）。
    """
    t = str(token or "").strip()
    if not t or "." not in t:
        return {"ok": False, "error": "bad_signature"}
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except Exception:
        return {"ok": False, "error": "unavailable"}
    body_b64, sig_b64 = t.split(".", 1)
    try:
        raw = _b64url_decode(body_b64)
        sig = _b64url_decode(sig_b64)
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        pub.verify(sig, raw)
        payload = json.loads(raw.decode("utf-8"))
    except InvalidSignature:
        return {"ok": False, "error": "bad_signature"}
    except Exception:
        return {"ok": False, "error": "bad_signature"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "bad_signature"}
    if str(payload.get("typ") or "") != VOUCHER_TYP:
        return {"ok": False, "error": "not_voucher"}
    chars = payload.get("chars")
    ref = str(payload.get("ref") or "").strip()
    if not isinstance(chars, int) or chars <= 0 or not ref:
        return {"ok": False, "error": "malformed"}
    if not str(payload.get("lic") or "").strip() and not str(payload.get("sub") or "").strip():
        return {"ok": False, "error": "malformed"}
    return {"ok": True, "payload": payload}


def redeem_topup_voucher(
    token: str,
    *,
    lic_status: Any = None,
    public_key_hex: Optional[str] = None,
) -> Dict[str, Any]:
    """客户侧兑换入口（会员中心「兑换加量包」按钮的全部后端逻辑）。

    返回 {ok, error?, chars?, ref?, lic_id?, topup_chars?, included?}。
    error 除 verify 三码外还有：not_licensed（无有效授权，凭证无处入账）、
    lic_mismatch / customer_mismatch（绑定不符=别人的凭证）、
    以及 ``add_license_topup`` 透传的 unlimited / duplicate_ref / store_unavailable。
    自身绝不抛；成功即入账（check_license_quota 立即反映新额度）。
    """
    try:
        from src.licensing.license_manager import get_license_manager
        mgr = get_license_manager()
        pub = (public_key_hex or mgr.public_key_hex).strip()
        st = lic_status if lic_status is not None else mgr.status()
        v = verify_topup_voucher(token, pub)
        if not v.get("ok"):
            return {"ok": False, "error": str(v.get("error") or "bad_signature")}
        payload = v["payload"]
        chars = int(payload["chars"])
        ref = str(payload["ref"])
        if not getattr(st, "licensed", False):
            return {"ok": False, "error": "not_licensed"}
        want_lic = str(payload.get("lic") or "").strip()
        want_sub = str(payload.get("sub") or "").strip()
        if want_lic:
            if want_lic != str(getattr(st, "lic_id", "") or ""):
                return {"ok": False, "error": "lic_mismatch"}
        elif want_sub != str(getattr(st, "customer", "") or ""):
            return {"ok": False, "error": "customer_mismatch"}
        from src.licensing.quota_store import add_license_topup
        res = add_license_topup(
            chars, ref,
            note=str(payload.get("note") or "") or "voucher",
            lic_status=st,
        )
        out = dict(res)
        out.setdefault("chars", chars)
        out.setdefault("ref", ref)
        return out
    except Exception:
        return {"ok": False, "error": "internal"}


__all__ = [
    "VOUCHER_TYP",
    "batch_refs",
    "issue_topup_voucher",
    "redeem_topup_voucher",
    "verify_topup_voucher",
]
