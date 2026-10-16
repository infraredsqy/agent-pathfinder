@echo off
rem The shim cmd.exe and PowerShell find on PATH. `pf` would collide with cmd's own PATH builtin,
rem so the command is `pf` and this shim is what carries it on Windows.
rem Which interpreter is a probe, not a fallback: the probe picks one, that one runs `pf.py`
rem exactly once, and its exit code is the exit code of `pf` -- a command that failed must not be
rem asked to run again (`tools/bin/pf` is the same shim for sh: probe, then one exec).
setlocal
set "D=%~dp0"
where python >nul 2>&1
if %ERRORLEVEL% EQU 0 goto run_python
where py >nul 2>&1
if %ERRORLEVEL% EQU 0 goto run_py
echo pf: python not found on PATH 1>&2
exit /b 127

:run_python
python -X utf8 "%D%pf.py" %*
exit /b %ERRORLEVEL%

:run_py
py -3 -X utf8 "%D%pf.py" %*
exit /b %ERRORLEVEL%