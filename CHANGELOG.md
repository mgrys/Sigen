# Changelog

Format wg [Keep a Changelog](https://keepachangelog.com/pl/1.1.0/), wersje wg [SemVer](https://semver.org/lang/pl/).

## [1.1.0] - 2026-10-21

### Dodano
- Ustawienia: przycisk „Znajdź falownik” obok pola adresu IP.
  - Skanuje sieć domową, czyli podsieci /24 komputera, w poszukiwaniu otwartego portu Modbus TCP (502).
  - Dla każdego trafienia odczytuje model falownika i SOC magazynu (tylko odczyt).
  - Przycisk „Użyj” wpisuje znaleziony adres; urządzenia, które nie są falownikiem Sigenergy, są oznaczone.
- API: `POST /api/discover` (`{"port": 502, "network": "192.168.0"}`, oba pola opcjonalne; tylko sieci prywatne).

## [1.0.0] - 2026-10-21

Pierwsze publiczne wydanie.

### Dodano
- Podgląd na żywo co 5 s: PV, dom, magazyn (SOC), sieć, moc AC falownika i straty przetwarzania.
  Straty są liczone z mocy DC stringów, a minuty z rozjechanymi odczytami są odrzucane.
- Energia dnia z liczników falownika:
  - wymiana z siecią z liczników punktu przyłączenia,
  - autokonsumpcja i samowystarczalność,
  - wykres mocy, tabela godzinowa, historia dni.
- Prognoza PV dla każdej połaci na dziś i jutro:
  - Open-Meteo (6 modeli i ich średnia) oraz forecast.solar,
  - porównanie z rzeczywistą produkcją i trafność prognoz z dnia poprzedniego,
  - wykresy od wschodu do zachodu słońca.
- Realna produkcja stringów PV1–PV4 (napięcie × prąd).
- Ceny energii z PSE (RCE, SDAC) z taryfą dystrybucyjną G11, G12, G13… dla 5 operatorów,
  konfigurowalnym wzorem ceny zakupu i wskazaniem najtańszych godzin.
- Sprawność falownika w przedziałach mocy, zapisywana dziennie.
- 59 odczytów falownika i ustawienia (tylko odczyt) z historią zmian.
- Baza SQLite, konfiguracja w `config.ini` i zakładka **Ustawienia** z zapisem i ponownym uruchomieniem.
- Wersja dla Windows bez Pythona: instalator (Inno Setup, autostart opcjonalnie) i wersja przenośna (PyInstaller).
  Buduje je GitHub Actions dla tagów `v*`.
- Chart.js dołączony lokalnie, więc strona działa także bez dostępu do CDN.
