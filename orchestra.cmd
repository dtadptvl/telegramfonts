@echo off
setlocal EnableExtensions DisableDelayedExpansion

if /I not "%~1"=="execute" (
  echo {"protocol":"orchestra/executor/v1","terminal":"STOP","reason":"unsupported_command"}
  exit /b 2
)

set "ORCH_ROOT=%~dp0"
set "ORCH_PYTHON="
set "ORCH_PYTHON_ARGS="

if defined ORCHESTRA_PYTHON if exist "%ORCHESTRA_PYTHON%" (
  set "ORCH_PYTHON=%ORCHESTRA_PYTHON%"
)
if not defined ORCH_PYTHON if exist "%ORCH_ROOT%.venv\Scripts\python.exe" (
  set "ORCH_PYTHON=%ORCH_ROOT%.venv\Scripts\python.exe"
)
if not defined ORCH_PYTHON for /f "delims=" %%P in ('where.exe py.exe 2^>nul') do if not defined ORCH_PYTHON (
  set "ORCH_PYTHON=%%P"
  set "ORCH_PYTHON_ARGS=-3"
)
if not defined ORCH_PYTHON for /f "delims=" %%P in ('where.exe python.exe 2^>nul') do if not defined ORCH_PYTHON (
  set "ORCH_PYTHON=%%P"
)

if not defined ORCH_PYTHON goto :python_unavailable
"%ORCH_PYTHON%" %ORCH_PYTHON_ARGS% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :python_unavailable

"%ORCH_PYTHON%" %ORCH_PYTHON_ARGS% "%ORCH_ROOT%.orchestra\executor_launcher.py" %*
exit /b %errorlevel%

:python_unavailable
echo {"protocol":"orchestra/executor/v1","terminal":"STOP","reason":"python_unavailable"}
exit /b 2
