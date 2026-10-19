@echo off
rem Start the mail triage UI: double-click to run.
rem Steps: go to the script's folder -> check GEMINI_API_KEY -> activate .venv -> python app.py
rem The window always ends with pause, whether it succeeds or fails, so it does not close by itself.
rem Note: this file contains only ASCII characters, so it displays correctly in any Windows console code page.

rem Go to the script's folder (fetch_mail.py and others read accounts.json and tokens\ by relative path)
cd /d "%~dp0"

rem Only check whether the environment variable exists; never show its value
if not defined GEMINI_API_KEY goto no_key

if not exist ".venv\Scripts\activate.bat" goto no_venv

call ".venv\Scripts\activate.bat"
echo Starting the mail triage UI. Once it starts, open the http://127.0.0.1:port address shown below in your browser.
echo To close the UI: just close this window, or press Ctrl+C.
echo.
python app.py
if errorlevel 1 goto app_failed
goto end

:no_key
echo [ERROR] The environment variable GEMINI_API_KEY was not found; startup stopped.
echo Create the user variable GEMINI_API_KEY under "Edit environment variables for your account" in Windows,
echo then close this window and double-click start.bat again.
goto end

:no_venv
echo [ERROR] The virtual environment .venv\Scripts\activate.bat was not found; startup stopped.
echo Make sure start.bat and the .venv folder are in the same project folder.
goto end

:app_failed
echo.
echo [ERROR] app.py exited abnormally; see the error messages above.
goto end

:end
echo.
pause
