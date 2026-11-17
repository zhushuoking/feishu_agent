@echo off
chcp 936 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo 还没初始化环境，请先双击 setup.bat
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

echo.
echo 请按提示粘贴 App ID 和 App Secret（在开发者后台「凭证与基础信息」）
echo 右键即可粘贴，回车确认。
echo.
python set_cred.py

echo.
pause
