"""客服赠量核销 bot 门禁（scripts/cs_redeem_bot.py，传输全注入离线跑）。

守的是「保守自动化」三条铁律：allowlist 空=全拒、每日封顶、幂等重复不重复入账；
外加 console 会话 401 自动重登这条唯一的状态机分支。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_SPEC = importlib.util.spec_from_file_location(
    "cs_redeem_bot",
    Path(__file__).resolve().parents[1] / "scripts" / "cs_redeem_bot.py")
bot_mod = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("cs_redeem_bot", bot_mod)
_SPEC.loader.exec_module(bot_mod)


# ── 纯函数 ─────────────────────────────────────────────────────────────────

def test_extract_codes_normalizes_and_dedupes():
    text = "客户发来 bc-p2xa-z795，重复 BC-P2XA-Z795，另一个 BC-1111-2222"
    assert bot_mod.extract_bind_codes(text) == ["BC-P2XA-Z795", "BC-1111-2222"]


def test_extract_ignores_lookalikes():
    assert bot_mod.extract_bind_codes("BC-12-34 / ABC-1234-5678x / 无码") == []
    assert bot_mod.extract_bind_codes("") == []
    assert bot_mod.extract_bind_codes(None) == []


def test_allowlist_empty_denies_everyone():
    assert bot_mod.is_allowed(123, []) is False
    assert bot_mod.is_allowed(123, None) is False


def test_allowlist_matches_int_and_str():
    assert bot_mod.is_allowed(123, [123]) is True
    assert bot_mod.is_allowed("123", ["123"]) is True
    assert bot_mod.is_allowed(456, [123]) is False


def test_daily_cap():
    assert bot_mod.cap_reached({"2026-07-28": 50}, "2026-07-28", 50) is True
    assert bot_mod.cap_reached({"2026-07-28": 49}, "2026-07-28", 50) is False
    assert bot_mod.cap_reached({}, "2026-07-28", 50) is False


def test_format_reply_covers_terminal_states():
    ok = bot_mod.format_reply(
        {"ok": True, "contact": "@ykj123", "chars": 100000, "pending_voucher": True},
        "BC-P2XA-Z795")
    assert "BC-P2XA-Z795" in ok and "@ykj123" in ok and "100,000" in ok
    dup = bot_mod.format_reply(
        {"ok": True, "already_redeemed": True, "contact": "@ykj123"}, "BC-P2XA-Z795")
    assert "已核销过" in dup
    assert "不存在" in bot_mod.format_reply({"ok": False, "error": "not_found"}, "BC-X")
    assert "格式" in bot_mod.format_reply({"ok": False, "error": "bad_code"}, "BC-X")
    assert "console" in bot_mod.format_reply({"ok": False, "error": "unauthorized"}, "BC-X")


# ── console 会话 + bot 编排（传输注入） ────────────────────────────────────

class FakeSite:
    """假官网：console 登录 + 核销。记录调用，按脚本回应。"""

    def __init__(self, redeem_responses: Optional[List[Dict[str, Any]]] = None,
                 expire_after_login: bool = False):
        self.calls: List[tuple] = []
        self.login_count = 0
        self.redeem_responses = redeem_responses or [
            {"status": 200,
             "json": {"ok": True, "contact": "@ykj123", "chars": 100000,
                      "pending_voucher": True}}]
        self._redeem_i = 0
        self.expire_after_login = expire_after_login

    def __call__(self, url: str, method: str = "GET", body: Optional[dict] = None,
                 headers: Optional[Dict[str, str]] = None, timeout: int = 30) -> Dict[str, Any]:
        self.calls.append((method, url, body, headers))
        if url.endswith("/api/console/login"):
            self.login_count += 1
            return {"status": 200, "json": {"ok": True},
                    "set_cookie": [f"console_session=tok{self.login_count}; Path=/; HttpOnly"]}
        if url.endswith("/api/console/trial-redeem"):
            cookie = (headers or {}).get("cookie") or ""
            # 模拟过期：第一把 cookie（tok1）一律 401，重登后的 tok2 才收
            if self.expire_after_login and cookie.endswith("tok1"):
                return {"status": 401, "json": {"ok": False, "error": "unauthorized"},
                        "set_cookie": []}
            r = self.redeem_responses[min(self._redeem_i, len(self.redeem_responses) - 1)]
            self._redeem_i += 1
            return {"set_cookie": [], **r}
        return {"status": 404, "json": {}, "set_cookie": []}


def _cfg(**over):
    base = {"site": "https://x.test", "console_user": "bot", "console_pass": "pw",
            "allowed_user_ids": [111], "daily_cap": 50, "bot_token": "t"}
    base.update(over)
    return base


def _msg(text, from_id=111):
    return {"from": {"id": from_id}, "chat": {"id": -1}, "message_id": 5, "text": text}


def test_bot_redeems_and_replies():
    site = FakeSite()
    bot = bot_mod.RedeemBot(_cfg(), transport=site)
    reply = bot.handle_message(_msg("客户码 BC-P2XA-Z795"))
    assert "已核销 BC-P2XA-Z795" in reply and "@ykj123" in reply
    redeems = [c for c in site.calls if c[1].endswith("/trial-redeem")]
    assert len(redeems) == 1 and redeems[0][2] == {"code": "BC-P2XA-Z795"}
    # 登录一次后复用 cookie
    assert site.login_count == 1
    assert redeems[0][3]["cookie"] == "console_session=tok1"


def test_bot_ignores_strangers_without_any_http():
    site = FakeSite()
    bot = bot_mod.RedeemBot(_cfg(), transport=site)
    assert bot.handle_message(_msg("BC-P2XA-Z795", from_id=999)) is None
    assert not site.calls, "陌生人消息绝不触发任何 HTTP"


def test_bot_ignores_messages_without_codes():
    site = FakeSite()
    bot = bot_mod.RedeemBot(_cfg(), transport=site)
    assert bot.handle_message(_msg("你好，在吗")) is None
    assert not site.calls


def test_session_relogin_on_401_then_retry_once():
    site = FakeSite(expire_after_login=True)
    bot = bot_mod.RedeemBot(_cfg(), transport=site)
    reply = bot.handle_message(_msg("BC-P2XA-Z795"))
    assert "已核销" in reply
    assert site.login_count == 2, "401 后应重登一次再试"


def test_already_redeemed_does_not_consume_daily_budget():
    site = FakeSite(redeem_responses=[
        {"status": 200, "json": {"ok": True, "already_redeemed": True, "contact": "@a"}}])
    bot = bot_mod.RedeemBot(_cfg(), transport=site)
    reply = bot.handle_message(_msg("BC-P2XA-Z795"))
    assert "已核销过" in reply
    assert bot.counts == {}, "幂等重复不占当日预算"


def test_daily_cap_blocks_and_says_so():
    site = FakeSite()
    bot = bot_mod.RedeemBot(_cfg(daily_cap=1), transport=site)
    assert "已核销" in bot.handle_message(_msg("BC-1111-2222"))
    reply = bot.handle_message(_msg("BC-3333-4444"))
    assert "上限" in reply
    redeems = [c for c in site.calls if c[1].endswith("/trial-redeem")]
    assert len(redeems) == 1, "到顶后不再打核销接口"


def test_dry_run_never_calls_redeem():
    site = FakeSite()
    bot = bot_mod.RedeemBot(_cfg(dry_run=True), transport=site)
    reply = bot.handle_message(_msg("BC-P2XA-Z795"))
    assert "试运行" in reply
    assert not [c for c in site.calls if c[1].endswith("/trial-redeem")]


def test_multiple_codes_in_one_message():
    site = FakeSite(redeem_responses=[
        {"status": 200, "json": {"ok": True, "contact": "@a", "chars": 100000}},
        {"status": 404, "json": {"ok": False, "error": "not_found"}}])
    bot = bot_mod.RedeemBot(_cfg(), transport=site)
    reply = bot.handle_message(_msg("BC-1111-2222 和 BC-3333-4444"))
    lines = reply.split("\n")
    assert len(lines) == 2
    assert "已核销 BC-1111-2222" in lines[0] and "BC-3333-4444 不存在" in lines[1]
