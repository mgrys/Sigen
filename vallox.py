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
import math
import threading
import time
from collections import deque

import config

# ---------------- KONFIGURACJA ----------------
HOST = config.get("rekuperacja", "vallox_ip")
ENABLED = bool(HOST) and config.module_vallox()   # Ustawienia -> "Co masz": rekuperator
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
# model: "auto" (domyslnie) = odczyt A_CYC_MACHINE_MODEL z rekuperatora; "096" / "110" / "145" / "245" recznie;
# "inny" = maks. przeplyw wpisany recznie. Modele z innym oznaczeniem tej samej jednostki (DV.. Adroit) -> ten sam przeplyw.
MODEL_ALIASES = {"096 MV": "096", "96 MV": "096", "DV96": "096", "110 MV": "110", "DV110": "110",
                 "145 MV": "145", "DV145": "145", "245 MV": "245", "DV245": "245"}
MODEL = (config.get("rekuperacja", "model", "auto") or "auto").lower()
_q = config.getf("rekuperacja", "przeplyw_max_m3h", 0) or None
_qe = config.getf("rekuperacja", "przeplyw_max_wywiew_m3h", 0) or _q
MODEL_INFO = {"key": None, "name": None, "detected": None, "source": None}
FLOW_MAX = {"sup": 385, "ext": 400}


def _apply_model(key, detected=None):
    """Ustawia maks. przeplyw wg modelu: z listy, recznie wpisany albo (gdy nieznany) wartosci 110 MV."""
    if key in MODELS:
        name, qs, qe = MODELS[key]
        src = "odczytany z rekuperatora" if detected else "wybrany w Ustawieniach"
    elif _q:
        name, qs, qe = detected or "Vallox (model spoza listy)", _q, _qe
        src = "przepływ wpisany w Ustawieniach"
    else:
        name, qs, qe = detected or "Vallox", 385, 400
        src = ("model spoza listy – przyjęto przepływ jak 110 MV; wpisz maks. przepływ w Ustawieniach"
               if detected else "model nieznany – przyjęto 110 MV")
    FLOW_MAX.update(sup=qs, ext=qe)
    MODEL_INFO.update(key=key if key in MODELS else "inny", name=name, detected=detected, source=src)


def detect_model(name):
    """Nazwa z rekuperatora (np. "Vallox 145 MV") -> klucz MODELS albo None."""
    n = (name or "").replace("Vallox ", "").strip()
    for alias, key in MODEL_ALIASES.items():
        if n == alias or n.startswith(alias + " ") or n.startswith(alias):
            return key
    return None


if MODEL != "auto":
    _apply_model(MODEL if MODEL in MODELS else None)
else:
    _apply_model(None)
    MODEL_INFO["source"] = "czekam na odczyt modelu z rekuperatora (do tego czasu jak 110 MV)"
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
    # 1.23: odszranianie wymiennika (sposob: obejscie z dogrzewaniem nawiewu / zatrzymanie wentylatora nawiewu)
    "defrost_mode": "A_CYC_DEFROST_MODE",
    "defrost_sup_temp": "A_CYC_SUPPLY_AIR_DEFROST_TEMP",  # nawiew podczas odszraniania (obejscie) [°C]
    "defrost_week": "A_CYC_DEFROST_COUNT_IN_WEEK",
    "defrost_min_time": "A_CYC_MIN_DEFROST_TIME",
    "defrost_temp_param": "A_CYC_DEFROST_TEMP_PARAM",
    "defrost_rh_param": "A_CYC_DEFROST_RH_PARAM",
    "defrost_comp_limit": "A_CYC_DEFROST_COMP_LIMIT",
    "defrost_rpm_limit": "A_CYC_DEFROST_RPM_LIMIT",
    "defrost_exh_offset": "A_CYC_DEFROST_EXH_OFFSET",
    "defrost_rh_offset": "A_CYC_DEFROST_RH_OFFSET",
    "defrost_supermelt": "A_CYC_DEFROST_SUPERMELT_THRESHOLD",
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


def read_log(host=None):
    """{ts: {kolumna: wartosc}} z dziennika rekuperatora (czas lokalny jednostki)."""
    host = host or HOST
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


def backfill(db_connect, db_mod, host=None):
    """Uzupelnia luki w vallox_samples odczytami z dziennika rekuperatora (src='log').
    Obroty wentylatorow odtwarzane z predkosci wentylacji (%) regresja z wlasnych odczytow."""
    host = host or HOST
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


def read_once(host=None):
    host = host or HOST
    m = asyncio.run(_fetch(host))
    vals = {k: m.get(v) for k, v in METRICS.items()}
    vals["profile"] = _profile(m)
    vals["eff_sup"], vals["eff_ext"] = efficiency(vals["t_out"], vals["t_sup_cell"], vals["t_ext"], vals["t_exh"])
    extra = {k: m.get(v) for k, v in EXTRA.items()}
    raw = extra.pop("ms_temp_raw", None)
    extra["ms_temp"] = round(raw / 100 - 273.15, 1) if isinstance(raw, (int, float)) and raw > 20000 else None
    _note_serial(m)
    if MODEL == "auto" and MODEL_INFO.get("detected") is None:
        idx = m.get("A_CYC_MACHINE_MODEL")
        try:
            from vallox_websocket_api.vallox import DEVICE_MODEL
            name = DEVICE_MODEL[idx] if isinstance(idx, int) and 0 <= idx < len(DEVICE_MODEL) else None
        except Exception:  # noqa: BLE001 - starsza biblioteka bez listy modeli
            name = None
        if name:
            _apply_model(detect_model(name), detected=name)
    return vals, extra


# --- numer seryjny i ponowne odnalezienie rekuperatora po zmianie adresu IP (DHCP) ------------------
SERIAL = config.get("rekuperacja", "vallox_serial")
REFIND_AFTER = 600        # s bez polaczenia, po ktorych szukamy rekuperatora w sieci (po numerze seryjnym)
REFIND_EVERY = 3600       # s miedzy kolejnymi probami
REFIND = {}               # wynik ostatniego szukania (podglad w status())


def serial_of(m):
    """Numer seryjny z metryk (A_CYC_SERIAL_NUMBER_MSW/LSW) albo None."""
    hi, lo = m.get("A_CYC_SERIAL_NUMBER_MSW"), m.get("A_CYC_SERIAL_NUMBER_LSW")
    if isinstance(hi, int) and isinstance(lo, int) and (hi or lo):
        return str((hi << 16) | (lo & 0xFFFF))
    return None


def _note_serial(m):
    """Zapamietanie numeru seryjnego w config.ini (raz) - do odnalezienia rekuperatora po zmianie IP."""
    global SERIAL
    sn = serial_of(m)
    if sn and sn != SERIAL:
        SERIAL = sn
        try:
            config.update({"rekuperacja": {"vallox_serial": sn}})
        except Exception as e:  # noqa: BLE001
            print(f"Vallox: zapis numeru seryjnego: {e}")


