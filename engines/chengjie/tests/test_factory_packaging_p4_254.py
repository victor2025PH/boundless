# -*- coding: utf-8 -*-
"""P-4 C #254（MTRCH2 ①③，2026-09-08）：打包清单两张 + handoff 两 yaml 进包 + fatex.db 出包。

skuio MTRCH2（1.0.77 clean 装）实录：
  ③ 「HandoffRenderer init skipped: scripts file not found: …data\\config\\handoff_scripts.yaml」
     「HandoffComplianceChecker init skipped」——两份 yaml 压根没在 build_backend.py DATAS 里，
     且 contacts/bootstrap 只在用户数据区找，转人工整条链在客户机上哑；
  ① 「✅ FateX 幻缘 产品库就绪（fatex.db）」——不是随包打进来，而是 main._init_fatex_store
     无条件建库；命理产品库只随 FateX 产品线走，用户版陪伴机不该有。

钉住：
- build_backend.py DATAS 登记 handoff_scripts.yaml / handoff_compliance.yaml，仓库源文件在；
- contacts/bootstrap 用户数据区缺 yaml → 回落随包 `<_internal>/config/`（开发态＝仓库 config/）；
  数据区有同名文件则优先（用户可覆盖话术）；
- fatex.enabled=false → 不建库且 get_fatex_store() 恒 None（懒建也封死）；enabled 才建；
- after-pack.js FORBIDDEN 点名 fatex.db / license.key / config.yaml / credpool；REQUIRED 点名
  handoff 两 yaml + config.desktop.min + domains manifest；package-layout.test.js 有两张清单。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.contacts import bootstrap as cb
from src.fatex import store as fx

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_fatex():
    fx.reset_fatex_store_for_tests()
    yield
    fx.reset_fatex_store_for_tests()


# ── handoff 两 yaml：进包 + 回落 ────────────────────────────────────────────

def test_handoff_yaml_registered_in_build_datas_and_present_in_repo():
    src = (REPO / "desktop" / "build" / "build_backend.py").read_text(encoding="utf-8")
    datas = re.search(r"DATAS\s*=\s*\[(.*?)\n\]", src, flags=re.S).group(1)
    assert '(REPO / "config" / "handoff_scripts.yaml", "config")' in datas
    assert '(REPO / "config" / "handoff_compliance.yaml", "config")' in datas
    assert (REPO / "config" / "handoff_scripts.yaml").is_file()
    assert (REPO / "config" / "handoff_compliance.yaml").is_file()
    # 「必须不在」：DATAS 里不许出现 .db / .key / 真实配置
    assert ".db\"" not in datas and ".key\"" not in datas
    assert '"config.yaml"' not in datas and '"config.local.yaml"' not in datas


def test_bootstrap_falls_back_to_bundled_yaml_when_data_dir_lacks_it(tmp_path):
    cfg_dir = tmp_path / "data" / "config"
    cfg_dir.mkdir(parents=True)
    p = cb._resolve_contacts_yaml(cfg_dir, "config/handoff_scripts.yaml")
    assert p.is_file()
    assert p.resolve() == (REPO / "config" / "handoff_scripts.yaml").resolve()
    assert cb._bundled_config_fallback("handoff_compliance.yaml").is_file()
    assert cb._bundled_config_fallback("nope.yaml") is None


def test_bootstrap_prefers_user_data_dir_copy(tmp_path):
    cfg_dir = tmp_path / "data" / "config"
    cfg_dir.mkdir(parents=True)
    mine = cfg_dir / "handoff_scripts.yaml"
    mine.write_text("scripts: []\n", encoding="utf-8")
    p = cb._resolve_contacts_yaml(cfg_dir, "config/handoff_scripts.yaml")
    assert p.resolve() == mine.resolve()


def test_bootstrap_initialises_renderer_and_compliance_from_bundled_yaml(tmp_path):
    cfg_dir = tmp_path / "data" / "config"
    cfg_dir.mkdir(parents=True)
    r = cb._safe_init_renderer(cfg_dir, {})
    c = cb._safe_init_compliance(cfg_dir, {})
    assert r is not None and c is not None        # 装配起来 → 启动日志出现「handoff_scripts loaded: N scripts」


# ── fatex.db 出包（不再凭空建库）─────────────────────────────────────────────

def test_main_gates_fatex_store_on_product_switch():
    src = (REPO / "main.py").read_text(encoding="utf-8")
    body = src.split("def _init_fatex_store", 1)[1].split("\n    def ", 1)[0]
    assert "fatex_enabled(self.config)" in body
    assert "disable_fatex_store()" in body
    assert "configure_fatex_store(_cfg_dir / \"fatex.db\")" in body   # 开着仍照旧建


def test_disable_blocks_lazy_creation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                    # DEFAULT_DB_PATH 相对 cwd
    fx.disable_fatex_store()
    assert fx.get_fatex_store() is None
    assert not (tmp_path / "config" / "fatex.db").exists()
    fx.reset_fatex_store_for_tests()
    (tmp_path / "config").mkdir()
    assert fx.get_fatex_store() is not None       # 未 disable：懒建照旧（FateX 产品线）
    assert (tmp_path / "config" / "fatex.db").exists()


def test_desktop_min_config_does_not_enable_fatex():
    import yaml
    from src.fatex.config import fatex_enabled
    cfg = yaml.safe_load((REPO / "config" / "config.desktop.min.yaml").read_text(encoding="utf-8"))
    assert fatex_enabled(cfg) is False


# ── 打包门禁两张清单 ─────────────────────────────────────────────────────────

def test_after_pack_lists_and_package_layout_gate():
    ap = (REPO / "desktop" / "build" / "after-pack.js").read_text(encoding="utf-8")
    required = ap.split("const REQUIRED = [", 1)[1].split("\n];", 1)[0]
    for needle in ('"config", "config.desktop.min.yaml"', '"config", "handoff_scripts.yaml"',
                   '"config", "handoff_compliance.yaml"', '"domains", "conversion", "manifest.yaml"'):
        assert needle in required, needle
    forbidden = ap.split("const FORBIDDEN = [", 1)[1].split("\n];", 1)[0]
    for needle in ('"config", "fatex.db"', '"config", "license.key"', '"config", "config.yaml"',
                   '"config", "config.local.yaml"', '"credpool", "data"', '"knowledge_base.db"'):
        assert needle in forbidden, needle
    assert "exports.FORBIDDEN = FORBIDDEN;" in ap
    gate = (REPO / "desktop" / "test" / "package-layout.test.js").read_text(encoding="utf-8")
    assert "const MUST_SHIP = [" in gate and "const MUST_NOT_SHIP_DATAS = [" in gate
    assert "afterPack.FORBIDDEN" in gate
    assert "PRIVATE_IP" in gate                    # 随包 YAML 零内网地址
