@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
if errorlevel 1 echo Uyari: bagimliliklar tam kurulamadi, yine de deneniyor.
python main.py
if errorlevel 1 (
  echo.
  echo Program hata ile kapandi. data\radyo.log dosyasina bakin.
  pause
)
