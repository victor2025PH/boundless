# 恢复 demo_record 改动前的 Bandicam 设置(原值见 bandicam_backup.json)
$k = 'HKCU:\Software\BANDISOFT\BANDICAM\OPTION'
Set-ItemProperty $k -Name sOutputFolder -Value 'C:\Users\user\Documents\Bandicam'
Set-ItemProperty $k -Name bTargetFullScreen -Value 0
Set-ItemProperty $k -Name nTargetDisplay -Value 0
Set-ItemProperty $k -Name bTargetFullHideControl -Value 0
Set-ItemProperty $k -Name 'VideoFormat.VideoKBitrate' -Value 4000
Set-ItemProperty $k -Name 'VideoFormat.VideoQuality' -Value 80
Set-ItemProperty $k -Name 'VideoFormat.VideoFrameRate' -Value 30000
Set-ItemProperty $k -Name bVideoNoCursor -Value 1
Set-ItemProperty $k -Name bVideoClickEffects -Value 0
Set-ItemProperty $k -Name bVideoExcludeBandicamWindowsFromCapture -Value 0
# 注意:nRunAsAdmin 原值为 2(自提权),会导致脚本无法拉起 Bandicam(UAC 无人点确认)。
# 如需恢复,取消下面注释:
# Set-ItemProperty $k -Name nRunAsAdmin -Value 2
Write-Host '已恢复 Bandicam 原设置'
