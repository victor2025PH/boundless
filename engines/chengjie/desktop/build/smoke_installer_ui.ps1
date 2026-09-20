# smoke_installer_ui.ps1 -- interactive (wizard) end-to-end run of the ChatX installer
# and uninstaller inside an isolated throwaway local user, with a PNG of every page.
#
# WHY: smoke_uninstall.ps1 covers the SILENT channels; the wizard pages (welcome /
# notice / directory / finish, uninstall welcome / data disposition / finish) were
# only ever compile-checked until 2026-09-05. Running the real installer on the build
# machine's own account is forbidden (it would replace the operator's live seat app),
# so this reuses the smoke_uninstall isolation model: a plain (non-admin) local user
# whose %APPDATA%/HKCU are separate and who cannot touch the operator's processes.
#
# HOW: pages are driven without stealing focus (BM_CLICK to the wizard's Next button,
# radio/checkbox found by caption) and captured with PrintWindow (works even when the
# window is covered). The app itself is NEVER launched -- the installer is killed on
# its finish page -- so no telemetry/device side effects. The uninstall run takes the
# "erase all data" branch so the wipe-result finish page is exercised too.
#
# ASCII-only on purpose (PS5.1-GBK lesson): CJK captions are built from char codes.
# Needs admin (creates/removes the local user). Not wired into predist; run by hand
# after touching build/installer.nsh, then look at the PNGs:
#   powershell -ExecutionPolicy Bypass -File build\smoke_installer_ui.ps1 -Setup <ChatX-Setup-x.y.z.exe> [-OutDir dir] [-KeepUser]
# Exit 0 = every page reached, 1 = a step failed (see e2e_error_state.png), 2 = setup problem.
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Setup,
  [string]$OutDir = (Join-Path $env:TEMP 'chatx_installer_ui_smoke'),
  [switch]$KeepUser
)
$ErrorActionPreference = 'Stop'
function Say($m) { Write-Output ("[smoke-ui] " + $m) }
function CJK([int[]]$codes) { return [string]::new([char[]]$codes) }
# captions the walk needs to recognise (zh / en); ASCII source, CJK at runtime
$CAP_ERASE   = CJK @(0x5F7B, 0x5E95, 0x5220, 0x9664)   # radio "erase all data"
$CAP_CONFIRM = CJK @(0x6211, 0x5DF2, 0x4E86, 0x89E3)   # checkbox "I understand"
$RX_FINISH   = (CJK @(0x5B8C, 0x6210)) + '|Finish'     # finish button caption

if (-not (Test-Path $Setup)) { Say "setup not found: $Setup"; exit 2 }
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null

