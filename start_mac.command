#!/bin/bash
# Sigen PV Monitor - uruchomienie na macOS (dwuklik w Finderze albo ./start_mac.command w Terminalu).
# Przy pierwszym uruchomieniu tworzy srodowisko .venv i instaluje biblioteki (potrzebny Python 3.11+, np. z python.org
# albo: brew install python). Kolejne uruchomienia startuja od razu. Zatrzymanie: Ctrl+C albo zamkniecie okna Terminala.
cd "$(dirname "$0")" || exit 1
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Brak Pythona 3.11 lub nowszego. Zainstaluj go z https://www.python.org/downloads/macos/ albo: brew install python"
  read -r -p "Enter - zamknij" _; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "Pierwsze uruchomienie: tworze srodowisko .venv i instaluje biblioteki..."
  "$PY" -m venv .venv || exit 1
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements.txt || { read -r -p "Blad instalacji - Enter, zamknij" _; exit 1; }
fi
exec .venv/bin/python main.py "$@"
