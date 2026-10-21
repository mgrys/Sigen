"""
Prognoza produkcji PV z dwoch darmowych zrodel, dzis + jutro:

  - open_meteo     - Open-Meteo, natezenie promieniowania na plaszczyzne polaci
                     (global_tilted_irradiance) co 15 min; moc = GTI/1000 * kWp * PR
  - forecast_solar - forecast.solar (limit 12 zapytan/h na IP)

Kazda polac pobierana osobno i sumowana. Wyniki w bazie (forecast_src, forecast_src_day),
wiec restart nie zuzywa limitow. Prognoza "z dnia poprzedniego" (ostatnia pobrana przed
polnoca) zapisywana osobno - na niej liczona jest trafnosc (forecast_accuracy).
"""
import datetime as dt
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

import config

# ---------------- KONFIGURACJA (config.ini: [instalacja], [polacie], [prognoza]) ----------------
LAT = config.getf("instalacja", "lat", 52.23)
LON = config.getf("instalacja", "lon", 21.01)
TZ = config.get("instalacja", "strefa_czasowa", "Europe/Warsaw")

# azymut: 0 = poludnie, -90 = wschod, 90 = zachod; nachylenie w stopniach; moc w kWp
POLACIE = config.planes()

# Zrodlo w kafelkach: open_meteo (najlepsze dopasowanie Open-Meteo), om_srednia (srednia modeli),
# om_ecmwf, om_icon_eu, om_ukmo, om_gfs, om_meteofrance albo forecast_solar
GLOWNE_ZRODLO = config.get("prognoza", "glowne_zrodlo", "open_meteo")
FORECAST_SOLAR = config.getb("prognoza", "forecast_solar", True)

# Modele pobierane z Open-Meteo jednym zapytaniem na polac: zrodlo -> nazwa modelu w API
OM_MODELS = {
    "open_meteo": "best_match",          # automatyczny wybor Open-Meteo
    "om_ecmwf": "ecmwf_ifs025",          # ECMWF IFS (Europa, 0,25°)
    "om_icon_eu": "icon_eu",             # DWD ICON-EU (7 km)
    "om_ukmo": "ukmo_seamless",          # UK Met Office UM
    "om_gfs": "gfs_seamless",            # NOAA GFS
    "om_meteofrance": "meteofrance_seamless",
}

# Wspolczynnik korekty dla kazdego zrodla - tabela trafnosci na stronie podpowiada wartosc
KOREKTA = config.corrections()

PR = config.getf("instalacja", "pr", 0.85)                        # wspolczynnik wydajnosci (Open-Meteo)
MOC_FALOWNIKA = config.getf("instalacja", "moc_falownika_kw", 10.0)  # kW - gorne ograniczenie prognozy

ODSWIEZANIE = 3600   # s - kazde zrodlo co godzine (forecast.solar: 3 zapytania/h przy limicie 12/h)
# ---------------------------------------------------------

SOURCES = {
    "open_meteo": "Open-Meteo", "om_srednia": "Średnia modeli", "om_ecmwf": "ECMWF",
    "om_icon_eu": "ICON-EU", "om_ukmo": "UK Met Office", "om_gfs": "GFS",
    "om_meteofrance": "Météo-France", "forecast_solar": "forecast.solar",
}
MODEL_SOURCES = [s for s in OM_MODELS if s != "open_meteo"]   # skladniki sredniej
GROUPS = {"open_meteo": "Open-Meteo", "forecast_solar": "forecast.solar"}   # grupy pobierania
FS_API = "https://api.forecast.solar/estimate/{lat}/{lon}/{dec}/{az}/{kwp}"
OM_API = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
          "&minutely_15=global_tilted_irradiance&tilt={dec}&azimuth={az}&models={models}"
          "&timezone=" + urllib.parse.quote(TZ) + "&forecast_days=2")

state = {g: {"updated": None, "error": None} for g in GROUPS}
_lock = threading.Lock()


def _get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sigen-dashboard"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


