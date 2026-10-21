"""
Ceny energii: rynek dnia nastepnego z PSE (darmowe API, bez tokenu) + taryfa dystrybucyjna (G11/G12/G13...).

Zrodla:
  - PSE API v2: https://api.raporty.pse.pl/api/csdac-pln  (cena SDAC dla PL, PLN/MWh, co 15 min)
                https://api.raporty.pse.pl/api/rce-pln    (Rynkowa Cena Energii, PLN/MWh, co 15 min)
    Ceny na jutro publikowane ok. 13:40.
  - Taryfy URE: https://davo22.github.io/polish-electricity-tariffs/data/<operator>.json
    (cache w folderze danych; gdy brak sieci - wartosci wbudowane Tauron G13 2026)

Konfiguracja wzoru w config.ini, sekcja [ceny].
"""
import datetime as dt
import json
import re
import threading
import time
import urllib.parse
import urllib.request

import config

PSE_URL = "https://api.raporty.pse.pl/api/{ep}?$filter="
OPERATOR = config.get("ceny", "operator", "tauron").lower()
TARIFF_URL = f"https://davo22.github.io/polish-electricity-tariffs/data/{OPERATOR}.json"
TARIFF_CACHE = config.DATA_DIR / f"tariff_cache_{OPERATOR}.json"
TARIFF_MAX_AGE = 7 * 86400

# --- konfiguracja wzoru (config.ini, sekcja [ceny]) ------------------------
C = dict(config.CFG.items("ceny")) if config.CFG.has_section("ceny") else {}


