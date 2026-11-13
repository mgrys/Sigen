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
# 1.107: lagodne ladowanie z sieci - kazda kWh ladowana z moca powyzej RATE_SOFT_KW kosztuje RATE_COST zl. Gdy jest czas
# (kilka godzin o podobnej cenie), plan rozklada ladowanie na dluzej mniejszym pradem (np. 10 kWh w 3 h zamiast w 1 h);
# gdy tania jest tylko jedna godzina (roznica cen > RATE_COST), nadal laduje szybko.
RATE_SOFT_KW = 3.5   # kW (app.py: 0,2 C pojemnosci magazynu, min. 2 kW)
RATE_COST = 0.04     # zl za kWh ponad lagodna moc

MODES = {
    "auto": ("autokonsumpcja", 2),
    "standby": ("standby – dom z sieci", 1),
    "grid": ("ładowanie z sieci", 3),
    "export": ("sprzedaż z magazynu", 5),
    "pvchg": ("PV do magazynu, dom z sieci", 4),   # tylko z Tarcza: cale PV do magazynu (pod sprzedaz), dom z sieci
    "nodis": ("samozużycie bez rozładowania", 2),  # EMS 2 + limit rozladowania 0: PV do domu, nadwyzka do magazynu, brak z sieci
}
PV_NODIS_KW = 0.5   # 1.28: kW sredniej PV w godzinie - od tej mocy standby z planu zamieniane na samozuzycie bez rozladowania
SELL_FLOOR = None   # zl/kWh - sprzedaz z magazynu tylko przy cenie sprzedazy >= pulap (None = bez sprzedazy z magazynu)
BUY_CAP = None      # zl/kWh - ladowanie z sieci tylko przy cenie zakupu <= pulap (None = bez ograniczenia)
# Tryb auto (1.89): sprzedaz z magazynu w kazdej godzinie, w ktorej plan po kosztach (sprawnosc, zuzycie baterii, wartosc
# energii zostawionej / rezerwy) na tym zyskuje co najmniej AUTO_MARGIN zl na kWh oddana z magazynu do sieci.
# SELL_FLOOR / BUY_CAP dzialaja wtedy jako bezpieczniki (nie sprzedawaj ponizej / nie kupuj powyzej), puste = bez.
AUTO = False
AUTO_MARGIN = 0.0
P_CHARGE = None     # kW - maks. moc ladowania magazynu z sieci (None = p_max) - oszczedzanie baterii
P_DISCHARGE = None  # kW - maks. moc rozladowania magazynu przy sprzedazy (None = p_max)
# Sprawnosc rozladowania przy malej mocy (z pomiarow falownika): [(od kW, do kW, sprawnosc)]. Przy mocy kilkuset W
# straty falownika sa duze (np. 300 W do domu = 400 W z magazynu) - plan widzi, ze taka energia jest drozsza.
ETA_D_CURVE = None
# Sprawnosc ladowania magazynu przy malej mocy ladowania (z pomiarow): [(od kW, do kW, sprawnosc)] - stale straty falownika
# i BMS / przetwornic magazynu sa przy kilkuset W duza czescia energii, wiec slabe PV lepiej oddac do domu.
ETA_C_CURVE = None
# Rezerwa pod sprzedaz (Tarcza): energia w magazynie do RESERVE_KWH na koniec planu wyceniana po RESERVE_PRICE
# (ostroznie liczona przyszla sprzedaz) zamiast po najtanszym zakupie - plan laduje magazyn (PV, tanio z sieci)
# do rezerwy i trzyma ja na drogie godziny, takze poza horyzontem planu.
RESERVE_KWH = None
RESERVE_PRICE = None
MIN_DISCHARGE = None  # kW - gdy dom pobiera mniej (minus PV), zamiast autokonsumpcji standby (dom z sieci)
PVCHG_MIN_KW = 0.5   # PV do magazynu (EMS 4) tylko przy sredniej PV >= progu [kW] (app.py: z krzywej ladowania, min. 0,5)
# Godziny z Tarcza Pstryk (h["tarcza"]): zakup po stalej cenie tarczy - przy niedoborze PV zawsze standby (dom z sieci),
# magazyn ladowany z PV / z sieci tylko pod sprzedaz w drogich godzinach (rozladowanie do domu oszczedza tylko cene tarczy).


def eta_at(p, default):
    """Sprawnosc rozladowania przy mocy p [kW] wg ETA_D_CURVE (powyzej krzywej - default)."""
    if ETA_D_CURVE and p > 0:
        for lo, hi, e in ETA_D_CURVE:
            if lo <= p < hi:
                return min(e, default)
        if p < ETA_D_CURVE[0][0]:          # 1.111: ponizej najnizszego zmierzonego przedzialu - nie lepiej niz on
            return min(ETA_D_CURVE[0][2], default)
    return default


def eta_c_at(p, default):
    """Sprawnosc ladowania przy mocy ladowania p [kW] wg ETA_C_CURVE (powyzej krzywej - default)."""
    if ETA_C_CURVE and p > 0:
        for lo, hi, e in ETA_C_CURVE:
            if lo <= p < hi:
                return min(e, default)
        if p < ETA_C_CURVE[0][0]:          # 1.111: ponizej najnizszego zmierzonego przedzialu - nie lepiej niz on
            return min(ETA_C_CURVE[0][2], default)
    return default