# --- pobieranie -----------------------------------------------------------
def fetch_forecast_solar():
    """{dzien: {minuta: W}}, {dzien: {polac: Wh}}"""
    moc = defaultdict(lambda: defaultdict(float))
    per_plane = defaultdict(dict)
    for i, p in enumerate(POLACIE):
        if i:
            time.sleep(1)
        res = _get_json(FS_API.format(lat=LAT, lon=LON, dec=p["nachylenie"], az=p["azymut"], kwp=p["kwp"]))["result"]
        for ts, w in res["watts"].items():
            d, t = ts.split(" ")
            h, m, s = (int(x) for x in t.split(":"))
            moc[d][h * 60 + m + s / 60] += w
        for d, wh in res.get("watt_hours_day", {}).items():
            per_plane[d][p["nazwa"]] = wh
    return moc, per_plane


def fetch_open_meteo():
    """Wszystkie modele Open-Meteo: {zrodlo: (moc {dzien: {minuta: W}}, polacie {dzien: {polac: Wh}})}.

    GTI co 15 min to srednia z poprzedzajacego kwadransa -> punkt w srodku kwadransa.
    """
    models = ",".join(OM_MODELS.values())
    raw = {src: (defaultdict(lambda: defaultdict(float)), defaultdict(lambda: defaultdict(float)))
           for src in OM_MODELS}
    for p in POLACIE:
        j = _get_json(OM_API.format(lat=LAT, lon=LON, dec=p["nachylenie"], az=p["azymut"], models=models))
        m15 = j["minutely_15"]
        for src, model in OM_MODELS.items():
            vals = m15.get(f"global_tilted_irradiance_{model}") or (
                m15.get("global_tilted_irradiance") if len(OM_MODELS) == 1 else None)
            if not vals:
                continue
            moc, pp = raw[src]
            for t, g in zip(m15["time"], vals):
                if g is None:
                    continue
                d, hm = t.split("T")
                h, mi = (int(x) for x in hm.split(":"))
                minute = h * 60 + mi - 7.5
                if minute < 0:   # kwadrans konczacy sie o 00:00 nalezy do poprzedniej doby
                    continue
                w = max(g, 0) * p["kwp"] * PR     # W (GTI W/m2 / 1000 * kWp * 1000)
                moc[d][minute] += w
                pp[d][p["nazwa"]] += w * 0.25
    out = {}
    cap = MOC_FALOWNIKA * 1000
    for src, (moc, pp) in raw.items():
        if not moc:
            continue
        for d in moc:
            for mi, w in moc[d].items():        # ograniczenie moca falownika
                if w > cap:
                    moc[d][mi] = cap
            total = sum(moc[d].values()) * 0.25  # sumy polaci przeskalowane po ograniczeniu
            rawsum = sum(pp[d].values())
            if rawsum > 0:
                for name in pp[d]:
                    pp[d][name] *= total / rawsum
        out[src] = (moc, pp)
    # srednia modeli (bez best_match, ktory sam jest jednym z nich)
    parts = [out[s] for s in MODEL_SOURCES if s in out]
    if parts:
        moc_m = defaultdict(lambda: defaultdict(float))
        pp_m = defaultdict(lambda: defaultdict(float))
        for moc, pp in parts:
            for d in moc:
                for mi, w in moc[d].items():
                    moc_m[d][mi] += w / len(parts)
                for name, wh in pp[d].items():
                    pp_m[d][name] += wh / len(parts)
        out["om_srednia"] = (moc_m, pp_m)
    return out


def fetch_forecast_solar_group():
    return {"forecast_solar": fetch_forecast_solar()}


FETCHERS = {"open_meteo": fetch_open_meteo, "forecast_solar": fetch_forecast_solar_group}


