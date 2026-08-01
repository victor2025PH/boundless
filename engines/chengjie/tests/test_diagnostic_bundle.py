# -*- coding: utf-8 -*-
"""P2-198 一键诊断包纯核心门禁：密钥打码 + zip 组装 + 软失败。"""

from __future__ import annotations

import io
import json
import zipfile

from src.utils.diagnostic_bundle import build_diagnostic_bundle, redact_secrets_text


def test_redact_yaml_secret_values():
    text = (
        "ai:\n"
        "  api_key: sk-live-abc123\n"
        "  base_url: https://api.deepseek.com\n"
        "web_admin:\n"
        "  auth_token: admin\n"
        "telegram:\n"
        "  api_hash: \"deadbeef\"\n"
        "  session_name: desktop\n"
        "  phone_number: ''\n"
    )
    out = redact_secrets_text(text)
    assert "sk-live-abc123" not in out
    assert "deadbeef" not in out
    assert "api_key: ***" in out
    assert "auth_token: ***" in out
    # 非密钥键保持原样（诊断可读性）
    assert "base_url: https://api.deepseek.com" in out
    assert "session_name: desktop" in out


def test_redact_json_secret_values():
    text = json.dumps([{
        "name": "webhook", "format": "telegram",
        "token": "123456:AAAbbb", "secret": "s3cr3t", "target": "-100999",
    }], ensure_ascii=False)
    out = redact_secrets_text(text)
    assert "AAAbbb" not in out and "s3cr3t" not in out
    assert '"token": "***"' in out
    assert '"target": "-100999"' in out     # 非密钥字段不动


def test_bundle_contents_redaction_and_tail(tmp_path):
    cfg = tmp_path / "config"
    logs = tmp_path / "logs"
    cfg.mkdir(); logs.mkdir()
    (cfg / "config.yaml").write_text(
        "ai:\n  api_key: sk-live-xyz\n  model: deepseek-chat\n", encoding="utf-8")
    (cfg / "notify_webhooks.json").write_text(
        '[{"token": "tok-1", "enabled": true}]', encoding="utf-8")
    (cfg / "ignored.db").write_bytes(b"\x00" * 64)          # 白名单外：绝不进包
    (logs / "app.log").write_text("hello log\n", encoding="utf-8")
    big = "x" * (300 * 1024)
    (logs / "big.log").write_text(big, encoding="utf-8")     # 超尾部截断上限

    blob = build_diagnostic_bundle(
        config_dir=cfg, logs_dir=logs, meta={"app": {"version": "t"}},
        log_tail_kb=64)
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = set(zf.namelist())
    assert "meta.json" in names
    assert "config/config.yaml" in names
    assert "config/notify_webhooks.json" in names
    assert "logs/app.log" in names and "logs/big.log" in names
    assert not any(n.endswith("ignored.db") for n in names)
    # 密钥打码进包
    assert b"sk-live-xyz" not in zf.read("config/config.yaml")
    assert b"tok-1" not in zf.read("config/notify_webhooks.json")
    # 尾部截断：64KB 上限
    assert len(zf.read("logs/big.log")) <= 64 * 1024
    meta = json.loads(zf.read("meta.json"))
    assert meta["app"]["version"] == "t" and meta.get("generated_at")


def test_bundle_never_raises_on_missing_dirs(tmp_path):
    blob = build_diagnostic_bundle(config_dir=None, logs_dir=None, meta=None)
    zf = zipfile.ZipFile(io.BytesIO(blob))
    assert "meta.json" in zf.namelist()
    blob2 = build_diagnostic_bundle(
        config_dir=tmp_path / "nope", logs_dir=tmp_path / "nada", meta={})
    assert zipfile.ZipFile(io.BytesIO(blob2)).namelist()
