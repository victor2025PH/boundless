"""runner 服务本体 pc_runner 门禁：逐级 env 开闸 / 冻结急停 / 工具集契约 / 审计。

不触真 OS：所有 handler 用 mock；win32/uiautomation 缺失也全跑。契约的另一半
（READONLY_TOOLS/ACTION_TOOLS 与小智动作同步）在 test_pc_actions.py。
"""
import json

import pytest

from runner import pc_runner as pr


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # 逐级开闸/冻结全部清空（kill-switch 走 PC_RUNNER_KILL，与 start_runner.ps1 同契约）
    for k in ("PC_RUNNER_ACTIONS", "PC_RUNNER_SHELL", "PC_RUNNER_FROZEN",
              "PC_RUNNER_KILL", "PC_RUNNER_AUDIT", "PC_RUNNER_APPS"):
        monkeypatch.delenv(k, raising=False)
    yield


def _ok_handlers(seen):
    def mk(name):
        def h(args):
            seen.append((name, dict(args)))
            return {"ok": True, "echo": name}
        return h
    return {t: mk(t) for t in (pr.READONLY_TOOLS + pr.ACTION_TOOLS)}


# ── 工具集契约 ───────────────────────────────────────────────────
def test_tool_sets_contract():
    assert set(pr.READONLY_TOOLS) == {"list_windows", "read_tree", "screenshot"}
    for t in ("launch_app", "focus_window", "run_command", "type_text", "send_keys"):
        assert t in pr.ACTION_TOOLS
    assert not (set(pr.READONLY_TOOLS) & set(pr.ACTION_TOOLS))
    assert "run_command" in pr.SHELL_TOOLS
    assert pr.tier_of("list_windows") == "readonly"
    assert pr.tier_of("launch_app") == "action"
    assert pr.tier_of("run_command") == "shell"
    assert pr.tier_of("nope") == "unknown"


# ── 逐级 env 开闸 ────────────────────────────────────────────────
def test_readonly_always_allowed(monkeypatch):
    seen = []
    r = pr.dispatch("list_windows", {}, handlers=_ok_handlers(seen))
    assert r["ok"] and seen and seen[0][0] == "list_windows"  # 无需 PC_RUNNER_ACTIONS


def test_action_blocked_without_env(monkeypatch):
    seen = []
    r = pr.dispatch("launch_app", {"app_id": "notepad"}, handlers=_ok_handlers(seen))
    assert r["ok"] is False and r["error"] == "actions_disabled" and seen == []


def test_action_allowed_with_env(monkeypatch):
    monkeypatch.setenv("PC_RUNNER_ACTIONS", "1")
    seen = []
    r = pr.dispatch("focus_window", {"window": "记事本"}, handlers=_ok_handlers(seen))
    assert r["ok"] and seen[0][0] == "focus_window"


def test_shell_needs_extra_gate(monkeypatch):
    monkeypatch.setenv("PC_RUNNER_ACTIONS", "1")
    seen = []
    r = pr.dispatch("run_command", {"command": "echo hi"}, handlers=_ok_handlers(seen))
    assert r["ok"] is False and r["error"] == "shell_disabled" and seen == []
    monkeypatch.setenv("PC_RUNNER_SHELL", "1")
    r2 = pr.dispatch("run_command", {"command": "echo hi"}, handlers=_ok_handlers(seen))
    assert r2["ok"] and seen and seen[-1][0] == "run_command"


def test_frozen_blocks_actions_but_not_readonly(monkeypatch):
    monkeypatch.setenv("PC_RUNNER_ACTIONS", "1")
    monkeypatch.setenv("PC_RUNNER_FROZEN", "1")
    seen = []
    r = pr.dispatch("launch_app", {"app_id": "notepad"}, handlers=_ok_handlers(seen))
    assert r["ok"] is False and r["error"] == "actions_frozen" and seen == []
    # 冻结只拦动作，只读侦察仍可用
    r2 = pr.dispatch("list_windows", {}, handlers=_ok_handlers(seen))
    assert r2["ok"]


def test_frozen_via_kill_file(monkeypatch, tmp_path):
    monkeypatch.setenv("PC_RUNNER_ACTIONS", "1")
    kill = tmp_path / "kill"
    kill.write_text("x")
    monkeypatch.setenv("PC_RUNNER_KILL", str(kill))  # 与 start_runner.ps1 同契约
    r = pr.dispatch("type_text", {"text": "hi"}, handlers=_ok_handlers([]))
    assert r["ok"] is False and r["error"] == "actions_frozen"


# ── 分发健壮性 ───────────────────────────────────────────────────
def test_unknown_tool(monkeypatch):
    r = pr.dispatch("rm_rf", {}, handlers=_ok_handlers([]))
    assert r["ok"] is False and r["error"] == "unknown_tool"


def test_handler_exception_no_crash(monkeypatch):
    def boom(args):
        raise RuntimeError("x")
    r = pr.dispatch("list_windows", {}, handlers={"list_windows": boom})
    assert r["ok"] is False and r["error"] == "handler_exception"


# ── health / 能力探测 ────────────────────────────────────────────
def test_health_fields(monkeypatch):
    monkeypatch.setenv("PC_RUNNER_ACTIONS", "1")
    h = pr.health()
    assert h["ok"] and h["version"] == pr.VERSION
    for k in ("win32_available", "uia_available", "actions_enabled",
              "shell_enabled", "actions_frozen"):
        assert k in h
    assert h["actions_enabled"] is True and h["shell_enabled"] is False


def test_capability_probes_return_bool():
    assert isinstance(pr.win32_available(), bool)
    assert isinstance(pr.uia_available(), bool)
    assert isinstance(pr.probe_desktop(), bool)
    assert isinstance(pr.is_elevated(), bool)


# ── 白名单 / 审计 ────────────────────────────────────────────────
def test_app_whitelist_parse(monkeypatch):
    monkeypatch.setenv("PC_RUNNER_APPS",
                       "notepad=C:\\Windows\\notepad.exe;calc=C:\\Windows\\System32\\calc.exe")
    wl = pr.app_whitelist()
    assert wl["notepad"].endswith("notepad.exe") and wl["calc"].endswith("calc.exe")


def test_launch_app_rejects_non_whitelisted(monkeypatch):
    monkeypatch.setenv("PC_RUNNER_ACTIONS", "1")  # 过动作闸，才轮到白名单判定
    r = pr.dispatch("launch_app", {"app_id": "evil"})  # 真 handler
    assert r["ok"] is False and r["error"] == "app_not_whitelisted"


def test_audit_written_when_configured(monkeypatch, tmp_path):
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("PC_RUNNER_AUDIT", str(audit))
    pr.dispatch("list_windows", {}, handlers=_ok_handlers([]), actor="boss@pc:117")
    lines = [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines()]
    assert lines[-1]["tool"] == "list_windows" and lines[-1]["ok"] is True
    assert lines[-1]["actor"] == "boss@pc:117" and lines[-1]["tier"] == "readonly"
