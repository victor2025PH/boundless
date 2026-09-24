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
  #define AppVersion "0.3.0"
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

procedure CurStepChanged(CurStep: TSetupStep);
var
  src, dir, params: String;
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;
  src := CmdParam('/ROOMKEYFILE=');
  if (src = '') and FileExists(ExpandConstant('{src}\room.key')) then
    src := ExpandConstant('{src}\room.key');
  dir := ExpandConstant('{commonappdata}\ChatX\fleet');
  ForceDirectories(dir);
  if (src <> '') and FileExists(src) then
    FileCopy(src, dir + '\room.key', False);
  WizardForm.StatusLabel.Caption := 'Connecting to the controller...';
  params := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\bootstrap.ps1') +
            '" -Controller "' + GetController('') + '"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    ResultCode := 1;
  if ResultCode <> 0 then
    SuppressibleMsgBox('The agent files were copied, but enrollment did not finish. Open "Fleet node status" from the Start menu, or run the installer again.', mbError, MB_OK, IDOK);
end;
