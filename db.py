"""
Baza danych SQLite (wbudowana w Pythona, bez instalacji). Plik w folderze danych (config.DATA_DIR).
"""
import sqlite3

import config

DB_PATH = config.DATA_DIR / (config.get("baza", "plik", "sigen.db") or "sigen.db")


class DbError(Exception):
    pass


def connect():
    """Nowe polaczenie. Kazdy watek/zapytanie API uzywa wlasnego."""
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def describe():
    return f"SQLite {DB_PATH}"


SCHEMA = [
    'CREATE TABLE IF NOT EXISTS bat_temps (ts INTEGER PRIMARY KEY, t_avg REAL, t_max REAL, t_min REAL, t_pcs REAL)',
    'CREATE TABLE IF NOT EXISTS pstryk_hour (ts INTEGER PRIMARY KEY, "day" TEXT, "imp" REAL, "exp" REAL, '
    'cost REAL, sold REAL, energy_net REAL, dist_var REAL, dist_fix REAL, service REAL, excise REAL, vat REAL, '
    'power_fee REAL, price_gross REAL, price_pros REAL, updated INTEGER)',
    'CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, soc REAL, pv REAL, '
    'bat REAL, grid REAL, plant REAL, "load" REAL)',
    'CREATE TABLE IF NOT EXISTS counters ("day" TEXT, "key" TEXT, "first" REAL, "last" REAL, '
    'ts INTEGER, PRIMARY KEY ("day", "key"))',
    'CREATE TABLE IF NOT EXISTS forecast ("day" TEXT, "minute" REAL, watts REAL, '
    'PRIMARY KEY ("day", "minute"))',
    'CREATE TABLE IF NOT EXISTS forecast_day ("day" TEXT PRIMARY KEY, planes TEXT, fetched INTEGER)',
    'CREATE TABLE IF NOT EXISTS settings_log (ts INTEGER, "key" TEXT, "value" TEXT, PRIMARY KEY (ts, "key"))',
    'CREATE TABLE IF NOT EXISTS prices (ts INTEGER PRIMARY KEY, "day" TEXT, csdac REAL, rce REAL)',
    'CREATE INDEX IF NOT EXISTS ix_prices_day ON prices ("day")',
    'CREATE TABLE IF NOT EXISTS efficiency_daily ("day" TEXT, bin_from REAL, hours REAL, e_in REAL, '
    'e_out REAL, updated INTEGER, PRIMARY KEY ("day", bin_from))',
    'CREATE TABLE IF NOT EXISTS forecast_src ("source" TEXT, "day" TEXT, "minute" REAL, watts REAL, '
    'PRIMARY KEY ("source", "day", "minute"))',
    'CREATE TABLE IF NOT EXISTS forecast_src_day ("source" TEXT, "day" TEXT, total_wh REAL, planes TEXT, '
    'fetched INTEGER, day_ahead_wh REAL, day_ahead_fetched INTEGER, day_ahead_planes TEXT, PRIMARY KEY ("source", "day"))',
    # przebieg prognozy "z dnia poprzedniego" (ostatnia pobrana przed polnoca) - zamrozony po polnocy
    'CREATE TABLE IF NOT EXISTS forecast_src_da ("source" TEXT, "day" TEXT, "minute" REAL, watts REAL, '
    'PRIMARY KEY ("source", "day", "minute"))',
    'CREATE TABLE IF NOT EXISTS forecast_accuracy ("day" TEXT, "source" TEXT, day_ahead_kwh REAL, '
    'latest_kwh REAL, actual_kwh REAL, updated INTEGER, PRIMARY KEY ("day", "source"))',
    'CREATE TABLE IF NOT EXISTS pv_strings (ts INTEGER PRIMARY KEY, u1 REAL, i1 REAL, u2 REAL, i2 REAL, '
    'u3 REAL, i3 REAL, u4 REAL, i4 REAL)',
    # rekuperator Vallox (opcjonalnie) i miernik jego poboru pradu
    'CREATE TABLE IF NOT EXISTS vallox_samples (ts INTEGER PRIMARY KEY, t_out REAL, t_sup REAL, t_sup_cell REAL, '
    't_ext REAL, t_exh REAL, rh REAL, co2 REAL, fan_pct REAL, rpm_sup REAL, rpm_ext REAL, cell_state INTEGER, '
    'profile INTEGER, eff_sup REAL, eff_ext REAL, src TEXT, co2_s0 REAL, co2_s1 REAL)',
    'CREATE TABLE IF NOT EXISTS vallox_log (ts INTEGER, param TEXT, old_value REAL, new_value REAL, '
    'source TEXT, ok INTEGER, msg TEXT, PRIMARY KEY (ts, param))',
    'CREATE TABLE IF NOT EXISTS meter_samples (ts INTEGER PRIMARY KEY, power REAL, total_wh REAL, energy_wh REAL)',
]


