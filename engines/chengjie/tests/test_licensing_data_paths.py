"""授权/额度数据落盘位置门禁。

守的不变量：**授权与用量必须跟着可写数据区走**，不能落在代码目录。

打包（PyInstaller onedir + electron-builder）后，代码目录 = 安装目录：
  · per-user 安装可写，但每次升级整目录被替换 → 授权要重新激活、字符用量归零
    （用量归零＝白送额度，属收入侧漏洞）；
  · 装 Program Files / 企业分发时只读 → 激活直接失败。
桌面壳 launcher 会注入 AITR_DATA_DIR + AITR_CONFIG_PATH，本门禁钉住「注了就必须用」。
"""
from __future__ import annotations

import os
from pathlib import Path

from src.licensing import data_paths


def _fresh(monkeypatch, **env):
    for k in ("AITR_CONFIG_PATH", "AITR_DATA_DIR"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_config_path_env_wins(monkeypatch, tmp_path):
    cfg = tmp_path / "data" / "config" / "config.yaml"
    _fresh(monkeypatch, AITR_CONFIG_PATH=str(cfg), AITR_DATA_DIR=str(tmp_path / "other"))
    assert data_paths.config_dir() == cfg.parent, "AITR_CONFIG_PATH 优先级最高"


def test_data_dir_env_used(monkeypatch, tmp_path):
    _fresh(monkeypatch, AITR_DATA_DIR=str(tmp_path / "root"))
    assert data_paths.config_dir() == tmp_path / "root" / "config"


def test_falls_back_to_repo_config(monkeypatch):
    _fresh(monkeypatch)
    d = data_paths.config_dir()
    assert d.name == "config"
    assert (d.parent / "src" / "licensing").is_dir(), "无 env 时应回落引擎根 config/"


def test_license_and_quota_follow_data_dir(monkeypatch, tmp_path):
    """核心断言：两个文件都必须落进注入的可写数据根。"""
    from src.licensing.license_manager import _default_license_path
    from src.licensing.quota_store import _default_db_path

    root = tmp_path / "userdata"
    _fresh(monkeypatch, AITR_DATA_DIR=str(root))
    lic = Path(_default_license_path())
    qdb = Path(_default_db_path())
    assert lic == root / "config" / "license.key"
    assert qdb == root / "config" / "license_quota.db"
    # 反向：绝不能还指着代码目录（那就是升级即丢的老行为）
    code_cfg = Path(__file__).resolve().parent.parent / "config"
    assert lic.parent != code_cfg and qdb.parent != code_cfg


def test_trial_claim_state_follows_license(monkeypatch, tmp_path):
    """领取进度与授权同目录——否则「换了数据区就重复领一份」。"""
    from src.licensing import trial_claim_client as tc
    from src.licensing.license_manager import LicenseManager

    root = tmp_path / "ud"
    _fresh(monkeypatch, AITR_DATA_DIR=str(root))
    import src.licensing as lic_pkg
    mgr = LicenseManager(license_path=str(root / "config" / "license.key"))
    monkeypatch.setattr(lic_pkg, "get_license_manager", lambda *a, **k: mgr)
    assert tc.state_path() == root / "config" / "trial_claim.json"


def test_split_config_outside_data_root(monkeypatch, tmp_path):
    """VPS：YAML 在 /etc，AITR_DATA_DIR 在 /var/lib。config_dir 不动，运行时库离开 /etc。"""
    etc = tmp_path / "etc" / "chatx-fleet"
    data = tmp_path / "var" / "lib" / "chatx-fleet"
    cfg = etc / "config.yaml"
    _fresh(monkeypatch, AITR_CONFIG_PATH=str(cfg), AITR_DATA_DIR=str(data))
    assert data_paths.config_dir() == etc
    assert data_paths.data_dir() == data
    assert data_paths.config_dir() != data_paths.data_dir()
    for name in (
        "cost_ledger.db", "license_quota.db", "token_ledger.db",
        "agent_char_usage.db", "license.key",
    ):
        p = Path(data_paths.data_file(name))
        assert p == data / name
        assert etc not in p.parents
    for name in (
        "web_users.db", "audit.db", "knowledge_base.db", "runtime_flags.db",
        "bot.db", "strategy_events.db", "inbox.db",
    ):
        p = data_paths.runtime_file(name, cfg)
        assert p == data / name
    assert data_paths.plugin_dir(cfg) == data / "plugins"
    assert data_paths.runtime_dir(None) == data


def test_other_config_path_is_not_relocated(monkeypatch, tmp_path):
    """进程级 AITR_DATA_DIR 不得把 pytest / --init 的另一份配置的库收走。"""
    etc = tmp_path / "etc" / "chatx-fleet"
    data = tmp_path / "var" / "lib" / "chatx-fleet"
    cfg = etc / "config.yaml"
    other = tmp_path / "case" / "config.yaml"
    _fresh(monkeypatch, AITR_CONFIG_PATH=str(cfg), AITR_DATA_DIR=str(data))
    assert data_paths.runtime_dir(other) == other.parent
    assert data_paths.runtime_file("web_users.db", other) == other.parent / "web_users.db"
    assert data_paths.plugin_dir(other) == other.parent.parent / "plugins"


def test_colocated_yaml_stays_inside_data_root(monkeypatch, tmp_path):
    """桌面：AITR_CONFIG_PATH 在 AITR_DATA_DIR/config 内，库路径与改前一致。"""
    root = tmp_path / "userdata"
    cfg = root / "config" / "config.yaml"
    _fresh(monkeypatch, AITR_DATA_DIR=str(root), AITR_CONFIG_PATH=str(cfg))
    assert data_paths.config_dir() == cfg.parent
    assert data_paths.data_dir() == cfg.parent
    assert Path(data_paths.data_file("license_quota.db")) == root / "config" / "license_quota.db"
    assert Path(data_paths.data_file("license.key")) == root / "config" / "license.key"
    assert data_paths.runtime_dir(cfg) == cfg.parent
    assert data_paths.plugin_dir(cfg) == root / "plugins"


def test_fleet_empty_db_path_follows_data_root(monkeypatch, tmp_path):
    from src.fleet.store import get_store, set_store

    etc = tmp_path / "etc" / "chatx-fleet"
    data = tmp_path / "var" / "lib" / "chatx-fleet"
    cfg = etc / "config.yaml"
    _fresh(monkeypatch, AITR_CONFIG_PATH=str(cfg), AITR_DATA_DIR=str(data))

    class _Cfg:
        config_path = cfg
        config = {"fleet_control": {"db_path": ""}}

    set_store(None)
    try:
        st = get_store(_Cfg())
        assert st is not None
        assert Path(st.db_path) == data / "fleet_control.db"
        assert etc not in Path(st.db_path).parents
    finally:
        set_store(None)

    class _Abs:
        config_path = cfg
        config = {"fleet_control": {"db_path": str(data / "fleet.db")}}

    set_store(None)
    try:
        st = get_store(_Abs())
        assert Path(st.db_path) == data / "fleet.db"
    finally:
        set_store(None)


def test_no_module_computes_config_dir_from_dunder_file():
    """回归钉：这两个模块不得再用 __file__ 往上推 config 目录。

    2026-07-27 就是这么踩的——两处各自 `dirname×3 + "config"`，打包后指向安装目录。
    统一走 data_paths 后，若有人复制粘贴老写法回来，这条会红。
    """
    base = Path(__file__).resolve().parent.parent / "src" / "licensing"
    for name in ("license_manager.py", "quota_store.py"):
        src = (base / name).read_text(encoding="utf-8")
        for fn in ("_default_license_path", "_default_db_path"):
            i = src.find(f"def {fn}(")
            if i < 0:
                continue
            body = src[i:i + 400]
            # 只抓真正的取路径写法（注释里提到 __file__ 是在解释这段历史，不算违规）
            assert "abspath(__file__)" not in body, \
                f"{name}::{fn} 不应再从 __file__ 推 config 目录"
            assert "data_file" in body, f"{name}::{fn} 应走 data_paths.data_file"