def _refind():
    """Szukanie rekuperatora o zapamietanym numerze seryjnym; przy innym IP - zmiana adresu w config.ini."""
    global HOST
    import discover
    r = discover.scan_vallox()
    hit = next((x for x in r["found"] if SERIAL and x.get("serial") == SERIAL), None)
    REFIND.update(ts=int(time.time()), found=len(r["found"]), new_ip=None)
    if hit and hit["ip"] != HOST:
        print(f"Vallox: rekuperator {SERIAL} zmienil adres {HOST} -> {hit['ip']}")
        REFIND["new_ip"] = hit["ip"]
        HOST = hit["ip"]
        config.update({"rekuperacja": {"vallox_ip": hit["ip"]}})
        return True
    return False


def loop(db_connect, db_mod):
    global _bal_resume
    if not ENABLED:
        return
    con = None
    last_backfill = 0
    fail_since, last_refind = None, 0
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
            fail_since = None
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
                speed_tick(db_connect, db_mod, vals, extra)
            except Exception as e:  # noqa: BLE001
                _ws_save(last_msg=f"błąd automatu: {e}")
            try:
                balref_tick(vals, extra)
            except Exception as e:  # noqa: BLE001
                print(f"balref: {e}")
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
            fail_since = fail_since or t0
            if SERIAL and t0 - fail_since >= REFIND_AFTER and t0 - last_refind >= REFIND_EVERY:
                last_refind = t0
                try:
                    if _refind():
                        continue                # od razu odczyt pod nowym adresem
                except Exception as e2:  # noqa: BLE001
                    REFIND.update(ts=int(time.time()), error=str(e2))
        time.sleep(max(5, INTERVAL - (time.time() - t0)))


def boost(minutes=2):
    """Profil Boost na podana liczbe minut - rekuperator sam wraca potem do poprzedniego profilu (timer w jednostce).
    Uzywane tylko przez test "ktory miernik to rekuperator" w Ustawieniach, po potwierdzeniu uzytkownika."""
    global _last_write
    if not FAN_WRITE_ENABLED:
        return {"ok": False, "error": "zapis wyłączony (vallox.py: FAN_WRITE_ENABLED)"}
    with _lock:
        ok_now, prof = latest.get("ok"), latest.get("profile")
    if not ok_now:
        return {"ok": False, "error": "brak aktualnego odczytu z rekuperatora"}
    if prof in (3, 4, 5):
        return {"ok": False, "error": "rekuperator jest teraz w profilu czasowym (Boost / Kominek / Extra) – spróbuj później"}

    async def _go():
        from vallox_websocket_api import Profile, Vallox
        r = Vallox(HOST).set_profile(Profile.BOOST, duration=int(minutes))
        if inspect.isawaitable(r):
            await r
    with _write_lock:
        _last_write = time.time()
        try:
            asyncio.run(_go())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True}


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
    if cs is not None:
        # rzeczywisty stan wymiennika; przelaczniki bypassu to tylko zezwolenie, nie dowod ze bypass jest otwarty
        return None if cs == 0 else f"wymiennik: {CELL_STATE.get(cs, cs)}"
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
               "frost_min": 15, "frost_step_min": 30,      # mroz: -1 °C najwyzej co 30 min (do 1.87: co 60 min)
               # 1.91: krzywa wg temperatury zewnetrznej (srednia 30 min) - w 1. kolejnosci przed nagrzewnica
               "curve_on": True, "curve_mode": "auto", "curve_t1": -2.0, "curve_s1": 17, "curve_t2": 5.0, "curve_s2": 19, "curve": False,
               "last_change": 0, "last_check": 0, "frost": False, "last_msg": ""}
LEARN_DAYS = 90                  # dni historii do uczenia krzywej (nastawa, temp. zewn., praca nagrzewnicy)
LEARN_MIN_H = 2                  # min. godzin danych dla pary (temperatura, nastawa)
LEARN_OK = 0.10                  # <= 10 % godzin z nagrzewnica -> nastawa bezpieczna przy tej temperaturze
LEARN_BAD = 0.30                 # >= 30 % godzin z nagrzewnica -> nastawa za wysoka
_learn = {"ts": 0, "data": None}
CURVE_HYST = 1.0                 # °C - wyjscie z krzywej dopiero przy zewn. >= curve_t2 + 1
CURVE_UP_MIN = 60                # min bez pracy nagrzewnicy, zanim nastawa wroci o 1 °C w gore do krzywej
SP_MIN_INTERVAL = 1800           # s - min. odstep zmian poza trybem mrozowym
SP_WINDOW = 1800                 # s - srednie temperatur z 30 min
SP_CHANGES_KEEP = 30             # ostatnie zmiany automatu (data, z -> na, powod) w vallox_supply.json


def _sp_load():
    try:
        d = json.loads(SP_FILE.read_text(encoding="utf-8")) if SP_FILE.exists() else {}
    except Exception:  # noqa: BLE001
        d = {}
    if not d.get("frost_step_v2"):      # 1.88: domyslnie co 30 min zamiast 60 (stary zapis mial 60 na sztywno)
        d["frost_step_min"], d["frost_step_v2"] = 30, True
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
               "spike_w", "spike_min", "frost_min", "frost_step_min", "summer_months",
               "curve_on", "curve_mode", "curve_t1", "curve_s1", "curve_t2", "curve_s2"}
    upd = {}
    for k, v in kw.items():
        if k not in allowed or v is None:
            continue
        if k == "curve_on":
            upd[k] = bool(v)
        elif k == "curve_mode":
            upd[k] = "auto" if str(v) == "auto" else "reczna"
            _learn["ts"] = 0
        elif k == "enabled":
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
    for k in ("curve_s1", "curve_s2"):
        if not SUPPLY_RANGE[0] <= c[k] <= SUPPLY_RANGE[1]:
            raise ValueError(f"nastawa krzywej poza zakresem {SUPPLY_RANGE[0]}–{SUPPLY_RANGE[1]} °C")
    if c["curve_t2"] <= c["curve_t1"]:
        raise ValueError("krzywa: druga temperatura musi być wyższa niż pierwsza")
    if not 10 <= c["frost_step_min"] <= 240:
        raise ValueError("odstęp zmian w mrozie: 10–240 min")
    if c["frost_exit"] <= c["frost_out"]:
        raise ValueError("koniec mrozu musi być wyżej niż próg mrozu")
    return _sp_save(**upd)


def _sp_avgs():
    now = time.time()
    xs = [(e, o) for ts, e, o in list(_bp_hist) if ts >= now - SP_WINDOW]
    if len(xs) < 10:
        return None, None, len(xs)
    return sum(e for e, _ in xs) / len(xs), sum(o for _, o in xs) / len(xs), len(xs)


def _meter_rows(db_mod, con, t0, t1):
    """Probki miernika poboru rekuperatora (meter.py) albo [] - bez miernika."""
    import meter
    return db_mod.load_meter(con, t0, t1) if meter.ENABLED else []


