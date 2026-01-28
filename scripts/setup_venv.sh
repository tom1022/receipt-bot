#!/usr/bin/env bash
set -euo pipefail

cd /opt/receipt-bot
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "Virtualenv ready at /opt/receipt-bot/venv"
