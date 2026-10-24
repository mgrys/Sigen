"""
Konto SmartLife (ta sama droga co integracja Tuya w Home Assistant 2026.10).

User Code z aplikacji, potem skan QR. Token zostaje w tabeli smartlife_session
i jest odswiezany w miejscu. Odczyt jest tylko do odczytu: zadnych komend na gniazdko.
Adres IP nie jest potrzebny. Klucz lokalny urzadzenia nie jest zapisywany.
"""
import io
import json
import threading
import time

import qrcode
import qrcode.image.svg
from tuya_sharing import LoginControl, Manager, SharingTokenListener

import db

# Te same stale co homeassistant/components/tuya (HA 2026.10.2).
CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
SCHEMA = "haauthorize"
QR_PREFIX = "tuyaSmart--qrLogin?token="
MEASURES = ("cur_power", "add_ele")
LOGIN_WAIT_S = 180

_lock = threading.Lock()
_cloud = threading.Lock()
_state = {"phase": "idle", "error": None, "qr_svg": None, "plugs": None}
_energy = {"wh": None, "t": None, "ignore": False, "device": None}


class _TokenSaver(SharingTokenListener):
    def update_token(self, token_info):
        con = db.connect()
        try:
            db.smartlife_save_token(con, token_info)
        finally:
            con.close()


def _session():
    con = db.connect()
    try:
        return db.smartlife_load(con)
    finally:
        con.close()


def _manager(session):
    if not session or not session.get("token_info") or not session.get("terminal_id") or not session.get("user_code"):
        raise RuntimeError("brak połączenia ze SmartLife (Ustawienia → Rekuperacja)")
    return Manager(CLIENT_ID, session["user_code"], session["terminal_id"], session["endpoint"],
                   session["token_info"], _TokenSaver())


def _scaled(device, code):
    raw = (device.status or {}).get(code)
    if isinstance(raw, str):
        try:
            raw = float(raw.replace(",", "."))
        except ValueError:
            return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    unit, scale = "", 0
    rng = (device.status_range or {}).get(code)
    if rng is not None and getattr(rng, "values", None):
        try:
            spec = json.loads(rng.values)
        except json.JSONDecodeError:
            spec = {}
        unit = spec.get("unit") or ""
        try:
            scale = int(spec.get("scale") or 0)
        except (TypeError, ValueError):
            scale = 0
    shown = raw / (10 ** scale) if scale else float(raw)
    return shown, unit, scale


def _is_plug(device):
    status = device.status or {}
    rng = device.status_range or {}
    return any(code in status or code in rng for code in MEASURES)


def _plug_view(device):
    got = _scaled(device, "cur_power")
    power = None if got is None else round(got[0], 1)
    return {"id": device.id, "name": device.name or device.id, "online": bool(device.online),
            "product_name": device.product_name or "", "power_w": power}


def qr_svg(payload):
    img = qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathImage, border=2, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    text = buf.getvalue().decode("utf-8")
    start = text.find("<svg")
    if start < 0:
        raise RuntimeError("nie udało się narysować kodu QR")
    return text[start:]


def _plugs_from(manager):
    manager.update_device_cache()
    plugs = [_plug_view(d) for d in manager.device_map.values() if _is_plug(d)]
    plugs.sort(key=lambda p: (p["name"] or "").lower())
    return plugs


def connect_status():
    with _lock:
        phase, error, svg, plugs = _state["phase"], _state["error"], _state["qr_svg"], _state["plugs"]
    session = _session() or {}
    return {
        "phase": phase,
        "error": error,
        "qr_svg": svg if phase == "waiting" else None,
        "plugs": plugs if phase == "ok" else None,
        "connected": bool(session.get("token_info") and session.get("terminal_id")),
        "device_id": session.get("device_id") or "",
        "device_name": session.get("device_name") or "",
    }


def account_status():
    s = connect_status()
    return {k: s[k] for k in ("connected", "device_id", "device_name", "phase", "error")}


def _fail(message):
    with _lock:
        _state.update(phase="error", error=message, qr_svg=None)


def _wait_login(login, qr_token, user_code):
    deadline = time.time() + LOGIN_WAIT_S
    try:
        while time.time() < deadline:
            ok, info = login.login_result(qr_token, CLIENT_ID, user_code)
            if ok:
                token_info = {"t": info["t"], "uid": info["uid"], "expire_time": info["expire_time"],
                              "access_token": info["access_token"], "refresh_token": info["refresh_token"]}
                con = db.connect()
                try:
                    db.smartlife_save_auth(con, user_code, info["terminal_id"], info["endpoint"], token_info)
                finally:
                    con.close()
                with _lock:
                    _state.update(phase="loading", error=None, qr_svg=None)
                with _cloud:
                    plugs = _plugs_from(_manager(_session()))
                with _lock:
                    _state.update(phase="ok", plugs=plugs, error=None)
                return
            if info.get("code") == "USERCODE_INCORRECT":
                _fail("User Code odrzucony. Wielkość liter ma znaczenie.")
                return
            time.sleep(2)
        _fail("Minął czas na zeskanowanie kodu QR. Połącz konto ponownie.")
    except Exception as e:  # noqa: BLE001
        _fail(f"{type(e).__name__}: {e}")


