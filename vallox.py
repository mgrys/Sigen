"""
Rekuperator Vallox (seria MV, MyVallox) - odczyt przez lokalny websocket (port 80, bez hasla).
Modul opcjonalny: wlacza sie po wpisaniu adresu IP rekuperatora w Ustawieniach ([rekuperacja] vallox_ip).
Zapis (tylko na zadanie uzytkownika albo wlaczonego automatu, z limitami i historia zmian):
wentylatory trybu Dom, obejscie wymiennika (bypass), nastawa temperatury nawiewu.

Biblioteka: vallox-websocket-api (dziala ze starsza 3.x i nowa 5.x/6.x).
Odczyt co INTERVAL s, zapis do tabeli vallox_samples.
"""
import asyncio
import datetime as dt
import inspect
import json
import threading
import time
from collections import deque

import config

# ---------------- KONFIGURACJA ----------------
HOST = config.get("rekuperacja", "vallox_ip")
ENABLED = bool(HOST)
INTERVAL = 60          # s
MIN_DT_EFF = 3.0       # °C - ponizej tej roznicy wywiew-zewnatrz sprawnosc nie jest liczona
# zapis ustawien wentylatorow (tryb Dom)
FAN_WRITE_ENABLED = True   # False = suwaki tylko do odczytu
FAN_MIN, FAN_MAX = 10, 80  # dozwolony zakres ustawienia [%]
FAN_MAX_STEP = 10          # maks. zmiana jednym zapisem [%]
FAN_WRITE_INTERVAL = 10    # min. odstep miedzy zapisami [s]
# przeplyw powietrza [m3/h] - seria MV nie mierzy przeplywu, SZACUNEK z obrotow wentylatora:
#   przeplyw = maks. przeplyw modelu (karta katalogowa Vallox, 100 Pa) x obroty / obroty przy 100 %.
# Obroty przy 100 %: z Ustawien (obroty_max) albo zapamietane automatycznie, gdy wentylacja pracuje na >= 98 %
# (np. Boost 100 %); do tego czasu 3300 obr/min (110 MV). Kalibracja z protokolu instalatora (anemometr):
# punkty (obroty, m3/h) w AIRFLOW_CAL - wtedy liczone jest z nich (interpolacja liniowa).
# model: (nazwa, maks. nawiew m3/h, maks. wywiew m3/h) - vallox.com, dane techniczne @ 100 Pa
MODELS = {
    "096": ("Vallox 096 MV", 292, 310),
    "110": ("Vallox 110 MV", 385, 400),
    "145": ("Vallox 145 MV", 511, 576),
    "245": ("Vallox 245 MV", 889, 976),
}
MODEL = config.get("rekuperacja", "model", "110") or "110"
if MODEL in MODELS:
    MODEL_NAME, _q_sup, _q_ext = MODELS[MODEL]
else:                                           # "inny": maks. przeplyw wpisany recznie (obie strony)
    _q = config.getf("rekuperacja", "przeplyw_max_m3h", 385) or 385
    _qe = config.getf("rekuperacja", "przeplyw_max_wywiew_m3h", 0) or _q
    MODEL_NAME, _q_sup, _q_ext = "Vallox (model spoza listy)", _q, _qe
FLOW_MAX = {"sup": _q_sup, "ext": _q_ext}
RPM_MAX_DEFAULT = 3300
RPM_MAX_CFG = config.getf("rekuperacja", "obroty_max", 0) or None
CAL_FILE = config.DATA_DIR / "vallox_rpm_max.json"      # obroty przy >= 98 % zapamietane automatycznie
AIRFLOW_CAL = {"sup": [], "ext": []}       # np. {"sup": [(1560, 180)], "ext": [(1300, 175)]}
AIR_RHO_CP = 0.335                         # Wh/(m3·K): gestosc 1,2 kg/m3 x cieplo wl. 1005 J/(kg·K) / 3600
# automatyczne wyrownywanie przeplywow (balans nawiew / wywiew)
AUTO_INTERVAL = 3600       # s miedzy korektami (co godzine)
AUTO_STEP = 1              # % jedna korekta (na przemian wywiew / nawiew)
AUTO_TOL = 0.5             # °C - roznica w tym zakresie = zrownowazone, bez zmian
AUTO_MIN_SPAN = 8.0        # °C - min. roznica wywiew - zewnatrz, zeby pomiar byl wiarygodny
AUTO_SETTLE = 600          # s po zmianie, zanim pomiary sa brane do sredniej
AUTO_BYPASS_SETTLE = 1800  # s po zakonczeniu obejscia / odszraniania - wymiennik musi sie nagrzac / ostygnac
AUTO_WINDOW = 1800         # s - srednia z ostatnich 30 min
AUTO_MIN_SAMPLES = 15      # min. liczba odczytow (co 60 s) w oknie
# ----------------------------------------------

