@echo off
rem Programi konsolsuz (pythonw) baslatir. .venv yoksa once kurulum.bat calisir.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
  echo Sanal ortam yok, kurulum yapiliyor...
  call "%~dp0kurulum.bat"
  if errorlevel 1 (
    echo Kurulum basarisiz oldu, program baslatilmadi.
    pause
    endlocal
    exit /b 1
  )
)

start "" ".venv\Scripts\pythonw.exe" main.py
endlocal
exit /b 0
