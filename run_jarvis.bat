@echo off
title Jarvis Local Agent
cd /d C:\Jarvis
call .venv\Scripts\activate
uvicorn app:app --host 127.0.0.1 --port 8000
pause