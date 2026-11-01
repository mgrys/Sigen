"""
Pradcast (pradcast.pl) - prognoza godzinowych cen energii RDN (Fixing I) na dzis i 1-3 dni do przodu.
TYLKO ODCZYT. Opcjonalnie: dziala, gdy w Ustawieniach (config.ini [pradcast] api_key) jest klucz API -
darmowy, anonimowy, do wygenerowania na pradcast.pl (dokumentacja API).

Uzycie: strategia osobista planuje dalej niz ceny opublikowane przez TGE/PSE/Pstryka (do pojutrza i dalej)
- godziny bez cen rzeczywistych biora cene z prognozy (oznaczone jako szacowane).
API: https://api.pradcast.pl, naglowek X-API-Key; GET /prices/date/{YYYY-MM-DD} -> {"prices": [{"hour", "price" (zl/MWh),
"price_kwh", "p10", "p90", ...}], "source", "horizon", "confidence"}. Limit zapytan wysoki - tu najwyzej 1 zapytanie
na dzien co CACHE_S.
"""
import datetime as dt
import json
import threading
import time
import urllib.error
import urllib.request

import config

URL = "https://api.pradcast.pl"
CACHE_S = 3 * 3600          # s - prognoza na dany dzien odswiezana co 3 h
DAYS_AHEAD = 3              # D+1 .. D+3

HIST = config.DATA_DIR / "pradcast_hist.json"   # zapamietane prognozy (dzien -> horyzont -> godzina -> cena) do oceny trafnosci
HIST_DAYS = 60

_lock = threading.Lock()
_cache = {}                 # data ISO -> (ts pobrania, odpowiedz)
state = {"enabled": False, "ok": None, "error": None, "last": None}


def api_key():
    return config.get("pradcast", "api_key")


def enabled():
    return bool(api_key())


def _get(path, key, timeout=20):
    req = urllib.request.Request(URL + path, headers={"X-API-Key": key, "Accept": "application/json",
                                                       "User-Agent": "SigenPVMonitor"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def day(d):
    """Prognoza / ceny na dzien d: {"hours": {godzina: {"price_kwh", "p10", "p90"}}, "source", "horizon", ...} albo None."""
    key = api_key()
    state["enabled"] = bool(key)
    if not key:
        return None
    iso = d.isoformat() if isinstance(d, dt.date) else str(d)
    now = time.time()
    with _lock:
        hit = _cache.get(iso)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
    try:
        r = _get(f"/prices/date/{iso}", key)
    except urllib.error.HTTPError as e:
        state.update(ok=False, error={401: "zły klucz API", 403: "brak dostępu (klucz)", 404: "brak prognozy na ten dzień",
                                      429: "przekroczony limit zapytań"}.get(e.code, f"HTTP {e.code}"))
        return hit[1] if hit else None
    except Exception as e:  # noqa: BLE001 - brak sieci itp.
        state.update(ok=False, error=f"{type(e).__name__}: {e}")
        return hit[1] if hit else None
    hours = {}
    unit_mwh = str(r.get("unit") or "MWh").lower() == "mwh"
    for p in r.get("prices") or []:
        h = p.get("hour")
        if not isinstance(h, int) or not 0 <= h <= 23:
            continue
        kwh = p.get("price_kwh")
        if kwh is None and p.get("price") is not None:
            kwh = p["price"] / 1000 if unit_mwh else p["price"]
        if kwh is None:
            continue
        conv = (lambda v: None if v is None else (v / 1000 if unit_mwh else v))
        hours[h] = {"price_kwh": float(kwh), "p10": conv(p.get("p10")), "p90": conv(p.get("p90"))}
    out = {"date": iso, "hours": hours, "source": r.get("source"), "horizon": r.get("horizon"),
           "confidence": r.get("confidence"), "fetched": int(now)}
    with _lock:
        _cache[iso] = (now, out)
    state.update(ok=True, error=None, last=int(now))
    try:
        _remember(iso, hours)
    except Exception:  # noqa: BLE001 - historia tylko do oceny trafnosci
        pass
    return out


def _hist_load():
    try:
        return json.loads(HIST.read_text(encoding="utf-8")) if HIST.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def _remember(iso, hours):
    """Pierwsza prognoza na dany dzien z kazdego horyzontu (D+1, D+2, D+3) - do porownania z cena rzeczywista."""
    h = (dt.date.fromisoformat(iso) - dt.date.today()).days
    if h < 1 or not hours:
        return
    with _lock:
        d = _hist_load()
        day = d.setdefault(iso, {})
        if f"D+{h}" in day:
            return
        day[f"D+{h}"] = {str(k): round(v["price_kwh"], 5) for k, v in hours.items()}
        cut = (dt.date.today() - dt.timedelta(days=HIST_DAYS)).isoformat()
        d = {k: v for k, v in d.items() if k >= cut}
        HIST.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def evaluate(actual_fn, days=30):
    """Trafnosc prognoz: {"D+1": {"days", "mae", "mape"}, ...}. actual_fn(data_iso) -> {godzina: cena rzeczywista zl/kWh}."""
    hist = _hist_load()
    cut = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    acc = {}
    for iso, hz in sorted(hist.items()):
        if iso < cut or iso >= dt.date.today().isoformat():
            continue
        act = actual_fn(iso) or {}
        if len(act) < 20:
            continue
        for hname, fc in hz.items():
            errs = [(fc[str(h)], a) for h, a in act.items() if str(h) in fc]
            if not errs:
                continue
            a = acc.setdefault(hname, {"n": 0, "abs": 0.0, "rel": 0.0, "nrel": 0, "days": set()})
            for f, r in errs:
                a["n"] += 1
                a["abs"] += abs(f - r)
                if abs(r) >= 0.05:              # blad wzgledny tylko przy cenie >= 5 gr (ceny bliskie 0 / ujemne)
                    a["rel"] += abs(f - r) / abs(r)
                    a["nrel"] += 1
            a["days"].add(iso)
    return {k: {"days": len(v["days"]), "mae": round(v["abs"] / v["n"], 4),
                "mape": round(100 * v["rel"] / v["nrel"], 1) if v["nrel"] else None}
            for k, v in sorted(acc.items())}


def status():
    return dict(state, enabled=enabled())
