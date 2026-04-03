@echo off
setlocal

set "REPO_ROOT=%~dp0.."
set "VENV_PYTHON=%REPO_ROOT%\.venv\Scripts\python.exe"

if exist "%VENV_PYTHON%" (
  set "PYTHON_EXE=%VENV_PYTHON%"
) else (
  set "PYTHON_EXE=python"
)

set "PYTHONPATH=%REPO_ROOT%\src"
pushd "%REPO_ROOT%"
"%PYTHON_EXE%" -m zoom_meeting_bot_cli %*
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
