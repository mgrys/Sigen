"""
Sigen PV Monitor - lokalny podglad instalacji PV z magazynem Sigenergy (SigenStor) przez Modbus TCP.
Nieoficjalny projekt, niezwiazany z Sigenergy. Tylko odczyt - aplikacja niczego nie zapisuje do falownika.

Uruchomienie ze zrodel:  pip install -r requirements.txt  &&  python main.py
Podglad:                 http://localhost:8000  (z telefonu: http://<IP-komputera>:8000)
Konfiguracja:            config.ini (tworzony przy pierwszym starcie) albo zakladka "Ustawienia".
"""
import datetime as dt
import os
import sys
import threading
import time

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pymodbus.client import ModbusTcpClient

import config
import db
import discover
import forecast
import prices
import pradcast
import pstryk
import strategy
import registers
import access
import gridwatch
import meter
import smartlife
import vallox
import inverter_ctl

__version__ = "1.21.0"

# --- konfiguracja (config.ini, sekcja [falownik]) --------------------------
PV_ENABLED = config.module_pv()         # Ustawienia -> "Co masz": instalacja PV Sigen
HOST = config.get("falownik", "host", "192.168.1.100")
PORT = config.geti("falownik", "port", 502)
PLANT_ID = registers.PLANT     # adres "plant" - dane calej instalacji
INVERTER_ID = registers.INV    # adres falownika hybrydowego
COUNTER_INTERVAL = 60    # s miedzy odczytami licznikow energii
SETTINGS_INTERVAL = 300  # s miedzy odczytami ustawien (tylko odczyt)
ALARM_INTERVAL = 30      # s miedzy odczytami alarmow falownika (tylko odczyt)
INTERVAL = max(2, config.geti("falownik", "interval", 5))   # s miedzy odczytami mocy
MAX_GAP = 60             # s - przerwy dluzsze nie sa liczone do energii

# klucz: (rejestr, liczba rejestrow, signed, dzielnik)
GRID_MODE_REG = 30009    # 0 praca z siecia, 1 off-grid (zanik sieci), 2 off-grid recznie
REGISTERS = {
    "soc":   (30014, 1, False, 10),     # %
    "pv":    (30035, 2, True, 1000),    # kW
    "bat":   (30037, 2, True, 1000),    # kW, + ladowanie / - rozladowanie
    "grid":  (30005, 2, True, 1000),    # kW, + import / - eksport
    "plant": (30031, 2, True, 1000),    # kW, moc czynna AC falownika
}
FIELDS = ["soc", "pv", "bat", "grid", "plant", "load"]

# Liczniki energii (klucze z registers.py): *_day zeruje falownik o polnocy,
# *_total to liczniki narastajace. Wszystkie rejestry czytane co COUNTER_INTERVAL.
COUNTER_KEYS = ["pv_day", "load_day", "imp_day", "exp_day", "chg_day", "dis_day",
                "pv_total", "load_total", "imp_total", "exp_total", "chg_total", "dis_total"]
ENERGY_KEYS = ["pv", "load", "imp", "exp", "chg", "dis"]
GRID_KEYS = ("imp", "exp")

latest = {"ok": False, "error": "Uruchamianie..."}
counters_state = {"values": {}, "unsupported": [], "ts": None}
registers_state = {"values": {}, "ts": None}
settings_state = {"values": {}, "ts": None, "known": None}
alarm_state = {"values": {}, "ts": None, "known": None}
lock = threading.Lock()


# --- Modbus --------------------------------------------------------------
def read(client, addr, count, unit=PLANT_ID):
    """Obsluga roznych wersji pymodbus 3.x (device_id / slave)."""
    try:
        return client.read_input_registers(addr, count=count, device_id=unit)
    except TypeError:
        return client.read_input_registers(addr, count=count, slave=unit)


def write_holding(client, addr, values, unit=PLANT_ID):
    """Zapis holding registers (funkcja 0x10) - TYLKO sterowanie Remote EMS wg strategii (inverter_ctl.py),
    gdy uzytkownik wlaczyl je na stronie (domyslnie wylaczone)."""
    try:
        return client.write_registers(addr, values, device_id=unit)
    except TypeError:
        return client.write_registers(addr, values, slave=unit)


def read_holding(client, addr, count, unit=PLANT_ID):
    """Odczyt holding registers (ustawienia) - funkcja 0x03, bez zapisu."""
    try:
        return client.read_holding_registers(addr, count=count, device_id=unit)
    except TypeError:
        return client.read_holding_registers(addr, count=count, slave=unit)


def read_alarms(client, con):
    """Odczyt alarmow (w tym AFCI); kazda zmiana stanu trafia do historii (settings_log)."""
    vals = registers.read_alarms(client, read)
    now = int(time.time())
    with lock:
        alarm_state.update(values=vals, ts=now)
    try:
        if alarm_state["known"] is None:
            alarm_state["known"] = {k: v for k, v in db.last_settings(con).items() if k in registers.ALARM_NAMES}
        known = alarm_state["known"]
        changed, changes = {}, []
        for k, v in vals.items():
            if v is None:
                continue
            txt = registers.alarm_text(k, v)
            if known.get(k) != txt:
                changed[k] = txt
                if k in known or v:          # pierwszy zapis "brak alarmow" bez powiadomienia
                    changes.append((k, known.get(k), txt, v))
        if changed:
            db.log_settings(con, now, changed)
            known.update(changed)
    except Exception as e:  # noqa: BLE001
        with lock:
            latest["db_error"] = str(e)
        return
    if changes:
        pass


def read_settings(client, con):
    """Odczyt ustawien falownika; zmiany wzgledem ostatniego stanu trafiaja do settings_log."""
    vals = registers.read_settings(client, read_holding)
    now = int(time.time())
    with lock:
        settings_state.update(values=vals, ts=now)
    try:
        if settings_state["known"] is None:
            settings_state["known"] = db.last_settings(con)
        known = settings_state["known"]
        changed = {k: str(v) for k, v in vals.items() if v is not None and known.get(k) != str(v)}
        if changed:
            db.log_settings(con, now, changed)
            known.update(changed)
    except Exception as e:  # noqa: BLE001
        with lock:
            latest["db_error"] = str(e)


def decode(regs, signed=False):
    """Big-endian, starsze slowo pierwsze; 1, 2 lub 4 rejestry."""
    v = 0
    for r in regs:
        v = (v << 16) | r
    bits = 16 * len(regs)
    return v - (1 << bits) if signed and v >= 1 << (bits - 1) else v


def read_counters(client, con):
    """Odczyt wszystkich 59 rejestrow + zapis licznikow energii do bazy."""
    day = dt.date.today().isoformat()
    allv = registers.read_all(client, read)
    vals = {k: allv[k] for k in COUNTER_KEYS if allv.get(k) is not None}
    unsupported = [k for k in COUNTER_KEYS if allv.get(k) is None]
    now = int(time.time())
    with lock:
        counters_state.update(values=vals, unsupported=unsupported, ts=now)
        registers_state.update(values=allv, ts=now)
    if allv.get("cell_temp") is not None:
        try:
            db.insert_bat_temps(con, now, [allv.get(k) for k in ("cell_temp", "bat_t_max", "bat_t_min", "pcs_temp")])
        except Exception as e:  # noqa: BLE001
            with lock:
                latest["db_error"] = str(e)
    try:
        for key, v in vals.items():
            db.upsert_counter(con, day, key, v, now)
        con.commit()
        used = {n for _name, n in config.strings()}
        sv = []
        for n in range(1, 5):
            u, i = allv.get(f"pv{n}_u"), allv.get(f"pv{n}_i")
            sv += [u, i] if n in used else [None, None]
        if any(v is not None for v in sv):
            db.insert_pv_strings(con, now, sv)
    except Exception as e:  # noqa: BLE001 - polaczenie odnowi sie przy kolejnym zapisie probki
        with lock:
            latest["db_error"] = str(e)


def db_connect():
    return db.connect()


def init_db():
    db_init()


def poll_loop():
    client = None
    con = None
    last_counters = 0
    last_settings = 0
    last_alarms = 0
    while True:
        t0 = time.time()
        try:
            if client is None or not client.connected:
                client = ModbusTcpClient(HOST, port=PORT, timeout=3)
                if not client.connect():
                    raise ConnectionError(f"Brak polaczenia z {HOST}:{PORT}")
            vals = {}
            for key, (addr, count, signed, div) in REGISTERS.items():
                rr = read(client, addr, count)
                if rr.isError():
                    raise IOError(f"{key} ({addr}): {rr}")
                vals[key] = round(decode(rr.registers, signed) / div, 3)
            # zuzycie domu = to co oddaje falownik + to co plynie z sieci
            vals["load"] = round(max(vals["plant"] + vals["grid"], 0), 3)
            # straty falownika = DC wejscie - DC wyjscie - AC wyjscie
            vals["loss"] = live_loss_kw(vals)
            vals["loss_mode"] = "czuwanie" if vals["pv"] < STANDBY_KW and abs(vals["bat"]) < STANDBY_KW else "konwersja"
            ts = int(t0)
            try:                                         # praca z siecia / off-grid (zanik sieci)
                rg = read(client, GRID_MODE_REG, 1)
                if not rg.isError():
                    gridwatch.update(int(rg.registers[0]), ts)
            except Exception:  # noqa: BLE001
                pass
            with lock:
                latest.clear()
                latest.update(vals, ts=ts, ok=True)
            try:
                if con is None:
                    con = db_connect()
                db.insert_sample(con, ts, [vals[f] for f in FIELDS])
            except Exception as e:  # noqa: BLE001 - blad bazy nie zatrzymuje odczytow
                with lock:
                    latest["db_error"] = str(e)
                try:
                    con.close()
                except Exception:
                    pass
                con = None
                time.sleep(max(0.5, INTERVAL - (time.time() - t0)))
                continue
            try:                                         # sterowanie wg strategii (tylko gdy wlaczone na stronie)
                inverter_ctl.apply(client, write_holding, read_holding)
            except Exception:  # noqa: BLE001
                pass
            if t0 - last_counters >= COUNTER_INTERVAL:
                last_counters = t0
                read_counters(client, con)
            if t0 - last_settings >= SETTINGS_INTERVAL:
                last_settings = t0
                read_settings(client, con)
            if t0 - last_alarms >= ALARM_INTERVAL:
                last_alarms = t0
                read_alarms(client, con)
        except Exception as e:  # noqa: BLE001 - chcemy przezyc kazdy blad
            with lock:
                latest["ok"] = False
                latest["error"] = str(e)
            try:
                if client:
                    client.close()
            except Exception:
                pass
            client = None
        time.sleep(max(0.5, INTERVAL - (time.time() - t0)))


def string_dc_kw(values):
    """Moc DC wszystkich stringow (U x I) albo None."""
    p = registers.string_powers(values)
    return None if any(v is None for v in p.values()) else round(sum(p.values()), 3)


def live_loss_kw(vals):
    """Straty na zywo. Rejestr 30035 (moc PV) jest mierzony za przetwornica DC/DC, a moc magazynu
    przy ladowaniu z PV wynika z bilansu - wzor PV - mag. - AC daje wtedy ~0. Gdy swieci slonce,
    jako wejscie bierzemy wiec moc DC stringow (napiecie x prad), odczytywana co 60 s."""
    with lock:
        regs, rts = registers_state["values"], registers_state["ts"]
    dc = string_dc_kw(regs) if rts and time.time() - rts < 150 else None
    if vals["pv"] > 0.05:
        if dc is None:
            return None
        # moc przed DC/DC nie moze byc mniejsza niz za nia (przy malym pradzie odczyt stringow spada do 0)
        dc = max(dc, vals["pv"])
        return max(0.0, round(dc + max(-vals["bat"], 0) + max(-vals["plant"], 0)
                              - max(vals["bat"], 0) - max(vals["plant"], 0), 3))
    return max(0.0, loss_kw(vals["pv"], vals["bat"], vals["plant"]))


def loss_kw(pv, bat, plant):
    """Straty przetwarzania [kW]: PV + rozladowanie - ladowanie - moc AC.

    bat > 0 to ladowanie (energia zostaje po stronie DC), plant > 0 to oddawanie
    AC do domu/sieci, plant < 0 to pobor AC (ladowanie z sieci). Wzor jest ten
    sam dla wszystkich kierunkow przeplywu.
    """
    return round(pv - bat - plant, 3)


