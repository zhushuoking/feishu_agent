@echo off
chcp 936 >nul
cd /d "%~dp0"

set "NO_PROXY=api.deepseek.com,dashscope.aliyuncs.com,open.bigmodel.cn,localhost,127.0.0.1"
set "no_proxy=%NO_PROXY%"

if not exist ".venv\Scripts\activate.bat" (
    echo 还没初始化环境，请先运行 setup.bat
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

echo.
echo 按提示选择模型服务并粘贴 API Key
echo.
python set_model.py

echo.
pause
