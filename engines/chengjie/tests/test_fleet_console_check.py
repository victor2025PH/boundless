"""deploy/fleet/console_check.py + deploy_controller.sh 控制台体检接线。

守住：
1. 真实渲染的控制台（默认模式 / 前缀模式）体检通过；
2. VPS ``_PAGE`` 事故形态（'/fleet/ui/revoke'、前缀补两遍、data-api-base 不对、拿到的是登录页）都能抓到；
3. 令牌只进请求头，不出现在任何输出里；
4. deploy_controller.sh：控制台体检失败 → 回滚；手补版现网 + config 没开 public_prefix → 动手前拒绝。
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "fleet"


def _load():
    spec = importlib.util.spec_from_file_location("fleet_console_check", DEPLOY / "console_check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


cc = _load()


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    from starlette.testclient import TestClient
    from tests.test_fleet_public_prefix import _login, _make_app
    out = {}
    for mode, web in (("default", {}), ("prefix", {"public_prefix": "/fleet/ui"})):
        tmp = tmp_path_factory.mktemp(mode)
        with TestClient(_make_app(tmp, web)) as c:
            _login(c, "/fleet/login" if web else "/login")
            r = c.get("/fleet/console")
            assert r.status_code == 200
            out[mode] = r.text
    return out


def test_real_console_passes_in_both_modes(rendered):
    assert cc.analyze_console(rendered["default"], "", "") == []
    assert cc.analyze_console(rendered["prefix"], "/fleet/ui", "/fleet/ui") == []


def test_detects_page_regex_damage(rendered):
    html = rendered["prefix"]
    broken = html.replace("</body>", "<script>fetch('/fleet/ui/api/fleet/nodes/'+id+'/fleet/ui/revoke')</script></body>")
    probs = cc.analyze_console(broken, "/fleet/ui", "/fleet/ui")
    assert any("broken write paths" in p for p in probs), probs
    doubled = html.replace("</body>", "<a href='/fleet/ui/fleet/ui/x'>x</a></body>")
    assert any("double prefix" in p for p in cc.analyze_console(doubled, "/fleet/ui", "/fleet/ui"))
    assert any("data-api-base" in p for p in cc.analyze_console(html, "/fleet/ui", "/fleet/x"))
    loose = html.replace("</body>", '<link href="/static/x.css"></body>')
    assert any("prefix not applied" in p for p in cc.analyze_console(loose, "/fleet/ui", "/fleet/ui"))
    assert cc.analyze_console("<form action='/login'>", "/fleet/ui", "/fleet/ui")[0].startswith("not the fleet console")


def test_run_never_prints_token(monkeypatch, rendered):
    token = "tok-SECRET-0123456789abcdef"
    seen = []

    def fake_req(method, url, headers=None, data=None, timeout=15.0):
        seen.append((method, url))
        headers = headers or {}
        if url.endswith("/login") and method == "POST":
            assert token.encode() in data and "Authorization" not in headers
            return 303, {"set-cookie": ["session=abc; Path=/; Secure; HttpOnly"], "location": ["/"]}, ""
        if url.endswith("/fleet/console"):
            assert headers.get("Cookie") == "session=abc"
            return 200, {}, rendered["prefix"]
        if url.endswith("/logout"):
            return 303, {"location": ["/login?manual=1"]}, ""
        if url.endswith("/fleet/login"):
            return 200, {}, "<form method=post>"
        return 200, {}, "body{}"

    monkeypatch.setattr(cc, "http_request", fake_req)
    lines = []
    s = {"token": token, "prefix": "/fleet/ui", "login_path": "/fleet/login", "api_base": "/fleet/ui"}
    assert cc.run("http://127.0.0.1:18798", s, "https://bd2026.cc/fleet", out=lines.append) == 0
    assert seen == [("POST", "http://127.0.0.1:18798/login"), ("GET", "http://127.0.0.1:18798/fleet/console"),
                    ("GET", "http://127.0.0.1:18798/logout"), ("GET", "https://bd2026.cc/fleet/login"),
                    ("GET", "https://bd2026.cc/fleet/ui/static/brand/brand.css")]
    assert lines[-1].endswith("OK") and not any(token in x for x in lines)

    # 令牌被拒（登录页 200）/ 会话不认（303 回登录）→ 失败，且同样不泄露令牌
    monkeypatch.setattr(cc, "http_request", lambda m, u, headers=None, data=None, timeout=15.0: (200, {}, "<form>"))
    lines.clear()
    assert cc.run("http://127.0.0.1:18798", s, "", out=lines.append) == 1
    assert any("token rejected" in x for x in lines) and not any(token in x for x in lines)

    def login_ok_console_303(m, u, headers=None, data=None, timeout=15.0):
        if m == "POST":
            return 303, {"set-cookie": ["session=abc; Path=/"]}, ""
        return 303, {"location": ["/fleet/login?next=%2Ffleet%2Fconsole"]}, ""
    monkeypatch.setattr(cc, "http_request", login_ok_console_303)
    lines.clear()
    assert cc.run("http://127.0.0.1:18798", s, "", out=lines.append) == 1
    assert any("GET /fleet/console -> HTTP 303" in x for x in lines)
    assert cc.run("http://x", dict(s, token=""), "", out=lines.append) == 2


def test_token_login_against_real_app(tmp_path):
    """真实 create_app（前缀模式）：令牌登录 → 拉控制台 → 体检通过（走 ASGI，不出网）。"""
    from starlette.testclient import TestClient
    from tests.test_fleet_public_prefix import _TOKEN, _make_app

    app = _make_app(tmp_path, {"public_prefix": "/fleet/ui"})
    with TestClient(app, base_url="http://127.0.0.1:18798") as c:
        def via_client(method, url, headers=None, data=None, timeout=15.0):
            path = url.split("18798", 1)[1]
            r = c.request(method, path, headers=headers, content=data, follow_redirects=False)
            c.cookies.clear()                       # 只靠显式 Cookie 头，验证脚本自己的 cookie 处理
            hd = {}
            for k, v in r.headers.multi_items():
                hd.setdefault(k.lower(), []).append(v)
            return r.status_code, hd, r.text

        lines = []
        orig = cc.http_request
        cc.http_request = via_client
        try:
            s = {"token": _TOKEN, "prefix": "/fleet/ui", "login_path": "/fleet/login", "api_base": "/fleet/ui"}
            rc = cc.run("http://127.0.0.1:18798", s, "", out=lines.append)
        finally:
            cc.http_request = orig
        assert rc == 0, lines
        assert not any(_TOKEN in x for x in lines)


def test_load_settings(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("web_admin:\n  auth_token: from-config\n  public_prefix: /fleet/ui/\n"
                   "fleet_control:\n  console_api_base: ''\n", encoding="utf-8")
    env = tmp_path / "env"
    env.write_text("# c\nexport AITR_WEB_TOKEN='from-env'\n", encoding="utf-8")
    s = cc.load_settings(str(cfg), str(env))
    assert s == {"token": "from-env", "prefix": "/fleet/ui", "login_path": "/fleet/login", "api_base": "/fleet/ui"}
    s = cc.load_settings(str(cfg), str(tmp_path / "missing"))
    assert s["token"] == "from-config"
    cfg.write_text("web_admin:\n  auth_token: t\nfleet_control:\n  console_api_base: /fleet/ui\n", encoding="utf-8")
    s = cc.load_settings(str(cfg), "")
    assert s["prefix"] == "" and s["login_path"] == "/login" and s["api_base"] == "/fleet/ui"


def test_deploy_script_wires_console_check_and_preflight():
    sh = (DEPLOY / "deploy_controller.sh").read_text(encoding="utf-8")
    assert "--console-check) console_check; exit $? ;;" in sh
    # 体检在健康检查之后、失败即回滚；跳过只能显式 CONSOLE_CHECK=0
    tail = sh[sh.index('log "8/8 health"'):]
    assert "console_check && { ok=1; break; }" in tail
    assert 'log "console check failed, rolling back"; rollback || true; exit 1;' in tail
    assert 'CONSOLE_CHECK="${CONSOLE_CHECK:-1}"' in sh
    # 前缀守卫在解包 / pip / 切换之前
    assert sh.index("\npreflight_public_prefix\n") < sh.index('log "1/8 user + dirs"')
    assert 'chmod -R go-w "$APP_ROOT/app.new"' in sh
    # 令牌不进命令行
    assert not re.search(r"Bearer \$|auth_token=\$\(", sh.split("log \"4/8 config\"")[0])


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="needs a POSIX bash")
def test_preflight_blocks_hand_patched_live_without_prefix(tmp_path):
    sh = (DEPLOY / "deploy_controller.sh").read_text(encoding="utf-8")
    fn = re.search(r"^preflight_public_prefix\(\) \{.*?^\}\n", sh, re.S | re.M).group(0)
    app = tmp_path / "opt"
    (app / "app/src/web").mkdir(parents=True)
    (app / "venv/bin").mkdir(parents=True)
    py = app / "venv/bin/python"
    py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
    (app / "app/src/web/admin.py").write_text("class FleetUiPrefixMiddleware: pass\n")
    conf = tmp_path / "etc"
    conf.mkdir()
    harness = ('log(){ echo "LOG $*"; }; fail(){ echo "FAIL $*"; exit 1; }\n'
               f'APP_ROOT="{app}"; CONF_DIR="{conf}"\n{fn}\npreflight_public_prefix; echo PASSED\n')

    def go(cfg_text):
        (conf / "config.yaml").write_text(cfg_text)
        return subprocess.run(["bash", "-c", harness], capture_output=True, text=True)

    r = go("web_admin:\n  auth_token: x\n")
    assert r.returncode == 1 and "has no web_admin.public_prefix" in r.stdout
    r = go("web_admin:\n  public_prefix: /fleet/ui\n")
    assert r.returncode == 0 and "PASSED" in r.stdout
    (app / "app/src/web/admin.py").write_text("# repo build\n")
    r = go("web_admin: {}\n")
    assert r.returncode == 0 and "PASSED" in r.stdout
