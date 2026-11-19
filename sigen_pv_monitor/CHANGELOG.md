# Changelog – dodatek Home Assistant

Zmiany aplikacji: [CHANGELOG.md](https://github.com/mgrys/Sigen/blob/main/CHANGELOG.md).

## 1.29.1
- Poprawka instalacji: dodatek budował się na obrazie Alpine i przerywał się błędem `apt-get: not found`.
  Teraz zawsze obraz Python (Debian), bez przestarzałego `build.yaml`.

## 1.29.0
- Pierwsza wersja dodatku: aplikacja w panelu bocznym HA (Ingress), opcje PIN / adres falownika, współrzędne i strefa
  czasowa z ustawień HA, automatyczna instalacja kart panelu (plik startowy i zasób).
