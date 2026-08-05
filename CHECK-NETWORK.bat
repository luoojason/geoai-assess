@echo off
REM HCAS network self-test. Double-click, wait ~30 seconds, screenshot this window.
REM Changes no settings. It writes one small scratch file under %TEMP% to capture
REM each server's HTTP status code, and deletes it before it finishes.
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

set "STATUSFILE=%TEMP%\hcas-netcheck-%RANDOM%.txt"

call :check https://data.cdc.gov "CDC health data (data.cdc.gov)"
call :check https://overpass-api.de/api/status "OpenStreetMap main (overpass-api.de)"
call :check https://overpass.kumi.systems/api/status "OpenStreetMap backup (kumi.systems)"
call :check https://cdn.jsdelivr.net "Map drawing library (cdn.jsdelivr.net)"
call :check https://openrouter.ai "AI provider (openrouter.ai)"

echo.
if exist "%STATUSFILE%" del "%STATUSFILE%" >nul 2>nul
echo Done. Please screenshot this whole window and send it back.
pause
exit /b 0

:check
REM curl exits 0 for ANY completed HTTP transaction, including the 403 block page
REM a filtering proxy answers with. Branching on the exit code alone printed a
REM clean sheet of OKs on exactly the blocked networks this test exists to find,
REM so capture the HTTP status code with -w and require a 2xx before saying OK.
REM
REM IMPORTANT: every test below is a SINGLE-LINE if. Do not convert these to
REM multi-line "if ... (" blocks: %~2 expands to labels that contain parentheses
REM ("CDC health data (data.cdc.gov)"), and cmd parses the whole block before
REM running it, so the ")" inside the expanded label closes the block early and
REM the entire script dies with "- was unexpected at this time." before printing
REM a single result.
REM -L follows redirects, because a plain 3xx is NORMAL for several of these hosts
REM (cdn.jsdelivr.net 301s the bare domain). Reporting an unfollowed 3xx as a captive
REM portal sends the user chasing a network fault that does not exist. We record the
REM FINAL status and the URL we actually landed on, so a real portal - which lands you
REM on a different host - is still visible in the output a human reads.
curl.exe -sSL -m 12 -o NUL -w "%%{http_code} %%{url_effective}" %1 > "%STATUSFILE%" 2>nul
set rc=%errorlevel%
set "code="
set "landed="
if exist "%STATUSFILE%" for /f "usebackq tokens=1,2" %%A in ("%STATUSFILE%") do set "code=%%A" & set "landed=%%B"
if not defined code set "code=000"

REM --- transport-level failures: the server was never reached ---
if "%rc%"=="6"  echo   FAILED  %~2 - DNS lookup failed: this computer cannot find the server's address
if "%rc%"=="6"  exit /b 0
if "%rc%"=="7"  echo   FAILED  %~2 - connection blocked: address found but unreachable
if "%rc%"=="7"  exit /b 0
if "%rc%"=="28" echo   FAILED  %~2 - timed out
if "%rc%"=="28" exit /b 0
if "%rc%"=="35" echo   FAILED  %~2 - secure connection failed: something on this network is intercepting HTTPS
if "%rc%"=="35" exit /b 0
if "%rc%"=="56" echo   FAILED  %~2 - a proxy on this network refused the connection
if "%rc%"=="56" exit /b 0
if "%rc%"=="60" echo   FAILED  %~2 - certificate not trusted: traffic is being intercepted by a proxy or filter
if "%rc%"=="60" exit /b 0
if not "%rc%"=="0" echo   FAILED  %~2 - error code %rc%
if not "%rc%"=="0" exit /b 0

REM --- the server was reached: judge what it actually said ---
echo %code%|findstr /r "^2[0-9][0-9]$" >nul
if not errorlevel 1 echo   OK      %~2 - HTTP %code% via %landed%
if not errorlevel 1 exit /b 0
echo %code%|findstr /r "^3[0-9][0-9]$" >nul
if not errorlevel 1 echo   FAILED  %~2 - still redirecting after 50 hops, last stop %landed%: usually a captive portal or sign-in page
if not errorlevel 1 exit /b 0
if "%code%"=="407" echo   FAILED  %~2 - HTTP 407: a proxy on this network demands authentication
if "%code%"=="407" exit /b 0
if "%code%"=="429" echo   FAILED  %~2 - HTTP 429: the server is rate-limiting this network
if "%code%"=="429" exit /b 0
if "%code%"=="000" echo   FAILED  %~2 - no HTTP status was returned; the request did not complete
if "%code%"=="000" exit /b 0
echo   FAILED  %~2 - HTTP %code%: reached, but the request was refused or blocked; a web filter usually answers 403
exit /b 0
