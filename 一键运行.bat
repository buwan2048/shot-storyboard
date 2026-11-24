@echo off
chcp 65001 >nul
title shot-storyboard

if "%~1"=="" goto :noarg
goto :run

:noarg
echo.
echo   Drag a video file onto this .bat and drop it.
echo.
echo   Or run:  python storyboard.py yourvideo.mp4
echo.
pause
exit /b

:run
echo.
echo   Input: %~nx1
echo.

where python >nul 2>nul
if errorlevel 1 goto :nopython

python "%~dp0storyboard.py" "%~1"
echo.
if errorlevel 1 goto :failed
echo   [DONE] Storyboard image created next to your video.
goto :end

:nopython
echo   [ERROR] Python not found.
echo   Install Python 3.8+ from https://www.python.org/downloads/
echo   Make sure "Add Python to PATH" is checked during install.
goto :end

:failed
echo   [FAILED] See the message above.
echo   Common causes: Pillow not installed, or ffmpeg not in PATH.
echo   Details: see README.md

:end
echo.
pause
