# Sigen PV Monitor – dodatek Home Assistant

Aplikacja [Sigen PV Monitor](https://github.com/mgrys/Sigen) działająca jako dodatek HA: panel w pasku bocznym
(„Sigen PV”) i 9 kart panelu z danymi falownika i magazynu Sigenergy.

## Wymagania

- Home Assistant OS albo Supervised (dodatki). HA w Dockerze / Core – aplikację uruchamia się osobno
  (Windows, Linux, macOS, opis w README), a karty instaluje wg `docs/HOME_ASSISTANT.md`.
- Falownik Sigenergy z włączonym **Modbus TCP** (mySigen → Urządzenie → Ustawienia → Modbus TCP).
- Procesor 64-bit (amd64 / aarch64, np. Raspberry Pi 4/5 z 64-bitowym HA OS).

## Instalacja

1. Ustawienia → Dodatki → **Sklep z dodatkami** → ⋮ (prawy górny róg) → **Repozytoria** → dodaj
   `https://github.com/mgrys/Sigen`.
2. Na liście pojawi się **Sigen PV Monitor** → Zainstaluj (pierwsza instalacja buduje dodatek kilka minut).
3. Zakładka **Konfiguracja**: PIN, adres falownika (albo puste – patrz niżej), zapisz.
4. **Uruchom**, włącz „Pokaż na pasku bocznym”. W pasku bocznym pojawi się **Sigen PV**.
5. Brak adresu falownika: Sigen PV → Ustawienia → **Znajdź falownik** (skan sieci domowej, tylko odczyt) → zapisz.
   Tam też: połacie (moc, azymut, nachylenie), operator i taryfa, Pstryk, rekuperator Vallox.

Współrzędne i strefa czasowa instalacji przy pierwszym starcie są brane z ustawień HA (Ustawienia → System → Ogólne).

## Opcje

| Opcja | Opis |
|---|---|
| PIN aplikacji | Zmiany ustawień z telefonu (aplikacja Android, przeglądarka) i z kart panelu HA. Panel w pasku bocznym HA działa bez PIN-u (HA już zalogował użytkownika). |
| Adres IP falownika | Puste = bez zmian (ustaw w aplikacji). |
| Port Modbus TCP | Zwykle 502. |
| Karty panelu HA | Plik startowy kart w `/config/www/sigen/` i zasób panelu `/local/sigen/sigen-loader.js` (dodawany automatycznie, gdy pulpity są edytowane w interfejsie). |

## Karty panelu

Pulpit → Edytuj → Dodaj kartę → wyszukaj **„Sigen”**: teraz, strategia (z ustawieniami i sterowaniem), aktualne
pomiary, ceny, prognoza PV, połacie, sprawność, licznik Pstryk, ustawienia falownika. Opis kart i opcji:
`docs/HOME_ASSISTANT.md` w repozytorium.

Przy pierwszej instalacji, gdy w konfiguracji HA nie było folderu `www`, uruchom HA ponownie raz
(Ustawienia → System → ⋮ → Uruchom ponownie) – dopiero wtedy HA udostępni pliki z `/local`.

## Sieć i porty

Dodatek działa w sieci hosta (wyszukiwanie falownika w sieci domowej) na porcie **8000** – tym samym adresem
korzystają karty panelu i aplikacja Android (`http://<adres HA>:8000`). Port 8000 nie może być zajęty przez inny dodatek.

## Dane

Ustawienia (`config.ini`) i baza (`sigen.db`) są w folderze danych dodatku – kopia zapasowa HA obejmuje je razem z dodatkiem.

## Sterowanie falownikiem

Domyślnie **wyłączone** – plan strategii jest tylko symulacją. Włączenie (Sigen PV → Instalacja PV → Strategia
osobista → Sterowanie falownikiem) wysyła tryby Remote EMS do falownika; przeczytaj najpierw opis w README.
