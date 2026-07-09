@echo off
setlocal EnableDelayedExpansion
title Jarvis Local Agent
cd /d C:\Jarvis

echo ============================================
echo  Jarvis Local Agent - Starting Up
echo ============================================

REM --- Check virtual environment ---
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo ERROR: Virtual environment not found. Run setup first.
    echo Expected: C:\Jarvis\.venv\Scripts\python.exe
    echo.
    pause
    exit /b 1
)

REM --- Activate virtual environment ---
call .venv\Scripts\activate

REM --- Check Ollama availability ---
echo.
echo Checking Ollama...
curl -s -o nul -w "%%{http_code}" http://localhost:11434 > "%TEMP%\ollama_check.txt" 2>nul
set /p OLLAMA_CODE=<"%TEMP%\ollama_check.txt"
del "%TEMP%\ollama_check.txt" 2>nul
if not "!OLLAMA_CODE!"=="200" (
    echo WARNING: Ollama not detected at localhost:11434. AI features may be unavailable.
) else (
    echo Ollama is running.
)

REM --- Kill any existing process on port 8000 ---
echo.
echo Checking port 8000...
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":8000 "') do (
    echo Stopping existing process on port 8000 ^(PID %%a^)...
    taskkill /F /PID %%a >nul 2>&1
)

REM --- Start Jarvis server in background ---
echo.
echo Starting Jarvis server on 127.0.0.1:8000...
start /B "" uvicorn app:app --host 127.0.0.1 --port 8000 --log-level warning

REM --- Wait for server to be ready (up to 30 seconds) ---
echo Waiting for Jarvis to be ready...
set READY=0
for /L %%i in (1,1,30) do (
    if "!READY!"=="0" (
        timeout /t 1 /nobreak >nul 2>&1
        curl -s -o nul -w "%%{http_code}" http://127.0.0.1:8000/health > "%TEMP%\jarvis_health.txt" 2>nul
        set /p HEALTH_CODE=<"%TEMP%\jarvis_health.txt"
        del "%TEMP%\jarvis_health.txt" 2>nul
        if "!HEALTH_CODE!"=="200" (
            set READY=1
            echo Jarvis is ready after %%i second^(s^).
        )
    )
)

if "!READY!"=="0" (
    echo.
    echo ERROR: Jarvis did not start within 30 seconds.
    echo Check the output above for errors.
    echo.
    pause
    exit /b 1
)

REM --- Open browser ---
echo.
echo Opening Jarvis UI in browser...
start http://127.0.0.1:8000/ui

echo.
echo ============================================
echo  Jarvis is running at http://127.0.0.1:8000
echo  Press Ctrl+C to stop.
echo ============================================
echo.

REM --- Keep terminal visible ---
pause
