# Changelog

Format wg [Keep a Changelog](https://keepachangelog.com/pl/1.1.0/), wersje wg [SemVer](https://semver.org/lang/pl/).

## [1.5.0] - 2026-10-21

### Dodano
- **Pstryk (opcjonalnie):** integracja z oficjalnym API Pstryka (tylko odczyt), włączana kluczem API
  w zakładce Ustawienia (pole maskowane, klucz zostaje w `config.ini`). Bez klucza dashboard działa jak dotąd.
  - Karta **Licznik operatora – Pstryk** (pokazywana tylko z kluczem), dla wybranego dnia:
    - pobór i oddanie z licznika operatora, z porównaniem do liczników Sigena,
    - koszt, sprzedaż i bilans dnia,
    - podsumowanie miesiąca, rozbicie kosztów (energia, dystrybucja, opłaty stałe, akcyza, VAT) i tabela godzinowa.
  - Ceny energii: linia **Pstryk (brutto)** na wykresie, kafel z bieżącą ceną Pstryka; najtańsze godziny według cen Pstryka.
  - Historia godzinowa w tabeli `pstryk_hour` : synchronizacja co 15 min,
    przy pierwszym uruchomieniu uzupełniane jest 35 dni wstecz.

## [1.4.0] - 2026-10-21

### Dodano
- Ustawienia falownika: duży kafel **Alarmy instalacji i falownika** (tylko odczyt, co 30 s).
  - Na zielono „Brak alarmów”, a przy alarmie jego nazwa na czerwono, z oznaczeniem źródła (falownik / instalacja).
  - Czytane rejestry: 30605/30606 (falownik) i 30027/30028 (instalacja).
  - **AFCI** – wykrycie łuku elektrycznego po stronie DC (Alarm1 bit 8) i usterka układu samotestu AFCI
    (Alarm2 bit 3) są wyróżnione.
  - Każda zmiana stanu alarmów trafia do historii („Historia zmian ustawień i alarmów”).

### Zmieniono
- W tabelach ustawień i odczytów falownika nie są już pokazywane numery rejestrów.

## [1.3.0] - 2026-10-21

### Zmieniono
- Prognoza na dziś to teraz **prognoza z dnia poprzedniego**, czyli ostatnia pobrana przed północą.
  Wykres, „Prognoza do teraz” i „Realizacja prognozy” porównują produkcję właśnie z nią.
  - Przebieg prognozy jest zamrażany o północy (tabela `forecast_src_da`).
  - Bieżąca, aktualizowana w ciągu dnia wartość jest widoczna tylko pomocniczo („aktualna X kWh”).
  - Prognoza na jutro jest aktualizowana do północy.
  - Bez prognozy z dnia poprzedniego (np. pierwszy dzień działania) pokazywana jest bieżąca, z ostrzeżeniem.

## [1.2.0] - 2026-10-21

### Dodano
- Straty falownika podzielone wg metodyki HTW Berlin (Energy Storage Inspection) na trzy kafelki dnia,
  z rozwijanym opisem metody:
  - straty konwersji,
  - zużycie własne w czuwaniu (średnia moc i czas),
  - odchyłki regulacji: pobór z sieci mimo rozładowującego się magazynu i oddanie do sieci mimo ładowania.
- Na żywo: oznaczenie „(czuwanie)” przy stratach, gdy falownik nic nie przetwarza.
- Prognoza: przy aktualizacji i w kafelkach widać dzień i godzinę pobrania prognozy, a także wartość prognozy
  z poprzedniego dnia, jeśli jest.

### Poprawiono
- Oś Y wykresów pokazuje zaokrąglone wartości zamiast np. 0.6000000000000001 kW.

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
