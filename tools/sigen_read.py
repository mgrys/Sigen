"""
Test odczytu Sigenergy (SigenStor / TP2) przez Modbus TCP.
Wymaga: pip install pymodbus
Uruchom na komputerze w tej samej sieci co falownik.
"""
import time
from pymodbus.client import ModbusTcpClient

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

HOST = config.get("falownik", "host")
PORT = config.geti("falownik", "port", 502)
PLANT_ID = 247      # adres "plant" - dane calej instalacji
INTERVAL = 5        # sekundy miedzy odczytami

# nazwa: (rejestr, liczba rejestrow, signed, dzielnik, jednostka)
# Wartosci wg Sigenergy Modbus Protocol (rejestry plant, slave 247)
REGISTERS = {
    "SOC magazynu":        (30014, 1, False, 10,   "%"),
    "Moc PV":              (30035, 2, True,  1000, "kW"),
    "Moc magazynu (+lad)": (30037, 2, True,  1000, "kW"),
    "Siec (+import)":      (30005, 2, True,  1000, "kW"),
    "Moc czynna plant":    (30031, 2, True,  1000, "kW"),
}


def read(client, addr, count):
    """Odczyt input registers; obsluga roznych wersji pymodbus (slave/device_id)."""
    try:
        return client.read_input_registers(addr, count=count, device_id=PLANT_ID)
    except TypeError:
        return client.read_input_registers(addr, count=count, slave=PLANT_ID)


def decode(regs, signed):
    if len(regs) == 1:
        val = regs[0]
        return val - 0x10000 if signed and val >= 0x8000 else val
    val = (regs[0] << 16) | regs[1]          # big-endian, starsze slowo pierwsze
    return val - 0x100000000 if signed and val >= 0x80000000 else val


def main():
    client = ModbusTcpClient(HOST, port=PORT, timeout=3)
    if not client.connect():
        print(f"Brak polaczenia z {HOST}:{PORT} - sprawdz Modbus TCP i siec.")
        return

    print(f"Polaczono z {HOST}. Ctrl+C konczy.\n")
    try:
        while True:
            print(time.strftime("%H:%M:%S"))
            for name, (addr, count, signed, div, unit) in REGISTERS.items():
                rr = read(client, addr, count)
                if rr.isError():
                    print(f"  {name:22} BLAD ({rr})")
                else:
                    raw = decode(rr.registers, signed)
                    print(f"  {name:22} {raw / div:8.3f} {unit}   (raw={raw})")
            print()
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()


if __name__ == "__main__":
    main()
