@echo off
:: Registers Forex-Daily scheduler as a Windows Task Scheduler job.
:: Run this script once as Administrator.
:: The task starts at logon and runs scheduler.py in a hidden window.

set TASK_NAME=ForexDailyScheduler
set PROJECT_DIR=%~dp0
:: Remove trailing backslash
set PROJECT_DIR=%PROJECT_DIR:~0,-1%

:: Detect python in the same environment that runs this bat
for /f "delims=" %%i in ('where python') do set PYTHON_EXE=%%i

echo Registering task: %TASK_NAME%
echo Project dir    : %PROJECT_DIR%
echo Python         : %PYTHON_EXE%

schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "\"%PYTHON_EXE%\" \"%PROJECT_DIR%\scheduler.py\" --config \"%PROJECT_DIR%\config.ini\"" ^
  /sc ONLOGON ^
  /rl HIGHEST ^
  /f

if %errorlevel% equ 0 (
    echo.
    echo Task registered successfully.
    echo To start it now without rebooting, run:
    echo   schtasks /run /tn "%TASK_NAME%"
) else (
    echo.
    echo Registration failed. Make sure you are running as Administrator.
)
pause
