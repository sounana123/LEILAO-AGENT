@echo off
cd /d "%~dp0"
start "Auction scanner" cmd /k python -m uvicorn app:app --host 127.0.0.1 --port 8000
timeout /t 4 >nul
start "" http://127.0.0.1:8000/leilao
