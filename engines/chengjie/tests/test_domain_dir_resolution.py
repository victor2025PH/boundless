"""域包目录定位门禁（安装版静默丢领域提示词事故回归）。

事故（0.2.1 安装版，173 日志）：``Domain 'conversion' has no manifest.yaml`` →
``Domain pack 'conversion' failed to load``。根因是 domains 目录从 **config_path**
推导（``config.yaml/../..``），而冻结态 config_path 在用户数据目录，只读的域包却随包
躺在 ``<_MEIPASS>/domains``。结果：领域系统提示词 / 术语 / 上下文补充 / 看板挂件全丢，
**不报错、不红灯**，只是 AI 回复质量无声降级——比崩溃更难发现。

不变量：
1. 首选位置（配置目录的 ../domains）有该域 → 用它（源码/生产行为不变，且客户可放私有域包）；
2. 首选位置没有、随代码那份有 → 回落到代码位置（冻结态的唯一活路）；
3. 两处都没有 → 返回首选位置（保持既有「加载失败」语义，不制造新异常）。
"""
from __future__ import annotations

from pathlib import Path

from src.utils.domain_loader import resolve_domains_dir

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_BUNDLED_DOMAINS = _ENGINE_ROOT / "domains"


def _make_domain(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.yaml").write_text("name: " + name + "\n", encoding="utf-8")
    return d


def test_prefers_config_relative_when_domain_present(tmp_path):
    cfg = tmp_path / "config" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("domain: acme\n", encoding="utf-8")
    _make_domain(tmp_path / "domains", "acme")
    assert resolve_domains_dir(str(cfg), "acme") == tmp_path / "domains"


def test_falls_back_to_code_relative_when_config_dir_empty(tmp_path):
    """冻结态实况：用户数据目录没有 domains，随包那份有 → 必须回落。"""
    cfg = tmp_path / "data" / "config" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("domain: conversion\n", encoding="utf-8")
    assert (_BUNDLED_DOMAINS / "conversion" / "manifest.yaml").exists(), \
        "仓库里 domains/conversion 不见了，本用例的前提失效"
    assert resolve_domains_dir(str(cfg), "conversion") == _BUNDLED_DOMAINS


def test_config_relative_wins_over_bundled(tmp_path):
    """同名域两边都有时优先用户侧——私有域包定制不被随包版本覆盖。"""
    cfg = tmp_path / "config" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("domain: conversion\n", encoding="utf-8")
    _make_domain(tmp_path / "domains", "conversion")
    assert resolve_domains_dir(str(cfg), "conversion") == tmp_path / "domains"


def test_unknown_domain_keeps_preferred_location(tmp_path):
    cfg = tmp_path / "config" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("domain: nope\n", encoding="utf-8")
    assert resolve_domains_dir(str(cfg), "does-not-exist") == tmp_path / "domains"


def test_no_config_path_uses_bundled(tmp_path):
    assert resolve_domains_dir(None, "conversion") == _BUNDLED_DOMAINS