Add-Type -AssemblyName System.Drawing
Add-Type @"
using System; using System.Text; using System.Runtime.InteropServices;
public class SmokeUi {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr parent, EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  [DllImport("user32.dll")] public static extern IntPtr GetDlgItem(IntPtr h, int id);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern bool IsWindowEnabled(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  public static int[] Rect(IntPtr h) { RECT r; GetWindowRect(h, out r); return new int[] { r.L, r.T, r.R, r.B }; }
  public static string Text(IntPtr h) { var sb = new StringBuilder(512); GetWindowText(h, sb, 512); return sb.ToString(); }
  public static IntPtr FindByText(IntPtr parent, string part) {
    IntPtr found = IntPtr.Zero;
    EnumChildWindows(parent, (h, l) => { if (IsWindowVisible(h) && Text(h).Contains(part)) { found = h; return false; } return true; }, IntPtr.Zero);
    return found;
  }
}
"@
$BM_CLICK = 0x00F5

# ---- test user (same lifecycle as smoke_uninstall.ps1) ---------------------------
$user = 'cxuiprobe'
$pass = 'Cx!' + [guid]::NewGuid().ToString('N').Substring(0, 11)
$cred = New-Object pscredential ($user, (ConvertTo-SecureString $pass -AsPlainText -Force))
function Remove-TestUser {
  $prof = Get-CimInstance Win32_UserProfile -ErrorAction SilentlyContinue | Where-Object { $_.LocalPath -like ("*\" + $user) }
  if ($prof) { $prof | Remove-CimInstance -ErrorAction SilentlyContinue }
  try { Remove-LocalUser -Name $user -ErrorAction Stop } catch { }
  $orphan = Join-Path (Split-Path $env:USERPROFILE -Parent) $user
  if (Test-Path $orphan) { Remove-Item $orphan -Recurse -Force -ErrorAction SilentlyContinue }
}
Remove-TestUser
try {
  New-LocalUser -Name $user -Password (ConvertTo-SecureString $pass -AsPlainText -Force) -Description 'ChatX installer UI smoke (auto-managed)' -ErrorAction Stop | Out-Null
  Add-LocalGroupMember -Group (Get-LocalGroup -SID 'S-1-5-32-545') -Member $user -ErrorAction Stop
} catch { Say ("cannot create test user (need admin): " + $_.Exception.Message); exit 2 }
Say "test user created: $user"
# the test user may lack read ACLs on the dev tree; stage under Public
$stage = Join-Path $env:PUBLIC 'cx_ui_smoke_setup.exe'
Copy-Item $Setup $stage -Force
$tProfile = Join-Path (Split-Path $env:USERPROFILE -Parent) $user
$tApp     = Join-Path $tProfile 'AppData\Local\Programs\telegram-ai-desktop'
$tPkgData = Join-Path $tProfile 'AppData\Roaming\telegram-ai-desktop'

# ---- wizard driving helpers --------------------------------------------------------
$script:proc = $null
function WinH() { $script:proc.Refresh(); return $script:proc.MainWindowHandle }
function WaitWindow([int]$sec) {
  for ($i = 0; $i -lt $sec * 2; $i++) {
    Start-Sleep -Milliseconds 500; $script:proc.Refresh()
    if ($script:proc.HasExited) { return $false }
    if ($script:proc.MainWindowHandle -ne 0) { return $true }
  }
  return $false
}
function Shot($name) {
  Start-Sleep -Milliseconds 700
  $h = WinH; $a = [SmokeUi]::Rect($h); $w = $a[2] - $a[0]; $hh = $a[3] - $a[1]
  if ($w -le 0) { Say ("shot {0}: bad rect" -f $name); return }
  $bmp = New-Object System.Drawing.Bitmap $w, $hh
  $g = [System.Drawing.Graphics]::FromImage($bmp); $hdc = $g.GetHdc()
  [SmokeUi]::PrintWindow($h, $hdc, 2) | Out-Null
  $g.ReleaseHdc($hdc); $g.Dispose()
  $bmp.Save((Join-Path $OutDir ("e2e_" + $name + ".png"))); $bmp.Dispose()
  Say ("shot {0}  next='{1}'" -f $name, [SmokeUi]::Text([SmokeUi]::GetDlgItem($h, 1)))
}
function ClickNext() {
  $btn = [SmokeUi]::GetDlgItem((WinH), 1)
  Say ("click next '" + [SmokeUi]::Text($btn) + "'")
  [SmokeUi]::SendMessage($btn, $BM_CLICK, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null
  Start-Sleep -Milliseconds 1800
}
function ClickByText($part) {
  $h = [SmokeUi]::FindByText((WinH), $part)
  if ($h -eq [IntPtr]::Zero) { throw ("control not found by caption: " + $part) }
  [SmokeUi]::SendMessage($h, $BM_CLICK, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null
  Start-Sleep -Milliseconds 600
}
function WaitNextCaption([string]$pattern, [int]$sec) {
  for ($i = 0; $i -lt $sec; $i++) {
    Start-Sleep -Seconds 1
    $script:proc.Refresh(); if ($script:proc.HasExited) { return $false }
    $btn = [SmokeUi]::GetDlgItem((WinH), 1)
    if (([SmokeUi]::Text($btn) -match $pattern) -and [SmokeUi]::IsWindowEnabled($btn)) { return $true }
  }
  return $false
}
function StartAsUser($exe, $argList) {
  if ($argList) { $script:proc = Start-Process -FilePath $exe -ArgumentList $argList -Credential $cred -LoadUserProfile -PassThru -WorkingDirectory $env:PUBLIC }
  else { $script:proc = Start-Process -FilePath $exe -Credential $cred -LoadUserProfile -PassThru -WorkingDirectory $env:PUBLIC }
}

$rc = 1
try {
  # ================= INSTALL: welcome -> notice -> directory -> install -> finish ====
  StartAsUser $stage $null
  if (-not (WaitWindow 90)) { throw "installer window did not appear" }
  Start-Sleep -Seconds 3
  Shot "i1_welcome"
  ClickNext                     # fresh profile: data page is skipped -> notice
  Shot "i2_notice"
  ClickNext                     # I understand -> directory
  Shot "i3_directory"
  ClickNext                     # Install (isolated profile)
  Start-Sleep -Seconds 5
  Shot "i4_installing"
  if (-not (WaitNextCaption $RX_FINISH 900)) { throw "install finish page not reached" }
  Shot "i5_finish"
  Say "killing installer on the finish page (the app is never launched)"
  Stop-Process -Id $script:proc.Id -Force; Start-Sleep -Seconds 2
  $un = Get-ChildItem $tApp -Filter 'Uninstall*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $un) { throw "no uninstaller in test profile ($tApp)" }
  Say ("installed: " + $un.FullName)
  # fake user data so the wipe branch has something to erase
  New-Item -ItemType Directory -Path (Join-Path $tPkgData 'data\config') -Force | Out-Null
  Set-Content -Path (Join-Path $tPkgData 'data\config\config.yaml') -Value 'smoke: 1' -Encoding ASCII

  # ================= UNINSTALL: welcome -> data (erase all) -> run -> finish =========
  StartAsUser $un.FullName @('_?=' + $tApp)
  if (-not (WaitWindow 90)) { throw "uninstaller window did not appear" }
  Start-Sleep -Seconds 3
  Shot "u1_welcome"
  ClickNext
  Shot "u2_data_default"
  ClickByText $CAP_ERASE
  ClickByText $CAP_CONFIRM
  Shot "u3_data_wipe_selected"
  ClickNext                     # "erase and uninstall"
  Start-Sleep -Seconds 3
  Shot "u4_uninstalling"
  if (-not (WaitNextCaption $RX_FINISH 300)) { throw "uninstall finish page not reached" }
  Shot "u5_finish"
  Stop-Process -Id $script:proc.Id -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2
  # under _?= the uninstaller runs in place and cannot delete its own directory;
  # the user data dir is the meaningful assertion for the wipe branch
  if (Test-Path $tPkgData) { throw "wipe branch left the package data dir behind: $tPkgData" }
  Say "wipe branch erased the test profile data dir"
  Say ("ALL PAGES REACHED -- screenshots in " + $OutDir)
  $rc = 0
} catch {
  Say ("FAIL: " + $_.Exception.Message)
  if ($script:proc -and -not $script:proc.HasExited) { try { Shot "error_state" } catch { }; Stop-Process -Id $script:proc.Id -Force -ErrorAction SilentlyContinue }
} finally {
  Remove-Item $stage -Force -ErrorAction SilentlyContinue
  if (-not $KeepUser) { Remove-TestUser; Say "test user removed" } else { Say ("test user kept: " + $user + " / " + $pass) }
}
exit $rc
