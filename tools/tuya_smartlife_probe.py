"""
Odczyt gniazdka SmartLife tą samą drogą co integracja Tuya w Home Assistant 2026.10.

Nie używa Home Assistanta ani konta deweloperskiego na iot.tuya.com.
User Code jest na stałe w aplikacji: Ja → zębatka → Konto i bezpieczeństwo → User Code.
Skrypt zamienia go na kod QR (ważny kilka minut). Po zeskanowaniu w SmartLife
pobiera listę urządzeń i wypisuje moc, prąd, napięcie i licznik energii.
Niczego nie przełącza.

  .venv/bin/pip install 'tuya-device-sharing-sdk==0.2.15' cryptography qrcode pillow
  .venv/bin/python tools/tuya_smartlife_probe.py --user-code KOD
  .venv/bin/python tools/tuya_smartlife_probe.py          # kolejny odczyt z zapisanego tokenu

Token ląduje w tools/.tuya_session.json (poza gitem). Klucze lokalne urządzeń nie są wypisywane.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from tuya_sharing import LoginControl, Manager, SharingTokenListener

# Te same stałe co homeassistant/components/tuya (HA 2026.10.2).
CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
SCHEMA = "haauthorize"
QR_PREFIX = "tuyaSmart--qrLogin?token="
MEASURES = ("cur_power", "cur_current", "cur_voltage", "add_ele")

HERE = Path(__file__).resolve().parent
SESSION = HERE / ".tuya_session.json"
QR_PNG = HERE / ".tuya_login_qr.png"
LOGIN_WAIT_S = 180


class _TokenSaver(SharingTokenListener):
    def __init__(self, session):
        self.session = session

    def update_token(self, token_info):
        self.session["token_info"] = token_info
        _save(self.session)


def _save(session):
    tmp = SESSION.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(session), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(SESSION)


def _load():
    if not SESSION.exists():
        return None
    return json.loads(SESSION.read_text(encoding="utf-8"))


def _scaled(device, code):
    raw = device.status.get(code)
    if raw is None:
        return None
    unit, scale = "", 0
    rng = (device.status_range or {}).get(code)
    if rng is not None and rng.values:
        try:
            spec = json.loads(rng.values)
        except json.JSONDecodeError:
            spec = {}
        unit = spec.get("unit") or ""
        try:
            scale = int(spec.get("scale") or 0)
        except (TypeError, ValueError):
            scale = 0
    shown = raw / (10 ** scale) if isinstance(raw, (int, float)) and scale else raw
    return shown, unit


def _login(user_code):
    import qrcode

    login = LoginControl()
    response = login.qr_code(CLIENT_ID, SCHEMA, user_code)
    if not response.get("success"):
        raise SystemExit(
            f"Logowanie odrzucone ({response.get('code')}): {response.get('msg')}"
        )
    token = response["result"]["qrcode"]
    payload = QR_PREFIX + token
    img = qrcode.make(payload)
    img.save(QR_PNG)
    print(f"Kod QR zapisany: {QR_PNG}")
    print("Zeskanuj go w SmartLife (ikona skanu). Czekam do 3 minut.")
    if sys.platform == "darwin":
        subprocess.run(["open", str(QR_PNG)], check=False)
    deadline = time.time() + LOGIN_WAIT_S
    last = None
    while time.time() < deadline:
        ok, info = login.login_result(token, CLIENT_ID, user_code)
        if ok:
            session = {
                "user_code": user_code,
                "terminal_id": info["terminal_id"],
                "endpoint": info["endpoint"],
                "username": info.get("username"),
                "token_info": {
                    "t": info["t"],
                    "uid": info["uid"],
                    "expire_time": info["expire_time"],
                    "access_token": info["access_token"],
                    "refresh_token": info["refresh_token"],
                },
            }
            _save(session)
            print(f"Zalogowano: {session.get('username') or '(bez nazwy konta)'}")
            return session
        code = info.get("code")
        if code != last:
            print(f"jeszcze nie: {code} {info.get('msg')}")
            last = code
        if code == "USERCODE_INCORRECT":
            raise SystemExit("User Code odrzucony.")
        time.sleep(2)
    raise SystemExit("Minął czas na zeskanowanie kodu QR.")


def _read(session):
    manager = Manager(
        CLIENT_ID,
        session["user_code"],
        session["terminal_id"],
        session["endpoint"],
        session["token_info"],
        _TokenSaver(session),
    )
    print("Pobieram urządzenia z chmury SmartLife…")
    manager.update_device_cache()
    devices = list(manager.device_map.values())
    plugs = [d for d in devices if any(c in (d.status or {}) for c in MEASURES)]
    print(f"Urządzeń: {len(devices)}, z pomiarem mocy: {len(plugs)}")
    for device in sorted(plugs, key=lambda d: (d.name or "").lower()):
        bits = []
        for code in MEASURES:
            got = _scaled(device, code)
            if got is None:
                continue
            shown, unit = got
            text = f"{shown:g}" if isinstance(shown, float) else str(shown)
            bits.append(f"{code}={text} {unit}".rstrip())
        mark = "  ← rekuperator" if "rekuper" in (device.name or "").lower() else ""
        online = "online" if device.online else "offline"
        print(f"{device.name}  {online}  {device.product_name or ''}  {', '.join(bits)}{mark}")


def main():
    ap = argparse.ArgumentParser(description="Próbny odczyt SmartLife jak w Home Assistant")
    ap.add_argument("--user-code", help="User Code z aplikacji SmartLife")
    args = ap.parse_args()
    session = _login(args.user_code.strip()) if args.user_code else _load()
    if not session:
        raise SystemExit(
            "Brak zapisanego logowania. Podaj User Code:\n"
            "  .venv/bin/python tools/tuya_smartlife_probe.py --user-code KOD"
        )
    _read(session)


if __name__ == "__main__":
    main()
