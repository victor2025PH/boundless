"""LINE 协议扫码的 Node 运行时解析门禁（2026-07-30 turnkey 化后加）。

背景：okline 不是纯 Python —— 它起一个**持久 Node 子进程**加载 ltsm.wasm 算 X-Hmac
签名，网关对每个请求强制校验。所以「有没有 node」直接决定 LINE 能不能用，而失败发生在
协议层，界面上只表现为「扫了没反应」。

本门禁钉住三组不变量：
1. **解析优先级**：显式配置 > 已设 LINE_NODE > PATH 上真 node > 随包 Electron。
   真 node 优先于 Electron 是刻意的（正统运行时行为最可预期）。
2. **Electron 回落必须同时设 ELECTRON_RUN_AS_NODE** —— 漏了它 Electron 会去开 GUI 窗口
   而不是跑桥，症状是扫码卡死且没有任何有用报错。这是整条 turnkey 链最脆的一环。
3. **就绪诊断如实报缺**：缺 node → `dep_missing`（带可照做的 install 指引），而不是挂着
   绿色「推荐」骗点击，也不是笼统的「未启用」。

刻意**不**在这里真起 Node 桥（那需要真 okline + 真运行时，属集成验证范畴）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.integrations import line_protocol_login as lnl  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每例都从干净 env + 干净缓存起跑（缓存会跨例串味）。"""
    monkeypatch.delenv("LINE_NODE", raising=False)
    monkeypatch.delenv("ELECTRON_RUN_AS_NODE", raising=False)
    monkeypatch.delenv("AITR_ELECTRON_NODE", raising=False)
    lnl.reset_node_runtime_cache()
    yield
    lnl.reset_node_runtime_cache()


def _fake_exe(tmp_path: Path, name: str) -> str:
    p = tmp_path / name
    p.write_text("", encoding="utf-8")
    return str(p)


# ── 解析优先级 ───────────────────────────────────────────────────────────────

def test_explicit_config_wins(tmp_path, monkeypatch):
    cfgnode = _fake_exe(tmp_path, "cfgnode.exe")
    envnode = _fake_exe(tmp_path, "envnode.exe")
    monkeypatch.setenv("LINE_NODE", envnode)
    monkeypatch.setattr(lnl.shutil, "which", lambda n: r"C:\sys\node.exe")
    cfg = {"platform_login": {"line": {"node_path": cfgnode}}}
    path, source = lnl.resolve_node_runtime(cfg)
    assert (path, source) == (cfgnode, "config")


def test_env_line_node_beats_path(tmp_path, monkeypatch):
    envnode = _fake_exe(tmp_path, "envnode.exe")
    monkeypatch.setenv("LINE_NODE", envnode)
    monkeypatch.setattr(lnl.shutil, "which", lambda n: r"C:\sys\node.exe")
    path, source = lnl.resolve_node_runtime({})
    assert (path, source) == (envnode, "env")


def test_real_node_on_path_beats_electron(tmp_path, monkeypatch):
    """正统运行时优先——Electron 只是没装 Node 时的回落。"""
    electron = _fake_exe(tmp_path, "electron.exe")
    monkeypatch.setenv("AITR_ELECTRON_NODE", electron)
    monkeypatch.setattr(lnl.shutil, "which", lambda n: r"C:\sys\node.exe")
    path, source = lnl.resolve_node_runtime({})
    assert (path, source) == (r"C:\sys\node.exe", "path")


def test_electron_used_when_no_system_node(tmp_path, monkeypatch):
    electron = _fake_exe(tmp_path, "electron.exe")
    monkeypatch.setenv("AITR_ELECTRON_NODE", electron)
    monkeypatch.setattr(lnl.shutil, "which", lambda n: None)
    path, source = lnl.resolve_node_runtime({})
    assert (path, source) == (electron, "electron")


def test_nothing_available_resolves_empty(monkeypatch):
    monkeypatch.setattr(lnl.shutil, "which", lambda n: None)
    assert lnl.resolve_node_runtime({}) == ("", "")


def test_bad_candidates_are_skipped_not_fatal(tmp_path, monkeypatch):
    """坏的显式值/坏 LINE_NODE 不能把整条链打死——跳过继续找。"""
    monkeypatch.setenv("LINE_NODE", str(tmp_path / "nope.exe"))
    monkeypatch.setattr(lnl.shutil, "which", lambda n: r"C:\sys\node.exe")
    cfg = {"platform_login": {"line": {"node_path": str(tmp_path / "also-nope.exe")}}}
    path, source = lnl.resolve_node_runtime(cfg)
    assert (path, source) == (r"C:\sys\node.exe", "path")


# ── env 钉入行为 ─────────────────────────────────────────────────────────────

def test_electron_choice_sets_run_as_node_flag(tmp_path, monkeypatch):
    """整条 turnkey 链最脆的一环：漏了这个 flag，Electron 会开 GUI 而不是跑桥。"""
    electron = _fake_exe(tmp_path, "electron.exe")
    monkeypatch.setenv("AITR_ELECTRON_NODE", electron)
    monkeypatch.setattr(lnl.shutil, "which", lambda n: None)
    assert lnl.ensure_node_runtime({}) == electron
    assert os.environ["LINE_NODE"] == electron
    assert os.environ["ELECTRON_RUN_AS_NODE"] == "1"


