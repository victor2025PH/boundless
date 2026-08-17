#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Messenger「为什么收发不了 / 全自动为什么不动」一键只读诊断（P1 2026-08-13）。

与 ``tools/probe_messenger_parity.py`` 分工：parity 管**入站镜像质量**（对等性），
本工具管**链路就绪度**——按「人工收发 / 全自动」双道给出第一阻塞原因与下一步
动作。判定逻辑单源在 ``src/integrations/messenger_readiness.py``（纯函数），
本文件只做采集与呈现；未来 ops 卡 / composer 提示复用同一判定即零口径分裂。

数据源（全部只读，服务宕着也能出报告）：
1. messenger-web sidecar ``/health`` + ``/accounts``（活体真相）
2. 账号注册表 ``config/account_registry.db``（mode=ro）
3. 实例合并配置（config.yaml + config.local.yaml，data-root 契约解析——
   磁盘配置=「重启后为真」，报告里显式标注）
4. 可选：后台 ``/api/workspace/metrics.platform_sessions``（主管会话门槛，
   纯 token 403 时优雅降级并如实标注，不猜）

用法::

    python tools/diagnose_messenger.py            # 人读报告
    python tools/diagnose_messenger.py --json     # 机器可读（接巡检）
    python tools/diagnose_messenger.py --data-root D:/chengjie-instances/zhiliao/data

退出码：0=全绿 / 1=有账号被拦（人工或自动）/ 2=sidecar 不可达或无账号。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ENGINE_ROOT))

from src.integrations.messenger_readiness import (  # noqa: E402
    ACTION_HINTS,
    evaluate_messenger_readiness,
    extract_config_gates,
    warn_hint,
)

try:  # Windows 控制台 GBK 防乱码（best-effort）
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


def _load_yaml(path: Path) -> dict:
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _resolve_data_root(cli: str = "") -> Path:
    if cli:
        return Path(cli)
    try:
        from scripts._data_root import resolve_data_roots
        roots = resolve_data_roots()
        if roots:
            return Path(roots[0])
    except Exception:
        pass
    return _ENGINE_ROOT


def _merged_config(root: Path) -> dict:
    base = _load_yaml(root / "config" / "config.yaml")
    over = _load_yaml(root / "config" / "config.local.yaml")
    return _deep_merge(base, over)


def _registry_accounts(root: Path) -> List[Dict[str, Any]]:
    """注册表 messenger 行（mode=ro；列名防漂移取交集）。"""
    db = root / "config" / "account_registry.db"
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM platform_accounts WHERE platform='messenger'"
        ).fetchall()
        con.close()
        out = []
        for r in rows:
            d = dict(r)
            out.append({
                "account_id": str(d.get("account_id") or ""),
                "status": str(d.get("status") or ""),
                "label": str(d.get("label") or d.get("display_name") or ""),
            })
        return out
    except Exception:
        return []


