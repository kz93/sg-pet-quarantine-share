@echo off
rem ASCII only on purpose: cmd reads .bat bytes in the OEM codepage,
rem so non-ASCII text here breaks line parsing. Chinese lives in the HTML.
setlocal
cd /d "%~dp0"

rem Actually run python instead of "where python": on a fresh Windows,
rem "where" finds the Microsoft Store stub, which exists but does not run.
set PY=
python -c "import sys" >nul 2>nul && set PY=python
if not defined PY py -3 -c "import sys" >nul 2>nul && set PY=py -3
if not defined PY goto nopython

%PY% -c "import requests, bs4" >nul 2>nul
if errorlevel 1 (
  echo First run: installing the two Python packages this tool needs ...
  %PY% -m pip install --user -r requirements.txt
  if errorlevel 1 goto pipfail
)

echo Scanning qms.avs.gov.sg ... this takes about 5-8 minutes.
echo (random 3-7s gap between requests on purpose - do not close this window)
echo.
%PY% qms_scan.py
set RC=%ERRORLEVEL%
echo.
if "%RC%"=="2" goto expired
if "%RC%"=="4" goto badconfig
if not "%RC%"=="0" goto failed
echo Done. Report opened in your browser; JSON snapshot saved next to this file.
goto end

:expired
echo No valid login session in _cookie.txt - nothing was scanned.
echo The how-to page has been opened in your browser.
goto end

:badconfig
echo config.txt has a problem - see the message above. Nothing was scanned.
goto end

:failed
echo Scan failed with code %RC%. See _probe.log for details.
goto end

:pipfail
echo Could not install packages. Try manually:  %PY% -m pip install requests beautifulsoup4
goto end

:nopython
echo Python not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" on the first installer screen.

:end
echo.
pause
endlocal
