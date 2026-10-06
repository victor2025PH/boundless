#!/usr/bin/env python3
"""控制台体检：deploy 之后用主令牌拉 /fleet/console，断言写操作路径没被改坏、前缀没补两遍。

    python3 console_check.py --config /etc/chatx-fleet/config.yaml \\
        [--base http://127.0.0.1:18798] [--public-url https://bd2026.cc/fleet] [--prefix /fleet/ui]

为什么要它（2026-10-05/06）：VPS 手补的 HTML 改写正则把控制台里的拼接片段改成
``/fleet/ui/revoke``、``/fleet/ui/tasks`` 等，节点撤销 / 下发任务 / 审批全部 404，
而 deploy 原有的健康检查只看 ``/fleet/`` 返回 200，完全发现不了。

鉴权：读 config 的 ``web_admin.auth_token``（env 文件里的 ``AITR_WEB_TOKEN`` 优先，与
ConfigManager 同口径），走本机 ``POST /login`` 令牌登录拿会话（页面鉴权只认会话，不认 Bearer），
拉完页面 ``GET /logout`` 收尾。令牌只进表单体，从不打印、不进命令行。
副作用只有一条 admin 会话记录（专用 UA，同设备替换，不影响操作员会话）。
退出码：0 通过；1 有问题；2 用法 / 配置错误。
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

_SEG_RE = re.compile(r"^(/[A-Za-z0-9._\-]+)+$")
# 控制台写操作的尾段（fleet_console.html 只经 fa() 拼，不该以「/<尾段>」字面量出现在前缀后面）
_WRITE_TAILS = ("revoke", "tasks", "approve", "reject", "cancel", "login-qr", "status")
_ATTR_RE = re.compile(r"""\b(?:href|src|action|formaction)\s*=\s*(["'])(/[^"'<>]*)\1""", re.I)
_API_BASE_RE = re.compile(r"""id=["']fc-cfg["'][^>]*\bdata-api-base=["']([^"']*)["']""")


def clean_path(value) -> str:
    s = str(value or "").strip().rstrip("/")
    if not _SEG_RE.match(s) or any(seg in (".", "..") for seg in s.split("/")):
        return ""
    return s


def analyze_console(html: str, prefix: str = "", api_base: str = "") -> List[str]:
    """返回问题列表（空 = 通过）。prefix = web_admin.public_prefix；api_base = 控制台应注入的 API 前缀。"""
    problems: List[str] = []
    if 'id="fc-cfg"' not in html and "id='fc-cfg'" not in html:
        return ["not the fleet console page (no #fc-cfg element; login page or error page?)"]
    if "function fa(" not in html:
        problems.append("console API helper fa() missing")
    m = _API_BASE_RE.search(html)
    got = m.group(1) if m else None
    if got is None:
        problems.append("data-api-base attribute missing")
    elif got != api_base:
        problems.append(f"data-api-base={got!r}, expected {api_base!r}")
    for p in {prefix, api_base} - {""}:
        if p + p in html:
            problems.append(f"double prefix {p + p!r} found x{html.count(p + p)}")
        tails = "|".join(re.escape(t) for t in _WRITE_TAILS)
        bad = re.findall(r"""["'`]""" + re.escape(p) + r"/(?:" + tails + r""")\b[^"'`]{0,40}""", html)
        if bad:
            problems.append(f"broken write paths under {p}: {len(bad)} e.g. {bad[:3]}")
    if prefix:
        mount = "/" + prefix.strip("/").split("/", 1)[0]
        loose = sorted({u for _, u in _ATTR_RE.findall(html)
                        if not (u.startswith("//") or u == mount or u.startswith(mount + "/")
                                or u.startswith("/downloads/"))})
        if loose:
            problems.append(f"links outside {mount} (prefix not applied): {len(loose)} e.g. {loose[:5]}")
    return problems


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D401 - urllib API
        return None


UA = "chatx-fleet-console-check/1"


def http_request(method: str, url: str, headers: Optional[Dict[str, str]] = None,
                 data: Optional[bytes] = None, timeout: float = 15.0) -> Tuple[int, Dict[str, List[str]], str]:
    """不跟随跳转。返回 (status, {小写头名: [值...]}, body)。"""
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, data=data, method=method)
    opener = urllib.request.build_opener(_NoRedirect)

    def _hdrs(msg) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for k, v in (msg.items() if msg is not None else []):
            out.setdefault(k.lower(), []).append(v)
        return out

    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, _hdrs(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        return e.code, _hdrs(e.headers), body


def http_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 15.0) -> Tuple[int, str, str]:
    """GET，不跟随跳转。返回 (status, location, body)。"""
    st, h, body = http_request("GET", url, headers, None, timeout)
    return st, (h.get("location") or [""])[0], body


def token_login(base: str, token: str) -> Tuple[str, str]:
    """本机令牌登录。返回 (Cookie 头, 错误)。Secure cookie 也照带（本机 http 回环）。"""
    from urllib.parse import urlencode

    st, h, body = http_request("POST", base.rstrip("/") + "/login",
                               {"Content-Type": "application/x-www-form-urlencoded"},
                               urlencode({"auth_token": token, "manual": "1"}).encode())
    cookies = [c.split(";", 1)[0].strip() for c in h.get("set-cookie", []) if "=" in c.split(";", 1)[0]]
    if st not in (302, 303) or not cookies:
        return "", f"token login -> HTTP {st}{' (token rejected?)' if st == 200 else ''}"
    return "; ".join(cookies), ""


def _read_env_token(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].strip()
                if line.startswith("AITR_WEB_TOKEN="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def load_settings(config_path: str, env_file: str = "") -> Dict[str, str]:
    import yaml  # 主控 venv 自带

    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    web = cfg.get("web_admin") or {}
    fleet = cfg.get("fleet_control") or {}
    prefix = clean_path(web.get("public_prefix"))
    login = clean_path(web.get("public_login_path"))
    if prefix and not login:
        parent = prefix.rsplit("/", 1)[0]
        login = (parent or prefix) + "/login"
        if login == "/login":
            login = prefix + "/login"
    token = (_read_env_token(env_file) if env_file else "") or str(web.get("auth_token") or "")
    return {
        "token": token,
        "prefix": prefix,
        "login_path": login or "/login",
        "api_base": clean_path(fleet.get("console_api_base")) or prefix,
    }


def run(base: str, settings: Dict[str, str], public_url: str = "", out=print) -> int:
    problems: List[str] = []
    token = settings.get("token") or ""
    if not token:
        out("[console-check] no web_admin.auth_token / AITR_WEB_TOKEN available")
        return 2
    cookie, err = token_login(base, token)
    if err:
        problems.append(err)
    else:
        try:
            st, loc, html = http_get(base.rstrip("/") + "/fleet/console", {"Cookie": cookie, "Accept": "text/html"})
        finally:
            try:
                http_get(base.rstrip("/") + "/logout", {"Cookie": cookie})
            except Exception:  # noqa: BLE001 - 收尾失败不影响结论
                pass
        if st != 200:
            problems.append(f"GET /fleet/console -> HTTP {st}{(' Location=' + loc) if loc else ''}")
        else:
            problems += analyze_console(html, settings.get("prefix", ""), settings.get("api_base", ""))
            out(f"[console-check] console {len(html)} bytes, api_base={settings.get('api_base')!r}, "
                f"prefix={settings.get('prefix')!r}")
    prefix = settings.get("prefix", "")
    if public_url:
        parts = urlsplit(public_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        probes = [(origin + settings.get("login_path", "/login"), "<form")]
        if prefix:
            probes.append((origin + prefix + "/static/brand/brand.css", ""))
        for url, needle in probes:
            st, loc, body = http_get(url)
            if st != 200 or (needle and needle not in body):
                problems.append(f"public GET {url} -> HTTP {st}{' (no ' + needle + ')' if st == 200 else ''}")
            else:
                out(f"[console-check] public {url}: 200")
    for p in problems:
        out(f"[console-check] FAIL {p}")
    if not problems:
        out("[console-check] OK")
    return 1 if problems else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--env-file", default="/etc/chatx-fleet/env")
    ap.add_argument("--base", default="http://127.0.0.1:18798")
    ap.add_argument("--public-url", default="")
    ap.add_argument("--prefix", default=None, help="override web_admin.public_prefix (e.g. for a pre-fold app)")
    a = ap.parse_args(argv)
    try:
        s = load_settings(a.config, a.env_file)
    except Exception as e:  # noqa: BLE001
        print(f"[console-check] cannot read config: {type(e).__name__}")
        return 2
    if a.prefix is not None:
        s["prefix"] = clean_path(a.prefix)
        s["api_base"] = s["api_base"] or s["prefix"]
        if s["prefix"] and s["login_path"] == "/login":
            parent = s["prefix"].rsplit("/", 1)[0]
            s["login_path"] = (parent + "/login") if parent else (s["prefix"] + "/login")
    return run(a.base, s, a.public_url)


if __name__ == "__main__":
    sys.exit(main())
