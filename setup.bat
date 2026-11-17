@echo off
chcp 936 >nul
cd /d "%~dp0"

echo ============================================
echo  飞书任务跟进 Agent - 环境初始化
echo ============================================

set PY311=C:\Users\48639\anaconda3\envs\tf_env2\python.exe

if not exist ".venv" (
    echo [1/3] 创建虚拟环境 Python 3.11 ...
    if exist "%PY311%" (
        "%PY311%" -m venv .venv
    ) else (
        echo      未找到 3.11 解释器，改用 PATH 里的 python
        python -m venv .venv
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
    echo      已生成 .env，接下来请填入 App ID / App Secret
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
