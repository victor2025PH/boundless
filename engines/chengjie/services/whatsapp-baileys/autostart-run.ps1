# autostart-run.ps1 — 计划任务 WhatsApp-Baileys-Service 的启动壳（用 -File 调用，避免 -Command 内联
#   env + `& '路径'` 的引号/隐藏窗口脆弱性——那正是任务启动失败 0xFFFFFFFF 的诱因之一）。
# 作用：注入本机在跑实例的数据根(AITR_DATA_DIR，供 start.ps1 解析该实例的入站 token)，再调 start.ps1。
# 换实例/换机：改下面 DataDir 默认值，或 install-autostart.ps1 -DataDir 覆盖后重装任务。
param([string]$DataDir = "D:\chengjie-instances\zhiliao\data")
$ErrorActionPreference = "Stop"
$env:AITR_DATA_DIR = $DataDir
& (Join-Path $PSScriptRoot "start.ps1")
