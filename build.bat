@echo off
set VER=v1.0.0
set NAME=posture_reminder_%VER%

pip install pyinstaller --quiet

if exist dist\posture_reminder_v*.exe del /q dist\posture_reminder_v*.exe

python -m PyInstaller --onefile --noconsole --name %NAME% ^
    --hidden-import pystray._win32 ^
    --hidden-import screeninfo.enumerators.windows_multimon ^
    posture_reminder.py

if exist %NAME%.spec del %NAME%.spec
if exist build rmdir /s /q build

echo.
echo Done! dist\%NAME%.exe
pause