# kolumna w bazie: metryka Vallox
METRICS = {
    "t_out": "A_CYC_TEMP_OUTDOOR_AIR",
    "t_sup": "A_CYC_TEMP_SUPPLY_AIR",
    "t_sup_cell": "A_CYC_TEMP_SUPPLY_CELL_AIR",
    "t_ext": "A_CYC_TEMP_EXTRACT_AIR",
    "t_exh": "A_CYC_TEMP_EXHAUST_AIR",
    "rh": "A_CYC_RH_VALUE",
    "co2": "A_CYC_CO2_VALUE",
    "fan_pct": "A_CYC_FAN_SPEED",
    "rpm_sup": "A_CYC_SUPP_FAN_SPEED",
    "rpm_ext": "A_CYC_EXTR_FAN_SPEED",
    "cell_state": "A_CYC_CELL_STATE",
    "co2_s0": "A_CYC_CO2_SENSOR_0",       # czujnik wielofunkcyjny w rekuperatorze (MyVallox multisensor)
    "co2_s1": "A_CYC_CO2_SENSOR_1",       # dodatkowy czujnik CO2 (opcja)
}
CO2_NAMES = {"co2_s0": "czujnik w rekuperatorze", "co2_s1": "czujnik dodatkowy"}
EXTRA = {   # tylko podglad na zywo
    "home_speed": "A_CYC_HOME_SPEED_SETTING",
    "away_speed": "A_CYC_AWAY_SPEED_SETTING",
    "boost_speed": "A_CYC_BOOST_SPEED_SETTING",
    "bypass_locked": "A_CYC_BYPASS_LOCKED",
    "partial_bypass": "A_CYC_PARTIAL_BYPASS",
    "partial_bypass_disabled": "A_CYC_PARTIAL_BYPASS_DISABLED",
    "coolreco_disabled": "A_CYC_COOLRECOVERY_DISABLED",
    "heating_adjust_mode": "A_CYC_SUPPLY_HEATING_ADJUST_MODE",
    "summer_setpoint": "A_CYC_MLV_SUMMER_SETPOINT",
    "summer_hyst": "A_CYC_MLV_SUMMER_HYSTERESIS",
    "defrosting": "A_CYC_DEFROSTING",
    "heater": "A_CYC_IO_HEATER",
    "filter_days": "A_CYC_REMAINING_TIME_FOR_FILTER",
    "filter_interval": "A_CYC_FILTER_CHANGE_INTERVAL",
    "home_temp": "A_CYC_HOME_AIR_TEMP_TARGET",
    "away_temp": "A_CYC_AWAY_AIR_TEMP_TARGET",
    "boost_temp": "A_CYC_BOOST_AIR_TEMP_TARGET",
    "co2_threshold": "A_CYC_CO2_THRESHOLD",          # prog CO2 [ppm], powyzej ktorego wentylacja rosnie
    "co2_ctrl_home": "A_CYC_HOME_CO2_CTRL_ENABLED",
    "rh_ctrl_home": "A_CYC_HOME_RH_CTRL_ENABLED",
    "rh_basic_level": "A_CYC_RH_BASIC_LEVEL",
    "ms_temp_raw": "A_CYC_MULTISENSOR_TEMP",         # setne czesci kelwina
    "supp_base": "A_CYC_SUPP_FAN_BALANCE_BASE",      # podstawowe ustawienie wentylatora nawiewu (tryb Dom) [%]
    "extr_base": "A_CYC_EXTR_FAN_BALANCE_BASE",      # podstawowe ustawienie wentylatora wywiewu (tryb Dom) [%]
}
FAN_KEYS = {"supp_base": "A_CYC_SUPP_FAN_BALANCE_BASE", "extr_base": "A_CYC_EXTR_FAN_BALANCE_BASE"}
FAN_SOURCES = ("ręcznie", "propozycja", "automat")
COLUMNS = list(METRICS) + ["profile", "eff_sup", "eff_ext"]
CELL_STATE = {0: "odzysk ciepła", 1: "odzysk chłodu", 2: "bypass", 3: "odszranianie"}
PROFILES = {0: "Dom", 1: "Poza domem", 2: "Auto", 3: "Boost", 4: "Kominek", 5: "Extra"}

_last_write = 0.0
_write_lock = threading.Lock()
AUTO_FILE = config.DATA_DIR / "vallox_auto.json"
_bal = deque(maxlen=240)      # (ts, roznica °C, rozpietosc °C) z odczytow co 60 s
latest = {"ok": False, "error": "Uruchamianie..." if ENABLED else "wyłączone – wpisz adres IP rekuperatora w Ustawieniach"}
_lock = threading.Lock()


def _profile(m):
    if (m.get("A_CYC_BOOST_TIMER") or 0) > 0:
        return 3
    if (m.get("A_CYC_FIREPLACE_TIMER") or 0) > 0:
        return 4
    if (m.get("A_CYC_EXTRA_TIMER") or 0) > 0:
        return 5
    return m.get("A_CYC_STATE")


def efficiency(t_out, t_sup_cell, t_ext, t_exh):
    """Sprawnosc odzysku: po stronie nawiewu i wywiewu [%], None przy malej roznicy temperatur."""
    if None in (t_out, t_ext) or abs(t_ext - t_out) < MIN_DT_EFF:
        return None, None
    d = t_ext - t_out
    sup = None if t_sup_cell is None else round(100 * (t_sup_cell - t_out) / d, 1)
    ext = None if t_exh is None else round(100 * (t_ext - t_exh) / d, 1)
    return sup, ext


async def _fetch(host):
    from vallox_websocket_api import Vallox
    client = Vallox(host)
    fn = getattr(client, "fetch_metric_data", None) or client.fetch_metrics
    r = fn()
    data = await r if inspect.isawaitable(r) else r
    return dict(data.items()) if hasattr(data, "items") else dict(data)


async def _write(host, values):
    from vallox_websocket_api import Vallox
    client = Vallox(host)
    r = client.set_values(values)
    if inspect.isawaitable(r):
        await r


# --- dziennik rekuperatora (MyVallox -> Historia): co 10 min, ok. 7 tygodni wstecz ---------------
LOG_FIELDS = {"extract_air_temp": "t_ext", "exhaust_air_temp": "t_exh", "outdoor_air_temp": "t_out",
              "supply_air_temp": "t_sup", "co2": "co2", "humidity": "rh", "8": "fan_pct"}
BACKFILL_EVERY = 6 * 3600
REC_CACHE = {}          # dzien -> energia odzyskana (app.py); czyszczone po uzupelnieniu luk
BACKFILL = {}   # wynik ostatniego uzupelniania z dziennika (osobno, bo `latest` jest czyszczone przy kazdym odczycie)


async def _request_sequential(self, payload, read_packets):
    """Starsze wersje biblioteki odbieraja pakiety rownolegle - nowsze websockets tego nie pozwalaja."""
    import websockets
    async with websockets.connect(f"ws://{self.ip_address}/", open_timeout=15, max_size=None) as ws:
        await ws.send(payload)
        return [await asyncio.wait_for(ws.recv(), timeout=60) for _ in range(read_packets)]


async def _fetch_log(host):
    from vallox_websocket_api import Vallox
    client = Vallox(host)
    if not hasattr(client, "fetch_raw_logs"):
        raise RuntimeError("biblioteka bez obslugi dziennika")
    if hasattr(client, "_websocket_request_multiple"):
        client._websocket_request_multiple = _request_sequential.__get__(client)
    r = client.fetch_raw_logs()
    return await r if inspect.isawaitable(r) else r


def read_log(host=HOST):
    """{ts: {kolumna: wartosc}} z dziennika rekuperatora (czas lokalny jednostki)."""
    out = {}
    for page in asyncio.run(_fetch_log(host)):
        for p in page:
            col = LOG_FIELDS.get(str(p["name"]).split(".")[-1])
            if col:
                ts = int(time.mktime(p["date"].timetuple()))
                out.setdefault(ts, {})[col] = p["value"]
    return out


