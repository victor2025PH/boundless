# -*- coding: utf-8 -*-
"""1.0.10 (2026-10-19, room-d): ChatX Fleet Panel task registration and unchanged adb restartreplace.

room-d, manual ``chatx-agent install-panel`` from ssh session 0:
  schtasks /Create -> rc 1 "ERROR: The task XML is malformed. (1,40):: unable to switch the encoding"
The same INTERACTIVE (S-1-5-4) + LogonTrigger task saved as UTF-16 registered fine from the same
session 0, so the encoding was the cause, not the session. The fake schtasks below behaves like
Task Scheduler did on room-d. No network, no real schtasks, no adb.
"""
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISS = (ROOT / "fleet_agent" / "setup" / "ChatXAgent.iss").read_text(encoding="utf-8").replace("\r\n", "\n")
PINS = {
    "adb.exe": "b4a6b455702684652cccf7b46258b29e653538904359a58fd4931cf3ef286b3f",
    "AdbWinApi.dll": "c1d653030b4bde65d3e07e4d0b0979e17be56df1436cdd15528630f27808050d",
    "AdbWinUsbApi.dll": "0710e894d9b40f71a670c13c694079d564c92c1279da382cfe4850983aaebe1b",
}


class _Reg:
    HKEY_LOCAL_MACHINE = object()
    KEY_SET_VALUE = 2
    KEY_QUERY_VALUE = 1
    KEY_WOW64_64KEY = 256
    REG_SZ = 1

    def CreateKeyEx(self, *_a):
        return self

    def SetValueEx(self, _k, name, _r, typ, value):
        self.saved = (value, typ)

    def QueryValueEx(self, _k, _name):
        return self.saved

    def __enter__(self):
        return self

    def __exit__(self, *_e):
        return False


def _task_scheduler(calls):
    """Fake schtasks: /Create /XML accepts only what Task Scheduler accepted on room-d."""
    def run(cmd):
        calls.append(list(cmd))

        class P:
            returncode = 0
            stdout = "SUCCESS"
            stderr = ""

        if cmd[:2] == ["schtasks", "/Create"] and "/XML" in cmd:
            data = Path(cmd[cmd.index("/XML") + 1]).read_bytes()
            if data[:2] not in (b"\xff\xfe", b"\xfe\xff"):
                P.returncode = 1
                P.stdout = "ERROR: The task XML is malformed.\r\n(1,40)::unable to switch the encoding"
            else:
                ET.fromstring(data)  # must parse as declared
        return P()
    return run


def test_install_panel_registers_task_with_xml_task_scheduler_accepts(monkeypatch, tmp_path):
    from src.fleet import service as S

    monkeypatch.setattr(S.os, "name", "nt")
    calls = []
    res = S.install_panel_logon(tmp_path / "fleet", run=_task_scheduler(calls), registry=_Reg())
    assert res["ok"] is True, res
    create = [c for c in calls if c[:2] == ["schtasks", "/Create"]]
    assert create and create[0][create[0].index("/TN") + 1] == "ChatX Fleet Panel"


def test_panel_task_xml_file_is_utf16_with_bom_and_matching_declaration(monkeypatch, tmp_path):
    from src.fleet import service as S

    monkeypatch.setattr(S.os, "name", "nt")
    S.install_panel_logon(tmp_path, run=_task_scheduler([]), registry=_Reg())
    data = (tmp_path / "panel_task.xml").read_bytes()
    assert data[:2] == b"\xff\xfe"
    text = data.decode("utf-16")
    assert text.startswith('<?xml version="1.0" encoding="UTF-16"?>')
    root = ET.fromstring(data)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find("t:Principals/t:Principal/t:GroupId", ns).text == "S-1-5-4"
    assert root.find("t:Triggers/t:LogonTrigger", ns) is not None
    assert "SYSTEM" not in text


def test_task_account_diag_reads_utf16_and_old_utf8_xml(tmp_path):
    from src.fleet import operator_alert as oa
    from src.fleet import service as S

    S.write_panel_task_xml(tmp_path)
    assert S.read_panel_task_xml(tmp_path / "panel_task.xml").count("S-1-5-4") == 1
    assert oa._panel_task_account(tmp_path) == "INTERACTIVE"
    old = tmp_path / "old"
    old.mkdir()
    (old / "panel_task.xml").write_text(S.build_panel_task_xml(old).replace("UTF-16", "UTF-8"), encoding="utf-8")
    assert oa._panel_task_account(old) == "INTERACTIVE"


def _files_line(name):
    lines = [l for l in ISS.splitlines()
             if l.startswith('Source: "{#PlatformToolsDir}\\' + name + '"') and 'DestDir: "{app}\\platform-tools"' in l]
    assert len(lines) == 1, lines
    return lines[0]


def test_bundled_adb_files_skip_when_hash_equals_baseline():
    defs = {"adb.exe": "AdbExeSha256", "AdbWinApi.dll": "AdbWinApiSha256", "AdbWinUsbApi.dll": "AdbWinUsbApiSha256"}
    for name, define in defs.items():
        line = _files_line(name)
        assert "restartreplace" in line
        assert f"Check: BundledAdbChanged('{name}', '{{#{define}}}')" in line
        assert f'#define {define} "{PINS[name]}"' in ISS


def test_bundled_adb_changed_function_compares_target_sha256():
    fn = ISS.split("function BundledAdbChanged(const Name, Expected: String): Boolean;")[1].split("\nend;\n")[0]
    assert "ExpandConstant('{app}\\platform-tools\\') + Name" in fn
    assert "GetSHA256OfFile(dest)" in fn
    assert "CompareText(h, Expected) = 0" in fn
    eq = fn[fn.index("CompareText(h, Expected) = 0"):]
    assert eq.index("Result := False;") < eq.index("ChatXInstallLog(")
    assert fn.index("if not FileExists(dest) then") < fn.index("GetSHA256OfFile(dest)")
    assert "kill-server" not in fn and "Stop-Process" not in fn and "taskkill" not in fn


def test_bundled_adb_check_defined_after_install_log_helper():
    assert ISS.index("procedure ChatXInstallLog") < ISS.index("function BundledAdbChanged")