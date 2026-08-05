@echo off
cd /d "%~dp0"
if /I "%~1"=="write" (
    python local_admin_gui.py --write-local-only
) else (
    python local_admin_gui.py
)