def _fit(pairs):
    """Prosta regresja y = a*x + b."""
    n = len(pairs)
    if n < 30:
        return None
    sx = sum(x for x, _ in pairs); sy = sum(y for _, y in pairs)
    sxx = sum(x * x for x, _ in pairs); sxy = sum(x * y for x, y in pairs)
    den = n * sxx - sx * sx
    if not den:
        return None
    a = (n * sxy - sx * sy) / den
    return a, (sy - a * sx) / n


def backfill(db_connect, db_mod, host=HOST):
    """Uzupelnia luki w vallox_samples odczytami z dziennika rekuperatora (src='log').
    Obroty wentylatorow odtwarzane z predkosci wentylacji (%) regresja z wlasnych odczytow."""
    log = read_log(host)
    if not log:
        return 0
    con = db_connect()
    try:
        t0, t1 = min(log), max(log) + 600
        have = db_mod.vallox_ts_buckets(con, t0 - 600, t1 + 600)
        fit = db_mod.vallox_rpm_fit(con, int(time.time()) - 30 * 86400)
        fs, fe = _fit([(f, a) for f, a, _ in fit]), _fit([(f, b) for f, _, b in fit])
        rows = []
        for ts, v in sorted(log.items()):
            if ts // 600 in have or (ts - 300) // 600 in have or (ts + 300) // 600 in have:
                continue
            if None in (v.get("t_out"), v.get("t_sup"), v.get("t_ext"), v.get("t_exh")):
                continue
            fan = v.get("fan_pct")
            r_s = round(fs[0] * fan + fs[1]) if fs and fan is not None else (round(fan * 33) if fan is not None else None)
            r_e = round(fe[0] * fan + fe[1]) if fe and fan is not None else (round(fan * 28) if fan is not None else None)
            eff_s, eff_e = efficiency(v["t_out"], v["t_sup"], v["t_ext"], v["t_exh"])
            cell = 2 if (eff_s is not None and eff_s < 30) else 0      # bardzo niska "sprawnosc" = bypass
            rows.append((ts, {"t_out": v["t_out"], "t_sup": v["t_sup"], "t_sup_cell": v["t_sup"], "t_ext": v["t_ext"],
                              "t_exh": v["t_exh"], "rh": v.get("rh"), "co2": v.get("co2"), "fan_pct": fan,
                              "rpm_sup": r_s, "rpm_ext": r_e, "cell_state": cell, "eff_sup": eff_s, "eff_ext": eff_e}))
        if rows:
            db_mod.insert_vallox_log(con, rows)
            REC_CACHE.clear()
    finally:
        con.close()
    with _lock:
        BACKFILL.clear()
        BACKFILL.update(ts=int(time.time()), added=len(rows), log_from=t0, log_to=t1, points=len(log))
    return len(rows)


def read_once(host=HOST):
    m = asyncio.run(_fetch(host))
    vals = {k: m.get(v) for k, v in METRICS.items()}
    vals["profile"] = _profile(m)
    vals["eff_sup"], vals["eff_ext"] = efficiency(vals["t_out"], vals["t_sup_cell"], vals["t_ext"], vals["t_exh"])
    extra = {k: m.get(v) for k, v in EXTRA.items()}
    raw = extra.pop("ms_temp_raw", None)
    extra["ms_temp"] = round(raw / 100 - 273.15, 1) if isinstance(raw, (int, float)) and raw > 20000 else None
    return vals, extra


def loop(db_connect, db_mod):
    global _bal_resume
    if not ENABLED:
        return
    con = None
    last_backfill = 0
    while True:
        t0 = time.time()
        if t0 - last_backfill > BACKFILL_EVERY:
            last_backfill = t0
            try:
                n = backfill(db_connect, db_mod)
                print(f"Vallox: uzupelniono {n} odczytow z dziennika rekuperatora")
            except Exception as e:  # noqa: BLE001 - brak dziennika nie blokuje odczytow
                with _lock:
                    BACKFILL.clear()
                    BACKFILL.update(ts=int(time.time()), error=f"{type(e).__name__}: {e}")
        try:
            vals, extra = read_once()
            learn_rpm_max(vals)
            ts = int(t0)
            with _lock:
                latest.clear()
                latest.update(vals, **extra, ts=ts, ok=True)
            b = balance(vals)
            if bal_paused({**vals, **extra}):          # bypass / odszranianie falszuja pomiar
                _bal.clear()
                _bal_resume = time.time()
            elif b and time.time() - _bal_resume >= AUTO_BYPASS_SETTLE:
                _bal.append((ts, b[0], b[1]))
            try:
                auto_tick(db_connect, db_mod)
            except Exception as e:  # noqa: BLE001
                _auto_save(last_msg=f"błąd automatu: {e}")
            if vals.get("t_ext") is not None and vals.get("t_out") is not None:
                _bp_hist.append((ts, vals["t_ext"], vals["t_out"]))
            try:
                bypass_tick(db_connect, db_mod)
            except Exception as e:  # noqa: BLE001
                _bp_save(last_msg=f"błąd automatu: {e}")
            try:
                supply_tick(db_connect, db_mod)
            except Exception as e:  # noqa: BLE001
                _sp_save(last_msg=f"błąd automatu: {e}")
            try:
                if con is None:
                    con = db_connect()
                db_mod.insert_vallox(con, ts, [vals[c] for c in COLUMNS])
            except Exception as e:  # noqa: BLE001
                with _lock:
                    latest["db_error"] = str(e)
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass
                con = None
        except ImportError:
            with _lock:
                latest.update(ok=False, error="brak biblioteki: python -m pip install vallox-websocket-api")
            time.sleep(600)
            continue
        except Exception as e:  # noqa: BLE001 - brak polaczenia z rekuperatorem
            with _lock:
                latest.update(ok=False, error=f"{type(e).__name__}: {e}")
        time.sleep(max(5, INTERVAL - (time.time() - t0)))


