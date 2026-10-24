# Changelog

Format wg [Keep a Changelog](https://keepachangelog.com/pl/1.1.0/), wersje wg [SemVer](https://semver.org/lang/pl/).

## [1.16.0] - 2026-10-24

### Dodano
- Rekuperacja: **automatyczne rozpoznanie modelu Vallox** z rekuperatora (A_CYC_MACHINE_MODEL) – wystarczy wpisać adres IP.
  Na tej podstawie dobierany jest maks. przepływ (096 / 110 / 145 / 245 MV, także odpowiedniki DV… Adroit);
  dla modelu spoza listy – przepływ do wpisania w Ustawieniach. Ustawienie `model = auto` (domyślne dla nowych instalacji);
  ręczny wybór modelu dalej możliwy.
- Wykryty model na grafice rekuperatora i w nagłówku karty; wiersz „Model” ze źródłem (odczytany / wybrany / wpisany).

### Dokumentacja
- README: osobna sekcja Rekuperacja (wymagania, ustawienia, szacunek przepływu, sterowanie, szczegółowo automaty,
  planowane czujniki CO₂ SONOFF SAWF-08P, miernik poboru) ze zrzutami ekranu.

## [1.15.1] - 2026-10-23

### Poprawiono
- Rekuperacja: **wybór modelu Vallox** trafia do wydania (1.15.0 liczyło przepływ zawsze jak dla 110 MV, 385 m³/h przy
  3300 obr/min). Przepływ = maks. przepływ modelu (osobno nawiew i wywiew) × obroty ÷ obroty przy 100 %;
  obroty przy 100 % zapamiętywane same przy pracy na ≥ 98 % (np. 145 MV: ok. 2300 / 2560 obr/min).
- Model „inny”: osobny maks. przepływ nawiewu i wywiewu.

## [1.15.0] - 2026-10-23

### Dodano
- **Rekuperacja (opcjonalnie): rekuperator Vallox seria MV** (MyVallox, lokalny websocket). Zakładka pojawia się
  po wpisaniu adresu IP w Ustawieniach → Rekuperacja; bez tego aplikacja wygląda jak dotąd.
  - Na żywo: temperatury (zewn., nawiew, wywiew, wyrzut), sprawność odzysku, profil, stan wymiennika, CO₂,
    wilgotność, filtr, nagrzewnica, przepływ m³/h i moc odzyskana.
  - **Wybór modelu** (096 / 110 / 145 / 245 MV albo inny z ręcznie wpisanym przepływem): przepływ szacowany
    z obrotów wentylatorów i maks. przepływu modelu osobno dla nawiewu i wywiewu (dane Vallox przy 100 Pa);
    obroty przy 100 % zapamiętywane automatycznie przy pracy na ≥ 98 % (np. Boost 100 %) albo wpisane w Ustawieniach.
  - Balans przepływów nawiew / wywiew z temperatur wymiennika; ręczna zmiana wentylatorów trybu Dom
    (zakres 10–80 %, maks. ±10 % na raz) i **automat korekty** (1 % co godzinę, z zabezpieczeniem).
  - Obejście wymiennika (bypass, częściowe, chłodzenie) ręcznie albo **automat komfortu** (letnie chłodzenie).
  - Nastawa temperatury nawiewu we wszystkich profilach i **automat nastawy** (lato / zima / mróz).
  - Wykresy dnia (CO₂, sprawność, wilgotność, temperatury, pobór mocy, moc odzyskana), energia odzyskana
    w dniu i miesiącu; luki uzupełniane z wewnętrznego dziennika rekuperatora; historia zmian.
  - Każda zmiana z innego urządzenia w sieci wymaga PIN-u; automaty działają też przy zamkniętej stronie.
- **Miernik poboru prądu rekuperatora** (`meter.py`, opcjonalnie): gniazdka Shelly Gen1 i Gen2/Plus
  (rozpoznawane automatycznie) – pobór teraz, zużycie i koszt dnia / miesiąca, wykrywanie pracy nagrzewnicy.
  Inny miernik (np. Tuya, Tasmota) – opis i przykład w nagłówku `meter.py`. Bez miernika automat nastawy
  w mrozie korzysta z sygnału nagrzewnicy z rekuperatora.

## [1.14.0] - 2026-10-23

### Dodano
- **Zaniki sieci (praca wyspowa / off-grid):** rejestr 30009 instalacji (0 praca z siecią, 1 off-grid – zanik
  sieci, 2 off-grid włączony ręcznie) czytany co 5 s razem z mocami.
  - Podczas zaniku: czerwony pasek „Brak sieci – praca wyspowa” (od kiedy, ile trwa, SOC), czerwonawe tło strony,
    w schemacie „Sieć: brak”.
  - Historia zaników w `grid_events.json` (folder danych) i na stronie: „Ostatni zanik sieci” w karcie na żywo
    oraz rozwijana lista; `GET /api/grid_events`, a `/api/live` ma pola `grid_mode`, `offgrid`, `offgrid_since`,
    `last_outage`.
  - Wykrywanie działa, gdy komputer z aplikacją i router mają w tym czasie zasilanie (np. z obwodów rezerwowych).
- `.gitignore`: `grid_events.json` i `access_sessions.json` (przy uruchomieniu ze źródeł nie trafią do repozytorium).

### Poprawiono
- Aplikacja Android: **czarny ekran** po chwilowym braku połączenia (np. restart aplikacji na komputerze, telefon
  poza Wi-Fi). Wcześniej po błędzie okno zostawało puste, a Android potrafił je takim przywrócić. Teraz:
  - strona błędu z przyciskami „Ponów” i „Zmień adres” oraz automatycznym ponawianiem co 10 s,
  - po powrocie do aplikacji (po błędzie albo po ponad 10 min w tle) dashboard ładuje się od nowa,
  - awaria procesu strony (np. brak pamięci) tworzy nowe okno zamiast czarnego ekranu,
  - zmiana trybu ciemnego / rozmiaru czcionki nie przeładowuje okna,
  - strona zawsze pobierana z komputera (bez starej kopii w pamięci telefonu).
- README: nowe zrzuty ekranu.

## [1.13.0] - 2026-10-23

### Dodano
- Instalator: opcja **„Uruchamiaj w tle jako usługę Windows”** (domyślnie wyłączona, wymaga zgody administratora) –
  start z systemem także bez logowania, automatyczny restart po awarii (WinSW 2.12, licencja MIT). Dane i ustawienia
  zostają w folderze użytkownika (`%LOCALAPPDATA%\SigenPVMonitor`). Odinstalowanie programu usuwa też usługę.

### Zmieniono
- Przy pracy jako usługa: restart z zakładki Ustawienia ponownie uruchamia usługę, a przycisk „Zamknij aplikację”
  podpowiada, jak zatrzymać usługę.

## [1.12.1] - 2026-10-23

### Poprawiono
- Budowanie aplikacji Android w GitHub Actions (w 1.12.0 plik APK nie trafił do wydania).

## [1.12.0] - 2026-10-23

### Dodano
- **Aplikacja Android** (`SigenPV-android-x.y.z.apk` w wydaniu): dashboard na pełnym ekranie telefonu, adres komputera
  podawany przy pierwszym uruchomieniu, okno błędu połączenia z ponowieniem i zmianą adresu.
- **PIN do zmian z innych urządzeń** w sieci (Ustawienia → Strona). Podgląd bez hasła; zapis ustawień, restart i
  wyszukiwanie falownika z telefonu dopiero po PIN-ie (urządzenie zapamiętywane na rok). 5 błędnych prób = 5 min blokady.
  Na komputerze z aplikacją bez zmian.

## [1.11.1] - 2026-10-23

### Dodano
- Sprawność falownika: pole „ukryj przedziały z * (< 15 min danych)”, domyślnie wyłączone.

## [1.11.0] - 2026-10-22

### Zmieniono
- Prognoza PV: **wybór prognoz do wykresu** (można zaznaczyć kilka, np. ECMWF i Open-Meteo).
  - Dla każdej wybranej prognozy są dwie linie: dziś (kreskowana) i jutro (kropkowana), w stałym kolorze źródła.
  - Na wykresie zostaje też rzeczywista produkcja i rozrzut modeli (można go wyłączyć).
  - Wybór jest zapamiętywany w przeglądarce.
- Realna produkcja PV – połacie: oś czasu wykresu dnia obejmuje okres od pierwszej do ostatniej minuty z produkcją, a nie od wschodu do zachodu słońca.
- Licznik operatora (Pstryk), tabela godzinowa: godziny, w których koszt jest większy od sprzedaży, mają czerwone tło, a te, w których sprzedaż przewyższa koszt, zielone.

## [1.10.3] - 2026-10-22

### Poprawiono
- Strategia osobista: siatka stanu magazynu jest 3× gęstsza i zaokrąglana do najbliższej wartości. Wcześniejsze zaokrąglanie w dół gubiło do 0,2 kWh na godzinę, więc plan zaniżał opłacalność magazynu.

## [1.10.2] - 2026-10-22

### Zmieniono
- Czytelniejsze kafle prognozy:
  - przy głównej prognozie dopisek „(główna) · cały dzień”, a inne źródła jako „dla porównania”,
  - „Realizacja prognozy” podaje „wyprodukowano X z Y kWh przewidzianych do HH:MM”, z jakiej prognozy i kiedy pobranej,
  - przy prognozie bieżącej podana jest godzina pobrania.
  Główne źródło wybierasz w zakładce Ustawienia → Prognoza.

## [1.10.1] - 2026-10-22

### Poprawiono
- Linie włączone lub wyłączone w legendzie wykresu temperatur nie resetują się już przy odświeżeniu.

## [1.10.0] - 2026-10-22

### Zmieniono
- Wykres temperatur jest **dobowy, od północy do północy**, z wyborem dnia (‹ › / kalendarz / Dziś) i linią „teraz”.
  Widoki 7 i 30 dni kończą się na wybranej dobie. API: `/api/bat_temps?date=RRRR-MM-DD&days=1`.

## [1.9.0] - 2026-10-22

### Dodano
- Na dole strony:
  - **formularz uwag do autora**: rodzaj zgłoszenia, wiadomość i opcjonalny kontakt; wysyłka e-mailem przez program pocztowy albo jako zgłoszenie (issue) na GitHubie,
  - opcjonalnie dołączana wersja aplikacji i przeglądarki (bez adresów IP i kluczy),
  - stopka z autorem (Marek Grys), linkiem do repozytorium i licencją.

## [1.8.2] - 2026-10-22

### Zmieniono
- Temperatura ogniw i falownika na **jednym wykresie**: linie ogniw (średnio, maks., min.) i falownika (PCS),
  zielone pole zakresu korzystnego dla ogniw, linia 40 °C (falownik: możliwe ograniczanie mocy).
  - Sześć kafli w jednym rzędzie: ogniwa teraz, w okresie, % w zakresie; falownik teraz, maks. w okresie, różnica falownik − ogniwa.

## [1.8.1] - 2026-10-22

### Dodano
- Ustawienia falownika: osobny wykres **Temperatura falownika (PCS)** pod temperaturą ogniw:
  - temperatura wewnętrzna części mocy falownika,
  - progi Sigen dla temperatury otoczenia: zalecana 10–35 °C, ograniczanie mocy powyżej 40 °C, maksimum pracy 60 °C (TP2),
  - kafle: temperatura teraz, maksimum w okresie, różnica falownik − ogniwa.

### Zmieniono
- Opis temperatury ogniw: poniżej 0 °C bateria SigenStor najpierw się grzeje (do ok. 2 h), dopiero potem się ładuje (wg instrukcji Sigen).

## [1.8.0] - 2026-10-22

### Dodano
- Ustawienia falownika: zamiast grupy „Zadawanie mocy (nie ruszać)” jest wykres **Temperatura ogniw magazynu**:
  - średnia, maksymalna i minimalna temperatura ogniw, a do włączenia temperatura falownika (PCS),
  - zakres 24 h, 7 lub 30 dni,
  - zielone pole z zakresem korzystnym dla ogniw LFP (15–35 °C),
  - kafle: temperatura teraz, zakres w okresie, procent czasu w zakresie korzystnym, temperatura falownika.
  - Zapis co 60 s w tabeli `bat_temps`. Nowe rejestry: maks. i min. temperatura baterii (30620, 30621).
  - API: `/api/bat_temps?days=1|7|30`.

### Usunięto
- Grupa „Zadawanie mocy” w ustawieniach falownika: rejestry 40001–40007, 40030, 41500 nie są już odczytywane.

## [1.7.0] - 2026-10-22

### Dodano
- Realna produkcja PV – połacie: obok wykresu dnia (teraz na pół szerokości) kafel **Produkcja w miesiącach**.
  - Prosty wykres słupkowy z sumaryczną produkcją PV w każdym miesiącu (ostatnie 12 miesięcy).
  - Wartości z licznika narastającego produkcji falownika, tego samego co „Produkcja PV (instalacja)”.
  - Energia sprzed pierwszego odczytu (od uruchomienia instalacji) jest doliczana do pierwszego miesiąca.
  - API: `/api/pv_monthly`.

## [1.6.1] - 2026-10-22

### Zmieniono
- Strategia osobista (nowe parametry w `[strategia]`: `wartosc_sprzedazy`, `koszt_zuzycia_zl_kwh`):
  - okno zawsze od dziś 00:00 do jutra 24:00; minione godziny pokazują rzeczywiste odczyty, a pionowa linia oznacza „teraz”,
  - słupki pokazują zakup (w dół, na czerwono) i sprzedaż (w górę, na zielono) w zł na godzinę; dodano linię ceny sprzedaży,
  - oszczędność uwzględnia różnicę stanu magazynu na koniec okresu; kafel „Razem dziś + jutro”,
  - koszt zużycia baterii za każdą kWh oddaną z magazynu (domyślnie 0,10 zł/kWh) – plan nie cykluje magazynu dla grosza zysku,
  - wartość sprzedaży = RCE × współczynnik (domyślnie 1,0); bez cen na jutro cena sprzedaży liczona ostrożniej (× 0,9),
  - opis „standby – PV na sprzedaż”, gdy w trybie standby nadwyżka PV trafia do sieci.

## [1.6.0] - 2026-10-22

### Dodano
- **Strategia osobista – symulacja** (karta pod cenami energii). **Nic nie jest wysyłane do falownika.**
  - Godzinowy plan pracy magazynu od bieżącej godziny do końca jutra, z trzema trybami:
    - autokonsumpcja,
    - standby: magazyn czeka, a dom bierze prąd z sieci, np. w tanie noce zimą,
    - ładowanie z sieci w najtańszych godzinach.
  - Plan wybiera najtańszy wariant (programowanie dynamiczne). Uwzględnia:
    - prognozę PV,
    - typowe zużycie domu w każdej godzinie (średnia z 14 dni),
    - ceny godzinowe (z Pstryka, jeśli jest klucz, w przeciwnym razie z PSE i taryfy),
    - sprawność ładowania i rozładowania,
    - granice SOC z falownika.
  - Plan ogranicza liczbę przełączeń trybu.
  - Kafle: tryb teraz, następna zmiana, oszczędność na resztę dnia i na jutro względem samej autokonsumpcji.
  - Wykres SOC (plan i bez strategii), ceny i poboru z sieci; tabela godzinowa i opis metody.
  - API: `/api/strategy`. Parametry w `config.ini`, sekcja `[strategia]`: pojemność (puste = z falownika),
    moc ładowania oraz sprawność ładowania i rozładowania.

### Zmieniono
- Kafle Pstryka w kolorach: pobór i koszt na czerwono, oddanie i sprzedaż na zielono, bilans na niebiesko.

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
