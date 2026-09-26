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
FinishedHeadingLabel=安装完成
FinishedLabel=接下来三步：1. 把配对码发给管理员，舰队控制台里能看到同一个码  2. 管理员批准后，点「打开舰队节点」并刷新本页  3. 状态变为在线后，可从本页打开舰队控制台

[Tasks]
Name: "desktopicon"; Description: "在桌面创建「舰队节点」快捷方式"; GroupDescription: "附加图标:"; Flags: checkedonce

[Files]
Source: "{#AgentExe}"; DestDir: "{app}"; DestName: "chatx-agent.exe"; Flags: ignoreversion
Source: "{#SetupDir}\bootstrap.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SetupDir}\Open-Status.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SetupDir}\Open-Panel.vbs"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SetupDir}\fleet-node.ico"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
Type: files; Name: "{autoprograms}\Fleet node status.lnk"
Type: files; Name: "{autoprograms}\Fleet console.lnk"
Type: files; Name: "{autodesktop}\Fleet node status.lnk"
Type: files; Name: "{autodesktop}\Fleet console.lnk"

[Icons]
Name: "{autodesktop}\舰队节点"; Filename: "{sys}\wscript.exe"; Parameters: "//B ""{app}\Open-Panel.vbs"""; IconFilename: "{app}\fleet-node.ico"; Tasks: desktopicon
Name: "{autoprograms}\舰队节点"; Filename: "{sys}\wscript.exe"; Parameters: "//B ""{app}\Open-Panel.vbs"""; IconFilename: "{app}\fleet-node.ico"
Name: "{autoprograms}\舰队控制台"; Filename: "{code:GetConsoleUrl}"; IconFilename: "{app}\fleet-node.ico"

[Code]
var
  SnapshotToDelete: String;
  BootstrapOk: Boolean;
  ConsoleButton: TNewButton;

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
  { A snapshot in TEMP holds node_key. Delete it before any exit, including ExitProcess. }
  if SnapshotToDelete <> '' then
    DeleteFile(SnapshotToDelete);
  SnapshotToDelete := '';
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

procedure AssertStateParent(const Dir: String);
var
  ResultCode: Integer;
  cmd: String;
