@echo off
chcp 936 >nul
cd /d "%~dp0"

rem 飞书接口必须直连（和 run.bat 同一个原因：代理会掐断 TLS）
set "NO_PROXY=open.feishu.cn,.feishu.cn,msg-frontier.feishu.cn,localhost,127.0.0.1"
set "no_proxy=%NO_PROXY%"

if not exist ".venv\Scripts\activate.bat" (
    echo 还没初始化环境，请先运行 setup.bat
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

echo ============================================
echo  定时提醒 Worker（每 60 秒检查一次）
echo  每天 09:00 之后生成当日日报并发私聊
echo  要停止就关掉这个窗口
echo ============================================
echo.
python worker.py

echo.
echo Worker 已退出，退出码 %errorlevel%
pause
