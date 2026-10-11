@echo off
setlocal
cd /d "%~dp0"

echo ==========================================================
echo   Agent 桌宠 · 清除 WebView2 缓存
echo ==========================================================
echo.
echo   [会清掉什么]
echo     .webview2 文件夹 —— 就是 WebView2 的浏览器缓存，
echo     通常占用 200-400MB；它只是缓存，删掉不影响设置和形象。
echo.
echo   [清完会怎样]
echo     桌宠会自动重启；缓存会在下次启动时自动重建，
echo     第一次加载会稍慢一点，属于正常现象。
echo.
echo   [更省事的入口]
echo     设置 -^> 排查 里也有「清除缓存并重启」，效果和这里一样。
echo.
set /p OK=确认要清除吗？(Y/N):
if /i not "%OK%"=="Y" exit /b 0
echo.
echo   [1/3] 正在关闭桌宠...
echo stop > "%~dp0stop.request"
ping -n 6 127.0.0.1 >nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*CodexPet*run.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" 2>nul
ping -n 2 127.0.0.1 >nul

echo   [2/3] 正在清除 WebView2 缓存...
if exist "%~dp0.webview2" (
  rmdir /s /q "%~dp0.webview2"
  echo         已清除。
) else (
  echo         没有找到缓存目录（可能已经清理过）。
)

echo   [3/3] 正在重新启动桌宠...
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0pet-launcher.ps1"
echo.
echo   完成！缓存会在下次启动时自动重建。
echo   如果桌宠没有出现，双击「启动桌宠.cmd」即可。
timeout /t 8 >nul
