"""
Konfiguracja aplikacji: plik config.ini + sciezki.

Gdzie sa dane (config.ini, baza SQLite, cache):
  - wersja przenosna (portable) i uruchomienie ze zrodel: folder programu,
  - wersja zainstalowana (instalator): %LOCALAPPDATA%\\SigenPVMonitor.
Wersja przenosna to folder, w ktorym obok .exe lezy plik "portable.txt" (albo config.ini).
"""
import configparser
import os
import sys
from pathlib import Path

APP_NAME = "SigenPVMonitor"
APP_TITLE = "Sigen PV Monitor"

FROZEN = getattr(sys, "frozen", False)
# pliki aplikacji (index.html, static/) - w .exe rozpakowane do katalogu tymczasowego
RES_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
WEB_DIR = RES_DIR / "web"
EXE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent


def _data_dir():
    if os.environ.get("SIGEN_PV_DATA"):
        return Path(os.environ["SIGEN_PV_DATA"])
    if not FROZEN or (EXE_DIR / "portable.txt").exists() or (EXE_DIR / "config.ini").exists():
        return EXE_DIR
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


DATA_DIR = _data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.ini"

# Wartosci domyslne - nadpisywane przez config.ini (ustawienia mozna zmienic tez na stronie, zakladka "Ustawienia")
DEFAULTS = {
    "falownik": {
        "host": "192.168.1.100",     # adres IP falownika (Modbus TCP musi byc wlaczony w mySigen)
        "port": "502",
        "plant_id": "247",           # adres Modbus "plant" (cala instalacja) - w Sigenergy zwykle 247
        "inverter_id": "1",          # adres falownika hybrydowego (pojedynczy falownik = 1)
        "interval": "5",             # s miedzy odczytami mocy
    },
    "serwer": {
        "port": "8000",
        "host": "0.0.0.0",           # 0.0.0.0 = dostep z telefonu w sieci domowej; 127.0.0.1 = tylko ten komputer
        "otworz_przegladarke": "tak",
    },
    "baza": {
        "plik": "sigen.db",          # baza SQLite (wzgledem folderu danych)
    },
    "instalacja": {
        "lat": "52.23",              # wspolrzedne instalacji (prognoza, wschod/zachod slonca)
        "lon": "21.01",
        "moc_falownika_kw": "10",    # gorne ograniczenie prognozy
        "pr": "0.85",                # wspolczynnik wydajnosci do prognozy Open-Meteo
        "strefa_czasowa": "Europe/Warsaw",
    },
    # polacie do prognozy: nazwa | kWp | azymut (0 = poludnie, -90 = wschod, 90 = zachod) | nachylenie [st.]
    "polacie": {
        "polac1": "Dach | 5.0 | 0 | 30",
    },
    # stringi PV falownika (nazwy do wykresu "Realna produkcja PV"); puste = nieuzywany
    "stringi": {
        "pv1": "PV1",
        "pv2": "PV2",
        "pv3": "",
        "pv4": "",
    },
    "prognoza": {
        "glowne_zrodlo": "open_meteo",   # open_meteo | om_srednia | om_ecmwf | om_icon_eu | ... | forecast_solar
        "forecast_solar": "tak",         # forecast.solar (limit 12 zapytan/h)
        "korekta": "",                   # np. open_meteo=1.05, om_ecmwf=0.97
    },
    "strategia": {                       # strategia osobista - tylko symulacja (nic nie trafia do falownika)
        "pojemnosc_kwh": "",             # puste = odczyt z falownika
        "moc_ladowania_kw": "5",         # maks. moc ladowania/rozladowania magazynu
        "sprawnosc_ladowania": "0.95",
        "sprawnosc_rozladowania": "0.95",
        "wartosc_sprzedazy": "1.0",      # wartosc sprzedanej kWh = RCE x wspolczynnik (np. 0.8, gdy depozyt nie pokrywa calego rachunku)
        "koszt_zuzycia_zl_kwh": "0.10",  # zuzycie baterii: zl za kWh oddana z magazynu (0 = pomijaj)
    },
    "pstryk": {
        "api_key": "",                   # klucz API Pstryka (aplikacja Pstryk -> Integracje); puste = bez Pstryka
    },
    "ceny": {
        "operator": "tauron",            # operator dystrybucyjny (taryfy z davo22/polish-electricity-tariffs)
        "taryfa": "G11",
        "vat": "0.23",
        "marza_zl_kwh": "0.0732",        # marza sprzedawcy netto zl/kWh (dynamiczna taryfa)
        "akcyza_zl_kwh": "0.005",
        "zakup_baza": "rce",             # rce | csdac
        "sprzedaz_baza": "rce",          # rce | csdac
    },
}

