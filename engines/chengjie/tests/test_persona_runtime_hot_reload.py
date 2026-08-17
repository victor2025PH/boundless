# -*- coding: utf-8 -*-
"""profiles_runtime.yaml 热重载门禁（2026-08-03「改了要等重启」修复）。

背景：PersonaManager 内存态只在启动对齐一次 → ① 双实例下 A 存 B 不知；
② 运维/agent 直改文件永不生效；③ persist 失败后重启还魂无从察觉。
本门禁钉住 maybe_reload_runtime_profiles 的四条语义：
外部改动能进来 / 自身 persist 不自我重载 / 节流真的在节流 / 文件缺席不崩。
全部走 tmp_path，绝不碰仓库 config/。
"""
import os
import time
import types

import yaml

from src.utils.persona_manager import PersonaManager


def _write_runtime(path, profiles):
    path.write_text(
        yaml.safe_dump({"profiles": profiles}, allow_unicode=True),
        encoding="utf-8")
    # Windows mtime 粒度 ~15ms，连续写可能同刻度；显式推后 mtime 保 sig 必变
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 2))


def _fresh_pm(tmp_path, profiles):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("persona_persistence:\n  enabled: true\n", encoding="utf-8")
    rt = tmp_path / "profiles_runtime.yaml"
    _write_runtime(rt, profiles)
    pm = PersonaManager()
    n = pm.load_profiles_runtime(cfg, {"persona_persistence": {"enabled": True}})
    assert n == len(profiles)
    return pm, rt


def test_external_edit_hot_reloads(tmp_path):
    pm, rt = _fresh_pm(tmp_path, {
        "p1": {"id": "p1", "name": "小雨", "context": {"hobbies": ["撸猫"]}},
    })
    assert pm.get_persona_by_id("p1")["context"]["hobbies"] == ["撸猫"]
    # 模拟外部落盘（另一实例 persist / agent 直改）：删掉爱好
    _write_runtime(rt, {
        "p1": {"id": "p1", "name": "小雨", "context": {"hobbies": []}},
    })
    pm._runtime_reload_next = 0.0            # 绕过节流窗（测试不等 3s）
    assert pm.maybe_reload_runtime_profiles(min_interval=0) is True
    assert pm.get_persona_by_id("p1")["context"]["hobbies"] == []


def test_read_path_triggers_reload(tmp_path):
    """get_persona_by_id / get_persona_with_tier 自带钩子——调用方零改动获益。"""
    pm, rt = _fresh_pm(tmp_path, {"p1": {"id": "p1", "name": "旧名"}})
    _write_runtime(rt, {"p1": {"id": "p1", "name": "新名"}})
    pm._runtime_reload_next = 0.0
    assert pm.get_persona_by_id("p1")["name"] == "新名"
    _write_runtime(rt, {"p1": {"id": "p1", "name": "更新名"}})
    pm._runtime_reload_next = 0.0
    p, _tier = pm.get_persona_with_tier(account_persona_id="p1")
    assert p["name"] == "更新名"


def test_self_persist_does_not_self_reload(tmp_path):
    """自己 persist 落盘后刷新签名——下一次 maybe_reload 不应触发重载。"""
    pm, rt = _fresh_pm(tmp_path, {"p1": {"id": "p1", "name": "小雨"}})
    cm = types.SimpleNamespace(
        config_path=str(tmp_path / "config.yaml"),
        config={"persona_persistence": {"enabled": True}},
    )
    pm.upsert_profile("p1", {"id": "p1", "name": "改过的小雨"})
    assert pm.persist_profiles(cm) is True
    pm._runtime_reload_next = 0.0
    assert pm.maybe_reload_runtime_profiles(min_interval=0) is False
    assert pm.get_persona_by_id("p1")["name"] == "改过的小雨"


def test_throttle_really_throttles(tmp_path):
    pm, rt = _fresh_pm(tmp_path, {"p1": {"id": "p1", "name": "小雨"}})
    pm._runtime_reload_next = 0.0
    pm.maybe_reload_runtime_profiles(min_interval=60)   # 拉起 60s 节流窗
    _write_runtime(rt, {"p1": {"id": "p1", "name": "变了"}})
    # 节流窗内：即使文件变了也不 stat/不重载
    assert pm.maybe_reload_runtime_profiles(min_interval=60) is False
    assert pm.get_persona_by_id("p1")["name"] == "小雨"


def test_missing_file_never_crashes(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("x: 1\n", encoding="utf-8")
    pm = PersonaManager()
    pm.load_profiles_runtime(cfg, {})        # 文件不存在：登记路径、载入 0 条
    pm._runtime_reload_next = 0.0
    assert pm.maybe_reload_runtime_profiles(min_interval=0) is False
    # 文件事后被创建 → 能被热加载进来
    _write_runtime(tmp_path / "profiles_runtime.yaml",
                   {"p9": {"id": "p9", "name": "后来的"}})
    pm._runtime_reload_next = 0.0
    assert pm.maybe_reload_runtime_profiles(min_interval=0) is True
    assert pm.get_persona_by_id("p9")["name"] == "后来的"


def test_never_loaded_is_inert():
    """从未 load 过 runtime（纯测试/CLI 构造）的实例：监视关闭，读取零开销零副作用。"""
    pm = PersonaManager()
    assert pm.maybe_reload_runtime_profiles(min_interval=0) is False
    assert pm.get_persona_by_id("nope") is None
