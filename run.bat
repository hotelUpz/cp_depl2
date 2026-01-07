@echo off
chcp 65001 > nul
cls

echo [1/4] Checking virtual environment...
if not exist .venv (
    echo [1/4] Creating virtual environment...
    python -m venv .venv
)

echo [2/4] Activating environment and installing dependencies...
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt

echo [3/4] Running main.py...
python main.py

echo [4/4] Task completed.
pause
