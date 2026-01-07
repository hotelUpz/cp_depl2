#!/bin/bash
set -e

echo "[1/4] Checking virtual environment..."
if [ ! -d ".venv" ]; then
    echo "[1/4] Creating virtual environment..."
    python3 -m venv .venv
fi

echo "[2/4] Activating environment and installing dependencies..."
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

echo "[3/4] Running main.py..."
python main.py

echo "[4/4] Task completed."
