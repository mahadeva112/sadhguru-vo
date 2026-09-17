@echo off
setlocal EnableExtensions
title Sadhguru VO - Setup

rem ---------------------------------------------------------------------------
rem  Creates a private .venv next to this file and installs requirements.txt
rem  into it. Run this once; after that use "Sadhguru VO.bat" to launch.
rem ---------------------------------------------------------------------------

cd /d "%~dp0"

echo ============================================
echo   Sadhguru VO - Setup
echo ============================================
echo.

rem --- locate a usable Python ------------------------------------------------
rem The Windows Store alias (WindowsApps\python.exe) is a stub that opens the
rem Store when Python is not actually installed, so prefer the py launcher.
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python was not found on PATH.
    echo         Install Python 3.10+ from https://www.python.org/downloads/
    echo         and tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

echo Using: %PY%
%PY% --version
echo.

rem --- create the venv ------------------------------------------------------
if exist ".venv\Scripts\python.exe" (
    echo Virtual environment already exists - reusing .venv
) else (
    echo Creating virtual environment in .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        pause
        exit /b 1
    )
)
echo.

set "VPY=%~dp0.venv\Scripts\python.exe"

echo Upgrading pip ...
"%VPY%" -m pip install --upgrade pip
echo.

echo Installing dependencies from requirements.txt ...
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Dependency installation failed. Scroll up for the reason.
    pause
    exit /b 1
)
echo.

rem --- verify the imports the app actually needs ------------------------------
echo Verifying installation ...
"%VPY%" -c "import tkinter" 2>nul
if errorlevel 1 (
    echo [WARN] Tkinter is missing from this Python build - the GUI will not
    echo        start. Install a python.org build and re-run this script.
) else (
    echo   tkinter  OK
)
"%VPY%" -c "import pydub" 2>nul
if errorlevel 1 (
    echo [WARN] pydub failed to import - multi-chunk renders will fall back to
    echo        raw MP3 concatenation.
) else (
    echo   pydub    OK
)

rem --- ffmpeg check (pydub needs it to decode/encode) -----------------------
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo.
    echo [WARN] ffmpeg was not found on PATH. pydub needs it to join audio.
    echo        Install it with:  winget install Gyan.FFmpeg
    echo        then open a new terminal so PATH refreshes.
) else (
    echo   ffmpeg   OK
)
echo.

rem --- create the pinnable desktop/taskbar shortcut ---------------------------
echo Creating the "Sadhguru VO" shortcut ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_shortcut.ps1"
echo.

echo ============================================
echo   Setup complete.
echo ============================================
echo.
echo   Launch the app :  "Sadhguru VO.bat"  (or the new shortcut)
echo   Pin to taskbar :  right-click "Sadhguru VO.lnk" - Pin to taskbar
echo   Command line   :  .venv\Scripts\python.exe cli.py --help
echo.
pause
