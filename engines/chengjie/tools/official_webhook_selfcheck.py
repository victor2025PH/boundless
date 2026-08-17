# -*- coding: utf-8 -*-
"""官方渠道 webhook 回调可达性——进程外文件口径自检 CLI（2026-08-07）。

与 ``GET /api/admin/official-webhook-status`` 同一判词逻辑（``official_webhook_stats.
classify`` 单一事实源），差异只在真相来源：路由多出**进程口径**（app.state 挂载真相、
内存台账）；本工具在进程外读**文件真相**（实例数据根的 config + 台账 JSON），
服务挂了/重启窗口内也能诊断——与 ``tools/alert_link_selfcheck.py`` 同一分工哲学。

数据根解析走 ``scripts/_data_root`` 契约（CLI --data-root → AITR_DATA_ROOT →
自动发现活跃实例 → 引擎根），**绝不**按 CWD 猜（protocol_doctor 的引擎根 CWD
读旧配置正是规则里记载的静默失真病，本工具刻意不重蹈）。

用法::

    python tools/official_webhook_selfcheck.py            # 逐实例根诊断
    python tools/official_webhook_selfcheck.py --json     # 机器可读
    python tools/official_webhook_selfcheck.py --data-root D:/chengjie-instances/zhiliao/data

退出码：0 = 无需关注（渠道全 live/握手中，或压根没启用官方渠道）；
1 = 有已启用渠道处于 never_reached / auth_failing / not_mounted（接入未完成或断了）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402
from src.integrations.official_webhook_stats import (  # noqa: E402
    HAS_GET_VERIFY, PLATFORM_CONFIG_BLOCKS, classify, creds_ok, inferred_mounted,
)

_ATTENTION = {"never_reached", "auth_failing", "not_mounted"}

_HINTS = {
    "never_reached": "公网回调 URL/隧道/开发者后台订阅未配好——对面从没打进来过",
    "auth_failing": "有请求到达但从未通过验证：核对 verify_token / app_secret（或为扫描噪声）",
    "not_mounted": "凭证缺失或启动后才填：webhook 路由等下次实例重启装载",
    "handshake_only": "握手已通；零事件多为冷启动，持续为零查开发者后台订阅字段",
    "live": "",
    "disabled": "",
}


def _age(ts: float, now: float) -> str:
    if not ts:
        return "never"
    s = int(now - ts)
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def check_root(root: Path) -> dict:
    """单个数据根的文件口径诊断（纯读，绝不写）。"""
    cfg = load_merged_config(root)
    ledger_path = Path(root) / "config" / "official_webhook_state.json"
    ledger: dict = {}
    try:
        if ledger_path.is_file():
            raw = json.loads(ledger_path.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                ledger = raw
    except Exception:  # noqa: BLE001
        pass  # 坏台账按空处理（诊断工具软失败）
    now = time.time()
    platforms = []
    for platform, block_key in PLATFORM_CONFIG_BLOCKS.items():
        block = (cfg.get(block_key) or {}) if isinstance(cfg, dict) else {}
        enabled = bool(block.get("enabled"))
        credok = creds_ok(platform, cfg if isinstance(cfg, dict) else {})
        row = ledger.get(platform) or {}
        # 文件口径没有 app.state：挂载真相退化为配置推断（单一事实源
        # inferred_mounted——热挂载平台 enabled 即挂，勿在此复制表达式）
        verdict = classify(enabled=enabled, creds=credok,
                           mounted=inferred_mounted(
                               platform, enabled=enabled, creds=credok),
                           has_verify=HAS_GET_VERIFY[platform], row=row)
        platforms.append({
            "platform": platform,
            "enabled": enabled,
            "creds_ok": credok,
            "verdict": verdict,
            "events_total": int(row.get("events_total") or 0),
            "last_event": _age(float(row.get("last_event_ts") or 0), now),
            "last_verify": _age(float(row.get("last_verify_ts") or 0), now),
            "errors": int(row.get("error_total") or 0),
            "verify_fails": int(row.get("verify_fail_total") or 0),
            "hint": _HINTS.get(verdict, ""),
        })
    attention = [p for p in platforms
                 if p["enabled"] and p["verdict"] in _ATTENTION]
    return {
        "root": str(root),
        "ledger": str(ledger_path) if ledger_path.is_file() else "",
        "mount_truth": "config-inferred (file scope; process truth lives in "
                       "/api/admin/official-webhook-status)",
        "platforms": platforms,
        "attention": [p["platform"] for p in attention],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="官方渠道 webhook 回调可达性自检（文件口径）")
    ap.add_argument("--data-root", default="", help="实例数据根（缺省自动发现）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    reports = [check_root(r) for r in resolve_data_roots(args.data_root)]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for rep in reports:
            print(f"\n=== {rep['root']} ===")
            print(f"  台账: {rep['ledger'] or '（尚无——从未有回调到达或服务未跑过）'}")
            for p in rep["platforms"]:
                mark = ("!" if (p["enabled"] and p["verdict"] in _ATTENTION)
                        else ("+" if p["verdict"] == "live" else " "))
                print(f"  [{mark}] {p['platform']:<10} {p['verdict']:<15} "
                      f"events={p['events_total']} last_event={p['last_event']} "
                      f"verify={p['last_verify']} errors={p['errors']}"
                      + (f"  <- {p['hint']}" if (p["enabled"] and p["hint"]) else ""))
            if not any(p["enabled"] for p in rep["platforms"]):
                print("  （没有启用任何官方 webhook 渠道——接入后本工具才有活干）")
    return 1 if any(rep["attention"] for rep in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