# --- zapis / odczyt -------------------------------------------------------
def save(con, source, moc, per_plane):
    """Zapis prognozy zrodla. Dla jutra i dalszych dni zapamietuje tez prognoze "dzien wczesniej"."""
    now = int(time.time())
    today = dt.date.today().isoformat()
    k = KOREKTA.get(source, 1.0) if isinstance(KOREKTA, dict) else 1.0
    cur = con.cursor()
    for d, pts in moc.items():
        planes = {n: wh * k for n, wh in per_plane.get(d, {}).items()}
        total = sum(planes.values())
        cur.execute('DELETE FROM forecast_src WHERE "source" = ? AND "day" = ?', (source, d))
        cur.executemany('INSERT INTO forecast_src ("source", "day", "minute", watts) VALUES (?, ?, ?, ?)',
                        [(source, d, float(m), float(w * k)) for m, w in sorted(pts.items())])
        old = cur.execute('SELECT day_ahead_wh, day_ahead_fetched, day_ahead_planes FROM forecast_src_day '
                          'WHERE "source" = ? AND "day" = ?', (source, d)).fetchone()
        da_wh, da_ts = (old[0], old[1]) if old else (None, None)
        da_planes = old[2] if old else None
        if d > today:                       # prognoza z wyprzedzeniem - nadpisujemy do polnocy, potem zamrozona
            da_wh, da_ts, da_planes = total, now, json.dumps(planes, ensure_ascii=False)
            cur.execute('DELETE FROM forecast_src_da WHERE "source" = ? AND "day" = ?', (source, d))
            cur.executemany('INSERT INTO forecast_src_da ("source", "day", "minute", watts) VALUES (?, ?, ?, ?)',
                            [(source, d, float(m), float(w * k)) for m, w in sorted(pts.items())])
        cur.execute('DELETE FROM forecast_src_day WHERE "source" = ? AND "day" = ?', (source, d))
        cur.execute('INSERT INTO forecast_src_day ("source", "day", total_wh, planes, fetched, '
                    'day_ahead_wh, day_ahead_fetched, day_ahead_planes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (source, d, float(total), json.dumps(planes, ensure_ascii=False), now, da_wh, da_ts, da_planes))
    con.commit()


def last_fetch(con, source):
    r = con.execute('SELECT MAX(fetched) FROM forecast_src_day WHERE "source" = ?', (source,)).fetchone()
    return r[0] if r and r[0] else None


GROUP_SOURCES = {"open_meteo": list(OM_MODELS), "forecast_solar": ["forecast_solar"]}
if not FORECAST_SOLAR:                       # forecast.solar wylaczone w config.ini
    for _d in (FETCHERS, GROUP_SOURCES, GROUPS, state, SOURCES):
        _d.pop("forecast_solar", None)
if GLOWNE_ZRODLO not in SOURCES:
    GLOWNE_ZRODLO = "open_meteo"


def last_fetch_group(con, grp):
    """Najstarsze z ostatnich pobran zrodel grupy; brak ktoregokolwiek zrodla -> None (pobierz od razu)."""
    ts = [last_fetch(con, s) for s in GROUP_SOURCES.get(grp, [grp])]
    return None if any(t is None for t in ts) else min(ts)


def load_day(con, day, source=None, latest=False):
    """Prognoza dnia z bazy albo None.

    Dla dzisiaj i dni minionych zwracana jest prognoza "z dnia poprzedniego" (ostatnia pobrana przed polnoca),
    bo z nia porownujemy rzeczywista produkcje; biezaca (aktualizowana w ciagu dnia) jest w polach latest_*.
    Brak prognozy z dnia poprzedniego (np. aplikacja dzialala dopiero od rana) - zwracana biezaca (kind="biezaca").
    Dla jutra i dalej - prognoza aktualizowana do polnocy (kind="do_polnocy"). latest=True wymusza biezaca.
    """
    source = source or GLOWNE_ZRODLO
    meta = con.execute('SELECT total_wh, planes, fetched, day_ahead_wh, day_ahead_fetched, day_ahead_planes '
                       'FROM forecast_src_day WHERE "source" = ? AND "day" = ?', (source, day)).fetchone()
    if not meta:
        return None
    total_wh, planes_js, fetched, da_wh, da_ts, da_planes = meta
    today = dt.date.today().isoformat()
    use_da = not latest and day <= today and da_wh is not None
    pts = []
    if use_da:
        pts = con.execute('SELECT "minute", watts FROM forecast_src_da WHERE "source" = ? AND "day" = ? '
                          'ORDER BY "minute"', (source, day)).fetchall()
        if not pts:                          # starsze dane bez zapisanego przebiegu z dnia poprzedniego
            use_da = False
    if not use_da:
        pts = con.execute('SELECT "minute", watts FROM forecast_src WHERE "source" = ? AND "day" = ? '
                          'ORDER BY "minute"', (source, day)).fetchall()
    nz = [p for p in pts if p[1] > 1]
    peak = max(pts, key=lambda x: x[1]) if pts else (None, 0)
    planes = json.loads((da_planes if use_da else planes_js) or "{}")
    return {
        "source": source, "source_name": SOURCES.get(source, source),
        "date": day,
        "kind": "dzien_wczesniej" if use_da else ("do_polnocy" if day > today else "biezaca"),
        "fetched": da_ts if use_da else fetched,
        "total_kwh": round(((da_wh if use_da else total_wh) or 0) / 1000, 2),
        "day_ahead_kwh": round(da_wh / 1000, 2) if da_wh is not None else None,
        "latest_kwh": round((total_wh or 0) / 1000, 2),
        "latest_fetched": fetched,
        "planes_kwh": {k: round(v / 1000, 2) for k, v in planes.items()},
        "sunrise": (nz[0][0] - 15) if nz else None,
        "sunset": (nz[-1][0] + 15) if nz else None,
        "peak_kw": round(peak[1] / 1000, 2),
        "peak_min": peak[0],
        "points": [[round(m, 1), round(w / 1000, 3)] for m, w in pts],
    }


def sun_times(day, lat=None, lon=None):
    """Wschod i zachod slonca (algorytm NOAA) jako minuty doby czasu lokalnego komputera.

    day: datetime.date. Zwraca (wschod, zachod) albo (None, None) w dzien/noc polarna.
    """
    import math
    lat = LAT if lat is None else lat
    lon = LON if lon is None else lon
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    la = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(la) * math.cos(decl))) - math.tan(la) * math.tan(decl)
    if not -1 <= cos_ha <= 1:
        return None, None
    ha = math.degrees(math.acos(cos_ha))
    out = []
    for sign in (1, -1):                         # wschod, zachod
        utc_min = 720 - 4 * (lon + sign * ha) - eqt
        t = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=utc_min)
        loc = dt.datetime.fromtimestamp(t.timestamp())
        out.append(loc.hour * 60 + loc.minute + loc.second / 60)
    return round(out[0], 1), round(out[1], 1)


