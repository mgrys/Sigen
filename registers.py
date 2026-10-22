"""
Pelna lista odczytow Sigenergy (59 rejestrow), pogrupowana.
Wg Sigenergy Modbus Protocol V2.7. Uzywane przez app.py i tools/sigen_scan.py.
"""
import config

PLANT = config.geti("falownik", "plant_id", 247)
INV = config.geti("falownik", "inverter_id", 1)
_SNAMES = {n: name for name, n in config.strings()}


def _pv(n):
    return f"PV{n} {_SNAMES[n]}" if n in _SNAMES and _SNAMES[n] != f"PV{n}" else f"PV{n}" + ("" if n in _SNAMES else " (nieużywany)")


STAN_PRACY = {0: "standby", 1: "praca", 2: "awaria", 3: "wyłączony"}
TRYB_EMS = {0: "maks. autokonsumpcja", 1: "Sigen AI", 2: "TOU (taryfa)", 7: "zdalne EMS"}
TRYB_SIECI = {0: "praca z siecią", 1: "off-grid (auto)", 2: "off-grid (ręcznie)"}

# (klucz, opis, slave, rejestr, liczba rej., typ, dzielnik, jednostka[, slownik])
# typ: u = bez znaku, s = ze znakiem, str = tekst ASCII
GROUPS = [
    ("Moce na bieżąco", [
        ("grid",        "Moc z licznika sieci (+pobór)", PLANT, 30005, 2, "s", 1000, "kW"),
        ("grid_a",      "Sieć faza L1",                  PLANT, 30052, 2, "s", 1000, "kW"),
        ("grid_b",      "Sieć faza L2",                  PLANT, 30054, 2, "s", 1000, "kW"),
        ("grid_c",      "Sieć faza L3",                  PLANT, 30056, 2, "s", 1000, "kW"),
        ("plant",       "Moc czynna instalacji",         PLANT, 30031, 2, "s", 1000, "kW"),
        ("plant_q",     "Moc bierna instalacji",         PLANT, 30033, 2, "s", 1000, "kvar"),
        ("pv",          "Moc PV",                        PLANT, 30035, 2, "s", 1000, "kW"),
        ("bat",         "Moc magazynu (+ładowanie)",     PLANT, 30037, 2, "s", 1000, "kW"),
    ]),
    ("Magazyn", [
        ("soc",         "SOC",                           PLANT, 30014, 1, "u", 10, "%"),
        ("soh_ess",     "SOH magazynu (instalacja)",     PLANT, 30087, 1, "u", 10, "%"),
        ("soh_bat",     "SOH baterii (falownik)",        INV,   30602, 1, "u", 10, "%"),
        ("cap_ess",     "Pojemność znamionowa ESS",      PLANT, 30083, 2, "u", 100, "kWh"),
        ("cap_bat",     "Pojemność baterii",             INV,   30548, 2, "u", 100, "kWh"),
        ("soc_max",     "Limit ładowania SOC",           PLANT, 30085, 1, "u", 10, "%"),
        ("soc_min",     "Limit rozładowania SOC",        PLANT, 30086, 1, "u", 10, "%"),
        ("avail_chg",   "Dostępna energia do ładowania", PLANT, 30064, 2, "u", 100, "kWh"),
        ("avail_dis",   "Dostępna energia do rozładowania", PLANT, 30066, 2, "u", 100, "kWh"),
        ("cell_temp",   "Śr. temperatura ogniw",         INV,   30603, 1, "s", 10, "°C"),
        ("bat_t_max",   "Maks. temperatura baterii",     INV,   30620, 1, "s", 10, "°C"),
        ("bat_t_min",   "Min. temperatura baterii",      INV,   30621, 1, "s", 10, "°C"),
        ("cell_v",      "Śr. napięcie ogniwa",           INV,   30604, 1, "u", 1000, "V"),
    ]),
    ("Liczniki dzienne", [
        ("load_day",    "Zużycie domu dziś",             PLANT, 30092, 2, "u", 100, "kWh"),
        ("pv_day",      "Produkcja PV dziś",             INV,   31509, 2, "u", 100, "kWh"),
        ("imp_day",     "Falownik: energia AC pobrana dziś", INV, 30560, 2, "u", 100, "kWh"),
        ("exp_day",     "Falownik: energia AC oddana dziś", INV,  30554, 2, "u", 100, "kWh"),
        ("chg_day",     "Ładowanie magazynu dziś",       INV,   30566, 2, "u", 100, "kWh"),
        ("dis_day",     "Rozładowanie magazynu dziś",    INV,   30572, 2, "u", 100, "kWh"),
    ]),
    ("Liczniki narastające", [
        ("pv_total",    "Produkcja PV (instalacja)",     PLANT, 30088, 4, "u", 100, "kWh"),
        ("pv_total_inv", "Produkcja PV (falownik)",      INV,   31511, 2, "u", 100, "kWh"),
        ("load_total",  "Zużycie domu",                  PLANT, 30094, 4, "u", 100, "kWh"),
        ("chg_total",   "Ładowanie magazynu",            PLANT, 30200, 4, "u", 100, "kWh"),
        ("dis_total",   "Rozładowanie magazynu",         PLANT, 30204, 4, "u", 100, "kWh"),
        ("imp_total",   "Pobór z sieci",                 PLANT, 30216, 4, "u", 100, "kWh"),
        ("exp_total",   "Oddane do sieci",               PLANT, 30220, 4, "u", 100, "kWh"),
    ]),
    ("Falownik", [
        ("model",       "Model",                         INV,   30500, 15, "str", 1, ""),
        ("serial",      "Numer seryjny",                 INV,   30515, 10, "str", 1, ""),
        ("firmware",    "Firmware",                      INV,   30525, 15, "str", 1, ""),
        ("rated_p",     "Moc znamionowa",                INV,   30540, 2, "u", 1000, "kW"),
        ("pcs_temp",    "Temperatura falownika (PCS)",   INV,   31003, 1, "s", 10, "°C"),
    ]),
    ("Strona sieci", [
        ("freq",        "Częstotliwość",                 INV,   31002, 1, "u", 100, "Hz"),
        ("u_a",         "Napięcie L1",                   INV,   31011, 2, "u", 100, "V"),
        ("u_b",         "Napięcie L2",                   INV,   31013, 2, "u", 100, "V"),
        ("u_c",         "Napięcie L3",                   INV,   31015, 2, "u", 100, "V"),
        ("i_a",         "Prąd L1",                       INV,   31017, 2, "s", 100, "A"),
        ("i_b",         "Prąd L2",                       INV,   31019, 2, "s", 100, "A"),
        ("i_c",         "Prąd L3",                       INV,   31021, 2, "s", 100, "A"),
        ("pf",          "Współczynnik mocy",             INV,   31023, 1, "u", 1000, ""),
    ]),
    ("Strona PV", [
        ("mppt",        "Liczba MPPT",                   INV,   31026, 1, "u", 1, ""),
        ("pv1_u",       _pv(1) + " – napięcie", INV,   31027, 1, "s", 10, "V"),
        ("pv1_i",       _pv(1) + " – prąd",     INV,   31028, 1, "s", 100, "A"),
        ("pv2_u",       _pv(2) + " – napięcie", INV,   31029, 1, "s", 10, "V"),
        ("pv2_i",       _pv(2) + " – prąd",     INV,   31030, 1, "s", 100, "A"),
        ("pv3_u",       _pv(3) + " – napięcie", INV,   31031, 1, "s", 10, "V"),
        ("pv3_i",       _pv(3) + " – prąd",     INV,   31032, 1, "s", 100, "A"),
        ("pv4_u",       _pv(4) + " – napięcie", INV,   31033, 1, "s", 10, "V"),
        ("pv4_i",       _pv(4) + " – prąd",     INV,   31034, 1, "s", 100, "A"),
        ("riso",        "Rezystancja izolacji",          INV,   31037, 1, "u", 1000, "MΩ"),
    ]),
    ("Diagnostyka", [
        ("ems_mode",    "Tryb pracy EMS",                PLANT, 30003, 1, "u", 1, "", TRYB_EMS),
        ("plant_state", "Stan pracy instalacji",         PLANT, 30051, 1, "u", 1, "", STAN_PRACY),
        ("grid_mode",   "Praca z siecią",                PLANT, 30009, 1, "u", 1, "", TRYB_SIECI),
        ("inv_state",   "Stan pracy falownika",          INV,   30578, 1, "u", 1, "", STAN_PRACY),
    ]),
]