def test_real_node_does_not_touch_env(monkeypatch):
    """最小干预：PATH 上有真 node 时一个字都不写（okline 默认 "node" 本来就对）。"""
    monkeypatch.setattr(lnl.shutil, "which", lambda n: r"C:\sys\node.exe")
    assert lnl.ensure_node_runtime({}) == r"C:\sys\node.exe"
    assert "LINE_NODE" not in os.environ
    assert "ELECTRON_RUN_AS_NODE" not in os.environ


def test_stale_line_node_is_corrected_by_real_node(tmp_path, monkeypatch):
    """残留的坏 LINE_NODE 会被 okline 优先取用而必败 → 必须纠正掉。"""
    monkeypatch.setenv("LINE_NODE", str(tmp_path / "gone.exe"))
    monkeypatch.setattr(lnl.shutil, "which", lambda n: r"C:\sys\node.exe")
    assert lnl.ensure_node_runtime({}) == r"C:\sys\node.exe"
    assert os.environ["LINE_NODE"] == r"C:\sys\node.exe"


def test_valid_line_node_left_alone(tmp_path, monkeypatch):
    envnode = _fake_exe(tmp_path, "envnode.exe")
    monkeypatch.setenv("LINE_NODE", envnode)
    monkeypatch.setattr(lnl.shutil, "which", lambda n: r"C:\sys\node.exe")
    assert lnl.ensure_node_runtime({}) == envnode
    assert os.environ["LINE_NODE"] == envnode
    assert "ELECTRON_RUN_AS_NODE" not in os.environ


def test_no_runtime_does_not_fabricate_env(monkeypatch):
    monkeypatch.setattr(lnl.shutil, "which", lambda n: None)
    assert lnl.ensure_node_runtime({}) == ""
    assert "LINE_NODE" not in os.environ
    assert "ELECTRON_RUN_AS_NODE" not in os.environ


def test_failure_is_not_cached_so_later_install_is_picked_up(monkeypatch):
    """失败不缓存：事后把 node 装进已在 PATH 的目录，下次探测就该认出来（无需重开应用）。"""
    monkeypatch.setattr(lnl.shutil, "which", lambda n: None)
    assert lnl.ensure_node_runtime({}) == ""
    monkeypatch.setattr(lnl.shutil, "which", lambda n: r"C:\sys\node.exe")
    assert lnl.ensure_node_runtime({}) == r"C:\sys\node.exe"


def test_success_is_cached(tmp_path, monkeypatch):
    """成功后不再重复解析（免重复写 env / 刷日志）。"""
    electron = _fake_exe(tmp_path, "electron.exe")
    monkeypatch.setenv("AITR_ELECTRON_NODE", electron)
    monkeypatch.setattr(lnl.shutil, "which", lambda n: None)
    assert lnl.ensure_node_runtime({}) == electron
    calls = []
    monkeypatch.setattr(lnl.shutil, "which", lambda n: calls.append(n))
    assert lnl.ensure_node_runtime({}) == electron
    assert calls == [], "命中缓存后不该再解析"


# ── 就绪诊断 ─────────────────────────────────────────────────────────────────

def _line_blockers(config):
    from src.integrations.platform_readiness import _line_protocol_blockers
    return _line_protocol_blockers(config)


def test_missing_node_reports_actionable_dep_missing(monkeypatch):
    monkeypatch.setattr(lnl.shutil, "which", lambda n: None)
    monkeypatch.setattr(lnl, "is_okline_available", lambda: True)
    cfg = {"platform_login": {"line": {"protocol_enabled": True}}}
    codes = [b["code"] for b in _line_blockers(cfg)]
    assert "dep_missing" in codes, f"缺 node 必须如实拦下，实际 blockers={codes}"
    dep = [b for b in _line_blockers(cfg) if b["code"] == "dep_missing"][0]
    # 复用 dep_missing 的 {dep}/{install} 参数化文案（i18n 约定：宁复用勿造同义新键）
    assert "Node" in dep["params"]["dep"]
    assert "nodejs.org" in dep["params"]["install"], "指引必须可照做"


def test_node_present_yields_no_blockers(monkeypatch):
    monkeypatch.setattr(lnl.shutil, "which", lambda n: r"C:\sys\node.exe")
    monkeypatch.setattr(lnl, "is_okline_available", lambda: True)
    cfg = {"platform_login": {"line": {"protocol_enabled": True}}}
    assert _line_blockers(cfg) == []


def test_missing_okline_does_not_double_report(monkeypatch):
    """okline 缺失是更根本的一环：此时不该再叠一条 node 的 dep_missing（噪音）。"""
    monkeypatch.setattr(lnl.shutil, "which", lambda n: None)
    monkeypatch.setattr(lnl, "is_okline_available", lambda: False)
    cfg = {"platform_login": {"line": {"protocol_enabled": True}}}
    deps = [b for b in _line_blockers(cfg) if b["code"] == "dep_missing"]
    assert len(deps) == 1 and deps[0]["params"]["dep"] == "okline"


def test_electron_fallback_makes_line_ready(tmp_path, monkeypatch):
    """turnkey 的正体：没装 Node 的机器靠随包 Electron 也应判为可用（零 blocker）。"""
    electron = _fake_exe(tmp_path, "electron.exe")
    monkeypatch.setenv("AITR_ELECTRON_NODE", electron)
    monkeypatch.setattr(lnl.shutil, "which", lambda n: None)
    monkeypatch.setattr(lnl, "is_okline_available", lambda: True)
    cfg = {"platform_login": {"line": {"protocol_enabled": True}}}
    assert _line_blockers(cfg) == []
