@echo off
py -3 "%~dp0manage.py" %*
exit /b %errorlevel%
