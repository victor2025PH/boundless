# -*- coding: utf-8 -*-
"""
ready.py — 服务就绪探针（纯标准库，零依赖）

并发轮询各服务 /health，实时显示「X/Y 就绪」，全部就绪即退出 0（超时退出 1）。
用于替代 start_all_services.bat 里「硬等 180 秒」的盲目等待 —— 真正就绪才放行。

用法：
  python ready.py                # 等核心链路就绪（fish_tts/stt/lipsync/vcam/hub）
  python ready.py --all          # 等全部服务（含换脸/情感/高清等扩展）
  python ready.py --timeout 240  # 自定义总超时（秒）
  python ready.py --quiet        # 只输出最终结果

退出码：0 = 目标服务全部就绪；1 = 超时仍有未就绪。
"""
import sys, io, time, argparse
from urllib.request import urlopen

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from concurrent.futures import ThreadPoolExecutor
except Exception:
    ThreadPoolExecutor = None

import app_config


def _probe(url: str, timeout: float = 3.0) -> bool:
    try:
        with urlopen(url, timeout=timeout) as r:
            return getattr(r, "status", r.getcode()) == 200
    except Exception:
        return False


def _probe_many(names, timeout=3.0):
    """并发探测一批服务，返回 {name: bool}。"""
    urls = {n: app_config.health_url(n) for n in names}
    if ThreadPoolExecutor and len(names) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(names))) as ex:
            futs = {n: ex.submit(_probe, u, timeout) for n, u in urls.items()}
            return {n: f.result() for n, f in futs.items()}
    return {n: _probe(u, timeout) for n, u in urls.items()}


def main():
    ap = argparse.ArgumentParser(description="服务就绪探针")
    ap.add_argument("--all", action="store_true", help="包含扩展服务（默认仅核心链路）")
    ap.add_argument("--timeout", type=int, default=0, help="总超时秒数（默认按最慢服务自动推算）")
    ap.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒")
    ap.add_argument("--quiet", action="store_true", help="只输出最终结果")
    args = ap.parse_args()

    svcs = app_config.SERVICES
    targets = [n for n, s in svcs.items() if (args.all or s.get("core"))]
    labels = {n: svcs[n].get("label", n) for n in targets}

    # 默认超时 = 最慢服务的 delay + 60s 缓冲
    timeout = args.timeout or (max((svcs[n].get("delay", 10) for n in targets), default=30) + 60)

    total = len(targets)
    if not args.quiet:
        scope = "全部" if args.all else "核心链路"
        print(f"[ready] 等待{scope} {total} 个服务就绪（超时 {timeout}s）：{', '.join(targets)}")

    start = time.time()
    ready = set()
    while True:
        pending = [n for n in targets if n not in ready]
        res = _probe_many(pending, timeout=3.0)
        for n in pending:
            if res.get(n):
                ready.add(n)
                if not args.quiet:
                    el = time.time() - start
                    print(f"  [OK] {labels[n]:28s} 就绪  ({el:.0f}s)")
        elapsed = time.time() - start
        if len(ready) >= total:
            break
        if elapsed >= timeout:
            break
        if not args.quiet:
            print(f"  ... 就绪 {len(ready)}/{total}  （已等 {elapsed:.0f}s，仍等：{', '.join(n for n in targets if n not in ready)}）",
                  flush=True)
        time.sleep(args.interval)

    elapsed = time.time() - start
    print("-" * 56)
    for n in targets:
        ok = n in ready
        print(f"  {'[OK]' if ok else '[--]'} {labels[n]:28s} {'就绪' if ok else '未就绪'}")
    ok_all = len(ready) >= total
    print(f"[ready] {len(ready)}/{total} 就绪，用时 {elapsed:.0f}s — {'全部就绪 ✓' if ok_all else '仍有未就绪 ✗'}")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
