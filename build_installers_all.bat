@echo off
chcp 65001 >nul
echo ============================================
echo   随心一阅 - Build All 6 Installers
echo ============================================
echo.

set NSIS=C:\Program Files (x86)\NSIS\makensis.exe
if not exist "%NSIS%" (
    echo [ERROR] NSIS not found at "%NSIS%"
    exit /b 1
)

echo [1/7] Building client exe...
C:\Python312-32\python.exe build_client.py
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] PyInstaller build failed
    exit /b 1
)
echo.

echo [2/7] Building Win8 x64 installer...
"%NSIS%" /INPUTCHARSET UTF8 /DARCH=x64 /DWIN_VER=win8 build_installers.nsi
echo [3/7] Building Win8 x86 installer...
"%NSIS%" /INPUTCHARSET UTF8 /DARCH=x86 /DWIN_VER=win8 build_installers.nsi
echo [4/7] Building Win10 x64 installer...
"%NSIS%" /INPUTCHARSET UTF8 /DARCH=x64 /DWIN_VER=win10 build_installers.nsi
echo [5/7] Building Win10 x86 installer...
"%NSIS%" /INPUTCHARSET UTF8 /DARCH=x86 /DWIN_VER=win10 build_installers.nsi
echo [6/7] Building Win11 x64 installer...
"%NSIS%" /INPUTCHARSET UTF8 /DARCH=x64 /DWIN_VER=win11 build_installers.nsi
echo [7/7] Building Win11 x86 installer...
"%NSIS%" /INPUTCHARSET UTF8 /DARCH=x86 /DWIN_VER=win11 build_installers.nsi

echo.
echo ============================================
echo   Done! Installers in dist-installers\
echo ============================================
dir /b dist-installers\*.exe