def kwh_until(points, minute):
    """Energia z prognozy od poczatku doby do podanej minuty (trapezy)."""
    e = 0.0
    for (m1, p1), (m2, p2) in zip(points, points[1:]):
        if m1 >= minute:
            break
        if m2 > minute:  # ucinamy ostatni odcinek
            p2 = p1 + (p2 - p1) * (minute - m1) / (m2 - m1)
            m2 = minute
        e += (p1 + p2) / 2 * (m2 - m1) / 60
    return round(e, 2)


def migrate_legacy(con):
    """Jednorazowo: stare tabele forecast/forecast_day (tylko forecast.solar) -> nowe."""
    n = con.execute('SELECT COUNT(*) FROM forecast_src WHERE "source" = ?', ("forecast_solar",)).fetchone()[0]
    if n:
        return
    try:
        days = con.execute('SELECT "day", planes, fetched FROM forecast_day').fetchall()
    except Exception:  # noqa: BLE001 - brak starej tabeli
        return
    cur = con.cursor()
    for d, planes, fetched in days:
        pts = con.execute('SELECT "minute", watts FROM forecast WHERE "day" = ?', (d,)).fetchall()
        cur.executemany('INSERT INTO forecast_src ("source", "day", "minute", watts) VALUES (?, ?, ?, ?)',
                        [("forecast_solar", d, float(m), float(w)) for m, w in pts])
        total = sum(json.loads(planes).values()) if planes else 0
        cur.execute('INSERT INTO forecast_src_day ("source", "day", total_wh, planes, fetched, '
                    'day_ahead_wh, day_ahead_fetched) VALUES (?, ?, ?, ?, ?, NULL, NULL)',
                    ("forecast_solar", d, float(total), planes, fetched))
    con.commit()


