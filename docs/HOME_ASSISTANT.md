# Karty Sigen PV Monitor w Home Assistant

Od wersji 1.27 aplikacja udostępnia **karty panelu Home Assistant** z tymi samymi danymi co zakładka „Instalacja PV”.
Karty pobierają dane z aplikacji Sigen PV Monitor (musi działać w tej samej sieci). Zmiany ustawień z HA – tylko po PIN-ie.

## Karty

| Karta | Co pokazuje |
|---|---|
| `custom:sigen-live-card` | Teraz: przepływy PV / dom / magazyn / sieć, SOC, strategia teraz |
| `custom:sigen-strategy-card` | Strategia osobista: kafle, wykres planu, plan godzinowy, ustawienia strategii i sterowanie (PIN) |
| `custom:sigen-period-card` | Aktualne pomiary – Dziś / Tydzień / Miesiąc, zużycie własne falownika, wykres słupkowy |
| `custom:sigen-prices-card` | Ceny energii dziś i jutro |
| `custom:sigen-forecast-card` | Prognoza produkcji PV, trafność, korekta wg zachmurzenia |
| `custom:sigen-strings-card` | Połacie PV (Dziś / Tydzień / Miesiąc) i produkcja w miesiącach |
| `custom:sigen-eff-card` | Sprawność falownika i magazynu |
| `custom:sigen-pstryk-card` | Licznik operatora (Pstryk), faktura, porównanie ze sprzedawcą taryfowym |
| `custom:sigen-inverter-card` | Ustawienia falownika (tylko odczyt): alarmy, granice SOC, limity, Remote EMS |

Wspólna opcja każdej karty: `api: http://192.168.1.50:8000` (adres aplikacji; domyślnie ten sam host co HA, port 8000)
oraz `title:` (nagłówek; `""` – bez nagłówka).

## Instalacja

1. **Plik startowy w HA.** Pobierz `http://<adres aplikacji>:8000/ha/sigen-loader.js` i zapisz w HA jako
   `/config/www/sigen/sigen-loader.js` (np. dodatkiem „File editor” albo „Samba share”).
   Jeśli folder `/config/www` był pusty / nie istniał – uruchom HA ponownie (Ustawienia → System → Uruchom ponownie).
2. **Zasób.** Profil użytkownika → włącz „Tryb zaawansowany”; Ustawienia → Pulpity → ⋮ → **Zasoby** → Dodaj zasób:
   URL `/local/sigen/sigen-loader.js`, typ **Moduł JavaScript**.
3. Odśwież stronę HA (Ctrl+F5) → pulpit → Edytuj → Dodaj kartę → wyszukaj **„Sigen”**.

Loader wczytuje karty na bieżąco z aplikacji (`/ha/sigen-cards.js`), więc po aktualizacji aplikacji wystarczy odświeżyć HA.
Gdy aplikacja działa na innym komputerze niż HA, w karcie ustaw `api: http://<adres aplikacji>:8000` albo przed loaderem
zdefiniuj `window.SIGEN_API`.

Uproszczenie (tylko w sieci domowej, bez dostępu z zewnątrz): jako zasób można dodać bezpośrednio
`http://<adres aplikacji>:8000/ha/sigen-cards.js` (Moduł JavaScript) – bez kopiowania pliku do HA.

## Zmiany z HA (PIN)

Ustawienia strategii, tryb sterowania, pauza i polecenia ręczne w karcie strategii wymagają **PIN-u aplikacji**
(Ustawienia → Strona → PIN). PIN wpisuje się raz w karcie – przeglądarka / aplikacja HA na telefonie pamięta sesję przez rok.
Bez PIN-u karty tylko pokazują dane.

Aplikacja pozwala na odczyt i zapis z HA tylko ze stron na porcie **8123**. Inny adres HA (np. https przez serwer
pośredniczący) – `config.ini`, sekcja `[ha]`, `origins = https://ha.moj-adres` (po przecinku kilka).

**https:** gdy HA jest otwierany po https, a aplikacja działa po http, przeglądarka zablokuje karty – aplikacja też musi
być dostępna po https (np. przez ten sam serwer pośredniczący).

## Pulpit – przykład (jedna kolumna)

```yaml
views:
  - title: Sigen PV
    path: sigen-pv
    type: panel
    cards:
      - type: vertical-stack
        cards:
          - type: custom:sigen-live-card
          - type: custom:sigen-strategy-card
          - type: custom:sigen-period-card
          - type: custom:sigen-prices-card
          - type: custom:sigen-forecast-card
          - type: custom:sigen-strings-card
          - type: custom:sigen-eff-card
          - type: custom:sigen-pstryk-card
          - type: custom:sigen-inverter-card
```