def _heater_run(db_connect, db_mod, threshold, seconds=3600):
    """Najdluzsza ciagla praca z poborem >= threshold [min] i najwieksza srednia moc [W] w ostatniej godzinie.

    Z miernikiem (meter.py): srednia moc w kazdym odstepie (ok. 60 s) z licznika energii - krotkie piki kontrolne Vallox
    prawie nie podnosza sredniej minutowej. Bez miernika: sygnal nagrzewnicy z rekuperatora (A_CYC_IO_HEATER)."""
    import meter
    if not meter.ENABLED:
        with _lock:
            on = bool(latest.get("heater"))
        return (float(_sp_load()["spike_min"]), 0.0) if on else (0.0, 0.0)
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
    Poza miesiacami letnimi zawsze 22 °C (wczesniej nastawa letnia zostawala, gdy wywiew byl w histerezie 21-22).
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
    month = dt.date.today().month
    if a.get("curve_on") and month not in a["summer_months"]:
        return _curve_tick(a, now, cur, t_ext, t_out, db_connect, db_mod)
    frost = a["frost"]
    if not frost and t_out <= a["frost_out"]:
        frost = True
    elif frost and t_out >= a["frost_exit"]:
        frost = False
    want, why = None, ""
    if frost:
        run, pmax = _heater_run(db_connect, db_mod, a["spike_w"])
        if run is not None and run >= a["spike_min"]:
            txt = f"nagrzewnica ~{run:.0f} min (do {pmax:.0f} W)"
            if cur > a["frost_min"] and now - a["last_change"] >= a["frost_step_min"] * 60:
                want, why = cur - 1, f"mróz ({t_out:.1f} °C), {txt} – nastawa −1 °C"
            else:
                why = f"mróz, {txt} – " + ("minimum osiągnięte" if cur <= a["frost_min"] else f"czekam {a['frost_step_min']:g} min po zmianie")
        else:
            why = (f"mróz ({t_out:.1f} °C), bez pracy nagrzewnicy (najdłużej {run or 0:.0f} min ≥ {a['spike_w']:g} W, "
                   f"maks. {pmax or 0:.0f} W) – bez zmian")
    else:
        summer = month in a["summer_months"]
        if summer and t_ext >= a["ext_hot"]:
            want, why = int(a["summer_target"]), f"lato, wywiew {t_ext:.1f} °C ≥ {a['ext_hot']:g} – chłodzenie / bypass"
        elif not summer:                       # poza miesiacami letnimi zawsze nastawa normalna (odzysk ciepla)
            want, why = int(a["normal_target"]), f"poza sezonem letnim (wywiew {t_ext:.1f} °C) – odzysk ciepła"
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
    ch = {"ts": int(now), "old": cur, "new": want, "why": why, "ok": bool(r.get("ok")),
          "t_ext": round(t_ext, 1), "t_out": round(t_out, 1)}
    if not r.get("ok"):
        ch["error"] = r.get("error")
    _sp_save(last_check=now, frost=frost, last_change=now if r.get("ok") else a["last_change"],
             changes=([ch] + list(a.get("changes") or []))[:SP_CHANGES_KEEP],
             last_msg=(f"{cur} → {want} °C: " + why) if r.get("ok") else f"zapis nieudany – {r.get('error')}")


def learn_curve(db_connect, db_mod, a=None, force=False):
    """Krzywa nastawy wyuczona z historii (co godzine): dla kazdej temperatury zewn. (co 1 °C) najwyzsza nastawa, przy ktorej
    nagrzewnica prawie nie pracowala (<= 10 % godzin), ponizej najnizszej, przy ktorej pracowala czesto (>= 30 %).
    Dane: zmiany nastawy (vallox_log, w tym automatu), temperatura zewn. (vallox_samples), pobor rekuperatora (miernik).
    Brakujace temperatury - interpolacja; krzywa niemalejaca z temperatura; zakres frost_min .. nastawa normalna.
    Zwraca {"bins": {T: nastawa}, "obs": {T: {nastawa: [godzin, z_nagrzewnica]}}, "exit_t", "hours", "points"} albo None."""
    now = time.time()
    if not force and _learn["data"] is not None and now - _learn["ts"] < 3600:
        return _learn["data"]
    a = a or _sp_load()
    t0 = int(now) - LEARN_DAYS * 86400
    con = db_connect()
    try:
        ev = con.execute("SELECT ts, old_value, new_value FROM vallox_log WHERE param = 'supply_temp' AND ok = 1 AND ts >= ? "
                         "ORDER BY ts", (t0,)).fetchall()
        tr = con.execute("SELECT ts / 3600, AVG(t_out) FROM vallox_samples WHERE ts >= ? AND t_out IS NOT NULL "
                         "GROUP BY ts / 3600", (t0,)).fetchall()
        sh = _meter_rows(db_mod, con, t0, int(now))
    finally:
        con.close()
    cur = supply_target()
    if not tr or (not ev and cur is None):
        _learn.update(ts=now, data=None)
        return None
    # nastawa w kazdej godzinie z historii zmian (przed pierwsza zmiana - jej "stara" wartosc)
    first = ev[0][1] if ev else cur
    def setpoint_at(ts, _i=[0]):
        sp = first
        for t, _o, n in ev:
            if t > ts:
                break
            sp = n
        return sp
    # minuty pracy nagrzewnicy w kazdej godzinie (srednia moc z licznika energii >= spike_w)
    heat, prev = {}, None
    for ts, pw, e in sh:
        if prev is not None and 0 < ts - prev <= 300:
            d = ts - prev
            avg = float(e) * 3600 / d if e is not None else (float(pw) if pw is not None else 0.0)
            if avg >= a["spike_w"]:
                h = ts // 3600
                heat[h] = heat.get(h, 0.0) + d / 60
        prev = ts
    has_shelly = bool(sh)
    obs = {}
    hours = 0
    summer = set(a.get("summer_months") or [])
    for hh, t in tr:
        if t is None or not has_shelly:
            continue
        if time.localtime(int(hh) * 3600).tm_mon in summer:      # lato: 17 °C = chlodzenie, nie ochrona przed mrozem
            continue
        sp = setpoint_at(int(hh) * 3600 + 1800)
        if sp is None:
            continue
        T, sp = int(math.floor(float(t))), int(round(float(sp)))
        o = obs.setdefault(T, {}).setdefault(sp, [0, 0])
        o[0] += 1
        o[1] += 1 if heat.get(int(hh), 0.0) >= a["spike_min"] else 0
        hours += 1
    lo_sp, hi_sp = int(a["frost_min"]), int(a["normal_target"])
    learned = {}
    for T, by in obs.items():
        good = [sp for sp, (n, h) in by.items() if n >= LEARN_MIN_H and h / n <= LEARN_OK]
        bad = [sp for sp, (n, h) in by.items() if n >= LEARN_MIN_H and h / n >= LEARN_BAD]
        lim_bad = min(bad) if bad else None
        ok = [sp for sp in good if lim_bad is None or sp < lim_bad]
        if ok and (lim_bad is not None or max(ok) >= int(a["normal_target"])):
            learned[T] = max(ok)
        elif lim_bad is not None:
            learned[T] = lim_bad - 1
        # tylko niskie nastawy bez nagrzewnicy i bez zadnej "za wysokiej" - nie wiadomo, czy wyzsza by nie wystarczyla
        # (dolne ograniczenie) - punkt pomijany, krzywa z sasiednich temperatur
    if not learned:
        data = {"bins": {}, "obs": obs, "exit_t": None, "hours": hours, "points": 0}
        _learn.update(ts=now, data=data)
        return data
    pts = dict(learned)
    # krzywa od -15 do +15 °C: interpolacja miedzy punktami, poza nimi - wartosci skrajne (najzimniejszy / najcieplejszy)
    ks = sorted(pts)
    bins = {}
    for T in range(-15, 16):
        if T <= ks[0]:
            v = pts[ks[0]] if T == ks[0] else min(pts[ks[0]], hi_sp)
        elif T >= ks[-1]:
            v = pts[ks[-1]]
        else:
            k1 = max(k for k in ks if k <= T)
            k2 = min(k for k in ks if k >= T)
            v = pts[k1] if k1 == k2 else pts[k1] + (pts[k2] - pts[k1]) * (T - k1) / (k2 - k1)
        bins[T] = max(lo_sp, min(hi_sp, int(round(v))))
    for T in range(14, -16, -1):          # niemalejaca z temperatura: zimniej nigdy wyzej niz cieplej
        bins[T] = min(bins[T], bins[T + 1])
    # gorna granica krzywej: najzimniejsza temperatura, od ktorej dane pokazuja nastawe normalna bez nagrzewnicy
    # (od niej w gore wszystkie punkty wyuczone na nastawie normalnej - pojedynczy cieply wyjatek nie przesuwa granicy)
    ks_l = sorted(learned)
    exit_t = next((T for i, T in enumerate(ks_l) if all(learned[x] >= hi_sp for x in ks_l[i:])), None)
    data = {"bins": bins, "obs": obs, "exit_t": exit_t, "hours": hours, "points": len(learned),
            "learned": {T: learned[T] for T in sorted(learned)}}
    _learn.update(ts=now, data=data)
    return data


