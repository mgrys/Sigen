"""
1.31.1: certyfikaty HTTPS przy pobieraniu danych z internetu (PSE, Pstryk, Open-Meteo, forecast.solar, Pradcast).

Program antywirusowy (skanowanie HTTPS: ESET, Avast, Kaspersky, Bitdefender...), firmowa zapora / proxy albo filtr w routerze
podmienia certyfikaty stron na wlasne. Przegladarka im ufa (certyfikat dopisany do systemu), a Python - nie, i kazde
pobranie konczy sie bledem CERTIFICATE_VERIFY_FAILED ("self-signed certificate in certificate chain").

- domyslnie: certyfikaty zaufane przez system (pakiet truststore: magazyn Windows / pek kluczy macOS / certyfikaty Linuksa),
- [siec] plik_certyfikatow = sciezka do pliku PEM (np. certyfikat firmowego proxy) - zamiast powyzszego (Linux, dodatek HA),
- explain(blad) - zrozumialy komunikat po polsku zamiast technicznego bledu SSL.
Sprawdzanie certyfikatow nie jest nigdy wylaczane (klucze API ida szyfrowanym polaczeniem).
"""
import ssl

MODE = {"mode": "python", "ca_file": None, "error": None}


def setup(ca_file=""):
    """Wywolywane raz przy starcie, przed pierwszym pobraniem. Zwraca opis trybu (do /api/version i logu)."""
    ca_file = (ca_file or "").strip().strip('"')
    if ca_file:
        try:
            ctx = ssl.create_default_context()
            ctx.load_verify_locations(cafile=ca_file)
        except Exception as e:  # noqa: BLE001
            MODE.update(error=f"plik certyfikatów {ca_file}: {e}")
        else:
            def _ctx():
                c = ssl.create_default_context()
                c.load_verify_locations(cafile=ca_file)
                # certyfikaty antywirusow / proxy bywaja niezgodne z RFC 5280 (np. bez "key usage") - Python 3.13 odrzuca je
                # w trybie scislym; lancuch i nazwa serwera dalej sa sprawdzane
                c.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
                return c
            ssl._create_default_https_context = _ctx       # urllib (urlopen bez context) bierze kontekst stad
            MODE.update(mode="plik", ca_file=ca_file, error=None)
            return MODE
    try:
        import truststore
        truststore.inject_into_ssl()
        MODE.update(mode="system")
    except Exception as e:  # noqa: BLE001 - brak pakietu: zostaja certyfikaty Pythona
        MODE.update(mode="python", error=MODE["error"] or f"truststore: {e}")
    return MODE


def describe():
    m = MODE["mode"]
    return {"system": "certyfikaty systemu", "plik": f"plik certyfikatów {MODE['ca_file']}",
            "python": "certyfikaty Pythona"}.get(m, m) + (f" (uwaga: {MODE['error']})" if MODE["error"] and (m != "system" or MODE["error"].startswith("plik")) else "")


def explain(err):
    """Tekst bledu dla uzytkownika: przechwytywanie HTTPS / brak sieci po polsku, reszta bez zmian."""
    s = str(err)
    if "CERTIFICATE_VERIFY_FAILED" in s or "certificate verify failed" in s:
        why = ("certyfikat samopodpisany w łańcuchu" if "self-signed" in s or "self signed" in s else
               "certyfikat wygasł" if "expired" in s else "nieznany wystawca certyfikatu")
        return (f"połączenie HTTPS jest przechwytywane ({why}) – zwykle robi to antywirus ze skanowaniem HTTPS, firmowa "
                "zapora albo filtr w routerze. Wyłącz skanowanie HTTPS dla tej aplikacji albo w Ustawieniach → Sieć wskaż "
                "plik certyfikatu (PEM) programu / proxy.")
    if "getaddrinfo failed" in s or "Name or service not known" in s or "Temporary failure in name resolution" in s:
        return "brak internetu albo DNS (nie można znaleźć serwera)"
    return s
