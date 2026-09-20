"""凭证有效期协议回归网（实施50 P2：voucher.months → grant_pack 批次有效期）。

承载「充值 ≥500U 实付 Token 24 个月有效」的官网承诺：有效期由签发侧写进
Ed25519 签名契约（months 字段，Token 载荷专属），兑换端透传给账本批次。
旧凭证无 months → 兑换端按 PACK_VALID_MONTHS=12 缺省（向后兼容）。
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from src.licensing.license_manager import generate_keypair
from src.licensing.token_ledger import PACK_VALID_MONTHS, TokenLedgerStore
from src.licensing.topup_voucher import (
    issue_topup_voucher,
    redeem_topup_voucher,
    verify_topup_voucher,
)

DAY = 86400.0
MONTH = 30.44 * DAY


@pytest.fixture()
def keypair():
    kp = generate_keypair()
    # generate_keypair 返回形状按 license_manager 实现（dict 或 tuple）——两种都兼容
    if isinstance(kp, dict):
        return kp["private_hex"], kp["public_hex"]
    return kp[0], kp[1]


def _payload(token: str) -> dict:
    seg = token.split(".")[0]
    pad = "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg + pad).decode("utf-8"))


# ── 签发侧 ───────────────────────────────────────────────────────────────────

def test_issue_tokens_with_months_lands_in_payload(keypair):
    priv, pub = keypair
    tok = issue_topup_voucher(priv, tokens=750_000, ref="AH-1", customer="@alice",
                              valid_months=24)
    p = _payload(tok)
    assert p["months"] == 24
    v = verify_topup_voucher(tok, pub)
    assert v["ok"] and v["payload"]["months"] == 24


def test_issue_without_months_omits_field(keypair):
    priv, pub = keypair
    tok = issue_topup_voucher(priv, tokens=10_000, ref="AH-2", customer="@alice")
    assert "months" not in _payload(tok)
    assert verify_topup_voucher(tok, pub)["ok"]


def test_issue_chars_only_ignores_months(keypair):
    """字符载荷无过期语义（quota_store 无过期列）：chars-only 给 months 静默不写。"""
    priv, _ = keypair
    tok = issue_topup_voucher(priv, chars=100_000, ref="AH-3", customer="@alice",
                              valid_months=24)
    assert "months" not in _payload(tok)


# ── 验签侧（签名契约里不许有垃圾字段）───────────────────────────────────────

def _sign_raw(priv_hex: str, body: dict) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex)).sign(raw)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")  # noqa: E731
    return f"{b64(raw)}.{b64(sig)}"


@pytest.mark.parametrize("bad", [-3, 0, "24", True, 121])
def test_verify_rejects_garbage_months(keypair, bad):
    priv, pub = keypair
    body = {"typ": "topup", "ref": "AH-4", "iat": int(time.time()),
            "tokens": 1000, "sub": "@alice", "months": bad}
    v = verify_topup_voucher(_sign_raw(priv, body), pub)
    assert not v["ok"] and v["error"] == "malformed"


def test_verify_accepts_fractional_months(keypair):
    priv, pub = keypair
    body = {"typ": "topup", "ref": "AH-5", "iat": int(time.time()),
            "tokens": 1000, "sub": "@alice", "months": 1.5}
    assert verify_topup_voucher(_sign_raw(priv, body), pub)["ok"]


# ── 账本侧 ───────────────────────────────────────────────────────────────────

def test_grant_pack_custom_validity():
    store = TokenLedgerStore(":memory:")
    now = time.time()
    assert store.grant_pack("w1", 1000, "ref-24", valid_months=24, now=now)
    assert store.grant_pack("w1", 1000, "ref-def", now=now)
    rows = {r["ref"]: r for r in store.usage("w1")["grants"]}
    exp24 = rows["ref-24"]["expires_at"]
    exp12 = rows["ref-def"]["expires_at"]
    assert abs(exp24 - (now + 24 * MONTH)) < DAY
    assert abs(exp12 - (now + PACK_VALID_MONTHS * MONTH)) < DAY


# ── 兑换端透传 ───────────────────────────────────────────────────────────────

class _FakeStatus:
    licensed = True
    lic_id = "lic-x"
    customer = "@alice"


def test_redeem_passes_months_to_grant(keypair, monkeypatch):
    priv, pub = keypair
    captured: dict = {}

    def fake_grant(tokens, ref, *, lic_status=None, note="", valid_months=None, now=None):
        captured.update(tokens=tokens, ref=ref, valid_months=valid_months)
        return {"ok": True, "wallet": "w", "tokens": tokens, "balance": tokens}

    import src.licensing.token_ledger as tl

    monkeypatch.setattr(tl, "grant_pack_for_status", fake_grant)
    tok = issue_topup_voucher(priv, tokens=750_000, ref="AH-6", customer="@alice",
                              valid_months=24)
    out = redeem_topup_voucher(tok, lic_status=_FakeStatus(), public_key_hex=pub)
    assert out["ok"], out
    assert captured["valid_months"] == 24
    assert captured["ref"] == "AH-6"


def test_redeem_legacy_voucher_defaults(keypair, monkeypatch):
    priv, pub = keypair
    captured: dict = {}

    def fake_grant(tokens, ref, *, lic_status=None, note="", valid_months=None, now=None):
        captured.update(valid_months=valid_months)
        return {"ok": True, "wallet": "w", "tokens": tokens, "balance": tokens}

    import src.licensing.token_ledger as tl

    monkeypatch.setattr(tl, "grant_pack_for_status", fake_grant)
    tok = issue_topup_voucher(priv, tokens=10_000, ref="AH-7", customer="@alice")
    out = redeem_topup_voucher(tok, lic_status=_FakeStatus(), public_key_hex=pub)
    assert out["ok"], out
    assert captured["valid_months"] is None  # 兑换端按 PACK_VALID_MONTHS 缺省
