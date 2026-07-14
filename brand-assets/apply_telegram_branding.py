# -*- coding: utf-8 -*-
"""
把新品牌头像 + 三系简介应用到 Telegram 频道/群（线上操作，可重复跑，幂等）。

  频道 @hykj7  → boundless-avatar-ring.png（光环 = 官方资源号识别符）+ 新标题/简介
  群   @hykjz  → boundless-avatar.png（纯主标）+ 新标题/简介

Token 读取顺序：环境变量 TELEGRAM_BOT_TOKEN → telegram-mtproto-ai/website/.env.local。
文案与 website/lib/tg-broadcast.ts::CHANNEL_BRAND 保持一致（改那边请同步这边）。

用法：  python apply_telegram_branding.py [--dry-run]
限制：  Bot 自身头像 Bot API 不支持（BotFather /setuserpic 手动）；
        人工客服个人号头像需在客户端手动上传。
"""

import json
import os
import re
import sys
import urllib.request

# Windows 控制台默认 GBK，简介含 emoji 会打印崩溃
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WS = r"D:\workspace"
LOGOS = os.path.join(WS, "ai-p0-integration", "website", "public", "brand", "logos")
ENV_LOCAL = os.path.join(WS, "telegram-mtproto-ai", "website", ".env.local")

CHANNEL = "@hykj7"
GROUP = "@hykjz"

# 与 tg-broadcast.ts::CHANNEL_BRAND 逐字一致
CHANNEL_TITLE = "无界科技 BOUNDLESS · 官方频道"
CHANNEL_DESC = (
    "无界科技官方频道 · 让沟通，无界。"
    "🎯智连（智拓获客·智聊AI成交）🎭幻境（幻颜换脸·幻声克隆·幻影直播分身）🌐通达（通译翻译·通传同传）。"
    "真实案例 · 新功能 · 限时优惠第一时间发布 · USDT 结算。"
    "⚠️官方客服头像带「客服」徽标，谨防假冒。官网与客服见置顶。"
)
GROUP_TITLE = "无界科技 · 交流群"
GROUP_DESC = (
    "无界科技官方交流群 · 三系七款：智拓获客/智聊AI成交/幻颜换脸/幻声克隆/幻影直播分身/通译翻译/通传同传。"
    "提问、领试用、同行交流。⚠️官方客服头像带「客服」徽标，谨防假冒。"
    "@小界 或点客服随时响应；广告与刷屏将被移除。"
)


def read_token():
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if tok:
        return tok
    with open(ENV_LOCAL, encoding="utf-8") as f:
        m = re.search(r"^TELEGRAM_BOT_TOKEN=(\S+)", f.read(), re.M)
    if not m:
        raise SystemExit("no TELEGRAM_BOT_TOKEN found")
    return m.group(1)


def api_json(token, method, payload):
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/%s" % (token, method),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def api_photo(token, chat, photo_path):
    """setChatPhoto multipart 上传（不依赖 requests）。"""
    boundary = "----brandsync7f3a"
    with open(photo_path, "rb") as f:
        img = f.read()
    parts = []
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n%s\r\n"
                  % (boundary, chat)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"%s\"\r\n"
                  "Content-Type: image/png\r\n\r\n" % (boundary, os.path.basename(photo_path))).encode())
    parts.append(img)
    parts.append(("\r\n--%s--\r\n" % boundary).encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/setChatPhoto" % token,
        data=body,
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def ok_or_not_modified(resp):
    if resp.get("ok"):
        return True, "ok"
    desc = str(resp.get("description", ""))
    if "is not modified" in desc:
        return True, "unchanged"
    return False, desc


def main():
    dry = "--dry-run" in sys.argv
    token = read_token()
    me = api_json(token, "getMe", {})
    if not me.get("ok"):
        raise SystemExit("getMe failed: %s" % me)
    print("[bot] @%s (id %s)" % (me["result"].get("username"), me["result"].get("id")))

    jobs = [
        ("setChatTitle", {"chat_id": CHANNEL, "title": CHANNEL_TITLE[:128]}),
        ("setChatDescription", {"chat_id": CHANNEL, "description": CHANNEL_DESC[:255]}),
        ("setChatTitle", {"chat_id": GROUP, "title": GROUP_TITLE[:128]}),
        ("setChatDescription", {"chat_id": GROUP, "description": GROUP_DESC[:255]}),
    ]
    photos = [
        (CHANNEL, os.path.join(LOGOS, "boundless-avatar-ring.png")),
        (GROUP, os.path.join(LOGOS, "boundless-avatar.png")),
    ]
    if dry:
        for m, p in jobs:
            print("[dry] %s %s" % (m, p))
        for chat, ph in photos:
            print("[dry] setChatPhoto %s <- %s (%d KB)" % (chat, ph, os.path.getsize(ph) // 1024))
        return

    failures = 0
    for method, payload in jobs:
        good, msg = ok_or_not_modified(api_json(token, method, payload))
        print("[%s] %s %s -> %s" % ("ok" if good else "FAIL", method, payload["chat_id"], msg))
        failures += 0 if good else 1
    for chat, ph in photos:
        good, msg = ok_or_not_modified(api_photo(token, chat, ph))
        print("[%s] setChatPhoto %s <- %s -> %s" % ("ok" if good else "FAIL", chat, os.path.basename(ph), msg))
        failures += 0 if good else 1

    print("DONE with %d failure(s)." % failures)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
