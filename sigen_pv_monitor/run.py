"""
Start dodatku Home Assistant „Sigen PV Monitor”.

1. Opcje dodatku (/data/options.json) -> /data/config.ini: PIN, adres falownika (puste = bez zmian; adres można
   znaleźć przyciskiem „Znajdź falownik” w Ustawieniach aplikacji), port 8000, bez otwierania przeglądarki.
2. Pierwsze uruchomienie: współrzędne i strefa czasowa instalacji z ustawień Home Assistant.
3. Karty panelu HA (opcja karty_ha): plik startowy kart w /homeassistant/www/sigen/ i zasób panelu /local/sigen/sigen-loader.js.
4. Start aplikacji (main.py) na porcie 8000 – panel w pasku bocznym HA (Ingress) i karty.
"""
import asyncio
import configparser
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

APP = Path("/app")
DATA = Path(os.environ.get("SIGEN_PV_DATA", "/data"))
CFG = DATA / "config.ini"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HA_CONFIG = Path("/homeassistant")
LOADER_URL = "/local/sigen/sigen-loader.js"


def log(msg):
    print(f"[sigen-addon] {msg}", flush=True)


def options():
    try:
        return json.loads((DATA / "options.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ha_config():
    """Ustawienia HA (szerokość / długość geograficzna, strefa czasowa) przez Supervisora."""
    if not TOKEN:
        return {}
    req = urllib.request.Request("http://supervisor/core/api/config", headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        log(f"nie można odczytać ustawień HA: {e}")
        return {}


def write_config(opt):
    cp = configparser.ConfigParser()
    first = not CFG.exists()
    if not first:
        cp.read(CFG, encoding="utf-8")
    for sec in ("serwer", "falownik", "instalacja"):
        if not cp.has_section(sec):
            cp.add_section(sec)
    cp.set("serwer", "port", "8000")
    cp.set("serwer", "host", "0.0.0.0")
    cp.set("serwer", "otworz_przegladarke", "nie")
    if opt.get("pin"):
        cp.set("serwer", "pin", str(opt["pin"]).strip())
    if (opt.get("falownik_host") or "").strip():
        cp.set("falownik", "host", opt["falownik_host"].strip())
        cp.set("falownik", "port", str(opt.get("falownik_port") or 502))
    if first or cp.get("instalacja", "lat", fallback="52.23") == "52.23":
        c = ha_config()
        if c.get("latitude") is not None:
            cp.set("instalacja", "lat", f"{float(c['latitude']):.4f}")
            cp.set("instalacja", "lon", f"{float(c['longitude']):.4f}")
            log(f"współrzędne instalacji z HA: {c['latitude']}, {c['longitude']}")
        if c.get("time_zone"):
            cp.set("instalacja", "strefa_czasowa", c["time_zone"])
    DATA.mkdir(parents=True, exist_ok=True)
    with open(CFG, "w", encoding="utf-8") as f:
        cp.write(f)
    log("config.ini zapisany" + (" (pierwsze uruchomienie – resztę ustaw w aplikacji: zakładka Ustawienia)" if first else ""))


async def register_resource():
    """Zasób panelu HA (tryb UI / storage): lovelace/resources/create przez websocket Supervisora."""
    import websockets
    async with websockets.connect("ws://supervisor/core/websocket", max_size=None) as ws:
        await ws.recv()                                             # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            raise RuntimeError("brak autoryzacji w HA")
        await ws.send(json.dumps({"id": 1, "type": "lovelace/resources"}))
        r = json.loads(await ws.recv())
        if not r.get("success"):
            raise RuntimeError("panel w trybie YAML – dodaj zasób ręcznie: " + LOADER_URL)
        if any(x.get("url", "").split("?")[0] == LOADER_URL for x in r.get("result") or []):
            return "zasób już jest"
        await ws.send(json.dumps({"id": 2, "type": "lovelace/resources/create", "res_type": "module", "url": LOADER_URL}))
        r = json.loads(await ws.recv())
        if not r.get("success"):
            raise RuntimeError(str(r.get("error")))
        return "dodano zasób " + LOADER_URL


def install_cards():
    www = HA_CONFIG / "www"
    if not HA_CONFIG.exists():
        log("brak dostępu do folderu konfiguracji HA – karty: instrukcja w docs/HOME_ASSISTANT.md")
        return
    new_www = not www.exists()
    (www / "sigen").mkdir(parents=True, exist_ok=True)
    shutil.copy(APP / "web" / "static" / "ha" / "sigen-loader.js", www / "sigen" / "sigen-loader.js")
    log("karty HA: plik startowy w /config/www/sigen/sigen-loader.js")
    if new_www:
        log("UWAGA: folder /config/www powstał teraz – uruchom HA ponownie raz, żeby karty były dostępne")
    if TOKEN:
        try:
            log("karty HA: " + asyncio.run(asyncio.wait_for(register_resource(), 20)))
        except Exception as e:  # noqa: BLE001
            log(f"karty HA: nie dodano zasobu automatycznie ({e}) – Ustawienia → Pulpity → Zasoby → {LOADER_URL}")


def main():
    opt = options()
    write_config(opt)
    if opt.get("karty_ha", True):
        try:
            install_cards()
        except Exception as e:  # noqa: BLE001
            log(f"karty HA: {e}")
    os.chdir(APP)
    os.execv(sys.executable, [sys.executable, str(APP / "main.py"), "--no-browser", "--port", "8000"])


if __name__ == "__main__":
    main()
