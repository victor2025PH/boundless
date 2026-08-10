# -*- coding: utf-8 -*-
"""告警外发通道配置引导：把 webhook 写入 secrets/alert_webhooks.txt 并发一条测试告警验证连通。

为什么需要它：定时巡检(selfcheck_pipeline --alert / 计划任务)发现红旗后经 alerts.py 告警，
但若没配任何 webhook，告警只落文件+本地托盘弹窗——人不在电脑前就收不到。配一个
钉钉/企业微信群机器人 URL，即可手机收警，定时巡检的价值才真正兑现。

类型自动识别（与 alerts.py 完全一致）：
  URL 含 dingtalk → 钉钉文本      含 weixin/qyapi → 企业微信文本      其它 → 通用 {"text": ...}

用法：
  python setup_alert_webhook.py                       # 交互式：看现状→粘贴URL→写入→发测试
  python setup_alert_webhook.py --url <URL>           # 直接添加并发测试
  python setup_alert_webhook.py --url <URL> --no-test # 只添加不发测试
  python setup_alert_webhook.py --list                # 只看当前通道(掩码,不泄露token)
  python setup_alert_webhook.py --test                # 只对已配置通道发测试告警
  python setup_alert_webhook.py --remove <URL>        # 移除某个 webhook
"""
import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import alerts   # 复用：channels / send_test / _payload / _mask_url / WEBHOOK_FILE / _webhooks


def _detect_type(url: str) -> str:
    low = url.lower()
    if "dingtalk" in low:
        return "钉钉 (DingTalk 文本)"
    if "weixin" in low or "qyapi" in low:
        return "企业微信 (WeCom 文本)"
    return '通用 JSON ({"text": ...})'


def _read_lines() -> list:
    p = alerts.WEBHOOK_FILE
    if not p.exists():
        return []
    return p.read_text(encoding="utf-8").splitlines()


def _existing_urls() -> list:
    return [ln.strip() for ln in _read_lines()
            if ln.strip() and not ln.strip().startswith("#")]


def add_url(url: str):
    url = (url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return False, "URL 必须以 http:// 或 https:// 开头"
    p = alerts.WEBHOOK_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    if url in _existing_urls():
        return True, "该 URL 已存在，无需重复添加"
    lines = _read_lines()
    if not lines:
        lines = ["# AvatarHub 告警 webhook（每行一个 URL；# 开头为注释）",
                 "# 类型按 URL 自动识别：dingtalk=钉钉 / weixin|qyapi=企业微信 / 其它=通用 JSON"]
    lines.append(url)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True, "已写入 %s" % p


def remove_url(url: str):
    url = (url or "").strip()
    p = alerts.WEBHOOK_FILE
    if not p.exists():
        return False, "配置文件不存在：%s" % p
    lines = _read_lines()
    kept = [ln for ln in lines if ln.strip() != url]
    if len(kept) == len(lines):
        return False, "未在配置里找到该 URL"
    p.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return True, "已移除该 URL"


def show_channels():
    ch = alerts.channels()
    print("当前告警通道：")
    print("  webhook 数：%d" % ch["webhook_count"])
    for w in ch["webhooks"]:
        print("    - %s" % w)
    print("  本地托盘弹窗：%s" % ("开" if ch["local_toast"] else "关(AVATARHUB_ALERT_TOAST=0)"))
    if ch["mentions"]:
        print("  @人手机号：%s" % ", ".join(ch["mentions"]))
    if os.environ.get("AVATARHUB_ALERT_WEBHOOK", "").strip():
        print("  注：环境变量 AVATARHUB_ALERT_WEBHOOK 也配置了 webhook（与文件叠加、自动去重）")


def do_test():
    print("发送测试告警到所有已配置通道 + 本地弹窗…")
    r = alerts.send_test()
    print("  已配置 webhook：%d   成功送达：%d" % (r["webhook_count"], r["sent"]))
    for res in r["results"]:
        print("    [%s] %s" % ("OK" if res["ok"] else "FAIL", res["url"]))
    print("  本地托盘弹窗：%s" % ("已触发" if r["local_toast"] else "关闭"))
    if r["webhook_count"] == 0:
        print("  （未配置任何 webhook，仅本地弹窗。用 --url <URL> 或交互式添加一个群机器人。）")
    elif r["sent"] < r["webhook_count"]:
        print("  ⚠ 有通道投递失败：请检查 URL 是否正确、机器人是否被移出群、网络是否可达。")
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description="告警外发通道配置引导")
    ap.add_argument("--url", help="要添加的 webhook URL（钉钉/企业微信/通用）")
    ap.add_argument("--remove", help="要移除的 webhook URL")
    ap.add_argument("--list", action="store_true", help="只显示当前通道（掩码）")
    ap.add_argument("--test", action="store_true", help="对已配置通道发测试告警")
    ap.add_argument("--no-test", action="store_true", help="添加后不自动发测试")
    args = ap.parse_args()

    if args.list:
        show_channels()
        return 0

    if args.remove:
        ok, msg = remove_url(args.remove)
        print(("[OK] " if ok else "[X] ") + msg)
        show_channels()
        return 0 if ok else 1

    if args.test and not args.url:
        show_channels()
        print()
        do_test()
        return 0

    url = args.url
    if url is None:                       # 交互式
        show_channels()
        print()
        print("粘贴要添加的告警 webhook URL（钉钉/企业微信群机器人/通用；直接回车跳过）：")
        try:
            url = input("URL> ").strip()
        except EOFError:
            url = ""
        if not url:
            print("未输入 URL。")
            if _existing_urls():
                print()
                do_test()
            return 0

    print("识别类型：%s" % _detect_type(url))
    ok, msg = add_url(url)
    print(("[OK] " if ok else "[X] ") + msg)
    if not ok:
        return 1
    print()
    show_channels()
    if not args.no_test:
        print()
        do_test()
    return 0


if __name__ == "__main__":
    sys.exit(main())
