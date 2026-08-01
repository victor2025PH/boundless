"""配置重复键检测器门禁（2026-07-31，一次真实事故后补）。

**事故**：往实例 overlay 的 `platform_login:` 下新插了一个 `telegram:` 块，没注意到
那里已经有一个。PyYAML 对重复键**不报错、不警告、静默取最后一个** → 原块整段被丢弃
（`protocol_enabled` / `companion_runtime` / credpool 令牌 / 设备指纹全没了）→ 重启后
不再注册 Telegram worker → 5 个 TG 号离线约 2 分钟。

这类错误完全无声：YAML 解析成功、服务照常启动、日志无异常，只是少了一整块配置。
所以检测器本身必须可信——它误报会让人不再信它，漏报等于没有。
"""

import importlib.util
from pathlib import Path

import pytest

from src.utils.config_check import ERROR, check_config
from src.utils.yaml_duplicates import find_duplicate_keys

ENGINE_ROOT = Path(__file__).resolve().parents[1]


def _mod():
    """CLI 模块（判据本身在 src.utils.yaml_duplicates，这里只测命令行契约）。"""
    p = ENGINE_ROOT / "tools" / "check_config_duplicates.py"
    spec = importlib.util.spec_from_file_location("_ccd", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


def test_single_source_of_truth():
    """三个消费口（CLI / 重启闸门 / 启动自检）必须共用同一个判据函数。

    各写一份的下场是「手查说没问题、启动自检说有问题」——那比没有检查更糟。
    """
    assert _mod().find_duplicate_keys is find_duplicate_keys


def test_detects_nested_duplicate_with_full_path():
    """事故的原型：同一层出现两个同名子块 → 报出完整路径。"""
    dups = find_duplicate_keys(
        "platform_login:\n"
        "  telegram:\n"
        "    protocol_enabled: true\n"
        "  whatsapp:\n"
        "    protocol_enabled: true\n"
        "  telegram:\n"
        "    companion_media: true\n"
    )
    assert dups == ["platform_login.telegram"]


def test_detects_top_level_duplicate():
    dups = find_duplicate_keys(
        "companion:\n  enabled: true\nother: 1\ncompanion:\n  goals: {}\n")
    assert dups == ["companion"]


def test_clean_config_reports_nothing():
    """同名键在**不同层**不算重复——误报会让人不再信这个检查。"""
    assert find_duplicate_keys(
        "a:\n  x: 1\n  y:\n    x: 2\nb:\n  x: 3\n") == []
    assert find_duplicate_keys("") == []
    assert find_duplicate_keys("a: 1\nb: 2\n") == []


def test_list_items_with_same_keys_are_not_duplicates():
    """列表里每个元素各自成 mapping，同名键属正常（真实配置里到处都是）。"""
    assert find_duplicate_keys(
        "hosts:\n  - name: a\n    url: u1\n  - name: b\n    url: u2\n") == []


def test_parse_error_is_surfaced_not_swallowed():
    """解析不了是另一类问题，但同样不该静默放过。"""
    assert find_duplicate_keys("a:\n  - [unclosed\n") == ["<parse-error>"]


def test_cli_scopes_to_data_root_and_signals_by_exit_code(tmp_path, monkeypatch,
                                                          capsys):
    """``--data-root`` 定向 + 退出码——**重启前置闸门就是靠这两样判断的**。

    闸门只认 rc：1=有重复必须拦，0=放行，其它=工具跑不起来（放行，护栏不能比它
    防的东西更危险）。所以这两条契约要钉死。
    """
    m = _mod()
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "config.yaml").write_text("a: 1\nb: 2\n", encoding="utf-8")

    monkeypatch.setattr("sys.argv", ["x", "--data-root", str(tmp_path)])
    assert m.main() == 0
    assert "no duplicate keys" in capsys.readouterr().out

    (cfgdir / "config.local.yaml").write_text(
        "platform_login:\n  telegram:\n    protocol_enabled: true\n"
        "  telegram:\n    companion_media: true\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["x", "--data-root", str(tmp_path)])
    assert m.main() == 1
    out = capsys.readouterr().out
    assert "platform_login.telegram" in out
    # 只查指定根：不该把本机其它实例/仓库配置混进来
    assert str(tmp_path) in out


# ─────────────────── 启动自检 / --check 闸门 ───────────────────

def _write(tmp_path, main="ai:\n  provider: x\n", local=None):
    d = tmp_path / "config"
    d.mkdir(exist_ok=True)
    (d / "config.yaml").write_text(main, encoding="utf-8")
    if local is not None:
        (d / "config.local.yaml").write_text(local, encoding="utf-8")
    return d / "config.yaml"


def _dup_issues(issues):
    return [i for i in issues if "不止一次" in i.message]


def test_startup_check_catches_overlay_duplicate(tmp_path):
    """事故现场就是 overlay——运营改开关都落 config.local.yaml。

    重启闸门只覆盖「经 restart_instance.ps1 的重启」；配置热重载、以及任何绕过该
    脚本的启动方式都不经过它。接进启动自检才是全覆盖。
    """
    p = _write(tmp_path, local=(
        "platform_login:\n  telegram:\n    protocol_enabled: true\n"
        "  telegram:\n    companion_media: true\n"))
    got = _dup_issues(check_config({"ai": {"provider": "x"}}, config_path=str(p)))
    assert [i.path for i in got] == ["platform_login.telegram"]
    assert got[0].severity == ERROR, "静默丢配置是错误，不是风格问题"
    assert "整段丢弃" in got[0].hint


def test_startup_check_catches_main_config_duplicate(tmp_path):
    p = _write(tmp_path, main="companion:\n  enabled: true\ncompanion:\n  goals: {}\n")
    got = _dup_issues(check_config({}, config_path=str(p)))
    assert [i.path for i in got] == ["companion"]


def test_startup_check_silent_when_clean_or_unavailable(tmp_path):
    """干净配置零误报；没有 config_path（大量单测就这么调）也不能崩、不能误报。"""
    p = _write(tmp_path, main="a: 1\nb: 2\n", local="c: 3\n")
    assert _dup_issues(check_config({"a": 1}, config_path=str(p))) == []
    assert _dup_issues(check_config({"a": 1})) == []
    # 文件不存在 → 跳过（自检不该因为 IO 抖动误报）
    assert _dup_issues(
        check_config({"a": 1}, config_path=str(tmp_path / "nope" / "config.yaml"))) == []


def test_startup_check_ignores_broken_yaml(tmp_path):
    """YAML 本身坏了是另一类问题（加载阶段已会报），不该混进重复键结论里。"""
    p = _write(tmp_path, local="a:\n  - [unclosed\n")
    assert _dup_issues(check_config({}, config_path=str(p))) == []


def test_live_instance_configs_have_no_duplicates():
    """本机实例配置必须干净——那是真正在跑的那份。

    没有实例部署的机器（CI/开发机）自动跳过。
    """
    from scripts._data_root import discover_instance_roots

    roots = discover_instance_roots()
    if not roots:
        pytest.skip("本机无实例部署")
    for root in roots:
        for name in ("config.yaml", "config.local.yaml"):
            p = Path(root) / "config" / name
            if not p.is_file():
                continue
            dups = find_duplicate_keys(p.read_text(encoding="utf-8"))
            assert not dups, "%s 有重复键 %s（前面同名块被静默丢弃）" % (p, dups)
