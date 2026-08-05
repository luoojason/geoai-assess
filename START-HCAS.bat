@echo off
REM HCAS - one-click setup ^& launch for Windows.
REM Requires Python 3.9+ from python.org (check "Add python.exe to PATH" during install).
REM Safe to run again any time - it reuses the install.
cd /d "%~dp0"

echo ===============================
echo   HCAS - setup ^& launch
echo ===============================

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.9+ from https://www.python.org/downloads/
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
REM Anchor to start-of-line: unanchored, this matched the COMMENTED placeholder
REM "# ANTHROPIC_API_KEY=sk-ant-..." that .env.example ships with, so anyone who
REM followed the README's "copy .env.example to .env" was told a key was already
REM saved and then failed at the step-2 LLM call instead of at this prompt.
findstr /r /c:"^ANTHROPIC_API_KEY=." /c:"^OPENROUTER_API_KEY=." .env >nul 2>nul
if errorlevel 1 goto :promptkey
REM A key is already saved. Offer to keep or replace it, so a wrong/revoked key
REM is not stuck (re-running used to silently reuse it). Write stays OUTSIDE any
REM ( ) block so cmd expands %NEWKEY% at run time, not parse time.
echo.
set "NEWKEY="
set /p NEWKEY="An API key is already saved. Press Enter to keep it, or paste a different key to replace it: "
if not defined NEWKEY goto :launch
REM Drop only the old key line, keep everything else. ">.env" truncated the file,
REM silently discarding DEMO_PASSWORD / CORS_ALLOWED_ORIGINS / HOST / PORT.
if exist .env (
  findstr /v /r /c:"^ANTHROPIC_API_KEY=" .env > "%TEMP%\hcas_env.tmp" 2>nul
  move /y "%TEMP%\hcas_env.tmp" .env >nul
)
>>.env echo ANTHROPIC_API_KEY=%NEWKEY%
echo Updated.
goto :launch

:promptkey
echo.
echo The AI steps ^(2 and 4^) need an API key ^(Anthropic sk-ant-... or OpenRouter sk-or-...^).
set "KEY="
set /p KEY="Paste the API key (or press Enter to skip for now): "
if not defined KEY goto :launch
REM Same non-truncating write as above.
if exist .env (
  findstr /v /r /c:"^ANTHROPIC_API_KEY=" .env > "%TEMP%\hcas_env.tmp" 2>nul
  move /y "%TEMP%\hcas_env.tmp" .env >nul
)
>>.env echo ANTHROPIC_API_KEY=%KEY%
echo Saved.

:launch

echo.
echo Starting HCAS. Your browser will open at http://127.0.0.1:8000/hcas
echo Leave this window open while using the tool. Close it to stop.
start "" "http://127.0.0.1:8000/hcas"
python -m uvicorn backend.app:app --port 8000
pause
