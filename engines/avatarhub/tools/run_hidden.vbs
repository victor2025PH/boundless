' run_hidden.vbs - run a command with its console window hidden (style 0) and
' wait for it to finish, propagating the exit code. wscript.exe is a GUI app,
' so nothing ever flashes on the interactive desktop.
' Used by scheduled tasks (AvatarHubFulfill / AvatarHubWatchdogGuard): running
' a .bat or powershell directly as a task action pops a console window every
' time (2026-07-13 fix, same episode as the vram_gate.py CREATE_NO_WINDOW fix).
' ASCII-only on purpose (encoding-proof).
'
' Usage: wscript.exe //B //Nologo run_hidden.vbs <exe-or-bat> [args...]
If WScript.Arguments.Count = 0 Then WScript.Quit 2
Dim sh, cmd, i, a, rc
Set sh = CreateObject("WScript.Shell")
cmd = ""
For i = 0 To WScript.Arguments.Count - 1
    a = WScript.Arguments(i)
    If InStr(a, " ") > 0 And Left(a, 1) <> """" Then a = """" & a & """"
    cmd = cmd & a
    If i < WScript.Arguments.Count - 1 Then cmd = cmd & " "
Next
On Error Resume Next
rc = sh.Run(cmd, 0, True)
If Err.Number <> 0 Then rc = 1
WScript.Quit rc
