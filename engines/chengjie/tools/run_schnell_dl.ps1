# schnell 下载 wrapper：exit 3(全局僵死自杀)/异常 → 循环重启续传；exit 0 完成即止。
$py = "D:\Miniconda3\envs\comfyui\python.exe"
for ($i = 0; $i -lt 60; $i++) {
    & $py -u D:\ComfyUI\download_schnell.py
    if ($LASTEXITCODE -eq 0) { Write-Output "WRAPPER: complete"; break }
    Write-Output "WRAPPER: exit=$LASTEXITCODE round=$i, restarting in 5s"
    Start-Sleep -Seconds 5
}
