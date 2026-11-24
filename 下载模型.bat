@echo off
chcp 65001 >nul
title Download whisper model

set MODELS=%~dp0whisper-cpp\models
set FILE=%MODELS%\ggml-base.bin

if not exist "%MODELS%" mkdir "%MODELS%"

if exist "%FILE%" (
    echo.
    echo   Model already exists: %FILE%
    echo   Nothing to do.
    echo.
    pause
    exit /b
)

echo.
echo   Downloading ggml-base.bin  ^(about 141 MB^)
echo   Source: hf-mirror.com  ^(HuggingFace mirror^)
echo.

powershell -NoProfile -Command "& { $ProgressPreference='SilentlyContinue'; $dst='%FILE%'; $urls=@('https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-base.bin','https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin'); foreach($u in $urls){ try { Write-Host ('  trying ' + $u); Invoke-WebRequest -Uri $u -OutFile $dst -TimeoutSec 600 -ErrorAction Stop; if((Get-Item $dst).Length -gt 100MB){ Write-Host '  done'; break } } catch { Write-Host ('  failed: ' + $_.Exception.Message) } } }"

if exist "%FILE%" (
    for %%A in ("%FILE%") do echo   OK: %%~nxA  %%~zA bytes
    echo.
    echo   Done. Dialogue transcription is now available.
) else (
    echo.
    echo   Download failed.
    echo   Download it manually from:
    echo     https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin
    echo   and put it into:  %MODELS%
)

echo.
pause
