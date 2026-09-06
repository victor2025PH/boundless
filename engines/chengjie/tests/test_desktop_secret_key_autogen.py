"""L-6 D：桌面首启随机 web_admin.secret_key（消除每次 boot 的 [SECURITY] 改绑 127.0.0.1）。

两台内测机日志实锤：桌面壳按 #57 把后端放到 0.0.0.0 供手机扫码配对，bootstrap fail-safe 又因
默认 secret 把它扳回回环 → 「局域网入口未就绪」。钉住：
  - 桌面态 + 默认/占位 secret → 生成 ≥32 字节随机值，写 config.local.yaml，内存即时生效，幂等；
  - 用户/运维显式配置的随机值不动；服务器态（无 AITR_DESKTOP_MODE）不生成（保留原 fail-safe）；
  - insecure_default_secret_exposed 与旧内联判定逐字同义（只认字面默认；空串不触发；ALLOW_INSECURE=1 放行）；
  - 生成后同一 host=0.0.0.0 不再判「不安全暴露」→ 不改绑。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.bootstrap.web_app import insecure_default_secret_exposed
from src.utils.config_manager import ConfigManager

DEFAULT = "change-me-in-production"


def _cm(tmp_path: Path, secret=DEFAULT, *, omit=False) -> ConfigManager:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)
    web = {"enabled": True, "host": "0.0.0.0", "port": 18799, "auth_token": "strong-token-xyz"}
    if not omit:
        web["secret_key"] = secret
    (cfg_dir / "config.yaml").write_text(
        yaml.safe_dump({"web_admin": web}, allow_unicode=True), encoding="utf-8")
    cm = ConfigManager(str(cfg_dir / "config.yaml"))
    cm.config = yaml.safe_load((cfg_dir / "config.yaml").read_text(encoding="utf-8"))
    return cm


def _overlay(cm: ConfigManager) -> dict:
    p = cm.config_path.parent / "config.local.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


def test_desktop_default_secret_gets_random_and_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _cm(tmp_path)
    assert cm._ensure_web_secret_key() is True
    secret = cm.config["web_admin"]["secret_key"]
    assert secret != DEFAULT and len(secret) >= 43            # token_urlsafe(48) → 64 字符
    assert _overlay(cm)["web_admin"]["secret_key"] == secret
    # 幂等：第二次不再生成、不改值
    assert cm._ensure_web_secret_key() is False
    assert cm.config["web_admin"]["secret_key"] == secret
    # 生成后 0.0.0.0 不再触发改绑
    assert insecure_default_secret_exposed(secret, "0.0.0.0") is False


def test_desktop_missing_key_or_placeholder_also_generated(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _cm(tmp_path, omit=True)
    assert cm._ensure_web_secret_key() is True
    assert not ConfigManager.web_secret_is_default(cm.config["web_admin"]["secret_key"])
    cm2 = _cm(tmp_path / "b", secret="YOUR_SECRET_KEY")
    assert cm2._ensure_web_secret_key() is True


def test_explicit_secret_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _cm(tmp_path, secret="operator-set-9f8e7d6c5b4a3921")
    assert cm._ensure_web_secret_key() is False
    assert cm.config["web_admin"]["secret_key"] == "operator-set-9f8e7d6c5b4a3921"
    assert _overlay(cm) == {}


def test_server_mode_does_not_generate(tmp_path, monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    cm = _cm(tmp_path)
    assert cm._ensure_web_secret_key() is False
    assert cm.config["web_admin"]["secret_key"] == DEFAULT
    assert _overlay(cm) == {}
    # 服务器手写默认值 + 非本地绑定 → 原 fail-safe 仍然要改绑
    assert insecure_default_secret_exposed(DEFAULT, "0.0.0.0") is True


def test_config_flag_desktop_mode_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    cm = _cm(tmp_path)
    cm.config["app"] = {"desktop_mode": True}
    assert cm._ensure_web_secret_key() is True


@pytest.mark.parametrize("secret,host,allow,expected", [
    (DEFAULT, "0.0.0.0", None, True),
    (None, "0.0.0.0", None, True),            # 键缺失按默认算（旧语义）
    ("", "0.0.0.0", None, False),             # 空串不触发（旧语义逐字保留）
    (DEFAULT, "127.0.0.1", None, False),
    (DEFAULT, "localhost", None, False),
    (DEFAULT, "", None, False),
    (DEFAULT, "0.0.0.0", "1", False),         # ALLOW_INSECURE=1 放行
    ("random-abcdefghijklmnopqrstuvwxyz", "0.0.0.0", None, False),
])
def test_insecure_exposure_predicate(secret, host, allow, expected, monkeypatch):
    monkeypatch.delenv("ALLOW_INSECURE", raising=False)
    assert insecure_default_secret_exposed(secret, host, allow_insecure=allow) is expected


def test_web_secret_is_default_variants():
    assert ConfigManager.web_secret_is_default(None)
    assert ConfigManager.web_secret_is_default("")
    assert ConfigManager.web_secret_is_default(DEFAULT)
    assert ConfigManager.web_secret_is_default("YOUR_SECRET")
    assert ConfigManager.web_secret_is_default("CHANGE_ME_NOW")
    assert not ConfigManager.web_secret_is_default("k3x9-real-random-secret-value")
