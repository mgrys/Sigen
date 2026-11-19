# Sigen PV Monitor – wersja publiczna (github.com/mgrys/Sigen, MIT)

Aplikacja FastAPI (app.py, web/index.html) dla falownika i magazynu Sigenergy (Modbus TCP): podgląd, prognoza PV, ceny
(PSE, taryfy URE, Pstryk), strategia magazynu (sterowanie Remote EMS domyślnie wyłączone), rekuperator Vallox (opcjonalnie),
karty Home Assistant (web/static/ha/), dodatek HA (sigen_pv_monitor/, repository.yaml). Rozmowa i komentarze po polsku.

## Zasady
- Bez danych, urządzeń i ustawień Marka (to jest w wersji domowej C:\priv\sigenergy). Żadnych sekretów w repo – config.ini
  jest w .gitignore.
- Wersja: `__version__` w app.py, `version` w pyproject.toml, `VER` w git_push_public.bat, `version` w
  sigen_pv_monitor/config.yaml – zawsze ta sama (dodatek buduje się z tagu v<wersja>).
- Każde wydanie: wpis w CHANGELOG.md, opis w git_push_public.bat (MSG + komentarz), Marek wypycha `git_push_public.bat`
  (commit, push, tag – GitHub Actions buduje instalator Windows).
- Końce linii: pliki Windows CRLF; sigen_pv_monitor/**, *.command, *.sh – LF (.gitattributes).
- Sterowanie falownikiem domyślnie wyłączone; opisy dla użytkowników po polsku (README, docs/).

## Test
- `python main.py --no-browser --port 8798` (SIGEN_PV_DATA=folder testowy), sprawdzenie /api/version i strony;
  GitHub Actions: pyflakes + start bez falownika.
