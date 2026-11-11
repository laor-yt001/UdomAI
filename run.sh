#!/bin/bash
echo "==================================="
echo "       Starting UdomAI-Bot"
echo "==================================="

# Check if Python 3 is installed
if ! command -v python3 &> /dev/null
then
    echo "Python 3 could not be found."
    echo "Please install Python 3.11 or newer and try again."
    exit 1
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Install requirements
echo "Installing dependencies..."
python3 -m pip install --upgrade pip
pip install -r requirements.txt

# Check if .env file exists
if [ ! -f ".env" ]; then
    echo "Warning: .env file not found!"
    echo "Please create a .env file and add your TELEGRAM_BOT_TOKEN, API_ID, and API_HASH."
fi

# Ensure SERVER_ID is set in .env
if ! grep -q "SERVER_ID=" .env; then
    echo ""
    echo "----------------------------------------------------"
    echo "First time setup: What is the ID of this server?"
    echo "If this is your main server, type 'main'."
    echo "If this is a secondary server, type 'server_1', etc."
    echo "----------------------------------------------------"
    read -p "Enter SERVER_ID: " NEW_SERVER_ID
    echo "SERVER_ID=$NEW_SERVER_ID" >> .env
    echo "Saved SERVER_ID=$NEW_SERVER_ID to .env!"
fi

# Run the bot
echo "Running the bot..."
python3 main.py
