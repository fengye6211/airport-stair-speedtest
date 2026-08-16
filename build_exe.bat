@echo off
rem ============================================================
rem  一键打包 exe（单文件，双击即用，内嵌 mihomo 内核）
rem  产物: dist\AirportSpeedtest.exe
rem  说明: 首次运行较慢（需解压内置文件），杀软可能误报请加白
rem ============================================================
cd /d %~dp0
where python >nul 2>nul || (echo [!] 未找到 python，请先安装 Python 3.8+ 并加入 PATH & goto :fail)

echo [*] 安装依赖 + PyInstaller ...
python -m pip install -r requirements.txt pyinstaller || goto :fail

echo [*] 打包中（约 1~3 分钟）...
python -m PyInstaller --noconfirm --clean --onefile --console ^
  --name AirportSpeedtest ^
  --icon tools\icon.ico ^
  --add-data "tools\mihomo.exe;tools" ^
  --add-data "tools\geoip.metadb;tools" ^
  webapp.py || goto :fail

echo.
echo [+] 打包完成: dist\AirportSpeedtest.exe
echo     双击运行即打开 Web 测速界面（自动弹出浏览器）
echo     想分发给别人，只需要拷走这一个 exe 文件
pause
exit /b 0

:fail
echo.
echo [x] 打包失败，请检查上方报错信息
pause
exit /b 1
