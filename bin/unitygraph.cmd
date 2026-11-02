@echo off
rem Plugin entry point for cmd/PowerShell: runs the bundled package.
where py >nul 2>nul && (py -3 "%~dp0..\src\unitygraph\__main__.py" %* & exit /b) || python "%~dp0..\src\unitygraph\__main__.py" %*
