' run_hidden.vbs - launch a command line with a fully hidden window (zero flash).
'
' Why: Task Scheduler actions that point at console apps (powershell.exe /
' python.exe / cmd.exe) always create a visible console window first, even
' with `powershell -WindowStyle Hidden` (the console exists before PowerShell
' can hide it -> brief black flash every run). wscript.exe is a GUI host, and
' WshShell.Run with intWindowStyle=0 passes SW_HIDE to CreateProcess, so the
' child console is born hidden. No flash, same user session, same env.
'
' Usage (Task Scheduler action):
'   Execute:  C:\Windows\SysWOW64\wscript.exe
'   Argument: //B //Nologo "D:\workspace\boundless\deploy\instances\run_hidden.vbs" <exe> [args...]
'
' MACHINE QUIRK (117, verified 2026-07-29): C:\Windows\System32\wscript.exe was
' REMOVED (anti-malware hardening); only the 32-bit SysWOW64 copy remains, so
' tasks MUST point at SysWOW64\wscript.exe (System32 -> 0x80070002 at launch).
' Because the host is 32-bit, WOW64 filesystem redirection applies to children:
' reference PowerShell as C:\Windows\Sysnative\WindowsPowerShell\v1.0\powershell.exe
' (Sysnative = the real 64-bit System32, only visible to 32-bit processes).
'
' Notes:
' - Waits for the child and propagates its exit code (LastTaskResult stays honest).
' - Re-quoting rule: an argument is quoted iff it contains a space. Arguments
'   that themselves contain double quotes are NOT supported (not needed here).
' - Registered consumers (2026-07-29): LanEmbedWatchdog, EmotionTTSWatchdog,
'   CredPoolWatchdog, Boundless-messenger-web-watchdog, ChatxFulfillWatch.
'   Backups of original task XML: D:\chengjie-instances\.ops\task_backups_20260729\
Option Explicit

Dim sh, cmd, i, a, rc

If WScript.Arguments.Count = 0 Then
  WScript.Quit 2
End If

cmd = ""
For i = 0 To WScript.Arguments.Count - 1
  a = WScript.Arguments(i)
  If InStr(a, " ") > 0 Then
    a = """" & a & """"
  End If
  If Len(cmd) > 0 Then
    cmd = cmd & " "
  End If
  cmd = cmd & a
Next

Set sh = CreateObject("WScript.Shell")
' 0 = SW_HIDE, True = wait for exit so the task's LastTaskResult reflects the child.
rc = sh.Run(cmd, 0, True)
WScript.Quit rc
