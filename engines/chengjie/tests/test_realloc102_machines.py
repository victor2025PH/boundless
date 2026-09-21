# -*- coding: utf-8 -*-
"""实施102 机器侧执行器（deploy/compute/realloc102_machines.py）门禁：假 runner，零 ssh。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[1]
_MOD = _ENGINE / "deploy" / "compute" / "realloc102_machines.py"


@pytest.fixture(scope="module")
def rm():
    # dataclass + `from __future__ import annotations` 解析字段类型时要在 sys.modules 里找到模块
    name = "_realloc102_machines"
    spec = importlib.util.spec_from_file_location(name, _MOD)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeRunner:
    def __init__(self, ssh_out=None, http_out=None, files=(), local_rc=0):
        self.calls = []
        self.ssh_out = ssh_out or {}
        self.http_out = http_out or {}
        self.files = set(files)
        self.local_rc = local_rc
        self.slept = 0

    def ssh(self, host, cmd, timeout):
        self.calls.append(("ssh", host, cmd))
        for key, (rc, out) in self.ssh_out.items():
            if key in cmd:
                return rc, out
        return 0, "ok"

    def http(self, url, timeout=6.0):
        self.calls.append(("http", url))
        return self.http_out.get(url, (1, "URLError"))

    def file_exists(self, path):
        self.calls.append(("file", path))
        return path in self.files

    def sleep(self, sec):
        self.slept += sec

    def local(self, argv, timeout):
        self.calls.append(("local", tuple(argv)))
        return self.local_rc, "overlay applied"


def test_json_quoting_for_cmd_exe(rm):
    cmd = rm._ollama_generate("qwen3-vl:8b-instruct", -1)
    # cmd.exe 里外层双引号 + 内层 \" 转义；绝不能出现单引号包 JSON
    assert '-d "{\\"model\\":\\"qwen3-vl:8b-instruct\\",\\"keep_alive\\":-1}"' in cmd
    assert "'" not in cmd
    assert "127.0.0.1:11434/api/generate" in cmd


def test_phases_cover_runbook_targets(rm):
    p0, p1, p2, p3, p4 = (rm.BUILDERS[n]() for n in rm.PHASES)
    assert any("qwen3:30b" in s.cmd and s.host == rm.H176 for s in p0.steps)      # 卸 30b
    assert any("num_ctx 8192" in s.cmd and s.host == rm.H198 for s in p0.steps)   # 198 钉 8k
    assert any("ollama_from_104" in s.cmd for s in p0.steps)                      # 放行 104
    assert p0.overlay_phase == "phase0" and p1.overlay_phase == "phase1"
    assert any(s.cmd.startswith("file:") and "service_token" in s.cmd for s in p1.steps)
    assert any("AITR_ASR" in s.cmd and s.host == rm.H176 for s in p2.steps)
    assert any(s.cmd.startswith("poll:") and "8765" in s.cmd for s in p2.steps)
    assert any("EmotionTTS" in s.cmd and s.host == rm.H140 for s in p3.steps)
    assert p4.overlay_phase == "" and all(s.host in (rm.H173, "local") for s in p4.steps)
    # phase4 只读：不含 stop/restart/robocopy 这类动作
    assert not any(k in s.cmd.lower() for s in p4.steps for k in ("stop_instance", "robocopy", "restart"))


def test_phase3_comfy_task_placeholder(rm):
    without = rm.build_phase3()
    assert any(s.cmd.startswith("skip:") and s.optional for s in without.steps)
    with_task = rm.build_phase3("ComfyUI_Boot")
    assert any('schtasks /Run /TN "ComfyUI_Boot"' in s.cmd for s in with_task.steps)
    assert any(s.cmd == "poll:http://192.168.0.104:8188/system_stats" for s in with_task.steps)


def test_dry_run_executes_nothing(rm, tmp_path, capsys):
    fr = FakeRunner()
    rc = rm.run_phase(rm.build_phase0(), apply=False, runner=fr, log_path=tmp_path / "l.jsonl")
    assert rc == 0 and fr.calls == []
    out = capsys.readouterr().out
    assert "dry-run" in out and "realloc102.py phase0 --apply" in out
    assert not (tmp_path / "l.jsonl").exists()


def test_apply_stops_at_first_required_failure(rm, tmp_path):
    fr = FakeRunner(ssh_out={"api/generate": (0, '{"done":false}')})   # 卸载没成功
    rc = rm.run_phase(rm.build_phase0(), apply=True, runner=fr, log_path=tmp_path / "l.jsonl")
    assert rc == 1
    assert len([c for c in fr.calls if c[0] == "ssh"]) == 1            # 第 1 步后即停
    assert not any(c[0] == "local" for c in fr.calls)                  # overlay 没跑
    lines = (tmp_path / "l.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1 and '"ok": false' in lines[0]


def test_apply_optional_failure_continues_and_runs_overlay(rm, tmp_path):
    fr = FakeRunner(
        ssh_out={"api/generate": (0, '{"done":true}'), "ollama list": (0, "qwen3-vl:8b-instruct"),
                 "setx": (1, "denied"), "findstr": (1, "")},
        http_out={"http://192.168.0.198:11434/api/ps": (0, '{"models":[{"name":"qwen3-vl:8b-instruct"}]}')},
    )
    rc = rm.run_phase(rm.build_phase0(), apply=True, runner=fr, overlay_extra=["--no-probe"],
                      log_path=tmp_path / "l.jsonl")
    assert rc == 0
    local = [c for c in fr.calls if c[0] == "local"]
    assert len(local) == 1 and local[0][1][-3:] == ("phase0", "--apply", "--no-probe")
    assert ("http", "http://192.168.0.198:11434/api/ps") in fr.calls        # 验收跑了


def test_poll_step_waits_then_succeeds(rm, tmp_path, monkeypatch):
    seq = iter([(1, "down"), (0, '{"asr_loaded":true,"ser_loaded":false}'),
                (0, '{"asr_loaded":true,"ser_loaded":true}')])
    fr = FakeRunner()
    fr.http = lambda url, timeout=6.0: next(seq)
    st = rm.Step("local", "poll:http://x/health", "等载入", expect='"ser_loaded":true', timeout=60)
    ok, out = rm.run_step(st, fr)
    assert ok and '"ser_loaded":true' in out and fr.slept == 10


def test_poll_step_times_out(rm, monkeypatch):
    fr = FakeRunner(http_out={"http://x/health": (0, '{"ser_loaded":false}')})
    clock = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(rm.time, "monotonic", lambda: next(clock))
    st = rm.Step("local", "poll:http://x/health", "等", expect='"ser_loaded":true', timeout=30)
    ok, out = rm.run_step(st, fr)
    assert not ok and "超时" in out


def test_file_and_skip_steps(rm):
    fr = FakeRunner(files={"D:/faceX/mfys/secrets/service_token.txt"})
    ok, _ = rm.run_step(rm.Step("local", "file:D:/faceX/mfys/secrets/service_token.txt", "令牌"), fr)
    assert ok
    ok, msg = rm.run_step(rm.Step("local", "skip:--comfy-task 未给", "x", optional=True), fr)
    assert not ok and "comfy-task" in msg
