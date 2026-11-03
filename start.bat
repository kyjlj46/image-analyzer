@echo off
cd /d "%~dp0"

rem 1) Use the project venv if it exists
if exist ".venv\Scripts\python.exe" goto venv

rem 2) Otherwise use uv (creates the venv automatically)
where uv >nul 2>nul && goto uvpath
if exist "%USERPROFILE%\.local\bin\uv.exe" goto uvhome

rem 3) Otherwise find a real Python (skip the Microsoft Store stub)
for %%P in (py.exe python3.exe python.exe) do (
  for /f "delims=" %%F in ('where %%P 2^>nul ^| findstr /v /i "WindowsApps"') do (
    "%%F" run.py
    goto end
  )
)
echo [ERROR] Python not found. Install it from https://www.python.org
goto end

:venv
".venv\Scripts\python.exe" run.py
goto end

:uvpath
uv run python run.py
goto end

:uvhome
"%USERPROFILE%\.local\bin\uv.exe" run python run.py
goto end

:end
pause
