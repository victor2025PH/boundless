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


def test_remote_commands_are_powershell_not_curl(rm):
    """176 默认 shell 是 PowerShell（curl=Invoke-WebRequest 别名）——远端一律 PowerShell 脚本。"""
    cmd = rm._ollama_generate("qwen3-vl:8b-instruct", -1)
    assert cmd.startswith("Invoke-RestMethod -Method Post -Uri http://127.0.0.1:11434/api/generate")
    assert "'{\"model\": \"qwen3-vl:8b-instruct\", \"keep_alive\": -1}'" in cmd
    assert "ConvertTo-Json -Compress" in cmd
    for name in rm.PHASES:
        for st in rm.BUILDERS[name]().steps + rm.BUILDERS[name]().verify:
            if st.host != "local":
                assert not st.cmd.lstrip().startswith("curl"), st.cmd
                assert "findstr" not in st.cmd and ">nul" not in st.cmd and "%TEMP%" not in st.cmd, st.cmd


def test_encode_ps_roundtrip(rm):
    import base64
    wrapped = rm.encode_ps("Write-Output hi")
    assert wrapped.startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")
    b64 = wrapped.rsplit(" ", 1)[1]
    script = base64.b64decode(b64).decode("utf-16-le")
    assert script.startswith(rm.PS_PRELUDE) and script.endswith("Write-Output hi")
    assert "[Console]::OutputEncoding" in script


