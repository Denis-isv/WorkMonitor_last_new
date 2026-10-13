#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 не найден. Установите Python 3.11+ с https://www.python.org/downloads/macos/"
  exit 1
fi

[ -d .venv ] || python3 -m venv .venv
[ -f .env ] || cp .env.example .env

# Replace example secrets once and print the generated initial password.
python3 - <<'PY'
from pathlib import Path
import secrets
p = Path(".env")
lines = p.read_text().splitlines()
values = {}
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        values[k] = v
changed = False
if not values.get("SECRET_KEY") or values["SECRET_KEY"] == "replace-with-a-long-random-secret":
    values["SECRET_KEY"] = secrets.token_hex(32)
    changed = True
if not values.get("ADMIN_PASSWORD") or values["ADMIN_PASSWORD"] == "ChangeThisToALongUniquePassword":
    values["ADMIN_PASSWORD"] = secrets.token_urlsafe(18)
    changed = True
if changed:
    out=[]
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            k=line.split("=",1)[0]
            if k in values:
                line=f"{k}={values[k]}"
        out.append(line)
    p.write_text("\n".join(out)+"\n")
    print("\nСгенерированы локальные учётные данные:")
    print("Логин администратора:", values.get("ADMIN_USERNAME","admin"))
    print("Пароль администратора:", values["ADMIN_PASSWORD"])
    print("Сохраните пароль сейчас: он записан в локальный .env. Не отправляйте .env другим людям.\n")
PY

source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m flask --app app init-db
python app.py
