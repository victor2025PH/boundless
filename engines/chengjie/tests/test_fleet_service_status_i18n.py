"""`chatx-agent status` / 本机页不能因为界面语言误报「未运行」（P0-5）。

中文 Windows 的 ``schtasks /FO LIST /V`` 把 Status 这一项写成「模式: 正在运行」，旧代码只认
status / 状态，于是 state 为空、页面显示「未运行」。现在先经 COM 读与语言无关的数值状态，
文本解析只做兜底。下面的输出取自 173（zh-CN，2026-10-01 只读执行）和 117（en-US）。
"""
from __future__ import annotations

import os
import subprocess

import pytest

from src.fleet import local_status as ls
from src.fleet import service as svc

ZH_LIST = """
文件夹: \\
主机名:                             HOST-ZH
任务名:                             \\ChatX Fleet Agent
下次运行时间:                       N/A
模式:                               正在运行
登录状态:                           交互方式/后台方式
上次运行时间:                       2026/10/1 11:40:33
上次结果:                           267009
要运行的任务:                       "C:\\Program Files\\ChatX Agent\\chatx-agent.exe" --state-dir C:\\ProgramData\\ChatX\\fleet run --service
计划任务状态:                       已启用
空闲时间:                           已禁用
作为用户运行:                       SYSTEM
重复: 如果还在运行，停止:           N/A
"""

EN_LIST = """
Folder: \\
HostName:                             HOST-EN
TaskName:                             \\ChatX Fleet Agent
Next Run Time:                        N/A
Status:                               Running
Logon Mode:                           Interactive/Background
Last Run Time:                        2026/10/1 11:32:43
Last Result:                          267009
Scheduled Task State:                 Enabled
Run As User:                          SYSTEM
"""


def _runner(list_out: str, com_out=None, com_rc: int = 0, com_exc: Exception = None):
    calls = []

    def run(cmd):
        calls.append(list(cmd))
        if cmd[0] == "schtasks":
            return subprocess.CompletedProcess(cmd, 0, stdout=list_out, stderr="")
        if com_exc is not None:
            raise com_exc
        return subprocess.CompletedProcess(cmd, com_rc, stdout=com_out or "", stderr="")

    return run, calls


@pytest.fixture
def nt(monkeypatch):
    monkeypatch.setattr(svc.os, "name", "nt")


def test_com_state_query_is_language_independent_and_uses_windows_powershell():
    cmd = svc.build_task_state_query(task_name="ChatX Fleet Agent")
    assert cmd[0].lower().endswith("\\system32\\windowspowershell\\v1.0\\powershell.exe")
    assert "-NoProfile" in cmd and "Schedule.Service" in cmd[-1] and "[int]" in cmd[-1]
    assert "GetTask('ChatX Fleet Agent')" in cmd[-1]
    assert "Get-ScheduledTask" not in cmd[-1]  # no module load -> immune to an inherited PSModulePath
    assert "GetTask('it''s')" in svc.build_task_state_query(task_name="it's")[-1]


@pytest.mark.parametrize("out,want", [("4\r\n", "Running"), ("3", "Ready"), ("1", "Disabled"), ("2", "Queued"),
                                      ("0", "Unknown"), ("9", ""), ("", ""), ("Running", ""), ("4\n4", "")])
def test_parse_task_state_number(out, want):
    assert svc.parse_task_state_number(out) == want


@pytest.mark.parametrize("text,want", [(ZH_LIST, "Running"), (EN_LIST, "Running"),
                                       ("状态: 就绪\n", "Ready"), ("Status: Ready\n", "Ready"),
                                       ("模式: 已禁用\n", "Disabled"), ("Status: Some New Value\n", "Some New Value"),
                                       ("登录状态: 交互方式/后台方式\n计划任务状态: 已启用\n", "")])
def test_parse_schtasks_list_state_zh_and_en(text, want):
    assert svc.parse_schtasks_list_state(text) == want


@pytest.mark.parametrize("list_out", [ZH_LIST, EN_LIST])
def test_service_status_prefers_numeric_com_state(nt, list_out):
    run, calls = _runner(list_out, com_out="3\r\n")
    st = svc.service_status(run=run)
    # COM wins over the text (text says Running, COM says Ready).
    assert st == {"installed": True, "kind": "schtasks", "task_name": svc.TASK_NAME, "state": "Ready"}
    assert calls[0][0] == "schtasks" and calls[1][0].lower().endswith("powershell.exe")


@pytest.mark.parametrize("list_out", [ZH_LIST, EN_LIST])
@pytest.mark.parametrize("com", [dict(com_rc=3), dict(com_out="garbage"),
                                 dict(com_exc=subprocess.TimeoutExpired("powershell", 5)),
                                 dict(com_exc=FileNotFoundError("powershell.exe"))])
def test_service_status_falls_back_to_text_in_both_languages(nt, list_out, com):
    run, _ = _runner(list_out, **com)
    st = svc.service_status(run=run)
    assert st["installed"] is True and st["state"] == "Running"


def test_service_status_missing_task_skips_com(nt):
    calls = []

    def run(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="错误: 系统找不到指定的文件。")

    assert svc.service_status(run=run) == {"installed": False, "kind": "schtasks", "task_name": svc.TASK_NAME, "state": ""}
    assert len(calls) == 1


@pytest.mark.parametrize("list_out", [ZH_LIST, EN_LIST])
def test_local_page_shows_running_on_chinese_and_english_windows(nt, monkeypatch, list_out):
    run, _ = _runner(list_out, com_rc=3)  # worst case: COM unavailable, text only
    monkeypatch.setattr(ls, "_run_quick", lambda cmd, timeout=ls.TASK_QUERY_TIMEOUT_SEC: run(cmd))
    got = ls.query_task_status()
    assert got["query"] == "ok" and got["installed"] is True
    running = ls.task_state_running(got["state"])
    assert running is True
    assert ls.task_label_of(installed=True, running=running, query="ok") == "运行中"


@pytest.mark.skipif(os.name != "nt", reason="needs Windows Task Scheduler")
def test_live_com_query_returns_a_known_state_or_missing():
    p = subprocess.run(svc.build_task_state_query(), capture_output=True, text=True, timeout=30)
    if p.returncode == 3:
        pytest.skip("ChatX Fleet Agent task not installed on this machine")
    assert p.returncode == 0 and svc.parse_task_state_number(p.stdout) in set(svc.TASK_STATE_NAMES.values())
