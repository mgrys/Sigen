"""
Wyszukiwanie falownika Sigenergy (przycisk "Znajdz falownik") i rekuperatora Vallox MV ("Znajdz rekuperator")
w sieci domowej.

1. Ustala podsieci komputera (np. 192.168.0.x) - tylko adresy prywatne.
2. Sprawdza rownolegle, ktore adresy maja otwarty port Modbus TCP (domyslnie 502).
3. Dla znalezionych odczytuje model falownika (rejestr 30500) i SOC magazynu (30014) - tylko odczyt.
"""
import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor

from pymodbus.client import ModbusTcpClient

import registers

TIMEOUT = 0.5      # s na jedna probe polaczenia
WORKERS = 64


def local_networks():
    """Prefiksy /24 prywatnych adresow tego komputera, np. ['192.168.0']."""
    ips = set()
    try:                                     # adres uzywany do wyjscia na swiat (bez wysylania danych)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    nets = []
    for ip in sorted(ips):
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if a.is_private and not a.is_loopback and not a.is_link_local:
            pref = ip.rsplit(".", 1)[0]
            if pref not in nets:
                nets.append(pref)
    return nets, sorted(ips)


def _open(ip, port):
    try:
        with socket.create_connection((ip, port), timeout=TIMEOUT):
            return True
    except OSError:
        return False


def _read(client, addr, count, unit):
    try:
        return client.read_input_registers(addr, count=count, device_id=unit)
    except TypeError:
        return client.read_input_registers(addr, count=count, slave=unit)


def probe(ip, port, plant_id=None, inverter_id=None):
    """Proba odczytu modelu i SOC; zwraca dict z wynikiem (sigen=True gdy to falownik Sigenergy)."""
    plant_id = registers.PLANT if plant_id is None else plant_id
    inverter_id = registers.INV if inverter_id is None else inverter_id
    out = {"ip": ip, "port": port, "sigen": False, "model": None, "soc": None, "error": None}
    c = ModbusTcpClient(ip, port=port, timeout=2)
    try:
        if not c.connect():
            out["error"] = "brak połączenia Modbus"
            return out
        rr = _read(c, 30500, 15, inverter_id)
        if not rr.isError():
            out["model"] = registers.decode(rr.registers, "str", 1) or None
        rr = _read(c, 30014, 1, plant_id)
        if not rr.isError():
            out["soc"] = round(rr.registers[0] / 10, 1)
        out["sigen"] = out["model"] is not None or out["soc"] is not None
        if not out["sigen"]:
            out["error"] = "port otwarty, ale brak odpowiedzi w formacie Sigenergy"
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
    finally:
        c.close()
    return out


def scan(port=502, networks=None):
    """Skan podsieci /24; zwraca {"networks": [...], "found": [...]}"""
    own_nets, own_ips = local_networks()
    nets = networks or own_nets
    targets = [f"{n}.{i}" for n in nets for i in range(1, 255)]
    targets = [ip for ip in targets if ip not in own_ips]
    with ThreadPoolExecutor(WORKERS) as ex:
        hits = [ip for ip, ok in zip(targets, ex.map(lambda ip: _open(ip, port), targets)) if ok]
    found = [probe(ip, port) for ip in hits]
    found.sort(key=lambda x: (not x["sigen"], [int(p) for p in x["ip"].split(".")]))
    return {"networks": [f"{n}.0/24" for n in nets], "port": port, "found": found, "scanned": len(targets)}


# --- rekuperator Vallox (seria MV, MyVallox) ------------------------------------------------
VALLOX_TIMEOUT = 8      # s na odczyt metryk przez websocket z jednego adresu
VALLOX_WORKERS = 8


def probe_vallox(ip):
    """Proba odczytu modelu i numeru seryjnego z rekuperatora (websocket MyVallox, port 80) - tylko odczyt."""
    import asyncio
    import vallox
    out = {"ip": ip, "vallox": False, "model": None, "serial": None, "error": None}
    try:
        m = asyncio.run(asyncio.wait_for(vallox._fetch(ip), VALLOX_TIMEOUT))
        idx = m.get("A_CYC_MACHINE_MODEL")
        try:
            from vallox_websocket_api.vallox import DEVICE_MODEL
            out["model"] = DEVICE_MODEL[idx] if isinstance(idx, int) and 0 <= idx < len(DEVICE_MODEL) else None
        except Exception:  # noqa: BLE001
            pass
        out["serial"] = vallox.serial_of(m)
        out["vallox"] = bool(m) and (idx is not None or out["serial"] is not None or "A_CYC_TEMP_OUTDOOR_AIR" in m)
        out["model"] = out["model"] or ("Vallox (model nieznany)" if out["vallox"] else None)
    except Exception as e:  # noqa: BLE001 - zwykle inne urzadzenie z portem 80 (router, drukarka)
        out["error"] = type(e).__name__
    return out


def scan_vallox(networks=None):
    """Skan podsieci /24: adresy z otwartym portem 80, potem proba odczytu Vallox. {"found": [...]} - tylko rekuperatory."""
    own_nets, own_ips = local_networks()
    nets = networks or own_nets
    targets = [ip for ip in (f"{n}.{i}" for n in nets for i in range(1, 255)) if ip not in own_ips]
    with ThreadPoolExecutor(WORKERS) as ex:
        hits = [ip for ip, ok in zip(targets, ex.map(lambda ip: _open(ip, 80), targets)) if ok]
    with ThreadPoolExecutor(VALLOX_WORKERS) as ex:
        res = list(ex.map(probe_vallox, hits))
    found = sorted((x for x in res if x["vallox"]), key=lambda x: [int(p) for p in x["ip"].split(".")])
    return {"networks": [f"{n}.0/24" for n in nets], "found": found, "checked": len(hits), "scanned": len(targets)}


# --- mierniki Shelly (gniazdka / przekazniki z pomiarem) ------------------------------------
def probe_shelly(ip):
    """GET /shelly (Gen1 i Gen2) + aktualny pobor - tylko odczyt. None, gdy to nie Shelly."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{ip}/shelly", timeout=2) as r:
            info = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(info, dict) or not (info.get("type") or info.get("gen") or info.get("model")):
        return None
    out = {"kind": "shelly", "id": ip, "ip": ip, "name": info.get("name") or info.get("id") or info.get("type") or ip,
           "model": info.get("model") or info.get("type") or "Shelly", "gen": info.get("gen") or 1, "power_w": None,
           "online": True}
    try:
        import meter
        v = meter.read_shelly(ip)
        out["power_w"] = None if v.get("power") is None else round(float(v["power"]), 1)
    except Exception as e:  # noqa: BLE001 - Shelly bez pomiaru (np. sam przekaznik)
        out["error"] = type(e).__name__
    return out


def scan_shelly(networks=None):
    """Skan podsieci /24 w poszukiwaniu urzadzen Shelly; {"found": [...]}."""
    own_nets, own_ips = local_networks()
    nets = networks or own_nets
    targets = [ip for ip in (f"{n}.{i}" for n in nets for i in range(1, 255)) if ip not in own_ips]
    with ThreadPoolExecutor(WORKERS) as ex:
        hits = [ip for ip, ok in zip(targets, ex.map(lambda ip: _open(ip, 80), targets)) if ok]
    with ThreadPoolExecutor(16) as ex:
        found = [x for x in ex.map(probe_shelly, hits) if x]
    found.sort(key=lambda x: [int(p) for p in x["ip"].split(".")])
    return {"networks": [f"{n}.0/24" for n in nets], "found": found}