def _auto_d(load, pv, stored, s_min, s_max, p_max, eta_c, eta_d, dur=1.0):
    """Przeplyw AC do magazynu [kWh] w trybie autokonsumpcji (+ ladowanie, - rozladowanie)."""
    net = pv - load
    if net >= 0:
        return min(net, p_max, max(0.0, (s_max - stored) / eta_c))
    return -min(-net, p_max, max(0.0, (stored - s_min) * eta_at(-net / dur, eta_d)))


def _apply(stored, d, eta_c, eta_d, dur=1.0):
    return stored + (d * eta_c_at(d / dur, eta_c) if d > 0 else d / eta_at(-d / dur, eta_d))


WEAR = 0.0   # zl za kWh oddana z magazynu (zuzycie baterii) - ustawiane z aplikacji
# 1.103: zywotnosc baterii - dlugie trzymanie bardzo wysokiego (> HOLD_HI) i bardzo niskiego (< HOLD_LO) stanu naladowania
# kosztuje HOLD_COST zl za kWh ponad / ponizej progu za kazda godzine. Przy rownych cenach (Tarcza) plan dobija do 100 %
# dopiero tuz przed sprzedaza i nie zostawia magazynu pustego na dlugo; tania noc nadal wygrywa z kilkoma godzinami czekania.
HOLD_HI = None     # kWh (ustawiane z aplikacji: 90 % pojemnosci)
HOLD_LO = None     # kWh (15 % pojemnosci)
HOLD_COST = 0.05     # zl za kWh ponad 90 % / ponizej 15 % za godzine (test: dobija do 100 % ok. 1 h przed sprzedaza)


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


