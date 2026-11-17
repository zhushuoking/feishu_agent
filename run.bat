@echo off
chcp 936 >nul
cd /d "%~dp0"

rem 飞书接口必须直连，走代理会报 SSL: UNEXPECTED_EOF_WHILE_READING
set "NO_PROXY=open.feishu.cn,.feishu.cn,msg-frontier.feishu.cn,localhost,127.0.0.1"
set "no_proxy=%NO_PROXY%"

if not exist ".venv\Scripts\activate.bat" (
    echo 还没初始化环境，请先运行 setup.bat
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

if not exist ".env" (
    echo 缺少 .env，请先运行 set_cred.bat 填写凭证
    pause
    exit /b 1
)

echo ============================================
echo  长连接启动中，去测试群发一条消息试试
echo  要停止就关掉这个窗口
echo ============================================
echo.
python main.py

echo.
echo 进程已退出，退出码 %errorlevel%
pause
