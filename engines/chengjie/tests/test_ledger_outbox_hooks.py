"""签发即台账接线（2026-09-10）：fulfill_trial / fulfill_chatx_watch 成功回填后写 outbox。

背景：此前只有 ``license_tool.py issue`` 写 outbox，厂商机每天真正在签的试用授权与
订单授权从未进集团账本（website /console 授权台账对智聊是盲区）。本文件钉住三件事：
  1. ``ledger_outbox.normalize_issue`` 透传 payload.machine → machine_fingerprint、
     kind/origin 进 raw；``record_payload`` fail-silent；
  2. ``fulfill_chatx_watch.run_once`` 成功回填 → outbox 出现同 lic_id 记录（dry-run 不写；
     充值/加量凭证不写）；
  3. ``fulfill_trial._ledger_record`` 写出的记录带绑机指纹、trial 状态、product_id；
  4. ``tools/license_ledger/export_chengjie.make_record`` 同口径透传 machine。
全部写 tmp（CHENGJIE_LEDGER_OUTBOX 指向 tmp_path），绝不碰仓库 config/。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from src.licensing.chatx_fulfillment import build_trial_payload
from src.licensing.license_manager import generate_keypair, issue_license

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def outbox(tmp_path, monkeypatch):
    p = tmp_path / "outbox" / "ledger_outbox.jsonl"
    monkeypatch.setenv("CHENGJIE_LEDGER_OUTBOX", str(p))
    return p


def _lines(p: Path):
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


# ── 1. normalize_issue / record_payload ──────────────────────────────────────

def test_normalize_issue_transmits_machine_and_kind(outbox):
    lo = _load("_lo_a", "scripts/ledger_outbox.py")
    kp = generate_keypair()
    payload = build_trial_payload(customer="tg:1", machine="ABCD-EF01-2345-6789",
                                  claim_id="clm_1", now=1_700_000_000)
    token = issue_license(payload, kp["private_hex"])
    rec = lo.normalize_issue(payload, token, kind=lo.KIND_TRIAL_ISSUE,
                             origin="scripts/fulfill_trial.py")
    assert rec["source_key"] == "trial-ABCDEF01"
    assert rec["machine_fingerprint"] == "ABCD-EF01-2345-6789"
    assert rec["product_id"] == "zhiliao" and rec["plan"] == "pro"
    assert rec["status"] == "trial" and rec["expires_at"] is None
    assert rec["raw"]["kind"] == "trial_fulfill"
    assert rec["raw"]["origin"] == "scripts/fulfill_trial.py"
    assert token not in json.dumps(rec, ensure_ascii=False)  # 绝不落 token 原文
    # 无 machine → null；'*' 站点授权原样
    assert lo.normalize_issue({"sub": "a", "plan": "basic"})["machine_fingerprint"] is None
    assert lo.normalize_issue({"sub": "a", "plan": "basic", "machine": "*"})["machine_fingerprint"] == "*"
    # 缺省 kind 保持 license_tool 兼容
    assert lo.normalize_issue({"sub": "a", "plan": "basic"})["raw"]["kind"] == "cli_issue"


def test_record_payload_is_fail_silent(outbox):
    lo = _load("_lo_b", "scripts/ledger_outbox.py")
    assert lo.record_payload({"sub": "a", "plan": "basic", "lic_id": "L1"}, None,
                             kind="order_fulfill", origin="t") is True
    assert _lines(outbox)[0]["source_key"] == "L1"
    # 不可序列化 / 非 dict → False 且不抛、不追加脏行
    assert lo.record_payload({"raw": {1, 2}}, None, kind="x", origin="y") is False  # type: ignore[arg-type]
    assert lo.record_payload(["nope"], None, kind="x", origin="y") is False  # type: ignore[arg-type]
    assert len(_lines(outbox)) == 1


# ── 2. fulfill_chatx_watch.run_once → outbox ─────────────────────────────────

def _fake_http(paid_orders, posts):
    def http_json(url, payload=None, key=""):
        if payload is None:
            return {"ok": True, "orders": paid_orders}
        posts.append(payload)
        return {"ok": True, "order": {"id": payload.get("id"), "status": payload.get("status")}}
    return http_json


def test_watch_run_once_writes_license_outbox_not_vouchers(outbox, tmp_path, monkeypatch):
    watch = _load("_watch_hook", "scripts/fulfill_chatx_watch.py")
    watch.STATE_FILE = tmp_path / "fulfilled_chatx.json"
    kp = generate_keypair()
    posts: list = []
    orders = [
        {"id": "O1", "sku_id": "chatx-team", "contact": "acme@x.com", "period": "monthly", "status": "paid"},
        # 充值单：签的是 topup 凭证，不是授权 → 不得进 outbox
        {"id": "R1", "sku_id": "recharge-200", "contact": "buyer@x.com", "status": "paid",
         "paid_at": "2026-09-01T00:00:00Z", "amount": 200, "pay_amount": 200},
    ]
    monkeypatch.setattr(watch, "http_json", _fake_http(orders, posts))
    conf = {"site": "https://x", "key": "k", "priv_hex": kp["private_hex"]}

    watch.run_once(conf, dry=False)
    rows = _lines(outbox)
    assert [r["source_key"] for r in rows] == ["chatx-team-O1"]
    r = rows[0]
    assert r["source_system"] == "chengjie" and r["raw"]["kind"] == "order_fulfill"
    assert r["raw"]["origin"] == "scripts/fulfill_chatx_watch.py"
    assert r["product_id"] == "zhiliao" and r["sku_id"] == "chatx-team"
    assert r["plan"] == "pro" and r["seats"] == 10 and r["status"] == "active"
    assert r["machine_fingerprint"] is None  # 订单授权不绑机
    # 回填的 token 与台账摘要对得上（同一份签发物）
    code = next(p["code"] for p in posts if p["id"] == "O1")
    import hashlib
    assert r["raw"]["token_sha256"] == hashlib.sha256(code.encode("utf-8")).hexdigest()


def test_watch_dry_run_and_backfill_failure_write_nothing(outbox, tmp_path, monkeypatch):
    watch = _load("_watch_hook2", "scripts/fulfill_chatx_watch.py")
    watch.STATE_FILE = tmp_path / "fulfilled_chatx.json"
    kp = generate_keypair()
    orders = [{"id": "O9", "sku_id": "chatx-entry", "contact": "c", "period": "monthly", "status": "paid"}]
    conf = {"site": "https://x", "key": "k", "priv_hex": kp["private_hex"]}

    monkeypatch.setattr(watch, "http_json", _fake_http(orders, []))
    watch.run_once(conf, dry=True)
    assert _lines(outbox) == []

    def failing_http(url, payload=None, key=""):
        if payload is None:
            return {"ok": True, "orders": orders}
        return {"ok": False, "error": "boom"}  # 回填失败 → 授权没交付，不入账
    monkeypatch.setattr(watch, "http_json", failing_http)
    watch.run_once(conf, dry=False)
    assert _lines(outbox) == []


# ── 3. fulfill_trial._ledger_record ──────────────────────────────────────────

def test_fulfill_trial_ledger_record(outbox):
    ft = _load("_fulfill_trial_hook", "scripts/fulfill_trial.py")
    kp = generate_keypair()
    payload = build_trial_payload(customer="user@x.com", machine="1111-2222-3333-4444",
                                  claim_id="clm_9", now=1_700_000_000)
    token = issue_license(payload, kp["private_hex"])
    assert ft._ledger_record(payload, token, "trial_fulfill") is True
    # 升级重签：同 lic_id 再记一行（导出侧同 source_key 后写覆盖）
    payload2 = build_trial_payload(customer="user@x.com", machine="1111-2222-3333-4444",
                                   claim_id="clm_9", chars=2_000_000, now=1_700_000_100)
    token2 = issue_license(payload2, kp["private_hex"])
    assert ft._ledger_record(payload2, token2, "trial_upgrade") is True
    rows = _lines(outbox)
    assert [r["source_key"] for r in rows] == ["trial-11112222", "trial-11112222"]
    assert rows[0]["raw"]["kind"] == "trial_fulfill" and rows[1]["raw"]["kind"] == "trial_upgrade"
    assert rows[0]["machine_fingerprint"] == "1111-2222-3333-4444"
    assert rows[0]["status"] == "trial" and rows[0]["product_id"] == "zhiliao"
    assert rows[0]["customer_name"] == "user@x.com"
    assert rows[1]["raw"]["payload"]["included_chars"] == 2_000_000
    # 异常输入 fail-silent
    assert ft._ledger_record(None, "x", "trial_fulfill") is False  # type: ignore[arg-type]


# ── 4. export_chengjie 全量导出同口径 ──────────────────────────────────────────

def test_export_chengjie_transmits_machine():
    exp = _load("_export_chengjie_hook", "../../tools/license_ledger/export_chengjie.py")
    kp = generate_keypair()
    payload = build_trial_payload(customer="c", machine="AAAA-BBBB-CCCC-DDDD", now=1_700_000_000)
    token = issue_license(payload, kp["private_hex"])
    rec = exp.make_record(exp.decode_token(token), token, origin="t", kind="license_key")
    assert rec["machine_fingerprint"] == "AAAA-BBBB-CCCC-DDDD"
    assert rec["product_id"] == "zhiliao" and rec["status"] == "trial"
    plain = exp.make_record({"sub": "c", "plan": "basic", "lic_id": "L"}, None, origin="t", kind="k")
    assert plain["machine_fingerprint"] is None