def curve_target(a, t_out, learned=None):
    """Nastawa z krzywej: wyuczonej (curve_mode = auto, gdy sa dane) albo recznej (t1 -> s1, t2 -> s2, liniowo)."""
    if learned and learned.get("bins"):
        T = max(-15, min(15, int(math.floor(t_out))))
        return learned["bins"][T]
    """Nastawa z krzywej: zewn. <= t1 -> s1, zewn. t2 -> s2, pomiedzy liniowo (zaokraglenie do 1 °C)."""
    t1, t2, s1, s2 = a["curve_t1"], a["curve_t2"], a["curve_s1"], a["curve_s2"]
    if t_out <= t1:
        v = s1
    elif t_out >= t2:
        v = s2
    else:
        v = s1 + (s2 - s1) * (t_out - t1) / (t2 - t1)
    return int(round(v))


def _sp_apply(a, now, cur, want, why, t_ext, t_out, **state):
    if want is None or want == cur:
        _sp_save(last_check=now, **state, last_msg=why + (f" (nastawa {cur} °C)" if want == cur else ""))
        return
    r = set_supply_target(want, "automat", _ctx_db["c"], _ctx_db["m"])
    ch = {"ts": int(now), "old": cur, "new": want, "why": why, "ok": bool(r.get("ok")),
          "t_ext": round(t_ext, 1), "t_out": round(t_out, 1)}
    if not r.get("ok"):
        ch["error"] = r.get("error")
    _sp_save(last_check=now, **state, last_change=now if r.get("ok") else a["last_change"],
             changes=([ch] + list(a.get("changes") or []))[:SP_CHANGES_KEEP],
             last_msg=(f"{cur} → {want} °C: " + why) if r.get("ok") else f"zapis nieudany – {r.get('error')}")


_ctx_db = {"c": None, "m": None}


def _curve_tick(a, now, cur, t_ext, t_out, db_connect, db_mod):
    """Poza latem: w 1. kolejnosci nastawa wg temperatury zewnetrznej (krzywa), w 2. - praca nagrzewnicy obniza ja dalej
    o 1 °C (co frost_step_min, do frost_min); bez nagrzewnicy przez CURVE_UP_MIN min nastawa wraca o 1 °C w gore do krzywej.
    Powyzej krzywej (zewn. >= t2 + 1 °C) - nastawa normalna."""
    _ctx_db.update(c=db_connect, m=db_mod)
    learned = None
    if a.get("curve_mode", "auto") == "auto":
        try:
            learned = learn_curve(db_connect, db_mod, a)
        except Exception as e:  # noqa: BLE001 - bez danych krzywa reczna
            print(f"vallox: uczenie krzywej: {e}")
        if learned and not learned.get("bins"):
            learned = None
    if learned and learned.get("exit_t") is not None:
        top = learned["exit_t"] - 1            # od tej temperatury dane pokazuja nastawe normalna bez nagrzewnicy
    elif learned:
        top = max(max(learned["learned"]), a["curve_t2"])   # brak danych z cieplejszych dni - nie dalej niz reczny punkt
    else:
        top = a["curve_t2"]
    zone = a.get("curve", False)
    if not zone and t_out <= top:
        zone = True
    elif zone and t_out >= top + CURVE_HYST:
        zone = False
    since = now - a["last_change"]
    if not zone:
        want = int(a["normal_target"])
        why = f"zewn. {t_out:.1f} °C powyżej krzywej (≥ {top + CURVE_HYST:g} °C) – nastawa normalna, odzysk ciepła"
        if want != cur and since < SP_MIN_INTERVAL:
            return _sp_save(last_check=now, curve=zone, frost=False, last_msg=why + " (czekam – min. 30 min między zmianami)")
        return _sp_apply(a, now, cur, want, why, t_ext, t_out, curve=zone, frost=False)
    base = curve_target(a, t_out, learned)
    run, pmax = _heater_run(db_connect, db_mod, a["spike_w"])
    heater = run is not None and run >= a["spike_min"]
    ctxt = (f"krzywa {'wyuczona' if learned else 'ręczna'}: zewn. {t_out:.1f} °C → {base} °C")
    if heater:
        low = max(int(a["frost_min"]), min(base, cur) - 1)
        if low < cur and since >= a["frost_step_min"] * 60:
            return _sp_apply(a, now, cur, low, f"{ctxt}; nagrzewnica ~{run:.0f} min (do {pmax:.0f} W) – dodatkowo −1 °C",
                             t_ext, t_out, curve=zone, frost=True)
        why = f"{ctxt}; nagrzewnica ~{run:.0f} min – " + ("minimum osiągnięte" if cur <= a["frost_min"]
                                                          else f"czekam {a['frost_step_min']:g} min po zmianie")
        return _sp_save(last_check=now, curve=zone, frost=True, last_msg=why)
    if cur > base:
        if since < SP_MIN_INTERVAL:
            return _sp_save(last_check=now, curve=zone, frost=False, last_msg=f"{ctxt} (czekam – min. 30 min między zmianami)")
        return _sp_apply(a, now, cur, base, ctxt, t_ext, t_out, curve=zone, frost=False)
    if cur < base:
        if since < CURVE_UP_MIN * 60:
            return _sp_save(last_check=now, curve=zone, frost=False,
                            last_msg=f"{ctxt}; bez nagrzewnicy (najdłużej {run or 0:.0f} min) – powrót w górę po {CURVE_UP_MIN} min od zmiany")
        return _sp_apply(a, now, cur, cur + 1, f"{ctxt}; nagrzewnica nie pracuje {CURVE_UP_MIN} min – +1 °C w stronę krzywej",
                         t_ext, t_out, curve=zone, frost=False)
    return _sp_save(last_check=now, curve=zone, frost=False, last_msg=f"{ctxt} (nastawa {cur} °C)")