def init_schema(con):
    for sql in SCHEMA:
        con.execute(sql)
    for table, col, typ in (("pv_strings", "u4", "REAL"), ("pv_strings", "i4", "REAL"),
                            ("forecast_src_day", "day_ahead_planes", "TEXT")):
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    con.commit()


# --- probki mocy ----------------------------------------------------------
def insert_sample(con, ts, vals):
    con.execute('INSERT OR IGNORE INTO samples (ts, soc, pv, bat, grid, plant, "load") VALUES (?, ?, ?, ?, ?, ?, ?)',
                (ts, *vals))
    con.commit()


def upsert_counter(con, day, key, value, ts):
    """Pierwszy odczyt dnia zostaje w "first", kazdy kolejny nadpisuje "last"."""
    con.execute('UPDATE counters SET "last" = ?, ts = ? WHERE "day" = ? AND "key" = ?', (value, ts, day, key))
    con.execute('INSERT OR IGNORE INTO counters ("day", "key", "first", "last", ts) VALUES (?, ?, ?, ?, ?)',
                (day, key, value, value, ts))


# --- historia ustawien falownika ---------------------------------------
def last_settings(con):
    rows = con.execute(
        'SELECT s."key", s."value" FROM settings_log s WHERE s.ts = '
        '(SELECT MAX(t.ts) FROM settings_log t WHERE t."key" = s."key")').fetchall()
    return {k: v for k, v in rows}


def log_settings(con, ts, changed):
    for k, v in changed.items():
        con.execute('INSERT OR IGNORE INTO settings_log (ts, "key", "value") VALUES (?, ?, ?)', (ts, k, v))
    con.commit()


def settings_history(con, limit=30):
    rows = con.execute('SELECT ts, "key", "value" FROM settings_log ORDER BY ts DESC LIMIT ?', (limit,)).fetchall()
    return [{"ts": t, "key": k, "value": v} for t, k, v in rows]


# --- sprawnosc falownika (dzienne przedzialy mocy) ---------------------
def save_efficiency_day(con, day, rows, ts):
    """rows: [(bin_from, hours, e_in, e_out)] - zastepuje dane dnia."""
    cur = con.cursor()
    cur.execute('DELETE FROM efficiency_daily WHERE "day" = ?', (day,))
    if rows:
        cur.executemany('INSERT INTO efficiency_daily ("day", bin_from, hours, e_in, e_out, updated) '
                        'VALUES (?, ?, ?, ?, ?, ?)',
                        [(day, float(b), float(h), float(i), float(o), ts) for b, h, i, o in rows])
    con.commit()


def efficiency_days(con):
    return {r[0] for r in con.execute('SELECT DISTINCT "day" FROM efficiency_daily').fetchall()}


def load_efficiency(con, first, last):
    if first is None:
        return con.execute('SELECT "day", bin_from, hours, e_in, e_out FROM efficiency_daily '
                           'WHERE "day" <= ?', (last,)).fetchall()
    return con.execute('SELECT "day", bin_from, hours, e_in, e_out FROM efficiency_daily '
                       'WHERE "day" >= ? AND "day" <= ?', (first, last)).fetchall()


