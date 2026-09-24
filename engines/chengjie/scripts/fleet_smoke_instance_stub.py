"""Minimal local instance stub for fleet stage-2/3 smoke (127.0.0.1 only).

Provides:
  GET /api/ping
  GET /api/accounts/fleet-health
  GET /api/player-care/overview
Numeric summary JSON only — no chat text.
Auth: Authorization: Bearer <token> (optional if STUB_TOKEN empty).
"""
from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


TOKEN = os.environ.get("STUB_TOKEN", "").strip()
PORT = int(os.environ.get("STUB_PORT", "18790"))

# Align with domains/player_care/profile.STAGES + overview.build_overview shape.
STAGES = [
    "new_friend", "chatting", "mentioned_game", "registered",
    "depositing", "active", "dormant",
]


def _ok_auth(handler: BaseHTTPRequestHandler) -> bool:
    if not TOKEN:
        return True
    auth = handler.headers.get("Authorization") or ""
    return auth == f"Bearer {TOKEN}"


def _overview_body() -> dict:
    """Numeric player-care overview — mirrors build_overview; no chat text."""
    ts = int(time.time())
    day = time.strftime("%Y-%m-%d", time.localtime(ts))
    return {
        "ok": True,
        "ts": ts,
        "day": day,
        "contacts": {
            "total": 42,
            "with_phone": 28,
            "handoff": 3,
            "active_7d": 11,
            "by_account": {"acc_smoke_1": 25, "acc_smoke_2": 17},
        },
        "stages": {
            "order": list(STAGES),
            "totals": {
                "new_friend": 8,
                "chatting": 12,
                "mentioned_game": 5,
                "registered": 7,
                "depositing": 2,
                "active": 6,
                "dormant": 2,
            },
            "by_account": {
                "acc_smoke_1": {
                    "new_friend": 5, "chatting": 7, "mentioned_game": 3,
                    "registered": 4, "depositing": 1, "active": 4, "dormant": 1,
                },
                "acc_smoke_2": {
                    "new_friend": 3, "chatting": 5, "mentioned_game": 2,
                    "registered": 3, "depositing": 1, "active": 2, "dormant": 1,
                },
            },
        },
        "today": {
            "inbound": 14,
            "lookups": 9,
            "found": 4,
            "visible": 3,
            "gate_hits": 2,
            "new_profiles": 1,
            "stage_ups": 2,
            "accounts": [
                {"account": "acc_smoke_1", "inbound": 9, "visible": 2, "gate_hits": 1},
                {"account": "acc_smoke_2", "inbound": 5, "visible": 1, "gate_hits": 1},
            ],
        },
        "gateway": {
            "status": "ok",
            "enabled": True,
            "url": "http://127.0.0.1:19999/gw",
            "key_set": True,
            "key_env": "GATEWAY_KEY",
            "timeout_sec": 5,
            "lookups_total": 120,
            "last_lookup_at": ts - 90,
            "recent_24h": {
                "total": 9,
                "found": 4,
                "not_found": 4,
                "errors": {"timeout": 1},
            },
        },
        "sync": {
            "enabled": True,
            "interval_min": 15,
            "last": {"ts": ts - 300, "ok": True, "upserted": 2},
        },
        "commandbus": {
            "enabled": True,
            "stats": {"queued": 0, "pulled": 1, "acked": 1, "failed": 0},
        },
        "profile_enabled": True,
        "notes": [],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter
        sys_stderr = __import__("sys").stderr
        sys_stderr.write("[stub] " + (fmt % args) + "\n")

    def _json(self, code: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if not _ok_auth(self):
            return self._json(401, {"ok": False, "error": "unauthorized"})
        if path == "/api/ping":
            return self._json(200, {
                "ok": True,
                "service": "fleet-smoke-stub",
                "role": "instance",
                "version": "smoke-1",
                "uptime_sec": 1,
            })
        if path == "/api/accounts/fleet-health":
            return self._json(200, {
                "ok": True,
                "total": 2,
                "lifecycle": {
                    "active": 1,
                    "warming": 1,
                    "pending": 0,
                    "restricted": 0,
                    "banned": 0,
                    "offline": 0,
                },
                "fleet": {
                    "state": "green",
                    "by_state": {"green": 1, "yellow": 1, "red": 0},
                    "counts": {"ok": 1, "warn": 1, "bad": 0},
                },
                "accounts": [
                    {
                        "platform": "whatsapp",
                        "account_id": "acc_smoke_1",
                        "stage": "active",
                        "quota": {"used": 3, "cap": 40, "auto_cap": 20, "reserve": 5, "auto_blocked": False},
                        "profile_churn_7d": 0,
                    },
                    {
                        "platform": "telegram",
                        "account_id": "acc_smoke_2",
                        "stage": "warming",
                        "quota": {"used": 1, "cap": 15, "auto_cap": 8, "reserve": 3, "auto_blocked": False},
                        "profile_churn_7d": 1,
                    },
                ],
                "profile_churn_hot": [],
            })
        if path == "/api/player-care/overview":
            return self._json(200, _overview_body())
        return self._json(404, {"ok": False, "error": "not_found", "path": path})


def main() -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[stub] listening on http://127.0.0.1:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
