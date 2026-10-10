@echo off
REM Build dist\Downpour\ (Downpour.exe, downpour-cli.exe, extension\).
REM Needs the dev tools:  py -3 -m pip install pyinstaller wxPython
setlocal
cd /d "%~dp0"
set PY=.venv\Scripts\python.exe
if not exist %PY% set PY=python
REM Close copies running from this build's folder only, never an installed Downpour.
powershell -NoProfile -Command "Get-Process Downpour,downpour-cli -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '%~dp0dist\*' } | Stop-Process -Force" >nul 2>&1
if not exist downpour\vendor\nvdaControllerClient.dll %PY% tools\fetch_nvda_client.py || exit /b 1
%PY% tools\version_info.py || exit /b 1
%PY% -m PyInstaller downpour.spec --noconfirm --distpath dist --workpath build\pyinstaller || exit /b 1
if exist extension xcopy extension dist\Downpour\extension\ /E /I /Y /Q >nul
copy /Y README.md dist\Downpour\ >nul
echo.
echo Built dist\Downpour\Downpour.exe
