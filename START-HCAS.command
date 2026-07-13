#!/bin/bash
# HCAS — one-click setup & launch for macOS.
# First time: right-click this file -> Open (macOS blocks double-click on downloaded files).
# After that, double-click works. Safe to run again any time — it reuses the install.
set -e
cd "$(dirname "$0")"

echo "==============================="
echo "  HCAS — setup & launch"
echo "==============================="

if ! command -v python3 >/dev/null 2>&1; then
  echo ""
  echo "Python 3 was not found. Install the Apple developer tools first:"
  echo "    open Terminal and run:  xcode-select --install"
  echo "then double-click this file again."
  read -r -p "Press Enter to close..."
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Creating Python environment (first run only)..."
  python3 -m venv .venv
fi
source .venv/bin/activate
echo "Installing dependencies (first run takes a few minutes)..."
python3 -m pip install --quiet --upgrade pip
python3 -m pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  echo ""
  echo "The AI steps (2 and 4) need an Anthropic API key."
  read -r -p "Paste the API key (or press Enter to skip for now): " KEY
  if [ -n "$KEY" ]; then
    echo "ANTHROPIC_API_KEY=$KEY" > .env
    echo "Saved."
  else
    echo "Skipped — steps 2 and 4 will error until a key is added to a .env file."
  fi
fi

echo ""
echo "Starting HCAS. Your browser will open at http://127.0.0.1:8000/hcas"
echo "Leave this window open while using the tool. Close it (or press Ctrl+C) to stop."
( sleep 4 && open "http://127.0.0.1:8000/hcas" ) &
exec python3 -m uvicorn backend.app:app --port 8000
