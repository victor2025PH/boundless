"""AccountRegistry.upsert ``merge_meta`` 语义回归网（2026-07-23 生产事故）。

事故：WhatsApp baileys 重登录 ``upsert(meta={"baileys_login_id"})`` 整块覆盖 meta，
把账号级 ``persona_id`` 绑定抹掉 → 新好友回落默认人设 + 错语言音色（"第一句总是
日语"投诉链的隐藏根因）。修复＝upsert 增 ``merge_meta=True`` 锁内原子浅合并档，
四处登录持久化 + ban_signal 全部切该档。

本文件钉死：
- merge 档只更新传入键、其余键（persona_id/auto_reply/banned/self_*…）原样保留；
- 默认档（merge_meta=False）保持整块替换的旧语义（向后兼容，显式删键仍可用）;
- merge 与 N3 静态加密正交（合并后 session_string 仍密文落盘、读出可解）；
- 端到端：baileys 登录 poll → authorized 持久化不再抹 persona_id。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from cryptography.fernet import Fernet

from src.integrations import registry_crypto as rc
from src.integrations.account_registry import AccountRegistry


@pytest.fixture
def reg(tmp_path):
    return AccountRegistry(tmp_path / "acc.db")


def test_merge_meta_preserves_existing_keys(reg):
    """事故主场景：运营绑好人设后，重登录只登记 login_id，绑定必须还在。"""
    reg.upsert("whatsapp", "639270135480", mode="protocol", status="online",
               meta={"persona_id": "lin_jiaxin", "auto_reply": True,
                     "self_name": "Calixa"})
    reg.upsert("whatsapp", "639270135480", mode="protocol", status="online",
               meta={"baileys_login_id": "wa_new123"}, merge_meta=True)
    m = reg.get("whatsapp", "639270135480")["meta"]
    assert m["persona_id"] == "lin_jiaxin"
    assert m["auto_reply"] is True
    assert m["self_name"] == "Calixa"
    assert m["baileys_login_id"] == "wa_new123"


def test_merge_meta_updates_overlapping_key(reg):
    reg.upsert("whatsapp", "1", meta={"baileys_login_id": "old", "persona_id": "p"})
    reg.upsert("whatsapp", "1", meta={"baileys_login_id": "new"}, merge_meta=True)
    m = reg.get("whatsapp", "1")["meta"]
    assert m["baileys_login_id"] == "new"
    assert m["persona_id"] == "p"


def test_default_replace_semantics_unchanged(reg):
    """向后兼容：默认档仍整块替换（read-merge-write 调用方 & 显式删键依赖此语义）。"""
    reg.upsert("telegram", "9", meta={"persona_id": "p", "junk": 1})
    reg.upsert("telegram", "9", meta={"persona_id": "p2"})
    assert reg.get("telegram", "9")["meta"] == {"persona_id": "p2"}


def test_meta_none_untouched_either_way(reg):
    reg.upsert("line", "5", meta={"persona_id": "p"})
    reg.upsert("line", "5", status="offline")
    reg.upsert("line", "5", status="online", merge_meta=True)  # meta=None + merge 档
    assert reg.get("line", "5")["meta"] == {"persona_id": "p"}


def test_merge_meta_on_insert_is_plain_insert(reg):
    reg.upsert("messenger", "new", meta={"messenger_login_id": "mg_1"},
               merge_meta=True)
    assert reg.get("messenger", "new")["meta"] == {"messenger_login_id": "mg_1"}


def test_merge_meta_orthogonal_to_encryption(tmp_path, monkeypatch):
    """merge 走「解密→合并→再加密」：既有密文键保留且仍可解，落盘无明文。"""
    monkeypatch.setenv("ACCOUNT_REGISTRY_KEY", Fernet.generate_key().decode())
    rc.reset_cache()
    try:
        db = tmp_path / "enc.db"
        reg2 = AccountRegistry(db)
        reg2.upsert("telegram", "77", meta={
            "session_string": "SS_SECRET", "persona_id": "p"})
        reg2.upsert("telegram", "77", meta={"phone": "138"}, merge_meta=True)
        m = reg2.get("telegram", "77")["meta"]
        assert m["session_string"] == "SS_SECRET"
        assert m["persona_id"] == "p" and m["phone"] == "138"
        raw = sqlite3.connect(str(db)).execute(
            "SELECT meta_json FROM platform_accounts").fetchone()[0]
        assert "SS_SECRET" not in raw and "enc:v1:" in raw
    finally:
        rc.reset_cache()


def test_baileys_relogin_end_to_end_keeps_persona(monkeypatch, tmp_path):
    """端到端回归：绑了人设的 WA 账号重扫码登录，persona_id 不再被抹。"""
    from src.integrations import whatsapp_baileys_login as wab

    async def fake_post(url, payload, timeout=20.0):
        return {"login_id": "wa_relogin", "qr_image": "data:image/png;base64,x"}

    async def fake_get(url, timeout=20.0):
        return {"status": "open", "account_id": "639270135480"}

    monkeypatch.setattr(wab, "_post_json", fake_post)
    monkeypatch.setattr(wab, "_get_json", fake_get)
    reg2 = AccountRegistry(tmp_path / "wa.db")
    monkeypatch.setattr(wab, "get_account_registry", lambda: reg2)

    # 运营已给该号绑人设（管理面写入）
    reg2.upsert("whatsapp", "639270135480", mode="protocol", status="offline",
                meta={"persona_id": "lin_jiaxin", "baileys_login_id": "wa_old"})

    async def run():
        provider = wab.make_provider(
            {"platform_login": {"whatsapp": {"baileys_url": "http://x"}}})
        info = await provider(None, "whatsapp", "protocol", "")
        r = await info["poll"](None)
        assert r["status"] == "authorized"

    asyncio.run(run())
    m = reg2.get("whatsapp", "639270135480")["meta"]
    assert m["persona_id"] == "lin_jiaxin", "重登录不得抹掉人设绑定"
    assert m["baileys_login_id"] == "wa_relogin", "login_id 应更新为新会话"


def test_ban_signal_merge_does_not_wipe_bindings(reg):
    """ban 标记走 merge 档：标记落上、人设/凭据绑定不受伤。"""
    from src.ops import ban_signal as bs

    reg.upsert("telegram", "42", mode="protocol", status="online",
               meta={"persona_id": "p", "session_name": "s"})

    class _KS:
        def set(self, *a, **k):
            pass

    bs.apply_action(
        "telegram", "42",
        {"kind": "ban", "cooldown_sec": 0.0, "reason": "test_ban"},
        kill_switch=_KS(), registry=reg)
    m = reg.get("telegram", "42")["meta"]
    assert m.get("banned") is True and m.get("ban_reason") == "test_ban"
    assert m["persona_id"] == "p" and m["session_name"] == "s"
