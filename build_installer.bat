@echo off
cd /d "%~dp0"
REM Build both exes, the portable folder and the Windows installer.
REM Needs: pip install pyinstaller PySide6 PyYAML pywin32
REM        Inno Setup 6 (https://jrsoftware.org/isdl.php)

echo === building dist\VCamSim.exe (GUI) ===
python -m PyInstaller --noconfirm --clean VCamSim.spec
if errorlevel 1 goto :fail

echo.
echo === building dist\VCamSimSvc.exe (Windows service) ===
python -m PyInstaller --noconfirm --clean VCamSimSvc.spec
if errorlevel 1 goto :fail

echo.
echo === assembling binaries, source and notices (install FFmpeg separately) ===
python tools\make_portable.py --zip
if errorlevel 1 goto :fail

echo.
echo === building the installer ===
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php
    goto :fail
)
"%ISCC%" installer\VCamSim.iss
if errorlevel 1 goto :fail

echo.
echo Done.
echo   installer\Output\VCamSim-Setup-*.exe   run this on the target PC
echo   dist\VCamSim-portable\                 no-install copy
pause
exit /b 0

:fail
echo BUILD FAILED
pause
exit /b 1
