"""
Skan rejestrow Sigenergy - sprawdza, co Twoj falownik faktycznie udostepnia.
Lista rejestrow jest w ../registers.py (ta sama, ktorej uzywa dashboard).
Rejestr z "BLAD" = brak w tym firmware.

Uzycie:  python tools/sigen_scan.py
"""
import sys
from pathlib import Path

from pymodbus.client import ModbusTcpClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import registers  # noqa: E402

import config  # noqa: E402

HOST = config.get("falownik", "host")
PORT = config.geti("falownik", "port", 502)


def read(client, addr, count, unit):
    try:
        return client.read_input_registers(addr, count=count, device_id=unit)
    except TypeError:
        return client.read_input_registers(addr, count=count, slave=unit)


def main():
    c = ModbusTcpClient(HOST, port=PORT, timeout=3)
    if not c.connect():
        print(f"Brak polaczenia z {HOST}:{PORT}")
        return
    values = registers.read_all(c, read)
    c.close()
    ok = bad = 0
    for g in registers.grouped(values):
        print(f"\n--- {g['group']} ---")
        for it in g["items"]:
            if it["value"] is None:
                bad += 1
                print(f"  {it['addr']:5}  {it['name']:34} BLAD")
            else:
                ok += 1
                v = it["text"] or it["value"]
                print(f"  {it['addr']:5}  {it['name']:34} {v} {it['unit']}")
    print(f"\nOdczytane: {ok}, niedostepne: {bad}")


if __name__ == "__main__":
    main()