# --- stringi PV (napiecie i prad) ------------------------------------------
def insert_pv_strings(con, ts, vals):
    """vals: (u1, i1, u2, i2, u3, i3, u4, i4) - None dla nieuzywanych."""
    con.execute('INSERT OR IGNORE INTO pv_strings (ts, u1, i1, u2, i2, u3, i3, u4, i4) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (ts, *vals))
    con.commit()


def load_pv_strings(con, start, end):
    """[(ts, [(u1, i1), (u2, i2), (u3, i3), (u4, i4)])]"""
    rows = con.execute('SELECT ts, u1, i1, u2, i2, u3, i3, u4, i4 FROM pv_strings '
                       'WHERE ts >= ? AND ts < ? ORDER BY ts', (start, end)).fetchall()
    return [(r[0], [(r[1 + 2 * k], r[2 + 2 * k]) for k in range(4)]) for r in rows]


# --- temperatura ogniw magazynu ---------------------------------------------
def insert_bat_temps(con, ts, vals):
    con.execute("INSERT OR IGNORE INTO bat_temps (ts, t_avg, t_max, t_min, t_pcs) VALUES (?, ?, ?, ?, ?)", (ts, *vals))
    con.commit()


def load_bat_temps(con, start, end, bucket=60):
    """[(ts, avg, max, min, pcs)] - srednie w przedzialach bucket sekund."""
    return con.execute(f"SELECT (ts / {int(bucket)}) * {int(bucket)} AS b, AVG(t_avg), MAX(t_max), MIN(t_min), AVG(t_pcs) "
                       f"FROM bat_temps WHERE ts >= ? AND ts < ? GROUP BY (ts / {int(bucket)}) * {int(bucket)} ORDER BY b",
                       (start, end)).fetchall()


# --- rekuperator Vallox ------------------------------------------------------
VALLOX_COLS = ("t_out, t_sup, t_sup_cell, t_ext, t_exh, rh, co2, fan_pct, rpm_sup, rpm_ext, cell_state, co2_s0, co2_s1, "
               "profile, eff_sup, eff_ext")


def insert_vallox(con, ts, vals):
    ph = ", ".join(["?"] * (len(vals) + 1))
    con.execute(f"INSERT OR IGNORE INTO vallox_samples (ts, {VALLOX_COLS}) VALUES ({ph})", (ts, *vals))
    con.commit()


def load_vallox(con, start, end):
    return con.execute(f"SELECT ts, {VALLOX_COLS} FROM vallox_samples WHERE ts >= ? AND ts < ? ORDER BY ts",
                       (start, end)).fetchall()


def log_vallox_change(con, ts, param, old, new, source, ok, msg):
    con.execute("INSERT OR IGNORE INTO vallox_log (ts, param, old_value, new_value, source, ok, msg) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", (ts, param, old, new, source, 1 if ok else 0, (msg or "")[:200]))
    con.commit()


def vallox_log_history(con, limit=20):
    rows = con.execute("SELECT ts, param, old_value, new_value, source, ok, msg FROM vallox_log "
                       "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(ts=r[0], param=r[1], old=r[2], new=r[3], source=r[4], ok=bool(r[5]), msg=r[6]) for r in rows]


def vallox_ts_buckets(con, start, end, bucket=600):
    """Zbior przedzialow (ts // bucket), w ktorych sa juz odczyty rekuperatora."""
    b = int(bucket)
    return {int(r[0]) for r in con.execute(f"SELECT DISTINCT ts / {b} FROM vallox_samples WHERE ts >= ? AND ts < ?",
                                           (start, end)).fetchall()}


def insert_vallox_log(con, rows):
    """rows: [(ts, {kolumna: wartosc})] - odczyty odtworzone z dziennika rekuperatora (src = 'log')."""
    for ts, v in rows:
        cols = list(v) + ["src"]
        ph = ", ".join(["?"] * len(cols))
        con.execute(f"INSERT OR IGNORE INTO vallox_samples (ts, {', '.join(cols)}) VALUES (?, {ph})",
                    (ts, *v.values(), "log"))
    con.commit()


def vallox_rpm_fit(con, since):
    """Pary (predkosc wentylacji %, obroty nawiewu, obroty wywiewu) z odczytow na zywo - do odtworzenia obrotow."""
    return con.execute("SELECT fan_pct, rpm_sup, rpm_ext FROM vallox_samples WHERE ts >= ? AND src IS NULL "
                       "AND fan_pct IS NOT NULL AND rpm_sup IS NOT NULL AND rpm_ext IS NOT NULL", (since,)).fetchall()


# --- miernik poboru pradu rekuperatora (meter.py) ---------------------------------
def insert_meter(con, ts, vals):
    con.execute("INSERT OR IGNORE INTO meter_samples (ts, power, total_wh, energy_wh) VALUES (?, ?, ?, ?)", (ts, *vals))
    con.commit()


def last_meter_total(con):
    r = con.execute("SELECT total_wh FROM meter_samples ORDER BY ts DESC LIMIT 1").fetchone()
    return r[0] if r else None


def load_meter(con, start, end):
    return con.execute("SELECT ts, power, energy_wh FROM meter_samples WHERE ts >= ? AND ts < ? ORDER BY ts",
                       (start, end)).fetchall()


def meter_energy(con, start, end):
    r = con.execute("SELECT SUM(energy_wh) FROM meter_samples WHERE ts >= ? AND ts < ?", (start, end)).fetchone()
    return (r[0] or 0.0) if r else 0.0
