# -*- coding: utf-8 -*-
"""智安 · Telegram official：Bot token / 回调密钥不得明文进日志。

纯本地：日志断言 + 回环关闭端口触发网络错误，不触达 Telegram、不真发。
测试里的 token 运行时拼出（源码里没有 token 形态字面量，免 gitleaks 误报）。
"""
from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path

import pytest

from src.integrations import telegram_bot_official as tbo

BOT_ID = "123456789"
TOKEN = BOT_ID + ":" + "A" + "b7" * 17          # 形如 <id>:<35 位>
SECRET = "hook_" + "s3" * 10


def _logs(caplog) -> str:
    return "\n".join(caplog.handler.format(r) for r in caplog.records)


def test_redact_token_keeps_bot_id_only():
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    out = tbo.redact_token(url)
    assert TOKEN not in out and f"bot{BOT_ID}:***/sendMessage" in out
    assert tbo.redact_token(f"/file/bot{TOKEN}/photos/a.jpg").count(":***") == 1
    assert tbo.redact_token("chat 12345678 said hi") == "chat 12345678 said hi"
    assert tbo.redact_token(None) == ""


def test_logger_message_and_args_are_redacted(caplog):
    caplog.set_level(logging.DEBUG, logger=tbo.logger.name)
    tbo.logger.warning("[tg_bot] 调用失败 url=%s", f"https://api.telegram.org/bot{TOKEN}/getMe")
    text = _logs(caplog)
    assert TOKEN not in text and f"{BOT_ID}:***" in text


def test_logger_exception_traceback_is_redacted(caplog):
    caplog.set_level(logging.DEBUG, logger=tbo.logger.name)
    try:
        raise RuntimeError(f"Cannot connect to host api.telegram.org url=/bot{TOKEN}/sendPhoto")
    except RuntimeError as e:
        tbo.logger.exception("[tg_bot] update 处理异常: %s", e)
    text = _logs(caplog)
    assert TOKEN not in text
    assert "RuntimeError" in text and f"{BOT_ID}:***" in text   # 排障信息还在


def test_network_failure_paths_leak_nothing(caplog, monkeypatch):
    """回环关闭端口 → 网络错误；返回值与日志都不得含 token / 回调密钥。"""
    caplog.set_level(logging.DEBUG, logger=tbo.logger.name)
    cfg = {"telegram_bot": {"enabled": True, "bot_token": TOKEN, "webhook_secret": SECRET,
                            "api_base": "http://127.0.0.1:9"}}
    tbo.configure_runtime(cfg["telegram_bot"])
    res = asyncio.run(tbo.tg_bot_set_webhook(cfg, "https://example.invalid", timeout=2.0))
    probe = asyncio.run(tbo.tg_bot_probe(cfg, timeout=2.0))
    blob = repr(res) + repr(probe) + _logs(caplog)
    assert res["ok"] is False
    assert TOKEN not in blob and SECRET not in blob


def test_no_logging_call_formats_credentials_directly():
    """静态钉：本模块任何 logger.* / print 调用的参数里都不直接放 token / secret 变量。"""
    src = Path(tbo.__file__).read_text(encoding="utf-8")
    bad = []
    names = {"token", "bot_token", "secret", "webhook_secret"}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            f = node.func
            is_log = (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                      and f.value.id == "logger") or (isinstance(f, ast.Name) and f.id == "print")
            if not is_log:
                continue
            for a in list(node.args) + [k.value for k in node.keywords]:
                for n in ast.walk(a):
                    if isinstance(n, ast.Name) and n.id in names:
                        bad.append((node.lineno, n.id))
    assert bad == []


def test_wizard_fields_are_secret_and_set_webhook_is_manual_only():
    from src.utils.channel_setup import CHANNELS
    ch = next(c for c in CHANNELS if c.id == "telegram_bot")
    secret = {f.key for f in ch.fields if getattr(f, "secret", False)}
    assert {"telegram_bot.bot_token", "telegram_bot.webhook_secret"} <= secret
    root = Path(tbo.__file__).resolve().parents[1]
    callers = sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*.py")
                     if "tg_bot_set_webhook(" in p.read_text(encoding="utf-8", errors="ignore"))
    # 定义处 + 唯一的人工入口（POST /api/admin/telegram-bot/set-webhook）
    assert callers == ["integrations/telegram_bot_official.py",
                       "web/routes/unified_inbox_account_routes.py"], callers


def test_official_mode_registered():
    from src.integrations import platform_registry as pr
    spec = pr.get("telegram")
    assert spec is not None and "official" in spec.modes
