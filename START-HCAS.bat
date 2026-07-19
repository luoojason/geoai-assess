@echo off
REM HCAS - one-click setup ^& launch for Windows.
REM Requires Python 3.11+ from python.org (check "Add python.exe to PATH" during install).
REM Safe to run again any time - it reuses the install.
cd /d "%~dp0"

echo ===============================
echo   HCAS - setup ^& launch
echo ===============================

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.11+ from https://www.python.org/downloads/
  echo and check "Add python.exe to PATH" during installation, then run this again.
  pause
  exit /b 1
)

if not exist .venv (
  echo Creating Python environment - first run only...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
echo Installing dependencies - first run takes a few minutes...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

REM Key prompt lives OUTSIDE any ( ) block: cmd expands %KEY% when a block is
REM parsed, not when it runs, so the old block-form wrote an EMPTY key to .env.
REM Also re-prompts when .env exists but its key line is empty (that bug's leftover).
if not exist .env goto :promptkey
findstr /r /c:"ANTHROPIC_API_KEY=." /c:"OPENROUTER_API_KEY=." .env >nul 2>nul
if errorlevel 1 goto :promptkey
goto :launch

:promptkey
echo.
echo The AI steps ^(2 and 4^) need an API key ^(Anthropic sk-ant-... or OpenRouter sk-or-...^).
set "KEY="
set /p KEY="Paste the API key (or press Enter to skip for now): "
if not defined KEY goto :launch
>.env echo ANTHROPIC_API_KEY=%KEY%
echo Saved.

:launch

echo.
echo Starting HCAS. Your browser will open at http://127.0.0.1:8000/hcas
echo Leave this window open while using the tool. Close it to stop.
start "" "http://127.0.0.1:8000/hcas"
python -m uvicorn backend.app:app --port 8000
pause
