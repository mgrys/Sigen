"""
Awaryjne oddanie sterowania falownikowi: Remote EMS = wylaczone (rejestr 40029 = 0) i sterowanie w aplikacji = wyl.
Falownik wraca do trybu ustawionego w aplikacji mySigen. Uzycie (np. gdy usluga nie dziala):
    python tools/inverter_release.py   (w folderze aplikacji)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pymodbus.client import ModbusTcpClient  # noqa: E402

import inverter_ctl  # noqa: E402
from app import HOST, PORT, write_holding, read_holding  # noqa: E402

inverter_ctl.set_tryb("wyl")
c = ModbusTcpClient(HOST, port=PORT, timeout=3)
if not c.connect():
    sys.exit(f"brak polaczenia z falownikiem {HOST}:{PORT}")
rr = write_holding(c, inverter_ctl.REG_ENABLE, [0])
print("zapis 40029 = 0:", "OK" if not rr.isError() else rr)
inverter_ctl._owned_save(False)
r = read_holding(c, inverter_ctl.REG_ENABLE, 1)
print("odczyt Remote EMS:", r.registers[0] if not r.isError() else r)
c.close()
