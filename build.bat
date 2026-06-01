@echo off
title Building Frame Extractor...
echo ========================================
echo   打包抽帧工具 exe
echo ========================================
echo.

pyinstaller --noconfirm --onefile --windowed ^
    --name "抽帧工具" ^
    --icon "抽帧1.ico" ^
    --add-data "static;static" ^
    --add-data "templates;templates" ^
    --hidden-import=engineio.async_drivers.threading ^
    --hidden-import=flask_socketio ^
    --hidden-import=socketio ^
    --hidden-import=engineio ^
    server.py

echo.
echo ========================================
if exist "dist\抽帧工具.exe" (
    echo   打包成功！
    echo   输出: dist\抽帧工具.exe
) else (
    echo   打包失败，请检查上方错误信息
)
echo ========================================
pause
