"""
Zaniki sieci (praca wyspowa / off-grid) z rejestru 30009 instalacji Sigen (0 = praca z siecia,
1 = off-grid automatycznie - zanik sieci, 2 = off-grid recznie). Odczyt co 5 s razem z mocami.

Historia w grid_events.json: [{"start", "end", "mode", "source", "approx"}] (najnowsze pierwsze, max 300).
Zanik, ktory zaczal lub skonczyl sie, gdy aplikacja nie dzialala, ma "approx": true.
Opcjonalnie powiadomienie: _notify(subject, body) ustawiane przez aplikacje (wersja publiczna - brak).
"""
import datetime as dt
import json
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
EVENTS = BASE / "grid_events.json"
MODES = {0: "praca z siecią", 1: "off-grid – zanik sieci", 2: "off-grid – włączony ręcznie"}
MIN_EVENT_S = 0          # kazda zmiana trybu jest zapisywana

state = {"mode": None, "since": None, "ts": None}
_lock = threading.Lock()
_notify = None           # funkcja(subject, body) ustawiana przez app.py


def _load():
    try:
        return json.loads(EVENTS.read_text(encoding="utf-8")) if EVENTS.exists() else []
    except Exception:  # noqa: BLE001
        return []


def _save(ev):
    EVENTS.write_text(json.dumps(ev[:300], ensure_ascii=False, indent=1), encoding="utf-8")


def _hm(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _dur(s):
    s = int(s)
    return f"{s // 3600} h {s % 3600 // 60} min" if s >= 3600 else f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


def _mail(subject, body):
    if not _notify:
        return

    def run():
        for _ in range(6):
            try:
                _notify(subject, body)
                return
            except Exception:  # noqa: BLE001 - np. brak internetu w czasie zaniku
                time.sleep(300)
    threading.Thread(target=run, daemon=True).start()


def update(mode, ts):
    """Wywolywane po kazdym odczycie rejestru 30009."""
    with _lock:
        prev = state["mode"]
        if prev == mode:
            state["ts"] = ts
            return
        ev = _load()
        open_ev = ev[0] if ev and ev[0].get("end") is None else None
        if mode != 0:                                   # przejscie w off-grid
            if open_ev is None:
                ev.insert(0, {"start": ts, "end": None, "mode": mode, "source": "falownik",
                              "approx": prev is None})  # prev None = aplikacja wlasnie wystartowala
                _save(ev)
                if prev is not None:
                    _mail("⚡ Zanik sieci – dom na zasilaniu z magazynu",
                          f"Instalacja przeszła w pracę bez sieci ({MODES.get(mode, mode)}) o {_hm(ts)}.\n"
                          "Dom zasilany z magazynu i PV (obwody rezerwowe).\n\n— Sigen Dashboard")
            state["since"] = open_ev["start"] if open_ev else ts
        else:                                           # powrot sieci
            if open_ev is not None:
                open_ev["end"] = ts
                if prev is None:
                    open_ev["approx"] = True            # koniec w czasie, gdy aplikacja nie dzialala
                _save(ev)
                if prev is not None:
                    _mail("✓ Sieć wróciła",
                          f"Zasilanie z sieci przywrócone o {_hm(ts)}; przerwa od {_hm(open_ev['start'])} "
                          f"({_dur(ts - open_ev['start'])}).\n\n— Sigen Dashboard")
            state["since"] = None
        state.update(mode=mode, ts=ts)


def status():
    with _lock:
        s = dict(state)
    ev = _load()
    last = next((e for e in ev if e.get("end")), None)
    return {"grid_mode": s["mode"], "grid_mode_text": MODES.get(s["mode"]), "offgrid": bool(s["mode"]),
            "offgrid_since": s["since"], "last_outage": last}


def events(limit=50):
    return _load()[:limit]