ALL = [r for _, regs in GROUPS for r in regs]


def decode(regs, typ, div):
    """Big-endian, starsze slowo pierwsze."""
    if typ == "str":
        b = b"".join(r.to_bytes(2, "big") for r in regs)
        return b.split(b"\0")[0].decode("ascii", "replace").strip()
    v = 0
    for r in regs:
        v = (v << 16) | r
    bits = 16 * len(regs)
    if typ == "s" and v >= 1 << (bits - 1):
        v -= 1 << bits
    return round(v / div, 3) if div != 1 else v


def read_all(client, read):
    """Czyta wszystkie rejestry. read(client, addr, count, unit) -> odpowiedz pymodbus.

    Zwraca {klucz: wartosc}; rejestr niedostepny -> None.
    """
    out = {}
    for key, _name, unit, addr, count, typ, div, *_ in ALL:
        rr = read(client, addr, count, unit)
        out[key] = None if rr.isError() else decode(rr.registers, typ, div)
    return out


# Polacie PV: (nazwa, klucz napiecia, klucz pradu) - moc liczona jako U x I
STRINGS = [(name, f"pv{n}_u", f"pv{n}_i") for name, n in config.strings()]


def string_powers(values):
    """{nazwa polaci: moc kW} z napiec i pradow stringow (None gdy brak odczytu)."""
    out = {}
    for name, ku, ki in STRINGS:
        u, i = values.get(ku), values.get(ki)
        out[name] = round(max(u * i, 0) / 1000, 3) if u is not None and i is not None else None
    return out


