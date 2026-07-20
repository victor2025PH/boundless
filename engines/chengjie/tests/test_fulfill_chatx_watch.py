"""Sprint4：chatx 履约守护 run_once 集成（假 HTTP + 测试密钥对 + tmp state）。

不触真网络/真私钥：monkeypatch http_json 喂 paid 订单并捕获回填 POST；用生成的测试密钥对
验证回填的 code 是可验签的 chatx license，且 done 幂等（二次运行不重复开通）。
"""
import importlib.util
from pathlib import Path

import pytest

from src.licensing.license_manager import LicenseManager, generate_keypair


def _load_watch():
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "fulfill_chatx_watch", root / "scripts" / "fulfill_chatx_watch.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def watch(tmp_path):
    mod = _load_watch()
    mod.STATE_FILE = tmp_path / "fulfilled_chatx.json"  # 隔离 state，不碰真 config
    return mod


def _fake_http(paid_orders, posts):
    def http_json(url, payload=None, key=""):
        if payload is None:  # GET orders
            return {"ok": True, "orders": paid_orders}
        posts.append(payload)  # POST order-status
        return {"ok": True, "order": {"id": payload.get("id"), "status": payload.get("status")}}
    return http_json


def test_run_once_signs_and_backfills(watch, monkeypatch):
    kp = generate_keypair()
    posts = []
    orders = [
        {"id": "O1", "sku_id": "chatx-team", "contact": "acme@x.com", "period": "monthly", "status": "paid"},
        {"id": "O2", "sku_id": "lingox-pro", "product_id": "tongyi", "status": "paid"},  # 非 chatx → 跳
    ]
    monkeypatch.setattr(watch, "http_json", _fake_http(orders, posts))
    conf = {"site": "https://x", "key": "k", "priv_hex": kp["private_hex"]}

    handled = watch.run_once(conf, dry=False)
    assert handled == 1
    assert len(posts) == 1
    p = posts[0]
    assert p["id"] == "O1" and p["status"] == "activated" and p["code"]
    # 回填的 code 是可验签的 chatx-team license（seats=10）
    st = LicenseManager(license_token=p["code"], public_key_hex=kp["public_hex"]).status()
    assert st.state == "active" and st.plan == "pro" and st.seats == 10

    # 幂等：O1 已 done → 二次运行不再回填
    posts2 = []
    monkeypatch.setattr(watch, "http_json", _fake_http(orders, posts2))
    handled2 = watch.run_once(conf, dry=False)
    assert handled2 == 0 and posts2 == []


def test_run_once_dry_run_signs_nothing(watch, monkeypatch):
    kp = generate_keypair()
    posts = []
    orders = [{"id": "O9", "sku_id": "chatx-entry", "contact": "c", "period": "monthly", "status": "paid"}]
    monkeypatch.setattr(watch, "http_json", _fake_http(orders, posts))
    conf = {"site": "https://x", "key": "k", "priv_hex": kp["private_hex"]}
    handled = watch.run_once(conf, dry=True)
    assert handled == 0 and posts == []  # dry-run 不回填
