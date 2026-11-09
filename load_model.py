"""
Prognoza zuzycia domu dla strategii (profil godzinowy na kazdy dzien planu).

Tryby (strategy.ini [pulapy] load_profile):
  prosty     - srednia dla danej godziny z ostatnich 14 dni (jak do 1.88), ten sam profil na kazdy dzien
  dni        - osobne profile: dzien roboczy (pn-pt), sobota, niedziela / swieto (swieta ustawowe w Polsce),
               z ostatnich 35 dni, nowsze dni wazniejsze (waga 1/2 co 7 dni); za malo dni danego typu -> wszystkie dni
  dni_temp   - jak "dni" + korekta wg prognozy temperatury: z ostatnich 35 dni dopasowana zaleznosc dobowego zuzycia
               od sredniej temperatury na zewnatrz (dni <= 16 °C - ogrzewanie klimatyzatorami, CWU); korekta tylko gdy
               zaleznosc jest wyrazna (co najmniej 10 dni, R^2 >= 0,25, wiecej zuzycia przy zimnie). Temperatura historyczna:
               rekuperator (t_out) albo czujnik Sonoff "Zewnetrzna"; prognoza: Open-Meteo (co godzine, cache 1 h).
"""
import datetime as dt
import json
import threading
import time
import unicodedata
import urllib.request

MODES = {"prosty": "prosty (14 dni)", "dni": "dni tygodnia", "dni_temp": "dni tygodnia + temperatura"}
TYPE_TXT = {"rob": "dzień roboczy", "sob": "sobota", "nd": "niedziela / święto"}
DAYS_BACK = 35
HALF_LIFE = 7.0          # dni - waga 1/2 co tyle dni
MIN_DAYS_TYPE = 3
HEAT_MAX_T = 16.0        # °C - dni do regresji temperatury (sezon grzewczy)
MIN_DAYS_TEMP = 10
MIN_R2 = 0.25
_fc_cache = {"ts": 0, "days": {}}
_lock = threading.Lock()


def _easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    mo = (h + m - 7 * n + 114) // 31
    da = (h + m - 7 * n + 114) % 31 + 1
    return dt.date(y, mo, da)


def holidays_pl(y):
    """Dni ustawowo wolne w Polsce (od 2025 tez Wigilia)."""
    e = _easter(y)
    fixed = [(1, 1), (1, 6), (5, 1), (5, 3), (8, 15), (11, 1), (11, 11), (12, 25), (12, 26)]
    if y >= 2025:
        fixed.append((12, 24))
    return {dt.date(y, m, d) for m, d in fixed} | {e, e + dt.timedelta(days=1), e + dt.timedelta(days=49),
                                                    e + dt.timedelta(days=60)}


def day_type(d):
    if d.weekday() == 6 or d in holidays_pl(d.year):
        return "nd"
    return "sob" if d.weekday() == 5 else "rob"


def _hourly_load(con, now, days):
    """{data: {godzina: kWh}} z tabeli samples (srednia moc w godzinie = kWh)."""
    rows = con.execute('SELECT ts / 3600, AVG("load") FROM samples WHERE ts >= ? GROUP BY ts / 3600',
                       (int(now) - days * 86400,)).fetchall()
    out = {}
    for hts, v in rows:
        if v is None:
            continue
        lt = time.localtime(int(hts) * 3600)
        out.setdefault(dt.date(lt.tm_year, lt.tm_mon, lt.tm_mday), {})[lt.tm_hour] = float(v)
    return out, len(rows)


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or "").replace("ł", "l")).encode("ascii", "ignore").decode().lower()
    return s.strip()


def _daily_temp(con, now, days):
    """{data: srednia temperatura zewn.} - rekuperator, a gdy brak - czujnik Sonoff 'Zewnetrzna'."""
    t0 = int(now) - days * 86400
    acc = {}
    try:
        for ts, t in con.execute("SELECT ts, t_out FROM vallox_samples WHERE ts >= ? AND t_out IS NOT NULL", (t0,)):
            acc.setdefault(dt.date.fromtimestamp(int(ts)), []).append(float(t))
    except Exception:  # noqa: BLE001
        pass
    if len(acc) < MIN_DAYS_TEMP:
        try:
            for ts, name, t in con.execute("SELECT ts, name, temp FROM sonoff_samples WHERE ts >= ? AND temp IS NOT NULL", (t0,)):
                if _norm(name).startswith(("zewn", "na zewn")):
                    acc.setdefault(dt.date.fromtimestamp(int(ts)), []).append(float(t))
        except Exception:  # noqa: BLE001
            pass
    return {d: sum(v) / len(v) for d, v in acc.items() if len(v) >= 12}


