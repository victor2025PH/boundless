# -*- coding: utf-8 -*-
"""手动设置 Telegram Bot 回调（setWebhook）——**只在人执行本脚本时调用**。

与向导「设置 Webhook」按钮同一实现（``telegram_bot_official.tg_bot_set_webhook``）：
url = ``<public_base_url><telegram_bot.webhook_path>``，secret_token = ``telegram_bot.webhook_secret``，
allowed_updates 只订阅本系统处理的更新类型。输出只含主机名 / 路径 / 结果，不打印 token 与 secret。

用法（在 engines/chengjie 下）::

    python tools/tg_bot_set_webhook.py --url https://bot.example.com            # 先看会设成什么（不调用）
    python tools/tg_bot_set_webhook.py --url https://bot.example.com --apply    # 真正调用 setWebhook
    python tools/tg_bot_set_webhook.py --url https://bot.example.com --apply --config config/config.yaml

``--apply`` 不带时只做 dry-run（校验凭证格式与 URL，打印将要设置的回调地址）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="手动设置 Telegram Bot 回调（setWebhook）")
    ap.add_argument("--url", required=True, help="本服务对外的 https 地址（不含路径）")
    ap.add_argument("--config", default="", help="配置文件路径（默认按 ConfigManager 规则查找）")
    ap.add_argument("--apply", action="store_true", help="真正调用 setWebhook（不带＝只预览）")
    ap.add_argument("--drop-pending", action="store_true", help="同时丢弃 Telegram 侧积压的旧更新")
    args = ap.parse_args(argv)

    from src.integrations.telegram_bot_official import (
        bot_id_from_token, tg_bot_set_webhook, webhook_url_for,
    )
    from src.utils.config_manager import ConfigManager

    cm = ConfigManager(args.config or None)
    cfg = dict(cm.config or {})
    tb = dict(cfg.get("telegram_bot") or {})
    target = webhook_url_for(tb, args.url)
    preview = {"bot_id": bot_id_from_token(tb.get("bot_token")), "webhook": target or "(invalid url)",
               "has_secret": bool(str(tb.get("webhook_secret") or "").strip()), "apply": bool(args.apply)}
    print(json.dumps(preview, ensure_ascii=False))
    if not args.apply:
        return 0 if target else 2
    out = asyncio.run(tg_bot_set_webhook(cfg, args.url, drop_pending_updates=args.drop_pending))
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
