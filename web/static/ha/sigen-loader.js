// Sigen Dashboard – karty Home Assistant (loader).
// Ten plik leży w HA: /config/www/sigen/sigen-loader.js  →  zasób panelu: /local/sigen/sigen-loader.js (moduł JavaScript).
// Same karty pobierane są na bieżąco z aplikacji Sigen (bez cache), więc po aktualizacji aplikacji wystarczy odświeżyć HA.
// Adres aplikacji: ten sam host co HA, port 8000 (działa lokalnie i przez Tailscale). Inny adres: window.SIGEN_API
// albo w karcie opcja  api: http://192.168.0.121:8000
(() => {
  const base = (window.SIGEN_API || `${location.protocol}//${location.hostname}:8000`).replace(/\/$/, '');
  window.SIGEN_API_DEFAULT = base;
  if (location.protocol === 'https:' && base.startsWith('http:'))
    console.warn('Sigen: HA działa po https, a aplikacja po http – przeglądarka zablokuje karty (ustaw window.SIGEN_API na adres https)');
  import(`${base}/ha/sigen-cards.js?v=${Date.now()}`)
    .catch(e => console.error('Sigen: nie można wczytać kart z', base, e));
})();
