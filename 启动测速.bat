@echo off
chcp 65001 >nul
title 机场节点测速 · Web 版
cd /d "%~dp0"
echo.
echo  ============================================================
echo    机场节点测速 · Web 版  (StairSpeedTest 风格 · 防失真)
echo  ============================================================
echo.
python webapp.py %*
if errorlevel 1 (
  echo.
  echo  [错误] 启动失败。请确认已安装 Python 3.8+ 并执行过:
  echo         pip install -r requirements.txt
  echo.
  pause
)
