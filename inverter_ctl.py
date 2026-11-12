"""
Sterowanie falownikiem Sigen wg planu strategii osobistej - Remote EMS po Modbus TCP (zapis rejestrow plant 247).
DOMYSLNIE WYLACZONE - po instalacji nic nie jest wysylane do falownika, dopoki uzytkownik nie przelaczy sterowania
na stronie (karta Strategia osobista). Falownik musi miec wlaczony serwer Modbus TCP z prawem zapisu.

Tryby pracy (inverter_ctl.ini, [sterowanie] tryb, ustawiane na stronie):
  wyl     - nic nie jest wysylane; jesli wczesniej sterowalismy - oddajemy sterowanie (Remote EMS = wylaczone),
  probny  - liczy, co zostaloby wyslane, i zapisuje to w dzienniku - BEZ zapisu do falownika,
  wl      - co kilka sekund pilnuje, by falownik pracowal w trybie biezacej godziny planu.

Plan -> Remote EMS (Sigenergy Modbus Protocol V2.7, tab. 5-2):
  autokonsumpcja    -> 40031 = 2 (maks. autokonsumpcja)
  standby           -> 40031 = 1
  ladowanie z sieci -> 40032 = moc ladowania (energia z planu rozlozona na reszte godziny, <= limit uzytkownika), 40031 = 3 (wymuszone ladowanie, najpierw siec);
                       po osiagnieciu SOC z planu na koniec godziny -> standby do konca godziny
  sprzedaz z mag.   -> 40034 = moc rozladowania (jw., <= limit uzytkownika), 40031 = 5 (wymuszone rozladowanie, najpierw PV);
                       po zejsciu do SOC z planu -> standby do konca godziny
  na koniec:        -> 40029 = 1 (Remote EMS wlaczone); oddanie sterowania: 40029 = 0 (falownik wraca do
                       wlasnego trybu z aplikacji mySigen).
Bezpieczenstwo: brak planu / brak swiezego SOC / blad -> oddanie sterowania; wylaczenie aplikacji -> oddanie
sterowania (release_now); po zapisie odczyt kontrolny 40029/40031; co REASSERT_S ponowienie ustawienia.
Priorytet: reczne > pauza > strategia (harmonogramu na razie brak). Sterowanie reczne (inverter_ctl.ini [reczne]):
tryb standby / samozuzycie / ladowanie z sieci / rozladowanie stala moca, czas lub "do anulowania", granice SOC
i limit mocy przylacza (domyslnie 22 kW) - po czasie powrot do strategii; dziala tez przy wylaczonej strategii.
Dziennik: tylko zmiany trybu (czas, poprzedni -> nowy tryb, zrodlo, SOC) - czas w trybach z roznic miedzy wpisami.
Pauza (inverter_ctl.ini [sterowanie] pauza_do = ts): sterowanie oddane falownikowi do podanej chwili, potem samo wraca.
Powiadomienia e-mail (alerts.ini, _notify z app.py): blad zapisu, sterowanie oddane z powodu braku planu / danych,
brak odczytu z falownika gdy sterowalismy (falownik mogl zostac w ostatnim trybie) - i informacja, gdy wszystko wroci.
Niezgodnosc: falownik robi co innego niz wyslany tryb (np. polecenie z aplikacji mySigen ma pierwszenstwo nad Remote EMS)
- kontrola przeplywow co DECIDE_S (np. standby, a magazyn laduje / rozladowuje; sprzedaz, a magazyn nie oddaje energii)
i odczyt kontrolny rejestrow 40029 / 40031 co REG_CHECK_S; trwajaca >= MISMATCH_AFTER -> ostrzezenie na stronie,
wpis w dzienniku i e-mail (oraz informacja, gdy ustapi).
"""
import configparser
import json
import math
import threading
import time
import traceback

import config

BASE = config.DATA_DIR      # pliki stanu obok bazy danych (folder danych aplikacji)
INI = BASE / "inverter_ctl.ini"
LOG = BASE / "inverter_ctl_log.json"
STATE = BASE / "inverter_ctl_state.json"

REG_ENABLE, REG_MODE, REG_CHG, REG_DIS = 40029, 40031, 40032, 40034
PLAN_TO_EMS = {"auto": 2, "standby": 1, "grid": 3, "export": 5, "pvchg": 4, "nodis": 2}
EMS_TXT = {1: "standby", 2: "maks. autokonsumpcja", 3: "ładowanie z sieci", 4: "ładowanie z PV (dom z sieci)",
           5: "rozładowanie do sieci",
           6: "rozładowanie stałą mocą", 0: "oddane falownikowi"}
MODE_NAME = {0: "falownik (mySigen)", 1: "standby", 2: "samozużycie", 3: "ładowanie z sieci", 4: "PV do magazynu",
             5: "sprzedaż z magazynu",
             6: "rozładowanie stałą mocą"}
MANUAL = {"standby": (1, "Standby"), "auto": (2, "Samozużycie"), "grid": (3, "Ładowanie z sieci"),
          "pvchg": (4, "PV do magazynu, dom z sieci"), "auto_nodis": (2, "Samozużycie bez rozładowania"),
          "discharge": (6, "Rozładowanie ze stałą mocą")}
