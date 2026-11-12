"""
1.24: porownanie Pstryka (Tarcza, depozyt na cala fakture) ze sprzedawca z cena taryfowa wg stref (np. G13 TAURON).

- strefy i stawki dystrybucji: z taryfy wybranej w Ustawieniach (operator + taryfa, prices.load_tariff),
- ceny energii sprzedawcy taryfowego (brutto, z akcyza) dla kazdej strefy: config.ini [porownanie] <strefa> = zl/kWh;
  domyslnie cena sprzedawcy z urzedu ze zbioru taryf (np. TAURON 2026: G11 / G12 / G12w / G13), dla innych - do wpisania,
- dystrybucja jak na fakturze Pstryka: Pstryk refakturuje dystrybucje operatora, wiec w wariancie "te same godziny"
  dystrybucja = rzeczywista z licznika (pstryk_hour), w wariancie "najtansza strefa" - stawka tej strefy z taryfy,
- depozyt u sprzedawcy taryfowego pokrywa TYLKO czesc energetyczna faktury (dystrybucja i oplaty stale zawsze do
  zaplaty), nadwyzka przechodzi na kolejne miesiace; sprzedaz (depozyt) przyjmowana taka sama jak u Pstryka.
"""
import datetime as dt

import config
import prices


def zone_prices(tariff):
    """{strefa: cena energii brutto} - z [porownanie] w config.ini, a gdy brak - cena sprzedawcy z urzedu ze zbioru taryf
    (netto + akcyza, z VAT), jesli zbior ja podaje dla tej taryfy."""
    zones = list((tariff or {}).get("zones", {}))
    cp = config.load()
    out = {}
    for z in zones:
        v = cp.get("porownanie", z, fallback="").strip().replace(",", ".") if cp.has_section("porownanie") else ""
        try:
            out[z] = float(v) if v else None
        except ValueError:
            out[z] = None
    en = (tariff or {}).get("energy_net") or {}
    for z in zones:
        if out.get(z) is None and en.get(z) is not None:
            out[z] = round((en[z] + prices.AKCYZA) * (1 + prices.VAT), 4)
    return out


def month(rows, tariff, zp):
    """Pobor wg stref i koszt u sprzedawcy taryfowego (bez depozytu) z godzin licznika operatora."""
    imp, hours, dvar, dfix = {}, 0, 0.0, 0.0
    for r in rows:
        if r.get("imp") is None:
            continue
        z = prices.zone_for(tariff, dt.datetime.fromtimestamp(r["ts"]))
        imp[z] = imp.get(z, 0.0) + r["imp"]
        hours += 1
        dvar += (r.get("dist_var") or 0) * (1 + prices.VAT)
        dfix += ((r.get("dist_fix") or 0) + (r.get("power_fee") or 0)) * (1 + prices.VAT)
    tot = sum(imp.values())
    dist_g = {z: prices.price_parts(tariff, 0.0, 0.0, z)["dist"] * (1 + prices.VAT) for z in tariff.get("zones", {})}
    if any(zp.get(z) is None for z in imp):
        return {"imp": round(tot, 2), "zones": {z: round(v, 2) for z, v in imp.items()}, "missing": True}
    cheap = min(zp, key=lambda z: (zp[z] or 9) + dist_g.get(z, 0)) if zp else None
    same = {"energy": sum(v * zp[z] for z, v in imp.items()),
            "dist": dvar if dvar > 0.01 else sum(v * dist_g.get(z, 0) for z, v in imp.items())}
    night = {"energy": tot * (zp.get(cheap) or 0), "dist": tot * dist_g.get(cheap, 0)}
    return {"imp": round(tot, 2), "zones": {z: round(v, 2) for z, v in imp.items()}, "hours": hours, "fixed": round(dfix, 2),
            "same": same, "night": night, "cheap_zone": cheap, "missing": False}


def settle(var, fixed, deposit):
    """Faktura u sprzedawcy taryfowego: depozyt tylko na energie. (do zaplaty, wykorzystany depozyt, reszta depozytu)."""
    used = min(deposit, var["energy"])
    return round(var["energy"] - used + var["dist"] + fixed, 2), round(used, 2), round(deposit - used, 2)