begin
  { Protected parent DACL: SYSTEM and Administrators full, Users read and execute.
    Refuse a junction on either path. Skip reset when the parent is already locked.
    icacls rights are single-quoted array elements, then splatted. A bare
    *S-1-5-18:(OI)(CI)F inside -Command is a PowerShell subexpression and exits 5. }
  cmd := '-NoProfile -ExecutionPolicy Bypass -Command "' +
    '$ErrorActionPreference=''Stop''; try {' +
    '$d=''' + PsLiteral(Dir) + '''; $p=Split-Path -Parent $d;' +
    'function Reparse([string]$x){ if(-not (Test-Path -LiteralPath $x)){ return $false };' +
    'try { $i=Get-Item -LiteralPath $x -Force -ErrorAction Stop } catch { return $true };' +
    'return [bool]($i.Attributes -band [IO.FileAttributes]::ReparsePoint) };' +
    'if(Reparse $p){ exit 4 };' +
    'if(-not (Test-Path -LiteralPath $p)){ New-Item -ItemType Directory -Force -Path $p | Out-Null };' +
    'if(Reparse $p){ exit 4 };' +
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
    '$ia=@($p, ''/setowner'', ''*S-1-5-32-544''); & icacls.exe @ia | Out-Null; if($LASTEXITCODE -ne 0){ exit 5 };' +
    '$ia=@($p, ''/reset''); & icacls.exe @ia | Out-Null; if($LASTEXITCODE -ne 0){ exit 5 };' +
    '$ia=@($p, ''/inheritance:r'', ''/grant:r'', ''*S-1-5-18:(OI)(CI)F'', ''*S-1-5-32-544:(OI)(CI)F'', ''*S-1-5-32-545:(OI)(CI)RX''); & icacls.exe @ia | Out-Null;' +
    'if($LASTEXITCODE -ne 0){ exit 5 }; $ErrorActionPreference=''Stop'' };' +
    'if(-not (ParentLocked $p)){ exit 5 };' +
    'if(Reparse $p){ exit 4 }; exit 0' +
    '} catch { exit 2 }"';
  if (not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), cmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode))
     or (ResultCode <> 0) then
    FailInstall('State directory parent is not safe; install aborted before any secret was written');
end;

function RetireUnlockedFleet(const Dir: String): Boolean;
var
  ResultCode: Integer;
  cmd: String;
begin
  { Exit 0: already locked, leave it. Exit 1: absent or renamed to fleet.legacy-<guid>. }
  cmd := '-NoProfile -ExecutionPolicy Bypass -Command "' +
    '$ErrorActionPreference=''Stop''; $d=''' + PsLiteral(Dir) + ''';' +
    'function Reparse([string]$x){ if(-not (Test-Path -LiteralPath $x)){ return $false };' +
    'try { $i=Get-Item -LiteralPath $x -Force -ErrorAction Stop } catch { return $true };' +
    'return [bool]($i.Attributes -band [IO.FileAttributes]::ReparsePoint) };' +
    'if(-not (Test-Path -LiteralPath $d)){ exit 1 };' +
    '$reparse=Reparse $d; $locked=$false;' +
    'if(-not $reparse){ try {' +
    '$a=Get-Acl -LiteralPath $d; $s=$a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value;' +
    'if((($s -eq ''S-1-5-18'') -or ($s -eq ''S-1-5-32-544'')) -and $a.AreAccessRulesProtected -and @($a.Access)){' +
    '$ok=$true; foreach($ace in @($a.Access)){' +
    '$id=$ace.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value;' +
    'if(($id -ne ''S-1-5-18'') -and ($id -ne ''S-1-5-32-544'')){ $ok=$false }}; $locked=$ok }' +
    '} catch { $locked=$false } };' +
    'if($locked){ exit 0 };' +
    '$leaf=Split-Path -Leaf $d;' +
    'Rename-Item -LiteralPath $d -NewName ($leaf + ''.legacy-'' + [guid]::NewGuid().ToString(''N'')); exit 1"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), cmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    FailInstall('Could not move the old state directory aside');
  if ResultCode = 0 then
    Result := False
  else if ResultCode = 1 then
    Result := True
  else
    FailInstall('Could not move the old state directory aside');
end;

function DirIsReparse(const Dir: String): Boolean;
var
  ResultCode: Integer;
  cmd: String;
begin
  cmd := '-NoProfile -ExecutionPolicy Bypass -Command "' +
    '$ErrorActionPreference=''Stop''; $d=''' + PsLiteral(Dir) + ''';' +
    'if(-not (Test-Path -LiteralPath $d)){ exit 4 };' +
    'try { $i=Get-Item -LiteralPath $d -Force -ErrorAction Stop } catch { exit 4 };' +
    'if($i.Attributes -band [IO.FileAttributes]::ReparsePoint){ exit 4 }; exit 0"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), cmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    FailInstall('Could not read the state directory');
  Result := ResultCode <> 0;
end;

procedure LockStateDir(const Dir: String);
begin
  AssertStateParent(Dir);
  if RetireUnlockedFleet(Dir) then
  begin
    if not ForceDirectories(Dir) then
      FailInstall('Could not create the state directory');
    if not RunIcacls('"' + Dir + '" /setowner *S-1-5-32-544') then
      FailInstall('icacls /setowner failed; install aborted before any secret was written');
    if not RunIcacls('"' + Dir + '" /reset') then
      FailInstall('icacls /reset failed; install aborted before any secret was written');
    if not RunIcacls('"' + Dir + '" /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F') then
      FailInstall('icacls grant failed; install aborted before any secret was written');
  end;
  if DirIsReparse(Dir) then
    FailInstall('state directory is a reparse point');
  if not StateDirWasLocked(Dir) then
    FailInstall('state directory ACL is not limited to SYSTEM and Administrators');
end;

procedure ApplyFinishedCopy(const PairText: String);
begin
  WizardForm.FinishedHeadingLabel.Caption := '安装完成';
  if PairText <> '' then
    WizardForm.FinishedLabel.Caption :=
      '配对码 ' + PairText + '。' + #13#10 +
      '1. 把配对码发给管理员，舰队控制台里能看到同一个码' + #13#10 +
      '2. 管理员批准后，点「打开舰队节点」并刷新本页' + #13#10 +
      '3. 状态变为在线后，可从本页打开舰队控制台'
  else
    WizardForm.FinishedLabel.Caption :=
      '1. 开机后计划任务会自动连接主控' + #13#10 +
      '2. 点「打开舰队节点」查看在线、离线或待批准' + #13#10 +
      '3. 要批准或查看其它电脑时，再打开舰队控制台';
end;

procedure OpenLocalPanel();
var
  ResultCode: Integer;
  vbs: String;
begin
  vbs := ExpandConstant('{app}\Open-Panel.vbs');
  if not Exec(ExpandConstant('{sys}\wscript.exe'), '//B "' + vbs + '"', '', SW_HIDE, ewNoWait, ResultCode) then
    ShellExec('', 'http://127.0.0.1:47321/', '', '', SW_SHOWNORMAL, ewNoWait, ResultCode);
end;

procedure OpenFleetConsoleClick(Sender: TObject);
var
  ErrorCode: Integer;
begin
  ShellExec('', GetConsoleUrl(''), '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  src, dir, params, legacy, pairText: String;
  pair: AnsiString;
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;
  BootstrapOk := False;
  src := CmdParam('/ROOMKEYFILE=');
  if (src = '') and FileExists(ExpandConstant('{src}\room.key')) then
    src := ExpandConstant('{src}\room.key');
  dir := ExpandConstant('{commonappdata}\ChatX\fleet');
  { Sample before LockStateDir. An unlocked agent.json is copied so bootstrap can
    rewrite it after the old directory is renamed aside. }
  legacy := '';
  SnapshotToDelete := '';
  try
    if DirExists(dir) and (not DirIsReparse(dir)) and FileExists(dir + '\agent.json') then
      if not StateDirWasLocked(dir) then
      begin
        legacy := ExpandConstant('{tmp}\chatx-agent-migrate.json');
        SnapshotToDelete := legacy;
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
      if legacy <> '' then
        DeleteFile(legacy);
      SnapshotToDelete := '';
      legacy := '';
      if WizardSilent then
        ExitProcess(1);
      if ResultCode = 3 then
        RaiseException('Could not lock the fleet state directory. Install aborted before any secret was written.');
      SuppressibleMsgBox('The agent files were copied, but setup did not finish. The service was not installed.', mbError, MB_OK, IDOK);
    end
    else
      BootstrapOk := True;
  finally
    if legacy <> '' then
      DeleteFile(legacy);
    SnapshotToDelete := '';
  end;
  if not BootstrapOk then
  begin
    WizardForm.FinishedHeadingLabel.Caption := '安装未完成';
    WizardForm.FinishedLabel.Caption := '文件已复制，但没有连上主控，计划任务没有安装。请用管理员身份重新运行安装程序。';
    Exit;
  end;
  pair := '';
  pairText := '';
  { LoadStringFromFile's second parameter is AnsiString on Inno Setup 6.3. Pairing codes are ASCII. }
  if LoadStringFromFile(dir + '\pairing.txt', pair) then
    pairText := Trim(String(pair));
  ApplyFinishedCopy(pairText);
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

procedure InitializeWizard();
begin
  ConsoleButton := TNewButton.Create(WizardForm);
  ConsoleButton.Parent := WizardForm;
  ConsoleButton.Caption := '打开舰队控制台';
  ConsoleButton.OnClick := @OpenFleetConsoleClick;
  ConsoleButton.Visible := False;
  ConsoleButton.Width := ScaleX(140);
  ConsoleButton.Height := WizardForm.NextButton.Height;
  ConsoleButton.Left := WizardForm.NextButton.Left - ConsoleButton.Width - ScaleX(8);
  ConsoleButton.Top := WizardForm.NextButton.Top;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  ConsoleButton.Visible := CurPageID = wpFinished;
  if CurPageID = wpFinished then
  begin
    WizardForm.NextButton.Caption := '打开舰队节点';
    ConsoleButton.Left := WizardForm.NextButton.Left - ConsoleButton.Width - ScaleX(8);
    ConsoleButton.Top := WizardForm.NextButton.Top;
    WizardForm.FinishedLabel.WordWrap := True;
    WizardForm.FinishedLabel.AutoSize := False;
    WizardForm.FinishedLabel.Height := ScaleY(160);
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpFinished then
    OpenLocalPanel();
end;
