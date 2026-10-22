"""
Strategia osobista - SYMULACJA (nic nie jest wysylane do falownika).

Plan godzinowy pracy magazynu od biezacej godziny do konca jutra, minimalizujacy koszt energii z sieci:
  - autokonsumpcja   - jak dzis: nadwyzka PV laduje magazyn, niedobor pokrywa magazyn (Remote EMS tryb 2),
  - standby          - magazyn czeka, dom bierze prad z sieci (tryb 1) - np. w tanich godzinach nocnych zima,
  - ladowanie z sieci - doladowanie w najtanszych godzinach na drogie godziny (tryb 3).
Dla kazdej godziny wybierany jest jeden z trybow (programowanie dynamiczne po stanie magazynu),
z uwzglednieniem sprawnosci ladowania/rozladowania, prognozy PV, typowego zuzycia domu i cen godzinowych.
Porownanie: ta sama prognoza i ceny przy samej autokonsumpcji (bez strategii).
"""
import math

TIE_EPS = 0.003   # zl/kWh - rozstrzyganie remisow na korzysc prostszego planu
SWITCH_COST = 0.03  # zl - "koszt" zmiany trybu (mniej przelaczen)

MODES = {
    "auto": ("autokonsumpcja", 2),
    "standby": ("standby – dom z sieci", 1),
    "grid": ("ładowanie z sieci", 3),
}


def _auto_d(load, pv, stored, s_min, s_max, p_max, eta_c, eta_d):
    """Przeplyw AC do magazynu [kWh] w trybie autokonsumpcji (+ ladowanie, - rozladowanie)."""
    net = pv - load
    if net >= 0:
        return min(net, p_max, max(0.0, (s_max - stored) / eta_c))
    return -min(-net, p_max, max(0.0, (stored - s_min) * eta_d))


def _apply(stored, d, eta_c, eta_d):
    return stored + (d * eta_c if d > 0 else d / eta_d)


WEAR = 0.0   # zl za kWh oddana z magazynu (zuzycie baterii) - ustawiane z aplikacji


def _cost(grid, buy, sell, d=0.0):
    """Koszt godziny: zakup (+) / sprzedaz (-) + zuzycie baterii przy rozladowaniu."""
    return (grid * buy if grid > 0 else grid * sell) + (WEAR * -d if d < 0 else 0.0)


def baseline(hours, stored, s_min, s_max, p_max, eta_c, eta_d):
    """Sama autokonsumpcja (dzisiejsze zachowanie bez strategii)."""
    out = []
    for h in hours:
        d = _auto_d(h["load"], h["pv"], stored, s_min, s_max, p_max, eta_c, eta_d)
        stored = _apply(stored, d, eta_c, eta_d)
        grid = h["load"] - h["pv"] + d
        out.append({"d": d, "grid": grid, "stored": stored, "cost": _cost(grid, h["buy"], h["sell"], d)})
    return out


def plan(hours, stored0, s_min, s_max, p_max, eta_c=0.95, eta_d=0.95, terminal_price=0.0, step=None):
    """hours: [{load, pv, buy, sell}] (kWh i zl/kWh w danej godzinie). Zwraca liste krokow planu.

    Stan: energia w magazynie (siatka ok. 0,25 % pojemnosci) i poprzedni tryb
    (kazda zmiana trybu kosztuje SWITCH_COST, zeby plan nie przelaczal falownika co godzine).
    """
    span = max(s_max - s_min, 0.1)
    step = step or max(0.02, span / 400)
    n = int(span / step + 1e-9)
    levels = [s_min + i * step for i in range(n + 1)]
    # zaokraglenie do najblizszego poziomu (zaokraglanie w dol "gubilo" energie co godzine i zanizalo oplacalnosc magazynu)
    idx = lambda s: min(n, max(0, int(round((s - s_min) / step))))  # noqa: E731
    grid_levels = max(1, int(math.ceil(p_max / 0.5)))  # moce ladowania z sieci co ~0.5 kW
    names = list(MODES)
    M = len(names)

    T = len(hours)
    INF = float("inf")
    V = [[[0.0] * M for _ in range(n + 1)] for _ in range(T + 1)]
    for i in range(n + 1):  # energia zostawiona na koniec - wyceniana ostroznie (tania energia z sieci)
        for m in range(M):
            V[T][i][m] = -(levels[i] - s_min) * eta_d * terminal_price
    choice = [[[None] * M for _ in range(n + 1)] for _ in range(T)]

    for t in range(T - 1, -1, -1):
        h = hours[t]
        load, pv, buy, sell = h["load"], h["pv"], h["buy"], h["sell"]
        surplus = max(pv - load, 0.0)
        for i in range(n + 1):
            s = levels[i]
            opts = [(0, _auto_d(load, pv, s, s_min, s_max, p_max, eta_c, eta_d)), (1, 0.0)]
            room = max(0.0, (s_max - s) / eta_c)
            top = min(p_max, room)
            if top > surplus + 0.05:
                for k in range(1, grid_levels + 1):
                    opts.append((2, surplus + (top - surplus) * k / grid_levels))
            evals = []
            for mi, d in opts:
                j = idx(_apply(s, d, eta_c, eta_d))
                grid = load - pv + d
                c = _cost(grid, buy, sell, d)
                if mi == 2:
                    c += TIE_EPS * (d - surplus)
                elif mi == 1:
                    c += TIE_EPS * 0.5
                evals.append((mi, d, j, c))
            for mp in range(M):
                best, bc = INF, None
                for mi, d, j, c in evals:
                    tot = c + V[t + 1][j][mi] + (SWITCH_COST if mi != mp else 0.0)
                    if tot < best - 1e-9:
                        best, bc = tot, (mi, d, j)
                V[t][i][mp] = best
                choice[t][i][mp] = bc

    out = []
    i, mp = idx(stored0), 0          # zaczynamy od autokonsumpcji (stan obecny)
    for t, h in enumerate(hours):
        mi, d, j = choice[t][i][mp]
        grid = h["load"] - h["pv"] + d
        out.append({"mode": names[mi], "d": d, "grid": grid, "stored": levels[j],
                    "cost": _cost(grid, h["buy"], h["sell"], d)})
        i, mp = j, mi
    return out


def label(step, h):
    """Opis kroku dla uzytkownika."""
    surplus = max(h["pv"] - h["load"], 0.0)
    if step["mode"] == "grid" and step["d"] - surplus >= 0.05:
        return f"ładowanie z sieci {max(step['d'] - surplus, 0):.1f} kWh"
    if step["mode"] == "standby":
        if h["pv"] > h["load"] + 0.05:
            return "standby – PV na sprzedaż"
        return "standby – dom z sieci" if h["load"] > h["pv"] + 0.02 else "standby"
    if step["d"] > 0.02:
        return "magazyn z PV"
    if step["d"] < -0.02:
        return "dom z magazynu"
    return "autokonsumpcja"