def loop(db_connect):
    con = None
    while True:
        try:
            if con is None:
                con = db_connect()
                migrate_legacy(con)
            waits = []
            for grp, fetch in FETCHERS.items():
                last = last_fetch_group(con, grp)
                wait = 0 if last is None else last + ODSWIEZANIE - time.time()
                if wait > 0:
                    with _lock:
                        state[grp]["updated"] = state[grp]["updated"] or last
                    waits.append(wait)
                    continue
                try:
                    for src, (moc, per_plane) in fetch().items():
                        save(con, src, moc, per_plane)
                    with _lock:
                        state[grp].update(updated=int(time.time()), error=None)
                    waits.append(ODSWIEZANIE)
                except urllib.error.HTTPError as e:
                    with _lock:
                        state[grp]["error"] = "limit zapytań API (429)" if e.code == 429 else f"HTTP {e.code}"
                    waits.append(900)
                except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
                    with _lock:
                        state[grp]["error"] = str(e)
                    waits.append(600)
            time.sleep(max(60, min(min(waits), 300)))
        except Exception as e:  # noqa: BLE001 - blad bazy
            with _lock:
                for src in state:
                    state[src]["error"] = f"baza: {e}"
            try:
                if con:
                    con.close()
            except Exception:  # noqa: BLE001
                pass
            con = None
            time.sleep(120)


def status():
    with _lock:
        return {k: dict(v) for k, v in state.items()}


# --- trafnosc ---------------------------------------------------------------
def save_accuracy(con, day, actual_kwh):
    """Zapis trafnosci prognoz dla zakonczonego dnia (wszystkie zrodla)."""
    now = int(time.time())
    for src in SOURCES:
        r = con.execute('SELECT total_wh, day_ahead_wh FROM forecast_src_day WHERE "source" = ? AND "day" = ?',
                        (src, day)).fetchone()
        if not r:
            continue
        con.execute('DELETE FROM forecast_accuracy WHERE "day" = ? AND "source" = ?', (day, src))
        con.execute('INSERT INTO forecast_accuracy ("day", "source", day_ahead_kwh, latest_kwh, actual_kwh, updated) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (day, src, None if r[1] is None else round(r[1] / 1000, 2), round((r[0] or 0) / 1000, 2),
                     float(actual_kwh), now))
    con.commit()


def accuracy(con, days=14):
    first = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = con.execute('SELECT "day", "source", day_ahead_kwh, latest_kwh, actual_kwh FROM forecast_accuracy '
                       'WHERE "day" >= ? ORDER BY "day" DESC', (first,)).fetchall()
    by_day = {}
    for d, src, da, lt, act in rows:
        by_day.setdefault(d, {"day": d, "actual_kwh": act})[src] = {"day_ahead": da, "latest": lt}
    out = sorted(by_day.values(), key=lambda x: x["day"], reverse=True)
    # sugerowana korekta = suma rzeczywista / suma prognoz dnia poprzedniego (bez dni < 0,5 kWh)
    sugg = {}
    for src in SOURCES:
        pairs = [(r["actual_kwh"], r[src]["day_ahead"]) for r in out
                 if src in r and r[src]["day_ahead"] and r[src]["day_ahead"] > 0.5]
        if pairs:
            sugg[src] = {"days": len(pairs),
                         "korekta": round(KOREKTA.get(src, 1) * sum(a for a, _ in pairs) / sum(f for _, f in pairs), 2),
                         "blad_sr_proc": round(100 * sum(abs(a - f) / f for a, f in pairs) / len(pairs), 1)}
    return {"days": out, "suggest": sugg}