def _get_json(url: str, *, token: str = "", timeout: float = 8.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _probe_sidecar(node_url: str) -> Dict[str, Any]:
    base = node_url.rstrip("/")
    out: Dict[str, Any] = {"url": base, "reachable": False, "accounts": []}
    try:
        h = _get_json(f"{base}/health", timeout=5.0)
        out["reachable"] = bool(h.get("ok"))
    except Exception as exc:
        out["error"] = str(exc)
        return out
    try:
        data = _get_json(f"{base}/accounts", timeout=8.0)
        out["accounts"] = list(data.get("accounts") or [])
    except Exception as exc:
        out["error"] = f"/accounts: {exc}"
    return out


def _probe_session_registry(base_url: str, token: str) -> Optional[Dict[str, Any]]:
    """后台登记表（主管门槛端点）：403/网络失败 → None（不可用，不猜）。"""
    if not token:
        return None
    try:
        data = _get_json(
            f"{base_url.rstrip('/')}/api/workspace/metrics", token=token,
            timeout=10.0)
        ps = (data or {}).get("platform_sessions") or {}
        return {
            "sessions": ps.get("sessions") or {},
            "inbox_health": ps.get("inbox_health") or {},
        }
    except Exception:
        return None


def _fmt_age(ts: Optional[float], now: float) -> str:
    try:
        t = float(ts or 0)
    except (TypeError, ValueError):
        return "-"
    if t <= 0:
        return "-"
    sec = max(0.0, now - t)
    if sec < 90:
        return f"{sec:.0f}s前"
    if sec < 5400:
        return f"{sec / 60:.0f}min前"
    return f"{sec / 3600:.1f}h前"


def _render(verdict: Dict[str, Any]) -> str:
    now = float(verdict.get("generated_at") or time.time())
    gates = verdict.get("gates") or {}
    src = verdict.get("sources") or {}
    lines: List[str] = []
    lines.append("=== Messenger 收发 / 全自动 就绪度诊断 ===")
    lines.append(
        f"sidecar(messenger-web): "
        f"{'OK' if src.get('sidecar_reachable') else '不可达'}"
        f"  配置闸门: web接入(有效)={gates.get('web_effective')}"
        f" l2_autosend={gates.get('l2_enabled')}"
        f" deliver={gates.get('deliver')}"
        f" platform_mode={gates.get('platform_mode') or '(未封顶)'}"
        f"  [磁盘配置=重启后为真]")
    if not src.get("session_registry_available"):
        lines.append("后台会话登记表: 不可用（需主管会话；已按 sidecar 活体 + 配置判定）")
    lines.append("")

    for a in verdict.get("accounts") or []:
        sig = a.get("signals") or {}
        label = f" ({a['label']})" if a.get("label") else ""
        lines.append(f"账号 {a['account_id']}{label}"
                     f"  [注册表:{'有' if a.get('in_registry') else '无'}"
                     f" sidecar:{'有' if a.get('in_sidecar') else '无'}]")
        m, au = a["manual"], a["auto"]
        if m["ok"]:
            lines.append("  人工收发: OK")
        else:
            lines.append(f"  人工收发: 拦 [{m['first_block']}]"
                         f" -> {ACTION_HINTS.get(m['first_block'], '')}")
        if au["ok"]:
            lines.append("  全自动:   链路就绪（会话须为全自动档才会自动回）")
        else:
            hint = ACTION_HINTS.get(au["first_block"], "")
            same = (not m["ok"]) and au["first_block"] == m["first_block"]
            lines.append("  全自动:   同上（先修人工道）" if same
                         else f"  全自动:   拦 [{au['first_block']}] -> {hint}")
        if a.get("in_sidecar"):
            ratio = sig.get("e2ee_ratio")
            try:
                ratio_s = f"{float(ratio) * 100:.0f}%" if ratio is not None and float(ratio) >= 0 else "-"
            except (TypeError, ValueError):
                ratio_s = "-"
            lines.append(
                f"  实况: logged_in={sig.get('logged_in')}"
                f" 读取={sig.get('read_fails')}/{sig.get('read_attempts')}败"
                f" 未读={sig.get('inbox_unread')}"
                f" e2ee占位={ratio_s}(PIN已托管={sig.get('e2ee_pin_set')})"
                f" 末次入站={_fmt_age(sig.get('last_inbound_ts'), now)}"
                f" 轮询={_fmt_age(sig.get('last_poll_ok_ts'), now)}")
        for w in a.get("warnings") or []:
            lines.append(f"  警示: {warn_hint(w)}")
        lines.append("")

    if not verdict.get("accounts"):
        lines.append("（注册表与 sidecar 均无 Messenger 账号——先在统一收件箱添加/登录）")
    lines.append(f"总判定: {verdict.get('overall')}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Messenger 就绪度只读诊断")
    ap.add_argument("--data-root", default="", help="实例数据根（缺省自动发现）")
    ap.add_argument("--base", default="http://127.0.0.1:18799", help="后台 base URL")
    ap.add_argument("--node", default="", help="messenger-web URL（缺省读配置）")
    ap.add_argument("--token", default="", help="后台 API token（缺省读配置）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()

    root = _resolve_data_root(args.data_root)
    cfg = _merged_config(root)
    gates = extract_config_gates(cfg)
    node_url = args.node or gates.get("web_url") or "http://127.0.0.1:8791"
    token = args.token or str(((cfg.get("web_admin") or {}).get("auth_token")) or "")

    sidecar = _probe_sidecar(node_url)
    session_registry = _probe_session_registry(args.base, token)
    registry = _registry_accounts(root)

    verdict = evaluate_messenger_readiness(
        registry_accounts=registry,
        sidecar=sidecar,
        session_registry=session_registry,
        config_gates=gates,
    )
    verdict["data_root"] = str(root)

    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
    else:
        print(_render(verdict))

    overall = verdict.get("overall")
    if overall in ("sidecar_down", "no_accounts"):
        return 2
    if overall in ("manual_blocked", "auto_blocked"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