def _f(key, default):
    try:
        return float(str(C.get(key, default)).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _b(key, default):
    v = str(C.get(key, default)).strip().lower()
    return v in ("1", "yes", "tak", "true", "on")


TARYFA = str(C.get("taryfa", "G11")).strip()
VAT = _f("vat", 0.23)
MARZA = _f("marza_zl_kwh", 0.0732)            # marza sprzedawcy netto, zl/kWh
AKCYZA = _f("akcyza_zl_kwh", 0.005)            # akcyza, zl/kWh (VAT liczony takze od niej)
ZAKUP_BAZA = str(C.get("zakup_baza", "rce")).strip()       # rce | csdac
SPRZEDAZ_BAZA = str(C.get("sprzedaz_baza", "rce")).strip()  # rce | csdac

# Wartosci awaryjne (Tauron 2026, netto zl/kWh) - gdy nie da sie pobrac taryf
FALLBACK = {
    "zones": {
        "morning_peak": {"hours": "lato 07:00-13:00; zima 07:00-13:00"},
        "afternoon_peak": {"hours": "lato 19:00-22:00; zima 16:00-21:00"},
        "off_peak": {"hours": "pozostałe godziny + całe dni wolne"},
    },
    "dist": {"morning_peak": 0.2203, "afternoon_peak": 0.3898, "off_peak": 0.0392},
    "system": {"quality_net": 0.0331, "oze_net": 0.0073, "cogeneration_net": 0.003},
    "source": "wbudowane (Tauron 2026)",
}
ZONE_LABEL = {"morning_peak": "szczyt przedpołudniowy", "afternoon_peak": "szczyt popołudniowy",
              "off_peak": "pozostałe"}

state = {"updated": None, "error": None, "tariff_source": None}
_lock = threading.Lock()


# --- swieta (strefa "pozostale" przez cala dobe) --------------------------
def _easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = (h + l_ - 7 * m + 114) % 31 + 1
    return dt.date(y, month, day)


def is_free_day(d):
    if d.weekday() >= 5:
        return True
    fixed = {(1, 1), (1, 6), (5, 1), (5, 3), (8, 15), (11, 1), (11, 11), (12, 24), (12, 25), (12, 26)}
    if (d.month, d.day) in fixed:
        return True
    e = _easter(d.year)
    return d in (e + dt.timedelta(days=1), e + dt.timedelta(days=60))   # Pon. Wielkanocny, Boze Cialo


# --- taryfa ---------------------------------------------------------------
def _http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sigen-pv-monitor"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _parse_hours(txt):
    """'lato 07:00-13:00; zima 16:00-21:00' -> {'lato': [(7,13)], 'zima': [(16,21)]}.
    Godziny bez pory roku ('06:00-13:00, 15:00-22:00') obowiazuja caly rok; zakres przez polnoc (22-06) dozwolony."""
    out = {}
    rng = lambda t: [(int(a), int(b)) for a, b in re.findall(r"(\d{1,2}):\d{2}\s*-\s*(\d{1,2}):\d{2}", t)]  # noqa: E731
    found = False
    for part in txt.split(";"):
        m = re.match(r"\s*(lato|zima)\s+(.*)", part)
        if m:
            out[m.group(1)] = rng(m.group(2))
            found = True
    if not found:
        r = rng(txt)
        if r:
            out = {"lato": r, "zima": r}
    return out


def _in_range(h, a, b):
    return a <= h < b if a < b else (h >= a or h < b)


def _default_zone(tariff):
    """Strefa 'pozostala': off_peak (G13), strefa bez godzin (G11 'all'), inaczej pierwsza."""
    zones = tariff.get("zones", {})
    if "off_peak" in zones:
        return "off_peak"
    for name, z in zones.items():
        if not _parse_hours(z.get("hours", "")):
            return name
    return next(iter(zones), "off_peak")


def load_tariff():
    """Taryfa z cache/sieci; zwraca {'zones':..., 'dist':..., 'system':..., 'source':...}."""
    raw = None
    if TARIFF_CACHE.exists() and time.time() - TARIFF_CACHE.stat().st_mtime < TARIFF_MAX_AGE:
        try:
            raw = json.loads(TARIFF_CACHE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            raw = None
    if raw is None:
        try:
            raw = _http_json(TARIFF_URL)
            TARIFF_CACHE.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            if TARIFF_CACHE.exists():
                raw = json.loads(TARIFF_CACHE.read_text(encoding="utf-8"))
    if raw is None:
        return FALLBACK
    try:
        today = dt.date.today().isoformat()
        tariffs = raw["tariffs"]
        t = tariffs[TARYFA] if isinstance(tariffs, dict) else next(x for x in tariffs if x.get("group") == TARYFA)

        def current(periods):
            for p in periods:
                if p["valid_from"] <= today and (p.get("valid_to") is None or today <= p["valid_to"]):
                    return p
            return periods[0]

        per = current(t["periods"])
        sysc = current(raw["system_components"])
        return {"zones": t["zones"], "dist": per["distribution_variable_net"],
                "system": {k: sysc.get(k, 0) for k in ("quality_net", "oze_net", "cogeneration_net")},
                "source": f"{raw.get('source', 'taryfy URE')} ({per['valid_from']})"}
    except Exception:  # noqa: BLE001
        return FALLBACK


def zone_for(tariff, local_dt):
    """Strefa G13 dla czasu lokalnego (poczatek kwadransa)."""
    if is_free_day(local_dt.date()) and "off_peak" in tariff.get("zones", {}):
        return "off_peak"
    season = "lato" if 4 <= local_dt.month <= 9 else "zima"
    h = local_dt.hour
    for zone, z in tariff["zones"].items():
        for a, b in _parse_hours(z.get("hours", "")).get(season, []):
            if _in_range(h, a, b):
                return zone
    return _default_zone(tariff)


def price_parts(tariff, base_buy_mwh, base_sell_mwh, zone):
    """Cena zakupu i sprzedazy w zl/kWh, rozbicie jak w aplikacji Pstryk.

    zakup = (cena rynkowa + marza + akcyza + dystrybucja netto) x (1 + VAT)
      obsluga = cena rynkowa + marza            ("cena pradu z obsluga", netto)
      dist    = dystrybucja + jakosciowa + OZE + kogeneracyjna (netto)
      podatki = akcyza + VAT od calosci
    """
    s = tariff["system"]
    dist = tariff["dist"].get(zone, 0) + s["quality_net"] + s["oze_net"] + s["cogeneration_net"]
    obsluga = base_buy_mwh / 1000 + MARZA
    buy = (obsluga + AKCYZA + dist) * (1 + VAT)
    return {"obsluga": round(obsluga, 5), "dist": round(dist, 5), "podatki": round(buy - obsluga - dist, 5),
            "buy": round(buy, 5), "sell": round(base_sell_mwh / 1000, 5)}


# --- PSE --------------------------------------------------------------------
def fetch_day(day):
    """{ts_utc_start: (csdac, rce)} dla doby handlowej day (YYYY-MM-DD)."""
    out = {}
    for ep, field in (("csdac-pln", "csdac_pln"), ("rce-pln", "rce_pln")):
        data = _http_json(PSE_URL.format(ep=ep) + urllib.parse.quote(f"business_date eq '{day}'"))
        for v in data.get("value", []):
            end = dt.datetime.strptime(v["dtime_utc"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
            ts = int(end.timestamp()) - 900
            row = out.setdefault(ts, [None, None])
            row[0 if ep == "csdac-pln" else 1] = v[field]
    return out


def save_day(con, day, rows):
    cur = con.cursor()
    cur.execute('DELETE FROM prices WHERE "day" = ?', (day,))
    cur.executemany('INSERT INTO prices (ts, "day", csdac, rce) VALUES (?, ?, ?, ?)',
                    [(ts, day, c, r) for ts, (c, r) in sorted(rows.items())])
    con.commit()


def have_day(con, day):
    r = con.execute('SELECT COUNT(*) FROM prices WHERE "day" = ?', (day,)).fetchone()
    return (r[0] or 0) >= 92   # 92-100 kwadransow (zmiana czasu)


def loop(db_connect):
    con = None
    while True:
        try:
            if con is None:
                con = db_connect()
            today = dt.date.today()
            todo = [today.isoformat()]
            if dt.datetime.now().time() >= dt.time(13, 45):
                todo.append((today + dt.timedelta(days=1)).isoformat())
            for day in todo:
                if not have_day(con, day):
                    rows = fetch_day(day)
                    if rows:
                        save_day(con, day, rows)
            tariff = load_tariff()
            with _lock:
                state.update(updated=int(time.time()), error=None, tariff_source=tariff["source"])
            time.sleep(900)
        except Exception as e:  # noqa: BLE001
            with _lock:
                state["error"] = str(e)
            try:
                if con:
                    con.close()
            except Exception:  # noqa: BLE001
                pass
            con = None
            time.sleep(600)


def series(con, day):
    """Ceny kwadransowe dla doby (czas lokalny)."""
    tariff = load_tariff()
    rows = con.execute('SELECT ts, csdac, rce FROM prices WHERE "day" = ? ORDER BY ts', (day,)).fetchall()
    out = []
    for ts, csdac, rce in rows:
        if csdac is None and rce is None:
            continue
        local = dt.datetime.fromtimestamp(ts)
        zone = zone_for(tariff, local)
        bb = (csdac if ZAKUP_BAZA == "csdac" else rce) or 0
        bs = (rce if SPRZEDAZ_BAZA == "rce" else csdac) or 0
        p = price_parts(tariff, bb, bs, zone)
        out.append({"ts": ts, "t": local.strftime("%H:%M"), "zone": ZONE_LABEL.get(zone, zone),
                    "csdac": csdac, "rce": rce, **p})
    return out, tariff


def cheapest_hours(points, n=4):
    """Najtansze godziny (srednia z kwadransow) wg ceny zakupu."""
    hours = {}
    for p in points:
        hours.setdefault(p["t"][:2], []).append(p["buy"])
    avg = sorted(((sum(v) / len(v), h) for h, v in hours.items() if len(v) >= 2))
    return [{"hour": f"{h}:00", "buy": round(a, 4)} for a, h in avg[:n]]


def status():
    with _lock:
        return dict(state)


def formula_desc():
    return {
        "zakup": f"({'SDAC' if ZAKUP_BAZA == 'csdac' else 'RCE'} + marża {MARZA:.4f} + akcyza {AKCYZA:.3f}"
                 f" + dystrybucja {TARYFA} z opłatami) × {1 + VAT:.2f}",
        "sprzedaz": "RCE (netto)" if SPRZEDAZ_BAZA == "rce" else "SDAC (netto)",
        "vat": VAT,
    }