def test_runner_ssh_sends_encoded_command(rm, monkeypatch):
    seen = {}

    class _P:
        returncode = 0
        stdout = b"ok"
        stderr = b""

    def fake_run(argv, capture_output, timeout):
        seen["argv"] = argv
        return _P()

    monkeypatch.setattr(rm.subprocess, "run", fake_run)
    rc, out = rm.Runner().ssh("ganzhi", "Write-Output x", 30)
    assert rc == 0 and out == "ok"
    assert seen["argv"][:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
    assert seen["argv"][5] == "ganzhi" and seen["argv"][6].startswith("powershell -NoProfile")


def test_phases_cover_runbook_targets(rm):
    p0, p0e, p1, p2, p3, p4 = (rm.BUILDERS[n]() for n in rm.PHASES)
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


def test_phase0_hardening_is_opt_in(rm):
    """setx OLLAMA_HOST 只在 --harden-ollama 时进步骤；缺省给调用方统计（先看谁在打 176 Ollama）。"""
    default = rm.build_phase0()
    assert not any("setx OLLAMA_HOST" in s.cmd for s in default.steps)
    assert any("server.log" in s.cmd and s.optional for s in default.steps)
    hard = rm.build_phase0(harden_ollama=True)
    assert any("setx OLLAMA_HOST" in s.cmd and s.optional for s in hard.steps)
    assert rm.BUILDERS["phase0"](harden_ollama=True).steps[-1].cmd.startswith("setx OLLAMA_HOST")


def test_phase3_comfy_task_placeholder(rm):
    without = rm.build_phase3()
    assert any(s.cmd.startswith("skip:") and s.optional for s in without.steps)
    # 104 没起来就不碰 176 的 ComfyUI（自拍唯一后端）
    assert not any("ComfyWatchdog" in s.cmd or "8188 -State Listen" in s.cmd for s in without.steps)
    with_task = rm.build_phase3("ComfyUI_Boot")
    assert any('schtasks /Run /TN "ComfyUI_Boot"' in s.cmd for s in with_task.steps)
    assert any(s.cmd == "poll:http://192.168.0.104:8188/system_stats" for s in with_task.steps)


def test_phase3_stops_176_comfy_only_after_104_is_up(rm):
    """176 ComfyUI 的停止写死任务名（04:12 发现步查到 ComfyBoot/ComfyWatchdog），且排在 104 poll 之后：
    先停任务再杀进程（否则 5 分内被看门狗拉回），只杀听 8188 的那一个。"""
    steps = rm.build_phase3("ComfyUI_Boot").steps
    idx_poll = next(i for i, s in enumerate(steps) if s.cmd.startswith("poll:") and "104:8188" in s.cmd)
    idx_task = next(i for i, s in enumerate(steps) if "ComfyWatchdog /Disable" in s.cmd and s.host == rm.H176)
    idx_kill = next(i for i, s in enumerate(steps) if "LocalPort 8188" in s.cmd and s.host == rm.H176)
    assert idx_poll < idx_task < idx_kill
    assert "ComfyBoot /Disable" in steps[idx_task].cmd and not steps[idx_task].optional
    assert "Stop-Process" in steps[idx_kill].cmd and "-Force" in steps[idx_kill].cmd


def test_phase0e_parks_musetalk_via_hub_not_kill(rm):
    """musetalk 走 AvatarHub 显存管家 /api/gpu/park（挂起自愈），不裸杀 :8090；0e 可单独重跑且不碰 overlay。"""
    p0e = rm.build_phase0e()
    assert p0e.overlay_phase == ""
    park = [s for s in p0e.steps if "/api/gpu/park?name=lipsync" in s.cmd and s.host == rm.H176]
    assert len(park) == 1 and not park[0].optional
    assert not any("Stop-Process" in s.cmd or "8090" in s.cmd for s in p0e.steps)
    assert any("8090" in s.cmd and s.expect == "0" for s in p0e.verify)
    # phase0 全跑也包含同一步；旧的“发现任务名”步已下线
    p0 = rm.build_phase0()
    assert any(s.cmd == park[0].cmd for s in p0.steps)
    assert not any("comfy|musetalk|lipsync" in s.cmd for s in p0.steps)


def test_phase2_uses_real_task_names_and_ships_repo_asr_server(rm):
    """现场任务名 AITR_ASR_176 / AITR_ASR_198（不是 AITR_ASR）；176 盘上是 08-18 旧版，先 scp 仓库版并哈希验收，
    再解 72h 时限、再 Enable；拆 198 放在 overlay 之后且先确认 176 在线、先停看门狗再杀进程。"""
    p2 = rm.build_phase2()
    cmds = [s.cmd for s in p2.steps]
    assert not any(" /TN AITR_ASR " in c or " /TN AITR_ASR;" in c for c in cmds + [s.cmd for s in p2.verify])
    i_scp = next(i for i, c in enumerate(cmds) if c.startswith("scp:") and c.endswith(":C:/aitr_asr/asr_server.py"))
    i_hash = next(i for i, c in enumerate(cmds) if "Get-FileHash" in c)
    i_limit = next(i for i, c in enumerate(cmds) if "ExecutionTimeLimit = 'PT0S'" in c)
    i_run = next(i for i, c in enumerate(cmds) if "schtasks /Run /TN AITR_ASR_176" in c)
    i_poll = next(i for i, c in enumerate(cmds) if c.startswith("poll:") and "176:8765" in c)
    assert i_scp < i_hash < i_limit < i_run < i_poll
    assert p2.steps[i_scp].host == "local" and rm.H176 + ":" in cmds[i_scp]
    assert p2.steps[i_hash].expect == rm._file_sha256(rm.ASR_SERVER_SRC) and len(p2.steps[i_hash].expect) == 64
    assert p2.steps[i_limit].expect == "PT0S"
    v = p2.verify
    assert v[0].cmd.startswith("poll:") and "176:8765" in v[0].cmd
    j_dis = next(i for i, s in enumerate(v) if "AITR_ASR_198 /Disable" in s.cmd and "AITR_ASR_WATCHDOG /Disable" in s.cmd)
    j_kill = next(i for i, s in enumerate(v) if "asr_server" in s.cmd and "Stop-Process" in s.cmd)
    assert 0 < j_dis < j_kill and all(v[j].host == rm.H198 for j in (j_dis, j_kill))
    assert not any("Stop-Process" in s.cmd for s in p2.steps)   # 步骤段不碰 198（overlay 前还在服务）


def test_scp_step_dispatches_to_local_scp(rm):
    fr = FakeRunner()
    ok, out = rm.run_step(rm.Step("local", "scp:C:/repo/a.py|ganzhi:C:/aitr_asr/a.py", "x"), fr)
    assert ok and fr.calls == [("local", ("scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                                           "C:/repo/a.py", "ganzhi:C:/aitr_asr/a.py"))]
    ok, _ = rm.run_step(rm.Step("local", "scp:a|b:c", "x"), FakeRunner(local_rc=1))
    assert not ok


def test_verify_only_skips_steps_and_overlay(rm, tmp_path):
    """首跑在 overlay 回显处崩掉后只补验收：不再重放步骤（schtasks /Run 二次触发）、不再改 overlay。"""
    fr = FakeRunner(http_out={"http://192.168.0.176:8765/health": (0, '{"asr_loaded":true}')})
    rc = rm.run_phase(rm.build_phase2(), apply=True, runner=fr, log_path=tmp_path / "l.jsonl", verify_only=True)
    assert rc == 0
    assert not any(c[0] == "local" and "realloc102.py" in " ".join(c[1]) for c in fr.calls)
    assert not any(c[0] == "ssh" and c[1] == rm.H176 for c in fr.calls)
    assert any(c[0] == "ssh" and c[1] == rm.H198 and "AITR_ASR_198 /Disable" in c[2] for c in fr.calls)


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
                 "vl8k.Modelfile\" -Encoding": (0, "FROM x\nPARAMETER num_ctx 8192"),
                 "server.log": (1, "no log")},                                  # 可选步失败
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
