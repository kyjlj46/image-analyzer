@echo off
cd /d "%~dp0"
where uv >nul 2>nul && (uv run python run.py) || (python -m pip install -r requirements.txt && python run.py)
pause
