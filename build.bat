@echo off
rem Market Radyo tek dosya exe derler: dist\MarketRadyo.exe
rem Sistem Python paketlerine dokunmaz; yalnizca .venv kullanir.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Sanal ortam yok, kurulum yapiliyor...
  call "%~dp0kurulum.bat"
  if errorlevel 1 (
    echo HATA: kurulum basarisiz, derleme yapilmadi.
    pause
    endlocal
    exit /b 1
  )
)

if not exist "bin\ffmpeg.exe" (
  echo HATA: bin\ffmpeg.exe yok. ffmpeg essentials paketinden bin\ klasorune kopyalayin.
  pause
  endlocal
  exit /b 1
)

".venv\Scripts\pyinstaller.exe" MarketRadyo.spec --noconfirm --clean
if errorlevel 1 (
  echo HATA: derleme basarisiz.
  pause
  endlocal
  exit /b 1
)

echo Tamam: dist\MarketRadyo.exe
endlocal
exit /b 0
