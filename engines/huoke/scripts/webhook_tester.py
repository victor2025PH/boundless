# -*- coding: utf-8 -*-
"""webhook_tester.py - 本地验签接收服务 (Phase 6 P1, 2026-04-22).

用于在真机/开发机端验证 src.host.lead_mesh.webhook_dispatcher 发出的
HMAC-SHA256 签名是否和接收方期望一致，以及 payload 的字段格式。

用法::

    # 方式 1: 直接传 secret (便利, 但会在进程表泄漏)
    python scripts/webhook_tester.py --port 9876 --secret my-ops-secret

    # 方式 2: 从环境变量读 secret (推荐, 和生产一致)
    export WEBHOOK_SECRET_OPS="my-ops-secret"
    python scripts/webhook_tester.py --port 9876 --secret-env WEBHOOK_SECRET_OPS

    # 方式 3: 关闭验签 (只看 payload, 不验)
    python scripts/webhook_tester.py --port 9876 --no-verify

    # 模拟 5xx 让 dispatcher 进入重试 (测 retry/DLQ 闭环)
    python scripts/webhook_tester.py --port 9876 --secret x --respond 500

启动后把 http://127.0.0.1:9876/hook 填到 config/webhook_targets.yaml 或某 receiver
的 webhook_url, 发送一个 handoff 即可看到实时打印。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Windows cmd 默认 GBK 编码会让 ✓/✗ 崩溃, 主动切成 utf-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                    errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                                    errors="replace")


OK = "[OK]"
NO = "[XX]"
DOT = "  -"


def _fmt_now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")


def _verify_signature(secret: str, body: bytes, header: str) -> bool:
    """Dispatcher 格式: X-OpenClaw-Signature: sha256=<hex>."""
    if not secret or not header:
        return False
    if "=" not in header:
        return False
    algo, _, got = header.partition("=")
    if algo.lower() != "sha256":
        return False
    want = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want.lower(), got.strip().lower())


class WebhookHandler(BaseHTTPRequestHandler):
    # 注入: 启动后在 main() 里赋值到 class 属性
    secret: str = ""
    verify: bool = True
    respond_status: int = 200
    respond_body: bytes = b'{"ok":true}'
    counter: int = 0

    def log_message(self, fmt, *args):  # 静默掉默认 HTTP 日志
        return

    def _stamp(self) -> str:
        return f"{_fmt_now()} #{WebhookHandler.counter}"

    def do_POST(self):  # noqa: N802 (BaseHTTPRequestHandler 约定)
        WebhookHandler.counter += 1
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        # 强制每行 flush, 保证管道/tee 不 buffer
        def p(msg=""):
            print(msg, flush=True)

        event = self.headers.get("X-OpenClaw-Event") or "(no-event)"
        disp_id = self.headers.get("X-OpenClaw-Dispatch-Id") or "?"
        ts = self.headers.get("X-OpenClaw-Timestamp") or "?"
        sig = self.headers.get("X-OpenClaw-Signature") or ""

        p(f"\n=== {self._stamp()} {self.command} {self.path} ===")
        p(f"{DOT} event           : {event}")
        p(f"{DOT} dispatch_id     : {disp_id}")
        p(f"{DOT} sent_at         : {ts}")
        p(f"{DOT} signature hdr   : {sig or '(missing)'}")

        # 验签
        if WebhookHandler.verify:
            if not sig:
                p(f"{NO} 验签失败: 缺少 X-OpenClaw-Signature header")
            else:
                ok = _verify_signature(WebhookHandler.secret, body, sig)
                if ok:
                    p(f"{OK} 验签通过 (HMAC-SHA256 匹配)")
                else:
                    expected = hmac.new(
                        WebhookHandler.secret.encode("utf-8"),
                        body, hashlib.sha256).hexdigest()
                    p(f"{NO} 验签失败! expected sha256={expected}")
        else:
            p(f"{DOT} 验签            : 已禁用 (--no-verify)")

        # payload pretty-print
        try:
            data = json.loads(body.decode("utf-8") or "{}")
            p(f"{DOT} payload         :")
            p(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False))
        except Exception as e:
            p(f"{NO} payload 解析失败 {e}: raw={body[:200]!r}")

        # 响应
        self.send_response(WebhookHandler.respond_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(WebhookHandler.respond_body)))
        self.end_headers()
        self.wfile.write(WebhookHandler.respond_body)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Webhook signature verifier + payload inspector.")
    ap.add_argument("--host", default="127.0.0.1",
                     help="监听地址 (默认 127.0.0.1, 仅本机)")
    ap.add_argument("--port", type=int, default=9876,
                     help="监听端口 (默认 9876)")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--secret", default="",
                      help="HMAC secret 明文 (和 dispatcher 一致; 仅开发用)")
    grp.add_argument("--secret-env", default="",
                      help="HMAC secret 环境变量名 (推荐, 和生产一致)")
    ap.add_argument("--no-verify", action="store_true",
                     help="跳过验签, 只打印 payload")
    ap.add_argument("--respond", type=int, default=200,
                     help="返回状态码 (默认 200; 用 500 触发 dispatcher 重试)")
    ap.add_argument("--respond-body", default='{"ok":true}',
                     help="返回 body JSON (默认 {\"ok\":true})")
    args = ap.parse_args()

    if not args.no_verify:
        if args.secret:
            secret = args.secret
            src = "--secret (plain)"
        elif args.secret_env:
            secret = os.environ.get(args.secret_env, "")
            src = f"env:{args.secret_env}"
            if not secret:
                print(f"{NO} 环境变量 {args.secret_env} 为空, "
                        f"请 export 或改用 --no-verify", file=sys.stderr)
                return 2
        else:
            print(f"{NO} 需要 --secret / --secret-env / --no-verify 任一, "
                    f"否则无法验签", file=sys.stderr)
            return 2
    else:
        secret = ""
        src = "(disabled)"

    WebhookHandler.secret = secret
    WebhookHandler.verify = not args.no_verify
    WebhookHandler.respond_status = int(args.respond)
    WebhookHandler.respond_body = args.respond_body.encode("utf-8")

    print(f"=== webhook_tester 启动 ===")
    print(f"{DOT} bind          : http://{args.host}:{args.port}/")
    print(f"{DOT} secret source : {src}")
    print(f"{DOT} verify mode   : {'on' if WebhookHandler.verify else 'off'}")
    print(f"{DOT} respond       : HTTP {WebhookHandler.respond_status}")
    print(f"{DOT} 把上方 URL 填到 webhook_targets.yaml 或 receiver.webhook_url,"
            f" 按 Ctrl+C 退出.")

    server = ThreadingHTTPServer((args.host, args.port), WebhookHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n=== 收到 Ctrl+C, 共接收 {WebhookHandler.counter} 条 webhook, 退出 ===")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