# --- energia z licznikow falownika --------------------------------------
def counter_energy(con, day):
    """Energia dnia z licznikow falownika: {klucz: kWh} tylko dla dostepnych.

    1. rejestr dzienny (*_day) - ostatni odczyt danego dnia,
    2. licznik narastajacy (*_total) - roznica wzgledem ostatniego odczytu
       poprzedniego dnia (albo pierwszego odczytu dnia, jesli brak poprzedniego).
    """
    rows = con.execute('SELECT "key", "first", "last" FROM counters WHERE "day" = ?', (day,)).fetchall()
    cur = {k: (f, l) for k, f, l in rows}
    out = {}
    for k in ENERGY_KEYS:
        # imp_day/exp_day (30560/30554) to energia AC pobrana/wyprowadzona przez falownik, NIE wymiana z siecia -
        # dla sieci tylko liczniki punktu przylaczenia (30216/30220)
        if k not in GRID_KEYS and f"{k}_day" in cur:
            out[k] = cur[f"{k}_day"][1]
        elif f"{k}_total" in cur:
            prev = con.execute(
                'SELECT "last" FROM counters WHERE "key" = ? AND "day" = '
                '(SELECT MAX("day") FROM counters WHERE "key" = ? AND "day" < ?)',
                (f"{k}_total", f"{k}_total", day)).fetchone()
            base = prev[0] if prev else cur[f"{k}_total"][0]
            out[k] = round(cur[f"{k}_total"][1] - base, 2)
    return out


def eff_into_energy(e, acc):
    """Straty i sprawnosc dnia z metody minutowej (moc DC stringow) zamiast calkowania rejestrow."""
    h = sum(v[0] for v in acc.values())
    e_in = sum(v[1] for v in acc.values())
    e_out = sum(v[2] for v in acc.values())
    loss = e_in - e_out
    e.update(loss=round(loss, 2), e_in=round(e_in, 2), e_out=round(e_out, 2),
             sprawnosc=round(100 * e_out / e_in, 1) if e_in > 0.05 else None,
             strata_sr_w=round(1000 * loss / h) if h > 0.1 else None)
    return e


def merge_energy(e, ce):
    """Podmienia wartosci calkowane na liczniki falownika tam, gdzie sa."""
    e["src"] = {k: ("falownik" if k in ce else "obliczone") for k in ENERGY_KEYS}
    e.update(ce)
    e["autokonsumpcja"] = round(100 * (e["pv"] - e["exp"]) / e["pv"], 1) if e["pv"] > 0.05 else None
    e["samowystarczalnosc"] = round(100 * (e["load"] - e["imp"]) / e["load"], 1) if e["load"] > 0.05 else None
    return e


# --- energia z mocy ------------------------------------------------------
def energy(rows):
    """Calkowanie mocy (kW) po czasie -> kWh, z rozbiciem na kierunki."""
    e = dict(pv=0.0, load=0.0, imp=0.0, exp=0.0, chg=0.0, dis=0.0,
             loss=0.0, e_in=0.0, e_out=0.0)
    hours = 0.0
    for (t1, _soc, pv, bat, grid, pl, load), (t2, *_) in zip(rows, rows[1:]):
        dt_s = t2 - t1
        if dt_s <= 0 or dt_s > MAX_GAP:
            continue
        h = dt_s / 3600
        e["pv"] += max(pv, 0) * h
        e["load"] += max(load, 0) * h
        e["imp"] += max(grid, 0) * h
        e["exp"] += max(-grid, 0) * h
        e["chg"] += max(bat, 0) * h
        e["dis"] += max(-bat, 0) * h
        e["loss"] += (pv - bat - pl) * h
        e["e_in"] += (max(pv, 0) + max(-bat, 0) + max(-pl, 0)) * h
        e["e_out"] += (max(bat, 0) + max(pl, 0)) * h
        hours += h
    e = {k: round(v, 2) for k, v in e.items()}
    e["sprawnosc"] = round(100 * e["e_out"] / e["e_in"], 1) if e["e_in"] > 0.05 else None
    e["strata_sr_w"] = round(1000 * e["loss"] / hours) if hours > 0.1 else None
    e["autokonsumpcja"] = round(100 * (e["pv"] - e["exp"]) / e["pv"], 1) if e["pv"] > 0.05 else None
    e["samowystarczalnosc"] = round(100 * (e["load"] - e["imp"]) / e["load"], 1) if e["load"] > 0.05 else None
    return e


# --- API -----------------------------------------------------------------
app = FastAPI(title=config.APP_TITLE, version=__version__)
access.INI, access.SECTION = config.CONFIG_FILE, "serwer"
access.PIN_HINT = "na komputerze z aplikacją: Ustawienia → Strona → PIN"
access.SESSIONS_FILE = config.DATA_DIR / "access_sessions.json"
gridwatch.EVENTS = config.DATA_DIR / "grid_events.json"


@app.middleware("http")
async def _access_guard(request: Request, call_next):
    """Zmiany z innych urzadzen w sieci tylko po PIN-ie (access.py); z tego komputera bez zmian."""
    if access.needs_auth(request):
        return JSONResponse(access.denied_body(), status_code=401)
    return await call_next(request)


@app.get("/api/access")
def api_access(request: Request):
    return access.status(request)


@app.post("/api/login")
def api_login(request: Request, payload: dict = Body(default={})):
    ok, msg, sid = access.login(request, payload.get("pin"))
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=403)
    r = JSONResponse({"ok": True})
    r.set_cookie(access.COOKIE, sid, max_age=access.MAX_AGE, httponly=True, samesite="lax")
    return r


@app.post("/api/logout")
def api_logout(request: Request):
    access.logout(request)
    r = JSONResponse({"ok": True})
    r.delete_cookie(access.COOKIE)
    return r
STATIC = config.WEB_DIR / "static"


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/static/{name}")
def static_file(name: str):
    """Pliki statyczne; tylko pliki bezposrednio w web/static/."""
    path = STATIC / name
    if path.parent != STATIC or not path.is_file():
        return JSONResponse({"error": "nie znaleziono"}, status_code=404)
    return FileResponse(path)


@app.get("/api/version")
def version():
    return {"version": __version__, "db": db.describe(), "data_dir": str(config.DATA_DIR), "title": config.APP_TITLE,
            "service": config.SERVICE, "vallox": vallox.ENABLED, "pv": PV_ENABLED,
            "modules": {"pv": PV_ENABLED, "vallox": config.module_vallox(), "vallox_ip": bool(vallox.HOST)}}


@app.get("/api/config")
def api_config():
    return {"config": config.as_dict(), "file": str(config.CONFIG_FILE)}


@app.post("/api/config")
def api_config_save(payload: dict = Body(...)):
    """Zapis ustawien z zakladki Ustawienia; zaczynaja dzialac po restarcie (/api/restart)."""
    try:
        config.update(payload.get("config") or {})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True}


@app.get("/api/smartlife")
def api_smartlife():
    """Czy konto jest podlaczone i ktore gniazdko wybrane. Bez tokenu i User Code."""
    return smartlife.account_status()


@app.get("/api/smartlife/connect")
def api_smartlife_connect_status():
    return smartlife.connect_status()


@app.post("/api/smartlife/connect")
def api_smartlife_connect(payload: dict = Body(default={})):
    """User Code z aplikacji SmartLife. Puste = zapisany kod. Potem skan QR ze stanu polaczenia."""
    try:
        return smartlife.start_connect((payload or {}).get("user_code"))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/smartlife/device")
def api_smartlife_device(payload: dict = Body(default={})):
    try:
        return smartlife.select_device((payload or {}).get("id"))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/discover")
def api_discover(payload: dict = Body(default={})):
    """Szukanie falownika w sieci domowej: {"port": 502, "network": "192.168.0"} (oba opcjonalne)."""
    import ipaddress
    port = payload.get("port") or 502
    try:
        port = int(port)
        assert 1 <= port <= 65535
    except (TypeError, ValueError, AssertionError):
        return JSONResponse({"ok": False, "error": "zły numer portu"}, status_code=400)
    nets = None
    net = str(payload.get("network") or "").strip().rstrip(".")
    if net:
        try:
            if net.count(".") != 2 or not ipaddress.ip_address(net + ".1").is_private:
                raise ValueError
        except ValueError:
            return JSONResponse({"ok": False, "error": "podaj prywatną sieć w formacie 192.168.0"}, status_code=400)
        nets = [net]
    r = discover.scan(port, nets)
    r["ok"] = True
    return r


@app.post("/api/discover_vallox")
def api_discover_vallox(payload: dict = Body(default={})):
    """Szukanie rekuperatora Vallox MV w sieci domowej: {"network": "192.168.0"} (opcjonalnie)."""
    import ipaddress
    nets = None
    net = str((payload or {}).get("network") or "").strip().rstrip(".")
    if net:
        try:
            if net.count(".") != 2 or not ipaddress.ip_address(net + ".1").is_private:
                raise ValueError
        except ValueError:
            return JSONResponse({"ok": False, "error": "podaj prywatną sieć w formacie 192.168.0"}, status_code=400)
        nets = [net]
    try:
        r = discover.scan_vallox(nets)
    except ImportError:
        return JSONResponse({"ok": False, "error": "brak biblioteki vallox-websocket-api"}, status_code=400)
    r["ok"] = True
    r["current"] = vallox.HOST or None
    return r


@app.post("/api/discover_meters")
def api_discover_meters():
    """Wspolna lista miernikow poboru: Shelly w sieci domowej + gniazdka z podlaczonego konta SmartLife."""
    r = discover.scan_shelly()
    sl, sl_err = [], None
    try:
        sl = [dict(p, kind="smartlife") for p in smartlife.list_plugs()]
    except Exception as e:  # noqa: BLE001
        sl_err = str(e)
    cur = {"kind": meter.KIND or None, "id": meter.HOST if meter.KIND == "shelly" else
           (smartlife.account_status().get("device_id") if meter.KIND == "smartlife" else None)}
    return {"ok": True, "networks": r["networks"], "found": r["found"] + sl, "smartlife_error": sl_err,
            "smartlife_connected": smartlife.account_status().get("connected"), "current": cur}


METER_TEST = {"phase": "idle"}     # test "ktory miernik to rekuperator": Boost 2 min i porownanie poboru
_meter_test_lock = threading.Lock()


def _meter_powers(cands):
    """{(rodzaj, id): pobor W} dla kandydatow [{"kind": "shelly"|"smartlife", "id": ...}]."""
    out = {}
    sl = None
    for c in cands:
        k = (c.get("kind"), str(c.get("id")))
        try:
            if k[0] == "shelly":
                v = meter.read_shelly(k[1])
                out[k] = None if v.get("power") is None else float(v["power"])
            elif k[0] == "smartlife":
                if sl is None:
                    sl = {p["id"]: p.get("power_w") for p in smartlife.list_plugs()}
                out[k] = sl.get(k[1])
        except Exception:  # noqa: BLE001
            out[k] = None
    return out


def _meter_test_run(cands):
    try:
        base = _meter_powers(cands)
        METER_TEST.update(phase="boost", base={f"{k[0]}:{k[1]}": v for k, v in base.items()})
        r = vallox.boost(2)
        if not r.get("ok"):
            METER_TEST.update(phase="error", error=r.get("error"))
            return
        time.sleep(75)                    # wentylatory rozkrecaja sie ok. 30-60 s
        after = _meter_powers(cands)
        res = []
        for k, b in base.items():
            a = after.get(k)
            res.append({"kind": k[0], "id": k[1], "before": b, "after": a,
                        "delta": None if a is None or b is None else round(a - b, 1)})
        res.sort(key=lambda x: -(x["delta"] or -1e9))
        best = res[0] if res and (res[0]["delta"] or 0) >= 10 else None
        METER_TEST.update(phase="done", result=res, best=best, ts=int(time.time()))
    except Exception as e:  # noqa: BLE001
        METER_TEST.update(phase="error", error=str(e))


@app.post("/api/meter_test")
def api_meter_test(payload: dict = Body(...)):
    """Test na zadanie: Boost 2 min (rekuperator sam wraca do profilu) i ktory miernik pokazal wzrost poboru.
    {"candidates": [{"kind", "id"}], "confirm": true}."""
    if not payload.get("confirm"):
        return JSONResponse({"ok": False, "error": "brak potwierdzenia"}, status_code=400)
    if not vallox.ENABLED:
        return JSONResponse({"ok": False, "error": "najpierw ustaw rekuperator (adres IP) i uruchom ponownie"}, status_code=400)
    cands = [c for c in (payload.get("candidates") or []) if isinstance(c, dict) and c.get("kind") in ("shelly", "smartlife")]
    if not cands:
        return JSONResponse({"ok": False, "error": "brak mierników do sprawdzenia – najpierw „Szukaj mierników”"}, status_code=400)
    with _meter_test_lock:
        if METER_TEST.get("phase") in ("start", "boost"):
            return JSONResponse({"ok": False, "error": "test już trwa"}, status_code=400)
        METER_TEST.clear()
        METER_TEST.update(phase="start", started=int(time.time()))
    threading.Thread(target=_meter_test_run, args=(cands[:20],), daemon=True).start()
    return {"ok": True}


