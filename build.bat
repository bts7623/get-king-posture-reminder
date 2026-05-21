@echo off
set VER=v1.0.0
for /f "tokens=*" %%i in ('powershell -command "Get-Date -Format 'yyyyMMdd_HHmmss'"') do set TS=%%i
set NAME=posture_reminder_%VER%_%TS%

pip install pyinstaller --quiet
python -m PyInstaller --onefile --noconsole --name %NAME% ^
    --hidden-import pystray._win32 ^
    --hidden-import screeninfo.enumerators.windows_multimon ^
    posture_reminder.py
if exist %NAME%.spec del %NAME%.spec
if exist build rmdir /s /q build

echo.
echo Done! dist\%NAME%.exe
pause
