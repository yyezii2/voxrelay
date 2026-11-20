@echo off
chcp 65001 >nul
title VoxRelay
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0runtime\python.exe"
if not exist "%PY%" set "PY=python"

echo Starting VoxRelay ...
echo 实际地址看下面这行启动日志（端口取自 config.json）
echo Press Ctrl+C to stop.
echo.
"%PY%" server.py %*
pause
