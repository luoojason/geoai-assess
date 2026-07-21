@echo off
REM HCAS network self-test. Double-click, wait ~30 seconds, screenshot this window.
REM Read-only: it changes nothing on this computer.
setlocal
echo ================================================
echo   HCAS network self-test
echo ================================================
echo Testing the 5 servers the app needs. About 30 seconds.
echo.

where curl.exe >nul 2>nul
if errorlevel 1 (
  echo curl.exe was not found. This needs Windows 10 or newer.
  pause
  exit /b 1
)

call :check https://data.cdc.gov "CDC health data (data.cdc.gov)"
call :check https://overpass-api.de/api/status "OpenStreetMap main (overpass-api.de)"
call :check https://overpass.kumi.systems/api/status "OpenStreetMap backup (kumi.systems)"
call :check https://cdn.jsdelivr.net "Map drawing library (cdn.jsdelivr.net)"
call :check https://openrouter.ai "AI provider (openrouter.ai)"

echo.
echo Done. Please screenshot this whole window and send it back.
pause
exit /b 0

:check
curl.exe -sS -m 12 -o NUL %1 2>nul
set rc=%errorlevel%
if "%rc%"=="0"  echo   OK      %~2
if "%rc%"=="6"  echo   FAILED  %~2 - DNS lookup failed: this computer cannot find the server's address
if "%rc%"=="7"  echo   FAILED  %~2 - connection blocked: address found but unreachable
if "%rc%"=="28" echo   FAILED  %~2 - timed out
if not "%rc%"=="0" if not "%rc%"=="6" if not "%rc%"=="7" if not "%rc%"=="28" echo   FAILED  %~2 - error code %rc%
exit /b 0
