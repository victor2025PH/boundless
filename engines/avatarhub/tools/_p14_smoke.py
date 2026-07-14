# -*- coding: utf-8 -*-
"""P14/P15 冒烟：license_server 的 quickissue 全链 + /api/customers + 发码四级漏斗
（临期通知→点开→出码→激活）+ Hub /api/share/trend?profile= 与 /api/share/export CSV。
不碰真实 secrets（台账/遥测/试用全部重定向临时目录）。退出码 0=全过 1=有失败。"""
import json
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import license_server as ls

FAIL = 0


def ok(cond, name):
    global FAIL
    print(("  OK: " if cond else "  FAIL: ") + name)
    if not cond:
        FAIL = 1


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=6) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="p14smoke_"))
    ls.SK_FILE = tmp / "sk.pem"
    ls.SK_FILE.write_bytes(b"smoke-sk")
    ls._STATE["orders_path"] = tmp / "orders.json"
    ls._STATE["telemetry_path"] = tmp / "tele.jsonl"
    ls._STATE["trials_path"] = tmp / "trials.json"   # P15 漏斗读 trials，同样隔离
    ls._STATE["qi_edition"] = "pro"
    ls._STATE["qi_days"] = 365
    (tmp / "tele.jsonl").write_text(json.dumps(
        {"anon_id": "smoke-A", "received_at": int(time.time()), "fail": 0,
         "edition": "pro", "manifest_version": "9.9",
         "items": [{"cid": "voice", "ok": False}]}) + "\n", encoding="utf-8")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), ls.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        link = ls.qi_link("fp-smoke/01", base)
        st, body = _get(link)
        ok(st == 200 and "一键出码" in body and "fp-smoke/01" in body, "GET /quickissue 确认页可渲染")
        st2, b2 = _get(base + "/quickissue?fp=fp-smoke%2F01&exp=1&sig=deadbeef")
        ok(st2 == 400 and "链接无效" in b2, "GET /quickissue 假签/过期被拒")
        import urllib.parse as up
        q = up.parse_qs(up.urlparse(link).query)
        payload = {"fp": q["fp"][0], "exp": int(q["exp"][0]), "sig": q["sig"][0]}
        st3, d3 = _post(base + "/api/quickissue", payload)
        ok(st3 == 200 and d3.get("ok") and d3.get("code", "").startswith("AVH-"), "POST /api/quickissue 出码")
        st4, d4 = _post(base + "/api/quickissue", payload)
        ok(st4 == 200 and d4.get("code") == d3.get("code") and d4.get("reused"), "再点幂等复用同码")
        bad = dict(payload, sig="0" * 32)
        st5, d5 = _post(base + "/api/quickissue", bad)
        ok(st5 == 403 and not d5.get("ok"), "假签 403")
        oj = json.loads((tmp / "orders.json").read_text(encoding="utf-8"))
        ok(oj["codes"][d3["code"]]["via"] == "quickissue", "订单台账落 via=quickissue")
        st6, b6 = _get(base + "/api/customers")
        d6 = json.loads(b6)
        ok(st6 == 200 and d6.get("ok") and d6["customers"][0]["anon_id"] == "smoke-A"
           and d6["customers"][0]["top_fails"].get("voice") == 1, "GET /api/customers 聚合正确")
        # P15-1：确认页首开已记 qi_opened（漏斗第二级），/api/funnel 附 quickissue 四级漏斗
        oj15 = json.loads((tmp / "orders.json").read_text(encoding="utf-8"))
        ok("fp-smoke/01" in oj15.get("qi_opened", {}), "GET /quickissue 首开记入 qi_opened")
        st8, b8 = _get(base + "/api/funnel")
        d8 = json.loads(b8)
        qf = d8.get("quickissue") or {}
        ok(st8 == 200 and d8.get("ok") and qf.get("opened") == 1 and qf.get("issued") == 1
           and qf.get("activated") == 0, "GET /api/funnel 附一键发码四级漏斗")
    finally:
        srv.shutdown()

    # Hub 在线则顺手验 /api/share/trend?profile= 与 /api/share/export（不在线跳过，不算失败）。
    # 注意 profile 必须 quote：urlopen 遇非 ASCII URL 直接 UnicodeEncodeError——P14 版没编码，
    # 这条检查其实从未跑过（异常被当"不在线"吞掉）；P15 修正并收窄 except 只吞连接错误。
    import urllib.error
    import urllib.parse as up2
    try:
        urllib.request.urlopen("http://127.0.0.1:9000/health", timeout=3)
        hub_on = True
    except Exception:
        hub_on = False
        print("  SKIP: Hub 不在线，跳过 trend?profile=/export 冒烟")
    if hub_on:
        st7, b7 = _get("http://127.0.0.1:9000/api/share/trend?days=7&profile="
                       + up2.quote("__p14冒烟不存在__"))
        d7 = json.loads(b7)
        ok(st7 == 200 and d7.get("ok") and d7.get("profile") and len(d7["trend"]) == 7
           and all(x["scan"] == 0 for x in d7["trend"]), "Hub trend?profile= 未知角色回零序列")
        st9, b9 = _get("http://127.0.0.1:9000/api/share/export?fmt=long&days=7")
        ok(st9 == 200 and b9.lstrip("\ufeff").startswith("date,profile,action,count"),
           "Hub share/export 长表 CSV 可导出")

    print("P14 smoke:", "PASS" if FAIL == 0 else "FAIL")
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