def plan(hours, stored0, s_min, s_max, p_max, eta_c=0.95, eta_d=0.95, terminal_price=0.0, step=None, mode0="auto",
         force0=None):
    """hours: [{load, pv, buy, sell}] (kWh i zl/kWh w danej godzinie). Zwraca liste krokow planu.

    Stan: energia w magazynie (siatka ok. 0,25 % pojemnosci) i poprzedni tryb
    (kazda zmiana trybu kosztuje SWITCH_COST, zeby plan nie przelaczal falownika co godzine).
    force0: tryb biezacej godziny ustalony na jej poczatku (blokada na godzine) - w kroku 0 tylko ten tryb
    (o ile mozliwy w tej godzinie), kolejne godziny liczone na nowo.
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
            e = levels[i] - s_min
            r = max(0.0, min(levels[i], RESERVE_KWH) - s_min) if RESERVE_KWH and RESERVE_PRICE else 0.0
            V[T][i][m] = -(r * (RESERVE_PRICE or 0.0) + (e - r) * terminal_price) * eta_d
    choice = [[[None] * M for _ in range(n + 1)] for _ in range(T)]
    hold_pen = [HOLD_COST * (max(0.0, lv - HOLD_HI) if HOLD_HI is not None else 0.0)
                + HOLD_COST * (max(0.0, HOLD_LO - lv) if HOLD_LO is not None else 0.0) for lv in levels]

    for t in range(T - 1, -1, -1):
        h = hours[t]
        load, pv, buy, sell = h["load"], h["pv"], h["buy"], h["sell"]
        dur = h.get("dur", 1.0)
        low = MIN_DISCHARGE is not None and 0 < (load - pv) / dur < MIN_DISCHARGE   # maly pobor - tylko standby
        if h.get("tarcza") and load > pv + 0.005:
            low = True    # Tarcza: dom z sieci po stalej cenie, magazyn tylko na sprzedaz (bez rozladowania do domu)
        surplus = max(pv - load, 0.0)
        for i in range(n + 1):
            s = levels[i]
            opts = [(1, 0.0)] if low else [(0, _auto_d(load, pv, s, s_min, s_max, p_max, eta_c, eta_d, dur)), (1, 0.0)]
            room = max(0.0, (s_max - s) / eta_c)
            top = min(p_chg, room)
            # 1.108: w godzinach z Tarcza pulap zakupu nie obowiazuje (cena tarczy waha sie o grosze w ciagu miesiaca, a o
            # oplacalnosci decyduje plan: sprzedaz od progu / min. zysk w trybie auto); bez Tarczy - pulap jak dotad
            if top > surplus + 0.05 and (BUY_CAP is None or buy <= BUY_CAP or h.get("tarcza")):
                for k in range(1, grid_levels + 1):
                    opts.append((2, surplus + (top - surplus) * k / grid_levels))
            if (SELL_FLOOR is not None and sell >= SELL_FLOOR) or (AUTO and SELL_FLOOR is None):
                avail = min(p_dis, max(0.0, (s - s_min) * eta_d))     # maks. rozladowanie (dom + siec)
                need = max(load - pv, 0.0)                              # tyle i tak zabralby dom
                extra = avail - need
                if h.get("exp_cap") is not None:          # sprzedaz tylko energii z PV (limit z produkcji)
                    extra = min(extra, h["exp_cap"])
                if extra > 0.05:
                    for k in range(1, ex_levels + 1):
                        opts.append((3, -(need + extra * k / ex_levels)))
            # samozuzycie bez rozladowania: PV najpierw dom, nadwyzka do magazynu (do mocy i miejsca), magazyn nie oddaje
            if pv > 0.05 * dur:
                opts.append((5, min(surplus, p_max, room)))
                if h.get("tarcza"):     # Tarcza: zamiast samozuzycia - bez rozladowania (magazyn tylko na sprzedaz)
                    opts = [o for o in opts if o[0] != 0]
            if h.get("tarcza") and pv / dur >= PVCHG_MIN_KW and room > 0.05 and load > 0.02:
                # Tarcza: cale PV do magazynu (Remote EMS 4, limit = moc PV), dom w calosci z sieci po cenie tarczy
                opts.append((4, min(pv, p_chg, room)))
            evals = []
            for mi, d in opts:
                j = idx(_apply(s, d, eta_c, eta_d, dur))
                grid = load - pv + d
                c = _cost(grid, buy, sell, d) + hold_pen[j] * dur
                if mi == 2:
                    c += TIE_EPS * (d - surplus) + SPREAD_EPS * (d - surplus) ** 2
                    if RATE_COST and RATE_SOFT_KW is not None:
                        c += RATE_COST * max(0.0, d - surplus - RATE_SOFT_KW * dur)
                elif mi == 4:
                    c += TIE_EPS * 0.7 * dur
                elif mi == 3:
                    c += TIE_EPS * (-d) + SPREAD_EPS * d * d
                    if AUTO_MARGIN:          # tryb auto: wymagany zysk na kWh z magazynu do sieci
                        c += AUTO_MARGIN * max(0.0, -d - max(load - pv, 0.0))
                elif mi == 1:
                    c += TIE_EPS * 0.5 * dur
                elif mi == 5:
                    c += TIE_EPS * 0.3 * dur      # przy rownych kosztach przed standby (nadwyzka PV nie ucieka do sieci)
                evals.append((mi, d, j, c))
            if t == 0 and force0 in names:
                fe = [e for e in evals if e[0] == names.index(force0)]
                if fe:
                    evals = fe
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
    nod = names.index("nodis")
    for t, h in enumerate(hours):
        mi, d, j = choice[t][i][mp]
        # 1.98: standby w godzinie z PV -> samozuzycie bez rozladowania, gdy kWh w magazynie jest warta wiecej niz sprzedaz
        # teraz (krańcowa wartosc z planu). Przy prognozie PV ponizej rzeczywistosci nadwyzka nie ucieka wtedy do sieci,
        # a przy niedoborze oba tryby dzialaja tak samo (dom z PV i sieci, magazyn nie oddaje).
        # (biezaca godzina z blokada trybu - bez zmiany: inaczej tryb przeskakiwal standby <-> samozuzycie co 5 min)
        # 1.124: nadwyzka PV zawsze do magazynu (przyda sie na autokonsumpcje albo sprzedaz w drogiej godzinie) - bez
        # warunku wartosci krańcowej; standby zostaje tylko bez nadwyzki PV albo przy pelnym magazynie
        if names[mi] == "standby" and h["pv"] > 0.05 * h.get("dur", 1.0) and j < n and not (t == 0 and force0):
            if h["pv"] - h["load"] > 0.05 * h.get("dur", 1.0):
                room = max(0.0, (s_max - levels[i]) / eta_c)
                d5 = min(max(h["pv"] - h["load"], 0.0), p_max, room)
                j5 = idx(_apply(levels[i], d5, eta_c, eta_d, h.get("dur", 1.0)))
                mi, d, j = nod, d5, j5
        grid = h["load"] - h["pv"] + d
        out.append({"mode": names[mi], "d": d, "grid": grid, "stored": levels[j],
                    "cost": _cost(grid, h["buy"], h["sell"], d)})
        i, mp = j, mi
    return out


def label(step, h):
    """Opis kroku dla uzytkownika."""
    surplus = max(h["pv"] - h["load"], 0.0)
    if step["mode"] == "pvchg":
        return f"PV do magazynu {step['d']:.1f} kWh, dom z sieci"
    if step["mode"] == "nodis":
        return (f"samozużycie bez rozładowania – nadwyżka PV {step['d']:.1f} kWh do magazynu" if step["d"] >= 0.05
                else "samozużycie bez rozładowania (dom z PV i sieci)")
    if step["mode"] == "export":
        out = max(0.0, -step["d"] - max(h["load"] - h["pv"], 0.0))
        return f"sprzedaż z magazynu {out:.1f} kWh"
    if step["mode"] == "grid" and step["d"] - surplus >= 0.05:
        return f"ładowanie z sieci {max(step['d'] - surplus, 0):.1f} kWh"
    if step["mode"] == "grid" and step["d"] - surplus > 0.005:
        return "ładowanie z sieci < 0,1 kWh"
    if step["mode"] == "standby" and h.get("tarcza") and h["load"] > h["pv"] + 0.005:
        return "standby – tarcza (dom z sieci)"
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
