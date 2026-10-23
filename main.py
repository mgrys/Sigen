"""
Sigen PV Monitor - punkt startowy (zrodla: python main.py, Windows: SigenPVMonitor.exe).

  --no-browser   nie otwieraj przegladarki po starcie
  --port N       port strony (domyslnie z config.ini, 8000)

Jesli aplikacja juz dziala, kolejne uruchomienie tylko otwiera strone w przegladarce.
"""
import argparse
import logging
import socket
import sys
import threading
import time
import webbrowser

import config


def _setup_logging():
    log_file = config.DATA_DIR / "sigen-pv-monitor.log"
    if sys.stdout is None or sys.stderr is None:          # .exe bez konsoli
        f = open(log_file, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)])
    for noisy in ("pymodbus", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file


def _port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _message(text):
    """Komunikat dla uzytkownika (okienko w Windows, inaczej konsola)."""
    print(text)
    if sys.platform == "win32" and config.FROZEN and not config.SERVICE:   # usluga nie ma pulpitu - okienko by wisialo
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, config.APP_TITLE, 0x40)
        except Exception:  # noqa: BLE001
            pass


def main():
    ap = argparse.ArgumentParser(description=config.APP_TITLE)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--port", type=int, default=config.geti("serwer", "port", 8000))
    ap.add_argument("--wait-port", action="store_true", help=argparse.SUPPRESS)   # restart z poziomu strony
    args = ap.parse_args()
    url = f"http://localhost:{args.port}"
    open_browser = config.getb("serwer", "otworz_przegladarke", True) and not args.no_browser

    if args.wait_port:                          # restart: poczekaj, az poprzednia instancja zwolni port
        for _ in range(40):
            if not _port_in_use(args.port):
                break
            time.sleep(0.25)
    if _port_in_use(args.port):                 # juz dziala - tylko pokaz strone
        if config.SERVICE:                      # usluga: zajety port (np. otwarte okno programu) - sprobuj pozniej
            time.sleep(60)
            sys.exit(1)
        if open_browser:
            webbrowser.open(url)
        return

    log_file = _setup_logging()
    log = logging.getLogger("sigen")

    import uvicorn
    import app

    try:
        app.start_background()
    except Exception as e:  # noqa: BLE001
        _message(f"Nie można otworzyć bazy danych ({app.db.describe()}):\n{e}")
        sys.exit(1)
    log.info("%s v%s, dane: %s, falownik %s:%s, strona %s", config.APP_TITLE, app.__version__,
             config.DATA_DIR, app.HOST, app.PORT, url)
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(url)), daemon=True).start()
    try:
        uvicorn.run(app.app, host=config.get("serwer", "host", "0.0.0.0"), port=args.port,
                    log_level="warning", log_config=None)
    except Exception as e:  # noqa: BLE001
        _message(f"Nie udało się uruchomić serwera na porcie {args.port}:\n{e}\n\nSzczegóły: {log_file}")
        sys.exit(1)


if __name__ == "__main__":
    main()
