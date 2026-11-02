@echo off
rem Plugin entry point for cmd/PowerShell: runs the bundled script.
where py >nul 2>nul && (py -3 "%~dp0..\src\unitygraph\core.py" %* & exit /b) || python "%~dp0..\src\unitygraph\core.py" %*