# --- 1.93: predkosc zimowa - przy chlodzie mniejszy przeplyw (lepszy odzysk, mniej ciepla wywiewanego na zewnatrz) ---------
# Obniza predkosc "W domu" (A_CYC_HOME_SPEED_SETTING) o 1 % co 30 min, az szacowany przeplyw nawiewu (z obrotow) zejdzie do
# zadanego minimum. Sterowanie CO2 / wilgotnoscia Vallox zostaje - sam podnosi obroty ponad predkosc bazowa, gdy trzeba.
# Gdy podnosi je zbyt czesto (CO2 / RH) albo wilgotnosc >= progu - predkosc bazowa +1 %. Cieplo (zewn. >= prog + 1 °C)
# albo wylaczenie automatu - powrot do predkosci normalnej (zapamietanej przed obnizeniem).
WS_FILE = config.DATA_DIR / "vallox_speed.json"
WS_DEFAULTS = {"enabled": False, "t_on": 10.0, "flow_min": 120, "rh_max": 60, "boost_max": 50,
               "active": False, "normal": None, "restore": False, "last_change": 0, "last_check": 0, "last_msg": "", "changes": []}
WS_KEY = "A_CYC_HOME_SPEED_SETTING"
WS_MIN_PCT = 20                  # % - nigdy nizej (niezaleznie od przeplywu)
WS_INTERVAL = 1800               # s - zmiana najwyzej co 30 min
WS_HYST = 1.0                    # °C
WS_FLOW_TOL = 4                  # m3/h - tolerancja wokol minimum
_ws_hist = deque(maxlen=90)      # (ts, t_out, rh, fan_pct, home_speed, flow_sup, defrosting) co ok. 1 min


def _ws_load():
    try:
        d = json.loads(WS_FILE.read_text(encoding="utf-8")) if WS_FILE.exists() else {}
    except Exception:  # noqa: BLE001
        d = {}
    return {**WS_DEFAULTS, **d}


def _ws_save(**kw):
    d = _ws_load()
    d.update(kw)
    WS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def set_home_speed(v, source, db_connect=None, db_mod=None):
    """Predkosc wentylacji w profilu Dom [%] (A_CYC_HOME_SPEED_SETTING)."""
    global _last_write
    if not FAN_WRITE_ENABLED:
        return {"ok": False, "error": "zapis wyłączony (vallox.py: FAN_WRITE_ENABLED)"}
    v = int(v)
    if not max(FAN_MIN, WS_MIN_PCT) <= v <= FAN_MAX:
        return {"ok": False, "error": f"zakres {max(FAN_MIN, WS_MIN_PCT)}–{FAN_MAX} %"}
    with _lock:
        cur, ok_now = latest.get("home_speed"), latest.get("ok")
    if not ok_now or cur is None:
        return {"ok": False, "error": "brak aktualnego odczytu z rekuperatora"}
    if int(cur) == v:
        return {"ok": True, "changed": {}}
    with _write_lock:
        wait = FAN_WRITE_INTERVAL - (time.time() - _last_write)
        if wait > 0:
            return {"ok": False, "error": f"odczekaj {int(wait) + 1} s przed kolejnym zapisem"}
        _last_write = time.time()
        ts, err = int(time.time()), None
        try:
            asyncio.run(_write(HOST, {WS_KEY: v}))
            time.sleep(2)
            vals, extra = read_once()
            with _lock:
                latest.update(vals, **extra, ts=int(time.time()), ok=True)
            if extra.get("home_speed") != v:
                err = f"po zapisie odczyt się nie zgadza: {extra.get('home_speed')}"
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        if db_connect and db_mod:
            try:
                con = db_connect()
                try:
                    db_mod.log_vallox_change(con, ts, "home_speed", int(cur), v, source, err is None, err)
                finally:
                    con.close()
            except Exception as e:  # noqa: BLE001
                print(f"vallox_log: {e}")
    return {"ok": False, "error": err} if err else {"ok": True, "changed": {"home_speed": [int(cur), v]}}


def _ws_stats(now):
    h30 = [x for x in _ws_hist if x[0] >= now - 1800]
    h60 = [x for x in _ws_hist if x[0] >= now - 3600]
    if len(h30) < 10:
        return None
    t_out = [x[1] for x in h30 if x[1] is not None]
    rh = [x[2] for x in h30 if x[2] is not None]
    base = [x[5] for x in h30 if x[3] is not None and x[4] is not None and x[3] <= x[4] and x[5]]   # bez podbicia CO2 / RH
    boosted = sum(1 for x in h60 if x[3] is not None and x[4] is not None and x[3] > x[4])
    return {"n": len(h30), "t_out": sum(t_out) / len(t_out) if t_out else None, "rh": sum(rh) / len(rh) if rh else None,
            "flow": sum(base) / len(base) if base else None, "boost": 100.0 * boosted / len(h60) if h60 else 0.0,
            "defrost": any(x[6] for x in h30)}


def _ws_change(a, now, cur, new, why, st, db_connect, db_mod, **state):
    r = set_home_speed(new, "automat", db_connect, db_mod)
    ch = {"ts": int(now), "old": cur, "new": new, "why": why, "ok": bool(r.get("ok")),
          "t_out": None if st.get("t_out") is None else round(st["t_out"], 1),
          "flow": None if st.get("flow") is None else round(st["flow"])}
    if not r.get("ok"):
        ch["error"] = r.get("error")
    _ws_save(last_check=now, last_change=now if r.get("ok") else a["last_change"], **state,
             changes=([ch] + list(a.get("changes") or []))[:SP_CHANGES_KEEP],
             last_msg=(f"{cur} → {new} %: " + why) if r.get("ok") else f"zapis nieudany – {r.get('error')}")


