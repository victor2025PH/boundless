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
import re
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
    chars: int = 0,
    tokens: int = 0,
    ref: str,
    lic_id: str = "",
    customer: str = "",
    note: str = "",
    valid_months: Optional[float] = None,
    now: Optional[int] = None,
) -> str:
    """签发一张加量凭证。``lic_id`` / ``customer`` 至少给一个（绑定防串号）。

    两种载荷（2026-08-19 Token 定价改版，同一 typ 同一兑换框）：
      · ``chars>0``  —— 字符加量包（停售存量轨道，入 quota_store）；
      · ``tokens>0`` —— Token 包（在售轨道，入 token_ledger 客户钱包）。
    至少给一个正数；两者可同给（换发场景），兑换端各自入账。

    ``valid_months``（实施50 P2，Token 载荷专属）：写进凭证的 ``months`` 字段，
    兑换端透传给 token_ledger.grant_pack 定批次有效期——承载「充值 ≥500U 实付
    24 个月有效」的官网承诺（缺省=兑换端 PACK_VALID_MONTHS=12）。字符载荷无
    过期语义（quota_store 无过期列），chars-only 凭证给了也不写（静默忽略）。
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except Exception as e:  # pragma: no cover - 生产环境必装
        raise LicenseError(f"cryptography 未安装，无法签发凭证: {e}")
    n = int(chars or 0)
    tk = int(tokens or 0)
    r = str(ref or "").strip()
    lic = str(lic_id or "").strip()
    sub = str(customer or "").strip()
    if n <= 0 and tk <= 0:
        raise LicenseError("chars / tokens 至少一个为正整数")
    if not r:
        raise LicenseError("ref（订单号）不能为空——它是兑换幂等键")
    if not lic and not sub:
        raise LicenseError("必须绑定 lic_id 或 customer 之一（防凭证串号）")
    body: Dict[str, Any] = {
        "typ": VOUCHER_TYP,
        "ref": r,
        "iat": int(now if now is not None else time.time()),
    }
    if n > 0:
        body["chars"] = n
    if tk > 0:
        body["tokens"] = tk
        if valid_months is not None and float(valid_months) > 0:
            body["months"] = round(float(valid_months), 2)
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

# TG handle / 邮箱抽取（contact_core 用）。handle 规则同 Telegram：5-32 位字母数字下划线；
# 前缀容忍 tg: / telegram: / t.me/ / @ 及其组合（「Tg: @Alice」「t.me/alice」都命中 alice）。
_TG_HANDLE_RE = re.compile(r"(?:t\.me/|tg\s*[:：]\s*|telegram\s*[:：]\s*|@)\s*@?([a-z0-9_]{4,32})")
_EMAIL_RE = re.compile(r"[a-z0-9][\w.+-]*@[\w-]+\.[\w.-]+")


def contact_core(s: str) -> str:
    """联系方式 → 可比对的核心标识（客户级绑定的「同一客户」判定锚点）。

    两次下单的 contact 几乎不可能逐字一致（大小写 / 空格 / ``tg:`` 前缀 / ``@`` 有无 /
    括号备注——P6 首单演练实锤：订阅单与加量包单只差备注文字，精确比对直接
    ``customer_mismatch`` 把合法凭证拒之门外）。比对语义应该是「同一个人」：

    - 提得出 TG handle → 以 handle 为准（``Tg: @Alice (老板)`` ≡ ``t.me/alice``）；
    - 否则提得出邮箱 → 以邮箱为准（大小写/围绕文本无关）；
    - 都提不出 → 保守回退：casefold + 去全部空白的整串（仍是精确语义，防串号）。

    返回空串表示无内容（调用方应拒绝）。
    """
    t = str(s or "").strip().casefold()
    if not t:
        return ""
    # 邮箱先判：``Boss@Acme.com`` 里的 ``@acme`` 会被 TG 正则误抢；
    # 反向不会（``tg:@alice`` / ``t.me/alice`` 无 ``local@domain.tld`` 结构）。
    m = _EMAIL_RE.search(t)
    if m:
        return "mail:" + m.group(0)
    m = _TG_HANDLE_RE.search(t)
    if m:
        return "tg:" + m.group(1)
    return "raw:" + re.sub(r"\s+", "", t)


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
    # 载荷合法性（2026-08-19 起支持双载荷）：chars / tokens 至少一个正整数。
    chars = payload.get("chars")
    tokens = payload.get("tokens")
    has_chars = isinstance(chars, int) and chars > 0
    has_tokens = isinstance(tokens, int) and tokens > 0
    ref = str(payload.get("ref") or "").strip()
    if (not has_chars and not has_tokens) or not ref:
        return {"ok": False, "error": "malformed"}
    if (chars is not None and not has_chars) or (tokens is not None and not has_tokens):
        return {"ok": False, "error": "malformed"}   # 显式给了但非法（0/负数/非整型）
    # months（实施50 P2）：可选；给了必须是 (0,120] 的数——签名契约里不许有垃圾字段。
    months = payload.get("months")
    if months is not None:
        if not isinstance(months, (int, float)) or isinstance(months, bool) \
                or not (0 < float(months) <= 120):
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

    返回 {ok, error?, chars?, tokens?, ref?, lic_id?, topup_chars?, included?,
    wallet?, balance?}。error 除 verify 三码外还有：not_licensed（无有效授权，
    凭证无处入账）、lic_mismatch / customer_mismatch（绑定不符=别人的凭证）、
    以及入账层透传的 unlimited / duplicate_ref / store_unavailable。

    双载荷（2026-08-19）：``chars`` 入 quota_store 字符池（挂 lic_id）；
    ``tokens`` 入 token_ledger 客户钱包（挂 contact_core——跨续费存活）。
    自身绝不抛；成功即入账（check_license_quota / wallet_snapshot 立即反映）。
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
        chars = int(payload.get("chars") or 0)
        tokens = int(payload.get("tokens") or 0)
        ref = str(payload["ref"])
        if not getattr(st, "licensed", False):
            return {"ok": False, "error": "not_licensed"}
        want_lic = str(payload.get("lic") or "").strip()
        want_sub = str(payload.get("sub") or "").strip()
        if want_lic:
            if want_lic != str(getattr(st, "lic_id", "") or ""):
                return {"ok": False, "error": "lic_mismatch"}
        else:
            # 客户级绑定按核心标识比对（同一 TG handle / 邮箱 = 同一客户），
            # 免「两次下单 contact 写法不一致」把合法凭证拒掉（P6 演练实锤）。
            core_want = contact_core(want_sub)
            core_have = contact_core(str(getattr(st, "customer", "") or ""))
            if not core_want or core_want != core_have:
                return {"ok": False, "error": "customer_mismatch"}
        note = str(payload.get("note") or "") or "voucher"
        # Token 包：入客户钱包（幂等 ref 在账本层）。纯 Token 凭证到此返回；
        # 双载荷（换发场景）继续走字符入账，任一失败如实报错。
        if tokens > 0:
            from src.licensing.token_ledger import grant_pack_for_status
            months = payload.get("months")
            tres = grant_pack_for_status(
                tokens, ref, lic_status=st, note=note,
                valid_months=float(months) if months else None)
            if not tres.get("ok"):
                return {"ok": False, "error": str(tres.get("error") or "internal"),
                        "tokens": tokens, "ref": ref}
            if chars <= 0:
                out = dict(tres)
                out.setdefault("tokens", tokens)
                out.setdefault("ref", ref)
                return out
        from src.licensing.quota_store import add_license_topup
        res = add_license_topup(chars, ref, note=note, lic_status=st)
        out = dict(res)
        out.setdefault("chars", chars)
        if tokens > 0:
            out.setdefault("tokens", tokens)
        out.setdefault("ref", ref)
        return out
    except Exception:
        return {"ok": False, "error": "internal"}


__all__ = [
    "VOUCHER_TYP",
    "batch_refs",
    "contact_core",
    "issue_topup_voucher",
    "redeem_topup_voucher",
    "verify_topup_voucher",
]