_COMMENTS = """; Sigen PV Monitor - konfiguracja. Zmiany mozna robic tez na stronie (zakladka Ustawienia).
; Po zmianie pliku uruchom aplikacje ponownie.
"""


def load():
    cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cp.optionxform = str
    cp.read_dict(DEFAULTS)
    if CONFIG_FILE.exists():
        user = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        user.optionxform = str
        user.read(CONFIG_FILE, encoding="utf-8")
        for sec in user.sections():
            if sec in ("polacie",) and user.has_section(sec):
                cp.remove_section(sec)          # lista polaci zastepuje domyslna w calosci
                cp.add_section(sec)
            if not cp.has_section(sec):
                cp.add_section(sec)
            for k, v in user.items(sec):
                cp.set(sec, k, v)
    else:
        save_parser(cp)
    return cp


def save_parser(cp):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(_COMMENTS)
        cp.write(f)


CFG = load()


def get(sec, key, default=""):
    return CFG.get(sec, key, fallback=default).strip()


def getf(sec, key, default=0.0):
    try:
        return float(get(sec, key, str(default)).replace(",", "."))
    except ValueError:
        return default


def geti(sec, key, default=0):
    try:
        return int(float(get(sec, key, str(default))))
    except ValueError:
        return default


def getb(sec, key, default=False):
    return get(sec, key, "tak" if default else "nie").lower() in ("1", "tak", "yes", "true", "on")


def planes():
    """Polacie do prognozy: [{nazwa, kwp, azymut, nachylenie}]."""
    out = []
    if CFG.has_section("polacie"):
        for _k, v in CFG.items("polacie"):
            parts = [p.strip() for p in v.split("|")]
            if len(parts) < 4:
                continue
            try:
                num = lambda x: (lambda f: int(f) if f.is_integer() else f)(float(x.replace(",", ".")))  # noqa: E731
                out.append({"nazwa": parts[0], "kwp": float(parts[1].replace(",", ".")),
                            "azymut": num(parts[2]), "nachylenie": num(parts[3])})
            except ValueError:
                continue
    return out


def strings():
    """Uzywane stringi PV: [(nazwa, numer 1-4)]."""
    out = []
    for n in range(1, 5):
        name = get("stringi", f"pv{n}")
        if name:
            out.append((name, n))
    return out


def corrections():
    """'open_meteo=1.05, om_ecmwf=0.97' -> {zrodlo: wspolczynnik}."""
    out = {}
    for part in get("prognoza", "korekta").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                out[k.strip()] = float(v.strip().replace(",", "."))
            except ValueError:
                pass
    return out


SECRET = "********"     # tak strona widzi zapisany klucz API (prawdziwy klucz nie wychodzi z aplikacji)


def as_dict():
    d = {s: dict(CFG.items(s)) for s in CFG.sections()}
    if d.get("pstryk", {}).get("api_key"):
        d["pstryk"]["api_key"] = SECRET
    return d


def update(data):
    """Zapis ustawien z formularza: {sekcja: {klucz: wartosc}}. Zmiana wymaga restartu aplikacji."""
    cp = load()
    for sec, vals in (data or {}).items():
        if sec not in DEFAULTS or not isinstance(vals, dict):
            continue
        if sec == "polacie":
            cp.remove_section(sec)
            cp.add_section(sec)
        for k, v in vals.items():
            k = str(k).strip()
            if sec == "pstryk" and k == "api_key" and str(v).strip() == SECRET:
                continue        # bez zmiany zapisanego klucza
            if not k or any(c in k for c in "[]=;#\n\r"):
                continue
            cp.set(sec, k, str(v).replace("\n", " ").strip())
    save_parser(cp)
    return True