def start_connect(user_code):
    """Zwraca stan logowania. QR jest w polu qr_svg, dopóki faza to waiting."""
    user_code = (user_code or "").strip()
    if not user_code:
        user_code = (_session() or {}).get("user_code") or ""
    if not user_code:
        raise ValueError("wpisz User Code z aplikacji SmartLife")
    with _lock:
        if _state["phase"] in ("waiting", "loading"):
            return connect_status()
        _state.update(phase="waiting", error=None, qr_svg=None, plugs=None)
    login = LoginControl()
    try:
        response = login.qr_code(CLIENT_ID, SCHEMA, user_code)
    except Exception as e:  # noqa: BLE001
        _fail(f"brak połączenia z chmurą SmartLife: {e}")
        raise ValueError(_state["error"]) from e
    if not response.get("success"):
        code = response.get("code")
        msg = "User Code odrzucony. Wielkość liter ma znaczenie." if code == "USERCODE_INCORRECT" \
            else f"logowanie odrzucone ({code}): {response.get('msg')}"
        _fail(msg)
        raise ValueError(msg)
    svg = qr_svg(QR_PREFIX + response["result"]["qrcode"])
    with _lock:
        _state.update(phase="waiting", error=None, qr_svg=svg, plugs=None)
    threading.Thread(target=_wait_login, args=(login, response["result"]["qrcode"], user_code),
                     daemon=True, name="smartlife-login").start()
    return connect_status()


def _device_by_id(device_id):
    session = _session()
    manager = _manager(session)
    found = manager.device_repository.query_devices_by_ids([device_id])
    device = next((d for d in found if d.id == device_id), None)
    if device is None or not _is_plug(device):
        raise ValueError("to urządzenie nie jest gniazdkiem z pomiarem mocy")
    return device


def select_device(device_id):
    device_id = (device_id or "").strip()
    if not device_id:
        raise ValueError("wybierz gniazdko")
    session = _session()
    if not session or not session.get("token_info"):
        raise ValueError("najpierw połącz konto SmartLife")
    with _lock:
        known = next((p for p in (_state.get("plugs") or []) if p["id"] == device_id), None)
    if known is None:
        known = _plug_view(_device_by_id(device_id))
    con = db.connect()
    try:
        if not db.smartlife_save_device(con, known["id"], known["name"]):
            raise ValueError("najpierw połącz konto SmartLife")
    finally:
        con.close()
    with _cloud:
        _energy.update(wh=None, t=None, ignore=False, device=known["id"])
    return {"ok": True, "device_id": known["id"], "device_name": known["name"]}


def _total_wh(device, power):
    got = _scaled(device, "add_ele")
    if got is None:
        return None
    shown, unit, scale = got
    key = (unit or "").lower().replace(" ", "").replace("·", "").replace(".", "")
    wh = shown * 1000 if key == "kwh" or (not key and scale >= 3) else shown
    now = time.time()
    with _cloud:
        if _energy["device"] != device.id:
            _energy.update(wh=None, t=None, ignore=False, device=device.id)
        if _energy["ignore"]:
            return None
        prev, prev_t = _energy["wh"], _energy["t"]
        _energy["wh"], _energy["t"] = wh, now
    if prev is None or power is None or not prev_t:
        return wh
    dt = now - prev_t
    if not 20 < dt <= 300:
        return wh
    expect = power * dt / 3600
    delta = wh - prev if wh >= prev else wh
    if expect > 0.05 and delta > 0 and (delta > 5 * expect or delta < 0.2 * expect):
        with _cloud:
            _energy["ignore"] = True
        return None
    return wh


def read_selected():
    """Moc [W] i licznik [Wh] wybranego gniazdka. Bez zmiany stanu przekaznika."""
    with _cloud:
        session = _session()
        if not session or not session.get("device_id"):
            raise RuntimeError("wybierz gniazdko SmartLife (Ustawienia → Rekuperacja)")
        device_id = session["device_id"]
        manager = _manager(session)
    try:
        found = manager.device_repository.query_devices_by_ids([device_id])
    except Exception as e:  # noqa: BLE001
        text = str(e)
        if "sign invalid" in text.lower():
            raise RuntimeError("sesja SmartLife wygasła – połącz konto ponownie w Ustawieniach") from e
        raise
    device = next((d for d in found if d.id == device_id), None)
    if device is None:
        raise RuntimeError("gniazdko zniknęło z konta SmartLife")
    got = _scaled(device, "cur_power")
    power = None if got is None else got[0]
    return {"power": power, "total_wh": _total_wh(device, power)}
