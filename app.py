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

from fastapi import Body, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pymodbus.client import ModbusTcpClient

import config
import db
import discover
import forecast
import prices
import pstryk
import strategy
import registers

__version__ = "1.10.3"

# --- konfiguracja (config.ini, sekcja [falownik]) --------------------------
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
    return {"version": __version__, "db": db.describe(), "data_dir": str(config.DATA_DIR), "title": config.APP_TITLE}


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


@app.post("/api/shutdown")
def api_shutdown():
    """Zamkniecie aplikacji (przycisk w zakladce Ustawienia)."""
    threading.Thread(target=lambda: (time.sleep(1), os._exit(0)), daemon=True).start()
    return {"ok": True}


@app.post("/api/restart")
def api_restart():
    """Ponowne uruchomienie aplikacji (po zmianie ustawien)."""
    def _later():
        import subprocess
        time.sleep(1)
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
        return dict(latest)


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
    return [(h, i, o) for h, i, o, _m in eff_minutes_ex(rows, dc_by_min)]


def eff_minutes_ex(rows, dc_by_min):
    """Jak eff_minutes, ale z trybem minuty: 'konwersja' albo 'czuwanie' (brak przetwarzania PV/magazynu)."""
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
        out.append((1 / 60, p_in, p_out, mode))
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
    for h, p_in, p_out, mode in eff_minutes_ex(rows, dc_by_min):
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


def eff_bins_from_rows(rows, dc_by_min=None):
    """{od: [godzin, e_in, e_out]} z probek (ts, pv, bat, plant) i mocy DC stringow {minuta: kW}."""
    acc = {lo: [0.0, 0.0, 0.0] for lo in EFF_BINS}
    for h, p_in, p_out in eff_minutes(rows, dc_by_min or {}):
        lo = max(b for b in EFF_BINS if p_in >= b)
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
        acc = {lo: [0.0, 0.0, 0.0] for lo in EFF_BINS}
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
    out = []
    for i, lo in enumerate(EFF_BINS):
        h, e_in, e_out = acc[lo]
        if h < 1 / 60:   # mniej niz minuta danych - pomijamy
            continue
        loss = e_in - e_out
        out.append({
            "od": lo, "do": EFF_BINS[i + 1] if i + 1 < len(EFF_BINS) else None,
            "godzin": round(h, 2),
            "e_in": round(e_in, 2),
            "straty_kwh": round(loss, 3),
            "strata_sr_w": round(1000 * loss / h),
            "moc_sr_kw": round(e_in / h, 2),
            "sprawnosc": round(100 * e_out / e_in, 1) if e_in > 0.01 else None,
            "mala_probka": h < EFF_MIN_HOURS,
        })
    return {"date": d.isoformat(), "days": days, "n_days": n_days, "bins": out}


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


# --- strategia osobista (SYMULACJA - nic nie jest wysylane do falownika) -----------------
STRAT = {"cap_kwh": config.getf("strategia", "pojemnosc_kwh", 0) or None,
         "p_max_kw": config.getf("strategia", "moc_ladowania_kw", 5.0),
         "eta_c": config.getf("strategia", "sprawnosc_ladowania", 0.95),
         "eta_d": config.getf("strategia", "sprawnosc_rozladowania", 0.95),
         "sell_factor": config.getf("strategia", "wartosc_sprzedazy", 1.0),
         "wear": config.getf("strategia", "koszt_zuzycia_zl_kwh", 0.10)}