def speed_tick(db_connect, db_mod, vals, extra):
    now = time.time()
    _ws_hist.append((now, vals.get("t_out"), vals.get("rh"), vals.get("fan_pct"), extra.get("home_speed"),
                     airflow(vals.get("rpm_sup"), "sup"), bool(extra.get("defrosting"))))
    a = _ws_load()
    cur = extra.get("home_speed")
    if cur is None or now - a["last_check"] < 300:
        return
    cur = int(cur)
    normal = a.get("normal")
    if not a["enabled"]:
        if a.get("restore") and normal and cur < normal and now - a["last_change"] >= 60:
            return _ws_change(a, now, cur, min(normal, cur + FAN_MAX_STEP), "automat wyłączony – powrót do prędkości normalnej",
                              {}, db_connect, db_mod, restore=cur + FAN_MAX_STEP < normal, active=False)
        if a.get("restore") or a.get("active"):
            _ws_save(restore=False, active=False, last_check=now)
        return
    if vals.get("profile") not in (0, None):
        return _ws_save(last_check=now, last_msg="profil inny niż Dom – czekam")
    st = _ws_stats(now)
    if not st or st["t_out"] is None:
        return _ws_save(last_check=now, last_msg="za mało pomiarów – czekam")
    active = a.get("active", False)
    if not active and st["t_out"] < a["t_on"]:
        active = True
    elif active and st["t_out"] >= a["t_on"] + WS_HYST:
        active = False
    if not active:
        back = a.get("active") or a.get("restore")          # dopiero co cieplej - powrot do predkosci normalnej
        if back and normal and cur < normal:
            if now - a["last_change"] < WS_INTERVAL:
                return _ws_save(last_check=now, active=False, restore=True,
                                last_msg="zewn. powyżej progu – powrót do prędkości normalnej po 30 min od zmiany")
            new = min(normal, cur + FAN_MAX_STEP)
            return _ws_change(a, now, cur, new, f"zewn. {st['t_out']:.1f} °C ≥ {a['t_on'] + WS_HYST:g} °C – prędkość normalna",
                              st, db_connect, db_mod, active=False, restore=new < normal)
        return _ws_save(last_check=now, active=False, restore=False, normal=cur,
                        last_msg=f"zewn. {st['t_out']:.1f} °C – powyżej progu {a['t_on']:g} °C, prędkość normalna {cur} %")
    if not normal or (not a.get("active") and cur > normal):
        normal = cur                          # predkosc sprzed obnizenia (do powrotu)
    top = normal
    floor = max(FAN_MIN, WS_MIN_PCT)
    flow = st["flow"]
    info = (f"zewn. {st['t_out']:.1f} °C, przepływ {'–' if flow is None else round(flow)} m³/h (min. {a['flow_min']:g}), "
            f"podbicie CO₂/RH {st['boost']:.0f} % czasu, RH {'–' if st['rh'] is None else round(st['rh'])} %")
    wait = now - a["last_change"] < WS_INTERVAL
    up = down = None
    if st["rh"] is not None and st["rh"] >= a["rh_max"]:
        up = f"wilgotność {st['rh']:.0f} % ≥ {a['rh_max']:g} % – +1 %"
    elif st["boost"] >= a["boost_max"]:
        up = f"Vallox podbija obroty (CO₂ / wilgotność) przez {st['boost']:.0f} % czasu – +1 %"
    elif flow is not None and flow < a["flow_min"] - WS_FLOW_TOL:
        up = f"przepływ {flow:.0f} m³/h poniżej minimum – +1 %"
    elif st["defrost"]:
        pass                                  # odszranianie - bez obnizania
    elif flow is not None and flow > a["flow_min"] + WS_FLOW_TOL and cur > floor:
        down = f"przepływ {flow:.0f} m³/h > {a['flow_min']:g} m³/h – −1 %"
    if up and cur < top:
        if wait:
            return _ws_save(last_check=now, active=True, normal=normal, last_msg=info + " (czekam – min. 30 min między zmianami)")
        return _ws_change(a, now, cur, cur + 1, up, st, db_connect, db_mod, active=True, normal=normal)
    if down:
        if wait:
            return _ws_save(last_check=now, active=True, normal=normal, last_msg=info + " (czekam – min. 30 min między zmianami)")
        return _ws_change(a, now, cur, cur - 1, down, st, db_connect, db_mod, active=True, normal=normal)
    return _ws_save(last_check=now, active=True, normal=normal,
                    last_msg=info + (" – odszranianie, bez obniżania" if st["defrost"] else " – bez zmian"))


def speed_auto_set(**kw):
    upd = {}
    for k, v in kw.items():
        if v is None:
            continue
        if k == "enabled":
            upd["enabled"] = bool(v)
            upd["last_change"] = 0
            upd["last_msg"] = "włączono" if v else "wyłączono – powrót do prędkości normalnej"
            if not v:
                upd["restore"] = True
        elif k in ("t_on", "flow_min", "rh_max", "boost_max"):
            upd[k] = float(v)
    c = {**_ws_load(), **upd}
    if not -10 <= c["t_on"] <= 20:
        raise ValueError("próg temperatury: −10 … 20 °C")
    if not 50 <= c["flow_min"] <= 300:
        raise ValueError("minimalny przepływ: 50–300 m³/h")
    if not 40 <= c["rh_max"] <= 80:
        raise ValueError("próg wilgotności: 40–80 %")
    if not 10 <= c["boost_max"] <= 100:
        raise ValueError("podbicie CO₂ / RH: 10–100 % czasu")
    return _ws_save(**upd)


def speed_status():
    a = _ws_load()
    st = _ws_stats(time.time()) or {}
    return {**a, "stats": {k: (round(v, 1) if isinstance(v, float) else v) for k, v in st.items()}, "min_pct": max(FAN_MIN, WS_MIN_PCT)}


# --- 1.94: odszranianie wymiennika - sposob (0 = obejscie, nawiew dogrzewany nagrzewnica; 1 = zatrzymanie wentylatora
# nawiewu, bez grzania) i temperatura nawiewu podczas odszraniania w trybie obejscia ----------------------------------
DEFROST_MODES = {0: "obejście wymiennika (nawiew dogrzewany nagrzewnicą)", 1: "zatrzymanie wentylatora nawiewu (bez grzania)"}
DEFROST_KEYS = {"defrost_mode": "A_CYC_DEFROST_MODE", "defrost_sup_temp": "A_CYC_SUPPLY_AIR_DEFROST_TEMP"}
DEFROST_TEMP_RANGE = (10, 25)


def set_defrost(values, source, db_connect=None, db_mod=None):
    """values: {"defrost_mode": 0|1, "defrost_sup_temp": °C} (jedno lub oba)."""
    global _last_write
    if not FAN_WRITE_ENABLED:
        return {"ok": False, "error": "zapis wyłączony (vallox.py: FAN_WRITE_ENABLED)"}
    with _lock:
        cur = {k: latest.get(k) for k in DEFROST_KEYS}
        ok_now = latest.get("ok")
    if not ok_now or None in cur.values():
        return {"ok": False, "error": "brak aktualnego odczytu z rekuperatora"}
    new = {}
    for k, v in values.items():
        if k not in DEFROST_KEYS or v is None:
            continue
        try:
            v = int(round(float(v)))
        except (TypeError, ValueError):
            return {"ok": False, "error": f"{k}: wartość musi być liczbą"}
        if k == "defrost_mode" and v not in DEFROST_MODES:
            return {"ok": False, "error": "nieznany sposób odszraniania"}
        if k == "defrost_sup_temp" and not DEFROST_TEMP_RANGE[0] <= v <= DEFROST_TEMP_RANGE[1]:
            return {"ok": False, "error": f"nawiew przy odszranianiu: {DEFROST_TEMP_RANGE[0]}–{DEFROST_TEMP_RANGE[1]} °C"}
        if round(float(cur[k])) != v:
            new[k] = v
    if not new:
        return {"ok": True, "changed": {}, "msg": "bez zmian"}
    with _write_lock:
        wait = FAN_WRITE_INTERVAL - (time.time() - _last_write)
        if wait > 0:
            return {"ok": False, "error": f"odczekaj {int(wait) + 1} s przed kolejnym zapisem"}
        _last_write = time.time()
        ts, err = int(time.time()), None
        try:
            asyncio.run(_write(HOST, {DEFROST_KEYS[k]: v for k, v in new.items()}))
            time.sleep(2)
            vals, extra = read_once()
            with _lock:
                latest.update(vals, **extra, ts=int(time.time()), ok=True)
            bad = [k for k, v in new.items() if extra.get(k) is None or round(float(extra[k])) != v]
            if bad:
                err = "po zapisie odczyt się nie zgadza: " + ", ".join(f"{k}={extra.get(k)}" for k in bad)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        if db_connect and db_mod:
            try:
                con = db_connect()
                try:
                    for k, v in new.items():
                        db_mod.log_vallox_change(con, ts, k, round(float(cur[k])), v, source, err is None, err)
                finally:
                    con.close()
            except Exception as e:  # noqa: BLE001
                print(f"vallox_log: {e}")
    return {"ok": False, "error": err} if err else {"ok": True, "changed": {k: [round(float(cur[k])), v] for k, v in new.items()}}


