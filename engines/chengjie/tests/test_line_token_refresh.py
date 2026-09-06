# -*- coding: utf-8 -*-
"""LINE access token 续期门禁（2026-09-06，zhiliao 两号「7 天必掉线」根因修复）。

钉住的事实（均为本机实测）：
- token JWT ``exp - iat`` = 7 天，refresh token 365 天、续期后轮换；
- 续期请求**必须**带 ``X-Line-Access``（哪怕已过期），不带即 REQUEST_NEED_LOGIN；
- 回写 session 文件不能走 ``save_tokens()``（从文件装回的 E2EE 密钥导不出 → keys 清空）；
- 同一 session 的多个 client 续期要先看文件（refresh token 轮换，各自续会互相作废）；
- 被标 ``worker:expired`` 的号续期成功要回 ``authorized``；真死的号按退避低频重试。
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.integrations import line_token_refresh as LTR  # noqa: E402

DAY = 86400.0


def _jwt(iat: float, exp: float, **extra) -> str:
    payload = {"iat": int(iat), "exp": int(exp), "aud": "LINE", "cmode": "SECONDARY"}
    payload.update(extra)
    seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{seg}.sig"


class FakeTokens:
    def __init__(self, access: str, refresh: str = "RT-0") -> None:
        self.access_token = access
        self.refresh_token = refresh


class FakeApiError(Exception):
    def __init__(self, msg: str, *, code=None, status=None) -> None:
        super().__init__(msg)
        self.code = code
        self.status = status


class FakeTransport:
    def __init__(self, tokens: FakeTokens, *, respond=None) -> None:
        self.tokens = tokens
        self._refresh_hook = "ORIG"
        self.calls: list = []
        self._respond = respond or (lambda body: {"accessToken": _jwt(0, 7 * DAY),
                                                  "refreshToken": "RT-1"})

    def post_json(self, path, body, **kw):
        self.calls.append((path, dict(body), dict(kw), self._refresh_hook))
        out = self._respond(body)
        if isinstance(out, Exception):
            raise out
        return out


class FakeClient:
    def __init__(self, access: str, refresh: str = "RT-0", *, path: str = "", respond=None) -> None:
        self.transport = FakeTransport(FakeTokens(access, refresh), respond=respond)
        self._session_path = path
        self.closed = False

    def close(self):
        self.closed = True


def _session_file(tmp_path, access: str, refresh: str = "RT-0") -> Path:
    p = tmp_path / "Umid.json"
    p.write_text(json.dumps({
        "accessToken": access, "refreshToken": refresh, "certificate": "CERT",
        "mid": "Umid", "regionCode": None,
        "e2ee": {"mid": "Umid", "latestKeyId": 1, "keys": {"1": {"priv": "x"}}},
    }), encoding="utf-8")
    return p


# ── 纯函数 ──────────────────────────────────────────────────────────────────

def test_jwt_expiry_and_due_windows():
    now = 1_000_000.0
    tok = _jwt(now, now + 7 * DAY)
    assert LTR.access_token_expiry(tok) == now + 7 * DAY
    assert LTR.token_expired(tok, now=now) is False
    assert LTR.token_expired(tok, now=now + 7 * DAY) is True
    assert LTR.refresh_due(tok, now=now) is False                       # 刚签发
    assert LTR.refresh_due(tok, now=now + 5 * DAY + 1) is True          # 剩 <48h
    assert LTR.refresh_due(tok, now=now + 30 * DAY) is True             # 过期也算 due


def test_non_jwt_token_is_never_judged():
    assert LTR.decode_jwt_claims("not-a-jwt") == {}
    assert LTR.access_token_expiry("") == 0.0
    assert LTR.token_expired("opaque") is False
    assert LTR.refresh_due("opaque") is False


def test_error_classifiers():
    assert LTR.is_token_stale_error(FakeApiError("Access token refresh required", code=119))
    assert LTR.is_relogin_error(FakeApiError("REQUEST_NEED_LOGIN", code=10004, status=401))
    assert not LTR.is_relogin_error(FakeApiError("timeout"))


# ── session 文件回写 ─────────────────────────────────────────────────────────

def test_write_session_tokens_keeps_e2ee_and_certificate(tmp_path):
    p = _session_file(tmp_path, "A0")
    assert LTR.write_session_tokens(str(p), "A1", "RT-1") is True
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["accessToken"] == "A1" and d["refreshToken"] == "RT-1"
    assert d["certificate"] == "CERT"
    assert d["e2ee"]["keys"] == {"1": {"priv": "x"}}          # 绝不清空
    assert not (tmp_path / "Umid.json.tmp").exists()


def test_write_session_tokens_never_creates_half_session(tmp_path):
    assert LTR.write_session_tokens(str(tmp_path / "missing.json"), "A1", "RT-1") is False
    assert not (tmp_path / "missing.json").exists()


# ── 续期 ─────────────────────────────────────────────────────────────────────

def test_refresh_sends_x_line_access_and_persists(tmp_path):
    now = 1_000_000.0
    old = _jwt(now - 8 * DAY, now - DAY)                       # 已过期 1 天
    p = _session_file(tmp_path, old)
    cli = FakeClient(old, path=str(p))
    res = LTR.refresh_line_tokens(cli, str(p), reason="t")
    assert res["ok"] and res["source"] == "network" and res["rotated"] is True
    path, body, kw, hook_during = cli.transport.calls[0]
    assert path == "/api/auth/tokenRefresh" and body == {"refreshToken": "RT-0"}
    assert kw["require_auth"] is True and kw["allow_refresh"] is False
    assert hook_during is None                                  # 期间摘钩
    assert cli.transport._refresh_hook == "ORIG"                # 完了装回
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["accessToken"] == cli.transport.tokens.access_token
    assert d["refreshToken"] == "RT-1"
    assert d["e2ee"]["keys"]                                    # 密钥还在


def test_refresh_adopts_tokens_already_refreshed_by_sibling_client(tmp_path):
    """拉取兜底 / 主连接各一个 client：别人续过了就采用文件里的，不上网（refresh token 轮换）。"""
    now = 1_000_000.0
    old = _jwt(now - 8 * DAY, now - DAY)
    fresh = _jwt(now, now + 7 * DAY)
    p = _session_file(tmp_path, fresh, "RT-9")                  # 文件已是新 token
    cli = FakeClient(old, path=str(p))
    res = LTR.refresh_line_tokens(cli, str(p), reason="t", now=now)
    assert res["ok"] and res["source"] == "file"
    assert cli.transport.calls == []
    assert cli.transport.tokens.access_token == fresh
    assert cli.transport.tokens.refresh_token == "RT-9"


def test_refresh_gateway_need_login_is_relogin(tmp_path):
    old = _jwt(0, DAY)
    p = _session_file(tmp_path, old)
    cli = FakeClient(old, path=str(p), respond=lambda b: FakeApiError(
        "REQUEST_NEED_LOGIN", code=10004, status=401))
    res = LTR.refresh_line_tokens(cli, str(p))
    assert res["ok"] is False and res["relogin"] is True
    assert cli.transport._refresh_hook == "ORIG"
    assert json.loads(p.read_text(encoding="utf-8"))["accessToken"] == old   # 文件不动


def test_refresh_network_failure_is_not_relogin(tmp_path):
    old = _jwt(0, DAY)
    p = _session_file(tmp_path, old)
    cli = FakeClient(old, path=str(p), respond=lambda b: FakeApiError("request failed: timeout"))
    res = LTR.refresh_line_tokens(cli, str(p))
    assert res["ok"] is False and res["relogin"] is False
    assert "timeout" in res["error"]


def test_refresh_without_transport_is_noop():
    class Bare:
        pass
    res = LTR.refresh_line_tokens(Bare())
    assert res["ok"] is False and res["relogin"] is False


def test_ensure_fresh_token_only_when_due_and_cooldown(tmp_path):
    now = 1_000_000.0
    fresh = _jwt(now, now + 7 * DAY)
    p = _session_file(tmp_path, fresh)
    cli = FakeClient(fresh, path=str(p))
    assert LTR.ensure_fresh_token(cli, str(p), now=now)["due"] is False
    assert cli.transport.calls == []

    near = _jwt(now - 6 * DAY, now + DAY)                       # 剩 24h → due
    cli = FakeClient(near, path=str(_session_file(tmp_path, near)),
                     respond=lambda b: {"accessToken": _jwt(now, now + 7 * DAY),
                                        "refreshToken": "RT-1"})
    r1 = LTR.ensure_fresh_token(cli, cli._session_path, now=now)
    assert r1["due"] and r1["refreshed"] is True
    # 刚续过：新 token 7 天，不 due
    assert LTR.ensure_fresh_token(cli, cli._session_path, now=now)["due"] is False

    # 续期失败时 10 分钟冷却，不对着故障网关连打
    bad = _jwt(now - 6 * DAY, now + DAY)
    cli = FakeClient(bad, path=str(_session_file(tmp_path, bad)),
                     respond=lambda b: FakeApiError("boom"))
    r = LTR.ensure_fresh_token(cli, cli._session_path, now=now)
    assert r["due"] and r["refreshed"] is False and r["relogin"] is False
    r2 = LTR.ensure_fresh_token(cli, cli._session_path, now=now + 60)
    assert r2["skipped"] is True
    assert len(cli.transport.calls) == 1
    r3 = LTR.ensure_fresh_token(cli, cli._session_path, now=now + LTR.ATTEMPT_COOLDOWN_SEC + 1)
    assert r3["skipped"] is False and len(cli.transport.calls) == 2


def test_install_refresh_hook_replaces_okline_hook(tmp_path):
    old = _jwt(0, DAY)
    p = _session_file(tmp_path, old)
    cli = FakeClient(old, path=str(p))
    assert LTR.install_refresh_hook(cli, str(p)) is True
    assert callable(cli.transport._refresh_hook)
    assert cli.transport._refresh_hook() is True                # 401 → 续期成功
    assert cli.transport.tokens.refresh_token == "RT-1"


# ── 复活扫描 ─────────────────────────────────────────────────────────────────

class FakeRegistry:
    def __init__(self, rows):
        self.rows = rows

    def list(self):
        return list(self.rows)


def _row(mid, path, status="offline", reason="worker:expired", platform="line", mode="protocol"):
    return {"platform": platform, "mode": mode, "account_id": mid, "status": status,
            "meta": {"offline_reason": reason, "tokens_path": path}}


@pytest.fixture(autouse=True)
def _clear_ledger():
    LTR._REVIVE_LEDGER.clear()
    yield
    LTR._REVIVE_LEDGER.clear()


def test_revive_refreshes_expired_account_and_reports_authorized(tmp_path):
    now = 1_000_000.0
    old = _jwt(now - 40 * DAY, now - 33 * DAY)                  # 过期 33 天（实测仍可续）
    p = _session_file(tmp_path, old)
    made, reports = [], []

    def factory(path):
        c = FakeClient(old, path=path)
        made.append(c)
        return c

    reg = FakeRegistry([
        _row("Uexp", str(p)),
        _row("Uop", str(p), reason="operator"),                # 运营主动登出：不碰
        _row("Uon", str(p), status="online", reason=""),        # 在线：不碰
        _row("Utg", str(p), platform="telegram"),               # 别的平台：不碰
        _row("Umissing", str(tmp_path / "nope.json")),          # 没文件：不碰
    ])
    out = LTR.revive_expired_line_accounts(
        reg, {}, now=now, client_factory=factory,
        report=lambda plat, acct, st, detail="": reports.append((plat, acct, st)))
    assert out == {"checked": 1, "revived": 1, "failed": 0, "skipped": 0}
    assert reports == [("line", "Uexp", "authorized")]
    assert made[0].closed is True
    assert json.loads(p.read_text(encoding="utf-8"))["refreshToken"] == "RT-1"
    assert "Uexp" not in LTR._REVIVE_LEDGER


def test_revive_dead_refresh_token_backs_off(tmp_path):
    now = 1_000_000.0
    old = _jwt(0, DAY)
    p = _session_file(tmp_path, old)
    calls = []

    def factory(path):
        calls.append(path)
        return FakeClient(old, path=path, respond=lambda b: FakeApiError(
            "REQUEST_NEED_LOGIN", code=10004, status=401))

    reg = FakeRegistry([_row("Udead", str(p))])
    kw = dict(client_factory=factory, report=lambda *a, **k: None)
    assert LTR.revive_expired_line_accounts(reg, {}, now=now, **kw)["failed"] == 1
    # 30 分钟内不再打
    assert LTR.revive_expired_line_accounts(reg, {}, now=now + 60, **kw)["skipped"] == 1
    assert len(calls) == 1
    # 首败退避 30min 到点 → 再试；再败 → 1h
    assert LTR.revive_expired_line_accounts(reg, {}, now=now + 1801, **kw)["failed"] == 1
    assert len(calls) == 2
    assert LTR.revive_expired_line_accounts(reg, {}, now=now + 1801 + 3000, **kw)["skipped"] == 1
    assert LTR._REVIVE_LEDGER["Udead"]["fails"] == 2
    assert LTR._revive_wait_sec(20) == LTR._REVIVE_CAP_SEC


# ── worker / 编排器接线 ratchet ───────────────────────────────────────────────

def test_worker_wiring():
    src = (REPO / "src/integrations/account_orchestrator.py").read_text(encoding="utf-8")
    start = src[src.index("class LineProtocolWorker"):]
    seg = start[start.index("async def start(self)"):start.index("def _sync_cfg")]
    assert "install_refresh_hook(self.client, path)" in seg
    assert "ensure_fresh_token, self.client, path" in seg
    assert "self._start_token_keeper(path)" in seg
    assert "self._recv_stop.set()" in start[start.index("async def stop(self)"):]
    assert "await self._revive_expired_line()" in src[src.index("async def _loop(self)"):]
