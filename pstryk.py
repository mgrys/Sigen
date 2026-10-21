"""
Pstryk - oficjalne API dla klientow Pstryka (TYLKO ODCZYT): licznik operatora (pobor / oddanie),
koszty godzinowe z rozbiciem i ceny godzinowe brutto.

Modul jest opcjonalny: dziala tylko, gdy podano klucz API (aplikacja Pstryk -> Integracje).
Bez klucza aplikacja dziala dokladnie jak wczesniej (ceny z PSE + taryfa).
Dane trafiaja do tabeli pstryk_hour (1 wiersz = 1 godzina, ts = poczatek godziny, unix UTC).
API ma limity zapytan - w normalnej pracy 1 zapytanie co 15 min.
"""
import datetime as dt
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

URL = "https://api.pstryk.pl/integrations/meter-data/unified-metrics/"
INTERVAL = 900          # s miedzy synchronizacjami
BACKFILL_DAYS = 35      # przy pierwszym uruchomieniu pobierz tyle dni wstecz
CHUNK_DAYS = 7          # dni na jedno zapytanie przy uzupelnianiu historii
RATE_PAUSE = 1800       # s przerwy po przekroczeniu limitu zapytan (HTTP 429)

METER = {"imp": "energy_active_import_register", "exp": "energy_active_export_register"}
COST = {"cost": "energy_import_cost", "sold": "energy_sold_value", "energy_net": "energy_cost_net",
        "dist_var": "var_dist_cost_net", "dist_fix": "fix_dist_cost_net", "service": "service_cost_net",
        "excise": "excise", "vat": "vat", "power_fee": "power_fee_cost_net"}
PRICE = {"price_gross": "price_gross", "price_pros": "price_prosumer_gross"}
VALUE_COLS = list(METER) + list(COST) + list(PRICE)
COLS = ["ts", "day"] + VALUE_COLS + ["updated"]
SUM_COLS = list(METER) + list(COST)

state = {"enabled": False, "ok": None, "last": None, "error": None, "backfill": None}
_lock = threading.Lock()


class PstrykError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def status():
    with _lock:
        return dict(state)


def enabled():
    with _lock:
        return state["enabled"]


def _set(**kw):
    with _lock:
        state.update(kw)


