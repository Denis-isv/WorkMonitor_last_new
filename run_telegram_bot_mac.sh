#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then echo "Python 3 не найден"; exit 1; fi
[ -d .venv ] || python3 -m venv .venv
[ -f .env ] || cp .env.example .env
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if ! grep -q '^TELEGRAM_BOT_TOKEN=.' .env; then echo "Откройте .env и заполните TELEGRAM_BOT_TOKEN токеном от @BotFather, затем запустите скрипт снова."; open -e .env 2>/dev/null || true; exit 2; fi

export SSL_CERT_FILE="$(./.venv/bin/python -c 'import certifi; print(certifi.where())')"
export REQUESTS_CA_BUNDLE="$SSL_CERT_FILE"
python telegram_bot.py