def defrost_stats(db_connect, db_mod, days=7):
    """Odszranianie a nagrzewnica w ostatnich dniach: minuty odszraniania (cell_state = 3), praca nagrzewnicy (sredni pobor
    >= spike_w z miernika) i energia - osobno podczas odszraniania i poza nim, dzien po dniu."""
    a = _sp_load()
    now = int(time.time())
    t0 = now - days * 86400
    con = db_connect()
    try:
        cs = con.execute("SELECT ts, cell_state, t_out FROM vallox_samples WHERE ts >= ? ORDER BY ts", (t0 - 600,)).fetchall()
        sh = _meter_rows(db_mod, con, t0, now + 60)
    finally:
        con.close()
    by_day = {}

    def day(ts):
        d = dt.date.fromtimestamp(ts).isoformat()
        return by_day.setdefault(d, {"date": d, "defrost_min": 0.0, "heat_def_min": 0.0, "heat_def_wh": 0.0,
                                     "heat_oth_min": 0.0, "heat_oth_wh": 0.0, "t_min": None})
    prev = None
    for ts, st, t in cs:                          # minuty odszraniania + najnizsza temperatura zewn.
        if ts >= t0:
            r = day(ts)
            if t is not None:
                r["t_min"] = t if r["t_min"] is None else min(r["t_min"], t)
            if prev is not None and prev[1] == 3 and 0 < ts - prev[0] <= 900:
                r["defrost_min"] += (ts - prev[0]) / 60
        prev = (ts, st)
    i, state, pv = 0, None, None
    for ts, pw, e in sh:                           # praca nagrzewnicy wg stanu wymiennika w tej chwili
        while i < len(cs) and cs[i][0] <= ts:
            state = cs[i][1]
            i += 1
        if pv is not None and 0 < ts - pv <= 300:
            d = ts - pv
            avg = float(e) * 3600 / d if e is not None else (float(pw) if pw is not None else 0.0)
            if avg >= a["spike_w"]:
                r = day(ts)
                k = "def" if state == 3 else "oth"
                r[f"heat_{k}_min"] += d / 60
                r[f"heat_{k}_wh"] += float(e) if e is not None else avg * d / 3600
        pv = ts
    rows = [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in r.items()} for _, r in sorted(by_day.items())]
    tot = {k: round(sum(r[k] for r in rows), 1) for k in ("defrost_min", "heat_def_min", "heat_def_wh", "heat_oth_min", "heat_oth_wh")}
    return {"days": rows, "total": tot, "spike_w": a["spike_w"]}


# --- 1.97: bilans wzorcowy + obserwacja parametrow pracy (zamiast korekt co godzine wg temperatur) -------------------
# Zima: bilans temperatur jest przeklamany (wilgoc / kondensacja w wymienniku entalpicznym), a obroty i przeplywy zmieniaja
# predkosc zimowa i podbicia CO2. Dlatego: ustawienie uznane za dobre zapisujemy jako wzorzec, a potem porownujemy
# tylko stabilne minuty (bez zmian ustawien od 10 min, bez podbicia CO2 / RH, wymiennik w odzysku) przy TEJ SAMEJ
# predkosci - obroty i ich stosunek. Odchylka > 5 % przez 3 dni = powiadomienie (np. zabrudzone filtry).
BR_FILE = config.DATA_DIR / "vallox_balref.json"
BR_SETTLE = 600            # s bez zmian ustawien przed liczeniem minut
BR_REF_MIN = 12 * 60       # min stabilnych minut, zeby wzorzec byl gotowy
BR_DAY_MIN = 120           # min stabilnych minut dnia (przy ustawieniu wzorcowym) do porownania
BR_DEV = 5.0               # % - odchylka obrotow / ich stosunku
BR_DAYS = 3                # dni z rzedu z odchylka -> powiadomienie
BR_KEEP_DAYS = 40
BR_FIELDS = ("rpm_sup", "rpm_ext", "flow_sup", "flow_ext", "diff", "eff_sup", "t_exh", "t_out")
_br = {"last_key": None, "since": 0.0, "dirty": 0, "data": None}


def _br_load():
    if _br["data"] is None:
        try:
            _br["data"] = json.loads(BR_FILE.read_text(encoding="utf-8")) if BR_FILE.exists() else {}
        except Exception:  # noqa: BLE001
            _br["data"] = {}
        d = _br["data"]
        d.setdefault("ref", None)
        d.setdefault("settings", {})
        d.setdefault("days", {})
        d.setdefault("notify", True)
        d.setdefault("dev_days", [])
        d.setdefault("last_eval", "")
        d.setdefault("msg", "")
    return _br["data"]


def _br_save(force=False):
    _br["dirty"] += 1
    if force or _br["dirty"] >= 10:
        try:
            BR_FILE.write_text(json.dumps(_br_load(), ensure_ascii=False), encoding="utf-8")
            _br["dirty"] = 0
        except OSError:
            pass


def _acc(bucket, x):
    bucket["n"] = bucket.get("n", 0) + 1
    for k in BR_FIELDS:
        v = x.get(k)
        if v is not None:
            bucket[k] = bucket.get(k, 0.0) + float(v)
            bucket["n_" + k] = bucket.get("n_" + k, 0) + 1


def _avg(bucket):
    out = {"min": bucket.get("n", 0)}
    for k in BR_FIELDS:
        n = bucket.get("n_" + k, 0)
        out[k] = round(bucket[k] / n, 2) if n else None
    if out["rpm_sup"] and out["rpm_ext"]:
        out["ratio"] = round(out["rpm_sup"] / out["rpm_ext"], 4)
    return out


def balref_tick(vals, extra):
    """Co odczyt (ok. 60 s): stabilna minuta -> statystyki ustawienia (nawiew/wywiew/predkosc), dnia i wzorca."""
    now = time.time()
    sb, eb, hs = extra.get("supp_base"), extra.get("extr_base"), extra.get("home_speed")
    if None in (sb, eb, hs):
        return
    key = f"{int(sb)}/{int(eb)}/{int(hs)}"
    if key != _br["last_key"]:
        _br.update(last_key=key, since=now)
    d = _br_load()
    _br_eval(d, now)
    if now - _br["since"] < BR_SETTLE or vals.get("cell_state") not in (0, None) or vals.get("profile") not in (0, None):
        return
    if vals.get("fan_pct") is None or int(vals["fan_pct"]) != int(hs):       # podbicie CO2 / wilgotnosc
        return
    b = balance(vals)
    x = {k: vals.get(k) for k in ("rpm_sup", "rpm_ext", "eff_sup", "t_exh", "t_out")}
    x["flow_sup"], x["flow_ext"] = airflow(vals.get("rpm_sup"), "sup"), airflow(vals.get("rpm_ext"), "ext")
    x["diff"] = b[0] if b and b[1] >= AUTO_MIN_SPAN else None
    st = d["settings"].setdefault(key, {"first": int(now)})
    st["last"] = int(now)
    _acc(st, x)
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    _acc(d["days"].setdefault(day, {}).setdefault(key, {}), x)
    r = d.get("ref")
    if r and r.get("pending") and key.startswith(f"{r['supp_base']}/{r['extr_base']}/"):
        _acc(r.setdefault("by_home", {}).setdefault(str(int(hs)), {}), x)
        if sum(v.get("n", 0) for v in r["by_home"].values()) >= BR_REF_MIN:
            r["pending"] = False
            r["ready_ts"] = int(now)
            _br_save(force=True)
            return
    _br_save()