def grouped(values):
    """Wartosci w strukturze grup do wyswietlenia."""
    res = []
    for gname, regs in GROUPS:
        items = []
        if gname == "Strona PV":
            for name, p in string_powers(values).items():
                items.append({"key": f"p_{name}", "name": f"{name} – moc (U×I)", "addr": "—",
                              "value": p, "unit": "kW", "text": None})
        for key, name, _unit, addr, _c, _t, _d, jm, *opt in regs:
            v = values.get(key)
            text = None
            if v is not None and opt:
                text = opt[0].get(v, f"kod {v}")
            items.append({"key": key, "name": name, "addr": addr, "value": v, "unit": jm, "text": text})
        res.append({"group": gname, "items": items})
    return res


# --- ustawienia (holding registers, funkcja 0x03) - TYLKO ODCZYT ----------
# Wg Sigenergy Modbus Protocol V2.7, tabela 5-2 (plant) i 5-4 (falownik).
# Rejestry tylko do zapisu (40000/40500 start-stop, 41000) celowo pominiete.
WLACZONE = {0: "wyłączone", 1: "włączone"}
REMOTE_EMS_MODE = {
    0: "zdalne sterowanie PCS", 1: "standby", 2: "maks. autokonsumpcja",
    3: "wymuszone ładowanie (najpierw sieć)", 4: "wymuszone ładowanie (najpierw PV)",
    5: "wymuszone rozładowanie (najpierw PV)", 6: "wymuszone rozładowanie (najpierw magazyn)",
}
NO_LIMIT = 0xFFFFFFFF   # dla limitow U32: "brak limitu / wartosc nieustawiona"

# (klucz, opis, slave, rejestr, liczba, typ, dzielnik, jednostka, ryzyko, kiedy dziala[, slownik])
# ryzyko: bezpieczne / ostroznie / nie_ruszac
SETTINGS_GROUPS = [
    ("Magazyn – granice SOC", [
        ("soc_charge_cut", "Górna granica ładowania", PLANT, 40047, 1, "u", 10, "%", "bezpieczne", "zawsze"),
        ("soc_discharge_cut", "Dolna granica rozładowania", PLANT, 40048, 1, "u", 10, "%", "bezpieczne", "zawsze"),
        ("soc_backup", "Rezerwa SOC (backup)", PLANT, 40046, 1, "u", 10, "%", "bezpieczne",
         "przy pracy bez sieci"),
    ]),
    ("Limity mocy na przyłączu", [
        ("grid_export_max", "Maks. eksport (punkt przyłączenia)", PLANT, 40038, 2, "u", 1000, "kW", "ostroznie",
         "zawsze, wymaga licznika sieci"),
        ("grid_import_max", "Maks. import (punkt przyłączenia)", PLANT, 40040, 2, "u", 1000, "kW", "ostroznie",
         "zawsze, wymaga licznika sieci"),
        ("pcs_export_max", "Maks. eksport falownika (PCS)", PLANT, 40042, 2, "u", 1000, "kW", "ostroznie", "zawsze"),
        ("pcs_import_max", "Maks. import falownika (PCS)", PLANT, 40044, 2, "u", 1000, "kW", "ostroznie", "zawsze"),
    ]),
    ("Zdalne sterowanie (Remote EMS)", [
        ("remote_ems", "Remote EMS", PLANT, 40029, 1, "u", 1, "", "ostroznie", "—", WLACZONE),
        ("remote_ems_mode", "Tryb Remote EMS", PLANT, 40031, 1, "u", 1, "", "ostroznie",
         "gdy Remote EMS włączone", REMOTE_EMS_MODE),
        ("ess_charge_max", "Limit mocy ładowania", PLANT, 40032, 2, "u", 1000, "kW", "ostroznie", "tryby 3–4"),
        ("ess_discharge_max", "Limit mocy rozładowania", PLANT, 40034, 2, "u", 1000, "kW", "ostroznie", "tryby 5–6"),
        ("pv_max", "Limit mocy PV", PLANT, 40036, 2, "u", 1000, "kW", "ostroznie", "tryby 3–6"),
    ]),
]

