"""
Miernik poboru pradu rekuperatora (opcjonalnie) - TYLKO ODCZYT, nic nie jest przelaczane.

Do czego: zuzycie i koszt pradu rekuperatora, wykrywanie pracy nagrzewnicy (kafle w zakladce Rekuperacja,
automat nastawy nawiewu w mrozie). Bez miernika zakladka Rekuperacja dziala, tylko bez tych informacji.

Ustawienia ([rekuperacja] w config.ini albo na stronie):
  miernik    = shelly   (puste = brak miernika)
  miernik_ip = adres IP gniazdka

Obslugiwane: Shelly Gen1 (Plug S, 1PM: GET /status) i Shelly Gen2/Plus/Pro (GET /rpc/Switch.GetStatus?id=0) -
rodzaj rozpoznawany automatycznie.

INNY MIERNIK (np. gniazdko Tuya, Tasmota, Home Assistant): dopisz funkcje read_<nazwa>(host) zwracajaca
    {"power": moc chwilowa [W], "total_wh": licznik energii narastajaco [Wh] albo None}
i dodaj ja do READERS. Licznik moze sie zerowac (np. po restarcie gniazdka) - przyrost liczony jest odpornie.
Gdy miernik nie ma licznika energii (total_wh = None), energia liczona jest z mocy x czas.
Przyklad dla Tuya (biblioteka tinytuya, klucz lokalny z `python -m tinytuya wizard`):
    def read_tuya(host):
        import tinytuya
        d = tinytuya.OutletDevice(DEVICE_ID, host, LOCAL_KEY); d.set_version(3.3)
        dps = d.status()["dps"]            # zwykle: "19" moc [0,1 W], "17" energia dodana [0,01 kWh]
        return {"power": dps.get("19", 0) / 10, "total_wh": None}
    READERS["tuya"] = read_tuya
"""
import json
import threading
import time
import urllib.request

import config

KIND = config.get("rekuperacja", "miernik").lower()
HOST = config.get("rekuperacja", "miernik_ip")
INTERVAL = 60          # s
NAME = "Rekuperator"
COLUMNS = ["power", "total_wh", "energy_wh"]

_gen = {}              # host -> 1 / 2 (rozpoznana generacja Shelly)
_lock = threading.Lock()


def _get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def read_shelly(host):
    if _gen.get(host) != 1:
        try:
            st = _get(f"http://{host}/rpc/Switch.GetStatus?id=0")
            _gen[host] = 2
            ae = st.get("aenergy") or {}
            return {"power": st.get("apower"), "total_wh": ae.get("total"), "temp": (st.get("temperature") or {}).get("tC")}
        except Exception:  # noqa: BLE001
            if _gen.get(host) == 2:
                raise
    st = _get(f"http://{host}/status")
    _gen[host] = 1
    m = (st.get("meters") or [{}])[0]
    tot = m.get("total")                           # Gen1: watominuty
    return {"power": m.get("power"), "total_wh": None if tot is None else tot / 60.0, "temp": st.get("temperature")}


READERS = {"shelly": read_shelly}
ENABLED = bool(KIND and HOST and KIND in READERS)
latest = {"ok": False, "error": "Uruchamianie..." if ENABLED else "brak miernika (Ustawienia → Rekuperacja)"}


def energy_delta(prev, cur, power, dt_s):
    """Przyrost energii [Wh]: z licznika (odporny na wyzerowanie), a bez licznika - moc x czas."""
    if cur is None:
        return round(power * dt_s / 3600, 3) if power is not None and 0 < dt_s <= 300 else None
    if prev is None:
        return 0.0
    return round(cur - prev if cur >= prev else cur, 3)


def loop(db_connect, db_mod):
    if not ENABLED:
        return
    read = READERS[KIND]
    con, prev, prev_t = None, None, None
    try:                                   # kontynuacja licznika po restarcie aplikacji
        c = db_connect()
        try:
            prev = db_mod.last_meter_total(c)
        finally:
            c.close()
    except Exception:  # noqa: BLE001
        prev = None
    while True:
        t0 = time.time()
        try:
            v = read(HOST)
            v["energy_wh"] = energy_delta(prev, v.get("total_wh"), v.get("power"), t0 - prev_t if prev_t else 0)
            if v.get("total_wh") is not None:
                prev = v["total_wh"]
            prev_t = t0
            ts = int(t0)
            with _lock:
                latest.clear()
                latest.update(v, ts=ts, ok=True)
            try:
                if con is None:
                    con = db_connect()
                db_mod.insert_meter(con, ts, [v.get(c) for c in COLUMNS])
            except Exception as e:  # noqa: BLE001
                with _lock:
                    latest["db_error"] = str(e)
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass
                con = None
        except Exception as e:  # noqa: BLE001 - brak polaczenia z miernikiem
            with _lock:
                latest.update(ok=False, error=f"{type(e).__name__}: {e}")
        time.sleep(max(5, INTERVAL - (time.time() - t0)))


def status():
    with _lock:
        d = dict(latest)
    d.update(host=HOST, name=NAME, kind=KIND or None, enabled=ENABLED)
    return d