def _br_compare(r, day_buckets):
    """Odchylka dnia od wzorca [%] przy tych samych predkosciach: (obroty nawiewu, obroty wywiewu, stosunek, minuty)."""
    acc = {"sup": 0.0, "ext": 0.0, "ratio": 0.0, "w": 0.0}
    for hs, rb in (r.get("by_home") or {}).items():
        db = day_buckets.get(f"{r['supp_base']}/{r['extr_base']}/{hs}")
        if not db or db.get("n", 0) < 30 or rb.get("n", 0) < 30:
            continue
        ra, da = _avg(rb), _avg(db)
        if not (ra.get("ratio") and da.get("ratio")):
            continue
        w = min(rb["n"], db["n"])
        acc["sup"] += w * (da["rpm_sup"] / ra["rpm_sup"] - 1) * 100
        acc["ext"] += w * (da["rpm_ext"] / ra["rpm_ext"] - 1) * 100
        acc["ratio"] += w * (da["ratio"] / ra["ratio"] - 1) * 100
        acc["w"] += w
    if acc["w"] <= 0:
        return None
    return {"sup": round(acc["sup"] / acc["w"], 1), "ext": round(acc["ext"] / acc["w"], 1),
            "ratio": round(acc["ratio"] / acc["w"], 1), "min": int(sum(b.get("n", 0) for k, b in day_buckets.items()
                                                                     if k.startswith(f"{r['supp_base']}/{r['extr_base']}/")))}


def _br_eval(d, now):
    """Raz na dobe (po polnocy) - porownanie wczorajszego dnia ze wzorcem; 3 dni z odchylka -> powiadomienie."""
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if d.get("last_eval") == today:
        return
    d["last_eval"] = today
    for k in sorted(d["days"])[:-BR_KEEP_DAYS]:
        d["days"].pop(k, None)
    r = d.get("ref")
    y = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
    if not r or r.get("pending") or y not in d["days"]:
        _br_save(force=True)
        return
    c = _br_compare(r, d["days"][y])
    if not c or c["min"] < BR_DAY_MIN:
        d["msg"] = f"{y}: za mało stabilnych minut przy ustawieniu wzorcowym – bez oceny"
        _br_save(force=True)
        return
    bad = max(abs(c["sup"]), abs(c["ext"]), abs(c["ratio"])) > BR_DEV
    d["dev_days"] = ([{"day": y, **c, "bad": bad}] + [x for x in d.get("dev_days", []) if x.get("day") != y])[:30]
    d["msg"] = (f"{y}: obroty nawiewu {c['sup']:+.1f} %, wywiewu {c['ext']:+.1f} %, stosunek {c['ratio']:+.1f} % względem wzorca"
                + (" – POWYŻEJ progu" if bad else " – w normie"))
    last = d["dev_days"][:BR_DAYS]
    if bad and len(last) == BR_DAYS and all(x["bad"] for x in last) and d.get("notify", True) and not d.get("alerted"):
        d["alerted"] = y
        side = "nawiewu" if abs(c["sup"]) >= abs(c["ext"]) else "wywiewu"
        txt = (f"Od {BR_DAYS} dni praca rekuperatora odbiega od bilansu wzorcowego ({r['supp_base']}/{r['extr_base']} %): "
               f"obroty nawiewu {c['sup']:+.1f} %, wywiewu {c['ext']:+.1f} %, stosunek {c['ratio']:+.1f} %. "
               f"Najbardziej zmienia się strona {side} – sprawdź filtry; ewentualnie popraw ustawienie o 1 %.")
        print(f"Vallox: {txt}")              # wersja publiczna: komunikat na stronie (bez powiadomien)
    elif not bad:
        d["alerted"] = None
    _br_save(force=True)


def balref_set(action, notify_on=None):
    """action: "save" - obecne ustawienie jako wzorzec (zbieranie danych od teraz), "clear" - usun wzorzec."""
    d = _br_load()
    if notify_on is not None:
        d["notify"] = bool(notify_on)
    if action == "save":
        with _lock:
            sb, eb = latest.get("supp_base"), latest.get("extr_base")
        if sb is None or eb is None:
            raise ValueError("brak odczytu ustawień wentylatorów")
        d["ref"] = {"ts": int(time.time()), "supp_base": int(sb), "extr_base": int(eb), "pending": True, "by_home": {}}
        d.update(dev_days=[], alerted=None, msg="zapisano wzorzec – zbieram dane (ok. 12 h stabilnej pracy)")
    elif action == "clear":
        d.update(ref=None, dev_days=[], alerted=None, msg="wzorzec usunięty")
    _br_save(force=True)
    return balref_status()


def balref_status():
    d = _br_load()
    r = d.get("ref")
    ref = None
    if r:
        ref = {k: r.get(k) for k in ("ts", "supp_base", "extr_base", "pending", "ready_ts")}
        ref["min"] = sum(v.get("n", 0) for v in (r.get("by_home") or {}).values())
        ref["need"] = BR_REF_MIN
        ref["by_home"] = {hs: _avg(b) for hs, b in sorted((r.get("by_home") or {}).items(), key=lambda x: int(x[0]))}
    today = time.strftime("%Y-%m-%d", time.localtime(time.time()))
    cmp_today = _br_compare(r, d["days"].get(today, {})) if r and not r.get("pending") else None
    sets = sorted(d["settings"].items(), key=lambda kv: -kv[1].get("last", 0))[:12]
    return {"ref": ref, "notify": d.get("notify", True), "msg": d.get("msg", ""), "dev_days": d.get("dev_days", [])[:10],
            "today": cmp_today, "dev_limit": BR_DEV, "dev_days_n": BR_DAYS, "current": _br["last_key"],
            "settings": [{"key": k, **_avg(v), "first": v.get("first"), "last": v.get("last")} for k, v in sets]}


def supply_status():
    a = _sp_load()
    t_ext, t_out, n = _sp_avgs()
    lc = _learn.get("data")
    return {**a, "avg_t_ext": None if t_ext is None else round(t_ext, 1), "avg_t_out": None if t_out is None else round(t_out, 1),
            "samples": n, "range": SUPPLY_RANGE,
            "learned": None if not lc else {"bins": lc.get("bins"), "learned": lc.get("learned"), "exit_t": lc.get("exit_t"),
                                            "hours": lc.get("hours"), "points": lc.get("points"),
                                            "obs": {T: {sp: v for sp, v in by.items()} for T, by in (lc.get("obs") or {}).items()}}}


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
    d["model"] = {"key": MODEL_INFO["key"], "setting": MODEL, "name": MODEL_INFO["name"], "detected": MODEL_INFO["detected"],
                  "source": MODEL_INFO["source"], "flow_max": FLOW_MAX,
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
    d["speed_ctl"] = speed_status()
    d["balref"] = balref_status()
    d["defrost_modes"] = DEFROST_MODES
    d["defrost_temp_range"] = DEFROST_TEMP_RANGE
    return d
