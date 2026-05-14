@echo off
setlocal
cd /d %~dp0

if not exist venv (
  python -m venv venv
)

call venv\Scripts\activate

python -m pip install --upgrade pip
pip install -r requirements.txt

if not exist config.yaml (
  copy config.example.yaml config.yaml
  echo Created config.yaml from config.example.yaml. Please edit it before streaming.
)

python -m app.main --config config.yaml

pause

