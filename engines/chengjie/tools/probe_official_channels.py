# -*- coding: utf-8 -*-
"""官方渠道（LINE / Messenger / Instagram / Zalo / WA Cloud）接入验收探针。

**运维显式执行，非 pytest**。默认只读：核对凭证齐全度、账号注册表 official 行、
诚实媒体能力位、公网媒体 URL 是否已填。不发任何消息。

与 ``tools/probe_line_media.py`` / ``probe_messenger_parity.py`` 互补——那两份专打
单端真链路；本工具是「向导填完 → 还缺什么」的一张体检单。

用法::

    python tools/probe_official_channels.py              # 全平台只读
    python tools/probe_official_channels.py --platform instagram
    python tools/probe_official_channels.py --json
    python tools/probe_official_channels.py --probe-media # 顺带对本机探测 public_base_url
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402

# 渠道声明里的「官方」平台 → config 块键（与 official_enabled 回落同口径）
_CFG_KEY = {
    "line": "line",
    "messenger": "facebook_messenger",
    "whatsapp": "whatsapp_cloud",
    "instagram": "instagram",
    "zalo": "zalo",
}

# 判定「凭证齐」的最小必填键（与 channel_setup Field.required 对齐；非穷尽）
_REQUIRED = {
    "line": ("channel_access_token", "channel_secret"),
    "messenger": ("page_access_token", "verify_token"),
    "whatsapp": ("access_token", "phone_number_id", "app_secret", "verify_token"),
    "instagram": ("ig_id", "page_access_token", "app_secret", "verify_token"),
    "zalo": ("access_token",),
}


def _registry_official(root: Path, platform: str) -> List[Dict[str, str]]:
    db = root / "config" / "account_registry.db"
    if not db.is_file():
        return []
    out: List[Dict[str, str]] = []
    try:
        uri = "file:%s?mode=ro" % str(db).replace("\\", "/")
        con = sqlite3.connect(uri, uri=True)
        try:
            rows = con.execute(
                "SELECT account_id, mode, status FROM platform_accounts"
                " WHERE platform=? AND COALESCE(mode,'')='official'",
                (platform,),
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        return [{"account_id": "?", "mode": "official", "status": "err:%s" % exc}]
    for account_id, mode, status in rows:
        out.append({
            "account_id": str(account_id or ""),
            "mode": str(mode or ""),
            "status": str(status or ""),
        })
    return out


def _probe_https(url: str) -> Dict[str, str]:
    u = str(url or "").strip().rstrip("/")
    if not u:
        return {"state": "unset", "detail": ""}
    if not u.lower().startswith("https://"):
        return {"state": "fail", "detail": "must_be_https"}
    try:
        req = Request(u + "/login", method="GET")
        with urlopen(req, timeout=4) as resp:  # noqa: S310 — 运维自检目标 URL
            code = getattr(resp, "status", None) or resp.getcode()
            return {"state": "ok" if int(code) < 500 else "fail",
                    "detail": "http_%s" % code}
    except URLError as ex:
        return {"state": "fail", "detail": type(ex).__name__}
    except Exception as ex:  # noqa: BLE001
        return {"state": "fail", "detail": type(ex).__name__}


def _check_platform(
    platform: str, config: Dict[str, Any], root: Path, *, probe_media: bool,
) -> Dict[str, Any]:
    from src.integrations.official_api_worker import (
        OFFICIAL_MEDIA_URL_PLATFORMS,
        official_enabled,
        official_send_caps,
    )

    cfg_key = _CFG_KEY.get(platform, platform)
    block = (config or {}).get(cfg_key) or {}
    required = _REQUIRED.get(platform) or ()
    missing = [k for k in required if not str(block.get(k) or "").strip()]
    caps = official_send_caps(platform, config)
    accounts = _registry_official(root, platform)
    orch = bool(((config or {}).get("platform_login") or {}).get("orchestrator_enabled"))
    row: Dict[str, Any] = {
        "platform": platform,
        "enabled": official_enabled(config, platform),
        "credentials_ok": not missing,
        "missing_fields": missing,
        "orchestrator_enabled": orch,
        "official_accounts": accounts,
        "can_media": bool(caps.get("can_media")),
        "can_voice": bool(caps.get("can_voice")),
        "caps_reason": str(caps.get("reason") or ""),
        "verdict": "ok",
        "hints": [],
    }
    hints: List[str] = row["hints"]
    if missing:
        hints.append("fill credentials in /workspace/setup -> %s" % platform)
    if not accounts:
        hints.append("no mode=official account in registry (re-save wizard to provision)")
    if not orch:
        hints.append("platform_login.orchestrator_enabled is false (workers won't start)")
    if platform in OFFICIAL_MEDIA_URL_PLATFORMS and caps.get("reason") == "needs_public_url":
        hints.append("set official_media.public_base_url (shared IG/LINE field in wizard)")
    if platform == "zalo":
        hints.append("Zalo OA API is text-only by design (media buttons stay disabled)")

    if probe_media and platform in OFFICIAL_MEDIA_URL_PLATFORMS:
        base = str(((config or {}).get("official_media") or {}).get("public_base_url") or "")
        row["media_probe"] = _probe_https(base)

    if missing or not accounts or not orch:
        row["verdict"] = "not_ready"
    elif hints and platform != "zalo":
        # zalo 的「只能文字」是设计态，不算 not_ready
        if any("public_base_url" in h for h in hints):
            row["verdict"] = "text_only"
    return row


def run(platforms: Optional[List[str]], *, as_json: bool, probe_media: bool) -> int:
    from src.integrations.official_api_worker import OFFICIAL_PLATFORMS

    roots = resolve_data_roots()
    if not roots:
        print("no data root found", file=sys.stderr)
        return 2
    target = [p.lower() for p in (platforms or list(OFFICIAL_PLATFORMS))]
    report: List[Dict[str, Any]] = []
    for root in roots:
        cfg = load_merged_config(root) or {}
        block = {
            "data_root": str(root),
            "platforms": [_check_platform(p, cfg, root, probe_media=probe_media)
                          for p in target if p in OFFICIAL_PLATFORMS],
        }
        report.append(block)

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    for block in report:
        print("=== data_root: %s ===" % block["data_root"])
        for row in block["platforms"]:
            mark = {"ok": "OK", "text_only": "TEXT", "not_ready": "!!"}.get(
                row["verdict"], row["verdict"])
            print("  [%s] %s  enabled=%s creds=%s orch=%s accounts=%d media=%s%s" % (
                mark, row["platform"], row["enabled"], row["credentials_ok"],
                row["orchestrator_enabled"], len(row["official_accounts"]),
                "yes" if row["can_media"] else ("no(%s)" % (row["caps_reason"] or "-")),
                "" if not row.get("media_probe") else
                " probe=%s" % row["media_probe"].get("state"),
            ))
            for h in row["hints"]:
                print("       - %s" % h)
        print()
    worst = "ok"
    for block in report:
        for row in block["platforms"]:
            if row["verdict"] == "not_ready":
                worst = "not_ready"
            elif row["verdict"] == "text_only" and worst == "ok":
                worst = "text_only"
    return 1 if worst == "not_ready" else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--platform", action="append", dest="platforms",
                    help="limit to platform (repeatable)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--probe-media", action="store_true",
                    help="GET public_base_url/login from this host (hint only)")
    args = ap.parse_args(argv)
    return run(args.platforms, as_json=args.json, probe_media=args.probe_media)


if __name__ == "__main__":
    raise SystemExit(main())
