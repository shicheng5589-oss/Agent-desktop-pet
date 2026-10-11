@echo off
setlocal
cd /d "%~dp0"

echo.
echo   正在桌面创建「Agent 桌宠」快捷方式...
echo   （图标是桌宠头像；桌面上的名字可以自己改）
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0pet-launcher.ps1" -CreateDesktopLink
echo.
echo   完成。想放别的位置，直接把桌面上的快捷方式拖过去即可。
ping -n 7 127.0.0.1 >nul
