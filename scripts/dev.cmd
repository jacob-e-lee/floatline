@echo off
REM ---------------------------------------------------------------------------
REM Floatline - launcher for scripts\dev.py
REM
REM Why a .cmd wrapper: this machine's PowerShell execution policy is Undefined,
REM which blocks both `npm` (the .ps1 shim) and any bare `.\scripts\*.ps1` file.
REM A batch shim plus the Python runner is the only combination that works
REM without changing machine policy.
REM
REM Usage:  scripts\dev.cmd            (or double-click)
REM         scripts\dev.cmd --smoke
REM         scripts\dev.cmd --prod
REM ---------------------------------------------------------------------------
setlocal
set "RUNNER=%~dp0dev.py"

REM Prefer the Windows Python launcher pinned to 3.14 (the version this project's
REM dependencies are installed under), then any 3.x, then whatever `python` is.
py -3.14 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    py -3.14 "%RUNNER%" %*
    exit /b %ERRORLEVEL%
)

py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    py -3 "%RUNNER%" %*
    exit /b %ERRORLEVEL%
)

python "%RUNNER%" %*
exit /b %ERRORLEVEL%
