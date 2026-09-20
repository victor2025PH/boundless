"""global_rules.yaml overlay 落盘门禁（P7-1，2026-07-29）。

背景：旧实现读写同一个 ``<repo>/config/global_rules.yaml``——那是**共享代码根**
（`start_zhiliao.ps1` 明写「代码：共享，只读；改代码走 git」）且被 git 跟踪：
运营每次在 UI 改全局规则都把仓库改脏（可能撞 restart_instance 的脏树闸门）、
打包态那是只读安装目录根本存不下，也是「测试写到生产配置」那类事故的土壤。

新语义（与 ``config.yaml`` + ``config.local.yaml`` 的 overlay 约定同构）：
  读：可写数据区那份存在就用它，否则回落仓库那份（**出厂默认**）
  写：只写可写数据区；首次保存把出厂默认迁过去并进 ``.bak.1``（可「恢复出厂」）

本文件钉住的不变量：
  1. **安全底线**：读永远有回落，最坏是「读到出厂默认」，**绝不读成空**
     （空 = 所有人设丢掉硬约束，含「不要自称AI」这类安全项）。
  2. **首次保存后读路径必须翻到数据区**——旧实现把自动解析结果缓存进
     ``_global_rules_path``，会造成「运营改了却读不到」（本次重构的头号风险点）。
  3. 写**绝不**落仓库出厂文件（无论保存多少次）。
  4. 备份轮转跟着写入落点；``.bak.1`` 首存后 = 出厂默认。
  5. 未设 ``AITR_DATA_DIR`` 时行为等价于旧版（读写都在仓内 config），零破坏。
  6. 显式覆写 ``_global_rules_path``（测试隔离用）同时钉住读与写。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.persona_manager import (  # noqa: E402
    GLOBAL_RULES_FILENAME,
    PersonaManager,
)

# 与产品侧 _global_rules_factory_path 同口径 resolve：本机 D:\boundless 是指向
# D:\workspace\boundless 的目录联接，两边解析口径不一致会让断言在同一物理文件上误判
# （实测抓到过 SameFileError 级真 bug，见 persona_manager._global_rules_data_path 注释）。
_FACTORY = (Path(__file__).resolve().parent.parent / "config"
            / GLOBAL_RULES_FILENAME).resolve()


def _rules(tag: str) -> dict:
    """真实 schema（{id,enabled,title,rule}）的最小规则集。"""
    return {"reply_constraints": [
        {"id": tag, "enabled": True, "title": f"标题-{tag}", "rule": f"规则-{tag}"},
    ]}


@pytest.fixture
def pm_on_data_root(tmp_path, monkeypatch):
    """把可写数据区指到 tmp，并清掉 conftest 的显式覆写（本文件要测自动解析）。"""
    data_root = tmp_path / "dataroot"
    (data_root / "config").mkdir(parents=True)
    monkeypatch.setenv("AITR_DATA_DIR", str(data_root))
    monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)

    PersonaManager.reset()
    pm = PersonaManager.get_instance()
    pm._global_rules_path = None          # noqa: SLF001 — 本文件专测自动解析链
    pm._global_rules = None
    pm._global_rules_sig = ("", 0.0, -1)
    yield pm, data_root / "config" / GLOBAL_RULES_FILENAME
    PersonaManager.reset()


# ── 1/2. 读优先级 + 首次保存后读路径翻转（头号风险点）────────────────


def test_reads_factory_default_before_any_save(pm_on_data_root):
    """数据区还没有那份 → 读出厂默认（**绝不读成空**，安全底线）。"""
    pm, data_file = pm_on_data_root
    assert not data_file.exists()
    assert pm.global_rules_read_path() == _FACTORY
    rules = pm.get_global_rules()
    # 出厂默认必须有内容——真读到了仓库那份
    assert rules.get("reply_constraints"), "读回落失效＝所有人设丢硬约束，安全红线"
    src = pm.global_rules_source()
    assert src["is_instance_override"] is False
    assert src["write_path"] == str(data_file)


def test_first_save_migrates_and_read_flips_to_data_root(pm_on_data_root):
    """首次保存 → 写数据区；**随后读必须翻到数据区**（旧实现缓存路径会读不到新值）。"""
    pm, data_file = pm_on_data_root
    factory_before = _FACTORY.read_bytes()

    pm.get_global_rules()                      # 先读一次出厂默认（触发旧实现的路径缓存）
    assert pm.save_global_rules(_rules("saved")) is True

    assert data_file.exists(), "保存必须落可写数据区"
    assert pm.global_rules_read_path() == data_file, "读路径必须翻到数据区那份"
    got = pm.get_global_rules()
    assert got["reply_constraints"][0]["id"] == "saved", "运营改了却读不到＝本次重构头号风险"
    assert pm.global_rules_source()["is_instance_override"] is True

    # 出厂文件一字未动
    assert _FACTORY.read_bytes() == factory_before


def test_repeated_saves_never_touch_factory_file(pm_on_data_root):
    """反复保存都不许碰仓库出厂文件（那是共享只读代码根 + git 跟踪）。"""
    pm, data_file = pm_on_data_root
    before = _FACTORY.read_bytes()
    for i in range(3):
        assert pm.save_global_rules(_rules(f"round{i}")) is True
    assert _FACTORY.read_bytes() == before
    assert yaml.safe_load(data_file.read_text(encoding="utf-8"))[
        "reply_constraints"][0]["id"] == "round2"
    # 出厂目录里不该冒出任何 .bak
    assert not list(_FACTORY.parent.glob(f"{GLOBAL_RULES_FILENAME}.bak*"))


# ── 4. 备份轮转跟着写入落点；.bak.1 首存后 = 出厂默认 ──────────────


def test_backups_land_next_to_write_target_and_seed_factory(pm_on_data_root):
    pm, data_file = pm_on_data_root
    assert pm.list_backups() == [], "全新实例没保存过 → 无备份"

    pm.save_global_rules(_rules("first"))
    baks = pm.list_backups()
    assert baks, "首次保存应产生 .bak.1（内容=出厂默认，供「恢复出厂」）"
    bak1 = Path(baks[0]["path"])
    assert bak1.parent == data_file.parent, "备份必须与写入落点同目录"
    assert bak1.read_bytes() == _FACTORY.read_bytes(), ".bak.1 应是出厂默认"

    # 第二次保存 → .bak.1 变成 first，出厂默认顺延到 .bak.2
    pm.save_global_rules(_rules("second"))
    slots = {b["slot"]: Path(b["path"]) for b in pm.list_backups()}
    assert yaml.safe_load(slots[1].read_text(encoding="utf-8"))[
        "reply_constraints"][0]["id"] == "first"
    assert slots[2].read_bytes() == _FACTORY.read_bytes()

    # restore 走同一落点：恢复 slot 2 → 回到出厂默认
    assert pm.restore_backup(2) is True
    assert pm.get_global_rules()["reply_constraints"] == yaml.safe_load(
        _FACTORY.read_text(encoding="utf-8"))["reply_constraints"]


# ── 5. 未设 AITR_DATA_DIR → 旧行为（读写都在仓内），零破坏 ──────────


def test_without_data_dir_falls_back_to_repo_paths(monkeypatch, tmp_path):
    """开发/裸跑场景：无 AITR_DATA_DIR / AITR_CONFIG_PATH → 落点回到仓内 config。

    只断言**路径解析**等价旧行为，绝不真写（写了就是污染生产配置）。
    """
    monkeypatch.delenv("AITR_DATA_DIR", raising=False)
    monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)
    PersonaManager.reset()
    try:
        pm = PersonaManager.get_instance()
        pm._global_rules_path = None       # noqa: SLF001
        assert pm.global_rules_write_path() == _FACTORY
        assert pm.global_rules_read_path() == _FACTORY
        # 读仍拿到出厂内容（等价旧版）
        assert pm.get_global_rules().get("reply_constraints")
    finally:
        PersonaManager.reset()


def test_save_succeeds_when_data_path_equals_factory_path(monkeypatch, tmp_path):
    """联接/无 DATA_DIR 场景：写入落点与出厂文件是**同一物理文件**时保存仍须成功。

    回归 2026-07-29 实施中抓到的真 bug：``D:\\boundless`` 是指向
    ``D:\\workspace\\boundless`` 的目录联接，出厂路径用 ``resolve()``、data_paths 用
    ``abspath()`` → 同一文件被算成两个路径 → 首存的种子拷贝 ``shutil.copy2(同一文件)``
    抛 ``SameFileError`` → save 静默返回 False（运营点保存没反应）。

    这里用 tmp 冒充「仓内 config」：把 AITR_DATA_DIR 指到 tmp，并把出厂路径也 monkeypatch
    到同一个 tmp 文件，复现「两者同文件」的形状——**不碰真仓库文件**。
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    target = cfg / GLOBAL_RULES_FILENAME
    target.write_text(yaml.dump(_rules("factory"), allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)

    PersonaManager.reset()
    try:
        pm = PersonaManager.get_instance()
        pm._global_rules_path = None       # noqa: SLF001
        pm._global_rules = None
        pm._global_rules_sig = ("", 0.0, -1)
        # 让「出厂默认」也指向同一个 tmp 文件（复现同文件形状）
        monkeypatch.setattr(pm, "_global_rules_factory_path", lambda: target)
        assert pm.global_rules_write_path() == target

        assert pm.save_global_rules(_rules("newval")) is True, (
            "同文件场景保存必须成功（旧 bug 会因 SameFileError 返回 False）")
        assert yaml.safe_load(target.read_text(encoding="utf-8"))[
            "reply_constraints"][0]["id"] == "newval"
    finally:
        PersonaManager.reset()


def test_config_path_env_wins_over_data_dir(monkeypatch, tmp_path):
    """``AITR_CONFIG_PATH`` 的父目录优先（与 licensing.data_paths 唯一事实源一致）。"""
    cfg_dir = tmp_path / "explicit"
    cfg_dir.mkdir()
    monkeypatch.setenv("AITR_CONFIG_PATH", str(cfg_dir / "config.yaml"))
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path / "ignored"))
    PersonaManager.reset()
    try:
        pm = PersonaManager.get_instance()
        pm._global_rules_path = None       # noqa: SLF001
        assert pm.global_rules_write_path() == cfg_dir / GLOBAL_RULES_FILENAME
    finally:
        PersonaManager.reset()