# --- API --------------------------------------------------------------------
def fetch(key, params):
    req = urllib.request.Request(f"{URL}?{urllib.parse.urlencode(params)}",
                                 headers={"Authorization": key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        msg = {401: "zły klucz API", 403: "brak dostępu (klucz)", 404: "zły klucz albo brak licznika / umowy",
               429: "przekroczony limit zapytań"}.get(e.code, f"HTTP {e.code}")
        raise PstrykError(msg, e.code) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PstrykError(f"brak połączenia z API Pstryk ({e})") from e


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def parse(payload):
    """Ramki godzinowe -> wiersze {kolumna: wartosc}."""
    now = int(time.time())
    out = []
    for fr in payload.get("frames") or []:
        if not isinstance(fr, dict) or "start" not in fr:
            continue
        m = fr.get("metrics") or {}
        mv, c, p = m.get("meter_values") or {}, m.get("cost") or {}, m.get("pricing") or {}
        ts = _ts(fr["start"])
        row = {"ts": ts, "day": time.strftime("%Y-%m-%d", time.localtime(ts)), "updated": now}
        row.update({k: _num(mv.get(f)) for k, f in METER.items()})
        row.update({k: _num(c.get(f)) for k, f in COST.items()})
        row.update({k: _num(p.get(f)) for k, f in PRICE.items()})
        out.append(row)
    return out


# --- baza ---------------------------------------------------------------------
def _q(c):
    return f'"{c}"'


def upsert(con, rows):
    """Zastapienie godzin nowszymi danymi (licznik operatora uzupelnia sie z opoznieniem)."""
    if not rows:
        return
    cols = ", ".join(_q(c) for c in COLS)
    marks = ", ".join("?" * len(COLS))
    for r in rows:
        con.execute("DELETE FROM pstryk_hour WHERE ts = ?", (r["ts"],))
        con.execute(f"INSERT INTO pstryk_hour ({cols}) VALUES ({marks})", [r.get(c) for c in COLS])
    con.commit()


def load(con, start, end):
    cols = ", ".join(_q(c) for c in COLS)
    rows = con.execute(f"SELECT {cols} FROM pstryk_hour WHERE ts >= ? AND ts < ? ORDER BY ts",
                       (start, end)).fetchall()
    return [dict(zip(COLS, r)) for r in rows]


def summary(rows):
    """Sumy z godzin, ktore maja juz dane licznika/kosztu."""
    done = [r for r in rows if r.get("imp") is not None or r.get("cost") is not None]
    s = {k: round(sum(r.get(k) or 0 for r in done), 4) for k in SUM_COLS}
    s["hours"] = len(done)
    s["last_ts"] = done[-1]["ts"] if done else None
    # oplaty stale (dystrybucja stala, oplata handlowa, oplata mocowa) - netto; brutto orientacyjnie z VAT 23 %
    s["fixed_net"] = round(s["dist_fix"] + s["service"] + s["power_fee"], 4)
    s["fixed_gross"] = round(s["fixed_net"] * 1.23, 2)
    s["balance"] = round(s["cost"] - s["sold"], 2)
    return s


def prices(rows, n=4):
    pts = [{"ts": r["ts"], "t": time.strftime("%H:00", time.localtime(r["ts"])), "price": r["price_gross"],
            "pros": r["price_pros"]} for r in rows if r.get("price_gross") is not None]
    cheap = sorted(pts, key=lambda p: p["price"])[:n]
    return {"points": pts, "cheapest": [{"hour": p["t"], "buy": round(p["price"], 4)}
                                        for p in sorted(cheap, key=lambda p: p["ts"])]}


# --- synchronizacja -----------------------------------------------------------
def _midnight(d):
    return int(time.mktime(d.timetuple()))


def sync_window(key, con, start, end):
    d = fetch(key, {"metrics": "meter_values,cost,pricing", "resolution": "hour",
                    "window_start": _iso(start), "window_end": _iso(end)})
    rows = parse(d)
    upsert(con, rows)
    return len(rows)


def backfill(key, con, days=BACKFILL_DAYS):
    today = dt.date.today()
    end = _midnight(today - dt.timedelta(days=1))
    start = _midnight(today - dt.timedelta(days=days))
    n = 0
    t = start
    while t < end:
        t2 = min(t + CHUNK_DAYS * 86400, end)
        n += sync_window(key, con, t, t2)
        _set(backfill=f"historia: {n} h")
        t = t2
        time.sleep(3)
    return n


def loop(get_key, db_connect):
    """get_key() -> klucz API lub '' (sprawdzany przy kazdej synchronizacji)."""
    backfilled = False
    while True:
        key = (get_key() or "").strip()
        _set(enabled=bool(key))
        if not key:
            time.sleep(300)
            continue
        pause = INTERVAL
        con = None
        try:
            con = db_connect()
            if not backfilled:
                r = con.execute("SELECT COUNT(*) FROM pstryk_hour WHERE ts < ?",
                                (int(time.time()) - 3 * 86400,)).fetchone()
                if not r or not r[0]:
                    backfill(key, con)
                backfilled = True
            today = dt.date.today()
            # wczoraj (licznik doplywa z opoznieniem) .. koniec jutra (ceny na jutro po ok. 14:00)
            n = sync_window(key, con, _midnight(today - dt.timedelta(days=1)), _midnight(today + dt.timedelta(days=2)))
            _set(ok=True, last=int(time.time()), error=None, hours=n)
        except PstrykError as e:
            _set(ok=False, error=str(e))
            if e.code == 429:
                pause = RATE_PAUSE
        except Exception as e:  # noqa: BLE001 - blad bazy/parsowania nie zatrzymuje aplikacji
            _set(ok=False, error=str(e))
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass
        time.sleep(pause)
