# -*- coding: utf-8 -*-
"""yt_auth.py — YouTube 上传授权（一次性，本机浏览器完成）。

前置（约 5 分钟）：
  1. https://console.cloud.google.com → 新建项目（如 avatarhub-publish）
  2. 「API 和服务」→ 启用 YouTube Data API v3
  3. OAuth 同意屏幕：External，填应用名和自己邮箱；「测试用户」加上自己的 Google 账号
  4. 「凭据」→ 创建凭据 → OAuth 客户端 ID → 应用类型选「桌面应用」
  5. 下载 JSON，改名 client_secret.json 放到 D:\\projects\\模仿音色\\secrets\\youtube\\

然后运行本脚本：python publish/yt_auth.py
浏览器弹出 Google 登录 → 选择要上传到的频道账号 → 允许 → 完成。
成功后生成 secrets/youtube/token.json，publish_daily.py 即自动带 YouTube 上传。

注意：未过「YouTube API 合规审核」的项目，API 上传的视频会被强制设为私享。
提交审核表单（免费）：https://support.google.com/youtube/contact/yt_api_form
审核通过后（一般数个工作日）即可正常公开发布；等待期间其它两端不受影响。
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

YT_DIR = Path(__file__).resolve().parent.parent / "secrets" / "youtube"
PORT = 8765
SCOPE = "https://www.googleapis.com/auth/youtube.upload"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def load_client() -> dict:
    f = YT_DIR / "client_secret.json"
    if not f.exists():
        print(f"[错误] 缺 {f}\n请按脚本开头的步骤从 Google Cloud Console 下载 OAuth 桌面端凭据。")
        sys.exit(2)
    data = json.loads(f.read_text(encoding="utf-8"))
    return data.get("installed") or data.get("web") or {}


class CodeCatcher(BaseHTTPRequestHandler):
    code: str | None = None

    def do_GET(self):  # noqa: N802
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        CodeCatcher.code = (qs.get("code") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = CodeCatcher.code is not None
        self.wfile.write(
            ("<h2>✅ 授权成功，可以关闭此页面回到终端。</h2>" if ok
             else "<h2>❌ 未拿到授权码，请回终端重试。</h2>").encode("utf-8"))

    def log_message(self, *a):  # 静默
        pass


def main():
    client = load_client()
    cid, csec = client.get("client_id"), client.get("client_secret")
    if not cid or not csec:
        print("[错误] client_secret.json 里没有 client_id/client_secret（要选「桌面应用」类型）。")
        sys.exit(2)

    redirect = f"http://127.0.0.1:{PORT}"
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    })

    srv = HTTPServer(("127.0.0.1", PORT), CodeCatcher)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    print("[授权] 正在打开浏览器……若未自动打开，手动访问：\n" + auth_url)
    webbrowser.open(auth_url)

    import time
    for _ in range(600):  # 最多等 10 分钟
        if CodeCatcher.code:
            break
        time.sleep(1)
    srv.server_close()
    if not CodeCatcher.code:
        print("[错误] 超时未完成授权。")
        sys.exit(1)

    data = urllib.parse.urlencode({
        "client_id": cid, "client_secret": csec,
        "code": CodeCatcher.code, "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.loads(r.read())
    refresh = tok.get("refresh_token")
    if not refresh:
        print(f"[错误] 未返回 refresh_token：{tok}\n（若此前授权过，请到 myaccount.google.com/permissions 移除后重试）")
        sys.exit(1)

    YT_DIR.mkdir(parents=True, exist_ok=True)
    (YT_DIR / "token.json").write_text(
        json.dumps({"client_id": cid, "client_secret": csec, "refresh_token": refresh}, indent=2),
        encoding="utf-8")
    print(f"[完成] token 已保存 → {YT_DIR / 'token.json'}\n之后 publish_daily.py 会自动上传 YouTube。")
    print("[提醒] 项目未过合规审核前，API 上传会被 YouTube 锁为私享；审核表单见脚本开头注释。")


if __name__ == "__main__":
    main()
