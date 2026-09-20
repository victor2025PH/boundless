"""可选配置文件缺失时的降级门禁（安装版 dashboard 500 事故回归）。

事故（0.2.1 安装版，173 实测）：点「管理后台」必 500，堆栈落在
``ConfigManager.get_dynamic_templates_config`` ——

    getattr(templates_file, 'as_posix', str)(templates_file)

``getattr`` 取到的是**已绑定**方法（零参），再传一个实参进去必然
``TypeError: as_posix() takes 1 positional argument but 2 were given``。
这行防御性日志「只要走到就一定崩」，而它只在可选 YAML 缺失时才走到：
源码树里 config/*.yaml 都在 → 永不触发；安装版没随包 → 每次必崩。

本门禁两条腿：
1. 真实调用三个 getter 并让「按包路径回退」也落空（模拟安装版），断言只降级不抛。
2. 全库棘轮：禁止 ``getattr(x, 'as_posix', str)(x)`` 这一族写法复活。
"""
from __future__ import annotations

import pathlib
import re

import pytest

from src.utils import config_manager as cm_mod
from src.utils.config_manager import ConfigManager, _path_str

_ENGINE_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture()
def frozen_like_cm(tmp_path, monkeypatch):
    """配置目录与「包路径回退」双双落空的 ConfigManager —— 即安装版实况。"""
    cfg = tmp_path / "config" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("web_admin: {}\n", encoding="utf-8")
    manager = ConfigManager(str(cfg))

    # 回退分支用模块级 Path(__file__) 推包根；指到一个不含 config/ 的临时树，
    # 使 templates/exchange_rates/quota 三个可选文件都找不到。
    fake_module_file = tmp_path / "frozen" / "src" / "utils" / "config_manager.py"
    fake_module_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cm_mod, "Path", lambda *_a, **_k: pathlib.Path(fake_module_file))
    return manager


def test_missing_optional_yaml_degrades_without_raising(frozen_like_cm):
    """三个可选配置缺失 → 返回空 dict，绝不抛（抛出去就是 dashboard 500）。"""
    assert frozen_like_cm.get_dynamic_templates_config() == {}
    assert frozen_like_cm.get_exchange_rates_config() == {}
    assert frozen_like_cm.get_quota_rules() == {}


def test_path_str_handles_path_and_non_path():
    assert _path_str(pathlib.PurePosixPath("/a/b.yaml")) == "/a/b.yaml"
    assert _path_str(pathlib.PureWindowsPath(r"C:\a\b.yaml")) == "C:/a/b.yaml"
    assert _path_str("plain/string.yaml") == "plain/string.yaml"
    assert _path_str(None) == "None"


def test_bound_method_default_antipattern_never_returns():
    """把事故写法本身钉成用例：它不是「偶尔失败」，是必然 TypeError。"""
    p = pathlib.PurePosixPath("/a/b.yaml")
    with pytest.raises(TypeError):
        getattr(p, "as_posix", str)(p)


def test_no_bound_method_getattr_default_in_src():
    """棘轮：``getattr(x, 'method', str)(x)`` 这族写法禁止再出现在 src/。"""
    pattern = re.compile(r"getattr\([^,()]+,\s*['\"][a-z_]+['\"],\s*str\)\s*\(")
    offenders = []
    for py in sorted((_ENGINE_ROOT / "src").rglob("*.py")):
        text = py.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{py.relative_to(_ENGINE_ROOT).as_posix()}:{i}")
    assert not offenders, (
        "getattr(obj, 'method', str)(obj) 取到的是已绑定方法，多传一个实参必 TypeError。"
        "改用不会抛的辅助函数（见 config_manager._path_str）。命中：\n  "
        + "\n  ".join(offenders)
    )
