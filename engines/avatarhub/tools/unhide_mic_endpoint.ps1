param(
  # 友好名/产品名子串（默认 PD100X）；匹配 Capture 端点后恢复其可见性
  [string]$NameMatch = 'PD100X'
)
# 恢复被隐藏的录音端点（DeviceState 带 0x10000000 隐藏位 → MME/DirectSound/WASAPI 全部不枚举，
# 仅 WDM-KS 可见，表现为「设备从麦克风下拉里消失但系统里还在」）。
# 隐藏通常来自 IPolicyConfig::SetEndpointVisibility(id,0)（部分声卡工具/系统设置会触发）。
# 本脚本用同一接口把可见性还原为 1，立即生效，无需重启 AudioSrv。

$base = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture'
$prodKey = '{b3f8fa53-0004-438e-9003-51a46e139bfc},6'
$nameKey = '{a45c254e-df1c-4efd-8020-67d146a850e0},2'
$targets = @()
Get-ChildItem $base -ErrorAction SilentlyContinue | ForEach-Object {
  $props = Get-ItemProperty ($_.PSPath + '\Properties') -ErrorAction SilentlyContinue
  $prod  = $props.$prodKey
  $name  = $props.$nameKey
  $state = (Get-ItemProperty $_.PSPath).DeviceState
  if (($prod -like "*$NameMatch*") -or ($name -like "*$NameMatch*")) {
    $targets += [pscustomobject]@{ Guid=$_.PSChildName; Prod=$prod; State=$state }
  }
}
if (-not $targets) { Write-Output "NOTFOUND: no capture endpoint matching '$NameMatch'"; exit 2 }

$cs = @"
using System;
using System.Runtime.InteropServices;
[Guid("f8679f50-850a-41cf-9c72-430f290290c8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IPolicyConfig {
  int GetMixFormat(string id, IntPtr p);
  int GetDeviceFormat(string id, int b, IntPtr p);
  int ResetDeviceFormat(string id);
  int SetDeviceFormat(string id, IntPtr a, IntPtr b);
  int GetProcessingPeriod(string id, int b, IntPtr a, IntPtr c);
  int SetProcessingPeriod(string id, IntPtr a);
  int GetShareMode(string id, IntPtr a);
  int SetShareMode(string id, IntPtr a);
  int GetPropertyValue(string id, int b, IntPtr key, IntPtr val);
  int SetPropertyValue(string id, int b, IntPtr key, IntPtr val);
  int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)] string id, int role);
  int SetEndpointVisibility([MarshalAs(UnmanagedType.LPWStr)] string id, int v);
}
[ComImport, Guid("870af99c-171d-4f9e-af0d-e63df40c2bc9")]
public class CPolicyConfigClient { }
public static class MicUnhide {
  public static int Show(string id) {
    IPolicyConfig pc = (IPolicyConfig)(new CPolicyConfigClient());
    return pc.SetEndpointVisibility(id, 1);
  }
}
"@
Add-Type -TypeDefinition $cs -Language CSharp

foreach ($t in $targets) {
  $id = '{0.0.1.00000000}.' + $t.Guid
  $before = '0x{0:X}' -f $t.State
  if (($t.State -band 0x10000000) -eq 0) {
    Write-Output ("SKIP (not hidden): " + $t.Prod + " state=" + $before)
    continue
  }
  $rc = [MicUnhide]::Show($id)
  Start-Sleep -Milliseconds 400
  $after = '0x{0:X}' -f (Get-ItemProperty ($base + '\' + $t.Guid)).DeviceState
  Write-Output ("UNHIDE " + $t.Prod + " rc=" + $rc + " state " + $before + " -> " + $after)
}
