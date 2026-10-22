"""
Dostep z innych urzadzen w sieci (telefon, inny komputer, aplikacja Android).

- Podglad (GET) - bez hasla.
- Zmiany (POST/PUT/PATCH/DELETE) z innych urzadzen - dopiero po wpisaniu PIN-u.
  Z tego komputera (localhost) wszystko dziala jak dotad, bez PIN-u.
- PIN: zakladka Ustawienia -> Strona -> PIN (config.ini: [serwer] pin = ...).
  Bez PIN-u zmiany z innych urzadzen sa zablokowane (tylko podglad).
- Po poprawnym PIN-ie urzadzenie dostaje ciasteczko sesji (waznosc 1 rok);
  sesje zapisywane jako skroty SHA-256 w access_sessions.json. Zmiana PIN-u uniewaznia wszystkie sesje.
- 5 blednych prob z jednego adresu -> blokada na 5 minut.
"""
import configparser
import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
INI = BASE / "dostep.ini"
SECTION, KEY = "dostep", "pin"     # wersja publiczna: config.ini, [serwer] pin
PIN_HINT = "w pliku dostep.ini na komputerze z aplikacją"
SESSIONS_FILE = BASE / "access_sessions.json"
COOKIE = "sigen_sid"
MAX_AGE = 365 * 86400
LOCAL = ("127.0.0.1", "::1", "localhost")
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
OPEN_PATHS = ("/api/login", "/api/logout")
FAIL_LIMIT, FAIL_LOCK = 5, 300

_lock = threading.Lock()
_fails = {}          # ip -> (liczba, czas pierwszej)
_cfg = {"mtime": None, "pin": None}


def _pin():
    try:
        m = INI.stat().st_mtime
    except OSError:
        return None
    if m != _cfg.get("mtime") or INI != _cfg.get("path"):
        cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        cp.read(INI, encoding="utf-8")
        p = cp.get(SECTION, KEY, fallback="").strip()
        _cfg.update(mtime=m, path=INI, pin=p or None)
    return _cfg["pin"]


def _pin_tag(pin):
    return hashlib.sha256(("pin:" + pin).encode()).hexdigest()[:16]


def _load():
    try:
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(d):
    try:
        SESSIONS_FILE.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def is_local(request):
    return (request.client.host if request.client else "") in LOCAL


def is_authed(request):
    if is_local(request):
        return True
    pin = _pin()
    sid = request.cookies.get(COOKIE)
    if not pin or not sid:
        return False
    h = hashlib.sha256(sid.encode()).hexdigest()
    with _lock:
        s = _load().get(h)
    return bool(s) and s.get("pin") == _pin_tag(pin) and s.get("exp", 0) > time.time()


def status(request):
    return {"local": is_local(request), "authed": is_authed(request), "pin_set": bool(_pin())}


def needs_auth(request):
    return request.method in WRITE_METHODS and request.url.path not in OPEN_PATHS and not is_authed(request)


def denied_body():
    pin = bool(_pin())
    return {"ok": False, "need_pin": True, "pin_set": pin,
            "error": "wpisz PIN, aby zmieniać ustawienia z tego urządzenia" if pin
            else "zmiany z innych urządzeń są wyłączone – ustaw PIN " + PIN_HINT}


def login(request, pin_given):
    """Zwraca (ok, komunikat, sid)."""
    ip = request.client.host if request.client else "?"
    now = time.time()
    with _lock:
        n, t0 = _fails.get(ip, (0, now))
        if n >= FAIL_LIMIT and now - t0 < FAIL_LOCK:
            return False, f"za dużo błędnych prób – spróbuj za {int(FAIL_LOCK - (now - t0)) // 60 + 1} min", None
    pin = _pin()
    if not pin:
        return False, "PIN nie jest ustawiony – ustaw go " + PIN_HINT, None
    if not hmac.compare_digest(str(pin_given or "").strip().encode(), pin.encode()):
        with _lock:
            n, t0 = _fails.get(ip, (0, now))
            _fails[ip] = (1, now) if now - t0 >= FAIL_LOCK else (n + 1, t0)
        return False, "błędny PIN", None
    with _lock:
        _fails.pop(ip, None)
        sid = secrets.token_urlsafe(32)
        d = {k: v for k, v in _load().items() if v.get("exp", 0) > now}
        d[hashlib.sha256(sid.encode()).hexdigest()] = {"exp": now + MAX_AGE, "pin": _pin_tag(pin),
                                                       "ua": (request.headers.get("user-agent") or "")[:80], "ip": ip}
        _save(d)
    return True, "ok", sid


def logout(request):
    sid = request.cookies.get(COOKIE)
    if sid:
        with _lock:
            d = _load()
            d.pop(hashlib.sha256(sid.encode()).hexdigest(), None)
            _save(d)
