"""
Strategia osobista - plan pracy magazynu (wykonanie: inverter_ctl.py, gdy sterowanie wlaczone na stronie).

Plan godzinowy pracy magazynu od biezacej godziny do konca jutra, minimalizujacy koszt energii z sieci:
  - autokonsumpcja   - jak dzis: nadwyzka PV laduje magazyn, niedobor pokrywa magazyn (Remote EMS tryb 2),
  - standby          - magazyn czeka, dom bierze prad z sieci (tryb 1) - np. w tanich godzinach nocnych zima,
  - ladowanie z sieci - doladowanie w najtanszych godzinach na drogie godziny (tryb 3),
  - sprzedaz z magazynu - rozladowanie do sieci w drogich godzinach (tryb 5: wymuszone rozladowanie, najpierw PV),
    tylko gdy cena sprzedazy >= pulap ustawiony przez uzytkownika (SELL_FLOOR).
Pulap zakupu (BUY_CAP): ladowanie z sieci tylko w godzinach z cena zakupu <= pulap.
Dla kazdej godziny wybierany jest jeden z trybow (programowanie dynamiczne po stanie magazynu),
z uwzglednieniem sprawnosci ladowania/rozladowania, prognozy PV, typowego zuzycia domu i cen godzinowych.
Porownanie: ta sama prognoza i ceny przy samej autokonsumpcji (bez strategii).
"""
import math

TIE_EPS = 0.003   # zl/kWh - rozstrzyganie remisow na korzysc prostszego planu
SPREAD_EPS = 0.001  # zl/kWh^2 - przy rownych cenach rozklada ladowanie / sprzedaz rowno (mniejsza moc = lagodniej)
SWITCH_COST = 0.03  # zl - "koszt" zmiany trybu (mniej przelaczen)

MODES = {
    "auto": ("autokonsumpcja", 2),
    "standby": ("standby – dom z sieci", 1),
    "grid": ("ładowanie z sieci", 3),
    "export": ("sprzedaż z magazynu", 5),
}
SELL_FLOOR = None   # zl/kWh - sprzedaz z magazynu tylko przy cenie sprzedazy >= pulap (None = bez sprzedazy z magazynu)
BUY_CAP = None      # zl/kWh - ladowanie z sieci tylko przy cenie zakupu <= pulap (None = bez ograniczenia)
P_CHARGE = None     # kW - maks. moc ladowania magazynu z sieci (None = p_max) - oszczedzanie baterii
P_DISCHARGE = None  # kW - maks. moc rozladowania magazynu przy sprzedazy (None = p_max)
# Sprawnosc rozladowania przy malej mocy (z pomiarow falownika): [(od kW, do kW, sprawnosc)]. Przy mocy kilkuset W
# straty falownika sa duze (np. 300 W do domu = 400 W z magazynu) - plan widzi, ze taka energia jest drozsza.
ETA_D_CURVE = None
MIN_DISCHARGE = None  # kW - gdy dom pobiera mniej (minus PV), zamiast autokonsumpcji standby (dom z sieci)


def eta_at(p, default):
    """Sprawnosc rozladowania przy mocy p [kW] wg ETA_D_CURVE (powyzej krzywej - default)."""
    if ETA_D_CURVE and p > 0:
        for lo, hi, e in ETA_D_CURVE:
            if lo <= p < hi:
                return min(e, default)
    return default


def _auto_d(load, pv, stored, s_min, s_max, p_max, eta_c, eta_d, dur=1.0):
    """Przeplyw AC do magazynu [kWh] w trybie autokonsumpcji (+ ladowanie, - rozladowanie)."""
    net = pv - load
    if net >= 0:
        return min(net, p_max, max(0.0, (s_max - stored) / eta_c))
    return -min(-net, p_max, max(0.0, (stored - s_min) * eta_at(-net / dur, eta_d)))


def _apply(stored, d, eta_c, eta_d, dur=1.0):
    return stored + (d * eta_c if d > 0 else d / eta_at(-d / dur, eta_d))


WEAR = 0.0   # zl za kWh oddana z magazynu (zuzycie baterii) - ustawiane z aplikacji


def _cost(grid, buy, sell, d=0.0):
    """Koszt godziny: zakup (+) / sprzedaz (-) + zuzycie baterii przy rozladowaniu."""
    return (grid * buy if grid > 0 else grid * sell) + (WEAR * -d if d < 0 else 0.0)