def forecast_temps(lat, lon):
    """{data: srednia prognozowana temperatura} na 3 dni (Open-Meteo, cache 1 h)."""
    with _lock:
        if time.time() - _fc_cache["ts"] < 3600 and _fc_cache["days"]:
            return dict(_fc_cache["days"])
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=temperature_2m"
           "&timezone=Europe%2FWarsaw&forecast_days=3")
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sigen-dashboard"})
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.load(r)
        acc = {}
        for t, v in zip(j["hourly"]["time"], j["hourly"]["temperature_2m"]):
            if v is not None:
                acc.setdefault(dt.date.fromisoformat(t[:10]), []).append(float(v))
        days = {d: sum(v) / len(v) for d, v in acc.items() if v}
    except Exception:  # noqa: BLE001
        days = {}
    with _lock:
        if days:
            _fc_cache.update(ts=time.time(), days=days)
        return dict(days or _fc_cache["days"])


def _fit(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx < 1e-6 or syy < 1e-6:
        return None, None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    r2 = b * b * sxx / syy
    return b, r2


def profiles(con, now, plan_days, mode, lat=None, lon=None):
    """Profil zuzycia [kWh w godzinie] dla kazdego dnia planu: ({data: [24]}, info)."""
    mode = mode if mode in MODES else "prosty"
    hist, nrows = _hourly_load(con, now, 14 if mode == "prosty" else DAYS_BACK)
    today = dt.date.fromtimestamp(now)
    info = {"mode": mode, "mode_txt": MODES[mode], "rows": nrows, "days": {}, "temp": None}

    def weighted(days_sel, recency=True):
        prof, wsum = [0.0] * 24, [0.0] * 24
        for d in days_sel:
            w = 0.5 ** ((today - d).days / HALF_LIFE) if recency else 1.0
            for h, v in hist[d].items():
                prof[h] += w * v
                wsum[h] += w
        allv = [prof[h] / wsum[h] for h in range(24) if wsum[h]]
        dflt = sum(allv) / len(allv) if allv else 0.3
        return [prof[h] / wsum[h] if wsum[h] else dflt for h in range(24)]

    past = [d for d in hist if d < today and len(hist[d]) >= 18]      # pelne dni
    out = {}
    if mode == "prosty":
        p = weighted(list(hist), recency=False)
        for d in plan_days:
            out[d] = p
            info["days"][d.isoformat()] = {"type": day_type(d), "type_txt": "wszystkie dni", "kwh": round(sum(p), 2), "n": len(hist)}
        return out, info
    by_type = {}
    for d in past:
        by_type.setdefault(day_type(d), []).append(d)
    for d in plan_days:
        t = day_type(d)
        sel = by_type.get(t, [])
        used_t = t
        if len(sel) < MIN_DAYS_TYPE:                 # za malo dni tego typu - weekend razem, potem wszystkie dni
            sel = by_type.get("sob", []) + by_type.get("nd", []) if t in ("sob", "nd") else []
            used_t = "weekend"
            if len(sel) < MIN_DAYS_TYPE:
                sel, used_t = past, "wszystkie"
        out[d] = weighted(sel) if sel else weighted(list(hist))
        info["days"][d.isoformat()] = {"type": t, "type_txt": TYPE_TXT[t], "from": used_t, "n": len(sel),
                                       "kwh": round(sum(out[d]), 2), "_sel": sel}
    if mode == "dni_temp":
        temps = _daily_temp(con, now, DAYS_BACK)
        pts = [(temps[d], sum(hist[d].values())) for d in past if d in temps and temps[d] <= HEAT_MAX_T]
        ti = {"n": len(pts), "slope": None, "r2": None, "used": False, "applied": {}, "source": None}
        if len(pts) >= MIN_DAYS_TEMP:
            b, r2 = _fit([p[0] for p in pts], [p[1] for p in pts])
            ti.update(slope=None if b is None else round(b, 3), r2=None if r2 is None else round(r2, 2))
            if b is not None and b < 0 and r2 >= MIN_R2 and lat is not None:
                fc = forecast_temps(lat, lon)
                ti["used"] = True
                for d in plan_days:
                    sel = info["days"][d.isoformat()]["_sel"]
                    tref_l = [temps[x] for x in sel if x in temps]
                    if d not in fc or not tref_l:
                        continue
                    tref = sum(tref_l) / len(tref_l)
                    tf = min(fc[d], HEAT_MAX_T)
                    base = sum(out[d])
                    corr = b * (tf - min(tref, HEAT_MAX_T))
                    corr = max(-0.5 * base, min(corr, base))           # najwyzej -50 % / +100 %
                    if base > 0.1:
                        out[d] = [v * (base + corr) / base for v in out[d]]
                    ti["applied"][d.isoformat()] = {"t_fc": round(fc[d], 1), "t_ref": round(tref, 1), "kwh": round(corr, 2)}
                    info["days"][d.isoformat()]["kwh"] = round(sum(out[d]), 2)
        info["temp"] = ti
    for v in info["days"].values():
        v.pop("_sel", None)
    return out, info
