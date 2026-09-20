#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Messenger 富交互 DOM 漂移探针 / 校准工具（P5 双面板融合 2026-08-13）。

react（表情回应）与 quote（引用回复）是脆 DOM 操作——FB 一改版，aria 抽取/按钮
选择器就可能失配，导致 target_not_found / react_ui_not_found 静默积累。本工具把
「选择器还活着吗」从坐席报障提前成一条可随时跑的探针，并在 FB 改版后用来重新校准。

安全默认＝**只读漂移探针**（不发消息、不加表情）：
  python tools/probe_messenger_dom.py
  python tools/probe_messenger_dom.py --jid 1054679137531045 --text "你在做什么。"
    - GET /accounts：登录态 + op_stats（react/quote 成败累计，选择器漂移读数）
    - GET /debug/tail?thread=<jid>：aria 抽取健康（msgLikeCount / msgSample）——
      判「消息抽取器还能从线程里读出消息吗」。⚠ /debug/tail 会打开该线程（FB 标已读）；
      对真实客户线程慎用，校准优先用自有测试线程。
    - --text：对 /debug/tail 回传的**最近 6 条** sample 按 sidecar 同款三级匹配预判
      （字段 locate_in_recent6）；权威定位以 --confirm 实弹为准（真定位器扫全 [role=log]）。

实弹校准（须显式 --confirm + --jid，向真实线程发内容）：
  python tools/probe_messenger_dom.py --jid <JID> --confirm --react 👍
  python tools/probe_messenger_dom.py --jid <JID> --confirm --quote "校准引用" --text "被引用原文"
    - --react：POST /accounts/<acct>/react（面板白名单外的 emoji 会被 sidecar 拒 unsupported_emoji）
    - --quote：POST /accounts/<acct>/send，quoted.text=--text（degrade-safe：定位不到就普通发送）
    - 无 --confirm 一律不写（只报「将要做什么」）。

退出码：0=探针通过（或只读）；1=漂移信号（rows 抽取为 0 / 实弹失败）；2=用法/连接错误。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_BASE = "http://127.0.0.1:8791"


def _req(method: str, url: str, payload: Optional[dict] = None,
         timeout: float = 90.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, method=method, data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return 0, {"_err": str(e)}


def _match_local(sample: List[str], want: str) -> str:
    """本地复刻 sidecar matchQuotedTarget 三级判定（诊断「文本可否定位」）。"""
    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", str(s or "")).strip().lower()
    w = norm(want)
    rows = [norm(s) for s in (sample or [])]
    if not w:
        return "empty_text"
    exact = [i for i, t in enumerate(rows) if t and t == w]
    if len(exact) == 1:
        return "exact"
    if len(exact) > 1:
        return "ambiguous_exact"
    pref = [i for i, t in enumerate(rows)
            if t and (t.startswith(w) or w.startswith(t)) and min(len(t), len(w)) >= 4]
    if len(pref) == 1:
        return "prefix"
    if len(pref) > 1:
        return "ambiguous_prefix"
    if len(w) >= 6:
        contains = [i for i, t in enumerate(rows) if t and w in t]
        if len(contains) == 1:
            return "contains"
    return "not_found"


def main() -> int:
    ap = argparse.ArgumentParser(description="Messenger 富交互 DOM 漂移探针 / 校准")
    ap.add_argument("--base", default=DEFAULT_BASE, help="sidecar base url")
    ap.add_argument("--account", default="", help="账号 id（默认取首个已登录）")
    ap.add_argument("--jid", default="", help="目标线程 key（漂移探针/实弹均用）")
    ap.add_argument("--text", default="", help="被引用/被回应的原文（定位靶）")
    ap.add_argument("--react", default="", help="实弹：发这个表情（如 👍）")
    ap.add_argument("--quote", default="", help="实弹：带引用发这段文字")
    ap.add_argument("--confirm", action="store_true", help="实弹开关（无则只读）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    out: Dict[str, Any] = {"base": base, "checks": []}

    st, acc = _req("GET", f"{base}/accounts", timeout=10)
    if st != 200 or not (acc.get("accounts")):
        print(f"[x] /accounts 不可达或无账号 (http={st})", file=sys.stderr)
        return 2
    accts = acc["accounts"]
    row = None
    if args.account:
        row = next((a for a in accts if str(a.get("account_id")) == args.account), None)
    else:
        row = next((a for a in accts if a.get("logged_in")), accts[0])
    if not row:
        print("[x] 找不到目标账号", file=sys.stderr)
        return 2
    acct = str(row.get("account_id"))
    out["account"] = acct
    out["logged_in"] = bool(row.get("logged_in"))
    out["op_stats"] = row.get("op_stats") or {}

    rc = 0
    # 1) 漂移探针：aria 抽取健康（须给 jid 才有靶线程）
    if args.jid:
        st, tail = _req("GET", f"{base}/debug/tail?thread={args.jid}", timeout=45)
        mlc = tail.get("probe", {}).get("msgLikeCount") if isinstance(
            tail.get("probe"), dict) else tail.get("msgLikeCount")
        sample = (tail.get("probe", {}) or {}).get("msgSample") if isinstance(
            tail.get("probe"), dict) else tail.get("msgSample")
        sample = sample or []
        drift = {"jid": args.jid, "http": st, "msgLikeCount": mlc, "sample": sample}
        if args.text:
            # ⚠ 仅对 /debug/tail 回传的**最近 6 条** sample 判定；真实 react/quote 定位器
            # 扫的是整个 [role=log]（比这多）。故 not_found 只表示「不在最近 6 条里」，
            # 不代表实际定位失败——权威结果以 --confirm 实弹为准。命名如实带 _in_recent6。
            drift["locate_in_recent6"] = _match_local(sample, args.text)
        out["checks"].append({"drift_probe": drift})
        if st == 200 and (mlc == 0):
            rc = 1  # 抽取器读出 0 条 = 强漂移信号
    else:
        out["checks"].append({"drift_probe": "skipped (no --jid)"})

    # 2) 实弹（须 --confirm + --jid）
    if args.react or args.quote:
        if not args.confirm or not args.jid:
            out["checks"].append({"live_action": "DRY (need --confirm and --jid)"})
        else:
            if args.react:
                if not args.text:
                    out["checks"].append({"react": "skipped (need --text as anchor)"})
                else:
                    st, b = _req("POST", f"{base}/accounts/{acct}/react",
                                 {"jid": args.jid, "emoji": args.react,
                                  "target_text": args.text})
                    out["checks"].append({"react": {"http": st, **b}})
                    if not b.get("ok"):
                        rc = 1
            if args.quote:
                anchor = args.text or ""
                st, b = _req("POST", f"{base}/accounts/{acct}/send",
                             {"jid": args.jid, "text": args.quote,
                              "quoted": {"text": anchor} if anchor else {}})
                out["checks"].append({"quote": {"http": st, **b}})
                if not (b.get("delivered")):
                    rc = 1
                elif anchor and not b.get("quoted"):
                    out["checks"].append({"quote_note": "delivered but NOT quoted (degraded) — locator drift?"})

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(f"account={out['account']} logged_in={out['logged_in']}")
        print(f"op_stats={json.dumps(out['op_stats'], ensure_ascii=False)}")
        for c in out["checks"]:
            print(json.dumps(c, ensure_ascii=False))
        print(f"verdict: {'DRIFT/FAIL' if rc == 1 else 'ok'}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
