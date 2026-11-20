@echo off
rem Gelistirme icin sanal ortam (.venv) kurar ve bagimliliklari yukler.
rem Hedef cihazda gerekmez: orada yalnizca MarketRadyo.exe + anonslar\ kopyalanir.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo HATA: Python bulunamadi. Python 3.10+ kurup PATH'e ekleyin.
  goto :fail
)

if not exist ".venv\Scripts\python.exe" (
  echo Sanal ortam olusturuluyor: .venv
  python -m venv .venv
  if errorlevel 1 (
    echo HATA: .venv olusturulamadi.
    goto :fail
  )
)

".venv\Scripts\python.exe" -m pip install -q -U pip
if errorlevel 1 (
  echo HATA: pip guncellenemedi. Internet baglantisini kontrol edin.
  goto :fail
)

".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo HATA: requirements.txt kurulumu basarisiz. Internet baglantisini kontrol edin.
  goto :fail
)

".venv\Scripts\python.exe" -m pip install -q -r requirements-dev.txt
if errorlevel 1 (
  echo HATA: requirements-dev.txt kurulumu basarisiz. Internet baglantisini kontrol edin.
  goto :fail
)

echo Kurulum tamam. Program: calistir.bat  Derleme: build.bat
endlocal
exit /b 0

:fail
echo.
pause
endlocal
exit /b 1
