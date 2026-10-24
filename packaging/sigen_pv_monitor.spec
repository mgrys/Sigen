# PyInstaller: jeden plik SigenPVMonitor.exe (Python + biblioteki + strona w srodku).
# Budowanie (z katalogu repozytorium):  pyinstaller packaging/sigen_pv_monitor.spec
import os
from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
hidden = (collect_submodules("uvicorn") + collect_submodules("pymodbus.client") + collect_submodules("vallox_websocket_api")
          + collect_submodules("websockets") + collect_submodules("tuya_sharing") + collect_submodules("cryptography")
          + ["app", "vallox", "meter", "smartlife", "qrcode", "qrcode.image.svg"])

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "web"), "web")],
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "numpy", "pandas", "matplotlib", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SigenPVMonitor",
    icon=os.path.join(ROOT, "packaging", "icon.ico"),
    console=False,          # bez okna konsoli; log w folderze danych (sigen-pv-monitor.log)
    upx=False,
    version=os.path.join(ROOT, "packaging", "version_info.txt") if os.path.exists(os.path.join(ROOT, "packaging", "version_info.txt")) else None,
)
