@echo off
rem Windows launcher: installs what is missing, then serves the dashboard on 127.0.0.1:8000.
cd /d "%~dp0backend"
python -m pip install -q -r requirements.txt
if exist .env for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"
python -m playwright install msedge >nul 2>&1
python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
pause
