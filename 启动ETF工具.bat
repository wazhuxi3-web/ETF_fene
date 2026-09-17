@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHON_CMD="
set "STARTUP_LOG=%~dp0etf_startup.log"
echo [%date% %time%] bat_entered>>"%STARTUP_LOG%"

if exist "E:\Python\python.exe" set "PYTHON_CMD=E:\Python\python.exe"
if defined PYTHON_CMD goto run_python

if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON_CMD=%~dp0.venv\Scripts\python.exe"
if defined PYTHON_CMD goto run_python

where python >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=python"
if defined PYTHON_CMD goto run_python

py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3"
if defined PYTHON_CMD goto run_python

echo No usable Python 3 was found.
echo Please install Python 3 and enable the PATH option, then run this file again.
goto finish

:run_python
echo Starting ETF tool...
echo [%date% %time%] command=%PYTHON_CMD%>>"%STARTUP_LOG%"
%PYTHON_CMD% "%~dp0etf_gui.py" >>"%STARTUP_LOG%" 2>&1
set "EXIT_CODE=%errorlevel%"
echo [%date% %time%] exit_code=%EXIT_CODE%>>"%STARTUP_LOG%"
if "%EXIT_CODE%"=="0" goto finish
echo ETF tool failed to start. Details were saved to:
echo %STARTUP_LOG%

:finish
echo.
echo Press any key to close this window...
pause
endlocal
