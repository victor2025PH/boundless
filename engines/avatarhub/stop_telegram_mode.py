# -*- coding: utf-8 -*-
"""
Telegram 通话模式 · 一键收尾
─────────────────────────────────────────────────────────────────────────
干净停掉本模式在【本机】启动的服务，便于切回数字人栈或释放资源。
远端 STT/换脸（其它机器，如 192.168.1.51 / .43）不受影响——只按本机端口与
脚本名结束进程，不会去动局域网里别的机器。

复用 start_telegram_mode.py 里已验证过的清场函数（单一来源，行为一致）。

用法：
    python stop_telegram_mode.py              停止全部(同传/换脸推流/字幕窗 + 本机 Hub/Fish)
    python stop_telegram_mode.py --keep-core  仅停 同传/换脸推流/字幕窗，保留 Hub+Fish 复用
"""
import sys
import time
import argparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import requests
except Exception:
    requests = None

# 复用编排器的清场逻辑（按端口找 PID / 按脚本名结束），保证与启动侧一致
from start_telegram_mode import kill_pids_on_ports, kill_by_script


def info(m):
    print(m, flush=True)


def _alive(url):
    if requests is None:
        return None
    try:
        return requests.get(url, timeout=2).status_code == 200
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="停止 Telegram 通话模式（本机服务）")
    ap.add_argument("--keep-core", action="store_true",
                    help="保留 Hub+Fish 运行，仅停 同传/换脸推流/字幕窗")
    args = ap.parse_args()

    info("=" * 44)
    info(" 停止 Telegram 通话模式")
    info("=" * 44)

    # 1) 先让同传优雅停止采集（释放麦克风/环回设备），再结束进程
    if requests is not None:
        try:
            requests.post("http://127.0.0.1:7900/stop", timeout=5)
            info("  已请求同传优雅停止采集 (7900/stop)")
        except Exception:
            pass

    # 2) 不占端口的进程(实时换脸/字幕窗/同传)按脚本名结束；占端口的按端口结束
    scripts = ["realtime_stream", "subtitle_overlay", "live_interpreter"]
    ports = {7900}
    if not args.keep_core:
        scripts += ["fish_speech_server", "stt_server", "avatar_hub", "faceswap_api"]
        ports |= {7855, 7854, 9000, 8000}     # 远端服务无本机监听 → 自动跳过，不误杀别的机器

    info("  按端口结束本机监听进程: " + ", ".join(str(p) for p in sorted(ports)))
    killed_ports = kill_pids_on_ports(ports)
    info("  按脚本名结束进程: " + ", ".join(scripts))
    kill_by_script(scripts)
    time.sleep(1.0)

    # 3) 复核
    info("\n复核：")
    for name, url in [("Hub(9000)", "http://127.0.0.1:9000/health"),
                      ("Fish(7855)", "http://127.0.0.1:7855/health"),
                      ("同传(7900)", "http://127.0.0.1:7900/health")]:
        a = _alive(url)
        tag = "仍在运行" if a else ("已停止" if a is False else "未知")
        info(f"  {name}: {tag}")

    info("\n完成。远端 STT/换脸（其它机器）未受影响。")
    if args.keep_core:
        info("已保留 Hub+Fish；再次启动通话模式会复用它们。")
    else:
        info("如需切回数字人栈，请运行对应的启动脚本（会自行拉起所需服务）。")


if __name__ == "__main__":
    main()