@app.get("/api/meter_test")
def api_meter_test_status():
    return METER_TEST


@app.post("/api/shutdown")
def api_shutdown():
    """Zamkniecie aplikacji (przycisk w zakladce Ustawienia)."""
    if config.SERVICE:
        return JSONResponse({"ok": False, "error": "aplikacja działa jako usługa Windows – zatrzymasz ją w Usługach Windows "
                             "(„Sigen PV Monitor”) albo odinstalowując usługę"}, status_code=400)
    threading.Thread(target=lambda: (time.sleep(1), os._exit(0)), daemon=True).start()
    return {"ok": True}


@app.post("/api/restart")
def api_restart():
    """Ponowne uruchomienie aplikacji (po zmianie ustawien)."""
    def _later():
        import subprocess
        time.sleep(1)
        if config.SERVICE:              # usluga Windows: restart przez WinSW ("restart!" = z wnetrza uslugi)
            svc = config.EXE_DIR / "SigenPVMonitor-service.exe"
            if svc.exists():
                try:
                    subprocess.Popen([str(svc), "restart!"], cwd=str(svc.parent), close_fds=True,
                                     creationflags=0x00000008 | 0x00000200)
                    time.sleep(30)      # WinSW zatrzyma ten proces
                except Exception as e:  # noqa: BLE001
                    print(f"restart uslugi: {e}")
            os._exit(1)                 # awaryjnie: odzyskiwanie uslugi (sc failureflag) uruchomi aplikacje ponownie
        args = [sys.executable] + (sys.argv[1:] if config.FROZEN else sys.argv)
        args = [a for a in args if a != "--wait-port"] + ["--wait-port", "--no-browser"]
        flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0   # DETACHED_PROCESS | NEW_PROCESS_GROUP
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")      # .exe: nowa, niezalezna instancja
        subprocess.Popen(args, close_fds=True, creationflags=flags, env=env)   # czeka na zwolnienie portu
        os._exit(0)
    threading.Thread(target=_later, daemon=True).start()
    return {"ok": True}



@app.get("/api/live")
def live():
    with lock:
        d = dict(latest)
    d.update(gridwatch.status())
    return d


@app.get("/api/grid_events")
def api_grid_events(limit: int = 50):
    """Historia zanikow sieci (praca off-grid)."""
    return {"events": gridwatch.events(max(1, min(limit, 300))), "modes": gridwatch.MODES}


def day_range(d):
    start = int(dt.datetime.combine(d, dt.time()).timestamp())
    end = int(dt.datetime.combine(d + dt.timedelta(days=1), dt.time()).timestamp())
    return start, end


def day_rows(con, start, end):
    return con.execute(
        'SELECT ts, soc, pv, bat, grid, plant, "load" FROM samples '
        "WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end)
    ).fetchall()


def actual_pv_kwh(con, day):
    """Produkcja PV dnia: licznik falownika, a gdy go brak - calkowanie mocy."""
    ce = counter_energy(con, day.isoformat())
    if "pv" in ce:
        return ce["pv"], "falownik"
    start, end = day_range(day)
    return energy(day_rows(con, start, end))["pv"], "obliczone"


def update_accuracy(con):
    """Trafnosc prognoz dla zakonczonych dni: wczoraj zawsze, starsze - jesli brak wpisu."""
    have = {r[0] for r in con.execute('SELECT DISTINCT "day" FROM forecast_accuracy').fetchall()}
    days = [r[0] for r in con.execute('SELECT DISTINCT "day" FROM forecast_src_day').fetchall()]
    today = dt.date.today()
    for ds in days:
        d = dt.date.fromisoformat(ds)
        if d >= today or (ds in have and d < today - dt.timedelta(days=1)):
            continue
        start, end = day_range(d)
        if not con.execute("SELECT COUNT(*) FROM samples WHERE ts >= ? AND ts < ?", (start, end)).fetchone()[0]:
            continue   # brak danych z instalacji z tego dnia
        kwh, _src = actual_pv_kwh(con, d)
        forecast.save_accuracy(con, ds, kwh)


@app.get("/api/forecast")
def api_forecast():
    today = dt.date.today()
    tomorrow = today + dt.timedelta(days=1)
    start, end = day_range(today)
    con = db_connect()
    try:
        sources = {}
        for src in forecast.SOURCES:
            sources[src] = {"today": forecast.load_day(con, today.isoformat(), src),
                            "tomorrow": forecast.load_day(con, tomorrow.isoformat(), src)}
        rows = day_rows(con, start, end)
        actual_kwh, actual_src = actual_pv_kwh(con, today)
        acc = forecast.accuracy(con)
    finally:
        con.close()

    # rzeczywista moc PV dzis: srednie 10-minutowe, minuta doby czasu lokalnego
    buckets = {}
    for ts, _soc, pv, *_ in rows:
        t = dt.datetime.fromtimestamp(ts)
        b = (t.hour * 60 + t.minute) // 10 * 10
        buckets.setdefault(b, []).append(pv)
    actual = [[b, round(sum(v) / len(v), 3)] for b, v in sorted(buckets.items())]

    now = dt.datetime.now()
    now_min = now.hour * 60 + now.minute
    main = sources[forecast.GLOWNE_ZRODLO]
    f_today = main["today"]
    return {
        "status": forecast.status(),
        "main": forecast.GLOWNE_ZRODLO,
        "names": forecast.SOURCES,
        "groups": forecast.GROUPS,
        "models": forecast.MODEL_SOURCES,
        "now_min": now_min,
        "today": f_today,
        "tomorrow": main["tomorrow"],
        "sources": sources,
        "actual": actual,
        "actual_kwh": actual_kwh,
        "actual_src": actual_src,
        "forecast_until_now_kwh": forecast.kwh_until(f_today["points"], now_min) if f_today else None,
        "accuracy": acc,
        "sun": {"today": forecast.sun_times(today), "tomorrow": forecast.sun_times(tomorrow)},
        "margin_min": CHART_SUN_MARGIN,
    }


@app.get("/api/pv_strings")
def api_pv_strings(date: str | None = None):
    """Rzeczywista moc kazdej polaci (U x I) w ciagu dnia + energia dzienna."""
    d = dt.date.fromisoformat(date) if date else dt.date.today()
    start, end = day_range(d)
    con = db_connect()
    try:
        rows = db.load_pv_strings(con, start, end)
    finally:
        con.close()
    used = config.strings()
    names = [n for n, _k in used]
    pts, kwh = [], {n: 0.0 for n in names}
    prev = None
    for ts, ui in rows:
        p = [max((ui[k - 1][0] or 0) * (ui[k - 1][1] or 0), 0) / 1000 for _n, k in used]
        if prev and 0 < ts - prev[0] <= 300:          # calkowanie, przerwy > 5 min pomijane
            h = (ts - prev[0]) / 3600
            for n, a, b in zip(names, prev[1], p):
                kwh[n] += (a + b) / 2 * h
        prev = (ts, p)
        pts.append({"ts": ts, "t": dt.datetime.fromtimestamp(ts).strftime("%H:%M"),
                    **{n: round(v, 3) for n, v in zip(names, p)}})
    total = sum(kwh.values())
    return {"date": d.isoformat(), "names": names, "points": pts,
            "sun": forecast.sun_times(d), "margin_min": CHART_SUN_MARGIN,
            "kwh": {n: round(v, 2) for n, v in kwh.items()},
            "share": {n: round(100 * v / total, 1) if total > 0.01 else None for n, v in kwh.items()}}




@app.get("/api/day")
def day(date: str | None = None):
    d = dt.date.fromisoformat(date) if date else dt.date.today()
    start, end = day_range(d)
    con = db_connect()
    rows = day_rows(con, start, end)
    # srednie 1-minutowe do wykresu
    series = con.execute(
        'SELECT (ts/60)*60 AS m, AVG(soc), AVG(pv), AVG(bat), AVG(grid), AVG("load"), '
        "AVG(pv - bat - plant), "
        "AVG(pv + CASE WHEN bat < 0 THEN -bat ELSE 0 END + CASE WHEN plant < 0 THEN -plant ELSE 0 END) "
        "FROM samples WHERE ts >= ? AND ts < ? GROUP BY (ts/60)*60 ORDER BY m", (start, end)
    ).fetchall()
    ce = counter_energy(con, d.isoformat())
    con_eff, _n, split = eff_compute_day(con, d, split=True)
    con.close()
    e = eff_into_energy(merge_energy(energy(rows), ce), con_eff)
    e["straty"] = split
    return {
        "date": d.isoformat(),
        "energy": e,
        "series": [
            {"t": m, "soc": round(s, 1), "pv": round(p, 3), "bat": round(b, 3),
             "grid": round(g, 3), "load": round(l, 3), "loss": round(x, 3), "pin": round(i, 3)}
            for m, s, p, b, g, l, x, i in series
        ],
    }


# przedzialy mocy przetwarzanej [kW] do tabeli sprawnosci
EFF_BINS = [0, 0.25, 0.5, 0.75, 1, 2, 3, 4, 6, 8]
CHART_SUN_MARGIN = 30    # min - wykresy PV: od wschodu + 30 min do zachodu - 30 min
EFF_INTERVAL = 600       # s - co ile przeliczac i zapisywac dzisiejsze przedzialy


EFF_MIN_HOURS = 0.25      # przedzial z mniejsza iloscia danych oznaczany jako "mala probka"
EFF_MIN_SAMPLES = 6       # min. probek 5 s w minucie (30 s danych)
EFF_MAX_SWING = 0.20      # minuta odrzucana, gdy moc wejsciowa zmienia sie w niej o > 20 %


STANDBY_KW = 0.03          # ponizej tej mocy PV i magazynu falownik nie przetwarza energii (tryb czuwania)
CTRL_MIN_KW = 0.03        # odchylka regulacji liczona powyzej tej mocy z/do sieci


def eff_minutes(rows, dc_by_min):
    """Minutowe srednie do liczenia sprawnosci: [(godzin, p_in, p_out)].

    - wejscie PV = moc DC stringow (U x I) z danej minuty; bez niej minuta z produkcja PV jest
      pomijana (rejestr 30035 jest za przetwornica i zaniza straty do ~0),
    - minuty bez PV (np. noc, tylko magazyn) liczone z rejestrow mocy,
    - minuty z gwaltowna zmiana mocy (rozjazd odczytow) odrzucane.
    """
    return [(h, i, o) for h, i, o, _m, _b in eff_minutes_ex(rows, dc_by_min)]


