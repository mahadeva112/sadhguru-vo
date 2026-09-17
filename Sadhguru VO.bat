@echo off
setlocal EnableExtensions
title Sadhguru VO

rem ---------------------------------------------------------------------------
rem  Launcher for the Sadhguru VO GUI.
rem
rem    "Sadhguru VO.bat"            launch with no console window
rem    "Sadhguru VO.bat" --debug    launch with the console attached, so
rem                                 tracebacks and status output stay visible
rem
rem  To pin to the taskbar, use "Sadhguru VO.lnk" instead (run setup_windows.bat
rem  or make_shortcut.ps1 to create it) - Windows will not pin a .bat directly.
rem ---------------------------------------------------------------------------

cd /d "%~dp0"

set "VPYW=%~dp0.venv\Scripts\pythonw.exe"
set "VPY=%~dp0.venv\Scripts\python.exe"

rem --- no venv yet? point at setup instead of failing cryptically ------------
if not exist "%VPY%" (
    echo [ERROR] No virtual environment found at .venv
    echo.
    echo Run setup_windows.bat first - it creates .venv and installs the
    echo dependencies listed in requirements.txt.
    echo.
    pause
    exit /b 1
)

rem --- debug mode: keep the console so errors are readable -------------------
if /i "%~1"=="--debug" (
    echo Running in debug mode - console output stays visible.
    echo.
    "%VPY%" "%~dp0app.py"
    echo.
    echo [exit code %errorlevel%]
    pause
    exit /b %errorlevel%
)

rem --- normal launch: pythonw detaches so no console window lingers ----------
if exist "%VPYW%" (
    start "Sadhguru VO" "%VPYW%" "%~dp0app.py"
) else (
    rem Some Python builds ship without pythonw - fall back to python.exe.
    start "Sadhguru VO" /min "%VPY%" "%~dp0app.py"
)

exit /b 0
