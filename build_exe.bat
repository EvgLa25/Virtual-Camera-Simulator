@echo off
cd /d "%~dp0"
REM Build VCamSim.exe and the portable folder.
REM Needs: pip install pyinstaller PySide6 PyYAML

echo === building dist\VCamSim.exe ===
python -m PyInstaller --noconfirm --clean VCamSim.spec
if errorlevel 1 goto :fail

echo.
echo === assembling binaries, source and notices (install FFmpeg separately) ===
python tools\make_portable.py --zip
if errorlevel 1 goto :fail

echo.
echo Done.
echo   dist\VCamSim.exe             single file, needs ffmpeg on PATH
echo   dist\VCamSim-portable\       copy this folder to any Windows PC
echo   dist\VCamSim-portable.zip    same, zipped for sending
pause
exit /b 0

:fail
echo BUILD FAILED
pause
exit /b 1