def set_fans(values, source, db_connect=None, db_mod=None):
    """Zapis podstawowych ustawien wentylatorow. values: {"supp_base": int, "extr_base": int} (jedno lub oba)."""
    global _last_write
    if not FAN_WRITE_ENABLED:
        return {"ok": False, "error": "zapis wyłączony (vallox.py: FAN_WRITE_ENABLED)"}
    if source not in FAN_SOURCES:
        return {"ok": False, "error": "nieznane źródło zmiany"}
    with _lock:
        cur = {k: latest.get(k) for k in FAN_KEYS}
        ok_now = latest.get("ok")
    if not ok_now or None in cur.values():
        return {"ok": False, "error": "brak aktualnego odczytu ustawień z rekuperatora"}
    new = {}
    for k, v in values.items():
        if k not in FAN_KEYS or v is None:
            continue
        if not isinstance(v, int) or isinstance(v, bool):
            return {"ok": False, "error": f"{k}: wartość musi być liczbą całkowitą"}
        if not FAN_MIN <= v <= FAN_MAX:
            return {"ok": False, "error": f"{k}: dozwolony zakres {FAN_MIN}–{FAN_MAX} %"}
        if abs(v - cur[k]) > FAN_MAX_STEP:
            return {"ok": False, "error": f"{k}: maks. zmiana o {FAN_MAX_STEP} % na raz (teraz {cur[k]} %)"}
        if v != cur[k]:
            new[k] = v
    if not new:
        return {"ok": True, "changed": {}, "msg": "bez zmian"}
    with _write_lock:
        wait = FAN_WRITE_INTERVAL - (time.time() - _last_write)
        if wait > 0:
            return {"ok": False, "error": f"odczekaj {int(wait) + 1} s przed kolejnym zapisem"}
        _last_write = time.time()
        ts = int(time.time())
        err = None
        try:
            asyncio.run(_write(HOST, {FAN_KEYS[k]: v for k, v in new.items()}))
            time.sleep(2)
            vals, extra = read_once()           # weryfikacja odczytem
            with _lock:
                latest.update(vals, **extra, ts=int(time.time()), ok=True)
            bad = [k for k, v in new.items() if extra.get(k) != v]
            if bad:
                err = "po zapisie odczyt się nie zgadza: " + ", ".join(f"{k}={extra.get(k)}" for k in bad)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        if db_connect and db_mod:
            try:
                con = db_connect()
                try:
                    for k, v in new.items():
                        db_mod.log_vallox_change(con, ts, k, cur[k], v, source, err is None, err)
                finally:
                    con.close()
            except Exception as e:  # noqa: BLE001
                print(f"vallox_log: {e}")
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "changed": {k: [cur[k], v] for k, v in new.items()}}


def airflow(rpm, side):
    """Szacowany przeplyw [m3/h] z obrotow wentylatora (side: "sup" | "ext")."""
    if rpm is None:
        return None
    pts = sorted(AIRFLOW_CAL.get(side) or [])
    if not pts:
        return round(rpm * FLOW_MAX[side] / rpm_max(side))
    pts = [(0, 0)] + pts
    for (r1, q1), (r2, q2) in zip(pts, pts[1:]):
        if rpm <= r2:
            return round(q1 + (q2 - q1) * (rpm - r1) / (r2 - r1)) if r2 > r1 else round(q2)
    r1, q1 = pts[-1]
    return round(q1 * rpm / r1) if r1 else None


_rpm_learned = None


def _rpm_load():
    global _rpm_learned
    if _rpm_learned is None:
        try:
            _rpm_learned = json.loads(CAL_FILE.read_text(encoding="utf-8")) if CAL_FILE.exists() else {}
        except Exception:  # noqa: BLE001
            _rpm_learned = {}
    return _rpm_learned


def rpm_max(side):
    """Obroty wentylatora przy 100 % [obr/min]: Ustawienia > zapamietane przy >= 98 % > 3300."""
    return RPM_MAX_CFG or _rpm_load().get(side) or RPM_MAX_DEFAULT


