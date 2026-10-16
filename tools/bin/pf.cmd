@echo off
rem The shim cmd.exe and PowerShell find on PATH. `pf` would collide with cmd's own PATH builtin,
rem so the command is `pf` and this shim is what carries it on Windows.
setlocal
set "D=%~dp0"
where python >nul 2>&1 && (
  python -X utf8 "%D%pf.py" %*
) || (
  py -3 -X utf8 "%D%pf.py" %*
)
exit /b %ERRORLEVEL%