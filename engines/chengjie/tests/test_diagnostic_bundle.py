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


# ── P2-12（B38 批 2026-08-22）：主日志 app.log 进包 ─────────────────────────
#
# 3DTSV9 实战暴露：包里只有 fatal/sidecar、主日志零痕迹——桌面部署主日志不在
# logs_dir 一级。extra_files 走「运行时 logger 树的真实落点」清单，每文件尾部
# 2MB（缺省），坏路径软跳过。

def test_bundle_extra_files_tail_2mb(tmp_path):
    main_log = tmp_path / "elsewhere" / "app.log"
    main_log.parent.mkdir()
    main_log.write_bytes(b"A" * (3 * 1024 * 1024))       # 3MB → 只收尾部 2MB
    blob = build_diagnostic_bundle(
        config_dir=None, logs_dir=None, meta={},
        extra_files={"logs/app/app.log": main_log})
    zf = zipfile.ZipFile(io.BytesIO(blob))
    assert "logs/app/app.log" in zf.namelist()
    data = zf.read("logs/app/app.log")
    assert len(data) == 2 * 1024 * 1024


def test_bundle_extra_files_soft_fail(tmp_path):
    blob = build_diagnostic_bundle(
        config_dir=None, logs_dir=None, meta={},
        extra_files={"logs/app/missing.log": tmp_path / "missing.log"})
    zf = zipfile.ZipFile(io.BytesIO(blob))
    assert "logs/app/missing.log" not in zf.namelist()
    assert "meta.json" in zf.namelist()


def test_runtime_log_files_collects_file_handlers(tmp_path):
    """diag_upload.runtime_log_files：进程正在写的 file handler 落点被收集。"""
    import logging

    from src.utils.diag_upload import runtime_log_files

    logf = tmp_path / "probe_app.log"
    h = logging.FileHandler(logf, encoding="utf-8")
    lg = logging.getLogger("ai_chat_assistant")
    lg.addHandler(h)
    try:
        lg.warning("probe line")
        out = runtime_log_files()
        assert any(str(p) == str(logf) for p in out.values()), out
        assert all(a.startswith("logs/app/") for a in out), out
    finally:
        lg.removeHandler(h)
        h.close()
