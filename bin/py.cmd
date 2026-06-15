@echo off
setlocal

set "PROJECT_ROOT=%~dp0.."
set "PORTABLE_PY=%PROJECT_ROOT%\python\python.exe"
set "LOCAL_PY=D:\app\conda\python.exe"

if exist "%PORTABLE_PY%" (
    "%PORTABLE_PY%" %*
    exit /b %ERRORLEVEL%
)

if exist "%LOCAL_PY%" (
    "%LOCAL_PY%" %*
    exit /b %ERRORLEVEL%
)

python %*
exit /b %ERRORLEVEL%
