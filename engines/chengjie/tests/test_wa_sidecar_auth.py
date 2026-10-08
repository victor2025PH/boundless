"""P0-1 WhatsApp Baileys 边车入站鉴权（2026-10-08）。

钉住三件事：
1. Node 侧：sidecar-auth.js 单测（无 token 401 / 有 token 200 / /health 放行 / 非回环无令牌拒绝启动），
   node 不在 PATH 时 skip（不假绿）；
2. Python 侧：调边车的唯一出口 ``_post_json`` / ``_get_json`` 自动带
   ``Authorization: Bearer <边车令牌>``，令牌来源 env → 实例 config 目录 key 文件，没有则不带头；
   令牌值不进日志、不进诊断报告；
3. 启动脚本：边车令牌独立生成，不再从 web_admin.auth_token 派生；ingest 方向优先 worker_token。

测试令牌在运行时拼出，避免密钥扫描误报。
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import shutil
import subprocess

import httpx
import pytest

from src.integrations import whatsapp_baileys_login as wbl

ENGINE = pathlib.Path(__file__).resolve().parents[1]
SVC = ENGINE / "services" / "whatsapp-baileys"
TOKEN = "k" * 20 + "-wa-sidecar-" + "7" * 20


@pytest.fixture(autouse=True)
def _isolate_token(monkeypatch, tmp_path):
    monkeypatch.delenv(wbl.SIDECAR_TOKEN_ENV, raising=False)
    monkeypatch.setenv(wbl.SIDECAR_TOKEN_FILE_ENV, str(tmp_path / "wa_sidecar_token.key"))
    wbl._token_cache.update(path=None, mtime=None, token="")
    wbl._last_401_warn = float("-inf")
    yield


def _capture_client(monkeypatch, status=200, body=None):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else {"ok": True})

    real = httpx.AsyncClient

    class _Client(real):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return seen


# ── 令牌解析 ─────────────────────────────────────────────────────────────────

def test_no_token_means_no_header():
    assert wbl.sidecar_token() == ("", "")
    assert wbl.sidecar_auth_headers() == {}
    st = wbl.sidecar_auth_status()
    assert st["configured"] is False and st["source"] == ""


def test_token_from_file_with_bom_and_whitespace(tmp_path):
    p = tmp_path / "wa_sidecar_token.key"
    p.write_bytes(b"\xef\xbb\xbf" + TOKEN.encode() + b"\r\n")
    assert wbl.sidecar_token() == (TOKEN, "file")
    assert wbl.sidecar_auth_headers() == {"Authorization": f"Bearer {TOKEN}"}


def test_env_wins_over_file(monkeypatch, tmp_path):
    (tmp_path / "wa_sidecar_token.key").write_text("f" * 40, encoding="ascii")
    monkeypatch.setenv(wbl.SIDECAR_TOKEN_ENV, f"  {TOKEN} ")
    assert wbl.sidecar_token() == (TOKEN, "env")


def test_file_rotation_picked_up_without_engine_restart(tmp_path):
    p = tmp_path / "wa_sidecar_token.key"
    p.write_text("a" * 40, encoding="ascii")
    assert wbl.sidecar_token()[0] == "a" * 40
    p.write_text("b" * 40, encoding="ascii")
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 5))
    assert wbl.sidecar_token()[0] == "b" * 40


def test_default_path_follows_instance_config_dir(monkeypatch, tmp_path):
    monkeypatch.delenv(wbl.SIDECAR_TOKEN_FILE_ENV, raising=False)
    monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path / "data"))
    assert wbl.sidecar_token_path() == tmp_path / "data" / "config" / "wa_sidecar_token.key"
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "cfg" / "config.yaml"))
    assert wbl.sidecar_token_path() == tmp_path / "cfg" / "wa_sidecar_token.key"


def test_status_never_contains_token(tmp_path):
    (tmp_path / "wa_sidecar_token.key").write_text(TOKEN, encoding="ascii")
    st = wbl.sidecar_auth_status()
    assert st["configured"] is True and st["source"] == "file"
    assert TOKEN not in repr(st)


# ── HTTP 出口带头 ────────────────────────────────────────────────────────────

def test_post_and_get_carry_bearer(monkeypatch, tmp_path):
    (tmp_path / "wa_sidecar_token.key").write_text(TOKEN, encoding="ascii")
    seen = _capture_client(monkeypatch)
    asyncio.run(wbl._post_json("http://127.0.0.1:8790/accounts/a1/send", {"to": "x"}))
    asyncio.run(wbl._get_json("http://127.0.0.1:8790/accounts"))
    assert [r.headers.get("authorization") for r in seen] == [f"Bearer {TOKEN}"] * 2


def test_no_token_sends_no_auth_header(monkeypatch):
    seen = _capture_client(monkeypatch)
    asyncio.run(wbl._get_json("http://127.0.0.1:8790/accounts"))
    assert "authorization" not in seen[0].headers


def test_401_raises_and_warns_without_leaking_token(monkeypatch, tmp_path, caplog):
    (tmp_path / "wa_sidecar_token.key").write_text(TOKEN, encoding="ascii")
    _capture_client(monkeypatch, status=401, body={"ok": False, "error": "unauthorized"})
    caplog.set_level(logging.WARNING, logger=wbl.logger.name)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(wbl._post_json("http://127.0.0.1:8790/accounts/a1/send?x=1", {}))
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "401" in msgs and "/accounts/a1/send" in msgs
    assert TOKEN not in msgs


def test_existing_monkeypatch_signatures_untouched():
    """91 处测试按 (url, payload[, timeout]) 替换 _post_json——签名不能变。"""
    import inspect
    assert list(inspect.signature(wbl._post_json).parameters) == ["url", "payload", "timeout"]
    assert list(inspect.signature(wbl._get_json).parameters) == ["url", "timeout"]


def test_diagnostics_report_exposes_auth_state_not_token(tmp_path):
    from src.integrations.protocol_diagnostics import _whatsapp_report_static
    cfg = {"platform_login": {"whatsapp": {"protocol_enabled": True}}}
    rep = _whatsapp_report_static(cfg)
    assert rep["sidecar_auth"]["configured"] is False
    assert any("鉴权未配置" in h for h in rep["hints"])
    (tmp_path / "wa_sidecar_token.key").write_text(TOKEN, encoding="ascii")
    rep2 = _whatsapp_report_static(cfg)
    assert rep2["sidecar_auth"]["configured"] is True
    assert not any("鉴权未配置" in h for h in rep2["hints"])
    assert TOKEN not in repr(rep2)


# ── 启动脚本 / 看门狗契约（源文本断言） ─────────────────────────────────────

def test_start_ps1_uses_independent_sidecar_token():
    src = (SVC / "start.ps1").read_text(encoding="utf-8-sig")
    assert "$env:SIDECAR_TOKEN" in src
    assert "wa_sidecar_token.key" in src
    assert "RandomNumberGenerator" in src, "缺令牌时必须随机生成"
    # 边车令牌不得来自 web_admin 的任何 token 键
    for line in src.splitlines():
        if "$env:SIDECAR_TOKEN =" in line:
            assert "auth_token" not in line and "worker_token" not in line and "PY_API_TOKEN" not in line
    assert src.index('"worker_token"') < src.index('"auth_token"'), "ingest 方向优先 worker_token"
    assert '$env:BIND_HOST = "127.0.0.1"' in src


def test_watchdog_sends_sidecar_token_to_node():
    src = (ENGINE / "scripts" / "watchdog_wa_baileys.ps1").read_text(encoding="utf-8")
    assert "wa_sidecar_token.key" in src
    assert src.count("-Headers (Get-SidecarHeaders)") >= 2   # GET /accounts + POST reconnect
    assert all(ord(c) < 128 for c in src), "看门狗脚本约定纯 ASCII（PS5.1 GBK 解码）"


# ── Node 单测 ────────────────────────────────────────────────────────────────

@pytest.mark.skipif(shutil.which("node") is None, reason="node 不在 PATH：Node 鉴权单测跳过（不假绿）")
def test_node_sidecar_auth_tests_pass():
    r = subprocess.run(["node", "--test", "test/sidecar-auth.test.js"], cwd=str(SVC),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
