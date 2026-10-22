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