def learn_rpm_max(vals):
    """Wentylacja na >= 98 % (np. Boost 100 %) -> zapamietaj obroty jako obroty przy 100 %."""
    if (vals.get("fan_pct") or 0) < 98:
        return
    d, changed = _rpm_load(), False
    for side, key in (("sup", "rpm_sup"), ("ext", "rpm_ext")):
        r = vals.get(key)
        if r and r > 500 and abs(r - (d.get(side) or 0)) > 30:
            d[side] = int(r)
            changed = True
    if changed:
        d["ts"] = int(time.time())
        try:
            CAL_FILE.write_text(json.dumps(d), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def recovered_w(t_out, t_sup_cell, q_sup, cell_state):
    """Moc odzyskana [W] = 0,335 x przeplyw nawiewu [m3/h] x przyrost temperatury nawiewu w wymienniku.
    Odzysk chlodu (lato) liczony jako wartosc dodatnia; bypass / odszranianie = 0."""
    if None in (t_out, t_sup_cell, q_sup) or cell_state not in (0, 1):
        return 0.0 if cell_state in (2, 3) else None
    dt_ = (t_sup_cell - t_out) if cell_state == 0 else (t_out - t_sup_cell)
    return round(max(dt_, 0) * q_sup * AIR_RHO_CP, 1)


def balance(v):
    """(roznica °C, rozpietosc °C): roznica = schlodzenie wywiewu - ogrzanie nawiewu; > 0 = przewaga nawiewu."""
    t_out, t_sc, t_ext, t_exh = v.get("t_out"), v.get("t_sup_cell"), v.get("t_ext"), v.get("t_exh")
    if None in (t_out, t_sc, t_ext, t_exh):
        return None
    return (t_ext - t_exh) - (t_sc - t_out), t_ext - t_out


_bal_resume = 0.0   # koniec ostatniego bypassu: pomiary do bilansu dopiero po AUTO_SETTLE od tej chwili


def bal_paused(v=None):
    """Powod wstrzymania bilansowania albo None. Przy obejsciu wymiennika (bypass, czesciowe obejscie, chlodzenie)
    i odszranianiu temperatury za wymiennikiem nie odpowiadaja przeplywom - pomiar i korekty sa niewiarygodne."""
    if v is None:
        with _lock:
            v = dict(latest)
    cs = v.get("cell_state")
    if cs is not None and cs != 0:
        return f"wymiennik: {CELL_STATE.get(cs, cs)}"
    if v.get("bypass_locked") == 0 or v.get("partial_bypass") == 1:
        return "włączone obejście wymiennika (bypass)"
    return None


def _auto_load():
    try:
        d = json.loads(AUTO_FILE.read_text(encoding="utf-8")) if AUTO_FILE.exists() else {}
    except Exception:  # noqa: BLE001
        d = {}
    d.setdefault("enabled", False)
    d.setdefault("next", "extr_base")
    d.setdefault("last_change", 0)
    d.setdefault("last_check", 0)
    return d


def _auto_save(**kw):
    d = _auto_load()
    d.update(kw)
    AUTO_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def auto_set(enabled):
    return _auto_save(enabled=bool(enabled), hist=[], last_msg="włączono" if enabled else "wyłączono")


def auto_window():
    """Srednia roznica i rozpietosc z ostatnich 30 min (tylko po ustabilizowaniu sie po ostatniej zmianie)."""
    a = _auto_load()
    t_from = max(time.time() - AUTO_WINDOW, a.get("last_change", 0) + AUTO_SETTLE)
    xs = [(d, sp) for ts, d, sp in list(_bal) if ts >= t_from]
    if not xs:
        return None, None, 0
    return sum(d for d, _ in xs) / len(xs), sum(sp for _, sp in xs) / len(xs), len(xs)


def _auto_alert(msg, hist, diff, cur, span):
    """Wylaczenie automatu bilansowego przez zabezpieczenie - wpis do logu (komunikat widac tez na stronie)."""
    print(f"Vallox: {msg} (roznice {hist} -> {diff:+.2f} °C, wentylatory {cur}, rozpietosc {span:.1f} °C)")



# --- obejscie wymiennika (bypass): reczne przelaczniki + automat komfortu -------------------------
# Ustawienia jak w MyVallox -> "Ustawienia obejscia wymiennika". Wartosc "zal." w MyVallox:
#   Obejscie wymiennika  -> A_CYC_BYPASS_LOCKED = 0 (blokada wylaczona)
#   Czesciowe obejscie   -> A_CYC_PARTIAL_BYPASS = 1
#   Chlodzenie           -> A_CYC_COOLRECOVERY_DISABLED = 0
BYPASS_KEYS = {   # klucz: (metryka, wartosc dla "zal.", wartosc dla "wyl.")
    "bypass": ("A_CYC_BYPASS_LOCKED", 0, 1),
    "partial": ("A_CYC_PARTIAL_BYPASS", 1, 0),
    "cooling": ("A_CYC_COOLRECOVERY_DISABLED", 0, 1),
}
BYPASS_EXTRA = {"bypass": "bypass_locked", "partial": "partial_bypass", "cooling": "coolreco_disabled"}
BP_FILE = config.DATA_DIR / "vallox_bypass.json"
BP_WINDOW = 600          # s - srednia temperatur z 10 min
BP_HYST = 0.3            # °C - histereza wokol temperatury komfortu (bez migania przy 22,0)
BP_OUT_HYST = 0.5        # °C - histereza progu temperatury zewnetrznej
BP_MIN_INTERVAL = 1800   # s - min. odstep miedzy zmianami automatu
BP_COMFORT_RANGE = (18.0, 28.0)
BP_OUT_RANGE = (-10.0, 20.0)
_bp_hist = deque(maxlen=60)   # (ts, t_ext, t_out)


def bypass_state(d=None):
    """{bypass, partial, cooling}: True = "zal." jak w MyVallox, None = brak odczytu."""
    if d is None:
        with _lock:
            d = dict(latest)
    out = {}
    for k, (_, on_v, _off) in BYPASS_KEYS.items():
        v = d.get(BYPASS_EXTRA[k])
        out[k] = None if v is None else v == on_v
    return out


def _bp_load():
    try:
        d = json.loads(BP_FILE.read_text(encoding="utf-8")) if BP_FILE.exists() else {}
    except Exception:  # noqa: BLE001
        d = {}
    d.setdefault("enabled", False)
    d.setdefault("comfort", 22.0)
    d.setdefault("out_min", 5.0)
    d.setdefault("last_change", 0)
    d.setdefault("last_msg", "")
    return d


def _bp_save(**kw):
    d = _bp_load()
    d.update(kw)
    BP_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def set_bypass(values, source, db_connect=None, db_mod=None):
    """values: {"bypass": bool, "partial": bool, "cooling": bool} (dowolne z nich). True = "zal."."""
    global _last_write
    if not FAN_WRITE_ENABLED:
        return {"ok": False, "error": "zapis wyłączony (vallox.py: FAN_WRITE_ENABLED)"}
    with _lock:
        ok_now = latest.get("ok")
    cur = bypass_state()
    if not ok_now or None in cur.values():
        return {"ok": False, "error": "brak aktualnego odczytu ustawień z rekuperatora"}
    new = {k: bool(v) for k, v in values.items() if k in BYPASS_KEYS and v is not None and bool(v) != cur[k]}
    if not new:
        return {"ok": True, "changed": {}, "msg": "bez zmian"}
    with _write_lock:
        wait = FAN_WRITE_INTERVAL - (time.time() - _last_write)
        if wait > 0:
            return {"ok": False, "error": f"odczekaj {int(wait) + 1} s przed kolejnym zapisem"}
        _last_write = time.time()
        ts = int(time.time())
        err = None
        try:
            asyncio.run(_write(HOST, {BYPASS_KEYS[k][0]: (BYPASS_KEYS[k][1] if v else BYPASS_KEYS[k][2]) for k, v in new.items()}))
            time.sleep(2)
            vals, extra = read_once()
            with _lock:
                latest.update(vals, **extra, ts=int(time.time()), ok=True)
            after = bypass_state()
            bad = [k for k, v in new.items() if after[k] != v]
            if bad:
                err = "po zapisie odczyt się nie zgadza: " + ", ".join(bad)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        if db_connect and db_mod:
            try:
                con = db_connect()
                try:
                    for k, v in new.items():
                        db_mod.log_vallox_change(con, ts, "bp_" + k, int(cur[k]), int(v), source, err is None, err)
                finally:
                    con.close()
            except Exception as e:  # noqa: BLE001
                print(f"vallox_log: {e}")
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "changed": {k: [cur[k], v] for k, v in new.items()}}


def bypass_auto_set(enabled=None, comfort=None, out_min=None):
    kw = {}
    if comfort is not None:
        c = float(comfort)
        if not BP_COMFORT_RANGE[0] <= c <= BP_COMFORT_RANGE[1]:
            raise ValueError(f"temperatura komfortu: zakres {BP_COMFORT_RANGE[0]:.0f}–{BP_COMFORT_RANGE[1]:.0f} °C")
        kw["comfort"] = round(c, 1)
    if out_min is not None:
        o = float(out_min)
        if not BP_OUT_RANGE[0] <= o <= BP_OUT_RANGE[1]:
            raise ValueError(f"próg temperatury zewnętrznej: zakres {BP_OUT_RANGE[0]:.0f}–{BP_OUT_RANGE[1]:.0f} °C")
        kw["out_min"] = round(o, 1)
    if enabled is not None:
        kw["enabled"] = bool(enabled)
        kw["last_msg"] = "włączono" if enabled else "wyłączono"
        kw["last_change"] = 0          # po wlaczeniu decyzja od razu (gdy sa dane z 10 min)
    return _bp_save(**kw)


