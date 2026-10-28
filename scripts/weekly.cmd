@echo off
rem Weekly refresh for Windows Task Scheduler: starts Docker Desktop if needed, then `make weekly`.
rem Install / remove the schedule: see CLAUDE.md, "Weekly refresh".
setlocal
cd /d "%~dp0.."
set "PKGS=%LOCALAPPDATA%\Microsoft\WinGet\Packages"
set "PATH=%PATH%;C:\Program Files\Docker\Docker\resources\bin;%PKGS%\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe;%PKGS%\ezwinports.make_Microsoft.Winget.Source_8wekyb3d8bbwe\bin"

docker info >nul 2>&1
if errorlevel 1 (
  start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
  for /l %%i in (1,1,60) do (
    timeout /t 10 /nobreak >nul
    docker info >nul 2>&1 && goto docker_ready
  )
  if not exist logs mkdir logs
  > "logs\weekly_docker_failed.log" echo STATUS: FAILED at step 'start Docker': Docker Desktop did not start within 10 minutes [%DATE% %TIME%]
  exit /b 1
)
:docker_ready
make weekly
exit /b %errorlevel%
