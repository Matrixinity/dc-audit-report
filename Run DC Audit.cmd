@echo off
rem Double-click to start the DC Audit app and open it in your browser.
cd /d "%~dp0"
if not exist ".venv\Scripts\streamlit.exe" (
    echo Setting up Python environment, one-time...
    python -m venv .venv
    ".venv\Scripts\pip" install -r requirements.txt
)
".venv\Scripts\streamlit.exe" run app.py --server.port 8501