def _bp_avg():
    now = time.time()
    xs = [(e, o) for ts, e, o in list(_bp_hist) if ts >= now - BP_WINDOW]
    if len(xs) < 5:
        return None, None, len(xs)
    return sum(e for e, _ in xs) / len(xs), sum(o for _, o in xs) / len(xs), len(xs)


def bypass_tick(db_connect, db_mod):
    """Temp. wywiewu (z domu) > komfort i na zewnatrz >= prog -> obejscie, czesciowe obejscie i chlodzenie "zal.";
    wywiew < komfort albo na zewnatrz < prog -> wszystko "wyl." (odzysk ciepla). Srednie z 10 min, histereza."""
    a = _bp_load()
    if not a["enabled"]:
        return
    t_ext, t_out, n = _bp_avg()
    if t_ext is None:
        return
    cur = bypass_state()
    if None in cur.values():
        return
    is_on = all(cur.values())
    c, o = a["comfort"], a["out_min"]
    if t_out < o or t_ext <= c - BP_HYST:
        want = False
        why = (f"na zewnątrz {t_out:.1f} °C < {o:.1f} °C" if t_out < o else f"wywiew {t_ext:.1f} °C < {c:.1f} °C") + " – odzysk ciepła"
    elif t_ext >= c + BP_HYST and t_out >= o + BP_OUT_HYST:
        want = True
        why = f"wywiew {t_ext:.1f} °C > {c:.1f} °C, na zewnątrz {t_out:.1f} °C – obejście i chłodzenie"
    else:
        return                         # w histerezie - bez zmian
    if want == is_on and (want or not any(cur.values())):
        return
    if time.time() - a["last_change"] < BP_MIN_INTERVAL:
        return
    r = set_bypass({k: want for k in BYPASS_KEYS}, "automat", db_connect, db_mod)
    _bp_save(last_change=time.time(), last_msg=why if r.get("ok") else f"zapis nieudany – {r.get('error')}")


def bypass_status():
    a = _bp_load()
    t_ext, t_out, n = _bp_avg()
    return {**a, "avg_t_ext": None if t_ext is None else round(t_ext, 1), "avg_t_out": None if t_out is None else round(t_out, 1),
            "samples": n, "hyst": BP_HYST, "min_interval_min": BP_MIN_INTERVAL // 60,
            "comfort_range": BP_COMFORT_RANGE, "out_range": BP_OUT_RANGE}



# --- temperatura nawiewu (nastawa we wszystkich profilach) + automat sezonowy / przeciwzamrozeniowy -------------
SUPPLY_KEYS = {"home_temp": "A_CYC_HOME_AIR_TEMP_TARGET", "away_temp": "A_CYC_AWAY_AIR_TEMP_TARGET",
               "boost_temp": "A_CYC_BOOST_AIR_TEMP_TARGET"}
SUPPLY_RANGE = (15, 25)          # °C - dopuszczalne nastawy
SP_FILE = config.DATA_DIR / "vallox_supply.json"
SP_DEFAULTS = {"enabled": False, "summer_months": [5, 6, 7, 8, 9],
               "ext_hot": 22.0, "ext_cold": 21.0,          # wywiew >= 22 (lato) -> 17 ; wywiew <= 21 -> 22
               "summer_target": 17, "normal_target": 22,
               "frost_out": 1.0, "frost_exit": 3.0,        # mroz: zewn. <= 1 °C, koniec: >= 3 °C
               "spike_w": 150, "spike_min": 5,             # nagrzewnica: srednio >= 150 W przez >= 5 min (krotkie piki pomijane)
               "frost_min": 15, "frost_step_min": 60,
               "last_change": 0, "last_check": 0, "frost": False, "last_msg": ""}
SP_MIN_INTERVAL = 1800           # s - min. odstep zmian poza trybem mrozowym
SP_WINDOW = 1800                 # s - srednie temperatur z 30 min


def _sp_load():
    try:
        d = json.loads(SP_FILE.read_text(encoding="utf-8")) if SP_FILE.exists() else {}
    except Exception:  # noqa: BLE001
        d = {}
    return {**SP_DEFAULTS, **d}


def _sp_save(**kw):
    d = _sp_load()
    d.update(kw)
    SP_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def supply_target():
    with _lock:
        vals = [latest.get(k) for k in SUPPLY_KEYS]
    return vals[0] if vals and vals[0] is not None else None


