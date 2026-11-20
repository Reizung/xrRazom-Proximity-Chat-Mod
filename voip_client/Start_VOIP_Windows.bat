@echo off
setlocal
cd /d "%~dp0"
python -c "import numpy, scipy, sounddevice" 2>nul
if errorlevel 1 (
    echo Installing required Python audio libraries ^(numpy, scipy, sounddevice^)...
    python -m pip install -r requirements.txt
)
python xr_razom_voip_client.py %*
pause
