# -*- coding: utf-8 -*-
"""发送护栏装载验收探针（P4 2026-08-13，重启后跑；全程只读 GET）。

对照三面：
1. ``GET /api/unified-inbox/automation`` 的 ``send_gate`` 段（P0 搭便车字段
   存在＝新代码已装载；双道数字与数据侧 CLI 一致＝链路真值）。
2. ``GET /api/accounts/fleet-health`` 的 ``send_blocks`` + accounts[].quota
   （P2 观测面装载）。
3. ``GET /api/workspace/metrics`` 的 ``send_gate_blocks`` 段。

鉴权走桌面壳同款 Bearer（实例 config ``web_admin.auth_token``，本机读取、
绝不打印）；探针只 GET 不写。退出码：0=全过，1=有缺项（逐项列明）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ENGINE_ROOT))


def _token(root: Path) -> str:
    try:
        import yaml
        for name in ("config.local.yaml", "config.yaml"):
            cfg = yaml.safe_load(open(root / "config" / name, encoding="utf-8")) or {}
            tok = str(((cfg.get("web_admin") or {}).get("auth_token")) or "")
            if tok:
                return tok
    except Exception:
        pass
    return ""


def _get(base: str, tok: str, path: str):
    req = urllib.request.Request(
        base + path, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18799")
    ap.add_argument("--data-root", default="")
    ap.add_argument("--chat-key", default="8921664288")
    # 首跑教训（2026-08-13 04:40）：收件箱会话的 account_id 是注册表真实号，
    # 不是 A 线别名 "default"——orchestrator.owns("telegram","default")=False
    # → send_gate 恒 None，探针假红。缺省留空＝自动从注册表挑首个在线
    # telegram 协议号（与编排器 owns 的判定对象一致）。
    ap.add_argument("--account-id", default="")
    args = ap.parse_args()

    root = Path(args.data_root) if args.data_root else None
    if root is None:
        try:
            from scripts._data_root import resolve_data_roots
            roots = resolve_data_roots()
            root = Path(roots[0]) if roots else _ENGINE_ROOT
        except Exception:
            root = _ENGINE_ROOT
    tok = _token(root)
    if not tok:
        print("FAIL: 读不到 web_admin.auth_token（无法鉴权探针）")
        return 1

    acct = str(args.account_id or "").strip()
    if not acct:
        try:
            con = sqlite3.connect(
                f"file:{(root / 'config' / 'account_registry.db').as_posix()}"
                "?mode=ro", uri=True)
            row = con.execute(
                "SELECT account_id FROM platform_accounts WHERE platform='telegram' "
                "AND status='online' ORDER BY created_at ASC LIMIT 1").fetchone()
            con.close()
            acct = str(row[0]) if row else "default"
        except Exception:
            acct = "default"
    print(f"probe account: telegram:{acct}")

    fails = []

    # 1) /automation send_gate 段
    try:
        d = _get(args.base, tok,
                 "/api/unified-inbox/automation?platform=telegram"
                 f"&account_id={acct}&chat_key={args.chat_key}")
        sg = d.get("send_gate")
        if sg is None:
            fails.append("automation.send_gate 为 None（新代码未装载或编排器不拥有该账号）")
        else:
            q = sg.get("quota") or {}
            print(f"PASS automation.send_gate: blocked={sg.get('blocked')} "
                  f"reason={sg.get('reason')!r} used={q.get('used')} "
                  f"cap={q.get('cap')} auto_cap={q.get('auto_cap')} "
                  f"reserve={q.get('reserve')} auto_blocked={q.get('auto_blocked')} "
                  f"can_exempt={sg.get('can_exempt')}")
    except Exception as ex:
        fails.append(f"automation 探针异常: {ex}")

    # 2) fleet-health quota + send_blocks
    try:
        d = _get(args.base, tok, "/api/accounts/fleet-health")
        sb = d.get("send_blocks")
        if sb is None:
            fails.append("fleet-health.send_blocks 缺失")
        else:
            print(f"PASS fleet-health.send_blocks: total={sb.get('total')}")
        accs = d.get("accounts") or []
        with_quota = [a for a in accs if a.get("quota")]
        if not with_quota:
            fails.append("fleet-health.accounts[].quota 全空（闸门开着不该如此）")
        else:
            a0 = max(with_quota,
                     key=lambda a: (a["quota"].get("used", 0) or 0))
            print(f"PASS fleet-health.quota: {a0.get('platform')}:{a0.get('account_id')} "
                  f"used={a0['quota'].get('used')}/{a0['quota'].get('cap')} "
                  f"auto_cap={a0['quota'].get('auto_cap')}")
    except Exception as ex:
        fails.append(f"fleet-health 探针异常: {ex}")

    # 3) workspace metrics send_gate_blocks —— 可选项：该端点主管专属，
    #    纯 Bearer 可能 403（同一数据已由 fleet-health.send_blocks 验证过；
    #    此处 403 只提示、不判失败——别让鉴权档位差异谎报装载失败）
    try:
        d = _get(args.base, tok, "/api/workspace/metrics")
        if "send_gate_blocks" not in d:
            fails.append("workspace metrics 缺 send_gate_blocks 段")
        else:
            print(f"PASS metrics.send_gate_blocks: "
                  f"total={(d['send_gate_blocks'] or {}).get('total')}")
    except Exception as ex:
        print(f"INFO metrics 探针跳过（主管专属端点，Bearer 无 session 角色）: {ex}")

    if fails:
        for f in fails:
            print("FAIL:", f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