def set_supply_target(t, source, db_connect=None, db_mod=None):
    """Nastawa temperatury nawiewu - ta sama we wszystkich profilach (Dom, Poza domem, Boost)."""
    global _last_write
    if not FAN_WRITE_ENABLED:
        return {"ok": False, "error": "zapis wyłączony (vallox.py: FAN_WRITE_ENABLED)"}
    try:
        t = int(round(float(t)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "temperatura musi być liczbą"}
    if not SUPPLY_RANGE[0] <= t <= SUPPLY_RANGE[1]:
        return {"ok": False, "error": f"zakres {SUPPLY_RANGE[0]}–{SUPPLY_RANGE[1]} °C"}
    with _lock:
        cur = {k: latest.get(k) for k in SUPPLY_KEYS}
        ok_now = latest.get("ok")
    if not ok_now or None in cur.values():
        return {"ok": False, "error": "brak aktualnego odczytu z rekuperatora"}
    new = {k: t for k, v in cur.items() if round(float(v)) != t}
    if not new:
        return {"ok": True, "changed": {}, "msg": "bez zmian"}
    with _write_lock:
        wait = FAN_WRITE_INTERVAL - (time.time() - _last_write)
        if wait > 0:
            return {"ok": False, "error": f"odczekaj {int(wait) + 1} s przed kolejnym zapisem"}
        _last_write = time.time()
        ts, err = int(time.time()), None
        try:
            asyncio.run(_write(HOST, {SUPPLY_KEYS[k]: v for k, v in new.items()}))
            time.sleep(2)
            vals, extra = read_once()
            with _lock:
                latest.update(vals, **extra, ts=int(time.time()), ok=True)
            bad = [k for k in new if extra.get(k) is None or round(float(extra.get(k))) != t]
            if bad:
                err = "po zapisie odczyt się nie zgadza: " + ", ".join(bad)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        if db_connect and db_mod:
            try:
                con = db_connect()
                try:
                    db_mod.log_vallox_change(con, ts, "supply_temp", round(float(cur["home_temp"])), t, source, err is None, err)
                finally:
                    con.close()
            except Exception as e:  # noqa: BLE001
                print(f"vallox_log: {e}")
    return {"ok": False, "error": err} if err else {"ok": True, "changed": {k: [cur[k], t] for k in new}}


def supply_auto_set(**kw):
    allowed = {"enabled", "ext_hot", "ext_cold", "summer_target", "normal_target", "frost_out", "frost_exit",
               "spike_w", "spike_min", "frost_min", "summer_months"}
    upd = {}
    for k, v in kw.items():
        if k not in allowed or v is None:
            continue
        if k == "enabled":
            upd[k] = bool(v)
            upd["last_change"] = 0
            upd["last_msg"] = "włączono" if v else "wyłączono"
        elif k == "summer_months":
            m = sorted({int(x) for x in v if 1 <= int(x) <= 12})
            upd[k] = m
        else:
            upd[k] = float(v)
    c = {**_sp_load(), **upd}
    for k in ("summer_target", "normal_target", "frost_min"):
        if not SUPPLY_RANGE[0] <= c[k] <= SUPPLY_RANGE[1]:
            raise ValueError(f"nastawa poza zakresem {SUPPLY_RANGE[0]}–{SUPPLY_RANGE[1]} °C")
    if c["ext_cold"] >= c["ext_hot"]:
        raise ValueError("próg „wywiew zimny” musi być niższy niż „wywiew ciepły”")
    if c["frost_exit"] <= c["frost_out"]:
        raise ValueError("koniec mrozu musi być wyżej niż próg mrozu")
    return _sp_save(**upd)


def _sp_avgs():
    now = time.time()
    xs = [(e, o) for ts, e, o in list(_bp_hist) if ts >= now - SP_WINDOW]
    if len(xs) < 10:
        return None, None, len(xs)
    return sum(e for e, _ in xs) / len(xs), sum(o for _, o in xs) / len(xs), len(xs)


def _heater_run(db_connect, db_mod, threshold, seconds=3600):
    """Najdluzsza ciagla praca z poborem >= threshold [min] i najwieksza srednia moc [W] w ostatniej godzinie.

    Srednia moc w kazdym odstepie (ok. 60 s) z licznika energii miernika (meter.py) - krotkie piki kontrolne Vallox
    (kilka sekund) prawie nie podnosza sredniej minutowej; moc chwilowa tylko gdy brak licznika."""
    con = db_connect()
    try:
        rows = db_mod.load_meter(con, int(time.time()) - seconds, int(time.time()) + 60)
    finally:
        con.close()
    if not rows:
        return None, None
    best = run = 0.0
    pmax, prev = 0.0, None
    for ts, p, e in rows:
        if prev is not None and 0 < ts - prev <= 300:
            dt_s = ts - prev
            avg = float(e) * 3600 / dt_s if e is not None else (float(p) if p is not None else 0.0)
            pmax = max(pmax, avg)
            run = run + dt_s / 60 if avg >= threshold else 0.0
            best = max(best, run)
        else:
            run = 0.0
        prev = ts
    return best, pmax


def supply_tick(db_connect, db_mod):
    """Lato (maj-wrzesien) i wywiew >= 22 -> 17 °C (chlodzenie / bypass w nocy); wywiew <= 21 -> 22 °C (odzysk).
    Mroz (zewn. <= 1 °C): przy skokach poboru mocy (nagrzewnica / odszranianie) nastawa -1 °C co godzine, do 15 °C."""
    a = _sp_load()
    now = time.time()
    if not a["enabled"] or now - a["last_check"] < 300:
        return
    t_ext, t_out, n = _sp_avgs()
    cur = supply_target()
    if t_ext is None or cur is None:
        _sp_save(last_check=now, last_msg=f"za mało pomiarów ({n}) – czekam")
        return
    cur = round(float(cur))
    frost = a["frost"]
    if not frost and t_out <= a["frost_out"]:
        frost = True
    elif frost and t_out >= a["frost_exit"]:
        frost = False
    want, why = None, ""
    if frost:
        import meter
        if meter.ENABLED:
            run, pmax = _heater_run(db_connect, db_mod, a["spike_w"])
        else:                                   # bez miernika: sygnal nagrzewnicy z rekuperatora (A_CYC_IO_HEATER)
            with _lock:
                on = bool(latest.get("heater"))
            run, pmax = (a["spike_min"], 0.0) if on else (0.0, 0.0)
        if run is not None and run >= a["spike_min"]:
            txt = f"nagrzewnica ~{run:.0f} min (do {pmax:.0f} W)" if meter.ENABLED else "nagrzewnica włączona (sygnał z rekuperatora)"
            if cur > a["frost_min"] and now - a["last_change"] >= a["frost_step_min"] * 60:
                want, why = cur - 1, f"mróz ({t_out:.1f} °C), {txt} – nastawa −1 °C"
            else:
                why = f"mróz, {txt} – " + ("minimum osiągnięte" if cur <= a["frost_min"] else "czekam godzinę po zmianie")
        else:
            why = (f"mróz ({t_out:.1f} °C), bez pracy nagrzewnicy (najdłużej {run or 0:.0f} min ≥ {a['spike_w']:g} W, "
                   f"maks. {pmax or 0:.0f} W) – bez zmian")
    else:
        month = dt.date.today().month
        if month in a["summer_months"] and t_ext >= a["ext_hot"]:
            want, why = int(a["summer_target"]), f"lato, wywiew {t_ext:.1f} °C ≥ {a['ext_hot']:g} – chłodzenie / bypass"
        elif t_ext <= a["ext_cold"]:
            want, why = int(a["normal_target"]), f"wywiew {t_ext:.1f} °C ≤ {a['ext_cold']:g} – odzysk ciepła"
        elif a["frost"]:                       # koniec mrozu, wywiew w histerezie - wroc do nastawy normalnej
            want, why = int(a["normal_target"]), "koniec mrozu – powrót do nastawy"
        else:
            why = f"wywiew {t_ext:.1f} °C w histerezie – bez zmian"
        if want is not None and want != cur and now - a["last_change"] < SP_MIN_INTERVAL:
            _sp_save(last_check=now, frost=frost, last_msg=why + " (czekam – min. 30 min między zmianami)")
            return
    if want is None or want == cur:
        _sp_save(last_check=now, frost=frost, last_msg=why + (f" (nastawa {cur} °C)" if want == cur else ""))
        return
    r = set_supply_target(want, "automat", db_connect, db_mod)
    _sp_save(last_check=now, frost=frost, last_change=now if r.get("ok") else a["last_change"],
             last_msg=(f"{cur} → {want} °C: " + why) if r.get("ok") else f"zapis nieudany – {r.get('error')}")


def supply_status():
    a = _sp_load()
    t_ext, t_out, n = _sp_avgs()
    return {**a, "avg_t_ext": None if t_ext is None else round(t_ext, 1), "avg_t_out": None if t_out is None else round(t_out, 1),
            "samples": n, "range": SUPPLY_RANGE}


def auto_tick(db_connect, db_mod):
    """Co godzine jedna korekta o 1 %, na przemian wywiew i nawiew, az roznica bedzie w zakresie ±0,5 °C."""
    a = _auto_load()
    now = time.time()
    if not a["enabled"] or now - a["last_change"] < AUTO_INTERVAL or now - a["last_check"] < 300:
        return
    with _lock:
        cur = {k: latest.get(k) for k in FAN_KEYS}
        prof = latest.get("profile")
    diff, span, n = auto_window()
    why = bal_paused()
    if why:
        _auto_save(last_check=now, hist=[], last_msg=f"wstrzymany – {why}; wznowi się po powrocie do odzysku ciepła")
        return
    if prof != 0:
        _auto_save(last_check=now, last_msg="profil inny niż Dom – czekam")
        return
    if n < AUTO_MIN_SAMPLES or diff is None:
        _auto_save(last_check=now, last_msg=f"za mało pomiarów ({n}/{AUTO_MIN_SAMPLES}) – czekam")
        return
    if span < AUTO_MIN_SPAN:
        _auto_save(last_check=now, last_msg=f"różnica wywiew − zewnątrz {span:.1f} °C < {AUTO_MIN_SPAN:.0f} °C – pomiar niewiarygodny, czekam")
        return
    if abs(diff) <= AUTO_TOL:
        _auto_save(last_check=now, hist=[], last_msg=f"zrównoważone ({diff:+.2f} °C) – bez zmian")
        return
    # zabezpieczenie: 3 kolejne korekty w tym samym kierunku, a roznica zamiast malec - rosnie -> wylacz automat
    hist = (a.get("hist") or [])[-3:]
    if len(hist) == 3 and all((h > 0) == (diff > 0) for h in hist) and abs(diff) > abs(hist[0]) + 0.3 \
            and abs(hist[1]) >= abs(hist[0]) and abs(hist[2]) >= abs(hist[1]):
        msg = f"WYŁĄCZONO: po 3 korektach różnica rośnie ({hist[0]:+.2f} → {diff:+.2f} °C) – sprawdź ustawienia ręcznie"
        _auto_save(enabled=False, last_check=now, hist=[], last_msg=msg)
        threading.Thread(target=_auto_alert, args=(msg, hist, diff, cur, span), daemon=True).start()
        return
    order = [a["next"], "supp_base" if a["next"] == "extr_base" else "extr_base"]
    for k in order:                              # przewaga nawiewu: wywiew +1 albo nawiew -1 (i odwrotnie)
        delta = AUTO_STEP if (diff > 0) == (k == "extr_base") else -AUTO_STEP
        v = (cur[k] or 0) + delta
        if cur[k] is None or not FAN_MIN <= v <= FAN_MAX:
            continue
        r = set_fans({k: v}, "automat", db_connect, db_mod)
        name = "wywiew" if k == "extr_base" else "nawiew"
        if r.get("ok"):
            _auto_save(last_change=now, last_check=now, next="supp_base" if k == "extr_base" else "extr_base",
                       hist=(a.get("hist") or [])[-5:] + [round(diff, 2)],
                       last_msg=f"{name} {cur[k]} → {v} % (różnica {diff:+.2f} °C z {n} min)")
        else:
            _auto_save(last_check=now, last_msg=f"{name}: zapis nieudany – {r.get('error')}")
        return
    _auto_save(last_check=now, last_msg="osiągnięto limit zakresu wentylatorów – bez zmian")


def status():
    with _lock:
        d = dict(latest)
        d["backfill"] = dict(BACKFILL) or None
    d["cell_state_txt"] = CELL_STATE.get(d.get("cell_state"))
    d["profile_txt"] = PROFILES.get(d.get("profile"))
    d["host"] = HOST
    d["co2_names"] = CO2_NAMES
    d["fan_limits"] = {"enabled": FAN_WRITE_ENABLED, "min": FAN_MIN, "max": FAN_MAX, "step": FAN_MAX_STEP}
    d["flow_sup"], d["flow_ext"] = airflow(d.get("rpm_sup"), "sup"), airflow(d.get("rpm_ext"), "ext")
    d["p_rec_w"] = recovered_w(d.get("t_out"), d.get("t_sup_cell"), d["flow_sup"], d.get("cell_state"))
    d["flow_calibrated"] = bool(AIRFLOW_CAL.get("sup") or AIRFLOW_CAL.get("ext"))
    cal = _rpm_load()
    d["model"] = {"key": MODEL, "name": MODEL_NAME, "flow_max": FLOW_MAX,
                  "rpm_max": {k: rpm_max(k) for k in ("sup", "ext")},
                  "rpm_source": "ustawienia" if RPM_MAX_CFG else ("zapamiętane przy 100 %" if cal.get("sup") else "domyślne 3300")}
    a = _auto_load()
    diff, span, n = auto_window()
    a.update(avg_diff=None if diff is None else round(diff, 2), avg_span=None if span is None else round(span, 1), samples=n,
             interval_min=AUTO_INTERVAL // 60, tol=AUTO_TOL, step=AUTO_STEP,
             next_check=max(a["last_change"] + AUTO_INTERVAL, a["last_check"] + 300) if a["enabled"] else None)
    a["paused"] = bal_paused(d)
    d["auto"] = a
    d["bypass_ctl"] = {**bypass_state(d), "auto": bypass_status()}
    d["supply_ctl"] = {"targets": {k: d.get(k) for k in SUPPLY_KEYS}, "auto": supply_status()}
    return d