# ── 6. 显式覆写同时钉住读与写（测试隔离依赖这条）────────────────────


def test_explicit_override_pins_both_read_and_write(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path / "dataroot"))
    PersonaManager.reset()
    try:
        pm = PersonaManager.get_instance()
        pinned = tmp_path / "pinned_rules.yaml"
        pm._global_rules_path = pinned     # noqa: SLF001
        pm._global_rules = None
        pm._global_rules_sig = ("", 0.0, -1)
        assert pm.global_rules_write_path() == pinned
        assert pm.global_rules_read_path() == pinned    # 显式覆写下读=写，不回落
        assert pm.save_global_rules(_rules("pinned")) is True
        assert pinned.exists()
        assert pm.get_global_rules()["reply_constraints"][0]["id"] == "pinned"
        # 数据区与出厂都没被写
        assert not (tmp_path / "dataroot" / "config" / GLOBAL_RULES_FILENAME).exists()
    finally:
        PersonaManager.reset()


def test_read_never_returns_empty_when_nothing_exists(tmp_path, monkeypatch):
    """极端场景：显式覆写指向不存在的文件 → 返回空 dict 但**不抛**，调用方照常降级。

    注意这是「显式指定了一个空落点」的语义（测试常用），与生产的自动解析链无关——
    生产链永远有出厂默认兜底（见 test_reads_factory_default_before_any_save）。
    """
    PersonaManager.reset()
    try:
        pm = PersonaManager.get_instance()
        pm._global_rules_path = tmp_path / "nope.yaml"   # noqa: SLF001
        pm._global_rules = None
        pm._global_rules_sig = ("", 0.0, -1)
        assert pm.get_global_rules() == {}
    finally:
        PersonaManager.reset()