EST_SELL = 0.9   # gdy brak cen na jutro (przyjete z dzis), cena sprzedazy liczona ostrozniej
_strat_cache = {"ts": 0, "data": None}


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
        ps = {r["ts"]: r["price_gross"] for r in pstryk.load(con, s, e) if r.get("price_gross") is not None}
        if ps:
            src = "Pstryk"
            out = {h: (b, out.get(h, (None, 0.0))[1]) for h, b in ps.items()}
    return out, src


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
    days = [today, today + dt.timedelta(days=1)]
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
    finally:
        con.close()
    h0 = int(now) // 3600 * 3600
    end = day_range(days[1])[1]
    hours = []
    for ts in range(h0, end, 3600):
        lt = time.localtime(ts)
        d = dt.date(lt.tm_year, lt.tm_mon, lt.tm_mday)
        frac = (ts + 3600 - now) / 3600 if ts == h0 else 1.0
        est = ts not in pr
        buy, sell = pr.get(ts) or pr.get(ts - 86400) or (None, None)   # brak cen na jutro -> ceny z dzis
        if buy is None:
            continue
        sell = (sell or 0.0) * STRAT["sell_factor"] * (EST_SELL if est else 1.0)   # szacowana cena - ostrozniej
        hours.append({"ts": ts, "day": "dziś" if d == today else "jutro", "t": time.strftime("%H:00", lt),
                      "load": prof[lt.tm_hour] * frac, "pv": pv.get(d, [0] * 24)[lt.tm_hour] * frac,
                      "buy": buy, "sell": sell, "est": est})
    if not hours:
        return {"ok": False, "error": "brak cen energii na dziś (PSE / Pstryk)"}
    st0 = min(max(float(soc) / 100 * cap, smin), smax)
    last = sorted(h["buy"] for h in hours[-24:])
    terminal = sum(last[:6]) / max(1, len(last[:6]))
    args = (smin, smax, STRAT["p_max_kw"], STRAT["eta_c"], STRAT["eta_d"])
    strategy.WEAR = STRAT["wear"]
    pl = strategy.plan(hours, st0, *args, terminal_price=terminal)
    bl = strategy.baseline(hours, st0, *args)
    out, tot = [], {}
    for h, p, b in zip(hours, pl, bl):
        out.append({**{k: (round(v, 3) if isinstance(v, float) else v) for k, v in h.items()},
                    "mode": p["mode"], "label": strategy.label(p, h), "ems_mode": strategy.MODES[p["mode"]][1],
                    "grid": round(p["grid"], 3), "soc": round(100 * p["stored"] / cap, 1), "cost": round(p["cost"], 3),
                    "base_grid": round(b["grid"], 3), "base_soc": round(100 * b["stored"] / cap, 1),
                    "base_cost": round(b["cost"], 3)})
        t = tot.setdefault(h["day"], {"plan": 0.0, "base": 0.0, "grid_charge": 0.0})
        t["plan"] += p["cost"]
        t["base"] += b["cost"]
        if p["mode"] == "grid":
            t["grid_charge"] += max(p["d"] - max(h["pv"] - h["load"], 0), 0)
    for t in tot.values():
        t["saving"] = round(t["base"] - t["plan"], 2)
        t["plan"], t["base"], t["grid_charge"] = round(t["plan"], 2), round(t["base"], 2), round(t["grid_charge"], 2)
    # koncowy stan magazynu inaczej w planie i bez strategii - roznica wyceniona jak w planie
    end_diff = round((pl[-1]["stored"] - bl[-1]["stored"]) * STRAT["eta_d"] * terminal, 2)
    tot["jutro" if "jutro" in tot else "dziś"]["end_diff"] = end_diff
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
            "params": {"cap_kwh": round(cap, 2), "soc_min": round(100 * smin / cap), "soc_max": round(100 * smax / cap),
                       "p_max_kw": STRAT["p_max_kw"], "eta_c": STRAT["eta_c"], "eta_d": STRAT["eta_d"],
                       "sell_factor": STRAT["sell_factor"], "wear": STRAT["wear"], "terminal_price": round(terminal, 3),
                       "profile_hours": nprof, "soc_now": soc}}
    _strat_cache.update(ts=now, data=data)
    return data


@app.get("/api/strategy")
def api_strategy():
    """Strategia osobista - symulacja planu magazynu na dzis i jutro (tylko obliczenia, bez sterowania)."""
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


@app.get("/api/counters")
def counters():
    """Diagnostyka: ostatnie odczyty licznikow i rejestry nieobslugiwane."""


def start_background():
    db_init()
    threading.Thread(target=poll_loop, daemon=True, name="modbus").start()
    threading.Thread(target=forecast.loop, args=(db_connect,), daemon=True, name="forecast").start()
    threading.Thread(target=prices.loop, args=(db_connect,), daemon=True, name="prices").start()
    threading.Thread(target=pstryk.loop, args=(lambda: config.get("pstryk", "api_key", ""), db_connect),
                     daemon=True, name="pstryk").start()
    threading.Thread(target=efficiency_loop, daemon=True, name="efficiency").start()


def db_init():
    con = db_connect()
    try:
        db.init_schema(con)
    finally:
        con.close()
