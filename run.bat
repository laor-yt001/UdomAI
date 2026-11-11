@echo off
echo ===================================
echo     Starting UdomAI-Bot
echo ===================================

:: Check if Python is installed
python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo Python is not installed or not in your PATH.
    echo Please install Python 3.11 or newer and try again.
    pause
    exit /b
)

:: Create virtual environment if it doesn't exist
IF NOT EXIST "venv" (
    echo Creating virtual environment...
    python -m venv venv
)

:: Activate virtual environment
echo Activating virtual environment...
call venv\Scripts\activate.bat

:: Install requirements
echo Installing dependencies...
python -m pip install --upgrade pip
pip install -r requirements.txt

:: Check if .env file exists
IF NOT EXIST ".env" (
    echo Warning: .env file not found!
    echo Please create a .env file and add your TELEGRAM_BOT_TOKEN, API_ID, and API_HASH.
    pause
)

:: Run the bot
echo Running the bot...
python main.py

pause