MANUAL_DUR = {"15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "cancel": 0}
P_MAX = 8.0            # kW - maks. moc falownika (ustawiane z app.py)
PRZYLACZE_KW = 22.0    # kW - domyslny limit mocy przylacza dla trybow recznych
get_load = None        # funkcja -> biezacy pobor domu [kW] (app.py)
PVCHG_MIN_KW = 0.5     # prog PV do magazynu [kW] - ustawiany z app.py (z pomiarow sprawnosci ladowania)
get_flows = None       # funkcja -> {"pv", "bat" (+ ladowanie), "grid" (+ pobor), "soc"} [kW, %] (app.py)
MISMATCH_KW = 0.3      # kW - odchylka przeplywu magazynu uznawana za niezgodnosc z trybem
MISMATCH_AFTER = 300   # s - niezgodnosc trwajaca dluzej = ostrzezenie + e-mail
MISMATCH_SETTLE = 60   # s - po zapisie trybu falownik ma czas na zmiane
REG_CHECK_S = 60       # s - odczyt kontrolny rejestrow Remote EMS miedzy ponowieniami
get_pv = None          # funkcja -> biezaca moc PV [kW] (app.py)
EXPORT_PV_KW = 0.5     # kW - ponizej: sprzedaz trybem 6 (najpierw magazyn, moc = 40034), powyzej: tryb 5 (najpierw PV)
TRYBY = {"wyl": "wyłączone", "probny": "próbnie (bez zapisu)", "wl": "włączone"}
ETA = 0.95             # sprawnosc ladowania / rozladowania (jak w strategii)
SOC_TOL = 0.5          # % - tolerancja docelowego SOC
REASSERT_S = 600       # s - ponowienie ustawienia mimo braku zmian
STALE_S = 180          # s - odczyt SOC starszy = brak danych -> oddanie sterowania
DECIDE_S = 30          # s - co ile decyzja wg planu
LOG_MAX = 300
ALERT_GAP = 1800       # s - ten sam rodzaj powiadomienia najwyzej co 30 min
BAD_AFTER = 90         # s - problem trwajacy dluzej = powiadomienie (bez alarmow przy starcie aplikacji)
PAUZY = {"godzina": "do końca godziny", "1h": "na 1 godzinę", "3h": "na 3 godziny", "polnoc": "do północy",
         "jutro": "do jutra 6:00"}
_notify = None         # funkcja (temat, tresc) - ustawiana w app.py (alerts.ini)

_lock = threading.Lock()
state = {"tryb": "wyl", "desired": None, "reason": None, "applied": None, "applied_ts": None, "error": None,
         "decided_ts": None, "verify": None, "owned": False, "pause_until": 0, "last_txt": None,
         "src": None, "write": False, "soc": None, "manual": None, "last_mode": None, "note": None}
_alerts = {}           # rodzaj -> {"since": ts, "sent": ts | None}
_mm = {"since": None, "txt": None, "miss": 0, "logged": False, "reg": None, "reg_ts": 0}
_latch = {}            # godzina (ts) -> True, gdy cel SOC tej godziny osiagniety
# 1.26: standby z planu przy nadwyzce PV ponad prognoze -> samozuzycie bez rozladowania (nadwyzka PV do magazynu, nie do
# sieci). Plan liczony z prognozy (np. pochmurno), a wyszlo slonce: energia z PV w magazynie przyda sie na autokonsumpcje
# albo sprzedaz w drogiej godzinie. Wlaczenie przy nadwyzce >= PV_OVR_ON kW przez PV_OVR_ON_S, wylaczenie, gdy
# < PV_OVR_OFF kW przez PV_OVR_OFF_S (histereza - bez przeskakiwania przy chmurach).
PV_OVR_ON, PV_OVR_OFF = 0.3, 0.1
PV_OVR_ON_S, PV_OVR_OFF_S = 120, 600
PV_OVR_SOC_MAX = 99.0
_pv_ovr = {"on": False, "since": None}


def _cfg():
    cp = configparser.ConfigParser()
    if INI.exists():
        cp.read(INI, encoding="utf-8")
    t = cp.get("sterowanie", "tryb", fallback="wyl").strip().lower()
    try:
        p = int(cp.get("sterowanie", "pauza_do", fallback="0") or 0)
    except ValueError:
        p = 0
    return (t if t in TRYBY else "wyl"), p


def _read_tryb():
    return _cfg()[0]


_MANUAL_KEYS = ("tryb", "kw", "od", "do", "soc_min", "soc_max", "przylacze_kw")


def _manual_read():
    """Biezace polecenie reczne z inverter_ctl.ini albo None."""
    cp = configparser.ConfigParser()
    if INI.exists():
        cp.read(INI, encoding="utf-8")
    if not cp.has_section("reczne") or cp.get("reczne", "tryb", fallback="") not in MANUAL:
        return None
    m = {"tryb": cp.get("reczne", "tryb")}
    for k in _MANUAL_KEYS[1:]:
        try:
            m[k] = float(cp.get("reczne", k, fallback="0") or 0)
        except ValueError:
            m[k] = 0.0
    m["od"], m["do"] = int(m["od"]), int(m["do"])
    return m


def _cfg_save(tryb=None, pauza=None, manual="keep"):
    t0, p0 = _cfg()
    m0 = _manual_read() if manual == "keep" else manual
    cp = configparser.ConfigParser()
    cp["sterowanie"] = {"tryb": tryb or t0, "pauza_do": str(int(p0 if pauza is None else pauza))}
    if m0:
        cp["reczne"] = {k: (str(m0[k]) if k == "tryb" else str(int(m0.get(k) or 0)) if k in ("od", "do")
                            else f"{m0.get(k, 0):g}") for k in _MANUAL_KEYS}
    with open(INI, "w", encoding="utf-8") as f:
        f.write("; Sterowanie falownikiem wg strategii: wyl | probny (tylko dziennik) | wl (zapis Remote EMS)\n"
                "; pauza_do: czas unix - do tej chwili sterowanie oddane falownikowi (0 = bez pauzy)\n"
                "; [reczne]: sterowanie reczne (ma pierwszenstwo); do = czas unix konca, 0 = do anulowania\n")
        cp.write(f)


def set_manual(tryb, dur=None, minutes=None, kw=None, soc_min=None, soc_max=None, przylacze_kw=None):
    """Polecenie reczne. dur: 15m | 30m | 1h | 2h | cancel (do anulowania) albo minutes. tryb 'koniec' = anulowanie."""
    now = int(time.time())
    if tryb in ("koniec", "anuluj"):
        if _manual_read():
            _cfg_save(manual=None)
            state["note"] = "anulowano ręczne"
        state["manual"] = None
        return None
    if tryb not in MANUAL:
        raise ValueError("tryb ręczny: " + " / ".join(MANUAL))
    if minutes not in (None, ""):
        secs = int(float(minutes) * 60)
        if not 1 <= secs // 60 <= 24 * 60:
            raise ValueError("czas 1–1440 min")
    elif dur in MANUAL_DUR:
        secs = MANUAL_DUR[dur]
    else:
        raise ValueError("czas: " + " / ".join(MANUAL_DUR) + " albo liczba minut")
    def num(v, lo, hi, d, what):
        if v in (None, ""):
            return d
        v = float(str(v).replace(",", "."))
        if not lo <= v <= hi:
            raise ValueError(f"{what} {lo:g}–{hi:g}")
        return v
    m = {"tryb": tryb, "od": now, "do": now + secs if secs else 0,
         "kw": num(kw, 0.5, P_MAX, P_MAX if tryb == "pvchg" else min(3.0, P_MAX), "moc kW"),
         "soc_min": num(soc_min, 5, 100, 20, "min. SOC %"), "soc_max": num(soc_max, 10, 100, 100, "maks. SOC %"),
         "przylacze_kw": num(przylacze_kw, 1, 100, PRZYLACZE_KW, "przyłącze kW")}
    _cfg_save(manual=m)
    state["manual"] = m
    _latch.pop("manual", None)
    return m


def _pause_end(kind, now=None):
    now = now or time.time()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    return int({"godzina": now // 3600 * 3600 + 3600, "1h": now + 3600, "3h": now + 3 * 3600,
                "polnoc": midnight, "jutro": midnight + 6 * 3600}[kind])


def set_pause(kind):
    """kind: godzina | 1h | 3h | polnoc | jutro | koniec (wznowienie)."""
    if kind == "koniec":
        _cfg_save(pauza=0)
        state["pause_until"] = 0
        state["note"] = "pauza zakończona ręcznie"
        return
    if kind not in PAUZY:
        raise ValueError("pauza: " + " / ".join(list(PAUZY) + ["koniec"]))
    until = _pause_end(kind)
    _cfg_save(pauza=until)
    state["pause_until"] = until
    state["note"] = f"pauza {PAUZY[kind]}"


def set_tryb(t):
    if t not in TRYBY:
        raise ValueError("tryb: wyl / probny / wl")
    _cfg_save(tryb=t)
    old = state["tryb"]
    state["tryb"] = t
    if old != t:
        _log({"kind": "ustawienie", "txt": f"sterowanie wg strategii: {TRYBY[old]} → {TRYBY[t]}", "soc": state.get("soc")})
    state["applied"] = None          # wymus ponowne ustawienie / oddanie


def _state_load():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _owned_load():
    return bool(_state_load().get("owned"))


def _state_save():
    try:
        STATE.write_text(json.dumps({"owned": state["owned"], "last_mode": state.get("last_mode"),
                                     "ts": int(time.time())}), encoding="utf-8")
    except OSError:
        pass


def _owned_save(v):
    state["owned"] = v
    _state_save()


def _transition(to, src, det=None, dry=False):
    """Wpis do dziennika tylko przy zmianie trybu (albo zrodla): czas, poprzedni -> nowy, zrodlo, SOC."""
    last = state.get("last_mode") or {}
    if last.get("to") == to and last.get("src") == src and bool(last.get("dry")) == dry:
        return
    note, state["note"] = state.get("note"), None
    _log({"kind": "zmiana", "from": last.get("to"), "from_src": last.get("src"), "to": to, "src": src,
          "soc": state.get("soc"), "det": "; ".join(x for x in (note, det) if x) or None, "dry": dry})
    state["last_mode"] = {"to": to, "src": src, "dry": dry, "ts": int(time.time())}
    _state_save()


def log_read(n=50):
    try:
        return json.loads(LOG.read_text(encoding="utf-8"))[:n] if LOG.exists() else []
    except Exception:  # noqa: BLE001
        return []


def _log(entry):
    entry = dict(entry, ts=int(time.time()))
    with _lock:
        h = log_read(LOG_MAX)
        h = ([entry] + h)[:LOG_MAX]
        try:
            LOG.write_text(json.dumps(h, ensure_ascii=False, indent=0), encoding="utf-8")
        except OSError:
            pass


# --- powiadomienia -------------------------------------------------------
def _send(subject, body):
    if not _notify:
        return False
    try:
        threading.Thread(target=_notify, args=("Sigen – sterowanie falownikiem: " + subject,
                                               body + "\n\nPodgląd i dziennik: karta Strategia osobista.\n"
                                               "Ręczne oddanie sterowania: przełącznik „wyłączone” albo pauza na stronie, "
                                               "awaryjnie: python tools/inverter_release.py"), daemon=True).start()
        return True
    except Exception:  # noqa: BLE001
        return False


def _problem(kind, active, subject, body, now=None):
    """Problem trwajacy > BAD_AFTER s -> mail (najwyzej co ALERT_GAP); po ustaniu - mail, ze wrocilo do normy."""
    now = now or time.time()
    a = _alerts.get(kind)
    if active:
        if not a:
            _alerts[kind] = a = {"since": now, "sent": None}
        if now - a["since"] >= BAD_AFTER and (a["sent"] is None or now - a["sent"] >= ALERT_GAP):
            if _send(subject, body):
                a["sent"] = now
                _log({"kind": "mail", "txt": "powiadomienie e-mail: " + subject, "why": body[:200]})
    elif a:
        _alerts.pop(kind, None)
        if a["sent"]:
            _send(subject + " – już w porządku", "Problem ustąpił: " + subject + ".\nStan: " + (state.get("reason") or ""))
            _log({"kind": "mail", "txt": "powiadomienie e-mail: " + subject + " – już w porządku"})


# --- decyzja wg planu ----------------------------------------------------
def _mismatch_txt(cmd, f):
    """Opis niezgodnosci przeplywow z trybem albo None."""
    if not cmd or not f or f.get("bat") is None:
        return None
    ems, bat, grid, pv = cmd.get("ems"), float(f["bat"]), float(f.get("grid") or 0), float(f.get("pv") or 0)
    k = MISMATCH_KW
    if ems == 1 and abs(bat) > k:
        return f"standby, a magazyn {'ładuje' if bat > 0 else 'oddaje'} {abs(bat):.1f} kW"
    if ems in (3, 4) and bat < -k:
        return f"{'ładowanie z sieci' if ems == 3 else 'PV do magazynu'}, a magazyn oddaje {-bat:.1f} kW"
    if ems in (5, 6):
        if bat > k:
            return f"sprzedaż z magazynu, a magazyn ładuje {bat:.1f} kW"
        if (cmd.get("discharge_kw") or 0) >= 0.5 and bat > -0.1:
            return f"sprzedaż z magazynu {cmd['discharge_kw']:g} kW, a magazyn nie oddaje energii"
    if ems == 2 and cmd.get("discharge_kw") == 0 and bat < -k:
        return f"samozużycie bez rozładowania, a magazyn oddaje {-bat:.1f} kW (falownik nie respektuje limitu 0 kW?)"
    if ems == 2:
        if bat < -k and grid < -k:
            return f"samozużycie, a magazyn oddaje {-bat:.1f} kW do sieci (eksport {-grid:.1f} kW)"
        if bat > k and grid > k and pv < 0.1:
            return f"samozużycie, a magazyn ładuje się z sieci {bat:.1f} kW"
    return None


def _check_mismatch(now):
    """Kontrola co DECIDE_S: niezgodnosc przeplywow (lub rejestrow) trwajaca >= MISMATCH_AFTER -> ostrzezenie i e-mail."""
    cmd = state.get("desired")
    txt = None
    if state.get("owned") and state.get("write") and cmd and state.get("applied") == _key(cmd) \
            and now - (state.get("applied_ts") or now) >= MISMATCH_SETTLE:
        try:
            txt = _mm["reg"] or _mismatch_txt(cmd, get_flows() if get_flows else None)
        except Exception:  # noqa: BLE001
            txt = None
    if txt:
        _mm["miss"] = 0
        if _mm["since"] is None:
            _mm.update(since=now, logged=False)
        _mm["txt"] = txt
    elif _mm["since"] is not None:
        _mm["miss"] += 1
        if _mm["miss"] >= 2:                        # dwa kolejne odczyty zgodne - koniec niezgodnosci
            if _mm["logged"]:
                _log({"kind": "niezgodnosc", "txt": "falownik znów zgodny z poleceniem", "why": _mm["txt"] or "",
                      "soc": state.get("soc")})
            _mm.update(since=None, txt=None, miss=0, logged=False)
    active = _mm["since"] is not None and now - _mm["since"] >= MISMATCH_AFTER
    if active and not _mm["logged"]:
        _mm["logged"] = True
        _log({"kind": "niezgodnosc", "txt": "falownik robi co innego niż polecenie: " + _txt(cmd), "why": _mm["txt"],
              "soc": state.get("soc"), "ok": False})
    state["mismatch"] = {"since": int(_mm["since"]), "txt": _mm["txt"], "alert": active} if _mm["since"] else None
    _problem("niezgodnosc", active, "falownik robi co innego niż polecenie",
             f"Wysłane polecenie: {_txt(cmd)}\nFalownik: {_mm['txt'] or '?'} (od "
             f"{time.strftime('%H:%M', time.localtime(_mm['since'] or now))}).\n"
             "Najczęstsza przyczyna: tryb ustawiony w aplikacji mySigen (ma pierwszeństwo przed Remote EMS) – "
             "sprawdź aplikację mySigen albo wyłącz tam ręczne polecenie.")


def _plan_hour(data, now):
    for h in data.get("hours") or []:
        if h.get("mode") in (None, "past"):
            continue
        if h["ts"] <= now < h["ts"] + 3600:
            return h
    return None


def _pace(kwh, now, hour_ts, limit):
    """Moc [kW], by zmiescic kwh w pozostalej czesci godziny (z zapasem 15 %), co 0,5 kW, 0,5 kW .. limit."""
    rem_h = max(0.1, (hour_ts + 3600 - now) / 3600)
    kw = math.ceil(kwh / rem_h * 1.15 / 0.5) * 0.5
    return max(0.5, min(limit, kw))


def _steady(kw, mode, hour_ts):
    """Bez przeskakiwania mocy co kilka minut: zwiekszenie od razu (trzeba wiecej), zmniejszenie dopiero o >= 1 kW."""
    prev = state.get("desired")
    if not prev or prev.get("hour") != hour_ts or prev.get("plan") != mode:
        return kw
    pk = prev.get("charge_kw") if mode == "grid" else prev.get("discharge_kw")
    if pk and pk - 1.0 < kw <= pk:
        return pk
    return kw


def _grid_pv(kw):
    """1.99: ladowanie z sieci przy dzialajacym PV. Tryb 3 (najpierw siec) bierze moc ladowania z sieci, a nadwyzki PV
    nie ma gdzie podziac (bez limitu eksportu falownik nie oddaje) - PV jest obcinane. Wtedy tryb 4 (najpierw PV):
    magazyn bierze najpierw PV, brakujaca czesc z sieci; limit nie mniejszy niz biezaca nadwyzka PV.
    Zwraca (ems, kW, dopisek)."""
    try:
        pv = float(get_pv()) if get_pv else None
        load = float(get_load()) if get_load else 0.0
    except Exception:  # noqa: BLE001
        pv, load = None, 0.0
    # zawsze tryb 4: bez slonca dziala jak tryb 3 (calosc z sieci), a w trybie 3 odczyt PV jest juz obciety (nie da sie
    # na nim opierac decyzji o przelaczeniu)
    surplus = max(0.0, (pv or 0.0) - max(load or 0.0, 0.0))
    kw2 = min(P_MAX, max(kw, math.ceil(surplus / 0.5) * 0.5))
    return 4, kw2, (f", najpierw PV ({pv:.1f} kW), reszta z sieci" if pv and pv >= 0.1 else "")


def decide(data, soc, soc_ts, now, limits):
    """(polecenie | None, powod). Polecenie: {"ems", "charge_kw", "discharge_kw", "hour", "plan", "target", "why"}."""
    if not data or not data.get("ok"):
        return None, "brak planu strategii" + (f" ({data.get('error')})" if data and data.get("error") else "")
    if soc is None or not soc_ts or now - soc_ts > STALE_S:
        return None, "brak aktualnego odczytu SOC z falownika"
    h = _plan_hour(data, now)
    if not h:
        return None, "brak bieżącej godziny w planie"
    mode = h.get("mode")
    if mode not in PLAN_TO_EMS:
        return None, f"nieznany tryb planu: {mode}"
    target = h.get("soc")
    p_chg, p_dis = limits if isinstance(limits, (tuple, list)) else (limits, limits)
    cap = float((data.get("params") or {}).get("cap_kwh") or 0)
    cmd = {"hour": h["ts"], "hour_txt": f"{h.get('day', '')} {h.get('t', '')}".strip(), "plan": mode,
           "label": h.get("label"), "target": target, "charge_kw": None, "discharge_kw": None}
    reached = _latch.get(h["ts"])
    if mode == "grid":
        if reached or (target is not None and soc >= target - SOC_TOL):
            _latch[h["ts"]] = True
            cmd.update(ems=1, why=f"ładowanie z sieci – osiągnięty SOC z planu {target:.0f} %, standby do końca godziny")
        else:
            kw = _pace((target - soc) / 100 * cap / ETA, now, h["ts"], p_chg) if cap and target is not None else p_chg
            kw = min(p_chg, _steady(kw, "grid", h["ts"]))
            ems, kw2, note = _grid_pv(kw)
            cmd.update(ems=ems, charge_kw=kw2, why=f"ładowanie z sieci do {target:.0f} % (teraz {soc:.0f} %), "
                                                   f"{kw2:g} kW (limit {p_chg:g} kW){note}")
    elif mode == "pvchg":
        # Remote EMS 4 (wymuszone ladowanie, najpierw PV) z limitem = biezaca moc PV zaokraglona w dol do 0,5 kW:
        # magazyn bierze tylko energie ze slonca (bez dokupowania z sieci), dom zasila siec. Malo PV -> standby.
        try:
            pv = float(get_pv()) if get_pv else 0.0
        except Exception:  # noqa: BLE001
            pv = 0.0
        kw = min(p_chg, math.floor(pv / 0.5) * 0.5)
        if kw < 0.5 or pv < PVCHG_MIN_KW:
            cmd.update(ems=1, label=f"standby – za mało PV do ładowania ({pv:.2f} kW < {PVCHG_MIN_KW:g} kW)".replace(".", ","),
                       why=f"PV do magazynu – za mało PV ({pv:.2f} kW), standby")
        else:
            cmd.update(ems=4, charge_kw=kw, why=f"PV do magazynu {kw:g} kW (PV {pv:.1f} kW), dom z sieci")
    elif mode == "export":
        if reached or (target is not None and soc <= target + SOC_TOL):
            _latch[h["ts"]] = True
            cmd.update(ems=1, why=f"sprzedaż z magazynu – osiągnięty SOC z planu {target:.0f} %, standby do końca godziny")
        else:
            kw = _pace((soc - target) / 100 * cap * ETA, now, h["ts"], p_dis) if cap and target is not None else p_dis
            kw = min(p_dis, _steady(kw, "export", h["ts"]))
            # Tryb 5 (najpierw PV) eksportuje do limitu eksportu na przylaczu (40038) - przy braku limitu falownik
            # 2.11 o 15:00 nie oddawal nic do sieci. Tryb 6 (najpierw magazyn) rozladowuje moca z 40034 - bez PV
            # (wieczor, noc) to wlasciwy tryb sprzedazy; przy PV tryb 6 ograniczalby PV, wiec wtedy zostaje 5.
            pv = None
            try:
                pv = float(get_pv()) if get_pv else None
            except Exception:  # noqa: BLE001
                pv = None
            ems_exp = 6 if pv is None or pv < EXPORT_PV_KW else 5
            cmd.update(ems=ems_exp, discharge_kw=kw, why=f"sprzedaż z magazynu do {target:.0f} % (teraz {soc:.0f} %), "
                                                   f"{kw:g} kW (limit {p_dis:g} kW)")
    else:
        cmd.update(ems=PLAN_TO_EMS[mode], why=h.get("label") or mode)
        if mode == "standby":
            sur = _pv_surplus(now, soc)
            if sur is not None:
                cmd.update(ems=2, plan_override="nodis", label="samozużycie bez rozładowania – PV ponad prognozę do magazynu",
                           why=f"standby z planu, ale nadwyżka PV {sur:.1f} kW (ponad prognozę) – do magazynu zamiast do sieci, "
                               f"magazyn nie oddaje (SOC {soc:.0f} %)".replace(".", ","))
                mode = "nodis"
        else:
            _pv_ovr.update(on=False, since=None)
        if cmd["ems"] == 2:          # samozuzycie: limity ladowania (40032) i rozladowania (40034) na pelna moc
            cmd["charge_kw"], cmd["discharge_kw"] = P_MAX, (0 if mode == "nodis" else P_MAX)
    for k in [k for k in _latch if k < now - 7200]:
        _latch.pop(k, None)
    return cmd, cmd["why"]


def _pv_surplus(now, soc):
    """Nadwyzka PV nad zuzyciem domu [kW] z biezacych mocy, gdy przelaczenie standby -> samozuzycie ma dzialac, inaczej None.
    W standby nadwyzka plynie do sieci (grid < 0), po przelaczeniu - do magazynu (bat > 0), wiec liczone sa oba."""
    try:
        f = get_flows() if get_flows else None
    except Exception:  # noqa: BLE001
        f = None
    if not f or f.get("pv") is None or f.get("grid") is None:
        return None
    pv = float(f.get("pv") or 0)
    sur = max(0.0, -float(f.get("grid") or 0)) + max(0.0, float(f.get("bat") or 0))
    sur = min(sur, pv)
    st = _pv_ovr
    if soc is not None and soc >= PV_OVR_SOC_MAX:
        st.update(on=False, since=None)
        return None
    if not st["on"]:
        if sur >= PV_OVR_ON:
            st["since"] = st["since"] or now
            if now - st["since"] >= PV_OVR_ON_S:
                st.update(on=True, since=None)
        else:
            st["since"] = None
    else:
        if sur < PV_OVR_OFF:
            st["since"] = st["since"] or now
            if now - st["since"] >= PV_OVR_OFF_S:
                st.update(on=False, since=None)
        else:
            st["since"] = None
    return sur if st["on"] else None


def _key(cmd):
    return None if cmd is None else (cmd["ems"], cmd.get("charge_kw"), cmd.get("discharge_kw"))


def _txt(cmd):
    if cmd is None:
        return "oddanie sterowania falownikowi (Remote EMS wyłączone)"
    t = f"Remote EMS tryb {cmd['ems']} – {EMS_TXT.get(cmd['ems'], '?')}"
    if cmd.get("charge_kw") and cmd["ems"] != 2:
        t += f", ładowanie do {cmd['charge_kw']:g} kW"
    if cmd.get("discharge_kw") == 0:
        t += ", bez rozładowania (limit 0 kW)"
    elif cmd.get("discharge_kw") and cmd["ems"] != 2:
        t += f", rozładowanie do {cmd['discharge_kw']:g} kW"
    return t


def _manual_cmd(m, soc, soc_ts, now):
    """Polecenie z trybu recznego (z granicami SOC i limitem przylacza) albo (None, powod)."""
    if soc is None or not soc_ts or now - soc_ts > STALE_S:
        return None, "ręczne: brak aktualnego odczytu SOC – sterowanie oddane falownikowi"
    ems, name = MANUAL[m["tryb"]]
    left = f" (do {time.strftime('%H:%M', time.localtime(m['do']))})" if m["do"] else " (do anulowania)"
    cmd = {"hour": None, "hour_txt": "ręczne", "plan": "manual", "label": name, "target": None,
           "charge_kw": None, "discharge_kw": None, "ems": ems, "why": "ręcznie: " + name + left}
    load = 0.0
    try:
        load = max(0.0, float(get_load() or 0)) if get_load else 0.0
    except Exception:  # noqa: BLE001
        pass
    lim = m.get("przylacze_kw") or PRZYLACZE_KW
    reached = _latch.get("manual") == m["od"]
    if m["tryb"] == "grid":
        if reached or soc >= m["soc_max"] - SOC_TOL:
            _latch["manual"] = m["od"]
            cmd.update(ems=1, why=f"ręcznie: ładowanie – osiągnięty SOC {m['soc_max']:g} %, standby" + left)
        else:
            kw = max(0.5, min(m["kw"], P_MAX, lim - load))          # pobor z sieci = dom + ladowanie <= przylacze
            ems, kw2, note = _grid_pv(round(kw * 2) / 2)
            cmd.update(ems=ems, charge_kw=kw2, why=f"ręcznie: ładowanie z sieci {kw2:g} kW do {m['soc_max']:g} %{note}" + left)
    elif m["tryb"] in ("auto", "auto_nodis"):
        # samozuzycie: PV najpierw do domu, nadwyzka do magazynu; auto_nodis - limit rozladowania 0 kW (40034),
        # brakujaca moc z sieci zamiast z magazynu
        nod = m["tryb"] == "auto_nodis"
        # limit ladowania 40032 na pelna moc - po "PV do magazynu" zostawal np. 0,5 kW i nadwyzka PV szla do sieci
        cmd.update(charge_kw=P_MAX, discharge_kw=0 if nod else P_MAX,
                   why=("ręcznie: samozużycie bez rozładowania – PV do domu, nadwyżka do magazynu, brak z sieci" if nod
                        else "ręcznie: samozużycie") + left)
    elif m["tryb"] == "pvchg":
        # jak w planie: limit ladowania = biezace PV w dol do 0,5 kW (bez dokupowania z sieci), maks. moc z pola "moc"
        try:
            pv = float(get_pv()) if get_pv else 0.0
        except Exception:  # noqa: BLE001
            pv = 0.0
        if reached or soc >= m["soc_max"] - SOC_TOL:
            _latch["manual"] = m["od"]
            cmd.update(ems=1, why=f"ręcznie: PV do magazynu – osiągnięty SOC {m['soc_max']:g} %, standby" + left)
        else:
            kw = min(m["kw"] or P_MAX, P_MAX, math.floor(pv / 0.5) * 0.5)
            if kw < 0.5:
                cmd.update(ems=1, why=f"ręcznie: PV do magazynu – za mało PV ({pv:.2f} kW), chwilowo standby" + left)
            else:
                cmd.update(charge_kw=kw, why=f"ręcznie: PV do magazynu {kw:g} kW (PV {pv:.1f} kW) do {m['soc_max']:g} %, dom z sieci" + left)
    elif m["tryb"] == "discharge":
        if reached or soc <= m["soc_min"] + SOC_TOL:
            _latch["manual"] = m["od"]
            cmd.update(ems=1, why=f"ręcznie: rozładowanie – osiągnięty min. SOC {m['soc_min']:g} %, standby" + left)
        else:
            kw = max(0.5, min(m["kw"], P_MAX, lim + load))          # oddanie do sieci = rozladowanie - dom <= przylacze
            cmd.update(discharge_kw=round(kw * 2) / 2, why=f"ręcznie: rozładowanie {round(kw * 2) / 2:g} kW do min. {m['soc_min']:g} %" + left)
    return cmd, cmd["why"]


def decide_loop(get_plan, get_soc, get_limits):
    """Watek: co DECIDE_S s ustala polecenie. Priorytet: reczne > pauza > strategia. get_soc() -> (soc %, ts odczytu),
    get_limits() -> (maks. moc ladowania z sieci, maks. moc rozladowania przy sprzedazy) [kW]."""
    state["tryb"] = _read_tryb()
    st = _state_load()
    state["owned"] = bool(st.get("owned"))
    state["last_mode"] = st.get("last_mode")
    while True:
        tick(get_plan, get_soc, get_limits)
        time.sleep(DECIDE_S)


_tick_lock = threading.Lock()
START = time.time()
STARTUP_HOLD_S = 120   # s - po starcie aplikacji, zanim przyjdzie pierwszy odczyt SOC: nic nie zmieniamy (bez oddawania)


def tick(get_plan=None, get_soc=None, get_limits=None):
    """Jedna decyzja (wolana z decide_loop co DECIDE_S i od razu po zmianie na stronie)."""
    with _tick_lock:
        _tick(get_plan, get_soc, get_limits)


def _tick(get_plan=None, get_soc=None, get_limits=None, _fns={}):
    if get_plan:
        _fns.update(plan=get_plan, soc=get_soc, limits=get_limits)
    if "plan" not in _fns:
        return
    now = time.time()
    soc = soc_ts = None
    paused = False
    man = None
    src = "strategia"
    try:
        state["tryb"], pz = _cfg()
        if pz and pz <= now:                      # koniec pauzy - automat wraca sam
            _cfg_save(pauza=0)
            pz = 0
            state["note"] = "koniec pauzy"
        state["pause_until"] = pz
        man = _manual_read()
        if man and man["do"] and man["do"] <= now:  # koniec recznego - powrot do strategii
            _cfg_save(manual=None)
            man = None
            state["note"] = "koniec ręcznego"
        state["manual"] = man
        soc, soc_ts = _fns["soc"]()
        state["soc"] = None if soc is None else round(soc, 1)
        if man:
            src = "ręczne"
            cmd, why = _manual_cmd(man, soc, soc_ts, now)
        elif state["tryb"] == "wyl":
            src = "wyłączone"
            cmd, why = None, "sterowanie wyłączone"
        elif pz > now:
            paused, src = True, "pauza"
            cmd, why = None, f"pauza do {time.strftime('%d.%m %H:%M', time.localtime(pz))} – sterowanie oddane falownikowi"
        else:
            if soc is None and now - START < STARTUP_HOLD_S:
                state.update(hold=True, reason="start aplikacji – czekam na pierwszy odczyt z falownika", decided_ts=int(now))
                return
            cmd, why = decide(_fns["plan"](), soc, soc_ts, now, _fns["limits"]())
            if cmd is None:
                src = "brak danych"
    except Exception as e:  # noqa: BLE001
        tb = traceback.extract_tb(e.__traceback__)
        where = " ← ".join(f"{fr.filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{fr.lineno} {fr.name}" for fr in tb[-3:][::-1])
        cmd, why, src = None, f"błąd planu: {type(e).__name__}: {e} ({where})", "błąd"
    if cmd:
        cmd["src"] = src
    state["hold"] = False
    write = bool(man) or state["tryb"] == "wl"
    state.update(desired=cmd, reason=why, decided_ts=int(now), src=src, write=write)
    on = (state["tryb"] == "wl" and not paused) or bool(man)
    _problem("oddane", on and cmd is None, "sterowanie oddane falownikowi",
             f"Automat nie ma polecenia i oddał sterowanie falownikowi (wraca tryb z mySigen).\nPowód: {why}")
    stale = soc_ts is None or now - soc_ts > STALE_S
    _problem("brak_odczytu", state["owned"] and stale, "brak odczytu z falownika",
             "Od ponad 3 minut brak odczytu z falownika, a automat sterował falownikiem – nie da się oddać sterowania,"
             f" falownik może pracować dalej w ostatnim trybie: {state.get('last_txt') or '?'}.\n"
             "Sprawdź połączenie (Wi-Fi / falownik) albo przestaw tryb w aplikacji mySigen.")
    _check_mismatch(now)
    if not write and state["tryb"] == "probny":   # proba: zmiany trybu w dzienniku, bez zapisu
        _transition(cmd["ems"] if cmd else 0, src, why, dry=True)


# --- zapis (wolane z watku odczytow Modbus - to samo polaczenie) ----------
def _u32(v):
    v = int(round(v))
    return [(v >> 16) & 0xFFFF, v & 0xFFFF]


def _check(rr, what):
    if rr is None or rr.isError():
        raise IOError(f"zapis {what}: {rr}")


def _release(client, write, why, src=None):
    _check(write(client, REG_ENABLE, [0]), "40029=0")
    _owned_save(False)
    state.update(applied=None, applied_ts=int(time.time()), verify=None, error=None, last_txt=_txt(None))
    _problem("zapis", False, "błąd zapisu do falownika", "")
    _transition(0, src or state.get("src") or "—", why)


def apply(client, write, read_holding):
    """Wolane co odczyt (ok. 5 s) w watku Modbus. write(client, addr, [wartosci]) / read_holding(client, addr, n)."""
    tryb, cmd = state["tryb"], state["desired"]
    now = time.time()
    try:
        if state.get("hold"):                   # start aplikacji - falownik zostaje w dotychczasowym trybie
            return
        if not state.get("write"):
            if state["owned"]:
                _release(client, write, f"sterowanie: {TRYBY[tryb]}")
            return
        if cmd is None:
            if state["owned"]:
                _release(client, write, state.get("reason") or "brak polecenia")
            return
        key = _key(cmd)
        same = state["applied"] == key
        if same and now - (state["applied_ts"] or 0) < REASSERT_S:
            if now - _mm["reg_ts"] >= REG_CHECK_S:      # odczyt kontrolny: czy ktos nie zmienil rejestrow Remote EMS
                _mm["reg_ts"] = now
                try:
                    r1, r2 = read_holding(client, REG_ENABLE, 1), read_holding(client, REG_MODE, 1)
                    if not r1.isError() and not r2.isError():
                        en, md = r1.registers[0], r2.registers[0]
                        _mm["reg"] = None if (en == 1 and md == cmd["ems"]) else \
                            f"rejestry zmienione poza aplikacją: Remote EMS {en}, tryb {md} (wysłano tryb {cmd['ems']})"
                        state["verify"] = {"enable": en, "mode": md}
                except Exception:  # noqa: BLE001
                    pass
            return
        _owned_save(True)              # przed zapisem - przy czesciowym bledzie i tak oddamy sterowanie
        if cmd.get("charge_kw") is not None:
            _check(write(client, REG_CHG, _u32(cmd["charge_kw"] * 1000)), "40032")
        if cmd.get("discharge_kw") is not None:
            _check(write(client, REG_DIS, _u32(cmd["discharge_kw"] * 1000)), "40034")
        _check(write(client, REG_MODE, [cmd["ems"]]), f"40031={cmd['ems']}")
        _check(write(client, REG_ENABLE, [1]), "40029=1")
        ver = None
        try:
            r1, r2 = read_holding(client, REG_ENABLE, 1), read_holding(client, REG_MODE, 1)
            if not r1.isError() and not r2.isError():
                ver = {"enable": r1.registers[0], "mode": r2.registers[0]}
        except Exception:  # noqa: BLE001
            pass
        ok = ver is None or (ver["enable"] == 1 and ver["mode"] == cmd["ems"])
        _mm.update(reg=None, reg_ts=now)
        state.update(applied=key if ok else None, applied_ts=int(now), verify=ver, last_txt=_txt(cmd), error=None if ok else
                     f"odczyt kontrolny niezgodny: Remote EMS {ver['enable']}, tryb {ver['mode']}")
        _problem("zapis", not ok, "błąd zapisu do falownika", state.get("error") or "")
        if ok:
            _transition(cmd["ems"], cmd.get("src") or state.get("src") or "strategia", cmd.get("why"))
        elif not same:
            _log({"kind": "blad", "txt": "odczyt kontrolny niezgodny po zapisie: " + _txt(cmd), "why": state.get("error"),
                  "ok": False, "verify": ver})
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"[:200]
        if state.get("error") != msg:
            _log({"kind": "blad", "txt": "błąd zapisu do falownika", "why": msg, "ok": False})
        state.update(error=msg, applied=None)   # ponowienie przy nastepnym odczycie
        _problem("zapis", True, "błąd zapisu do falownika",
                 f"Nie udało się zapisać polecenia do falownika: {msg}\nPolecenie: {_txt(cmd)}\n"
                 f"Ostatnio ustawione: {state.get('last_txt') or '?'}")


def release_now(make_client, write, why="zamknięcie aplikacji"):
    """Oddanie sterowania przy wylaczaniu aplikacji (osobne polaczenie). Tylko gdy sterowalismy."""
    if not (state.get("owned") or _owned_load()):
        return
    try:
        c = make_client()
        if c.connect():
            _release(c, write, why, src="zamknięcie aplikacji")
        c.close()
    except Exception as e:  # noqa: BLE001
        _log({"kind": "blad", "txt": "nie udało się oddać sterowania przy zamknięciu", "why": str(e)[:200], "ok": False})


def durations(window_s, now=None):
    """Czas w kazdym trybie w ostatnim oknie [s] - z roznic miedzy kolejnymi wpisami zmian (bez prob)."""
    now = now or time.time()
    start = now - window_s
    ev = sorted((e for e in log_read(LOG_MAX) if e.get("kind") == "zmiana" and not e.get("dry")), key=lambda e: e["ts"])
    out = {}
    for i, e in enumerate(ev):
        t0 = max(e["ts"], start)
        t1 = ev[i + 1]["ts"] if i + 1 < len(ev) else now
        if t1 <= start:
            continue
        name = MODE_NAME.get(e["to"], str(e["to"]))
        out[name] = out.get(name, 0) + (t1 - t0)
    return {k: int(v) for k, v in sorted(out.items(), key=lambda x: -x[1])}


def status():
    d = {k: state.get(k) for k in ("tryb", "reason", "applied_ts", "error", "decided_ts", "verify", "owned",
                                   "pause_until", "last_txt", "src", "soc", "manual", "last_mode", "mismatch")}
    d["manual_modes"] = {k: v[1] for k, v in MANUAL.items()}
    d["mode_names"] = MODE_NAME
    d["p_max"] = P_MAX
    d["przylacze_kw"] = PRZYLACZE_KW
    d["czas_24h"] = durations(86400)
    d["czas_7d"] = durations(7 * 86400)
    d["pauzy"] = PAUZY
    d["alerts"] = sorted(k for k, a in _alerts.items() if a.get("sent"))
    d["mail"] = _notify is not None
    cmd = state.get("desired")
    d["tryb_txt"] = TRYBY.get(d["tryb"], d["tryb"])
    d["desired"] = None if cmd is None else dict(cmd, txt=_txt(cmd))
    d["desired_txt"] = _txt(cmd) if d["tryb"] != "wyl" else None
    d["applied"] = state["applied"] is not None and state["applied"] == _key(cmd)
    d["log"] = log_read(30)
    return d
