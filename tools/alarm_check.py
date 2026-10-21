"""
Odczyt alarmow falownika Sigenergy (w tym AFCI - wykrywanie luku elektrycznego). TYLKO ODCZYT.
Uzycie: python tools/alarm_check.py <ip falownika>
"""
import sys

from pymodbus.client import ModbusTcpClient

if len(sys.argv) < 2:
    sys.exit("Uzycie: python tools/alarm_check.py <ip falownika>")
IP = sys.argv[1]

ALARM1 = {0: "niezgodnosc wersji oprogramowania", 1: "niska rezystancja izolacji", 2: "przegrzanie",
          3: "usterka urzadzenia", 4: "blad uziemienia systemu", 5: "przepiecie na stringu PV",
          6: "string PV podlaczony odwrotnie", 7: "prad wsteczny stringu PV", 8: "AFCI - WYKRYTY LUK ELEKTRYCZNY",
          9: "zanik sieci", 10: "przepiecie sieci", 11: "za niskie napiecie sieci", 12: "za wysoka czestotliwosc",
          13: "za niska czestotliwosc", 14: "asymetria napiec sieci", 15: "skladowa DC pradu wyjsciowego"}
ALARM2 = {0: "prad uplywu poza limitem", 1: "blad komunikacji", 2: "zabezpieczenie wewnetrzne",
          3: "AFCI - USTERKA UKLADU SAMOTESTU", 4: "zabezpieczenie off-grid", 5: "zabezpieczenie reczne",
          7: "zla kolejnosc faz", 8: "zwarcie do PE", 9: "nieudany soft start"}

REGS = [(30605, 1, "falownik Alarm1", ALARM1), (30606, 1, "falownik Alarm2", ALARM2),
        (30027, 247, "instalacja Alarm1", ALARM1), (30028, 247, "instalacja Alarm2", ALARM2)]


def read(c, addr, unit):
    try:
        rr = c.read_input_registers(addr, count=1, device_id=unit)
    except TypeError:
        rr = c.read_input_registers(addr, count=1, slave=unit)
    if rr.isError():
        raise RuntimeError(str(rr))
    return rr.registers[0]


def main():
    c = ModbusTcpClient(IP, port=502, timeout=5)
    if not c.connect():
        sys.exit(f"Brak polaczenia z {IP}:502")
    try:
        for addr, unit, name, table in REGS:
            try:
                v = read(c, addr, unit)
            except Exception as e:  # noqa: BLE001
                print(f"{addr} {name:20} blad odczytu: {e}")
                continue
            active = [f"bit {b}: {table.get(b, 'nieznany')}" for b in range(16) if v >> b & 1]
            print(f"{addr} {name:20} = {v:5}  {'OK - brak alarmow' if not active else '; '.join(active)}")
    finally:
        c.close()


if __name__ == "__main__":
    main()
