@echo off
rem 启动邮件分拣界面：双击运行。
rem 步骤：进入脚本所在目录 → 检查 GEMINI_API_KEY → 激活 .venv → python app.py
rem 无论成功还是出错，结尾都会 pause，窗口不会自己消失。
rem 注意：本文件以 GBK（ANSI）编码保存，这是中文 Windows 命令行的默认编码；
rem       改成 UTF-8 保存会导致中文提示乱码、甚至命令被拆坏。

rem 进入脚本所在目录（fetch_mail.py 等用相对路径读 accounts.json、tokens\）
cd /d "%~dp0"

rem 只检查环境变量是否存在，不显示它的值
if not defined GEMINI_API_KEY goto no_key

if not exist ".venv\Scripts\activate.bat" goto no_venv

call ".venv\Scripts\activate.bat"
echo 正在启动邮件分拣界面，启动后在浏览器打开下面显示的 http://127.0.0.1:端口 地址。
echo 关闭界面：直接关掉这个窗口，或按 Ctrl+C。
echo.
python app.py
if errorlevel 1 goto app_failed
goto end

:no_key
echo [错误] 没有找到环境变量 GEMINI_API_KEY，已停止启动。
echo 请在 Windows 的"编辑账户的环境变量"里新建用户变量 GEMINI_API_KEY，
echo 填好后关闭本窗口，再重新双击 start.bat。
goto end

:no_venv
echo [错误] 找不到虚拟环境 .venv\Scripts\activate.bat，已停止启动。
echo 请确认 start.bat 和 .venv 文件夹在同一个项目文件夹里。
goto end

:app_failed
echo.
echo [错误] app.py 异常退出，请查看上面的错误信息。
goto end

:end
echo.
pause
