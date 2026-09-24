; ChatXAgent.iss -- Inno Setup 6 script for ChatXAgentSetup.exe (free compiler, no paid tools).
; Build: powershell -File fleet_agent\build_setup.ps1
; Silent mass deploy: ChatXAgentSetup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
; Room key:          ChatXAgentSetup.exe /ROOMKEYFILE=C:\path\room.key
;                    (or drop room.key next to this exe; the key is never baked into the public build)
; Controller:        ChatXAgentSetup.exe /CONTROLLER=https://bd2026.cc/fleet

#ifndef AgentExe
  #define AgentExe "..\dist\chatx-agent.exe"
#endif
#ifndef SetupDir
  #define SetupDir "."
#endif
#ifndef DistDir
  #define DistDir "..\dist"
#endif
#ifndef AppVersion
  #define AppVersion "0.3.1"
#endif

#define AppName "ChatX Fleet Agent"
#define AppPublisher "ChatX"

[Setup]
AppId={{A7C3E1B2-4F58-4C0A-9B1E-6D2F8A0C4E71}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\ChatX Agent
DefaultGroupName=ChatX Fleet
DisableProgramGroupPage=yes
OutputDir={#DistDir}
OutputBaseFilename=ChatXAgentSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayName={#AppName}
VersionInfoVersion={#AppVersion}
CloseApplications=no

[UninstallRun]
; Stop the scheduled task before Inno deletes chatx-agent.exe.
Filename: "{app}\chatx-agent.exe"; Parameters: "uninstall-service"; Flags: runhidden; RunOnceId: "StopChatXFleetAgent"

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Messages]
FinishedHeadingLabel=Installed
FinishedLabel=This PC is set up. If you used the public installer, an admin still has to approve it in the fleet console (pending). Open "Fleet node status" from the Start menu to check this PC. A room installer joins its group without that extra click.

[Files]
Source: "{#AgentExe}"; DestDir: "{app}"; DestName: "chatx-agent.exe"; Flags: ignoreversion
Source: "{#SetupDir}\bootstrap.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SetupDir}\Open-Status.cmd"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Fleet node status"; Filename: "{app}\Open-Status.cmd"
Name: "{autoprograms}\Fleet console"; Filename: "{code:GetConsoleUrl}"

[Code]
function CmdParam(const Prefix: String): String;
var
  i: Integer;
  s: String;
begin
  Result := '';
  for i := 1 to ParamCount do
  begin
    s := ParamStr(i);
    if Pos(Prefix, s) = 1 then
      Result := Copy(s, Length(Prefix) + 1, 500);
  end;
end;

function GetController(Param: String): String;
begin
  Result := CmdParam('/CONTROLLER=');
  if Result = '' then
    Result := 'https://bd2026.cc/fleet';
end;

function GetConsoleUrl(Param: String): String;
begin
  Result := GetController('') + '/console';
end;

procedure ExitProcess(uExitCode: Cardinal);
  external 'ExitProcess@kernel32.dll stdcall';

function PsLiteral(const S: String): String;
var
  i: Integer;
begin
  { PowerShell single-quoted literal: a ' inside the value is written as ''. }
  Result := '';
  for i := 1 to Length(S) do
  begin
    if S[i] = '''' then
      Result := Result + ''''''
    else
      Result := Result + S[i];
  end;
end;

procedure FailInstall(const Msg: String);
begin
  { RaiseException during ssPostInstall still exits 0 on a silent install. }
  if WizardSilent then
    ExitProcess(1);
  RaiseException(Msg);
end;

procedure StopOurAgent();
var
  ResultCode: Integer;
  exe, cmd: String;
begin
  { End both tasks. Kill only the process whose image is this install path. }
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/End /TN "ChatX Fleet Agent"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/End /TN "ChatX Fleet Agent Upgrade"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  exe := PsLiteral(ExpandConstant('{app}\chatx-agent.exe'));
  cmd := '-NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq ''chatx-agent.exe'' -and $_.ExecutablePath -eq ''' + exe + ''' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"';
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), cmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  NeedsRestart := False;
  StopOurAgent();
end;

function RunIcacls(const Params: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\icacls.exe'), Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
            and (ResultCode = 0);
end;

function StateDirWasLocked(const Dir: String): Boolean;
var
  ResultCode: Integer;
  cmd: String;
begin
  { Sampled before the directory-only setowner. Exit 0 locked, 1 not, 2 error. }
  cmd := '-NoProfile -ExecutionPolicy Bypass -Command "' +
    '$ErrorActionPreference=''Stop''; try {' +
    '$a=Get-Acl -LiteralPath ''' + PsLiteral(Dir) + ''';' +
    '$s=$a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value;' +
    'if(($s -ne ''S-1-5-18'') -and ($s -ne ''S-1-5-32-544'')){ exit 1 };' +
    'if(-not $a.AreAccessRulesProtected){ exit 1 };' +
    'if(-not @($a.Access)){ exit 1 };' +
    'foreach($ace in $a.Access){' +
    '$id=$ace.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value;' +
    'if(($id -ne ''S-1-5-18'') -and ($id -ne ''S-1-5-32-544'')){ exit 1 }};' +
    'exit 0 } catch { exit 2 }"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), cmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    FailInstall('Could not read the state directory ACL; install aborted before any secret was written');
  if ResultCode = 2 then
    FailInstall('Could not read the state directory ACL; install aborted before any secret was written');
  Result := ResultCode = 0;
end;

procedure DropUntrustedState(const Dir: String; Force: Boolean);
var
  ResultCode: Integer;
  cmd, flag: String;
begin
  { After the directory-only grant, before /setowner /T. A failed delete aborts. }
  if Force then flag := '$true' else flag := '$false';
  cmd := '-NoProfile -ExecutionPolicy Bypass -Command "' +
    '$ErrorActionPreference=''Stop''; try {' +
    '$d=''' + PsLiteral(Dir) + '''; $force=' + flag + ';' +
    'foreach($n in @(''agent.json'',''machine_id'',''room.key'')){' +
    '$p=Join-Path $d $n; if(Test-Path -LiteralPath $p){' +
    '$drop=$force; if(-not $drop){' +
    '$s=(Get-Acl -LiteralPath $p).GetOwner([System.Security.Principal.SecurityIdentifier]).Value;' +
    '$drop=($s -ne ''S-1-5-18'') -and ($s -ne ''S-1-5-32-544'')};' +
    'if($drop){Remove-Item -LiteralPath $p -Force; if(Test-Path -LiteralPath $p){exit 1}}}}' +
    '} catch { exit 1 }; exit 0"';
  if (not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), cmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode))
     or (ResultCode <> 0) then
    FailInstall('Could not delete an untrusted state file; install aborted before any secret was written');
end;

procedure AssertStateParent(const Dir: String);
var
  ResultCode: Integer;
  cmd: String;
begin
  { Protected parent DACL: SYSTEM and Administrators full, Users read and execute.
    Refuse a junction on either path. Skip reset when the parent is already locked. }
  cmd := '-NoProfile -ExecutionPolicy Bypass -Command "' +
    '$ErrorActionPreference=''Stop''; try {' +
    '$d=''' + PsLiteral(Dir) + '''; $p=Split-Path -Parent $d;' +
    'function Reparse([string]$x){ $i=Get-Item -LiteralPath $x -Force -ErrorAction SilentlyContinue;' +
    'if(-not $i){ return $false }; return [bool]($i.Attributes -band [IO.FileAttributes]::ReparsePoint) };' +
    'if(Reparse $p){ exit 4 }; if(Reparse $d){ exit 4 };' +
    'if(-not (Test-Path -LiteralPath $p)){ New-Item -ItemType Directory -Force -Path $p | Out-Null };' +
    'if(Reparse $p){ exit 4 }; if(Reparse $d){ exit 4 };' +
    'function ParentLocked([string]$x){' +
    '$a=Get-Acl -LiteralPath $x; $s=$a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value;' +
    'if(($s -ne ''S-1-5-18'') -and ($s -ne ''S-1-5-32-544'')){ return $false };' +
    'if(-not $a.AreAccessRulesProtected){ return $false }; if(-not @($a.Access)){ return $false };' +
    '$bad=0x2 -bor 0x4 -bor 0x10 -bor 0x40 -bor 0x100 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x10000000 -bor 0x40000000;' +
    'foreach($ace in @($a.Access)){' +
    '$id=$ace.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value;' +
    'if(($id -eq ''S-1-5-18'') -or ($id -eq ''S-1-5-32-544'')){ continue };' +
    'if(($id -eq ''S-1-5-32-545'') -and ($ace.AccessControlType -eq ''Allow'') -and (([int]$ace.FileSystemRights -band $bad) -eq 0)){ continue };' +
    'return $false }; return $true };' +
    '$locked=$false; try { $locked=ParentLocked $p } catch { $locked=$false };' +
    'if(-not $locked){ $ErrorActionPreference=''Continue'';' +
    '& icacls.exe $p /setowner *S-1-5-32-544 | Out-Null; if($LASTEXITCODE -ne 0){ exit 5 };' +
    '& icacls.exe $p /reset | Out-Null; if($LASTEXITCODE -ne 0){ exit 5 };' +
    '& icacls.exe $p /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F *S-1-5-32-545:(OI)(CI)RX | Out-Null;' +
    'if($LASTEXITCODE -ne 0){ exit 5 }; $ErrorActionPreference=''Stop'' };' +
    'if(-not (ParentLocked $p)){ exit 5 };' +
    'if(Reparse $p){ exit 4 }; if(Reparse $d){ exit 4 }; exit 0' +
    '} catch { exit 2 }"';
  if (not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), cmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode))
     or (ResultCode <> 0) then
    FailInstall('State directory parent is not safe; install aborted before any secret was written');
end;

procedure AssertNoChildReparse(const Dir: String);
var
  ResultCode: Integer;
  cmd: String;
begin
  { After the directory grant and before any /T. Do not follow a junction. }
  cmd := '-NoProfile -ExecutionPolicy Bypass -Command "' +
    '$ErrorActionPreference=''Stop''; try {' +
    '$q=New-Object System.Collections.Generic.Queue[string];' +
    '$q.Enqueue(''' + PsLiteral(Dir) + ''');' +
    'while($q.Count -gt 0){ $cur=$q.Dequeue();' +
    'foreach($c in @(Get-ChildItem -Force -LiteralPath $cur -ErrorAction SilentlyContinue)){' +
    'if($c.Attributes -band [IO.FileAttributes]::ReparsePoint){ exit 4 };' +
    'if($c.PSIsContainer){ $q.Enqueue($c.FullName) }}}; exit 0' +
    '} catch { exit 2 }"';
  if (not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), cmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode))
     or (ResultCode <> 0) then
    FailInstall('A reparse point is under the state directory; install aborted before any secret was written');
end;

procedure LockStateDir(const Dir: String);
var
  FindRec: TFindRec;
  hasChild, wasLocked: Boolean;
begin
  AssertStateParent(Dir);
  if not DirExists(Dir) then
    if not ForceDirectories(Dir) then
      FailInstall('Could not create the state directory');
  wasLocked := StateDirWasLocked(Dir);
  if not wasLocked then
  begin
    if not RunIcacls('"' + Dir + '" /setowner *S-1-5-32-544') then
      FailInstall('icacls /setowner failed; install aborted before any secret was written');
    if not RunIcacls('"' + Dir + '" /reset') then
      FailInstall('icacls /reset failed; install aborted before any secret was written');
    if not RunIcacls('"' + Dir + '" /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F') then
      FailInstall('icacls grant failed; install aborted before any secret was written');
  end;
  if not StateDirWasLocked(Dir) then
    FailInstall('state directory ACL is not limited to SYSTEM and Administrators');
  AssertNoChildReparse(Dir);
  DropUntrustedState(Dir, False);
  if not wasLocked then
  begin
    if not RunIcacls('"' + Dir + '" /setowner *S-1-5-32-544 /T /C') then
      FailInstall('icacls /setowner failed; install aborted before any secret was written');
    hasChild := FindFirst(AddBackslash(Dir) + '*', FindRec);
    if hasChild then
      FindClose(FindRec);
    if hasChild then
      if not RunIcacls('"' + Dir + '\*" /reset /T /C') then
        FailInstall('icacls /reset failed; install aborted before any secret was written');
    if not RunIcacls('"' + Dir + '" /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F /T /C') then
      FailInstall('icacls grant failed; install aborted before any secret was written');
    if not StateDirWasLocked(Dir) then
      FailInstall('state directory ACL is not limited to SYSTEM and Administrators');
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  src, dir, params, pair, legacy: String;
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;
  src := CmdParam('/ROOMKEYFILE=');
  if (src = '') and FileExists(ExpandConstant('{src}\room.key')) then
    src := ExpandConstant('{src}\room.key');
  dir := ExpandConstant('{commonappdata}\ChatX\fleet');
  { Sample before LockStateDir. An unlocked agent.json is copied so bootstrap can
    rewrite it after the lock drops the original. }
  legacy := '';
  if DirExists(dir) and FileExists(dir + '\agent.json') then
    if not StateDirWasLocked(dir) then
    begin
      legacy := ExpandConstant('{tmp}\chatx-agent-migrate.json');
      if not FileCopy(dir + '\agent.json', legacy, False) then
        FailInstall('Could not snapshot agent.json before locking the state directory');
    end;
  LockStateDir(dir);
  { room.key is copied only after icacls succeeded. LockStateDir raises otherwise. }
  if (src <> '') and FileExists(src) then
    FileCopy(src, dir + '\room.key', False);
  WizardForm.StatusLabel.Caption := 'Connecting to the controller...';
  params := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\bootstrap.ps1') +
            '" -Controller "' + GetController('') + '"';
  if legacy <> '' then
    params := params + ' -Snapshot "' + legacy + '"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    ResultCode := 1;
  { Any non-zero bootstrap code must fail a silent install. A suppressed message box exits 0. }
  if ResultCode <> 0 then
  begin
    if WizardSilent then
      ExitProcess(1);
    if ResultCode = 3 then
      RaiseException('Could not lock the fleet state directory. Install aborted before any secret was written.');
    SuppressibleMsgBox('The agent files were copied, but setup did not finish. The service was not installed.', mbError, MB_OK, IDOK);
  end;
  pair := '';
  if LoadStringFromFile(dir + '\pairing.txt', pair) then
    WizardForm.FinishedLabel.Caption :=
      'Pairing code ' + Trim(pair) + '. An admin approves this PC in the fleet console, which shows the same code.';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    StopOurAgent();
    Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /TN "ChatX Fleet Agent Upgrade" /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
  { State dir is kept unless the uninstall is started with /REMOVESTATE=1. }
  if (CurUninstallStep = usPostUninstall) and (CmdParam('/REMOVESTATE=') = '1') then
    DelTree(ExpandConstant('{commonappdata}\ChatX\fleet'), True, True, True);
end;
