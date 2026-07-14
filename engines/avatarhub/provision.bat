@echo off
rem ==========================================
rem  Environment provisioning / health-check launcher.
rem    provision.bat            -> health check (read-only)
rem    provision.bat --create   -> create missing conda envs from requirements\
rem  ASCII-only on purpose (encoding-proof).
rem ==========================================
call "%~dp0env_config.bat"

set "PROV_PY=%FACEFUSION_PY%"
if not exist "%PROV_PY%" set "PROV_PY=python"

"%PROV_PY%" "%~dp0provision.py" %*
