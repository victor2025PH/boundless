Option Explicit
Dim sh, fso, dir, exe, stateDir, cmd
Set sh = CreateObject("Wscript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
exe = dir & "\chatx-agent.exe"
stateDir = sh.ExpandEnvironmentStrings("%ProgramData%") & "\ChatX\fleet"
cmd = """" & exe & """ --state-dir """ & stateDir & """ ui"
sh.Run cmd, 0, False
