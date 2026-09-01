@echo off
rem FireRed 部署入口（schtasks 用：独立会话跑，不随 ssh 断；免 /TR 引号地狱）
powershell -ExecutionPolicy Bypass -File C:\firered\deploy_104.ps1
