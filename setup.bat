@echo off
chcp 936 >nul
cd /d "%~dp0"

echo ============================================
echo  飞书任务跟进 Agent - 环境初始化
echo ============================================

rem 想指定解释器就改下面这行，例如 set "PYTHON=C:\Python311\python.exe"
rem 留空则依次尝试 py -3.11 / python。建议 3.11 或 3.12。
set "PYTHON="

if not exist ".venv" (
    echo [1/3] 创建虚拟环境 ...
    if defined PYTHON (
        "%PYTHON%" -m venv .venv
    ) else (
        py -3.11 -m venv .venv >nul 2>&1
        if errorlevel 1 python -m venv .venv
    )
    if errorlevel 1 goto :fail
) else (
    echo [1/3] 虚拟环境已存在，跳过
)

echo [2/3] 安装依赖（清华源）...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple >nul
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 goto :fail

echo [3/3] 检查 .env ...
if not exist ".env" (
    copy .env.example .env >nul
    echo      已生成 .env，接下来请填 App ID / App Secret / 模型配置
    start "" notepad .env
) else (
    echo      .env 已存在，跳过
)

echo.
echo 初始化完成。下一步：双击 run.bat 启动长连接。
echo.
pause
exit /b 0

:fail
echo.
echo [失败] 上一步出错了，把上面的报错贴出来。
echo.
pause
exit /b 1
