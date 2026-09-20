# -*- coding: utf-8 -*-
"""双向全自动「对级验收」报告 CLI（P1 2026-08-07，.104/.198 验收工具）。

一条命令回答：**这对账号的双向全自动到底跑起来没有、质量如何**——
两个方向各自的应答率 / 回复延迟 p50·p95 / 往返轮次 / 复读·秒回风险
（与 peer_bot_guard 同一把尺子），加上两侧当前档位与封顶，最后给验收判词。

与工具邻居的分工：
- ``tools/why_no_reply.py``＝单会话负向排障（「为什么没回」，逐层闸门）；
- 本工具＝账号对正向验收（「跑起来没有」，消息动力学 + 判词指路）。

**零凭据、只读**（sqlite mode=ro；不 import InboxStore；注册表字段 ro 自取
显式注入 effective_automation）。数据根走 ``scripts/_data_root`` 契约。

用法::

    python tools/duplex_report.py --a 8041810715 --b 8755679833
    python tools/duplex_report.py --a ... --b ... --hours 6 --json
    python tools/duplex_report.py ... --data-root D:\\chengjie-instances\\zhiliao\\data

退出码：任一 block（单边哑火 / 档位·封顶拦截）→ 1；否则 0（可挂验收脚本）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402
from src.inbox.duplex_report import (  # noqa: E402
    build_side_stats,
    duplex_verdict,
    side_summary,
)


def _ro(path: Path) -> Optional[sqlite3.Connection]:
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con
    except Exception:
        return None


def _row(con, sql, args=()) -> Optional[Dict[str, Any]]:
    try:
        r = con.execute(sql, args).fetchone()
        return dict(r) if r is not None else None
    except Exception:
        return None


def _rows(con, sql, args=()) -> List[Dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    except Exception:
        return []


class _RoModeStore:
    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con

    def get_automation_mode_if_set(self, conversation_id: str):
        r = _row(self._con,
                 "SELECT automation_mode FROM conversation_settings "
                 "WHERE conversation_id=?", (conversation_id,))
        return r["automation_mode"] if r else None


def _mode_meta(con, cid: str) -> Dict[str, Any]:
    """显式档位 + 来源（source 列由 governance 线新增；旧库缺列时优雅降级）。"""
    r = _row(con, "SELECT automation_mode, source, updated_at FROM "
                  "conversation_settings WHERE conversation_id=?", (cid,))
    if r is None:
        r = _row(con, "SELECT automation_mode, updated_at FROM "
                      "conversation_settings WHERE conversation_id=?", (cid,))
    return r or {}


def _side(root: Path, cfg: Dict[str, Any], ib, reg, platform: str,
          account_id: str, peer: str, since_ts: float,
          now: float) -> Dict[str, Any]:
    cid = f"{platform}:{account_id}:{peer}"
    msgs = _rows(
        ib, "SELECT direction, ts, COALESCE(text,'') AS text FROM messages "
            "WHERE conversation_id=? AND ts>=? ORDER BY ts ASC",
        (cid, since_ts)) if ib is not None else []
    stats = build_side_stats(msgs, now=now)
    meta = _mode_meta(ib, cid) if ib is not None else {}

    created_at, business_line = 0.0, ""
    if reg is not None:
        ar = _row(reg, "SELECT created_at, business_line, meta_json FROM "
                       "platform_accounts WHERE platform=? AND account_id=?",
                  (platform, account_id))
        if ar:
            created_at = float(ar.get("created_at") or 0.0)
            business_line = str(ar.get("business_line") or "")
            if not business_line:
                try:
                    business_line = str(json.loads(
                        ar.get("meta_json") or "{}").get("business_line") or "")
                except Exception:
                    business_line = ""
    eff: Dict[str, Any] = {}
    try:
        from src.inbox.effective_automation import effective_automation
        eff = effective_automation(
            _RoModeStore(ib) if ib is not None else None, cfg,
            conversation_id=cid, platform=platform, account_id=account_id,
            business_line=business_line or None, connected_at=created_at)
    except Exception as exc:  # pragma: no cover - 验收工具自身别崩
        eff = {"error": str(exc)}
    return {
        "conversation_id": cid,
        "account_id": account_id,
        "stats": stats,
        "summary": side_summary(stats),
        "mode": str(eff.get("mode") or ""),
        "mode_source": str(meta.get("source") or ""),
        "effective_mode": str(eff.get("effective_mode") or ""),
        "caps": eff.get("caps") or [],
        "messages_in_window": len(msgs),
    }


def report_pair(root: Path, platform: str, a: str, b: str,
                hours: float, min_reply_rate: float) -> Dict[str, Any]:
    now = time.time()
    cfg = load_merged_config(root) or {}
    cfg_dir = root / "config"
    ib = _ro(cfg_dir / "inbox.db")
    reg = _ro(cfg_dir / "account_registry.db")
    since = now - hours * 3600.0
    sa = _side(root, cfg, ib, reg, platform, a, b, since, now)
    sb = _side(root, cfg, ib, reg, platform, b, a, since, now)
    verdicts = duplex_verdict(
        sa["stats"], sb["stats"], a_label=a, b_label=b,
        min_reply_rate=min_reply_rate)
    # 闸门类判词（原因细节归 why_no_reply；这里只点名+指路）
    for s in (sa, sb):
        acct = s["account_id"]
        if s["mode"] and s["mode"] != "auto_ai":
            src = f"（来源 {s['mode_source']}）" if s.get("mode_source") else ""
            verdicts.append({
                "level": "block", "code": f"{acct}_mode",
                "msg": f"{acct} 侧档位={s['mode']}{src}——切回全自动才会自动发"})
        for cap in s["caps"]:
            verdicts.append({
                "level": "block", "code": f"{acct}_cap_{cap.get('layer')}",
                "msg": f"{acct} 侧被 {cap.get('layer')} 封顶为 "
                       f"{cap.get('ceiling')}（{cap.get('detail')}）——详情跑 "
                       f"why_no_reply --account {acct}"})
    # drill 提示（governance 线的演练白名单；未启用/模块缺失静默跳过）
    try:
        from src.inbox.duplex_drill import is_drill_pair, parse_cfg
        d_cfg = parse_cfg(cfg)
        if is_drill_pair(d_cfg, a, b):
            verdicts.append({
                "level": "ok", "code": "drill_pair",
                "msg": f"本对在演练白名单（duplex_drill，日硬顶 "
                       f"{d_cfg.get('max_rounds')} 轮）——守卫/封顶按配置豁免"})
    except Exception:
        pass
    for con in (ib, reg):
        if con is not None:
            con.close()
    return {
        "root": str(root), "platform": platform, "a": a, "b": b,
        "window_hours": hours,
        "sides": {a: {k: v for k, v in sa.items() if k != "stats"},
                  b: {k: v for k, v in sb.items() if k != "stats"}},
        "verdicts": verdicts,
    }


def _print_human(d: Dict[str, Any]) -> None:
    icon = {"ok": "✅", "warn": "⚠️ ", "block": "⛔"}
    print("=" * 72)
    print(f"root   : {d['root']}   window={d['window_hours']}h")
    for acct, s in (d.get("sides") or {}).items():
        m = s.get("summary") or {}
        cap_txt = ",".join(c.get("layer", "?") for c in s.get("caps") or []) or "-"
        print(f"  {acct}: mode={s.get('mode')}→{s.get('effective_mode')}"
              f" caps={cap_txt} src={s.get('mode_source') or '-'}")
        print(f"      in={m.get('inbound')} out={m.get('outbound')} "
              f"answer={m.get('answered')}/{m.get('in_bursts')}"
              f"({m.get('answer_rate', 0):.0%}) "
              f"p50={m.get('latency_p50_s')}s p95={m.get('latency_p95_s')}s "
              f"repeat_max={m.get('out_repeat_max')} "
              f"fast={m.get('fast_replies')}")
    print("-" * 72)
    for v in d.get("verdicts") or []:
        print(f" {icon.get(v['level'], '  ')} [{v['code']}] {v['msg']}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="duplex_report：双向全自动对级验收")
    ap.add_argument("--platform", default="telegram")
    ap.add_argument("--a", required=True, help="账号 A（account_id）")
    ap.add_argument("--b", required=True, help="账号 B（account_id）")
    ap.add_argument("--hours", type=float, default=24.0, help="观察窗（小时）")
    ap.add_argument("--min-reply-rate", type=float, default=0.5,
                    dest="min_reply_rate")
    ap.add_argument("--data-root", default="", help="实例数据根（默认自动发现）")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    roots = resolve_data_roots(args.data_root)
    results = [report_pair(r, str(args.platform).lower(), str(args.a),
                           str(args.b), float(args.hours),
                           float(args.min_reply_rate)) for r in roots]
    if args.as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for d in results:
            _print_human(d)
    blocked = any(v.get("level") == "block"
                  for d in results for v in (d.get("verdicts") or []))
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
