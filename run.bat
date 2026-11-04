@echo off
chcp 65001 >nul
title eHRD Auto Player
cd /d "%~dp0"

if not exist "python\python.exe" (
    echo [ERROR] Portable Python not found!
    echo Please make sure the 'python' folder exists.
    pause
    exit /b 1
)

"python\python.exe" -u main.py

if %errorlevel% neq 0 (
    echo.
    echo [!] Program exited with error code: %errorlevel%
    pause
)