# -*- coding: utf-8 -*-
"""0.3.30: exit confirm, node tray icon, system adb only when absent, x64compatible, room-a adb pins, --no-ocr."""
from pathlib import Path
from src.fleet import operator_alert as oa

ROOT = Path(__file__).resolve().parents[1]
ISS = (ROOT / "fleet_agent" / "setup" / "ChatXAgent.iss").read_text(encoding="utf-8").replace("\r\n", "\n")
PS1 = (ROOT / "fleet_agent" / "build_setup.ps1").read_text(encoding="utf-8")
BUILD = (ROOT / "fleet_agent" / "build_agent.py").read_text(encoding="utf-8")
PINS = {'adb.exe': 'B4A6B455702684652CCCF7B46258B29E653538904359A58FD4931CF3EF286B3F', 'AdbWinApi.dll': 'C1D653030B4BDE65D3E07E4D0B0979E17BE56DF1436CDD15528630F27808050D', 'AdbWinUsbApi.dll': '0710E894D9B40F71A670C13C694079D564C92C1279DA382CFE4850983AAEBE1B'}


def test_exit_asks_before_quitting():
    s = oa.PANEL_SCRIPT
    assert s.isascii()
    body = s[s.index("$menuExit.add_Click({"):][:600]
    assert "MessageBox]::Show($script:exitAsk" in body
    assert body.index("DialogResult]::Yes) { return }") < body.index("$script:quitting = $true")
    assert "$script:exitAsk = Txt $snap $script:lang 'exit_confirm'" in s
    assert oa.STRINGS["zh"]["exit_confirm"] and oa.STRINGS["en"]["exit_confirm"]


def test_tray_icon_node_ico_with_fallback():
    s = oa.PANEL_SCRIPT
    assert "'fleet-node.ico'" in s and "SystemIcons]::Warning" in s
    assert s.index("SystemIcons]::Warning") < s.index("'fleet-node.ico'")


def test_iss_x64compatible_and_logging():
    assert "ArchitecturesAllowed=x64compatible\n" in ISS
    assert "ArchitecturesInstallIn64BitMode=x64compatible\n" in ISS
    assert "ArchitecturesAllowed=x64\n" not in ISS
    assert "SetupLogging=yes" in ISS


def test_iss_system_adb_never_overwrites_existing():
    lines = [l for l in ISS.splitlines() if 'DestDir: "C:\\platform-tools"' in l]
    assert len(lines) == 3
    for name in PINS:
        assert any("\\" + name + '"' in l for l in lines)
    for l in lines:
        assert "onlyifdoesntexist" in l and "uninsneveruninstall" in l and "Check: NeedSystemAdb" in l
        assert "ignoreversion" not in l and "restartreplace" not in l
    fn = ISS.split("function NeedSystemAdb")[1].split("end;\n\nfunction")[0]
    assert "GetSHA256OfFile('C:\\platform-tools\\adb.exe')" in fn and "Log(" in fn
    prep = ISS.split("function PrepareToInstall")[1]
    assert prep.index("NeedSystemAdb();") < prep.index("StopOurAgent()")


def test_installer_never_kill_server_and_logs_to_fixed_path():
    assert "kill-server" not in ISS and "& $adb" not in ISS
    stop = ISS.split("procedure StopBundledAdb")[1].split("function PrepareToInstall")[0]
    assert "Stop-Process" not in stop and "taskkill" not in stop
    log = ISS.split("procedure ChatXInstallLog")[1].split("function NeedSystemAdb")[0]
    assert "{commonappdata}\\ChatX" in log and "\\install.log" in log and "SaveStringToFile" in log
    fn = ISS.split("function NeedSystemAdb")[1].split("end;\n\nfunction")[0]
    assert "ChatXInstallLog('system adb present" in fn


def test_build_setup_pins_room_a_baseline():
    assert PS1.isascii()
    for n, h in PINS.items():
        assert "'%s' = '%s'" % (n, h.lower()) in PS1
    assert "does not match the room-a baseline" in PS1
    assert "The setup still builds" not in PS1


def test_build_agent_no_ocr_flag():
    assert '"--no-ocr"' in BUILD and '"--exclude-module"' in BUILD
    assert "ocr_exclude_flags() if args.no_ocr else ocr_collect_flags()" in BUILD
