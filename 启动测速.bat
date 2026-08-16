@echo off
title Airport Speedtest Web
cd /d "%~dp0"
echo.
echo  ============================================================
echo    Airport Speedtest Web  (StairSpeedTest style + anti-fake)
echo  ============================================================
echo.
echo  Starting web service, please wait...
echo  URL:  http://127.0.0.1:8787   (browser will open automatically)
echo  Close this window to stop the service.
echo.
python webapp.py %*
if errorlevel 1 (
  echo.
  echo  [ERROR] Failed to start. Check Python 3.8+ installed:
  echo          pip install -r requirements.txt
  echo.
  pause
)