def eff_minutes_ex(rows, dc_by_min):
    """Jak eff_minutes, ale z trybem minuty: 'konwersja' albo 'czuwanie' (brak przetwarzania PV/magazynu)
    i moca magazynu (+ ladowanie, - rozladowanie): [(godzin, p_in, p_out, tryb, bat)]."""
    buckets = {}
    for ts, pv, bat, pl, *_ in rows:
        buckets.setdefault(ts // 60, []).append((pv, bat, pl))
    out = []
    for m, smp in buckets.items():
        if len(smp) < EFF_MIN_SAMPLES:
            continue
        n = len(smp)
        pv = sum(x[0] for x in smp) / n
        bat = sum(x[1] for x in smp) / n
        pl = sum(x[2] for x in smp) / n
        ins = [max(x[0], 0) + max(-x[1], 0) + max(-x[2], 0) for x in smp]
        mean_in = sum(ins) / n
        if mean_in > 0.05 and (max(ins) - min(ins)) / mean_in > EFF_MAX_SWING:
            continue
        if pv > 0.05:
            dc = dc_by_min.get(m)
            if dc is None:
                continue
            pv_in = max(dc, pv)          # moc przed DC/DC >= moc za nia
        else:
            pv_in = max(pv, 0)
        p_in = pv_in + max(-bat, 0) + max(-pl, 0)
        p_out = max(bat, 0) + max(pl, 0)
        if p_out > p_in:                 # ujemna strata - rozjazd odczytow, minuta odrzucana
            continue
        mode = "czuwanie" if pv_in < STANDBY_KW and abs(bat) < STANDBY_KW else "konwersja"
        out.append((1 / 60, p_in, p_out, mode, bat))
    return out


def loss_split(rows, dc_by_min, soc_min=None, soc_max=None):
    """Podzial strat dnia wg metodyki HTW Berlin (Energy Storage Inspection), w kWh:

    - konwersja: straty przetwarzania energii (PV->AC, PV->magazyn, magazyn->AC, siec->magazyn),
    - czuwanie: zuzycie wlasne falownika i magazynu, gdy nic nie przetwarza (np. noc przy pustym magazynie),
    - regulacja: pobor z sieci, gdy magazyn mogl pokryc zuzycie, i oddawanie do sieci, gdy mogl sie ladowac
      (powolna reakcja sterowania); to energia "przepuszczona" przez siec, nie strata w falowniku.
    rows: (ts, pv, bat, plant, grid, soc)
    """
    out = {"konwersja": 0.0, "czuwanie": 0.0, "h_czuwanie": 0.0, "regulacja_pobor": 0.0, "regulacja_oddanie": 0.0}
    for h, p_in, p_out, mode, _bat in eff_minutes_ex(rows, dc_by_min):
        out[mode] += (p_in - p_out) * h
        if mode == "czuwanie":
            out["h_czuwanie"] += h
    lo = (soc_min if soc_min is not None else 10) + 2
    hi = (soc_max if soc_max is not None else 100) - 2
    buckets = {}
    for ts, _pv, bat, _pl, grid, soc in rows:
        buckets.setdefault(ts // 60, []).append((bat, grid, soc))
    for smp in buckets.values():
        n = len(smp)
        bat = sum(x[0] for x in smp) / n
        grid = sum(x[1] for x in smp) / n
        soc = sum(x[2] for x in smp) / n
        h = 1 / 60
        if grid > CTRL_MIN_KW and bat < -CTRL_MIN_KW and soc > lo:      # rozladowanie nie nadaza za zuzyciem
            out["regulacja_pobor"] += grid * h
        elif grid < -CTRL_MIN_KW and bat > CTRL_MIN_KW and soc < hi:    # ladowanie nie nadaza za nadwyzka PV
            out["regulacja_oddanie"] += -grid * h
    res = {k: round(v, 3) for k, v in out.items()}
    res["czuwanie_sr_w"] = round(1000 * out["czuwanie"] / out["h_czuwanie"]) if out["h_czuwanie"] > 0.25 else None
    return res


IDLE_BIN = -1      # przedzial "czuwanie": falownik nic nie przetwarza (PV i magazyn < 30 W) - zuzycie wlasne z sieci
# Ladowanie magazynu wg mocy ladowania: minuty, w ktorych >= 70 % energii wyjsciowej idzie do magazynu (PV -> magazyn,
# siec -> magazyn), zapisywane w efficiency_daily jako bin_from = CHG_BIN_OFFSET + od [kW] (ten sam podzial EFF_BINS).
CHG_BIN_OFFSET = 100
CHG_DOMINANT = 0.7
ALL_BINS = [IDLE_BIN] + EFF_BINS + [CHG_BIN_OFFSET + b for b in EFF_BINS]


def eff_bins_from_rows(rows, dc_by_min=None):
    """{od: [godzin, e_in, e_out]} z probek (ts, pv, bat, plant) i mocy DC stringow {minuta: kW}.
    Minuty czuwania (bez przetwarzania PV / magazynu) - osobno w IDLE_BIN, nie zanizaja sprawnosci przy malej mocy."""
    acc = {lo: [0.0, 0.0, 0.0] for lo in ALL_BINS}
    for h, p_in, p_out, mode, bat in eff_minutes_ex(rows, dc_by_min or {}):
        los = [IDLE_BIN if mode == "czuwanie" else max(b for b in EFF_BINS if p_in >= b)]
        if mode == "konwersja" and bat >= STANDBY_KW and p_out > 0 and bat >= CHG_DOMINANT * p_out:
            los.append(CHG_BIN_OFFSET + max(b for b in EFF_BINS if bat >= b))
        for lo in los:
            a = acc[lo]
            a[0] += h
            a[1] += p_in * h
            a[2] += p_out * h
    return acc


def eff_compute_day(con, day, split=False):
    start, end = day_range(day)
    rows = con.execute("SELECT ts, pv, bat, plant, grid, soc FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts",
                       (start, end)).fetchall()
    dc = {}
    used = [k for _n, k in config.strings()]
    for ts, ui in db.load_pv_strings(con, start, end):
        if all(ui[k - 1][0] is not None and ui[k - 1][1] is not None for k in used):
            dc[ts // 60] = sum(max(ui[k - 1][0] * ui[k - 1][1], 0) / 1000 for k in used)
    if split:
        with lock:
            sv = dict(settings_state["values"])
        return eff_bins_from_rows(rows, dc), len(rows), loss_split(rows, dc, sv.get("soc_discharge_cut"), sv.get("soc_charge_cut"))
    return eff_bins_from_rows(rows, dc), len(rows)


def eff_store_day(con, day):
    acc, n = eff_compute_day(con, day)
    if n:
        db.save_efficiency_day(con, day.isoformat(),
                               [(lo, *v) for lo, v in acc.items() if v[0] > 0], int(time.time()))
    return n


def efficiency_loop():
    """Zapis dziennych przedzialow sprawnosci: przy starcie uzupelnia brakujace dni,
    potem co EFF_INTERVAL przelicza dzis (a po polnocy jeszcze raz domyka wczoraj)."""
    con = None
    last_day = None
    while True:
        try:
            if con is None:
                con = db_connect()
                first = con.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
                if first:   # przy starcie przeliczamy wszystkie dni (zmiany metody liczenia)
                    d = dt.date.fromtimestamp(first)
                    while d < dt.date.today():
                        eff_store_day(con, d)
                        d += dt.timedelta(days=1)
            today = dt.date.today()
            if last_day and last_day != today:
                eff_store_day(con, last_day)          # domkniecie poprzedniego dnia
            eff_store_day(con, today)
            update_accuracy(con)
            last_day = today
        except Exception as e:  # noqa: BLE001
            with lock:
                latest["db_error"] = f"sprawnosc: {e}"
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
            con = None
        time.sleep(EFF_INTERVAL)


@app.get("/api/efficiency")
def efficiency(date: str | None = None, days: int = 1):
    """Sprawnosc falownika w przedzialach mocy przetwarzanej (z tabeli efficiency_daily).

    days: 1 = wybrany dzien, N = N dni konczacych sie tym dniem, 0 = cala historia.
    Dzisiejszy dzien liczony na biezaco z probek.
    """
    d = dt.date.fromisoformat(date) if date else dt.date.today()
    first = None if days <= 0 else (d - dt.timedelta(days=days - 1)).isoformat()
    con = db_connect()
    try:
        acc = {lo: [0.0, 0.0, 0.0] for lo in ALL_BINS}
        today = dt.date.today()
        for day, lo, h, e_in, e_out in db.load_efficiency(con, first, d.isoformat()):
            if day == today.isoformat():
                continue
            if lo in acc:
                a = acc[lo]
                a[0] += h
                a[1] += e_in
                a[2] += e_out
        n_days = len({r for r in db.efficiency_days(con)
                      if (first is None or r >= first) and r <= d.isoformat() and r != today.isoformat()})
        if (first is None or today.isoformat() >= first) and today <= d:
            live, n = eff_compute_day(con, today)
            for lo, v in live.items():
                for k in range(3):
                    acc[lo][k] += v[k]
            n_days += 1 if n else 0
    finally:
        con.close()
    def _bins(off):
        res = []
        for i, lo in enumerate(EFF_BINS):
            h, e_in, e_out = acc[off + lo]
            if h < 1 / 60:   # mniej niz minuta danych - pomijamy
                continue
            loss = e_in - e_out
            res.append({
                "od": lo, "do": EFF_BINS[i + 1] if i + 1 < len(EFF_BINS) else None,
                "godzin": round(h, 2),
                "e_in": round(e_in, 2),
                "e_out": round(e_out, 2),
                "straty_kwh": round(loss, 3),
                "strata_sr_w": round(1000 * loss / h),
                "moc_sr_kw": round((e_out if off else e_in) / h, 2),
                "sprawnosc": round(100 * e_out / e_in, 1) if e_in > 0.01 else None,
                "mala_probka": h < EFF_MIN_HOURS,
            })
        return res
    out, chg = _bins(0), _bins(CHG_BIN_OFFSET)
    h, e_in, e_out = acc[IDLE_BIN]
    idle = {"godzin": round(h, 2), "kwh": round(e_in - e_out, 3), "sr_w": round(1000 * (e_in - e_out) / h)} if h >= 1 / 60 else None
    return {"date": d.isoformat(), "days": days, "n_days": n_days, "bins": out, "ladowanie": chg, "czuwanie": idle}


@app.get("/api/registers")
def api_registers():
    """Wszystkie odczyty falownika, pogrupowane."""
    with lock:
        vals, ts = dict(registers_state["values"]), registers_state["ts"]
    return {"ts": ts, "groups": registers.grouped(vals)}


MONTHS = ["styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec", "lipiec", "sierpień", "wrzesień",
          "październik", "listopad", "grudzień"]


# --- produkcja PV w miesiacach (licznik narastajacy produkcji PV instalacji) ---------
MONTHS_SHORT = ["sty", "lut", "mar", "kwi", "maj", "cze", "lip", "sie", "wrz", "paź", "lis", "gru"]


@app.get("/api/pv_monthly")
def api_pv_monthly(months: int = 12):
    """Produkcja PV w miesiacach z licznika narastajacego pv_total (ta sama wartosc co "Produkcja PV (instalacja)").

    Miesiac = przyrost licznika od ostatniego odczytu poprzedniego dnia. Energia wyprodukowana przed pierwszym
    odczytem (od uruchomienia instalacji do startu aplikacji) jest doliczana do pierwszego miesiaca zapisu.
    """
    con = db_connect()
    try:
        rows = con.execute('SELECT "day", "first", "last" FROM counters WHERE "key" = ? ORDER BY "day"',
                           ("pv_total",)).fetchall()
    finally:
        con.close()
    out, prev = {}, None
    for day, first, last in rows:
        if first is None or last is None:
            continue
        m = out.setdefault(day[:7], {"kwh": 0.0, "pre": 0.0, "days": 0})
        if prev is None:
            m["pre"] = float(first)                  # przed rozpoczeciem zapisu
            base = float(first)
        else:
            base = prev
        m["kwh"] += max(float(last) - base, 0.0)
        m["days"] += 1
        prev = float(last)
    res = []
    for ym, m in sorted(out.items())[-months:]:
        y, mo = map(int, ym.split("-"))
        res.append({"month": ym, "label": f"{MONTHS_SHORT[mo - 1]} {y}", "days": m["days"],
                    "kwh": round(m["kwh"] + m["pre"], 1), "pre": round(m["pre"], 1)})
    return {"months": res, "total": round(prev, 1) if prev is not None else None}

@app.get("/api/pstryk")
def api_pstryk(date: str | None = None):
    """Licznik operatora z API Pstryka (dzien + miesiac) i porownanie z licznikami Sigena. Bez klucza: enabled=False."""
    st = pstryk.status()
    if not st["enabled"]:
        return {"enabled": False}
    d = dt.date.fromisoformat(date) if date else dt.date.today()
    start, end = day_range(d)
    m0 = d.replace(day=1)
    m1 = (m0 + dt.timedelta(days=32)).replace(day=1)
    ms, _x = day_range(m0)
    me, _x = day_range(m1)
    con = db_connect()
    try:
        hours = pstryk.load(con, start, end)
        month = pstryk.load(con, ms, me)
        e = merge_energy(energy(day_rows(con, start, end)), counter_energy(con, d.isoformat()))
    finally:
        con.close()
    ds = pstryk.summary(hours)
    # porownanie tylko dla godzin, ktore operator juz przekazal (dzis dane doplywaja z opoznieniem)
    return {"enabled": True, "status": st, "date": d.isoformat(), "hours": hours, "day": ds,
            "month": {**pstryk.summary(month), "label": f"{MONTHS[d.month - 1]} {d.year}",
                      "days": len({r["day"] for r in month if r.get("imp") is not None})},
            "sigen": {"imp": e.get("imp"), "exp": e.get("exp")}}


# --- strategia osobista (plan; wykonanie przez inverter_ctl.py tylko po wlaczeniu na stronie) -----------------
STRAT = {"cap_kwh": config.getf("strategia", "pojemnosc_kwh", 0) or None,
         "p_max_kw": config.getf("strategia", "moc_ladowania_kw", 5.0),
         "eta_c": config.getf("strategia", "sprawnosc_ladowania", 0.95),
         "eta_d": config.getf("strategia", "sprawnosc_rozladowania", 0.95),
         "sell_factor": config.getf("strategia", "wartosc_sprzedazy", 1.0),
         "wear": config.getf("strategia", "koszt_zuzycia_zl_kwh", 0.10)}
EST_SELL = 0.9   # gdy brak cen na jutro (przyjete z dzis), cena sprzedazy liczona ostrozniej
_strat_cache = {"ts": 0, "data": None}
LIMIT_KEYS = {"sell_floor": "sprzedaz_z_magazynu_od", "buy_cap": "ladowanie_z_sieci_do",
              "p_charge": "moc_ladowania_z_sieci_maks_kw", "p_discharge": "moc_sprzedazy_maks_kw",
              "standby_below_w": "standby_ponizej_w", "tarcza": "tarcza_limit_zl_kwh",
              "tarcza_rezerwa": "tarcza_rezerwa_proc", "legal_days": "sprzedaz_tylko_z_pv_dni"}
LIMIT_RANGE = {"p_charge": (0.5, 30, "moc 0,5–30 kW"), "p_discharge": (0.5, 30, "moc 0,5–30 kW"),
               "standby_below_w": (0, 3000, "próg 0–3000 W"), "tarcza": (0, 5, "limit tarczy 0–5 zł/kWh"),
               "tarcza_rezerwa": (0, 100, "rezerwa 0–100 %"), "legal_days": (0, 366, "okres 0–366 dni")}
LIMIT_DEFAULTS = {"tarcza": "0.61", "tarcza_rezerwa": "60", "legal_days": "7"}
RESERVE_FACTOR = 0.8   # rezerwa pod sprzedaz wyceniana na 80 % pulapu sprzedazy (sprzedaz w przyszlosci niepewna)
def legal_budget(days):
    """Limit sprzedazy (arbitraz zgodny z prawem): produkcja PV - energia oddana do sieci w ostatnich `days` dniach
    (dzis wlacznie), z licznikow falownika (tabela counters: pv_total, exp_total). {"pv", "exp", "budget"} [kWh]."""
    first = (dt.date.today() - dt.timedelta(days=max(1, int(days)) - 1)).isoformat()
    con = db_connect()
    try:
        r = {}
        for key in ("pv_total", "exp_total"):
            row = con.execute('SELECT MIN("first"), MAX("last") FROM counters WHERE "key" = ? AND "day" >= ?',
                              (key, first)).fetchone()
            r[key] = max(0.0, float(row[1]) - float(row[0])) if row and row[0] is not None and row[1] is not None else None
    finally:
        con.close()
    if r["pv_total"] is None or r["exp_total"] is None:
        return None
    return {"pv": round(r["pv_total"], 2), "exp": round(r["exp_total"], 2),
            "budget": round(max(0.0, r["pv_total"] - r["exp_total"]), 2), "days": int(days)}


def _legal_caps(hours, pl, budget):
    """Godziny sprzedazy z magazynu ponad limit: limit rosnie o prognozowana produkcje PV i maleje o oddawanie do sieci.
    Zwraca {indeks godziny: maks. energia z magazynu do sieci [kWh]} albo {}, gdy plan miesci sie w limicie."""
    caps, b = {}, budget
    for t, (h, p) in enumerate(zip(hours, pl)):
        b += max(h["pv"], 0.0)
        b -= max(-p["grid"], 0.0)
        if b < -0.01 and p["mode"] == "export":
            bat_exp = max(0.0, -p["d"] - max(h["load"] - h["pv"], 0.0))
            cut = min(-b, bat_exp)
            caps[t] = max(0.0, bat_exp - cut)
            b += cut
    return caps


TARCZA_MARGIN_DEFAULT = 0.098   # zl/kWh brutto - marza / oplata handlowa Pstryka (0,08 netto), gdy brak danych


def strat_limits():
    """Pulapy cenowe strategii z config.ini [strategia] (czytane na biezaco - zmiana bez restartu); puste = brak."""
    cp = config.load()
    out = {}
    for k, key in LIMIT_KEYS.items():
        v = cp.get("strategia", key, fallback=LIMIT_DEFAULTS.get(k, "")).strip().replace(",", ".")
        try:
            out[k] = float(v) if v else None
        except ValueError:
            out[k] = None
    out["tarcza_mode"] = cp.get("strategia", "tarcza_tryb", fallback="auto").strip().lower() or "auto"   # auto | tak | nie
    return out


def tarcza_info(lim, now=None):
    """Tarcza Pstryk: czy dziala w biezacym miesiacu i efektywna cena zakupu.

    Tarcza ogranicza srednia cene samej energii w miesiacu (z VAT) do limitu. Gdy srednia jest powyzej limitu, kazda
    kWh energii kosztuje tyle samo (limit) - niezaleznie od godziny zakupu. Zostaja skladniki zalezne od kWh, ktorych
    tarcza nie obejmuje: marza / oplata handlowa Pstryka, dystrybucja zmienna, akcyza (z VAT).
    efektywna cena zakupu = limit + marza + dystrybucja + akcyza  [zl/kWh brutto]."""
    cap = lim.get("tarcza") or 0.61
    mode = lim.get("tarcza_mode", "auto")
    out = {"cap": cap, "mode": mode, "active": False, "avg": None, "eff_buy": None, "margin": None, "rest": None}
    if not pstryk.enabled() or mode == "nie":
        out["reason"] = "wyłączona" if mode == "nie" else "brak Pstryka"
        return out
    now = now or time.time()
    d = dt.date.fromtimestamp(now)
    ms = day_range(d.replace(day=1))[0]
    con = db_connect()
    try:
        month = pstryk.load(con, ms, int(now) + 3600)
        recent = pstryk.load(con, int(now) - 14 * 86400, int(now) + 3600)
    finally:
        con.close()
    imp = sum(r.get("imp") or 0 for r in month)
    if imp > 0.5:
        out["avg"] = round(sum(r.get("energy_net") or 0 for r in month) * 1.23 / imp, 3)
        out["month_kwh"] = round(imp, 1)
    rimp = sum(r.get("imp") or 0 for r in recent)
    if rimp > 0.5:
        out["margin"] = round(sum(r.get("service") or 0 for r in recent) * 1.23 / rimp, 4)
        out["rest"] = round(sum((r.get("dist_var") or 0) + (r.get("excise") or 0) for r in recent) * 1.23 / rimp, 4)
    margin = out["margin"] if out["margin"] is not None else TARCZA_MARGIN_DEFAULT
    rest = out["rest"] if out["rest"] is not None else 0.10
    out["eff_buy"] = round(cap + margin + rest, 4)
    out["active"] = mode == "tak" or (out["avg"] is not None and out["avg"] > cap)
    out["reason"] = ("włączona ręcznie" if mode == "tak" else
                     f"średnia energii {out['avg']:.2f} zł > limit {cap:.2f} zł" if out["active"] else
                     f"średnia energii {out['avg']:.2f} zł ≤ limit – ceny godzinowe" if out["avg"] is not None else "za mało danych w miesiącu")
    return out


def _interp(pts, m):
    if not pts or m < pts[0][0] or m > pts[-1][0]:
        return 0.0
    for k in range(1, len(pts)):
        (m1, p1), (m2, p2) = pts[k - 1], pts[k]
        if m <= m2:
            return p2 if m2 == m1 else p1 + (p2 - p1) * (m - m1) / (m2 - m1)
    return 0.0


def _hourly_pv(con, day):
    """Prognoza PV [kWh] dla kazdej godziny doby (biezaca prognoza)."""
    try:
        f = forecast.load_day(con, day.isoformat(), latest=True)
    except Exception:  # noqa: BLE001
        f = None
    pts = (f or {}).get("points") or []
    return [sum(_interp(pts, h * 60 + k * 5) for k in range(12)) / 12 for h in range(24)]


def _load_profile(con, now):
    """Typowe zuzycie domu [kWh] w kazdej godzinie doby - srednia z 14 dni."""
    rows = con.execute('SELECT ts / 3600, AVG("load") FROM samples WHERE ts >= ? GROUP BY ts / 3600',
                       (int(now) - 14 * 86400,)).fetchall()
    by = {}
    for hts, v in rows:
        if v is not None:
            by.setdefault(time.localtime(int(hts) * 3600).tm_hour, []).append(float(v))
    allv = [x for v in by.values() for x in v]
    dflt = sum(allv) / len(allv) if allv else 0.3
    return [sum(by[h]) / len(by[h]) if by.get(h) else dflt for h in range(24)], len(rows)


def _hourly_prices(con, day):
    """{poczatek godziny: (zakup, sprzedaz)} - zakup z Pstryka (jesli jest) albo z PSE + taryfy."""
    pse = {}
    try:
        pts, _t = prices.series(con, day.isoformat())
    except Exception:  # noqa: BLE001
        pts = []
    for p in pts:
        pse.setdefault(p["ts"] // 3600 * 3600, []).append((p["buy"], p["sell"]))
    out = {h: (sum(b for b, _ in v) / len(v), sum(s for _, s in v) / len(v)) for h, v in pse.items()}
    src = "PSE"
    if pstryk.enabled():
        s, e = day_range(day)
        rows = [r for r in pstryk.load(con, s, e) if r.get("price_gross") is not None]
        if rows:
            src = "Pstryk"
            # sprzedaz: cena prosumencka Pstryka (depozyt Pstryka pokrywa caly rachunek), bez niej - z PSE
            out = {r["ts"]: (r["price_gross"], r["price_pros"] if r.get("price_pros") is not None
                             else out.get(r["ts"], (None, 0.0))[1]) for r in rows}
    return out, src


def _price_model(con, days=7):
    """Przeliczenie ceny rynkowej RDN [zl/kWh] na cene zakupu i sprzedazy (jak w danych z Pstryka / PSE+taryfa):
    regresja liniowa z ostatnich dni (para: cena rynkowa z PSE w godzinie <-> cena zakupu / sprzedazy tej godziny).
    Zwraca funkcje m -> (zakup, sprzedaz)."""
    xs, buys, sells = [], [], []
    today = dt.date.today()
    for k in range(1, days + 1):
        d = today - dt.timedelta(days=k)
        try:
            pts, _t = prices.series(con, d.isoformat())
        except Exception:  # noqa: BLE001
            continue
        mk = {}
        for p in pts:
            base = p.get("csdac") if prices.ZAKUP_BAZA == "csdac" else p.get("rce")
            if base is not None:
                mk.setdefault(p["ts"] // 3600 * 3600, []).append(base / 1000)
        hp, _src = _hourly_prices(con, d)
        for h, (b, sl) in hp.items():
            if h in mk and b is not None and sl is not None:
                xs.append(sum(mk[h]) / len(mk[h]))
                buys.append(b)
                sells.append(sl)

    def fit(ys):
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        vx = sum((x - mx) ** 2 for x in xs)
        a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx if vx > 1e-9 else 1.0
        return a, my - a * mx
    if len(xs) >= 24:
        (ab, bb), (as_, bs) = fit(buys), fit(sells)
        return lambda m: (ab * m + bb, max(0.0, as_ * m + bs)), f"dopasowanie z {len(xs)} h"
    tariff = prices.load_tariff()

    def via_tariff(m, ts=None):
        z = prices.zone_for(tariff, dt.datetime.fromtimestamp(ts) if ts else dt.datetime.now())
        pp = prices.price_parts(tariff, m * 1000, m * 1000, z)
        return pp["buy"], pp["sell"]
    return via_tariff, "taryfa"


def _market_actual(con):
    """Funkcja: data ISO -> {godzina: cena rynkowa zl/kWh} z PSE (do oceny trafnosci prognoz Pradcast)."""
    def f(iso):
        try:
            pts, _t = prices.series(con, iso)
        except Exception:  # noqa: BLE001
            return {}
        by = {}
        for p in pts:
            v = p.get("csdac") if p.get("csdac") is not None else p.get("rce")
            if v is not None:
                by.setdefault(time.localtime(p["ts"]).tm_hour, []).append(v / 1000)
        return {h: sum(v) / len(v) for h, v in by.items()}
    return f


_hour_lock = {}            # {"ts": poczatek godziny, "mode": tryb planu, "at": kiedy ustalony} - blokada trybu na godzine
_eta_cache = {"ts": 0, "data": [], "chg": []}
ETA_C_MIN = 0.80           # PV do magazynu (EMS 4) dopiero od mocy, przy ktorej zmierzona sprawnosc ladowania >= 80 %
PVCHG_MIN_FLOOR = 0.5      # i nie ponizej 0,5 kW (krok limitu ladowania w sterowaniu)
ETA_CURVE_DAYS = 30
ETA_CURVE_MAX_KW = 1.0     # krzywa tylko dla malych mocy - wyzej stala sprawnosc STRAT["eta_d"]


def _eta_curve():
    """Sprawnosc falownika przy malej mocy z pomiarow (tabela sprawnosci, ostatnie 30 dni): [{od, do, eta, godzin}].
    Przedzialy z co najmniej 1 h danych; odswiezane co godzine."""
    now = time.time()
    if now - _eta_cache["ts"] < 3600:
        return _eta_cache["data"]
    out, chg = [], []
    try:
        eff = efficiency(None, ETA_CURVE_DAYS)
        for key, dst in (("bins", out), ("ladowanie", chg)):
            for b in eff.get(key) or []:
                if b["od"] < ETA_CURVE_MAX_KW and b["godzin"] >= 1 and b["sprawnosc"]:
                    dst.append({"od": b["od"], "do": b["do"] or ETA_CURVE_MAX_KW,
                                "eta": round(max(0.4, min(0.97, b["sprawnosc"] / 100)), 3),
                                "godzin": b["godzin"], "strata_w": b["strata_sr_w"]})
    except Exception:  # noqa: BLE001 - bez krzywej plan liczy ze stala sprawnoscia
        out, chg = [], []
    _eta_cache.update(ts=now, data=out, chg=chg)
    return out


def _eta_c_curve():
    """Sprawnosc ladowania magazynu przy malej mocy ladowania (pomiary 30 dni): [{od, do, eta, godzin}]."""
    _eta_curve()
    return _eta_cache["chg"]


def _pvchg_min_kw(chg):
    """Prog trybu PV do magazynu z pomiarow: koniec najwyzszego przedzialu ponizej 1 kW ze sprawnoscia < ETA_C_MIN."""
    thr = PVCHG_MIN_FLOOR
    for c in chg:
        if c["eta"] < ETA_C_MIN and c["do"] > thr:
            thr = c["do"]
    return round(min(thr, ETA_CURVE_MAX_KW), 2)


def compute_strategy():
    now = time.time()
    if _strat_cache["data"] and now - _strat_cache["ts"] < 300:
        return _strat_cache["data"]
    with lock:
        soc = latest.get("soc")
        regs = dict(registers_state["values"])
        sv = dict(settings_state["values"])
    cap = STRAT["cap_kwh"] or regs.get("cap_ess") or regs.get("cap_bat")
    if not cap or soc is None:
        return {"ok": False, "error": "czekam na odczyt SOC i pojemności magazynu z falownika"}
    cap = float(cap)
    smin = float(sv.get("soc_discharge_cut") or 10) / 100 * cap
    smax = float(sv.get("soc_charge_cut") or 100) / 100 * cap
    today = dt.date.today()
    use_pc = pradcast.enabled()
    days = [today + dt.timedelta(days=k) for k in range(3 if use_pc else 2)]   # z Pradcast - do pojutrza
    DAY_TXT = {0: "dziś", 1: "jutro", 2: "pojutrze"}
    con = db_connect()
    try:
        prof, nprof = _load_profile(con, now)
        pv = {d: _hourly_pv(con, d) for d in days}
        pr, srcs = {}, set()
        for d in days:
            p, src = _hourly_prices(con, d)
            pr.update(p)
            if p:
                srcs.add(src)
        fc = {}                     # prognoza cen Pradcast: ts godziny -> (zakup, sprzedaz)
        pc_info = None
        if use_pc:
            model, model_src = _price_model(con)
            for d in days[1:]:
                r = pradcast.day(d)
                if not r or not r["hours"]:
                    continue
                pc_info = {"model": model_src, "horizon": r.get("horizon"), "source": r.get("source")}
                for hr, v in r["hours"].items():
                    ts = int(time.mktime(dt.datetime(d.year, d.month, d.day, hr).timetuple()))
                    try:
                        fc[ts] = model(v["price_kwh"], ts) if model_src == "taryfa" else model(v["price_kwh"])
                    except Exception:  # noqa: BLE001
                        pass
            if fc:
                srcs.add("Pradcast (prognoza)")
            try:
                pc_eval = pradcast.evaluate(_market_actual(con))
            except Exception:  # noqa: BLE001
                pc_eval = None
            pc_info = dict(pc_info or {}, eval=pc_eval)
    finally:
        con.close()
    h0 = int(now) // 3600 * 3600
    end = day_range(days[-1])[1]
    hours = []
    for ts in range(h0, end, 3600):
        lt = time.localtime(ts)
        d = dt.date(lt.tm_year, lt.tm_mon, lt.tm_mday)
        frac = (ts + 3600 - now) / 3600 if ts == h0 else 1.0
        est = ts not in pr
        # brak cen rzeczywistych: prognoza Pradcast, a bez niej - ceny z dzis z tej samej godziny
        buy, sell = pr.get(ts) or fc.get(ts) or pr.get(ts - 86400) or (None, None)
        if buy is None:
            continue
        sell = (sell or 0.0) * STRAT["sell_factor"] * (EST_SELL if est else 1.0)   # szacowana cena - ostrozniej
        hours.append({"ts": ts, "day": DAY_TXT.get((d - today).days, d.isoformat()), "t": time.strftime("%H:00", lt),
                      "fc": est and ts in fc,
                      "load": prof[lt.tm_hour] * frac, "pv": pv.get(d, [0] * 24)[lt.tm_hour] * frac,
                      "buy": buy, "sell": sell, "est": est, "dur": frac})
    if not hours:
        return {"ok": False, "error": "brak cen energii na dziś (PSE / Pstryk)"}
    # Tarcza Pstryk: w miesiacu, w ktorym dziala, zakup energii kosztuje tyle samo o kazdej godzinie
    lim = strat_limits()
    try:
        tz = tarcza_info(lim, now)
    except Exception as e:  # noqa: BLE001
        tz = {"active": False, "reason": f"błąd: {e}"}
    if tz.get("active") and tz.get("eff_buy"):
        cur_month = dt.date.fromtimestamp(now).month
        for h in hours:
            if time.localtime(h["ts"]).tm_mon == cur_month:
                h["buy_hour"] = h["buy"]
                h["buy"] = tz["eff_buy"]
                h["tarcza"] = True
    st0 = min(max(float(soc) / 100 * cap, smin), smax)
    last = sorted(h["buy"] for h in hours[-24:])
    terminal = sum(last[:6]) / max(1, len(last[:6]))
    args = (smin, smax, STRAT["p_max_kw"], STRAT["eta_c"], STRAT["eta_d"])
    strategy.WEAR = STRAT["wear"]
    strategy.SELL_FLOOR, strategy.BUY_CAP = lim["sell_floor"], lim["buy_cap"]
    strategy.P_CHARGE, strategy.P_DISCHARGE = lim["p_charge"], lim["p_discharge"]
    strategy.MIN_DISCHARGE = lim["standby_below_w"] / 1000 if lim.get("standby_below_w") else None
    rez = lim.get("tarcza_rezerwa")
    if tz.get("active") and rez and lim.get("sell_floor"):
        strategy.RESERVE_KWH = rez / 100 * cap
        strategy.RESERVE_PRICE = lim["sell_floor"] * RESERVE_FACTOR
    else:
        strategy.RESERVE_KWH = strategy.RESERVE_PRICE = None
    curve = _eta_curve()
    strategy.ETA_D_CURVE = [(c["od"], c["do"], c["eta"]) for c in curve] or None
    curve_c = _eta_c_curve()
    strategy.ETA_C_CURVE = [(c["od"], c["do"], c["eta"]) for c in curve_c] or None
    strategy.PVCHG_MIN_KW = inverter_ctl.PVCHG_MIN_KW = _pvchg_min_kw(curve_c)
    cur = inverter_ctl.state.get("desired") if inverter_ctl.state.get("tryb") == "wl" else None
    # tryb z PLANU (nie wyslany kod EMS - np. PV do magazynu przy chwilowo malym PV wysyla standby, a plan to nadal pvchg)
    mode0 = (cur.get("plan") if cur and cur.get("plan") in strategy.MODES else
             {1: "standby", 2: "auto", 3: "grid", 4: "pvchg", 5: "export", 6: "export"}.get(cur.get("ems") if cur else None, "auto"))
    if mode0 == "auto" and cur and cur.get("ems") == 2 and cur.get("discharge_kw") == 0:
        mode0 = "nodis"
    # blokada trybu na godzine: tryb wybrany na poczatku godziny obowiazuje do jej konca, przeliczanie od kolejnej
    h0 = hours[0]["ts"]
    hlock = _hour_lock if _hour_lock.get("ts") == h0 else None
    pl = strategy.plan(hours, st0, *args, terminal_price=terminal, mode0=mode0, force0=hlock["mode"] if hlock else None)
    legal = None
    if lim.get("legal_days"):
        try:
            legal = legal_budget(lim["legal_days"])
        except Exception as e:  # noqa: BLE001
            legal = {"error": str(e)}
        if legal and legal.get("budget") is not None:
            capped = {}
            for _it in range(4):          # sprzedaz z magazynu nie wiecej niz wyprodukowane PV (z prognoza do konca planu)
                caps = _legal_caps(hours, pl, legal["budget"])
                if not caps:
                    break
                for t, cap_ in caps.items():
                    hours[t]["exp_cap"] = min(cap_, hours[t].get("exp_cap", cap_))
                    capped[t] = hours[t]["exp_cap"]
                pl = strategy.plan(hours, st0, *args, terminal_price=terminal, mode0=mode0,
                                   force0=hlock["mode"] if hlock else None)
            legal["capped_hours"] = len(capped)
    if not hlock:
        _hour_lock.update(ts=h0, mode=pl[0]["mode"], at=int(now))
    bl = strategy.baseline(hours, st0, *args)
    out, tot = [], {}
    for h, p, b in zip(hours, pl, bl):
        out.append({**{k: (round(v, 3) if isinstance(v, float) else v) for k, v in h.items()},
                    "mode": p["mode"], "label": strategy.label(p, h), "ems_mode": strategy.MODES[p["mode"]][1],
                    "grid": round(p["grid"], 3), "soc": round(100 * p["stored"] / cap, 1), "cost": round(p["cost"], 3),
                    "base_grid": round(b["grid"], 3), "base_soc": round(100 * b["stored"] / cap, 1),
                    "base_cost": round(b["cost"], 3)})
        t = tot.setdefault(h["day"], {"plan": 0.0, "base": 0.0, "grid_charge": 0.0, "bat_export": 0.0})
        t["plan"] += p["cost"]
        t["base"] += b["cost"]
        if p["mode"] == "grid":
            t["grid_charge"] += max(p["d"] - max(h["pv"] - h["load"], 0), 0)
        if p["mode"] == "export":
            t["bat_export"] += max(-p["d"] - max(h["load"] - h["pv"], 0), 0)
    for t in tot.values():
        t["saving"] = round(t["base"] - t["plan"], 2)
        t["plan"], t["base"], t["grid_charge"] = round(t["plan"], 2), round(t["base"], 2), round(t["grid_charge"], 2)
        t["bat_export"] = round(t["bat_export"], 2)
    # koncowy stan magazynu inaczej w planie i bez strategii - roznica wyceniona jak w planie
    end_diff = round((pl[-1]["stored"] - bl[-1]["stored"]) * STRAT["eta_d"] * terminal, 2)
    tot[list(tot)[-1]]["end_diff"] = end_diff
    for t in tot.values():
        t["saving_total"] = round(t["saving"] + t.get("end_diff", 0.0), 2)
    total_saving = round(sum(t["saving_total"] for t in tot.values()), 2)
    nxt = next((o for o in out[1:] if o["label"] != out[0]["label"]), None)
    cur = out[0]
    # godziny dzisiaj przed biezaca - rzeczywiste odczyty (okno zawsze: dzis 00:00 - jutro 24:00)
    past = []
    d0 = day_range(today)[0]
    if h0 > d0:
        con = db_connect()
        try:
            rows = con.execute('SELECT ts / 3600, AVG(soc), AVG(grid), AVG("load"), AVG(pv) FROM samples '
                               'WHERE ts >= ? AND ts < ? GROUP BY ts / 3600', (d0, h0)).fetchall()
        finally:
            con.close()
        by = {int(r[0]) * 3600: r for r in rows}
        for ts in range(d0, h0, 3600):
            r = by.get(ts)
            buy, sell = pr.get(ts) or (None, 0.0)
            sell = (sell or 0.0) * STRAT["sell_factor"]
            if r is None or r[1] is None:
                past.append({"ts": ts, "day": "dziś", "t": time.strftime("%H:00", time.localtime(ts)), "mode": "past",
                             "label": "brak danych", "load": None, "pv": None, "buy": buy, "sell": sell, "est": False,
                             "grid": 0.0, "soc": None, "base_soc": None, "cost": 0.0, "base_cost": 0.0, "base_grid": 0.0})
                continue
            g = float(r[2] or 0)
            c = round((g * buy if g > 0 else g * sell) if buy is not None else 0.0, 3)
            past.append({"ts": ts, "day": "dziś", "t": time.strftime("%H:00", time.localtime(ts)), "mode": "past",
                         "label": "rzeczywiste", "load": round(float(r[3] or 0), 3), "pv": round(float(r[4] or 0), 3),
                         "buy": buy, "sell": sell, "est": False, "grid": round(g, 3), "soc": round(float(r[1]), 1),
                         "base_soc": round(float(r[1]), 1), "cost": c, "base_cost": c, "base_grid": round(g, 3)})
    out = past + out
    data = {"ok": True, "ts": int(now), "hours": out, "totals": tot, "end_value_diff": end_diff,
            "total_saving": total_saving,
            "now": cur, "next": nxt, "now_index": len(past), "price_source": " + ".join(sorted(srcs)) or "–",
            "pradcast": pc_info, "pradcast_status": pradcast.status() if use_pc else None,
            "params": {"cap_kwh": round(cap, 2), "soc_min": round(100 * smin / cap), "soc_max": round(100 * smax / cap),
                       "p_max_kw": STRAT["p_max_kw"], "eta_c": STRAT["eta_c"], "eta_d": STRAT["eta_d"],
                       "sell_factor": STRAT["sell_factor"], "wear": STRAT["wear"], "terminal_price": round(terminal, 3),
                       "profile_hours": nprof, "soc_now": soc, "sell_floor": lim["sell_floor"], "buy_cap": lim["buy_cap"],
                       "p_charge": lim["p_charge"], "p_discharge": lim["p_discharge"],
                       "standby_below_w": lim.get("standby_below_w"), "eta_curve": curve, "eta_c_curve": curve_c,
                       "pvchg_min_kw": strategy.PVCHG_MIN_KW, "tarcza_rezerwa": lim.get("tarcza_rezerwa"),
                       "reserve_price": strategy.RESERVE_PRICE and round(strategy.RESERVE_PRICE, 2),
                       "tarcza": tz, "legal": legal, "legal_days": lim.get("legal_days"),
                       "hour_lock": dict(_hour_lock) if _hour_lock.get("ts") == h0 else None}}
    _strat_cache.update(ts=now, data=data)
    return data


@app.get("/api/inverter/ctl")
def api_inverter_ctl():
    """Sterowanie falownikiem wg strategii (domyslnie wylaczone) - stan, polecenie, dziennik zmian trybu."""
    return inverter_ctl.status()


@app.post("/api/inverter/ctl")
def api_inverter_ctl_set(payload: dict = Body(...)):
    """{"tryb": "wyl" | "probny" | "wl"} albo {"pauza": "godzina" | "1h" | "3h" | "polnoc" | "jutro" | "koniec"}
    albo {"reczne": {"tryb": "standby" | "auto" | "grid" | "discharge" | "koniec", "czas": "15m" | "30m" | "1h" | "2h" |
    "cancel", "minuty": N, "kw": ..., "soc_min": ..., "soc_max": ..., "przylacze_kw": ...}}."""
    try:
        if payload.get("reczne"):
            r = payload["reczne"]
            inverter_ctl.set_manual(str(r.get("tryb") or "").lower(), r.get("czas"), r.get("minuty"), r.get("kw"),
                                    r.get("soc_min"), r.get("soc_max"), r.get("przylacze_kw"))
        elif payload.get("pauza"):
            inverter_ctl.set_pause(str(payload["pauza"]).lower())
        else:
            inverter_ctl.set_tryb(str(payload.get("tryb") or "").lower())
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    try:
        inverter_ctl.tick()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, **inverter_ctl.status()}


def _soc_now():
    with lock:
        return (latest.get("soc"), latest.get("ts")) if latest.get("ok") else (None, None)


def _ctl_limits():
    """(maks. moc ladowania z sieci, maks. moc rozladowania przy sprzedazy) [kW] - pulapy uzytkownika <= p_max."""
    lim, pm = strat_limits(), STRAT["p_max_kw"]
    return (min(pm, lim["p_charge"]) if lim["p_charge"] else pm, min(pm, lim["p_discharge"]) if lim["p_discharge"] else pm)


inverter_ctl.get_load = lambda: latest.get("load") if latest.get("ok") else None
inverter_ctl.get_pv = lambda: latest.get("pv") if latest.get("ok") else None
inverter_ctl.get_flows = lambda: ({k: latest.get(k) for k in ("pv", "bat", "grid", "soc")}
                                  if latest.get("ok") and time.time() - (latest.get("ts") or 0) < 60 else None)


def release_inverter():
    """Zatrzymanie aplikacji - oddanie sterowania falownikowi (tylko gdy aplikacja nim sterowala)."""
    inverter_ctl.release_now(lambda: ModbusTcpClient(HOST, port=PORT, timeout=3), write_holding)


@app.post("/api/strategy/limits")
def api_strategy_limits(payload: dict = Body(...)):
    """Pulapy cenowe strategii: {"sell_floor": 2.5, "buy_cap": 0.8} (puste = brak) - zapis w config.ini [strategia]."""
    vals = {}
    if "tarcza_mode" in payload:
        tm = str(payload.get("tarcza_mode") or "auto").lower()
        if tm not in ("auto", "tak", "nie"):
            return JSONResponse({"ok": False, "error": "tarcza: auto / tak / nie"}, status_code=400)
        vals["tarcza_tryb"] = tm
    for k, key in LIMIT_KEYS.items():
        if k not in payload:                     # brak pola = bez zmian
            continue
        v = payload.get(k)
        if v in (None, ""):
            vals[key] = ""
            continue
        lo, hi, msg = LIMIT_RANGE.get(k, (0, 20, "pułap 0–20 zł/kWh"))
        try:
            v = float(str(v).replace(",", "."))
            assert lo <= v <= hi
        except (TypeError, ValueError, AssertionError):
            return JSONResponse({"ok": False, "error": msg}, status_code=400)
        vals[key] = f"{v:g}"
    config.update({"strategia": vals})
    _strat_cache.update(ts=0, data=None)
    _hour_lock.clear()             # nowe pulapy / limity - biezaca godzina liczona od nowa
    return {"ok": True, "limits": strat_limits()}


@app.get("/api/strategy")
def api_strategy():
    """Strategia osobista - plan magazynu (sterowanie falownikiem osobno: /api/inverter/ctl, domyslnie wylaczone)."""
    try:
        return compute_strategy()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.get("/api/prices")
def api_prices():
    """Ceny zakupu/sprzedazy na dzis i jutro (co 15 min) + najtansze godziny."""
    today = dt.date.today()
    con = db_connect()
    try:
        days = {}
        tariff = None
        for label, d in (("today", today), ("tomorrow", today + dt.timedelta(days=1))):
            pts, tariff = prices.series(con, d.isoformat())
            days[label] = {"date": d.isoformat(), "points": pts,
                           "cheapest": prices.cheapest_hours(pts),
                           "avg_buy": round(sum(p["buy"] for p in pts) / len(pts), 4) if pts else None,
                           "avg_sell": round(sum(p["sell"] for p in pts) / len(pts), 4) if pts else None}
            if pstryk.enabled():
                ps, pe = day_range(d)
                days[label]["pstryk"] = pstryk.prices(pstryk.load(con, ps, pe))
    finally:
        con.close()
    return {"status": prices.status(), "formula": prices.formula_desc(), "pstryk": pstryk.enabled(),
            "tariff": {"source": tariff["source"], "dist": tariff["dist"], "system": tariff["system"]},
            **days}


# --- temperatura ogniw magazynu ------------------------------------------------
BAT_T_OPT = (15, 35)   # zakres korzystny dla ogniw LFP [°C] (praca 20-35, ladowanie 15-40; Sigen: praca -20..55)


@app.get("/api/bat_temps")
def api_bat_temps(date: str | None = None, days: int = 1):
    """Temperatura ogniw magazynu (srednia / maks. / min.) i falownika - doba od polnocy (lub 7/30 dni do tej doby).

    Wynik na stalej siatce czasu (start..koniec doby), zeby os wykresu zawsze obejmowala cala dobe."""
    days = max(1, min(int(days), 90))
    d = dt.date.fromisoformat(date) if date else dt.date.today()
    start = day_range(d - dt.timedelta(days=days - 1))[0]
    end = day_range(d)[1]
    bucket = 300 if days == 1 else (1800 if days <= 7 else 7200)
    con = db_connect()
    try:
        rows = db.load_bat_temps(con, start, end, bucket)
    finally:
        con.close()
    by = {int(b): (a, mx, mn, p) for b, a, mx, mn, p in rows}
    slots = list(range(start // bucket * bucket, end, bucket))
    r1 = lambda v: None if v is None else round(v, 1)  # noqa: E731
    pts = [{"ts": x, **dict(zip(("avg", "max", "min", "pcs"), map(r1, by.get(x, (None,) * 4))))} for x in slots]
    rooms = []
    vals = [p["avg"] for p in pts if p["avg"] is not None]
    lo, hi = BAT_T_OPT
    return {"date": d.isoformat(), "days": days, "bucket": bucket, "points": pts, "rooms": rooms,
            "opt": [lo, hi], "limits": [-20, 55],
            "stats": {"min": min((p["min"] for p in pts if p["min"] is not None), default=None),
                      "max": max((p["max"] for p in pts if p["max"] is not None), default=None),
                      "avg": round(sum(vals) / len(vals), 1) if vals else None,
                      "in_opt": round(100 * sum(lo <= v <= hi for v in vals) / len(vals)) if vals else None}}

@app.get("/api/settings")
def api_settings():
    """Ustawienia falownika (tylko odczyt) + historia zmian."""
    with lock:
        vals, ts = dict(settings_state["values"]), settings_state["ts"]
    con = db_connect()
    try:
        hist = db.settings_history(con)
    finally:
        con.close()
    names = {r[0]: r[1] for r in registers.SETTINGS}
    names.update(registers.ALARM_NAMES)
    for h in hist:
        h["name"] = names.get(h["key"], h["key"])
        h["alarm"] = h["key"] in registers.ALARM_NAMES
    with lock:
        avals, ats = dict(alarm_state["values"]), alarm_state["ts"]
    return {"ts": ts, "groups": registers.grouped_settings(vals), "history": hist,
            "alarms": registers.grouped_alarms(avals), "alarms_ts": ats}


# --- rekuperator Vallox (opcjonalnie, [rekuperacja] vallox_ip) ---------------------------------
@app.get("/api/vallox/live")
def api_vallox_live():
    d = vallox.status()
    d["enabled"] = vallox.ENABLED
    sh = meter.status()
    if meter.ENABLED:
        try:
            today = dt.date.today()
            start, end = day_range(today)
            m_start, _ = day_range(today.replace(day=1))
            con = db_connect()
            try:
                sh["day_kwh"] = round(db.meter_energy(con, start, end) / 1000, 3)
                sh["month_kwh"] = round(db.meter_energy(con, m_start, end) / 1000, 2)
                sh["hour_kwh"] = round(db.meter_energy(con, int(time.time()) - 3600, int(time.time()) + 60) / 1000, 3)
            finally:
                con.close()
        except Exception:  # noqa: BLE001
            pass
    sh["heater_w"] = config.getf("rekuperacja", "nagrzewnica_w", 150)
    d["shelly"] = sh            # nazwa pola jak w wersji z gniazdkiem Shelly (strona)
    d["alert"] = None
    return d


@app.post("/api/vallox/fans")
def api_vallox_fans(payload: dict = Body(...)):
    """Zapis podstawowych ustawien wentylatorow (tryb Dom): {"supp_base": 37, "extr_base": 20, "source": "ręcznie"}."""
    vals = {k: payload.get(k) for k in vallox.FAN_KEYS if payload.get(k) is not None}
    r = vallox.set_fans(vals, payload.get("source", "ręcznie"), db_connect, db)
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@app.post("/api/vallox/auto")
def api_vallox_auto(payload: dict = Body(...)):
    """Automatyczne wyrownywanie przeplywow: {"enabled": true}."""
    return {"ok": True, "auto": vallox.auto_set(bool(payload.get("enabled")))}


@app.post("/api/vallox/bypass")
def api_vallox_bypass(payload: dict = Body(...)):
    """Obejscie wymiennika: {"bypass": true, "partial": true, "cooling": false} (True = "zal." jak w MyVallox)."""
    if vallox._bp_load().get("enabled"):
        return JSONResponse({"ok": False, "error": "steruje automat – wyłącz automat, aby zmieniać ręcznie"}, status_code=400)
    vals = {k: payload.get(k) for k in vallox.BYPASS_KEYS if payload.get(k) is not None}
    r = vallox.set_bypass(vals, "ręcznie", db_connect, db)
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@app.post("/api/vallox/supply")
def api_vallox_supply(payload: dict = Body(...)):
    """Nastawa temperatury nawiewu we wszystkich profilach: {"value": 22}."""
    if vallox._sp_load().get("enabled"):
        return JSONResponse({"ok": False, "error": "steruje automat – wyłącz automat, aby zmieniać ręcznie"}, status_code=400)
    r = vallox.set_supply_target(payload.get("value"), "ręcznie", db_connect, db)
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@app.post("/api/vallox/supply_auto")
def api_vallox_supply_auto(payload: dict = Body(...)):
    """Automat nastawy nawiewu: {"enabled": true, "ext_hot": 22, ...} (dowolne z pol)."""
    try:
        a = vallox.supply_auto_set(**payload)
    except (TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "auto": a}


@app.post("/api/vallox/bypass_auto")
def api_vallox_bypass_auto(payload: dict = Body(...)):
    """Automat obejscia: {"enabled": true, "comfort": 22, "out_min": 5} (dowolne z pol)."""
    try:
        a = vallox.bypass_auto_set(payload.get("enabled"), payload.get("comfort"), payload.get("out_min"))
    except (TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "auto": a}


@app.get("/api/vallox/log")
def api_vallox_log():
    con = db_connect()
    try:
        return db.vallox_log_history(con)
    finally:
        con.close()


def meter_day(d, start, end, series):
    """Pobor pradu rekuperatora (miernik): srednia moc 5 min do serii, zuzycie i koszt dnia, zuzycie miesiaca."""
    con = db_connect()
    try:
        rows = db.load_meter(con, start, end)
        m_start, _ = day_range(d.replace(day=1))
        month_wh = db.meter_energy(con, m_start, end)
        try:
            pts, _t = prices.series(con, d.isoformat())
        except Exception:  # noqa: BLE001
            pts = []
    finally:
        con.close()
    buy = {p["ts"]: p.get("buy") for p in pts}
    pw, kwh, cost, cost_wh = {}, 0.0, 0.0, 0.0
    for ts, power, e_wh in rows:
        if power is not None:
            pw.setdefault(ts // 300 * 300, []).append(power)
        if e_wh:
            kwh += e_wh / 1000
            price = buy.get(ts // 900 * 900)
            if price is not None:
                cost += e_wh / 1000 * price
                cost_wh += e_wh
    by_ts = {p["ts"]: p for p in series}
    for b, v in pw.items():
        p = by_ts.get(b)
        if p is None:
            p = {"ts": b, "t": dt.datetime.fromtimestamp(b).strftime("%H:%M")}
            series.append(p)
        p["power"] = round(sum(v) / len(v), 1)
    series.sort(key=lambda p: p["ts"])
    allp = [x for v in pw.values() for x in v]
    return {"el_kwh": round(kwh, 3) if rows else None, "el_month_kwh": round(month_wh / 1000, 2) if month_wh else None,
            "el_cost": round(cost, 2) if cost_wh else None,
            "el_cost_share": round(100 * cost_wh / 1000 / kwh) if kwh > 0 else None,
            "power_avg": round(sum(allp) / len(allp), 1) if allp else None,
            "power_max": round(max(allp), 1) if allp else None}


_rec_cache = vallox.REC_CACHE   # dzien -> energia odzyskana [kWh] (dni minione); czyszczone po uzupelnieniu luk


def _rec_energy(rows, cols):
    """Energia odzyskana [kWh] z odczytow minutowych (przerwy > 5 min pomijane)."""
    e, prev = 0.0, None
    for r in rows:
        x = dict(zip(cols, r))
        pw = vallox.recovered_w(x["t_out"], x["t_sup_cell"], vallox.airflow(x["rpm_sup"], "sup"), x["cell_state"])
        if prev is not None and pw is not None and 0 < x["ts"] - prev[0] <= 900:   # 900 s: odczyty z dziennika co 10 min
            e += (pw + prev[1]) / 2 * (x["ts"] - prev[0]) / 3600 / 1000
        prev = (x["ts"], pw) if pw is not None else None
    return e


EFF_MIN_DT = 3.0     # °C - roznica wywiew - zewnatrz, od ktorej liczymy efektywnosc (jak sprawnosc chwilowa)


def _eff_stats(rows, cols):
    """Efektywnosc odzysku wazona energia (odpowiednik "rocznej efektywnosci") i temperatura wyrzutu za okres.

    efektywnosc = Σ q·(t_nawiew_za_wym − t_zewn)·dt / Σ q·(t_wywiew − t_zewn)·dt  - gdy wywiew − zewn ≥ 3 °C,
    w trybie odzysku ciepla i odszraniania (bypass / odzysk chlodu - celowo bez odzysku - pomijane). Odszranianie
    i nagrzewnica obnizaja wynik - to roznica miedzy sprawnoscia chwilowa a efektywnoscia sezonowa."""
    num = den = 0.0
    defrost = exh_lt0 = n = 0.0
    exh_min = None
    prev = None
    for r in rows:
        x = dict(zip(cols, r))
        if prev is not None:
            dt_s = x["ts"] - prev["ts"]
            if 0 < dt_s <= 900:
                p = prev
                q = vallox.airflow(p["rpm_sup"], "sup")
                if (None not in (q, p["t_out"], p["t_ext"]) and p["t_ext"] - p["t_out"] >= EFF_MIN_DT
                        and p.get("cell_state") not in (1, 2)):
                    avail = q * (p["t_ext"] - p["t_out"]) * dt_s
                    got = q * ((p["t_sup_cell"] if p["t_sup_cell"] is not None else p["t_out"]) - p["t_out"]) * dt_s
                    num += max(0.0, got)
                    den += avail
                if p.get("cell_state") == 3:
                    defrost += dt_s
                if p.get("t_exh") is not None and p["t_exh"] < 0:
                    exh_lt0 += dt_s
                n += dt_s
        if x.get("t_exh") is not None:
            exh_min = x["t_exh"] if exh_min is None else min(exh_min, x["t_exh"])
        prev = x
    return {"eff_energy": round(100 * num / den, 1) if den > 0 else None, "exh_min": exh_min,
            "exh_lt0_min": round(exh_lt0 / 60), "defrost_min": round(defrost / 60), "hours": round(n / 3600, 1),
            "_num": num, "_den": den}


@app.get("/api/vallox/eff")
def api_vallox_eff(days: int = 30):
    """Efektywnosc odzysku dzien po dniu (wazona energia) + za caly okres, min. temperatura wyrzutu, odszranianie."""
    days = max(1, min(int(days), 366))
    cols = ["ts"] + [c.strip() for c in db.VALLOX_COLS.split(",")]
    today = dt.date.today()
    out, tn, td = [], 0.0, 0.0
    tot = {"defrost_min": 0, "exh_lt0_min": 0, "exh_min": None}
    con = db_connect()
    try:
        for k in range(days):
            day = today - dt.timedelta(days=k)
            s0, e0 = day_range(day)
            rows = db.load_vallox(con, s0, e0)
            if not rows:
                continue
            st = _eff_stats(rows, cols)
            tv = [r[cols.index("t_out")] for r in rows if r[cols.index("t_out")] is not None]
            tn, td = tn + st.pop("_num"), td + st.pop("_den")
            tot["defrost_min"] += st["defrost_min"]
            tot["exh_lt0_min"] += st["exh_lt0_min"]
            if st["exh_min"] is not None:
                tot["exh_min"] = st["exh_min"] if tot["exh_min"] is None else min(tot["exh_min"], st["exh_min"])
            out.append({"date": day.isoformat(), **st, "t_out_avg": round(sum(tv) / len(tv), 1) if tv else None,
                        "t_out_min": round(min(tv), 1) if tv else None})
    finally:
        con.close()
    tot["eff_energy"] = round(100 * tn / td, 1) if td > 0 else None
    return {"days": out, "total": tot}


def vallox_rec_month(d):
    """Energia odzyskana od poczatku miesiaca do dnia d wlacznie [kWh]."""
    cols = ["ts"] + [c.strip() for c in db.VALLOX_COLS.split(",")]
    total, day = 0.0, d.replace(day=1)
    con = db_connect()
    try:
        while day <= d:
            key = day.isoformat()
            if key in _rec_cache and day < dt.date.today():
                total += _rec_cache[key]
            else:
                s0, e0 = day_range(day)
                v = _rec_energy(db.load_vallox(con, s0, e0), cols)
                _rec_cache[key] = v
                total += v
            day += dt.timedelta(days=1)
    finally:
        con.close()
    return round(total, 1)


@app.get("/api/vallox/day")
def api_vallox_day(date: str | None = None):
    """Odczyty rekuperatora z dnia (srednie 5-minutowe) + podsumowanie."""
    d = dt.date.fromisoformat(date) if date else dt.date.today()
    start, end = day_range(d)
    con = db_connect()
    try:
        rows = db.load_vallox(con, start, end)
        try:
            n_log = con.execute("SELECT COUNT(*) FROM vallox_samples WHERE ts >= ? AND ts < ? AND src = 'log'",
                                (start, end)).fetchone()[0]
        except Exception:  # noqa: BLE001 - starsza baza bez kolumny src
            n_log = 0
    finally:
        con.close()
    cols = ["ts"] + [c.strip() for c in db.VALLOX_COLS.split(",")]
    buckets = {}
    for r in rows:
        rec = dict(zip(cols, r))
        buckets.setdefault(rec["ts"] // 300 * 300, []).append(rec)
    avg_keys = ["t_out", "t_sup", "t_sup_cell", "t_ext", "t_exh", "rh", "co2", "co2_s0", "co2_s1", "fan_pct",
                "eff_sup", "eff_ext", "rpm_sup", "rpm_ext"]
    series = []
    for b, recs in sorted(buckets.items()):
        p = {"ts": b, "t": dt.datetime.fromtimestamp(b).strftime("%H:%M")}
        for k in avg_keys:
            v = [x[k] for x in recs if x[k] is not None]
            p[k] = round(sum(v) / len(v), 1) if v else None
        p["flow_sup"], p["flow_ext"] = vallox.airflow(p["rpm_sup"], "sup"), vallox.airflow(p["rpm_ext"], "ext")
        pw = [vallox.recovered_w(x["t_out"], x["t_sup_cell"], vallox.airflow(x["rpm_sup"], "sup"), x["cell_state"]) for x in recs]
        pw = [v for v in pw if v is not None]
        p["p_rec"] = round(sum(pw) / len(pw)) if pw else None
        series.append(p)

    def stat(k, fn):
        v = [x[k] for x in series if x[k] is not None]
        return round(fn(v), 1) if v else None
    summary = {
        "t_out_min": stat("t_out", min), "t_out_max": stat("t_out", max),
        "co2_max": stat("co2", max), "co2_avg": stat("co2", lambda v: sum(v) / len(v)),
        "co2_s0_avg": stat("co2_s0", lambda v: sum(v) / len(v)), "co2_s0_max": stat("co2_s0", max),
        "co2_s1_avg": stat("co2_s1", lambda v: sum(v) / len(v)), "co2_s1_max": stat("co2_s1", max),
        "rh_avg": stat("rh", lambda v: sum(v) / len(v)),
        "eff_sup_avg": stat("eff_sup", lambda v: sum(v) / len(v)),
        "eff_ext_avg": stat("eff_ext", lambda v: sum(v) / len(v)),
    }
    summary["rec_kwh"] = round(_rec_energy(rows, cols), 2)
    est = _eff_stats(rows, cols)
    summary.update({k: est[k] for k in ("eff_energy", "exh_min", "exh_lt0_min", "defrost_min")})
    summary["rec_month_kwh"] = vallox_rec_month(d)
    summary["flow_sup_avg"] = stat("flow_sup", lambda v: sum(v) / len(v))
    summary["flow_ext_avg"] = stat("flow_ext", lambda v: sum(v) / len(v))
    summary["n_log"] = n_log
    summary.update(meter_day(d, start, end, series))
    return {"date": d.isoformat(), "series": series, "summary": summary, "n": len(rows),
            "co2_names": vallox.CO2_NAMES, "co2_threshold": vallox.status().get("co2_threshold")}


@app.get("/api/counters")
def counters():
    """Diagnostyka: ostatnie odczyty licznikow i rejestry nieobslugiwane."""


def start_background():
    db_init()
    if PV_ENABLED:      # bez instalacji PV (tylko rekuperator) - bez odczytu falownika, prognoz i komunikatow o falowniku
        threading.Thread(target=poll_loop, daemon=True, name="modbus").start()
        threading.Thread(target=forecast.loop, args=(db_connect,), daemon=True, name="forecast").start()
        threading.Thread(target=efficiency_loop, daemon=True, name="efficiency").start()
        inverter_ctl.P_MAX = STRAT["p_max_kw"]
        threading.Thread(target=inverter_ctl.decide_loop, args=(compute_strategy, _soc_now, _ctl_limits),
                         daemon=True, name="inverter_ctl").start()
    threading.Thread(target=prices.loop, args=(db_connect,), daemon=True, name="prices").start()
    threading.Thread(target=pstryk.loop, args=(lambda: config.get("pstryk", "api_key", ""), db_connect),
                     daemon=True, name="pstryk").start()
    if vallox.ENABLED:
        threading.Thread(target=vallox.loop, args=(db_connect, db), daemon=True, name="vallox").start()
    if meter.ENABLED:
        threading.Thread(target=meter.loop, args=(db_connect, db), daemon=True, name="meter").start()


def db_init():
    con = db_connect()
    try:
        db.init_schema(con)
    finally:
        con.close()