def baseline(hours, stored, s_min, s_max, p_max, eta_c, eta_d):
    """Sama autokonsumpcja (dzisiejsze zachowanie bez strategii)."""
    out = []
    for h in hours:
        dur = h.get("dur", 1.0)
        d = _auto_d(h["load"], h["pv"], stored, s_min, s_max, p_max, eta_c, eta_d, dur)
        stored = _apply(stored, d, eta_c, eta_d, dur)
        grid = h["load"] - h["pv"] + d
        out.append({"d": d, "grid": grid, "stored": stored, "cost": _cost(grid, h["buy"], h["sell"], d)})
    return out


def plan(hours, stored0, s_min, s_max, p_max, eta_c=0.95, eta_d=0.95, terminal_price=0.0, step=None, mode0="auto"):
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
    p_chg = min(p_max, P_CHARGE) if P_CHARGE else p_max          # limity mocy ustawione przez uzytkownika
    p_dis = min(p_max, P_DISCHARGE) if P_DISCHARGE else p_max
    grid_levels = max(1, int(math.ceil(p_chg / 0.5)))  # moce ladowania z sieci co ~0.5 kW
    ex_levels = max(1, int(math.ceil(p_dis / 1.0)))    # sprzedaz co ~1 kW (szybsze liczenie)
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
        dur = h.get("dur", 1.0)
        low = MIN_DISCHARGE is not None and 0 < (load - pv) / dur < MIN_DISCHARGE   # maly pobor - tylko standby
        surplus = max(pv - load, 0.0)
        for i in range(n + 1):
            s = levels[i]
            opts = [(1, 0.0)] if low else [(0, _auto_d(load, pv, s, s_min, s_max, p_max, eta_c, eta_d, dur)), (1, 0.0)]
            room = max(0.0, (s_max - s) / eta_c)
            top = min(p_chg, room)
            if top > surplus + 0.05 and (BUY_CAP is None or buy <= BUY_CAP):
                for k in range(1, grid_levels + 1):
                    opts.append((2, surplus + (top - surplus) * k / grid_levels))
            if SELL_FLOOR is not None and sell >= SELL_FLOOR:
                avail = min(p_dis, max(0.0, (s - s_min) * eta_d))     # maks. rozladowanie (dom + siec)
                need = max(load - pv, 0.0)                              # tyle i tak zabralby dom
                if avail > need + 0.05:
                    for k in range(1, ex_levels + 1):
                        opts.append((3, -(need + (avail - need) * k / ex_levels)))
            evals = []
            for mi, d in opts:
                j = idx(_apply(s, d, eta_c, eta_d, dur))
                grid = load - pv + d
                c = _cost(grid, buy, sell, d)
                if mi == 2:
                    c += TIE_EPS * (d - surplus) + SPREAD_EPS * (d - surplus) ** 2
                elif mi == 3:
                    c += TIE_EPS * (-d) + SPREAD_EPS * d * d
                elif mi == 1:
                    c += TIE_EPS * 0.5 * dur
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
    # tryb, w ktorym falownik pracuje teraz (sterowanie) - zmiana kosztuje SWITCH_COST, wiec plan nie przeskakuje
    # na koniec godziny (np. standby -> autokonsumpcja na ostatnie minuty) ani przy drobnych zmianach prognozy
    i, mp = idx(stored0), names.index(mode0) if mode0 in names else 0
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
    if step["mode"] == "export":
        out = max(0.0, -step["d"] - max(h["load"] - h["pv"], 0.0))
        return f"sprzedaż z magazynu {out:.1f} kWh"
    if step["mode"] == "grid" and step["d"] - surplus >= 0.05:
        return f"ładowanie z sieci {max(step['d'] - surplus, 0):.1f} kWh"
    if step["mode"] == "grid" and step["d"] - surplus > 0.005:
        return "ładowanie z sieci < 0,1 kWh"
    if step["mode"] == "standby" and MIN_DISCHARGE is not None and 0 < h["load"] - h["pv"] < MIN_DISCHARGE * h.get("dur", 1.0):
        return "standby – mały pobór (straty falownika)"
    if step["mode"] == "standby":
        if h["pv"] > h["load"] + 0.05:
            return "standby – PV na sprzedaż"
        return "standby – dom z sieci" if h["load"] > h["pv"] + 0.02 else "standby"
    if step["d"] > 0.02:
        return "magazyn z PV"
    if step["d"] < -0.02:
        return "dom z magazynu"
    return "autokonsumpcja"
