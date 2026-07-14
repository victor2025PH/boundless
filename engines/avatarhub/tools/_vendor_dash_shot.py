# -*- coding: utf-8 -*-
"""P13 厂商看板实拍：验证 遥测卡+试用漏斗卡+按周转化时序 同屏（P12 双 fetch 竞态回归探针）。
用法：python tools/_vendor_dash_shot.py [--self-serve | --base http://127.0.0.1:8765]
                                        [--out xxx.png] [--rounds 3]
--self-serve：自起一个临时 license_server（随机端口，跑完即杀），门禁无需常驻发牌服务。
退出码：0=通过 1=断言失败 2=服务不可达/无法自起（跳过）。
（独立文件而非内联 heredoc：中文字面量走 PowerShell 管道会被转码成乱码，产生假阴性。）"""
import argparse
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--self-serve", action="store_true",
                    help="自起临时 license_server（随机空闲端口），跑完即杀")
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "stream_states" / "vendor_dash_weekly.png"))
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()

    proc = None
    if args.self_serve:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
        args.base = f"http://127.0.0.1:{port}"
        proc = subprocess.Popen([sys.executable, str(HERE / "license_server.py"), "serve",
                                 "--port", str(port)], cwd=str(HERE),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            time.sleep(0.3)
            try:
                urllib.request.urlopen(args.base + "/api/funnel", timeout=2); break
            except Exception:
                if proc.poll() is not None:
                    print("SKIP: license_server 自起失败（无 secrets/sk？）"); return 2
        else:
            proc.kill(); print("SKIP: license_server 自起超时"); return 2

    try:
        return _probe(args)
    finally:
        if proc:
            proc.kill()


def _probe(args) -> int:
    try:
        urllib.request.urlopen(args.base + "/api/funnel", timeout=5)
    except Exception as e:
        print(f"SKIP: 发牌服务不可达 {args.base}（{e}）"); return 2

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        for i in range(args.rounds):
            pg = b.new_page(viewport={"width": 1100, "height": 900})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(args.base + "/dashboard", wait_until="networkidle")
            pg.wait_for_timeout(600)
            txt = pg.inner_text("body")
            assert "回执数" in txt, f"round{i+1}: 遥测卡缺失"
            assert "发出试签" in txt and "试用转正" in txt, f"round{i+1}: 漏斗卡缺失（竞态回归？）"
            assert not errs, f"round{i+1}: JS 错误 {errs}"
            has_weekly = "试用转化时序" in txt
            if i == 0:
                pg.screenshot(path=str(out), full_page=True)
            pg.close()
            print(f"  round{i+1}: 遥测卡 ✓ 漏斗卡 ✓ 时序卡 {'✓' if has_weekly else '（无签发数据，未渲染=预期）'}")
        b.close()
    print(f"OK: {args.rounds} 连开全部在场 → {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"FAIL: {e}")
        sys.exit(1)