SETTINGS = [r for _, regs in SETTINGS_GROUPS for r in regs]


# --- alarmy (input registers, funkcja 0x04) - TYLKO ODCZYT ---------------
# Wg Sigenergy Modbus Protocol, dodatek 2 (Alarm1) i 3 (Alarm2); te same bity dla falownika i calej instalacji.
ALARM1_BITS = {
    0: "niezgodność wersji oprogramowania", 1: "niska rezystancja izolacji", 2: "przegrzanie",
    3: "usterka urządzenia", 4: "błąd uziemienia systemu", 5: "przepięcie na stringu PV",
    6: "string PV podłączony odwrotnie", 7: "prąd wsteczny stringu PV", 8: "AFCI – wykryty łuk elektryczny",
    9: "zanik sieci", 10: "przepięcie sieci", 11: "za niskie napięcie sieci", 12: "za wysoka częstotliwość sieci",
    13: "za niska częstotliwość sieci", 14: "asymetria napięć sieci", 15: "za duża składowa DC prądu wyjściowego",
}
ALARM2_BITS = {
    0: "prąd upływu poza limitem", 1: "błąd komunikacji", 2: "zabezpieczenie wewnętrzne",
    3: "AFCI – usterka układu samotestu", 4: "zabezpieczenie pracy bez sieci", 5: "zabezpieczenie ręczne",
    7: "zła kolejność faz", 8: "zwarcie do PE", 9: "nieudany miękki start",
}
# (klucz, opis, slave, rejestr, bity, bit AFCI)
ALARMS = [
    ("alarm_inv1", "Falownik – DC / sieć", INV, 30605, ALARM1_BITS, 8),
    ("alarm_inv2", "Falownik – upływ / AFCI", INV, 30606, ALARM2_BITS, 3),
    ("alarm_plant1", "Instalacja – DC / sieć", PLANT, 30027, ALARM1_BITS, 8),
    ("alarm_plant2", "Instalacja – upływ / AFCI", PLANT, 30028, ALARM2_BITS, 3),
]
ALARM_NAMES = {a[0]: a[1] for a in ALARMS}
NO_ALARM = "brak alarmów"


def read_alarms(client, read_input):
    """{klucz: wartosc U16 | None}. read_input(client, addr, count, unit) - funkcja 0x04."""
    out = {}
    for key, _n, unit, addr, _bits, _afci in ALARMS:
        try:
            rr = read_input(client, addr, 1, unit)
            out[key] = None if rr.isError() else rr.registers[0]
        except Exception:  # noqa: BLE001
            out[key] = None
    return out


def alarm_active(key, value):
    """Lista aktywnych alarmow [(bit, opis, afci)]."""
    spec = next(a for a in ALARMS if a[0] == key)
    if not value:
        return []
    return [(b, spec[4].get(b, f"nieznany alarm (bit {b})"), b == spec[5]) for b in range(16) if value >> b & 1]


def alarm_text(key, value):
    act = alarm_active(key, value)
    return "; ".join(f"bit {b}: {t}" for b, t, _ in act) if act else NO_ALARM


def grouped_alarms(values):
    items = []
    for key, name, _u, addr, _bits, afci in ALARMS:
        v = values.get(key)
        act = alarm_active(key, v) if v is not None else []
        items.append({"key": key, "name": name, "addr": addr, "value": v,
                      "active": [{"bit": b, "text": t, "afci": a} for b, t, a in act],
                      "afci_bit": afci})
    return items


def read_settings(client, read_holding):
    """Odczyt ustawien. read_holding(client, addr, count, unit). Zwraca {klucz: wartosc|None|'brak limitu'}."""
    out = {}
    for key, _n, unit, addr, count, typ, div, *_ in SETTINGS:
        rr = read_holding(client, addr, count, unit)
        if rr.isError():
            out[key] = None
        elif count == 2 and typ == "u" and decode(rr.registers, "u", 1) == NO_LIMIT:
            out[key] = "brak limitu"
        else:
            out[key] = decode(rr.registers, typ, div)
    return out


def grouped_settings(values):
    res = []
    for gname, regs in SETTINGS_GROUPS:
        items = []
        for key, name, _u, addr, _c, _t, _d, jm, risk, when, *opt in regs:
            v = values.get(key)
            text = None
            if isinstance(v, str):
                text = v
            elif v is not None and opt:
                text = opt[0].get(v, f"kod {v}")
            items.append({"key": key, "name": name, "addr": addr, "value": v, "unit": jm,
                          "text": text, "risk": risk, "when": when})
        res.append({"group": gname, "items": items})
    return res
