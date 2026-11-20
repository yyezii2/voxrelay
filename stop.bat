@echo off
chcp 65001 >nul
title Stop VoxRelay
cd /d "%~dp0"

REM 端口不写死：交给 voice_services 按 config.json 解析。
REM 没装的组件会被跳过，不会去杀端口上别人的进程。
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0runtime\python.exe"
if not exist "%PY%" set "PY=python"

echo Stopping VoxRelay services ...
"%PY%" -c "import voice_services as v; [print('  ', s['name'], v.stop(s['name']).get('note') or '已停止') for s in v.SERVICES]"
echo Done.
timeout /t 2 >nul
