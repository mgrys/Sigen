// Sigen Dashboard – karty Home Assistant (Lovelace custom cards). Etap 0 + K1 (1.137.0), K4 (1.138.0), K2 + K3 (1.139.0).
// Wczytywane przez /local/sigen/sigen-loader.js z aplikacji Sigen (http://<host>:8000/ha/sigen-cards.js).
// Dane: API aplikacji Sigen (tylko odczyt – GET). Sterowanie falownikiem zostaje w aplikacji.
//
// Karty:
//   type: custom:sigen-live-card      – Teraz: przepływy PV / dom / magazyn / sieć, SOC, strategia teraz
//   type: custom:sigen-prices-card    – Ceny energii dziś i jutro (opcja tiles: false)
//   type: custom:sigen-period-card    – Aktualne pomiary Dziś / Tydzień / Miesiąc (opcje okres: day|week|month, losses: false)
//   type: custom:sigen-forecast-card  – Prognoza PV (kafle, wybór źródeł, trafność, korekta)
//   type: custom:sigen-strings-card   – Połacie PV Dziś / Tydzień / Miesiąc + produkcja w miesiącach (opcja months: false)
//   type: custom:sigen-inverter-card  – Ustawienia falownika: alarmy, granice SOC, limity, Remote EMS (tylko odczyt)
//   type: custom:sigen-eff-card       – Sprawność falownika (opcja okres: 1 | y | 7 | 30 | 0)
//   type: custom:sigen-pstryk-card    – Licznik operatora Pstryk (opcja g13: false)
//   type: custom:sigen-strategy-card  – Strategia osobista: kafle, sterowanie (odczyt), wykres planu, plan godzinowy
//                                       + ustawienia strategii i sterowanie (PIN aplikacji); opcje tiles / control / settings / chart / table: false
// Wspólne opcje:  api: http://192.168.0.121:8000   (domyślnie ten sam host co HA, port 8000)
//                 title: "…"                         (nagłówek karty; title: "" – bez nagłówka)
const SIGEN_CARDS_VERSION = '1.143.0';
const SIGEN_DEF_API = (window.SIGEN_API_DEFAULT || window.SIGEN_API || `${location.protocol}//${location.hostname}:8000`).replace(/\/$/, '');

// kolory serii jak w aplikacji (jasny / ciemny motyw)
const SIGEN_PAL = {
  light: { pv: '#eb6834', load: '#2a78d6', bat: '#1baf7a', grid: '#eda100', loss: '#e87ba4', sell: '#008300', buy: '#e34948', err: '#e34948', ok: '#1baf7a' },
  dark:  { pv: '#d95926', load: '#3987e5', bat: '#199e70', grid: '#c98500', loss: '#d55181', sell: '#2f9e2f', buy: '#e66767', err: '#e66767', ok: '#199e70' },
};

// wersja aplikacji: domowa albo publiczna (sigen-pv-monitor - /api/version ma "modules"); raz na adres API
const sgFlavorP = {};
function sgFlavor(api) {
  if (!sgFlavorP[api]) sgFlavorP[api] = fetch(api + '/api/version', { cache: 'no-store' }).then(r => r.json())
    .then(v => ({ pub: !!v.modules, version: v.version })).catch(() => ({ pub: false }));
  return sgFlavorP[api];
}

const SIGEN_BASE_CSS = `
  :host { display: block; }
  ha-card { overflow: hidden; }
  .sg { --sg-text: var(--primary-text-color); --sg-text2: var(--secondary-text-color); --sg-muted: var(--disabled-text-color, #8a8a8a);
        --sg-border: var(--divider-color, rgba(127,127,127,.25)); --sg-surface: var(--card-background-color, var(--ha-card-background, #fff));
        --sg-line: color-mix(in srgb, var(--sg-text) 12%, transparent);
        color: var(--sg-text); padding: 12px 16px 16px; container-type: inline-size; font-variant-numeric: tabular-nums; }
  .hdr { display: flex; align-items: center; gap: 8px; margin: 0 0 8px; }
  .hdr h2 { font-size: 18px; font-weight: 500; margin: 0; flex: 1; line-height: 1.3; }
  .st { font-size: 12px; color: var(--sg-text2); display: flex; align-items: center; gap: 6px; white-space: nowrap; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--sg-muted); flex: none; }
  .dot.ok { background: var(--sg-ok); } .dot.err { background: var(--sg-err); }
  .st.old { color: var(--sg-err); }
  a.open { color: var(--sg-text2); text-decoration: none; font-size: 15px; line-height: 1; padding: 2px 4px; border-radius: 6px; }
  a.open:hover { background: var(--sg-line); }
  .err { color: var(--sg-err); font-size: 13px; margin: 6px 0; }
`;

class SigenBase extends HTMLElement {
  // podklasy: static css, static name, polls() -> [[fn, ms], ...], build() (szkielet), title domyślny
  setConfig(cfg) {
    this._cfg = { ...(cfg || {}) };
    this.api = String(this._cfg.api || SIGEN_DEF_API).replace(/\/$/, '');
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._built = false;
    if (this.isConnected) this._build();
  }
  set hass(h) {
    this._hass = h;
    const dark = !!(h && h.themes && h.themes.darkMode);
    if (dark !== this._dark) { this._dark = dark; this._applyPal(); }
  }
  get title() { return this._cfg.title !== undefined ? this._cfg.title : this.constructor.defTitle; }
  connectedCallback() { if (!this._built && this._cfg) this._build(); this._start(); }
  disconnectedCallback() { this._stop(); }
  _applyPal() {
    const root = this.shadowRoot && this.shadowRoot.querySelector('.sg'); if (!root) return;
    const p = SIGEN_PAL[this._dark ? 'dark' : 'light'];
    for (const k in p) root.style.setProperty('--sg-' + k, p[k]);
  }
  _build() {
    if (!this._cfg) return;
    const t = this.title;
    this.shadowRoot.innerHTML = `<style>${SIGEN_BASE_CSS}${this.constructor.css || ''}</style>
      <ha-card><div class="sg">
        <div class="hdr">${t ? `<h2>${this.esc(t)}</h2>` : '<span style="flex:1"></span>'}
          <span class="st" id="st"><span class="dot" id="dot"></span><span id="sttxt">łączenie…</span></span>
          <a class="open" href="${this.api}/?full=1" target="_blank" rel="noopener" title="Otwórz aplikację Sigen">↗</a></div>
        <div class="err" id="err" hidden></div>
        ${this.body()}
      </div></ha-card>`;
    this._built = true;
    this._applyPal();
    this.afterBuild && this.afterBuild();
    if (this._running) { this._stop(); this._start(); }
  }
  $(id) { return this.shadowRoot.getElementById(id); }
  esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
  async get(path) {
    const r = await fetch(this.api + path, { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }
  // zapis (POST) i stan logowania - z ciasteczkiem sesji PIN-u aplikacji (credentials)
  async post(path, body) {
    let r;
    try {
      const res = await fetch(this.api + path, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
      r = await res.json().catch(() => ({ ok: false, error: 'HTTP ' + res.status }));
      if (res.status === 401 && r.ok === undefined) r.ok = false;
    } catch (e) { r = { ok: false, error: 'brak połączenia z aplikacją (' + (e.message || e) + ')' }; }
    return r;
  }
  async access() {
    try { return await (await fetch(this.api + '/api/access', { credentials: 'include', cache: 'no-store' })).json(); }
    catch (e) { return { authed: false, pin_set: false, error: String(e) }; }
  }
  showErr(msg) { const e = this.$('err'); if (!e) return; e.hidden = !msg; e.textContent = msg || ''; }
  setStatus(ok, txt, old) {
    const d = this.$('dot'), t = this.$('sttxt'), s = this.$('st'); if (!d) return;
    d.className = 'dot ' + (ok ? (old ? 'err' : 'ok') : 'err'); t.textContent = txt; s.classList.toggle('old', !!old);
  }
  _start() {
    if (this._running || !this._built) return;
    this._running = true; this._timers = [];
    for (const [fn, ms] of this.polls()) {
      const run = () => { if (!document.hidden) fn.call(this).catch(e => this.onFail(e)); };
      run(); this._timers.push(setInterval(run, ms));
    }
    this._vis = () => { if (!document.hidden) for (const [fn] of this.polls()) fn.call(this).catch(e => this.onFail(e)); };
    document.addEventListener('visibilitychange', this._vis);
  }
  _stop() {
    (this._timers || []).forEach(clearInterval); this._timers = []; this._running = false;
    if (this._vis) document.removeEventListener('visibilitychange', this._vis);
  }
  onFail(e) {
    this.setStatus(false, 'brak połączenia');
    this.showErr(`Brak połączenia z aplikacją Sigen (${this.api}) – ${e && e.message || e}`);
  }
  static getStubConfig() { return {}; }
}

const sgKw = v => (v == null ? '–' : Math.abs(v) < 1 ? (v * 1000).toFixed(0) + ' W' : v.toFixed(2) + ' kW');
const sgHm = t => new Date(t * 1000).toLocaleTimeString('pl-PL', { hour: '2-digit', minute: '2-digit' });
const sgHms = t => new Date(t * 1000).toLocaleTimeString('pl-PL');
const sgDm = t => new Date(t * 1000).toLocaleDateString('pl-PL', { day: '2-digit', month: '2-digit' });
const sgDur = s => { s = Math.max(0, Math.round(s)); return s >= 3600 ? Math.floor(s / 3600) + ' h ' + Math.floor(s % 3600 / 60) + ' min'
  : s >= 60 ? Math.floor(s / 60) + ' min' : s + ' s'; };
const sgCap = t => t ? t.charAt(0).toUpperCase() + t.slice(1) : t;


// tryb "teraz" z /api/inverter/ctl (+ plan /api/strategy) - wspolne dla K1 i K4 (jak renderStNow w aplikacji)
function sgNowMode(c, strat) {
  const ds = c.desired, MN = c.mode_names || {}, sim = c.tryb !== 'wl' && !c.manual, now = strat && strat.now;
  let mode, cls = '';
  if (ds) {
    const PL = { auto: 'Autokonsumpcja', grid: 'Ładowanie z sieci', export: 'Sprzedaż z magazynu', pvchg: 'PV do magazynu, dom z sieci', nodis: 'Samozużycie bez rozładowania' };
    mode = ds.ems === 2 && ds.discharge_kw === 0 ? 'Samozużycie bez rozładowania'
      : ds.ems === 1 ? 'Standby – dom z sieci' : ds.plan === 'manual' ? sgCap(ds.label || MN[ds.ems] || '–') : PL[ds.plan] || sgCap(MN[ds.ems] || ds.label || '–');
    cls = { 5: 'm-sell', 6: 'm-sell', 3: 'm-buy', 1: 'm-stby', 2: 'm-auto', 4: 'm-auto' }[ds.ems] || '';
  } else if (sim && now) {
    mode = sgCap(now.label); cls = { export: 'm-sell', grid: 'm-buy', standby: 'm-stby', auto: 'm-auto', pvchg: 'm-auto', nodis: 'm-auto' }[now.mode] || '';
  } else mode = 'Falownik (mySigen)';
  const x = strat && strat.next && strat.next.ts > Date.now() / 1000 ? strat.next : null;
  return { mode, cls,
    src: c.manual ? 'ręcznie' : c.tryb === 'wl' ? (c.src || '') : c.tryb === 'probny' ? 'próbnie – bez zapisu' : 'symulacja – sterowanie wyłączone',
    why: ds ? (ds.why || '') + (c.applied ? '' : ' · wysyłam…') : (c.reason || ''),
    next: c.manual ? (c.manual.do ? 'ręczne do ' + sgHm(c.manual.do) : 'ręczne do anulowania')
      : x ? `Następnie ${x.day !== 'dziś' ? x.day + ' ' : ''}${x.t}: ${x.label}` : (strat ? 'bez zmian do końca planu' : '') };
}

// ---------------------------------------------------------------------------------------------------------------
// K1: Teraz – przepływy energii, magazyn, strategia teraz (odpowiednik pierwszej karty zakładki PV)
class SigenLiveCard extends SigenBase {
  static defTitle = 'Sigen – teraz';
  static css = `
    .grid { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr); gap: 8px 22px; align-items: start; }
    @container (max-width: 560px) { .grid { grid-template-columns: 1fr; } }
    svg.flow { width: 100%; max-width: 440px; height: auto; display: block; margin: 0 auto; }
    .flow .lbl { fill: var(--sg-text2); font-size: 12px; text-anchor: middle; }
    .flow .val { fill: var(--sg-text); font-size: 15px; font-weight: 600; text-anchor: middle; }
    .flow .sub { fill: var(--sg-muted); font-size: 11px; text-anchor: middle; }
    .icon { fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
    .icon-bg { fill: var(--sg-surface); }
    .line { fill: none; stroke-width: 3; stroke-linecap: round; opacity: .25; }
    .line.on { opacity: 1; stroke-dasharray: 6 8; animation: sgflow 1s linear infinite; }
    .line.on.rev { animation-direction: reverse; }
    @keyframes sgflow { to { stroke-dashoffset: -14; } }
    @media (prefers-reduced-motion: reduce) { .line.on { animation: none; } }
    .socrow { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
    .soclbl { font-size: 13px; color: var(--sg-text2); }
    .soc { font-size: 40px; font-weight: 700; line-height: 1; }
    .bar { height: 10px; background: var(--sg-line); border-radius: 5px; overflow: hidden; margin: 8px 0 14px; }
    .bar > div { height: 100%; background: var(--sg-bat); border-radius: 5px; transition: width .5s; width: 0; }
    .kv { display: grid; grid-template-columns: 1fr auto; gap: 6px 12px; font-size: 14px; }
    .kv span:nth-child(odd) { color: var(--sg-text2); display: flex; align-items: center; gap: 8px; }
    .kv span:nth-child(even) { font-weight: 600; text-align: right; }
    .sw { width: 10px; height: 10px; border-radius: 3px; display: inline-block; flex: none; }
    .now { margin-top: 14px; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--sg-border); }
    .now-h { font-size: 12px; color: var(--sg-text2); display: flex; justify-content: space-between; gap: 8px; }
    .now-big { font-size: 24px; font-weight: 700; line-height: 1.15; margin: 4px 0 3px; }
    .now-sub { font-size: 12px; color: var(--sg-text2); }
    .now-next { font-size: 13px; margin-top: 6px; }
    .now.m-sell .now-big { color: var(--sg-sell); } .now.m-buy .now-big { color: var(--sg-buy); }
    .now.m-stby .now-big { color: var(--sg-muted); } .now.m-auto .now-big { color: var(--sg-bat); }
    .og { margin: 0 0 10px; padding: 8px 12px; border-radius: 10px; background: color-mix(in srgb, var(--sg-err) 14%, transparent);
          border: 1px solid var(--sg-err); font-size: 13px; }
    .og b { color: var(--sg-err); }
  `;
  body() {
    const c = this._cfg, A = this.api;
    const flow = c.flow !== false, details = c.details !== false, strat = c.strategy !== false;
    return `<div class="og" id="og" hidden><b id="og-t"></b><div id="og-s"></div></div>
    <div class="grid">
    ${flow ? `<svg class="flow" viewBox="0 0 400 312" role="img" aria-label="Przepływ energii">
      <path id="l-pv" class="line" d="M200 64 L200 116" stroke="var(--sg-pv)"/>
      <path id="l-grid" class="line" d="M60 150 L172 150" stroke="var(--sg-grid)"/>
      <path id="l-load" class="line" d="M334 150 L228 150" stroke="var(--sg-load)"/>
      <path id="l-bat" class="line" d="M200 230 L200 186" stroke="var(--sg-bat)"/>
      <image id="invimg" href="${A}/static/inverter.png" x="171" y="117" width="58" height="68" preserveAspectRatio="xMidYMid meet"><title>Falownik</title></image>
      <g class="icon" transform="translate(200 36)" stroke="var(--sg-pv)">
        <polygon points="-28,12 28,12 19,-14 -19,-14" class="icon-bg"/>
        <line x1="-9.5" y1="-14" x2="-14" y2="12"/><line x1="0" y1="-14" x2="0" y2="12"/>
        <line x1="9.5" y1="-14" x2="14" y2="12"/><line x1="-23.5" y1="-1" x2="23.5" y2="-1"/>
        <line x1="-16" y1="12" x2="-16" y2="24"/><line x1="16" y1="12" x2="16" y2="24"/></g>
      <text class="lbl" x="268" y="27">PV</text><text class="val" x="268" y="53" id="v-pv">–</text>
      <g class="icon" transform="translate(38 150)" stroke="var(--sg-grid)">
        <line x1="-13" y1="26" x2="-3" y2="-26"/><line x1="13" y1="26" x2="3" y2="-26"/><line x1="-3" y1="-26" x2="3" y2="-26"/>
        <line x1="-20" y1="-15" x2="20" y2="-15"/><line x1="-15" y1="-3" x2="15" y2="-3"/>
        <line x1="-20" y1="-15" x2="-20" y2="-10"/><line x1="20" y1="-15" x2="20" y2="-10"/>
        <line x1="-15" y1="-3" x2="-15" y2="2"/><line x1="15" y1="-3" x2="15" y2="2"/>
        <line x1="-11" y1="16" x2="9" y2="5"/><line x1="11" y1="16" x2="-9" y2="5"/>
        <line x1="-8.5" y1="3" x2="6" y2="-9"/><line x1="8.5" y1="3" x2="-6" y2="-9"/></g>
      <text class="lbl" x="38" y="190">Sieć</text><text class="val" x="38" y="217" id="v-grid">–</text>
      <text class="sub" x="38" y="234" id="s-grid"></text>
      <g class="icon" transform="translate(362 150)" stroke="var(--sg-load)">
        <path d="M-27,-2 L0,-25 L27,-2"/><path d="M-20,-7 V24 H20 V-7" class="icon-bg"/>
        <rect x="-5" y="9" width="10" height="15"/><rect x="8" y="1" width="7" height="7"/>
        <line x1="13" y1="-14" x2="13" y2="-22"/><line x1="13" y1="-22" x2="18" y2="-22"/><line x1="18" y1="-22" x2="18" y2="-10"/></g>
      <text class="lbl" x="358" y="190">Dom</text><text class="val" x="358" y="217" id="v-load">–</text>
      <g class="icon" transform="translate(200 260)" stroke="var(--sg-bat)">
        <rect x="-6" y="-30" width="12" height="4" rx="1"/>
        <rect x="-17" y="-26" width="34" height="52" rx="5" class="icon-bg"/>
        <rect id="bat-fill" x="-12" y="21" width="24" height="0" rx="2" fill="var(--sg-bat)" stroke="none" opacity=".75"/>
        <path d="M2,-16 L-5,-1 H1 L-3,12 L6,-4 H0 Z" stroke-width="1.5" fill="var(--sg-surface)"/></g>
      <text class="sub" x="200" y="302" id="v-soc-c">–</text>
      <text class="lbl" x="268" y="248">Magazyn</text><text class="val" x="268" y="275" id="v-bat">–</text>
      <text class="sub" x="268" y="292" id="s-bat"></text>
    </svg>` : ''}
    <div>
      ${details ? `<div class="socrow"><span class="soclbl">Magazyn energii</span><span class="soc" id="soc">–</span></div>
      <div class="bar"><div id="socbar"></div></div>
      <div class="kv">
        <span><i class="sw" style="background:var(--sg-pv)"></i>Produkcja PV</span><span id="k-pv">–</span>
        <span><i class="sw" style="background:var(--sg-load)"></i>Zużycie domu</span><span id="k-load">–</span>
        <span><i class="sw" style="background:var(--sg-bat)"></i>Magazyn</span><span id="k-bat">–</span>
        <span><i class="sw" style="background:var(--sg-grid)"></i>Sieć</span><span id="k-grid">–</span>
        <span>Moc AC falownika</span><span id="k-plant">–</span>
        <span><i class="sw" style="background:var(--sg-loss)"></i>Straty falownika</span><span id="k-loss">–</span>
        <span>Ostatni zanik sieci</span><span id="k-outage">–</span>
      </div>` : ''}
      ${strat ? `<div class="now" id="now">
        <div class="now-h"><span>Strategia – teraz</span><span id="now-src"></span></div>
        <div class="now-big" id="now-mode">–</div>
        <div class="now-sub" id="now-why"></div>
        <div class="now-next" id="now-next"></div>
      </div>` : ''}
    </div></div>`;
  }
  afterBuild() {   // wersja publiczna: rysunek falownika w SVG
    const im = this.$('invimg'); if (im) im.addEventListener('error', () => im.setAttribute('href', this.api + '/static/inverter.svg'), { once: true });
  }
  polls() {
    const p = [[this.loadLive, 10000]];
    if (this._cfg.strategy !== false) p.push([this.loadCtl, 30000], [this.loadStrat, 300000]);
    return p;
  }
  set(id, txt) { const e = this.$(id); if (e) e.textContent = txt; }
  line(id, v, inward) { const e = this.$(id); if (!e) return; const on = Math.abs(v) >= 0.01; e.classList.toggle('on', on); e.classList.toggle('rev', on && !inward); }
  async loadLive() {
    const d = await this.get('/api/live');
    this.showErr(d.ok ? '' : 'Falownik: ' + (d.error || 'błąd odczytu'));
    const old = d.ts && Date.now() / 1000 - d.ts > 120;
    this.setStatus(d.ok, d.ts ? 'na żywo · ' + sgHms(d.ts) : (d.ok ? 'na żywo' : 'błąd'), old);
    if (d.pv === undefined) return;
    const og = !!d.offgrid && d.ok;
    this.set('v-pv', sgKw(d.pv)); this.set('v-load', sgKw(d.load));
    this.set('v-grid', og ? 'brak' : sgKw(Math.abs(d.grid)));
    this.set('s-grid', og ? 'zanik sieci' : d.grid > 0.01 ? 'pobór' : d.grid < -0.01 ? 'oddawanie' : '');
    this.set('v-bat', sgKw(Math.abs(d.bat)));
    this.set('s-bat', d.bat > 0.01 ? 'ładowanie' : d.bat < -0.01 ? 'rozładowanie' : 'bezczynny');
    this.set('v-soc-c', d.soc.toFixed(0) + '%');
    const bf = this.$('bat-fill');
    if (bf) { const fh = Math.max(0, Math.min(100, d.soc)) / 100 * 42; bf.setAttribute('height', fh.toFixed(1)); bf.setAttribute('y', (21 - fh).toFixed(1)); }
    this.line('l-pv', d.pv, true); this.line('l-load', d.load, false); this.line('l-bat', d.bat, d.bat < 0); this.line('l-grid', d.grid, d.grid > 0);
    this.set('soc', d.soc.toFixed(1) + ' %');
    const sb = this.$('socbar'); if (sb) sb.style.width = Math.min(100, d.soc) + '%';
    this.set('k-pv', sgKw(d.pv)); this.set('k-load', sgKw(d.load));
    this.set('k-bat', (d.bat > 0 ? '+' : '') + sgKw(d.bat));
    this.set('k-grid', og ? 'brak sieci' : (d.grid > 0 ? 'pobór ' : 'oddawanie ') + sgKw(Math.abs(d.grid)));
    this.set('k-plant', sgKw(d.plant));
    this.set('k-loss', d.loss == null ? '–' : sgKw(d.loss) + (d.loss_mode === 'czuwanie' ? ' (czuwanie)' : ''));
    const lo = d.last_outage;
    this.set('k-outage', og ? 'trwa' : lo ? `${sgDm(lo.start)} ${sgHm(lo.start)}–${sgHm(lo.end)} (${lo.approx ? '~' : ''}${sgDur(lo.end - lo.start)})` : 'brak zapisanych');
    const ob = this.$('og'); ob.hidden = !og;
    if (og) {
      this.set('og-t', d.grid_mode === 2 ? 'Praca wyspowa (off-grid) – włączona ręcznie' : 'Brak sieci – praca wyspowa (off-grid)');
      this.set('og-s', (d.offgrid_since ? 'od ' + sgHm(d.offgrid_since) + ' (' + sgDur(Date.now() / 1000 - d.offgrid_since) + ') · ' : '') +
        'dom zasilany z magazynu' + (d.pv > 0.05 ? ' i PV' : '') + ' · SOC ' + d.soc.toFixed(0) + ' %');
    }
  }
  async loadStrat() {
    const s = await this.get('/api/strategy');
    this._strat = s && s.ok !== false ? s : null;
    if (this._ctl) this.renderNow(this._ctl);
  }
  async loadCtl() { this._ctl = await this.get('/api/inverter/ctl'); this.renderNow(this._ctl); }
  renderNow(c) {
    const box = this.$('now'); if (!box) return;
    const r = sgNowMode(c, this._strat);
    box.className = 'now ' + r.cls;
    this.set('now-mode', r.mode); this.set('now-src', r.src); this.set('now-why', r.why); this.set('now-next', r.next);
  }
  getCardSize() { return 7; }
  getGridOptions() { return { columns: 'full', min_columns: 6, rows: 'auto' }; }
}


// ---------------------------------------------------------------------------------------------------------------
// Chart.js – z aplikacji Sigen (/ha/chart.umd.min.js), raz na stronę
function sgChartJs(api) {
  if (window.Chart) return Promise.resolve(window.Chart);
  if (!window.__sgChartP) window.__sgChartP = new Promise((ok, bad) => {
    const s = document.createElement('script');
    s.src = api + '/ha/chart.umd.min.js'; s.onload = () => window.Chart ? ok(window.Chart) : bad(new Error('Chart.js'));
    s.onerror = () => { window.__sgChartP = null; bad(new Error('nie można wczytać Chart.js')); };
    document.head.appendChild(s);
  });
  return window.__sgChartP;
}
// kafle jak w aplikacji: wartość + opis w dymku (najechanie / dotknięcie)
const sgTile = (cls, t, v, unit, note) => `<div class="tile ${cls || ''}"><div class="t">${t}</div><div class="v">${v ?? '–'} <small>${v == null ? '' : unit || ''}</small></div>` +
  (note ? `<div class="n">${note}</div>` : '') + '</div>';
const SIGEN_TILE_CSS = `
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin: 4px 0 12px; }
  .tile { position: relative; border: 1px solid var(--sg-border); border-radius: 10px; padding: 8px 10px; cursor: help; min-width: 0; }
  .tile .t { font-size: 12px; color: var(--sg-text2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tile:has(.n) .t::after { content: ' ⓘ'; color: var(--sg-muted); }
  .tile .v { font-size: 19px; font-weight: 650; line-height: 1.2; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tile .v.vt { font-size: 15px; white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
  .tile .v small { font-size: 13px; font-weight: 500; color: var(--sg-text2); }
  .tile .n { display: none; position: absolute; z-index: 30; top: calc(100% + 6px); left: 0; width: max(240px, 100%); max-width: 340px;
    padding: 8px 10px; border: 1px solid var(--sg-border); border-radius: 8px; background: var(--sg-surface);
    box-shadow: 0 8px 22px rgba(0,0,0,.25); font-size: 12px; line-height: 1.45; color: var(--sg-text2); white-space: normal; }
  .tile.pr .n { left: auto; right: 0; }
  .tile:hover .n, .tile.open .n { display: block; }
  .tile.c-red { border-color: color-mix(in srgb, var(--sg-buy) 55%, var(--sg-border)); background: color-mix(in srgb, var(--sg-buy) 7%, transparent); }
  .tile.c-red .v { color: var(--sg-buy); }
  .tile.c-green { border-color: color-mix(in srgb, var(--sg-sell) 55%, var(--sg-border)); background: color-mix(in srgb, var(--sg-sell) 7%, transparent); }
  .tile.c-green .v { color: var(--sg-sell); }
  .tile.c-blue, .tile.nw { border-color: color-mix(in srgb, var(--sg-load) 55%, var(--sg-border)); background: color-mix(in srgb, var(--sg-load) 7%, transparent); }
  .tile.c-blue .v, .tile.nw .v { color: var(--sg-load); }
  .tile.c-amber { border-color: color-mix(in srgb, var(--sg-grid) 60%, var(--sg-border)); background: color-mix(in srgb, var(--sg-grid) 9%, transparent); }
  .tile.c-amber .v { color: var(--sg-grid); }
  .tile.c-pv { border-color: color-mix(in srgb, var(--sg-pv) 60%, var(--sg-border)); background: color-mix(in srgb, var(--sg-pv) 9%, transparent); }
  .tile.c-pv .v { color: var(--sg-pv); }
  @container (max-width: 520px) { .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
`;
function sgPopTiles(box) {
  const W = box.clientWidth;
  box.querySelectorAll('.tile').forEach(t => {
    t.classList.toggle('pr', t.offsetLeft + t.offsetWidth / 2 > W / 2);
    const v = t.querySelector('.v'); if (v) v.classList.toggle('vt', v.textContent.trim().length > 10 && !/^[+\-−]?[\d.,]+\s/.test(v.textContent.trim()));
    t.onclick = () => { const o = t.classList.contains('open'); box.querySelectorAll('.tile.open').forEach(x => x.classList.remove('open')); if (!o) t.classList.add('open'); };
  });
}
const sgLs = { get: k => { try { return localStorage.getItem(k); } catch (e) { return null; } },
               set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* brak pamieci przegladarki */ } } };

// ---------------------------------------------------------------------------------------------------------------
// K4: Strategia osobista – kafle, sterowanie (odczyt), wykres planu, plan godzinowy (odpowiednik karty w aplikacji)
// opcje: tiles / control / chart / table: false – ukrycie części karty
class SigenStrategyCard extends SigenBase {
  static defTitle = 'Strategia osobista';
  static css = SIGEN_TILE_CSS + `
    .ctl { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; padding: 8px 12px; border-radius: 10px;
           border: 1px solid var(--sg-border); margin: 0 0 12px; font-size: 13px; color: var(--sg-text2); }
    .ctl b { font-size: 17px; color: var(--sg-text); }
    .ctl.m-sell b { color: var(--sg-sell); } .ctl.m-buy b { color: var(--sg-buy); } .ctl.m-stby b { color: var(--sg-muted); } .ctl.m-auto b { color: var(--sg-bat); }
    .ctl .why { flex-basis: 100%; font-size: 12px; }
    .ctl .warn { color: var(--sg-err); }
    .chart { position: relative; height: 320px; margin: 4px 0 8px; }
    @container (max-width: 520px) { .chart { height: 340px; } }
    .stinfo { font-size: 12px; color: var(--sg-text2); margin: -4px 0 8px; }
    details summary { cursor: pointer; font-size: 14px; font-weight: 500; margin: 6px 0; }
    .opts { display: flex; flex-wrap: wrap; gap: 6px 16px; margin: 4px 0 6px; font-size: 13px; color: var(--sg-text2); }
    .opts label { display: flex; align-items: center; gap: 5px; cursor: pointer; }
    .tw { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th { font-weight: 500; color: var(--sg-text2); text-align: right; padding: 5px 6px; border-bottom: 1px solid var(--sg-border); white-space: nowrap; }
    td { text-align: right; padding: 4px 6px; border-bottom: 1px solid var(--sg-border); white-space: nowrap; }
    th:first-child, td:first-child, th:nth-child(6), td:nth-child(6) { text-align: left; }
    .stm { font-size: 11px; padding: 1px 6px; border-radius: 6px; border: 1px solid; white-space: nowrap; }
    .stm.past { color: var(--sg-muted); } .stm.auto { color: var(--sg-bat); } .stm.standby { color: var(--sg-grid); } .stm.grid { color: var(--sg-buy); }
    .stm.export { color: var(--sg-sell); font-weight: 600; } .stm.pvchg { color: var(--sg-pv); font-weight: 600; } .stm.nodis { color: var(--sg-bat); font-weight: 600; }
    td:nth-child(6) { white-space: normal; } td .stm { white-space: normal; display: inline-block; line-height: 1.3; }
    tr.cur td { background: color-mix(in srgb, var(--sg-load) 8%, transparent); }
    .notes { font-size: 12px; color: var(--sg-text2); margin-top: 4px; }
    details.set { margin: 0 0 12px; border: 1px solid var(--sg-border); border-radius: 10px; padding: 2px 12px 8px; }
    details.set > summary { margin: 6px 0; }
    .grp { margin: 8px 0 4px; }
    .grp-h { font-size: 13px; font-weight: 600; margin: 10px 0 6px; color: var(--sg-text); }
    .frm { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; font-size: 13px; color: var(--sg-text2); }
    .frm > span, .frm > label { display: inline-flex; align-items: center; gap: 5px; flex-wrap: wrap; }
    .frm input[type=number], .frm input[type=password], .frm select { font: inherit; font-size: 13px; color: var(--sg-text);
      background: var(--sg-surface); border: 1px solid var(--sg-border); border-radius: 6px; padding: 4px 6px; }
    .frm input[type=number] { width: 72px; }
    .frm input:disabled, .frm select:disabled { opacity: .5; }
    .btn { font: inherit; font-size: 13px; padding: 5px 12px; border-radius: 8px; border: 1px solid var(--sg-border);
      background: transparent; color: var(--sg-text); cursor: pointer; }
    .btn.pri { background: var(--primary-color, #03a9f4); border-color: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
    .btn.warn { background: var(--sg-err); border-color: var(--sg-err); color: #fff; }
    .btn:disabled { opacity: .5; cursor: default; }
    .act { display: flex; justify-content: flex-end; align-items: center; gap: 10px; margin-top: 8px; flex-wrap: wrap; }
    .msg { font-size: 12px; color: var(--sg-text2); }
    .msg.bad { color: var(--sg-err); } .msg.good { color: var(--sg-sell); }
    .lock { font-size: 13px; padding: 8px 10px; border-radius: 8px; background: color-mix(in srgb, var(--sg-grid) 12%, transparent); margin: 6px 0; }
    .ro .frm input, .ro .frm select, .ro .act .btn { pointer-events: none; opacity: .5; }
    @container (max-width: 600px) {
      tr > :nth-child(2), tr > :nth-child(4), tr > :nth-child(5), tr > :nth-child(7), tr > :nth-child(9), tr > :nth-child(10) { display: none; }
      td .stm { white-space: normal; display: inline-block; line-height: 1.3; }
      td, th { padding-left: 4px; padding-right: 4px; }
    }
  `;
  body() {
    const c = this._cfg;
    return `<div class="stinfo" id="info"></div>
      ${c.tiles !== false ? '<div class="tiles" id="tiles"></div>' : ''}
      ${c.control !== false ? '<div class="ctl" id="ctl"><span>Sterowanie falownikiem…</span></div>' : ''}
      ${c.settings !== false ? this.setBody() : ''}
      ${c.chart !== false ? '<div class="chart"><canvas id="cv"></canvas></div>' : ''}
      ${c.table !== false ? `<details open id="det"><summary>Plan godzinowy</summary>
        <div class="opts"><label title="Pokaż też godziny, które już minęły"><input type="checkbox" id="arch"> Archiwalne</label>
          <label title="Pokaż też godziny po jutrze 12:00"><input type="checkbox" id="to12"> Po 12:00 jutro</label></div>
        <div class="tw"><table><thead><tr><th>Godz.</th><th>Cena zakupu</th><th>Cena sprzedaży</th><th>Dom kWh</th><th>PV kWh</th><th>Plan</th>
          <th>Z sieci kWh</th><th>SOC plan</th><th>SOC bez strategii</th><th>Koszt plan / bez</th></tr></thead><tbody id="tb"></tbody></table></div>
        <div class="notes" id="notes"></div></details>` : ''}`;
  }
  setBody() {
    const n = (id, min, max, step, w) => `<input type="number" id="${id}" min="${min}" max="${max}" step="${step}"${w ? ` style="width:${w}px"` : ''}>`;
    return `<details class="set" id="set"><summary>Ustawienia strategii i sterowanie</summary>
      <div id="lock" class="lock" hidden></div>
      <div id="setbox">
      <div class="grp"><div class="grp-h">Sterowanie falownikiem</div><div class="frm">
        <span>Tryb <select id="s-ctl"><option value="wyl">wyłączone</option><option value="probny">próbnie (tylko dziennik)</option>
          <option value="wl">włączone – wysyłaj plan do falownika</option></select></span>
        <span id="s-pzbox">Pauza <select id="s-pz"><option value="">wybierz…</option><option value="godzina">do końca godziny</option><option value="1h">na 1 godzinę</option>
          <option value="3h">na 3 godziny</option><option value="polnoc">do północy</option><option value="jutro">do jutra 6:00</option></select>
          <button type="button" class="btn" id="s-pzend" hidden>Wznów sterowanie</button></span>
        <span class="msg" id="s-ctlmsg"></span></div></div>
      <div class="grp"><div class="grp-h">Ręcznie <span class="msg" id="m-st"></span></div><div class="frm">
        <span>Tryb <select id="m-tryb"><option value="standby">Standby</option><option value="auto">Samozużycie</option><option value="grid">Ładowanie z sieci</option>
          <option value="pvchg">PV do magazynu (dom z sieci)</option><option value="auto_nodis">Samozużycie bez rozładowania</option><option value="discharge">Rozładowanie ze stałą mocą</option></select></span>
        <span>na <select id="m-czas"><option value="15m">15 min</option><option value="30m">30 min</option><option value="1h" selected>1 h</option><option value="2h">2 h</option>
          <option value="min">własny…</option><option value="cancel">do anulowania</option></select> <span id="m-minw" hidden>${n('m-min', 1, 1440, 5, 64)} min</span></span>
        <span id="m-pw">moc ${n('m-kw', 0.5, 30, 0.5, 60)} kW</span>
        <span id="m-smax">do SOC ${n('m-socmax', 10, 100, 5, 60)} %</span>
        <span id="m-smin">min. SOC ${n('m-socmin', 5, 100, 5, 60)} %</span>
        <span id="m-prz" title="ładowanie + dom (albo oddanie do sieci) nie przekroczy tej mocy">przyłącze ${n('m-przyl', 1, 100, 1, 56)} kW</span>
      </div><div class="act"><span class="msg" id="m-msg"></span><button type="button" class="btn" id="m-stop" hidden>Anuluj ręczne</button>
        <button type="button" class="btn pri" id="m-go">Włącz ręczne</button></div></div>
      <div class="grp"><div class="grp-h">Pułapy i limity</div><div class="frm">
        <span title="progi ręczne: sprzedaż z magazynu od „Sprzedawaj od”, ładowanie z sieci do „Ładuj do”; auto: plan sam decyduje przy zysku po kosztach ≥ min. zysk">Tryb cen
          <select id="l-pm"><option value="progi">progi ręczne</option><option value="auto">auto – zysk po kosztach</option></select></span>
        <span>min. zysk ${n('l-mg', 0, 3, 0.05, 64)} zł/kWh</span>
        <span>Profil zużycia <select id="l-prof"><option value="prosty">prosty (14 dni)</option><option value="dni">dni tygodnia</option><option value="dni_temp">dni tygodnia + temperatura</option></select></span>
        <span>Sprzedawaj z magazynu od ${n('l-floor', 0, 20, 0.05)} zł/kWh</span>
        <span title="przy włączonej Tarczy próg nie obowiązuje">Ładuj z sieci do ${n('l-cap', 0, 20, 0.05)} zł/kWh</span>
        <span>Tarcza Pstryk: limit ${n('l-tz', 0, 5, 0.01, 64)} zł/kWh <select id="l-tzm"><option value="auto">auto (wg średniej w miesiącu)</option><option value="tak">zawsze</option><option value="nie">wyłączona</option></select></span>
        <span>Rezerwa pod sprzedaż (Tarcza) do ${n('l-rez', 0, 100, 5, 60)} %</span>
        <span>Sprzedaż tylko energii z PV – z ostatnich ${n('l-legal', 1, 366, 1, 56)} dni</span>
        <label><input type="checkbox" id="l-illegal"> Niezgodny z prawem arbitraż</label>
        <label><input type="checkbox" id="l-nofc"> Limit z PV bez prognozy</label>
        <span>Moc maks. ładowania z sieci ${n('l-pc', 0.5, 30, 0.5, 60)} kW</span>
        <span>Moc maks. sprzedaży z magazynu ${n('l-pd', 0.5, 30, 0.5, 60)} kW</span>
        <span title="w godzinach z mniejszym przewidywanym poborem zawsze standby; puste = plan decyduje sam">Standby, gdy dom pobiera &lt; ${n('l-sb', 0, 3000, 50, 70)} W</span>
      </div><div class="act"><span class="msg" id="l-msg"></span><button type="button" class="btn pri" id="l-save">Zapisz pułapy</button></div></div>
      </div></details>`;
  }
  // przycisk z potwierdzeniem drugim kliknięciem (bez okien confirm – działają też w aplikacji HA na telefonie)
  confirm2(btn, txt, fn) {
    if (btn.dataset.arm === '1') { btn.dataset.arm = ''; btn.textContent = btn.dataset.orig; btn.classList.remove('warn'); clearTimeout(btn._t); fn(); return; }
    btn.dataset.orig = btn.textContent; btn.dataset.arm = '1'; btn.textContent = txt; btn.classList.add('warn');
    btn._t = setTimeout(() => { btn.dataset.arm = ''; btn.textContent = btn.dataset.orig; btn.classList.remove('warn'); }, 6000);
  }
  bindSettings() {
    const set = this.$('set'); if (!set) return;
    const $ = id => this.$(id);
    set.addEventListener('toggle', () => { if (set.open) this.refreshAuth(); });
    // formularz pułapów: wypełniany z planu, dopóki nic nie zmieniono
    this._dirtyL = false; this._dirtyM = false;
    $('setbox').querySelectorAll('[id^="l-"]').forEach(e => e.addEventListener('input', () => { this._dirtyL = true; }));
    $('setbox').querySelectorAll('[id^="m-"]').forEach(e => e.addEventListener('input', () => { this._dirtyM = true; }));
    $('l-pm').addEventListener('change', () => { $('l-mg').disabled = $('l-pm').value !== 'auto'; });
    const mnShow = () => {
      const t = $('m-tryb').value;
      $('m-minw').hidden = $('m-czas').value !== 'min';
      $('m-pw').hidden = !(t === 'grid' || t === 'discharge' || t === 'pvchg');
      $('m-prz').hidden = !(t === 'grid' || t === 'discharge');
      $('m-smax').hidden = !(t === 'grid' || t === 'pvchg');
      $('m-smin').hidden = t !== 'discharge';
    };
    this._mnShow = mnShow;
    $('m-tryb').onchange = () => { if ($('m-tryb').value === 'pvchg') $('m-kw').value = ''; else if (!$('m-kw').value) $('m-kw').value = 3; mnShow(); };
    $('m-czas').onchange = mnShow;
    $('m-kw').placeholder = 'maks.';
    [['m-kw', 3], ['m-socmax', 100], ['m-socmin', 20], ['m-przyl', 22]].forEach(([id, v]) => { if (!$(id).value) $(id).value = v; });
    mnShow();
    const ctlPost = async (body, msgId) => {
      const r = await this.post('/api/inverter/ctl', body), m = $(msgId);
      if (!r.ok) { m.className = 'msg bad'; m.textContent = r.error || 'błąd'; if (r.need_pin) this.refreshAuth(); }
      else { m.className = 'msg good'; m.textContent = 'zapisano'; setTimeout(() => { if (m.textContent === 'zapisano') m.textContent = ''; }, 4000); }
      setTimeout(() => this.loadCtl().catch(e => this.onFail(e)), 1200);
      return r;
    };
    $('s-ctl').onchange = () => {
      const t = $('s-ctl').value;
      if (t === 'wl' && this._ctl && this._ctl.tryb !== 'wl') {
        $('s-ctlmsg').className = 'msg bad';
        $('s-ctlmsg').innerHTML = 'Falownik dostanie tryb Remote EMS bieżącej godziny planu. ';
        const b = document.createElement('button'); b.type = 'button'; b.className = 'btn warn'; b.textContent = 'Potwierdź włączenie';
        b.onclick = () => { $('s-ctlmsg').textContent = ''; ctlPost({ tryb: 'wl' }, 's-ctlmsg'); };
        const x = document.createElement('button'); x.type = 'button'; x.className = 'btn'; x.textContent = 'Anuluj';
        x.onclick = () => { $('s-ctlmsg').textContent = ''; $('s-ctl').value = this._ctl.tryb; };
        $('s-ctlmsg').append(b, ' ', x);
        return;
      }
      ctlPost({ tryb: t }, 's-ctlmsg');
    };
    $('s-pz').onchange = () => { const v = $('s-pz').value; $('s-pz').value = ''; if (v) ctlPost({ pauza: v }, 's-ctlmsg'); };
    $('s-pzend').onclick = () => ctlPost({ pauza: 'koniec' }, 's-ctlmsg');
    $('m-go').onclick = () => {
      const t = $('m-tryb').value, c = $('m-czas').value;
      this.confirm2($('m-go'), 'Na pewno? Kliknij ponownie', async () => {
        const r = await ctlPost({ reczne: { tryb: t, czas: c === 'min' ? null : c, minuty: c === 'min' ? $('m-min').value : null, kw: $('m-kw').value,
          soc_min: $('m-socmin').value, soc_max: $('m-socmax').value, przylacze_kw: $('m-przyl').value } }, 'm-msg');
        if (r.ok) this._dirtyM = false;
      });
    };
    $('m-stop').onclick = () => ctlPost({ reczne: { tryb: 'koniec' } }, 'm-msg');
    $('l-save').onclick = async () => {
      const r = await this.post('/api/strategy/limits', { price_mode: $('l-pm').value, auto_margin: $('l-mg').value, load_profile: $('l-prof').value,
        sell_floor: $('l-floor').value, buy_cap: $('l-cap').value, tarcza: $('l-tz').value, tarcza_mode: $('l-tzm').value, tarcza_rezerwa: $('l-rez').value,
        legal_days: $('l-legal').value, illegal_arb: $('l-illegal').checked, legal_no_fc: $('l-nofc').checked, p_charge: $('l-pc').value,
        p_discharge: $('l-pd').value, standby_below_w: $('l-sb').value });
      const m = $('l-msg');
      m.className = 'msg ' + (r.ok ? 'good' : 'bad'); m.textContent = r.ok ? 'zapisano – przeliczam plan…' : (r.error || 'błąd');
      if (!r.ok && r.need_pin) this.refreshAuth();
      if (r.ok) { this._dirtyL = false; setTimeout(() => this.loadStrat().catch(e => this.onFail(e)), 1500); }
    };
  }
  async refreshAuth() {
    const box = this.$('lock'), sb = this.$('setbox'); if (!box) return;
    const a = await this.access();
    this._authed = !!(a.authed || a.local);
    sb.classList.toggle('ro', !this._authed);
    box.hidden = this._authed;
    if (this._authed) return;
    if (!a.pin_set) { box.textContent = 'Zmiany z innych urządzeń są wyłączone – ustaw PIN w pliku dostep.ini na serwerze aplikacji.'; return; }
    box.innerHTML = 'Zmiany wymagają PIN-u aplikacji Sigen (jak na stronie aplikacji; urządzenie zapamięta go na rok). ' +
      '<span class="frm" style="display:inline-flex;margin-top:4px"><input type="password" id="pin" inputmode="numeric" autocomplete="off" placeholder="PIN" style="width:110px">' +
      '<button type="button" class="btn pri" id="pinok">Zaloguj</button><span class="msg" id="pinmsg"></span></span>';
    const go = async () => {
      const r = await this.post('/api/login', { pin: this.$('pin').value });
      if (!r.ok) { this.$('pinmsg').className = 'msg bad'; this.$('pinmsg').textContent = r.error || 'błędny PIN'; return; }
      this.refreshAuth();
    };
    this.$('pinok').onclick = go;
    this.$('pin').onkeydown = e => { if (e.key === 'Enter') go(); };
  }
  fillLimits(P) {
    if (!this.$('l-pm') || this._dirtyL || !P) return;
    const $ = id => this.$(id), ae = this.shadowRoot.activeElement && this.shadowRoot.activeElement.id;
    const put = (id, v) => { if (ae !== id) $(id).value = v ?? ''; };
    const TZ = typeof P.tarcza === 'object' && P.tarcza ? P.tarcza : { cap: P.tarcza, mode: P.tarcza_mode };
    put('l-pm', P.price_mode || 'progi'); put('l-mg', P.auto_margin ?? 0.2); put('l-prof', P.load_profile || 'prosty');
    put('l-floor', P.sell_floor); put('l-cap', P.buy_cap); put('l-tz', TZ.cap ?? 0.61); put('l-tzm', TZ.mode || 'auto');
    put('l-rez', P.tarcza_rezerwa); put('l-legal', P.legal_days); put('l-pc', P.p_charge); put('l-pd', P.p_discharge); put('l-sb', P.standby_below_w);
    $('l-illegal').checked = !!P.illegal_arb; $('l-nofc').checked = !!P.legal_no_fc;
    $('l-mg').disabled = $('l-pm').value !== 'auto';
    if (P.p_max_kw) $('l-pc').placeholder = $('l-pd').placeholder = P.p_max_kw;
  }
  fillCtl(c) {
    const $ = id => this.$(id); if (!$('s-ctl')) return;
    if (!(this.shadowRoot.activeElement && this.shadowRoot.activeElement.id === 's-ctl') && !$('s-ctlmsg').querySelector('button')) $('s-ctl').value = c.tryb;
    const paused = c.pause_until && c.pause_until * 1000 > Date.now();
    $('s-pzbox').hidden = c.tryb !== 'wl';
    $('s-pzend').hidden = !paused;
    const M = c.manual;
    $('m-stop').hidden = !M; $('m-go').textContent = M ? 'Zmień ręczne' : 'Włącz ręczne';
    $('m-st').textContent = M ? (M.do ? '· aktywne do ' + sgHm(M.do) : '· aktywne do anulowania') : '';
    $('m-st').className = 'msg' + (M ? ' good' : '');
    if (M && !this._dirtyM) {
      $('m-tryb').value = M.tryb; if (M.kw != null) $('m-kw').value = M.kw; if (M.soc_min != null) $('m-socmin').value = M.soc_min;
      if (M.soc_max != null) $('m-socmax').value = M.soc_max; if (M.przylacze_kw != null) $('m-przyl').value = M.przylacze_kw;
      if (this._mnShow) this._mnShow();
    }
  }
  afterBuild() {
    this.bindSettings();
    [['arch', 'sigen_ha_st_arch'], ['to12', 'sigen_ha_st_after12']].forEach(([id, k]) => {
      const e = this.$(id); if (!e) return;
      e.checked = sgLs.get(k) === '1';
      e.onchange = () => { sgLs.set(k, e.checked ? '1' : '0'); if (this._d) this.renderTable(this._d); };
    });
    if (this._chart) { this._chart.destroy(); this._chart = null; }
    if (this._d) this.render(this._d);
    if (this._ro) this._ro.disconnect();
    this._ro = new ResizeObserver(() => { const t = this.$('tiles'); if (t) sgPopTiles(t); });
    this._ro.observe(this.shadowRoot.querySelector('.sg'));
  }
  _applyPal() { super._applyPal(); if (this._chart) { this._chart.destroy(); this._chart = null; if (this._d) this.renderChart(this._d); } }
  polls() { return [[this.loadStrat, 300000], ...(this._cfg.control !== false || this._cfg.settings !== false ? [[this.loadCtl, 30000]] : [])]; }
  cv(n) { const e = this.shadowRoot.querySelector('.sg'); return e ? getComputedStyle(e).getPropertyValue(n).trim() : ''; }
  async loadStrat() {
    const d = await this.get('/api/strategy');
    if (!d.ok) { this.setStatus(false, 'plan niedostępny'); this.showErr('Symulacja niedostępna: ' + (d.error || '')); this.fillLimits(d.limits); return; }
    this.fillLimits(d.params);
    this.showErr(''); this._d = d;
    this.setStatus(true, 'przeliczono ' + sgHm(d.ts));
    this.render(d);
    if (this._ctl) this.renderCtl(this._ctl);
  }
  async loadCtl() { this._ctl = await this.get('/api/inverter/ctl'); this.renderCtl(this._ctl); }
  renderCtl(c) {
    this.fillCtl(c);
    const box = this.$('ctl'); if (!box) return;
    const r = sgNowMode(c, this._d), T = { wl: 'włączone', probny: 'próbnie (tylko dziennik)', wyl: 'wyłączone – plan to symulacja' };
    box.className = 'ctl ' + r.cls;
    box.innerHTML = `<span>Teraz:</span> <b>${this.esc(r.mode)}</b><span>${this.esc(r.src)}</span>` +
      `<span>· sterowanie: ${this.esc(T[c.tryb] || c.tryb || '–')}</span>` +
      (c.pause_until && c.pause_until * 1000 > Date.now() ? `<span class="warn">· pauza do ${sgHm(c.pause_until)}</span>` : '') +
      (c.error ? `<span class="warn">· błąd: ${this.esc(c.error)}</span>` : '') +
      `<span class="why">${this.esc(r.why)}${r.next ? ' · ' + this.esc(r.next) : ''}</span>`;
  }
  render(d) {
    const P = d.params || {};
    const pe = d.pradcast && d.pradcast.eval || {};
    const peTxt = Object.entries(pe).map(([h, v]) => `${h} ±${v.mae.toFixed(2)} zł${v.mape != null ? ' (' + v.mape + ' %)' : ''}`).join(' · ');
    const inf = this.$('info'); if (inf) inf.textContent = `ceny: ${d.price_source || '–'}` + (peTxt ? ` · trafność Pradcast: ${peTxt}` : '');
    this.renderTiles(d, P); this.renderChart(d); this.renderTable(d);
  }
  renderTiles(d, P) {
    const box = this.$('tiles'); if (!box) return;
    const esc = s => this.esc(s), T = d.totals || {}, n = d.now || {}, x = d.next;
    const f1 = v => v.toFixed(1).replace('.', ','), f2 = v => v == null ? '–' : v.toFixed(2).replace('.', ',');
    const col = v => v >= 0.005 ? 'c-green' : v <= -0.005 ? 'c-red' : 'c-blue';
    const sav = (lbl, t) => !t ? '' : sgTile(col(t.saving_total), lbl, t.saving_total.toFixed(2), 'zł',
      `plan ${t.plan.toFixed(2)} zł · bez strategii ${t.base.toFixed(2)} zł` + (t.grid_charge ? ` · z sieci do magazynu ${t.grid_charge.toFixed(1)} kWh` : '') +
      (t.bat_export ? ` · sprzedaż z magazynu ${t.bat_export.toFixed(1)} kWh` : '') +
      (t.end_diff ? ` · w tym stan magazynu na koniec ${(t.end_diff >= 0 ? '+' : '') + t.end_diff.toFixed(2)} zł` : ''));
    let h = sgTile('', 'Teraz wg planu', esc((n.label || '–').split(' – ')[0]), '',
        ((n.label || '').includes(' – ') ? esc(n.label) + ' · ' : '') + `SOC ${P.soc_now} % · magazyn ${P.cap_kwh} kWh (${P.soc_min}–${P.soc_max} %)`) +
      sgTile('', 'Następna zmiana', x ? `${x.day} ${x.t}` : null, '', x ? 'na: ' + esc(x.label) : 'bez zmian do końca planu') +
      sav('Oszczędność – reszta dnia', T['dziś']) + sav('Oszczędność – jutro', T['jutro']) + sav('Oszczędność – pojutrze', T['pojutrze']) +
      (d.total_saving != null ? sgTile('c-blue', T['pojutrze'] ? 'Razem dziś – pojutrze' : 'Razem dziś + jutro', (d.total_saving >= 0 ? '+' : '') + d.total_saving.toFixed(2), 'zł',
        `zużycie baterii ${(P.wear ?? 0).toFixed(2)} zł/kWh · ` + (P.price_mode === 'auto' ? `tryb auto: sprzedaż z magazynu przy zysku po kosztach ≥ ${(P.auto_margin ?? 0).toFixed(2)} zł/kWh`
          : P.sell_floor != null ? `sprzedaż z magazynu od ${P.sell_floor.toFixed(2)} zł` : 'bez sprzedaży z magazynu (ustaw pułap w aplikacji)') +
        (P.buy_cap != null ? ` · ładowanie z sieci do ${P.buy_cap.toFixed(2)} zł${P.tarcza && P.tarcza.active ? ' (z Tarczą bez progu)' : ''}` : '') +
        ` · moc ładowania z sieci ≤ ${P.p_charge ?? P.p_max_kw} kW, sprzedaży ≤ ${P.p_discharge ?? P.p_max_kw} kW`) : '');
    if (P.horizon) {
      const Z = P.horizon, endTxt = new Date(Z.end * 1000).toLocaleString('pl-PL', { weekday: 'short', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
      h += sgTile('nw', 'Horyzont planu', Z.hours, 'h', `plan liczy ${Z.hours} godzin – do ${endTxt} (${(Z.days || []).join(', ')}); dalej plan nie widzi cen. ` +
        `Energia zostawiona na koniec (${Z.end_kwh ?? '–'} kWh ponad minimum) wyceniana po ${f2(Z.terminal_price)} zł/kWh` +
        (Z.reserve_kwh ? `, do ${Z.reserve_kwh} kWh rezerwy Tarczy po ${f2(Z.reserve_price)} zł/kWh` : '') + '.');
    }
    if (P.profile) {
      const Q = P.profile, lbl = ['dziś', 'jutro', 'pojutrze'];
      h += sgTile('nw', 'Profil zużycia', esc(Q.mode_txt || Q.mode), '', Object.entries(Q.days || {}).map(([k, v], i) =>
        `${lbl[i] || k}: ${esc(v.type_txt)} – ${String(v.kwh).replace('.', ',')} kWh`).join(' · ') + (Q.error ? ' · błąd: ' + esc(Q.error) : ''));
    }
    if (P.legal && P.legal.budget != null) {
      const bat = P.soc_now != null && P.cap_kwh ? Math.max(0, (P.soc_now - (P.soc_min ?? 0)) / 100 * P.cap_kwh * (P.eta_d ?? 0.95)) : null;
      const lim = P.legal.budget, now = bat == null ? lim : Math.min(lim, bat);
      const by = bat == null ? '' : bat < 0.5 ? 'magazyn pusty' : lim < bat ? 'ogranicza limit z PV' : 'ogranicza stan magazynu';
      const cls = bat != null && bat < 0.5 ? '' : lim < 0.5 || (bat != null && lim < bat) ? 'c-amber' : 'c-green';
      h += sgTile(cls, 'Do sprzedaży teraz', f1(now), 'kWh', `${by ? by + ' · ' : ''}limit z PV: ${f1(lim)} kWh (produkcja ${f1(P.legal.pv)} − oddane ${f1(P.legal.exp)} kWh, ostatnie ${P.legal.days} dni)` +
        (bat != null ? ` · w magazynie do oddania: ${f1(bat)} kWh` : '') +
        (P.legal.capped_hours ? ` · sprzedaż przycięta limitem w ${P.legal.capped_hours} godz.${P.legal.cut_kwh ? ` (przekroczony arbitraż o ${f1(P.legal.cut_kwh)} kWh – żółte słupki)` : ''}` : ''));
    } else if (P.illegal_arb) h += sgTile('c-red', 'Limit z PV', 'wyłączony', '', 'zaznaczony „Niezgodny z prawem arbitraż” – sprzedaż bez limitu z produkcji PV');
    const now = Date.now() / 1000, ex = (d.hours || []).filter(q => q.mode === 'export' && (q.e_exp || 0) >= 0.5 && q.ts + 3600 > now);
    if (ex.length) {
      const bl = []; ex.forEach(q => { const l = bl[bl.length - 1]; if (l && q.ts === l.b + 3600 && q.day === l.day) { l.b = q.ts; l.p = Math.max(l.p, q.sell); } else bl.push({ day: q.day, a: q.ts, b: q.ts, p: q.sell }); });
      const hh = ts => new Date(ts * 1000).getHours(), txt = bl.slice(0, 3).map(q => `${q.day} ${hh(q.a)}–${hh(q.b) + 1 || 24}`);
      h += sgTile('c-amber', 'Sprzedaż – bez dużych odbiorników', txt[0], '', `planowana sprzedaż z magazynu: ${txt.join(' · ')} – w tych godzinach nie włączaj suszarki, ` +
        `zmywarki, piekarnika, grzania wody (do ${f2(bl[0].p)} zł/kWh)`);
    }
    if (P.tarcza && P.tarcza.mode !== 'nie' && P.tarcza.reason !== 'brak Pstryka')
      h += sgTile(P.tarcza.active ? 'c-green' : 'c-blue', 'Tarcza Pstryk', P.tarcza.active ? 'działa' : 'nie działa', '', esc(P.tarcza.reason || '') +
        (P.tarcza.active && P.tarcza.eff_buy_zones ? ` · zakup: ${f2(P.tarcza.eff_buy_zones.pozostale)} zł (pozostałe) · ${f2(P.tarcza.eff_buy_zones.przedpoludniowa)} zł (7–13) · ${f2(P.tarcza.eff_buy_zones.popoludniowa)} zł (16–21)`
          : P.tarcza.active && P.tarcza.eff_buy ? ` · zakup po stałej cenie ${f2(P.tarcza.eff_buy)} zł/kWh` : ''));
    box.innerHTML = h;
    sgPopTiles(box);
  }
  async renderChart(d) {
    const cvs = this.$('cv'); if (!cvs) return;
    let Chart;
    try { Chart = await sgChartJs(this.api); } catch (e) { this.showErr('Wykres: ' + e.message); return; }
    const P = d.params || {}, H = d.hours || [], labels = H.map(h => `${h.day} ${h.t}`), nowI = d.now_index || 0;
    const pal = SIGEN_PAL[this._dark ? 'dark' : 'light'], muted = this.cv('--sg-muted') || '#888', text2 = this.cv('--sg-text2') || '#666';
    const gridC = this._dark ? 'rgba(255,255,255,.08)' : 'rgba(0,0,0,.08)';
    const expPv = h => h.grid < 0 && h.pv != null && h.load != null ? Math.min(-h.grid, Math.max(h.pv - h.load, 0)) : h.grid < 0 ? -h.grid : 0;
    const expBat = h => h.grid < 0 ? Math.max(-h.grid - expPv(h), 0) : 0;
    const imp = h => h.grid > 0 ? h.grid : 0;
    const KINDS = [{ k: 'pv', label: 'Sprzedaż PV', color: pal.sell }, { k: 'bat', label: 'Sprzedaż z magazynu', color: '#39d353' },
      { k: 'buy', label: 'Zakup (rzeczywisty)', color: pal.buy }, { k: 'grid', label: 'Zakup celowy (plan)', color: '#9e1b1e' }, { k: 'home', label: 'Pobór domu (plan)', color: '#f19d9c' }];
    const kindOf = (h, i) => expBat(h) > 0.005 ? 'bat' : expPv(h) > 0.005 ? 'pv' : imp(h) > 0.005 ? (i < nowI ? 'buy' : h.mode === 'grid' ? 'grid' : 'home') : null;
    const amt = h => h.grid < 0 ? -h.grid : imp(h), kinds = H.map(kindOf);
    const maxExp = Math.max(0.3, ...H.map(h => h.grid < 0 ? -h.grid : 0)), maxImp = Math.max(0.3, ...H.map(imp));
    const rgba = (hex, a) => { const n = parseInt(hex.replace('#', ''), 16); return `rgba(${n >> 16 & 255},${n >> 8 & 255},${n & 255},${a.toFixed(2)})`; };
    const M = Math.max(0.5, Math.ceil(Math.max(...H.map(h => Math.max(Math.abs(h.sell ?? 0), Math.abs(h.buy ?? 0))), P.sell_floor ?? 0) * 1.1 * 2) / 2);
    const ln = (label, data, color, ex) => ({ label, data, borderColor: color, backgroundColor: color, fill: false, ...ex });
    const sets = [
      ln('SOC – plan', H.map(h => h.soc), pal.bat, { yAxisID: 'y' }),
      ln('SOC – bez strategii', H.map(h => h.base_soc), muted, { yAxisID: 'y', borderDash: [4, 4] }),
      ln('Cena sprzedaży', H.map(h => h.sell), pal.sell, { yAxisID: 'y1', stepped: 'middle', borderWidth: 1.5, pointRadius: 0, $price: 'sell' }),
      ln('Cena zakupu (w dół)', H.map(h => h.buy == null ? null : -h.buy), pal.buy, { yAxisID: 'y1', stepped: 'middle', borderWidth: 1.5, pointRadius: 0, $price: 'buy' }),
      ...(P.sell_floor != null ? [ln('Próg sprzedaży z magazynu', H.map(() => P.sell_floor), '#39d353', { yAxisID: 'y1', borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0, $skipTip: true })] : []),
      ln('0 zł', H.map(() => 0), text2, { yAxisID: 'y1', borderWidth: 1.2, pointRadius: 0, $skipTip: true, $hide: true }),
      ...KINDS.map(K => {
        const data = H.map((h, i) => kinds[i] === K.k ? (K.k === 'pv' || K.k === 'bat' ? (h.sell ?? 0) : -(h.buy ?? 0)) : null);
        const a = H.map(h => (K.k === 'home' ? 0.4 : 0.35) + 0.6 * Math.min(1, amt(h) / (h.grid < 0 ? maxExp : maxImp)));
        return { label: K.label, data, yAxisID: 'y2', type: 'bar', borderWidth: 0, stack: 'tx', $kind: K.k, $empty: data.every(v => v == null),
          backgroundColor: a.map(v => rgba(K.color, v)), borderColor: K.color };
      }),
      (() => { const data = H.map(h => (h.legal_cut || 0) > 0.05 ? (h.sell ?? 0) : null);
        return { label: 'Sprzedaż wstrzymana – limit arbitrażu', data, yAxisID: 'y2', type: 'bar', borderWidth: 0, stack: 'cut', $kind: 'cut',
          $empty: data.every(v => v == null), backgroundColor: H.map(h => rgba('#facc15', 0.45 + 0.5 * Math.min(1, (h.legal_cut || 0) / maxExp))), borderColor: '#facc15' }; })(),
    ];
    const zl2 = v => v.toFixed(2).replace('.', ','), narrow = cvs.parentElement.clientWidth < 520;
    const nowLine = { id: 'sgNow', afterDatasetsDraw: ch => {
      const i = ch.$nowIdx; if (i == null || i < 0) return;
      const xx = ch.scales.x.getPixelForValue(i), { top, bottom } = ch.chartArea, c = ch.ctx;
      c.save(); c.strokeStyle = muted; c.setLineDash([3, 3]); c.lineWidth = 1; c.beginPath(); c.moveTo(xx, top); c.lineTo(xx, bottom); c.stroke();
      c.fillStyle = text2; c.font = '11px sans-serif'; c.fillText('teraz', xx + 4, top + 10); c.restore(); } };
    if (!this._chart) {
      const genL = Chart.defaults.plugins.legend.labels.generateLabels;
      this._chart = new Chart(cvs, { type: 'line', data: { labels, datasets: sets }, plugins: [nowLine], options: {
        responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: 'index', intersect: false },
        elements: { point: { radius: 0, hoverRadius: 4 }, line: { borderWidth: 2, tension: 0.2 } },
        scales: {
          x: { stacked: true, ticks: { color: muted, maxTicksLimit: narrow ? 6 : 12, autoSkip: true, maxRotation: 0 }, grid: { display: false } },
          y: { min: 0, max: 100, ticks: { color: muted, callback: v => v + ' %' }, grid: { color: gridC }, border: { display: false } },
          y1: { position: 'right', grid: { display: false }, title: { display: !narrow, text: '↑ sprzedaż · cena zł/kWh · zakup ↓', color: muted },
            ticks: { color: muted, callback: v => Math.abs(v).toFixed(2).replace('.', ',') + (narrow ? '' : ' zł') } },
          y2: { display: false, stacked: true } },
        plugins: {
          legend: { position: 'top', align: 'start', labels: { color: text2, boxWidth: narrow ? 8 : 10, boxHeight: narrow ? 8 : 10, padding: narrow ? 6 : 10, font: { size: narrow ? 10 : 12 }, useBorderRadius: true, borderRadius: 3,
            filter: (it, dd) => { const q = dd.datasets[it.datasetIndex]; return !q.$hide && !q.$empty; },
            generateLabels: ch => genL(ch).map(l => { const q = ch.data.datasets[l.datasetIndex]; if (q && q.$kind) l.fillStyle = l.strokeStyle = q.borderColor; return l; }) } },
          tooltip: { enabled: !narrow, filter: it => !it.dataset.$skipTip && it.parsed.y != null && !(it.dataset.yAxisID === 'y2' && it.raw == null),
            callbacks: {
              label: c => {
                const h = this._chart.$h[c.dataIndex], q = c.dataset;
                if (q.yAxisID === 'y') return ` ${q.label}: ${c.parsed.y.toFixed(0)} %`;
                if (q.$price === 'sell') return ` Cena sprzedaży: ${c.parsed.y.toFixed(3)} zł/kWh`;
                if (q.$price === 'buy') return ` Cena zakupu: ${(-c.parsed.y).toFixed(3)} zł/kWh` + (h && h.tarcza ? ' (z Tarczą)' : '');
                if (!h) return '';
                if (q.$kind === 'cut') return ` Przekroczony arbitraż o ${zl2(h.legal_cut)} kWh (≈ ${zl2(h.legal_cut * h.sell)} zł)`;
                if (q.$kind === 'pv' || q.$kind === 'bat') { const v = q.$kind === 'bat' ? expBat(h) : expPv(h); return ` ${q.label}: ${zl2(v)} kWh × ${zl2(h.sell)} zł = ${zl2(v * h.sell)} zł`; }
                return ` ${q.label}: ${zl2(imp(h))} kWh × ${zl2(h.buy)} zł = ${zl2(imp(h) * h.buy)} zł`;
              },
              afterBody: it => { const h = this._chart.$h[it[0].dataIndex]; return h ? [`plan: ${h.label}`] : []; } } } } } });
    } else this._chart.data = { labels, datasets: sets };
    const ch = this._chart;
    ch.options.scales.y1.min = ch.options.scales.y2.min = -M; ch.options.scales.y1.max = ch.options.scales.y2.max = M;
    ch.$h = H; ch.$nowIdx = d.now_index ?? null; ch.update();
  }
  renderTable(d) {
    const tb = this.$('tb'); if (!tb) return;
    const P = d.params || {}, H = d.hours || [], f = (v, k) => v == null ? '–' : v.toFixed(k);
    const cut12 = (() => { const t = new Date(); t.setDate(t.getDate() + 1); t.setHours(12, 0, 0, 0); return t.getTime() / 1000; })();
    const arch = this.$('arch').checked, to12 = this.$('to12').checked, now = Date.now() / 1000;
    tb.innerHTML = H.filter(h => (arch || h.mode !== 'past') && (to12 || h.ts < cut12)).map(h =>
      `<tr${h.ts <= now && now < h.ts + 3600 ? ' class="cur"' : ''}><td>${h.day} ${h.t}</td><td>${f(h.buy, 3)}${h.tarcza ? '‡' : ''}${h.est ? (h.fc ? '†' : '*') : ''}</td>` +
      `<td${P.sell_floor != null && h.sell >= P.sell_floor ? ' style="color:var(--sg-sell);font-weight:600"' : ''}>${f(h.sell, 3)}</td><td>${f(h.load, 2)}</td><td>${f(h.pv, 2)}</td>` +
      `<td><span class="stm ${h.mode}">${this.esc(h.label)}</span></td><td>${f(Math.max(h.grid, 0), 2)}</td><td>${h.soc == null ? '–' : f(h.soc, 0) + ' %'}</td>` +
      `<td>${h.base_soc == null ? '–' : f(h.base_soc, 0) + ' %'}</td><td>${f(h.cost, 2)} / ${f(h.base_cost, 2)}</td></tr>`).join('') ||
      '<tr><td colspan="10">Brak godzin do pokazania.</td></tr>';
    this.$('notes').innerHTML = (H.some(h => h.est && !h.fc) ? '<div>* brak cen na jutro – przyjęto ceny z dziś</div>' : '') +
      (H.some(h => h.tarcza) ? '<div>‡ Tarcza Pstryk – zakup = limit + dystrybucja strefy G13 i akcyza</div>' : '') +
      (H.some(h => h.fc) ? '<div>† prognoza cen Pradcast (rynek RDN), sprzedaż liczona ostrożniej (× 0,9)</div>' : '');
  }
  disconnectedCallback() { super.disconnectedCallback(); if (this._ro) this._ro.disconnect(); }
  getCardSize() { return 12; }
  getGridOptions() { return { columns: 'full', min_columns: 6, rows: 'auto' }; }
}

// wspólne dla wykresów kart: opcje osi / legendy w kolorach motywu HA i linia „teraz”
function sgChartBase(card, unit, narrow) {
  const muted = card.cv('--sg-muted') || '#888', text2 = card.cv('--sg-text2') || '#666';
  const gridC = card._dark ? 'rgba(255,255,255,.08)' : 'rgba(0,0,0,.08)';
  return { responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: 'index', intersect: false },
    elements: { point: { radius: 0, hoverRadius: 4 }, line: { borderWidth: 2, tension: 0.2 } },
    plugins: { legend: { position: 'top', align: 'start', labels: { color: text2, boxWidth: narrow ? 8 : 10, boxHeight: narrow ? 8 : 10,
        padding: narrow ? 6 : 10, font: { size: narrow ? 10 : 12 }, useBorderRadius: true, borderRadius: 3 } },
      tooltip: { enabled: !narrow, callbacks: {} } },
    scales: { x: { ticks: { color: muted, maxTicksLimit: narrow ? 6 : 12, autoSkip: true, maxRotation: 0 }, grid: { display: false } },
      y: { ticks: { color: muted, callback: v => +v.toFixed(2) + ' ' + unit }, grid: { color: gridC }, border: { display: false } } } };
}
function sgNowLinePlugin(card) {
  return { id: 'sgNow', afterDatasetsDraw: ch => {
    const i = ch.$nowIdx; if (i == null || i < 0) return;
    const xx = ch.scales.x.getPixelForValue(i), { top, bottom } = ch.chartArea, c = ch.ctx;
    c.save(); c.strokeStyle = card.cv('--sg-muted') || '#888'; c.setLineDash([3, 3]); c.lineWidth = 1;
    c.beginPath(); c.moveTo(xx, top); c.lineTo(xx, bottom); c.stroke();
    c.fillStyle = card.cv('--sg-text2') || '#666'; c.font = '11px sans-serif'; c.fillText('teraz', xx + 4, top + 10); c.restore(); } };
}
// karta z wykresem: przebudowa wykresu po zmianie motywu / szerokości (telefon ↔ komputer)
class SigenChartCard extends SigenBase {
  cv(n) { const e = this.shadowRoot.querySelector('.sg'); return e ? getComputedStyle(e).getPropertyValue(n).trim() : ''; }
  _applyPal() { super._applyPal(); this._redraw(); }
  _redraw() { if (this._chart) { this._chart.destroy(); this._chart = null; } if (this._d) this.render(this._d); }
  afterBuild() {
    if (this._chart) { this._chart.destroy(); this._chart = null; }
    if (this._d) this.render(this._d);
    if (this._ro) this._ro.disconnect();
    this._ro = new ResizeObserver(() => {
      const t = this.$('tiles'); if (t) sgPopTiles(t);
      const w = this.shadowRoot.querySelector('.sg').clientWidth < 520;
      if (this._narrow !== undefined && w !== this._narrow) this._redraw();
      this._narrow = w;
    });
    this._ro.observe(this.shadowRoot.querySelector('.sg'));
  }
  get narrow() { const e = this.shadowRoot.querySelector('.sg'); return !!e && e.clientWidth < 520; }
  disconnectedCallback() { super.disconnectedCallback(); if (this._ro) this._ro.disconnect(); }
  getGridOptions() { return { columns: 'full', min_columns: 6, rows: 'auto' }; }
}
const SIGEN_CHART_CSS = `
  .chart { position: relative; height: 280px; margin: 4px 0 6px; }
  @container (max-width: 520px) { .chart { height: 300px; } }
  .note { font-size: 12px; color: var(--sg-text2); margin-top: 4px; }
  .seg { display: inline-flex; gap: 4px; }
  .seg button { font: inherit; font-size: 13px; padding: 4px 10px; border-radius: 8px; border: 1px solid var(--sg-border);
    background: transparent; color: var(--sg-text); cursor: pointer; }
  .seg button.on { background: var(--primary-color, #03a9f4); border-color: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
  .bar2 { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; margin: 0 0 8px; }
  .bar2 .sub { font-size: 13px; color: var(--sg-text2); }
  h3 { font-size: 14px; font-weight: 500; margin: 10px 0 6px; }
`;

// ---------------------------------------------------------------------------------------------------------------
// K2: Ceny energii – dziś i jutro (kafle + wykres zakup / sprzedaż / Pstryk)
class SigenPricesCard extends SigenChartCard {
  static defTitle = 'Ceny energii – dziś i jutro';
  static css = SIGEN_TILE_CSS + SIGEN_CHART_CSS;
  body() {
    return `${this._cfg.tiles !== false ? '<div class="tiles" id="tiles"></div>' : ''}
      <div class="chart"><canvas id="cv"></canvas></div><div class="note" id="note"></div>`;
  }
  polls() { return [[this.load, 300000], [this.tick, 60000]]; }
  async load() {
    const d = await this.get('/api/prices');
    this._d = d; this.showErr(d.status && d.status.error ? 'Ceny: ' + d.status.error : '');
    this.setStatus(true, 'ceny ' + sgHm(Date.now() / 1000));
    this.render(d);
  }
  async tick() { if (this._d) this.render(this._d); }   // przesuwanie „teraz” i kafli co minutę
  render(d) {
    const T = d.today || { points: [], cheapest: [] }, J = d.tomorrow || { points: [], cheapest: [] };
    const pts = [...T.points.map(p => ({ ...p, day: 'dziś' })), ...J.points.map(p => ({ ...p, day: 'jutro' }))];
    const nowTs = Date.now() / 1000, nowIdx = pts.findIndex(p => p.ts <= nowTs && nowTs < p.ts + 900), now = nowIdx >= 0 ? pts[nowIdx] : null;
    const PS = d.pstryk && T.pstryk, psMap = {};
    if (PS) [...T.pstryk.points, ...((J.pstryk && J.pstryk.points) || [])].forEach(p => psMap[p.ts] = p);
    const psAt = p => psMap[Math.floor(p.ts / 3600) * 3600], psNow = now ? psAt(now) : null;
    const zl = v => v == null ? null : v.toFixed(3);
    const jc = PS && J.pstryk && J.pstryk.cheapest.length ? J.pstryk.cheapest : J.cheapest;
    const ch = jc.length ? jc : PS ? T.pstryk.cheapest : T.cheapest;
    const box = this.$('tiles');
    if (box) {
      box.innerHTML = sgTile('c-red', 'Teraz – zakup', zl(now && now.buy), 'zł/kWh', now ? 'strefa: ' + this.esc(now.zone) : '') +
        sgTile('c-green', 'Teraz – sprzedaż', zl(now && now.sell), 'zł/kWh') +
        sgTile('', 'Śr. zakup dziś', zl(T.avg_buy), 'zł/kWh') +
        sgTile('', 'Śr. zakup jutro', zl(J.avg_buy), 'zł/kWh', J.points.length ? '' : 'jeszcze brak cen (PSE publikuje ok. 13:40)') +
        (PS ? sgTile('c-blue', 'Teraz – Pstryk (brutto)', zl(psNow && psNow.price), 'zł/kWh', psNow && psNow.pros != null ? `prosument ${psNow.pros.toFixed(3)} zł` : '') : '') +
        sgTile('', (jc.length ? 'Najtańsze godziny jutro' : 'Najtańsze godziny dziś') + (PS ? ' (Pstryk)' : ''),
          ch.length ? ch.map(c => c.hour.slice(0, 2)).join(', ') : null, '', ch.length ? ch.map(c => `${c.hour} ${c.buy.toFixed(3)} zł`).join(' · ') : '');
      sgPopTiles(box);
    }
    const nt = this.$('note');
    if (nt) nt.textContent = !pts.length ? 'Brak cen – aplikacja pobiera je z PSE (ceny na jutro po ok. 13:40).'
      : d.formula ? `Zakup = ${d.formula.zakup}. Sprzedaż = ${d.formula.sprzedaz}.` : '';
    if (pts.length) this.chart(pts, nowIdx, PS ? psAt : null);
  }
  async chart(pts, nowIdx, psAt) {
    const cvs = this.$('cv'); if (!cvs) return;
    let Chart; try { Chart = await sgChartJs(this.api); } catch (e) { this.showErr('Wykres: ' + e.message); return; }
    const pal = SIGEN_PAL[this._dark ? 'dark' : 'light'], labels = pts.map(p => `${p.day} ${p.t}`);
    const ln = (label, data, color) => ({ label, data, borderColor: color, backgroundColor: color, fill: false });
    const sets = [ln('Zakup', pts.map(p => p.buy), pal.buy), ln('Sprzedaż', pts.map(p => p.sell), pal.sell)];
    if (psAt) sets.push(ln('Pstryk (brutto)', pts.map(p => { const q = psAt(p); return q ? q.price : null; }), this._dark ? '#9085e9' : '#6250d6'));
    if (!this._chart) {
      const o = sgChartBase(this, 'zł', this.narrow);
      o.scales.y.ticks.callback = v => v.toFixed(2) + (this.narrow ? '' : ' zł');
      o.plugins.tooltip.callbacks.label = c => ` ${c.dataset.label}: ${c.parsed.y.toFixed(3)} zł/kWh`;
      o.plugins.tooltip.callbacks.afterBody = it => { const p = this._pts[it[0].dataIndex];
        return p && p.obsluga != null ? [`strefa: ${p.zone}`, `prąd z obsługą ${p.obsluga.toFixed(2)} + dystrybucja ${p.dist.toFixed(2)} + podatki ${p.podatki.toFixed(2)}`] : []; };
      this._chart = new Chart(cvs, { type: 'line', data: { labels, datasets: sets }, options: o, plugins: [sgNowLinePlugin(this)] });
    } else this._chart.data = { labels, datasets: sets };
    this._pts = pts; this._chart.$nowIdx = nowIdx >= 0 ? nowIdx : null; this._chart.update();
  }
  getCardSize() { return 7; }
}

// ---------------------------------------------------------------------------------------------------------------
// K3: Aktualne pomiary – Dziś / Tydzień / Miesiąc: kafle energii, autokonsumpcja / samowystarczalność, straty, wykres słupkowy
// opcje: okres: day | week | month (domyślny), losses: false – bez kafli zużycia własnego w miesiącu
class SigenPeriodCard extends SigenChartCard {
  static defTitle = 'Aktualne pomiary';
  static css = SIGEN_TILE_CSS + SIGEN_CHART_CSS + `
    @container (max-width: 520px) { .tiles { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
      .tile { padding: 6px 7px; } .tile .t { font-size: 11px; } .tile .v { font-size: 16px; } .tile .v small { font-size: 11px; } }`;
  body() {
    return `<div class="bar2"><span class="sub" id="ptitle"></span><span class="seg" id="seg">
        <button type="button" data-p="day">Dziś</button><button type="button" data-p="week">Tydzień</button><button type="button" data-p="month">Miesiąc</button></span></div>
      <div class="tiles" id="tiles"></div>
      ${this._cfg.losses !== false ? '<h3 id="lmh">Zużycie własne falownika i magazynu</h3><div class="tiles" id="lmt"></div>' : ''}
      <div class="chart"><canvas id="cv"></canvas></div><div class="note" id="note"></div>`;
  }
  afterBuild() {
    this._p = this._p || sgLs.get('sigen_ha_period') || this._cfg.okres || 'day';
    this.shadowRoot.querySelectorAll('#seg button').forEach(b => {
      b.classList.toggle('on', b.dataset.p === this._p);
      b.onclick = () => { this._p = b.dataset.p; sgLs.set('sigen_ha_period', this._p);
        this.shadowRoot.querySelectorAll('#seg button').forEach(x => x.classList.toggle('on', x === b));
        this.load().catch(e => this.onFail(e)); };
    });
    super.afterBuild();
  }
  polls() { return [[this.load, 300000], ...(this._cfg.losses !== false ? [[this.loadLm, 900000]] : [])]; }
  async load() {
    const d = await this.get('/api/period?okres=' + (this._p || 'day'));
    this._d = d; this.showErr(''); this.setStatus(true, 'odczyt ' + sgHm(Date.now() / 1000));
    this.render(d);
  }
  async loadLm() {
    const box = this.$('lmt'); if (!box) return;
    const m = new Date().toLocaleDateString('sv-SE').slice(0, 7);
    const r = await this.get('/api/losses_month?month=' + m), T = r.total || {}, f = (v, n = 1) => v == null ? null : v.toFixed(n).replace('.', ',');
    this.$('lmh').textContent = 'Zużycie własne falownika i magazynu – ' + (r.label || m);
    box.innerHTML = sgTile('c-red', 'Razem w miesiącu', f(T.system, 1), 'kWh', `${(r.days || []).length} dni${r.cost != null ? ' · ≈ ' + f(r.cost, 2) + ' zł' : ''}`) +
      sgTile('', 'Średnio na dobę', f(r.avg_day_kwh, 2), 'kWh', r.avg_w != null ? `≈ ${r.avg_w} W bez przerwy (pełne dni: ${r.full_days})` : 'brak pełnych dni') +
      sgTile('', 'Szacunek na miesiąc', f(r.month_est_kwh, 0), 'kWh', r.month_est_cost != null ? `≈ ${f(r.month_est_cost, 0)} zł (${f(r.price, 3)} zł/kWh)` : '') +
      sgTile('', 'w tym bateria', f(T.bat_loss, 1), 'kWh', `ładowanie ${f(T.chg, 1)} − rozładowanie ${f(T.dis, 1)} − zmiana zapasu ${f(T.d_bat, 1)} kWh`) +
      sgTile('', 'w tym czuwanie', f(T.czuwanie, 1), 'kWh', T.h_czuwanie ? `${Math.round(T.h_czuwanie)} h bez przetwarzania` : 'falownik nic nie przetwarza') +
      sgTile('', 'w tym konwersja', f(T.konwersja, 1), 'kWh', 'przetwarzanie PV, magazynu i AC');
    sgPopTiles(box);
  }
  render(d) {
    const e = d.energy || {}, src = k => e.src && e.src[k] === 'falownik' ? 'licznik falownika' : 'obliczone z mocy';
    const st = e.straty || {}, reg = st.regulacja_pobor != null ? st.regulacja_pobor + st.regulacja_oddanie : null, f2 = v => v == null ? null : v.toFixed(2);
    this.$('ptitle').textContent = d.title || '';
    const box = this.$('tiles');
    box.innerHTML = sgTile('c-pv', 'Produkcja PV', e.pv, 'kWh', src('pv')) + sgTile('c-blue', 'Zużycie domu', e.load, 'kWh', src('load')) +
      sgTile('c-red', 'Pobór z sieci', e.imp, 'kWh', src('imp')) + sgTile('c-green', 'Oddane do sieci', e.exp, 'kWh', src('exp')) +
      sgTile('', 'Ładowanie mag.', e.chg, 'kWh', src('chg')) + sgTile('', 'Rozładowanie mag.', e.dis, 'kWh', src('dis')) +
      sgTile('c-green', 'Autokonsumpcja', e.autokonsumpcja, '%', e.exp_pv != null ? `PV zużyte w domu i magazynie (do sieci z PV ${e.exp_pv.toFixed(1)} kWh, bez sprzedaży z magazynu)` : '') +
      sgTile('c-amber', 'Samowystarczalność', e.samowystarczalnosc, '%', e.imp_home != null ? `dom bez prądu z sieci (dom z sieci ${e.imp_home.toFixed(1)} kWh, bez ładowania magazynu z sieci)` : '') +
      sgTile('', 'Straty konwersji', f2(st.konwersja), 'kWh', 'przetwarzanie PV, magazynu i AC') +
      sgTile('', 'Czuwanie', f2(st.czuwanie), 'kWh', st.czuwanie_sr_w != null ? `śr. ${st.czuwanie_sr_w} W przez ${(st.h_czuwanie || 0).toFixed(1)} h bez przetwarzania` : 'gdy falownik nic nie przetwarza') +
      sgTile('', 'Odchyłki regulacji', f2(reg), 'kWh', reg != null ? `pobór z sieci ${f2(st.regulacja_pobor)} · oddanie ${f2(st.regulacja_oddanie)} kWh` : '') +
      sgTile('', 'Sprawność falownika', e.sprawnosc, '%', 'konwersja + czuwanie');
    sgPopTiles(box);
    this.$('note').textContent = (d.partial ? 'Historia miesięcy jest jeszcze przeliczana w tle – odśwież za kilka minut. ' : '') +
      'Autokonsumpcja i samowystarczalność bez arbitrażu (sprzedaż z magazynu i ładowanie z sieci nie zaniżają wyniku).';
    this.chart(d.buckets || []);
  }
  async chart(B) {
    const cvs = this.$('cv'); if (!cvs) return;
    let Chart; try { Chart = await sgChartJs(this.api); } catch (e) { this.showErr('Wykres: ' + e.message); return; }
    const pal = SIGEN_PAL[this._dark ? 'dark' : 'light'], labels = B.map(b => b.label);
    const rgba = (hex, a) => { const n = parseInt(hex.replace('#', ''), 16); return `rgba(${n >> 16 & 255},${n >> 8 & 255},${n & 255},${a})`; };
    const bar = (label, k, color, axis) => ({ label, data: B.map(b => b[k]), backgroundColor: color, borderColor: color, type: 'bar', yAxisID: axis, borderRadius: 3, maxBarThickness: 28 });
    const sets = [bar('Produkcja PV', 'pv', pal.pv, 'y'), bar('Zużycie domu', 'load', pal.load, 'y'),
      bar('Autokonsumpcja', 'autokonsumpcja', rgba(pal.bat, 0.55), 'y1'), bar('Samowystarczalność', 'samowystarczalnosc', 'rgba(250,204,21,0.8)', 'y1')];
    if (!this._chart) {
      const o = sgChartBase(this, 'kWh', this.narrow);
      o.scales.y1 = { position: 'right', min: 0, max: 100, grid: { display: false }, ticks: { color: this.cv('--sg-muted') || '#888', callback: v => v + ' %' } };
      o.plugins.tooltip.callbacks.label = c => c.parsed.y == null ? '' : ` ${c.dataset.label}: ${c.parsed.y.toFixed(c.dataset.yAxisID === 'y1' ? 0 : 1)} ${c.dataset.yAxisID === 'y1' ? '%' : 'kWh'}`;
      this._chart = new Chart(cvs, { type: 'bar', data: { labels, datasets: sets }, options: o });
    } else { this._chart.data = { labels, datasets: sets }; this._chart.update(); }
  }
  getCardSize() { return 9; }
}

// ---------------------------------------------------------------------------------------------------------------
// K7: Sprawność falownika – 3 kafle magazynu i jedna tabela: przedziały mocy × (całość / ładowanie / rozładowanie)
// opcje: okres: 1 | y | 7 | 30 | 0 (domyślny wybór; potem zapamiętany)
class SigenEffCard extends SigenBase {
  static defTitle = 'Sprawność falownika';
  static css = SIGEN_TILE_CSS + `
    .tiles { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    @container (max-width: 520px) { .tiles { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
      .tile { padding: 6px 7px; } .tile .t { font-size: 11px; white-space: normal; } .tile .v { font-size: 16px; } }
    .bar2 { display: flex; align-items: center; justify-content: space-between; gap: 8px 12px; flex-wrap: wrap; margin: 0 0 8px; font-size: 13px; color: var(--sg-text2); }
    .bar2 select { font: inherit; font-size: 13px; color: var(--sg-text); background: var(--sg-surface); border: 1px solid var(--sg-border); border-radius: 6px; padding: 4px 6px; }
    .bar2 label { display: flex; align-items: center; gap: 5px; cursor: pointer; }
    .tw { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th { font-weight: 500; color: var(--sg-text2); text-align: right; padding: 5px 6px; border-bottom: 1px solid var(--sg-border); }
    td { text-align: right; padding: 4px 6px; border-bottom: 1px solid var(--sg-border); white-space: nowrap; }
    th:first-child, td:first-child { text-align: left; }
    td.mu { color: var(--sg-muted); }
    tr.dim td { color: var(--sg-text2); }
    .effbar { display: inline-block; height: 8px; border-radius: 4px; vertical-align: middle; margin-right: 6px; }
    .sub { font-size: 11px; color: var(--sg-muted); font-weight: 400; }
    .expl { font-size: 12px; color: var(--sg-text2); margin: 8px 0 0; line-height: 1.45; }
    .expl p { margin: 0 0 4px; }
    @container (max-width: 520px) { .effbar, .sub, .expl { display: none; } th { font-size: 11px; } td, th { padding: 5px 4px; } }
  `;
  body() {
    return `<div class="tiles" id="tiles"></div>
      <div class="bar2"><select id="days"><option value="1">Dziś</option><option value="y">Wczoraj</option><option value="7">7 dni</option>
        <option value="30">30 dni</option><option value="0">Cała historia</option></select>
        <label><input type="checkbox" id="hide"> ukryj przedziały z * (&lt; 15 min danych)</label><span id="info"></span></div>
      <div class="tw"><table><thead><tr><th>Moc [kW]</th><th>Sprawność falownika (całość)</th><th>Ładowanie magazynu</th><th>Rozładowanie magazynu</th></tr></thead>
        <tbody id="tb"></tbody></table></div>
      <div class="expl"><p><b>Całość</b> – wszystkie minuty pracy wg łącznej mocy przetwarzanej (PV + rozładowanie + pobór AC); wyjście ÷ wejście.</p>
        <p><b>Ładowanie</b> – minuty, gdy ≥ 70 % energii wyjściowej idzie do magazynu; <b>Rozładowanie</b> – gdy ≥ 70 % wejścia pochodzi z magazynu.
        Pod wartością: czas w przedziale i średnia strata. <b>Czuwanie</b> – zużycie własne z sieci, gdy nic nie jest przetwarzane;
        <b>świt / zmierzch</b> (PV &lt; 0,25 kW) – niemiarodajne.</p></div>`;
  }
  afterBuild() {
    const sel = this.$('days'), hide = this.$('hide');
    sel.value = sgLs.get('sigen_ha_effdays') || String(this._cfg.okres ?? '7');
    hide.checked = sgLs.get('sigen_ha_effhide') === '1';
    sel.onchange = () => { sgLs.set('sigen_ha_effdays', sel.value); this.load().catch(e => this.onFail(e)); };
    hide.onchange = () => { sgLs.set('sigen_ha_effhide', hide.checked ? '1' : '0'); if (this._d) this.renderTable(this._d); };
    if (this._ro) this._ro.disconnect();
    this._ro = new ResizeObserver(() => sgPopTiles(this.$('tiles'))); this._ro.observe(this.shadowRoot.querySelector('.sg'));
  }
  disconnectedCallback() { super.disconnectedCallback(); if (this._ro) this._ro.disconnect(); }
  polls() { return [[this.load, 600000]]; }
  async load() {
    const sel = this.$('days').value;
    let date = new Date().toLocaleDateString('sv-SE'), days = sel;
    if (sel === 'y') { const y = new Date(); y.setDate(y.getDate() - 1); date = y.toLocaleDateString('sv-SE'); days = 1; }
    const bd = sel === '1' || sel === 'y' ? 1 : sel === '0' ? 90 : +sel;
    const [d, b] = await Promise.all([this.get(`/api/efficiency?date=${date}&days=${days}`), this.get(`/api/battery_eff?days=${bd}`).catch(() => null)]);
    this._d = d; this.showErr(''); this.setStatus(true, 'odczyt ' + sgHm(Date.now() / 1000));
    this.renderTiles(b, bd); this.renderTable(d);
  }
  renderTiles(b, days) {
    const box = this.$('tiles');
    if (!b || !b.ok) { box.innerHTML = ''; return; }
    const I = b.idle || {};
    box.innerHTML = sgTile(b.round_trip == null ? 'c-blue' : b.round_trip >= 90 ? 'c-green' : b.round_trip >= 80 ? 'c-blue' : 'c-red', 'Sprawność magazynu (round-trip)',
        b.round_trip == null ? null : b.round_trip.toFixed(1), '%',
        `ładowanie ${b.chg_kwh} kWh · rozładowanie ${b.dis_kwh} kWh · SOC ${b.soc_start} → ${b.soc_end} % w ${Math.round(b.period_h)} h (${days === 1 ? 'doba' : days + ' dni'}) – im dłuższy okres, tym dokładniej`) +
      sgTile('c-blue', 'Magazyn w czuwaniu – z ogniw', I.cells_w, 'W', I.hours ? `spadek SOC ${I.soc_drop_pct_h} %/h w ${I.hours} h (${I.runs} odcinki ≥ 3 h bez pracy magazynu); ok. 0 W = BMS zasilany z sieci` : 'brak odcinków ≥ 3 h bez pracy magazynu') +
      sgTile('c-blue', 'Czuwanie – pobór z sieci', I.grid_w, 'W', I.hours ? 'pobór falownika z AC w tych samych odcinkach (zużycie własne systemu)' : '');
    sgPopTiles(box);
  }
  renderTable(d) {
    const hide = this.$('hide').checked, range = b => b.do == null ? `≥ ${b.od}` : `${b.od}–${b.do}`;
    const pct = v => { const c = v >= 90 ? '--sg-sell' : v >= 70 ? '--sg-grid' : '--sg-buy';
      return `<span class="effbar" style="width:${Math.max(4, Math.min(60, (v - 50) * 1.2))}px;background:var(${c})"></span><span style="color:var(${c});font-weight:600">${v.toFixed(1)} %</span>`; };
    const cell = b => {
      if (!b || (hide && b.mala_probka)) return '<td class="mu">–</td>';
      const tip = `${range(b)} kW: ${b.godzin.toFixed(2)} h, śr. moc ${b.moc_sr_kw.toFixed(2)} kW, energia wej. ${b.e_in.toFixed(2)} kWh, straty ${b.straty_kwh.toFixed(3)} kWh (śr. ${b.strata_sr_w} W)` +
        (b.mala_probka ? ' – mniej niż 15 min danych' : '');
      return `<td title="${tip}"${b.mala_probka ? ' style="opacity:.55"' : ''}>${b.sprawnosc == null ? '–' : pct(b.sprawnosc)}${b.mala_probka ? ' *' : ''}` +
        `<div class="sub">${b.godzin.toFixed(1)} h · ${b.strata_sr_w} W</div></td>`;
    };
    const by = a => Object.fromEntries((a || []).map(b => [b.od, b]));
    const G = by(d.bins), C = by(d.ladowanie), D = by(d.rozladowanie);
    const ods = [...new Set([...(d.bins || []), ...(d.ladowanie || []), ...(d.rozladowanie || [])].map(b => b.od))].sort((x, y) => x - y);
    let rows = ods.filter(od => [G[od], C[od], D[od]].some(b => b && !(hide && b.mala_probka)))
      .map(od => `<tr><td>${range(G[od] || C[od] || D[od])}</td>${cell(G[od])}${cell(C[od])}${cell(D[od])}</tr>`).join('');
    const I = d.czuwanie, W = d.slabe_pv;
    if (W) rows = `<tr class="dim"><td>świt / zmierzch</td><td>${W.sprawnosc == null ? '–' : W.sprawnosc.toFixed(1) + ' %'}<div class="sub">niemiarodajne</div></td><td class="mu">–</td><td class="mu">–</td></tr>` + rows;
    if (I) rows = `<tr class="dim"><td>czuwanie</td><td>${I.sr_w} W<div class="sub">pobór z sieci, ${I.godzin.toFixed(1)} h</div></td><td class="mu">–</td><td class="mu">–</td></tr>` + rows;
    this.$('tb').innerHTML = rows || '<tr><td colspan="4" class="mu">Brak danych.</td></tr>';
    this.$('info').textContent = `dni z danymi: ${d.n_days ?? '–'}`;
  }
  getCardSize() { return 8; }
  getGridOptions() { return { columns: 'full', min_columns: 6, rows: 'auto' }; }
}

// ---------------------------------------------------------------------------------------------------------------
// K8: Licznik operatora – Pstryk: dzień i miesiąc z licznika, Tarcza, depozyt, szacowana faktura, porównanie z G13
// opcje: g13: false – bez porównania z G13
class SigenPstrykCard extends SigenBase {
  static defTitle = 'Licznik operatora – Pstryk';
  static css = SIGEN_TILE_CSS + `
    h3 { font-size: 14px; font-weight: 500; margin: 10px 0 6px; }
    .tile.bal { border-width: 2px; }
    details summary { cursor: pointer; font-size: 14px; font-weight: 500; margin: 8px 0 6px; }
    .tw { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th { font-weight: 500; color: var(--sg-text2); text-align: right; padding: 5px 6px; border-bottom: 1px solid var(--sg-border); }
    td { text-align: right; padding: 4px 6px; border-bottom: 1px solid var(--sg-border); white-space: nowrap; }
    th:first-child, td:first-child { text-align: left; }
    td.best { color: var(--sg-sell); font-weight: 700; }
    tr.buy td { background: color-mix(in srgb, var(--sg-buy) 6%, transparent); } tr.sell td { background: color-mix(in srgb, var(--sg-sell) 7%, transparent); }
    .note { font-size: 12px; color: var(--sg-text2); margin-top: 4px; }
    @container (max-width: 520px) { .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .g13 tr > :nth-child(2), .g13 tr > :nth-child(6), .hrs tr > :nth-child(6), .hrs tr > :nth-child(7) { display: none; } td, th { padding: 5px 4px; } }
  `;
  body() {
    return `<div id="off" class="note" hidden>Pstryk nie jest skonfigurowany w aplikacji (brak klucza API).</div>
      <h3 id="dt">Dziś</h3><div class="tiles" id="day"></div>
      <h3 id="mt"></h3><div class="tiles" id="month"></div>
      ${this._cfg.g13 !== false ? `<details id="g13box" hidden><summary id="g13s">Porównanie z taryfą G13 (TAURON)</summary>
        <div class="tw"><table class="g13"><thead><tr><th>Miesiąc</th><th>Pobór kWh</th><th>Pstryk</th><th id="g13a">G13 – te same godziny</th><th id="g13b">G13 – ładowanie w pozostałych</th><th>Depozyt niewykorzystany</th></tr></thead>
        <tbody id="g13"></tbody></table></div><div class="note">Zielone – najtańsza opcja w miesiącu; szczegóły po najechaniu na kwotę.
        W G13 depozyt pokrywa tylko energię (dystrybucja i opłaty stałe zawsze do zapłaty).</div></details>` : ''}
      <details><summary>Tabela godzinowa (dziś)</summary><div class="tw"><table class="hrs"><thead><tr><th>Godz.</th><th>Pobór kWh</th><th>Oddanie kWh</th>
        <th>Koszt zł</th><th>Sprzedaż zł</th><th>Cena zakupu</th><th>Cena sprzedaży</th></tr></thead><tbody id="hrs"></tbody></table></div></details>
      <div class="note">Dane z licznika operatora przez API Pstryka (co 15 min, ostatnie godziny mogą się jeszcze uzupełnić). Depozyt zarobiony w miesiącu
        pomniejsza fakturę za następny miesiąc.</div>`;
  }
  afterBuild() {
    if (this._ro) this._ro.disconnect();
    this._ro = new ResizeObserver(() => this.shadowRoot.querySelectorAll('.tiles').forEach(sgPopTiles));
    this._ro.observe(this.shadowRoot.querySelector('.sg'));
  }
  disconnectedCallback() { super.disconnectedCallback(); if (this._ro) this._ro.disconnect(); }
  polls() { return [[this.load, 900000], ...(this._cfg.g13 !== false ? [[this.loadG13, 3600000]] : [])]; }
  async load() {
    const d = await this.get('/api/pstryk?date=' + new Date().toLocaleDateString('sv-SE'));
    this.$('off').hidden = !!d.enabled;
    if (!d.enabled) { this.setStatus(false, 'brak Pstryka'); return; }
    const st = d.status || {}, D = d.day || {}, M = d.month || {}, S = d.sigen || {}, I = d.invoice;
    this.showErr(st.error ? 'Pstryk: ' + st.error : '');
    this.setStatus(!st.error, st.last ? 'synchronizacja ' + sgHm(st.last) : 'pierwsza synchronizacja…');
    const z2 = v => v == null ? null : v.toFixed(2);
    const MN = ['styczeń', 'luty', 'marzec', 'kwiecień', 'maj', 'czerwiec', 'lipiec', 'sierpień', 'wrzesień', 'październik', 'listopad', 'grudzień'];
    const mn = (key, k = 0) => { const m = +key.split('-')[1]; return MN[(m - 1 + k + 12) % 12]; };
    const sigenOk = S.imp != null && (S.imp > 0 || S.exp > 0);
    const diff = (a, b) => a == null || b == null || !sigenOk ? '' : `Sigen: ${b.toFixed(2)} kWh (różnica ${(a - b >= 0 ? '+' : '') + (a - b).toFixed(2)})`;
    const lastH = D.last_ts ? new Date(D.last_ts * 1000).getHours() + 1 : null;
    const upto = D.hours && D.hours < 24 && lastH ? ` · dane do ${String(lastH).padStart(2, '0')}:00` : '';
    const bal = (t, b) => b == null ? sgTile('c-blue', t, null, 'zł') : sgTile('bal ' + (b > 0.005 ? 'c-red' : b < -0.005 ? 'c-green' : 'c-blue'), t,
      (b < -0.005 ? '+' : b > 0.005 ? '−' : '') + Math.abs(b).toFixed(2), 'zł', b > 0.005 ? 'koszt większy od sprzedaży' : b < -0.005 ? 'sprzedaż większa od kosztu' : 'koszt = sprzedaż');
    this.$('dt').textContent = 'Dziś' + (upto ? ' (' + upto.slice(3) + ')' : '');
    this.$('day').innerHTML = !D.hours ? '<div class="note">Brak danych licznika dla dzisiaj.</div>' :
      sgTile('c-red', 'Pobór (licznik)', D.imp.toFixed(3), 'kWh', diff(D.imp, S.imp)) + sgTile('c-green', 'Oddanie (licznik)', D.exp.toFixed(3), 'kWh', diff(D.exp, S.exp)) +
      sgTile('c-red', 'Koszt dnia', z2(D.cost), 'zł', `w tym stałe ok. ${z2(D.fixed_gross)} zł${upto}`) + sgTile('c-green', 'Sprzedaż energii', z2(D.sold), 'zł') + bal('Bilans dnia', D.balance);
    this.$('mt').textContent = `Miesiąc: ${M.label || ''}` + (M.days ? ` (${M.days} ${M.days === 1 ? 'dzień' : 'dni'} z danymi)` : '');
    this.$('month').innerHTML = !M.hours ? '<div class="note">Brak danych w tym miesiącu.</div>' :
      sgTile('c-red', 'Koszt miesiąca', z2(M.cost), 'zł', `w tym stałe ok. ${z2(M.fixed_gross)} zł`) + sgTile('c-red', 'Pobór', M.imp.toFixed(2), 'kWh') +
      sgTile('c-green', 'Oddanie', M.exp.toFixed(2), 'kWh') +
      (I && I.tarcza_avg != null ? sgTile(I.tarcza > 0.005 ? 'c-green' : 'c-blue', 'Tarcza Pstryk', '≈' + I.tarcza.toFixed(2), 'zł',
        `średni koszt energii z opłatą za obsługę ≈ ${I.tarcza_avg.toFixed(3)} zł/kWh · limit ${I.tarcza_cap.toFixed(2)} zł/kWh` + (I.tarcza > 0.005 ? ' – zniżka na fakturze' : ' – bez zniżki')) : '') +
      (I ? sgTile('c-green', `Depozyt na fakturę (${mn(I.month)})`, I.deposit.toFixed(2), 'zł', `${this.esc(I.deposit_src)} · zarobiony w ${mn(I.month, -1)} (poprawka depozytu – w aplikacji)`) +
        sgTile(I.to_pay > 0.005 ? 'c-red' : 'c-green', `Faktura za ${mn(I.month)} (szac.)`, I.to_pay > 0.005 ? I.to_pay.toFixed(2) : '0,00', 'zł',
          `koszt ${I.cost.toFixed(2)} (energia ${I.parts.energy.toFixed(2)} + dystrybucja ${I.parts.dist.toFixed(2)} + obsługa ${I.parts.service.toFixed(2)}) − Tarcza ${I.tarcza.toFixed(2)} − depozyt ${I.deposit.toFixed(2)} zł` +
          (I.left > 0.005 ? ` · reszta ${I.left.toFixed(2)} zł depozytu przechodzi na ${mn(I.month, 1)}` : '')) +
        sgTile('c-green', `Depozyt na ${mn(I.month, 1)}`, (I.next_deposit + I.left).toFixed(2), 'zł', `oddanie w ${mn(I.month)} ${I.next_deposit.toFixed(2)} zł` +
          (I.left > 0.005 ? ` + reszta ${I.left.toFixed(2)} zł` : '') + ` – pomniejszy fakturę za ${mn(I.month, 1)} (do teraz)`) : '');
    this.shadowRoot.querySelectorAll('.tiles').forEach(sgPopTiles);
    const f = (v, n) => v == null ? '–' : v.toFixed(n);
    this.$('hrs').innerHTML = (d.hours || []).map(h => `<tr class="${h.cost == null && h.sold == null ? '' : (h.cost || 0) > (h.sold || 0) ? 'buy' : (h.sold || 0) > (h.cost || 0) ? 'sell' : ''}">` +
      `<td>${sgHm(h.ts)}</td><td>${f(h.imp, 3)}</td><td>${f(h.exp, 3)}</td><td>${f(h.cost, 2)}</td><td>${f(h.sold, 2)}</td><td>${f(h.price_gross, 3)}</td><td>${f(h.price_pros, 3)}</td></tr>`).join('') ||
      '<tr><td colspan="7">Brak danych.</td></tr>';
  }
  async loadG13() {
    const tb = this.$('g13'); if (!tb) return;
    let r;
    try { r = await this.get('/api/g13_compare?months=6'); }
    catch (e) {   // wersja publiczna: porównanie ze sprzedawcą taryfowym wybranej taryfy (/api/porownanie)
      r = await this.get('/api/porownanie?months=6');
      const tn = r.tariff ? [r.tariff.operator, r.tariff.taryfa].filter(Boolean).join(' ') || 'taryfa' : 'taryfa';
      this.$('g13s').textContent = 'Porównanie ze sprzedawcą taryfowym (' + tn + ')';
      this.$('g13a').textContent = 'Taryfa – te same godziny'; this.$('g13b').textContent = 'Taryfa – najtańsza strefa';
      r.months = (r.months || []).filter(m => m.same && m.night).map(m => ({ ...m, g13_same: m.same, g13_night: m.night }));
    }
    if (!r.enabled || !(r.months || []).length) return;
    this.$('g13box').hidden = false;
    const z = v => v.toFixed(2).replace('.', ',');
    tb.innerHTML = r.months.map(m => {
      const opts = [['p', m.pstryk.to_pay], ['s', m.g13_same.to_pay], ['n', m.g13_night.to_pay]], b = opts.reduce((x, y) => y[1] < x[1] ? y : x)[0];
      const c = (k, v, t) => `<td title="${t}"${b === k ? ' class="best"' : ''}>${z(v)} zł</td>`;
      return `<tr><td>${m.label}${m.partial ? ` (${m.days} dni)` : ''}</td><td>${m.imp.toFixed(0)}</td>` +
        c('p', m.pstryk.to_pay, `koszt ${z(m.pstryk.cost)} − Tarcza ${z(m.pstryk.tarcza)} − depozyt ${z(m.pstryk.deposit)} zł`) +
        c('s', m.g13_same.to_pay, `energia ${z(m.g13_same.energy)} − depozyt ${z(m.g13_same.deposit_used)} + dystrybucja ${z(m.g13_same.dist)} + stałe ${z(m.g13_same.fixed)} zł`) +
        c('n', m.g13_night.to_pay, `energia ${z(m.g13_night.energy)} − depozyt ${z(m.g13_night.deposit_used)} + dystrybucja ${z(m.g13_night.dist)} + stałe ${z(m.g13_night.fixed)} zł`) +
        `<td>${z(m.g13_night.deposit_left)} zł</td></tr>`;
    }).join('');
  }
  getCardSize() { return 8; }
  getGridOptions() { return { columns: 'full', min_columns: 6, rows: 'auto' }; }
}

// ---------------------------------------------------------------------------------------------------------------
// K5: Prognoza produkcji PV – kafle, wybór głównego źródła, wykres prognoz (wybór źródeł, rozrzut modeli), trafność, korekta
const sgMin = m => String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(Math.floor(m % 60)).padStart(2, '0');
const sgDT = ts => new Date(ts * 1000).toLocaleString('pl-PL', { weekday: 'short', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
function sgInterp(pts, m) {
  if (!pts || !pts.length || m < pts[0][0] || m > pts[pts.length - 1][0]) return 0;
  for (let i = 1; i < pts.length; i++) { const [m1, p1] = pts[i - 1], [m2, p2] = pts[i]; if (m <= m2) return m2 === m1 ? p2 : p1 + (p2 - p1) * (m - m1) / (m2 - m1); }
  return 0;
}
class SigenForecastCard extends SigenChartCard {
  static defTitle = 'Prognoza produkcji PV';
  static css = SIGEN_TILE_CSS + SIGEN_CHART_CSS + `
    .chart { height: 300px; }
    .bar2 select { font: inherit; font-size: 13px; color: var(--sg-text); background: var(--sg-surface); border: 1px solid var(--sg-border); border-radius: 6px; padding: 4px 6px; }
    .pick { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 12px; color: var(--sg-text2); margin: 0 0 4px; }
    .pick label { display: flex; align-items: center; gap: 4px; cursor: pointer; }
    .pick i { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
    details summary { cursor: pointer; font-size: 14px; font-weight: 500; margin: 8px 0 6px; }
    .tw { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th { font-weight: 500; color: var(--sg-text2); text-align: right; padding: 4px 6px; border-bottom: 1px solid var(--sg-border); }
    td { text-align: right; padding: 4px 6px; border-bottom: 1px solid var(--sg-border); white-space: nowrap; }
    th:first-child, td:first-child { text-align: left; }
    .mu { color: var(--sg-muted); font-size: 11px; }
  `;
  body() {
    return `<div class="bar2"><span class="sub" id="fst"></span><label class="sub">główne źródło <select id="main"></select></label></div>
      <div class="note" id="mainmsg"></div>
      <div class="tiles" id="tiles"></div>
      <div class="pick" id="pick"></div>
      <div class="chart"><canvas id="cv"></canvas></div><div class="note" id="note"></div>
      <details><summary>Trafność prognoz (z dnia poprzedniego vs rzeczywista)</summary><div class="tw"><table><thead id="acch"></thead><tbody id="accb"></tbody></table></div></details>
      <details><summary>Korekta prognozy wg zachmurzenia</summary><div class="tw"><table><thead id="corh"></thead><tbody id="corb"></tbody></table></div>
        <div class="note" id="corn"></div></details>`;
  }
  polls() { return [[this.load, 600000], [this.tick, 120000]]; }
  async tick() { if (this._d) { const d = await this.get('/api/forecast'); this._d = d; this.render(d); } }
  async load() {
    const d = await this.get('/api/forecast');
    this._d = d; this.showErr(''); this.setStatus(true, 'prognoza ' + sgHm(Date.now() / 1000));
    this.render(d);
  }
  render(d) {
    const N = d.names || {}, S = d.sources || {}, T = d.today, J = d.tomorrow, FS = S.forecast_solar || {}, EC = S.om_ecmwf || {}, OM = S.open_meteo || {};
    const sel = this.$('main');
    if (!sel.dataset.fl) { sel.dataset.fl = '1'; sgFlavor(this.api).then(f => { if (f.pub) { sel.disabled = true; sel.title = 'główne źródło – w Ustawieniach aplikacji'; } }); }
    if (!sel.dataset.ready) {
      sel.innerHTML = Object.keys(N).map(k => `<option value="${k}">${this.esc(N[k])}</option>`).join(''); sel.dataset.ready = '1';
      sel.onchange = async () => {
        const r = await this.post('/api/forecast/main', { source: sel.value }), m = this.$('mainmsg');
        m.textContent = r.ok ? '' : (r.need_pin ? 'Zmiana źródła wymaga PIN-u – wpisz go w karcie „Sigen – strategia” (Ustawienia), potem spróbuj ponownie.' : (r.error || 'błąd'));
        if (r.ok) this.load().catch(e => this.onFail(e)); else sel.value = this._d.main;
      };
    }
    if (document.activeElement !== this) sel.value = d.main;
    this.$('fst').textContent = 'aktualizacja: ' + Object.keys(d.groups || {}).map(k => { const s = (d.status || {})[k] || {};
      return d.groups[k] + (s.error ? ' – błąd: ' + s.error : s.updated ? ' ' + sgDT(s.updated) : ''); }).join(' · ');
    const range = when => { const v = (d.models || []).map(m => S[m] && S[m][when] && S[m][when].total_kwh).filter(x => x != null); return v.length ? [Math.min(...v), Math.max(...v)] : null; };
    const note = (r, fs, ec, om) => [d.main !== 'open_meteo' && om ? `Open-Meteo ${om.total_kwh}` : '', d.main !== 'om_ecmwf' && ec ? `ECMWF ${ec.total_kwh}` : '',
      d.main !== 'forecast_solar' && fs ? `forecast.solar ${fs.total_kwh}` : '', r ? `modele ${r[0].toFixed(1)}–${r[1].toFixed(1)}` : ''].filter(Boolean).join(' · ');
    const when = f => !f ? '' : f.kind === 'dzien_wczesniej' ? ` · prognoza z dnia poprzedniego (${f.fetched ? sgDT(f.fetched) : '–'})` : '';
    const nowHM = sgMin(d.now_min || 0), fcSrc = T ? `${N[d.main]}${T.kind === 'dzien_wczesniej' ? ' z ' + sgDT(T.fetched) : ' (bieżąca)'}` : '';
    const pct = T && d.forecast_until_now_kwh > 0.05 ? Math.round(100 * d.actual_kwh / d.forecast_until_now_kwh) : null;
    const nT = note(range('today'), FS.today, EC.today, OM.today), nJ = note(range('tomorrow'), FS.tomorrow, EC.tomorrow, OM.tomorrow);
    const box = this.$('tiles');
    box.innerHTML = sgTile('c-pv', 'Prognoza na dziś', T && T.total_kwh, 'kWh', `<b>${this.esc(N[d.main] || '')}</b> (główna)${when(T)}` + (nT ? '<br>dla porównania: ' + nT : '')) +
      sgTile('', 'Wyprodukowano dziś', d.actual_kwh, 'kWh', d.actual_src === 'falownik' ? 'licznik falownika' : 'obliczone z mocy') +
      sgTile('', 'Prognoza do teraz', d.forecast_until_now_kwh, 'kWh', T ? `od wschodu do ${nowHM} · ${fcSrc}` : '') +
      sgTile(pct == null ? '' : pct >= 90 ? 'c-green' : pct >= 60 ? 'c-amber' : 'c-red', 'Realizacja prognozy', pct, '%', pct != null ? `wyprodukowano ${d.actual_kwh} z ${d.forecast_until_now_kwh} kWh przewidzianych do ${nowHM}` : '') +
      sgTile('c-pv', 'Prognoza na jutro', J && J.total_kwh, 'kWh', `<b>${this.esc(N[d.main] || '')}</b> (główna)${when(J)}` + (nJ ? '<br>dla porównania: ' + nJ : '')) +
      sgTile('', 'Szczyt jutro', J ? J.peak_kw : null, J ? 'kW o ' + sgMin(J.peak_min) : '');
    sgPopTiles(box);
    this.renderAcc(d, N); this.renderCorr(d, N, T);
    this.chart(d, N, S, T, J, FS);
  }
  renderAcc(d, N) {
    const acc = d.accuracy || { days: [] }, cols = Object.keys(N).filter(k => acc.days.some(r => r[k]));
    this.$('acch').innerHTML = '<tr><th>Dzień</th><th>Rzeczywista</th>' + cols.map(k => `<th>${this.esc(N[k])}</th>`).join('') + '</tr>';
    const tint = t => `background:hsla(${Math.round(120 * (1 - Math.max(0, Math.min(1, t))))},70%,45%,.22)`;
    const mean = Object.fromEntries(cols.map(k => { const ds = acc.days.filter(r => r[k] && r[k].day_ahead != null && r.actual_kwh >= 0.3);
      if (!ds.length) return [k, null]; const abs = ds.reduce((a, r) => a + Math.abs(r.actual_kwh - r[k].day_ahead), 0), act = ds.reduce((a, r) => a + r.actual_kwh, 0);
      return [k, { mae: abs / ds.length, pct: act > 0 ? 100 * abs / act : null, n: ds.length }]; }));
    const ranked = cols.filter(k => mean[k]).sort((a, b) => mean[a].mae - mean[b].mae);
    const rankT = k => ranked.length > 1 ? ranked.indexOf(k) / (ranked.length - 1) : 0;
    const cell = (r, k) => { const v = r[k] && r[k].day_ahead; if (v == null) return '<td>–</td>';
      const err = r.actual_kwh > 0.3 ? Math.abs(r.actual_kwh - v) / r.actual_kwh : null;
      return `<td${err != null ? ` style="${tint(err / 0.5)}"` : ''}>${v}</td>`; };
    this.$('accb').innerHTML = acc.days.length ? (ranked.length ? '<tr><td><b>Średnie odchylenie</b></td><td class="mu">' + Math.max(...ranked.map(k => mean[k].n)) + ' dni</td>' +
      cols.map(k => mean[k] ? `<td style="${tint(rankT(k))}"><b>±${mean[k].mae.toFixed(2)}</b> <span class="mu">(${mean[k].pct != null ? mean[k].pct.toFixed(0) + ' %' : '–'})</span></td>` : '<td>–</td>').join('') + '</tr>' : '') +
      acc.days.map(r => `<tr><td>${r.day}</td><td>${r.actual_kwh}</td>${cols.map(k => cell(r, k)).join('')}</tr>`).join('')
      : `<tr><td colspan="${2 + cols.length}" class="mu">Pierwsze wyniki po pierwszym pełnym dniu z prognozą z dnia poprzedniego.</td></tr>`;
  }
  renderCorr(d, N, T) {
    const CR = d.correction || {}, CC = d.correction_cfg || {}, CL = [['cloudy', 'pochmurno'], ['partly', 'częściowo'], ['sunny', 'słonecznie']];
    this.$('corh').innerHTML = '<tr><th>Źródło</th>' + CL.map(([, n]) => `<th>${n}</th>`).join('') + '</tr>';
    this.$('corb').innerHTML = Object.keys(CR).length ? Object.entries(CR).sort((a, b) => (b[0] === d.main) - (a[0] === d.main)).map(([k, v]) =>
      `<tr><td>${this.esc(N[k] || k)}${k === d.main ? ' (główna)' : ''}</td>` + CL.map(([c]) => { const x = v[c];
        return !x || x.factor == null ? '<td>–</td>' : `<td style="${x.active ? 'font-weight:600' : 'color:var(--sg-muted)'}">×${x.factor.toFixed(2)} <span class="mu">${x.days} dni</span></td>`; }).join('') + '</tr>').join('')
      : '<tr><td colspan="4" class="mu">Brak danych.</td></tr>';
    this.$('corn').textContent = `Współczynnik = rzeczywista ÷ prognoza z dnia poprzedniego (ostatnie ${CC.learn_days || 45} dni); pogrubione działają (od ${CC.min_days || 5} dni w klasie).` +
      (T && T.correction && T.correction !== 1 ? ` Dziś główna po korekcie ${T.total_kwh} kWh (surowa ${T.raw_kwh} kWh).` : '');
  }
  async chart(d, N, S, T, J, FS) {
    const cvs = this.$('cv'); if (!cvs) return;
    const all = [T, J, ...Object.values(S).flatMap(x => [x && x.today, x && x.tomorrow])].filter(x => x && x.sunrise != null);
    if (!all.length) { this.$('note').textContent = 'Brak prognozy – aplikacja pobierze ją, gdy będzie dostęp do internetu.'; return; }
    let Chart; try { Chart = await sgChartJs(this.api); } catch (e) { this.showErr('Wykres: ' + e.message); return; }
    const [sr, ss] = (d.sun && d.sun.today) || [null, null], mg = d.margin_min || 0;
    const from = sr != null ? Math.floor((sr + mg) / 10) * 10 : Math.floor(Math.min(...all.map(x => x.sunrise)) / 10) * 10;
    const to = ss != null ? Math.ceil((ss - mg) / 10) * 10 : Math.ceil(Math.max(...all.map(x => x.sunset)) / 10) * 10;
    const grid = []; for (let m = from; m <= to; m += 10) grid.push(m);
    const act = new Map((d.actual || []).map(([m, v]) => [m, v]));
    const line = x => grid.map(m => x ? +sgInterp(x.points, m).toFixed(3) : null);
    const band = when => { const ls = (d.models || []).map(m => S[m] && S[m][when]).filter(Boolean).map(line);
      return ls.length ? [grid.map((_, i) => Math.min(...ls.map(l => l[i]))), grid.map((_, i) => Math.max(...ls.map(l => l[i])))] : [null, null]; };
    const [tMin, tMax] = band('today'), [jMin, jMax] = band('tomorrow');
    const pal = SIGEN_PAL[this._dark ? 'dark' : 'light'];
    const COL = { open_meteo: pal.load, om_srednia: '#6250d6', om_ecmwf: pal.sell, om_icon_eu: pal.grid, om_ukmo: '#6250d6', om_gfs: pal.pv,
      ens_p50: pal.load, ens_p10: '#6250d6', ens_p90: pal.pv, forecast_solar: pal.grid };
    const srcKeys = Object.keys(N).filter(k => S[k] && (S[k].today || S[k].tomorrow));
    let pick; try { pick = JSON.parse(sgLs.get('sigen_ha_fcpick') || 'null'); } catch (e) { pick = null; }
    if (!Array.isArray(pick) || !pick.some(k => srcKeys.includes(k))) pick = [d.main, d.main === 'om_ecmwf' ? 'open_meteo' : 'om_ecmwf'].filter(k => srcKeys.includes(k));
    const pb = this.$('pick');
    pb.innerHTML = srcKeys.map(k => `<label><input type="checkbox" value="${k}"${pick.includes(k) ? ' checked' : ''}><i style="background:${COL[k] || '#888'}"></i>${this.esc(N[k])}${k === d.main ? ' (główna)' : ''}</label>`).join('') +
      `<label><input type="checkbox" value="_band" id="band"${pick.includes('_band') ? '' : ' checked'}> rozrzut modeli</label>`;
    pb.querySelectorAll('input').forEach(x => x.onchange = () => {
      const s = [...pb.querySelectorAll('input:checked')].map(y => y.value).filter(v => v !== '_band');
      sgLs.set('sigen_ha_fcpick', JSON.stringify(this.$('band').checked ? s : [...s, '_band']));
      if (this._chart) { this._chart.destroy(); this._chart = null; } this.render(this._d);
    });
    const ln = (label, data, color, ex) => ({ label, data, borderColor: color, backgroundColor: color, fill: false, pointStyle: 'line', ...ex });
    const bs = (label, data, color, fill) => ({ label, data, borderColor: color + '55', backgroundColor: color + '26', pointRadius: 0, borderWidth: 0, fill, tension: 0.2, pointStyle: 'rect' });
    const sets = [ln('Produkcja dziś', grid.map(m => m <= d.now_min ? (act.get(m) ?? null) : null), pal.pv, { borderWidth: 3 })];
    pick.filter(k => srcKeys.includes(k)).forEach(k => { const c = COL[k] || '#888';
      if (S[k].today) sets.push(ln(`${N[k]} dziś`, line(S[k].today), c, { borderDash: [6, 4], borderWidth: 2 }));
      if (S[k].tomorrow) sets.push(ln(`${N[k]} jutro`, line(S[k].tomorrow), c, { borderDash: [2, 3], borderWidth: 1.5 })); });
    if (!pick.includes('_band') && tMin) sets.push(bs('_tmin', tMin, pal.load, false), bs('Rozrzut modeli dziś', tMax, pal.load, '-1'), bs('_jmin', jMin, '#6250d6', false), bs('Rozrzut modeli jutro', jMax, '#6250d6', '-1'));
    const labels = grid.map(sgMin), nowIdx = d.now_min >= from && d.now_min <= to ? (d.now_min - from) / 10 : null;
    if (!this._chart) {
      const o = sgChartBase(this, 'kW', this.narrow); o.scales.y.min = 0;
      Object.assign(o.plugins.legend.labels, { usePointStyle: true, boxWidth: 20,
        filter: (it, dd) => !it.text.startsWith('_') && (dd.datasets[it.datasetIndex].data || []).some(v => v != null && v > 0) });
      o.plugins.tooltip.filter = it => !it.dataset.label.startsWith('_') && it.parsed.y != null;
      o.plugins.tooltip.callbacks.label = c => ` ${c.dataset.label}: ${c.parsed.y.toFixed(2)} kW`;
      this._chart = new Chart(cvs, { type: 'line', data: { labels, datasets: sets }, options: o, plugins: [sgNowLinePlugin(this)] });
    } else this._chart.data = { labels, datasets: sets };
    this._chart.$nowIdx = nowIdx; this._chart.update();
    this.$('note').textContent = sr != null ? `Wschód ${sgMin(sr)} · zachód ${sgMin(ss)} · wykres od ${sgMin(from)} do ${sgMin(to)}` : '';
  }
  getCardSize() { return 9; }
}

// ---------------------------------------------------------------------------------------------------------------
// K6: Realna produkcja PV – połacie (kafle Dziś / Tydzień / Miesiąc) + produkcja w miesiącach (słupki)
class SigenStringsCard extends SigenChartCard {
  static defTitle = 'Realna produkcja PV – połacie';
  static css = SIGEN_TILE_CSS + SIGEN_CHART_CSS + `
    .tiles { grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); }
    @container (max-width: 520px) { .tiles { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; } .tile { padding: 6px 7px; } .tile .t { font-size: 11px; } .tile .v { font-size: 16px; } }
    .chart { height: 240px; }
  `;
  body() {
    return `<div class="bar2"><span class="sub" id="pst"></span><span class="seg" id="seg">
        <button type="button" data-p="day">Dziś</button><button type="button" data-p="week">Tydzień</button><button type="button" data-p="month">Miesiąc</button></span></div>
      <div class="tiles" id="tiles"></div>
      ${this._cfg.months !== false ? '<h3>Produkcja w miesiącach <span class="sub" id="mst" style="font-size:12px;color:var(--sg-text2)"></span></h3><div class="chart"><canvas id="cv"></canvas></div>' : ''}
      <div class="note">Moc połaci = napięcie × prąd stringu (DC, przed falownikiem).</div>`;
  }
  afterBuild() {
    this._p = this._p || sgLs.get('sigen_ha_pvs') || 'day';
    this.shadowRoot.querySelectorAll('#seg button').forEach(b => {
      b.classList.toggle('on', b.dataset.p === this._p);
      b.onclick = () => { this._p = b.dataset.p; sgLs.set('sigen_ha_pvs', this._p);
        this.shadowRoot.querySelectorAll('#seg button').forEach(x => x.classList.toggle('on', x === b)); this.loadS().catch(e => this.onFail(e)); };
    });
    super.afterBuild();
  }
  polls() { return [[this.loadS, 120000], ...(this._cfg.months !== false ? [[this.load, 1800000]] : [])]; }
  async loadS() {
    const d = await this.get('/api/pv_strings?okres=' + (this._p || 'day'));
    this.showErr(''); this.setStatus(true, 'odczyt ' + sgHm(Date.now() / 1000));
    const per = { day: 'dziś', week: 'tydzień', month: 'miesiąc' }[d.okres || 'day'];
    let nowP = null, nowT = null;
    if ((d.okres || 'day') === 'day') { const l = (d.points || [])[d.points.length - 1]; if (l) { nowP = l; nowT = l.t; } } else { nowP = d.now; nowT = d.now_t; }
    this.$('pst').textContent = (d.okres && d.okres !== 'day' && d.from ? `od ${new Date(d.from + 'T12:00').toLocaleDateString('pl-PL', { day: 'numeric', month: 'numeric' })} · ` : '') +
      (nowT ? 'ostatni odczyt ' + nowT : '');
    const box = this.$('tiles'), cls = ['c-pv', 'c-amber', 'c-blue', 'c-green'];
    box.innerHTML = (d.names || []).map((n, i) => sgTile(cls[i % cls.length], 'PV ' + this.esc(n) + ' – ' + per, d.kwh[n], 'kWh',
      (d.share[n] != null ? d.share[n] + ' % produkcji' : '') + (nowP && nowP[n] != null ? ` · teraz ${sgKw(nowP[n])}` : ''))).join('') ||
      '<div class="note">Brak danych ze stringów.</div>';
    sgPopTiles(box);
  }
  async load() { const d = await this.get('/api/pv_monthly'); this._d = d; this.render(d); }
  render(d) {
    const M = d.months || [];
    const st = this.$('mst'); if (st) st.textContent = d.total != null ? `· licznik: ${d.total.toLocaleString('pl-PL')} kWh` : '';
    this.chart(M);
  }
  async chart(M) {
    const cvs = this.$('cv'); if (!cvs) return;
    let Chart; try { Chart = await sgChartJs(this.api); } catch (e) { this.showErr('Wykres: ' + e.message); return; }
    const MCOL = ['#3b82f6', '#06b6d4', '#14b8a6', '#22c55e', '#84cc16', '#eab308', '#f59e0b', '#f97316', '#ef4444', '#db2777', '#a855f7', '#6366f1'];
    const mcol = m => MCOL[((+String(m.month || '').slice(5, 7) || 1) - 1) % 12];
    const sets = [{ label: 'Produkcja PV', data: M.map(m => m.kwh), backgroundColor: M.map(mcol), borderWidth: 0, borderRadius: 4, maxBarThickness: 60 }];
    const labels = M.map(m => m.label);
    if (!this._chart) {
      const o = sgChartBase(this, 'kWh', this.narrow); o.scales.y.min = 0; o.plugins.legend.display = false;
      o.plugins.tooltip.callbacks.label = c => ` Produkcja PV: ${c.parsed.y.toFixed(1)} kWh`;
      o.plugins.tooltip.callbacks.footer = it => { const m = this._m[it[0].dataIndex]; return m ? [`${m.days} dni z odczytami`] : []; };
      this._chart = new Chart(cvs, { type: 'bar', data: { labels, datasets: sets }, options: o });
    } else this._chart.data = { labels, datasets: sets };
    this._m = M; this._chart.update();
  }
  getCardSize() { return 6; }
}

// ---------------------------------------------------------------------------------------------------------------
// K9: Ustawienia falownika (tylko odczyt) – alarmy, granice SOC (pasek), limity przyłącza, Remote EMS, historia zmian
class SigenInverterCard extends SigenBase {
  static defTitle = 'Ustawienia falownika';
  static css = `
    .al { border: 2px solid var(--sg-border); border-radius: 12px; padding: 10px 14px; margin-bottom: 12px; }
    .al.good { border-color: var(--sg-ok); background: color-mix(in srgb, var(--sg-ok) 10%, transparent); }
    .al.bad { border-color: var(--sg-err); background: color-mix(in srgb, var(--sg-err) 12%, transparent); }
    .al-t { font-size: 13px; font-weight: 600; color: var(--sg-text2); margin-bottom: 2px; }
    .al-v { font-size: 22px; font-weight: 700; } .al.good .al-v { color: var(--sg-ok); }
    .al-i { font-size: 17px; font-weight: 650; color: var(--sg-err); margin: 2px 0; }
    .al-n { font-size: 11px; color: var(--sg-muted); margin-top: 4px; }
    .regs { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 10px; }
    .grp { border: 1px solid var(--sg-border); border-radius: 10px; padding: 8px 12px; }
    .grp.soc { grid-column: 1 / -1; }
    .grp h3 { font-size: 13px; margin: 0 0 6px; color: var(--sg-text2); font-weight: 600; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    td { padding: 5px 4px; border-bottom: 1px solid var(--sg-border); vertical-align: top; }
    tr:last-child td { border-bottom: 0; }
    td.v { font-weight: 600; text-align: right; } td.na { color: var(--sg-muted); font-weight: 400; }
    .w { font-size: 11px; color: var(--sg-muted); font-weight: 400; }
    .risk { font-size: 10px; padding: 0 5px; border-radius: 5px; border: 1px solid; font-weight: 500; }
    .risk.bezpieczne { color: var(--sg-ok); } .risk.ostroznie { color: var(--sg-grid); } .risk.nie_ruszac { color: var(--sg-err); }
    .socrow { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); border: 1px solid var(--sg-border); border-radius: 10px; overflow: hidden; }
    .soccell { padding: 8px 12px; border-left: 1px solid var(--sg-border); } .soccell:first-child { border-left: 0; }
    .soccell .t { font-size: 12px; color: var(--sg-text2); } .soccell .v { font-size: 22px; font-weight: 700; margin: 2px 0; }
    .socg { margin: 4px 0 12px; }
    .socg-bar { position: relative; height: 18px; border-radius: 9px; background: color-mix(in srgb, var(--sg-text) 12%, transparent); margin-top: 22px; }
    .socg-bar > div { position: absolute; top: 0; bottom: 0; }
    .socg-off { background: repeating-linear-gradient(45deg, color-mix(in srgb, var(--sg-text) 14%, transparent) 0 4px, transparent 4px 8px); }
    .socg-on { background: color-mix(in srgb, var(--sg-bat) 55%, transparent); }
    .socg-bk { width: 2px; margin-left: -1px; top: -4px !important; bottom: -4px !important; background: var(--sg-grid); }
    .socg-now { width: 3px; margin-left: -1.5px; top: -5px !important; bottom: -5px !important; background: var(--sg-text); border-radius: 2px; }
    .socg-now span { position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%); font-size: 12px; font-weight: 700; white-space: nowrap; margin-bottom: 1px; }
    .socg-ax { position: relative; height: 16px; font-size: 11px; color: var(--sg-text2); } .socg-ax span { position: absolute; top: 2px; white-space: nowrap; }
    .socg-leg { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 11px; color: var(--sg-text2); margin-top: 4px; }
    .socg-leg i { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 4px; vertical-align: -1px; }
    details summary { cursor: pointer; font-size: 14px; font-weight: 500; margin: 10px 0 6px; }
    @container (max-width: 520px) { .socrow { grid-template-columns: repeat(3, minmax(0, 1fr)); } .soccell { padding: 6px 8px; }
      .soccell .t { font-size: 11px; } .soccell .v { font-size: 18px; } .soccell .w { display: none; } .regs { grid-template-columns: 1fr; } }
  `;
  body() {
    return `<div id="alarms"></div><div class="regs" id="regs"></div>
      <details><summary>Historia zmian ustawień i alarmów</summary><table><tbody id="hist"></tbody></table></details>
      <div class="w" style="margin-top:6px">Tylko odczyt z falownika (co 5 min). Zmiany ustawień – w aplikacji mySigen.</div>`;
  }
  polls() { return [[this.load, 300000], [this.loadSoc, 30000]]; }
  async loadSoc() {
    const d = await this.get('/api/live'); if (d.soc == null) return;
    this._soc = d.soc;
    const n = this.shadowRoot.querySelector('.socg-now');
    if (n) { n.style.left = Math.max(0, Math.min(100, d.soc)) + '%'; n.firstChild.textContent = d.soc.toFixed(0) + ' %'; }
    else if (this._d) this.render(this._d);
  }
  async load() {
    const d = await this.get('/api/settings'); this._d = d; this.showErr('');
    this.setStatus(!!d.ts, d.ts ? 'odczyt ' + sgHms(d.ts) : 'czekam na odczyt…');
    this.render(d);
  }
  fmt(it) { return it.value == null ? 'brak' : this.esc(it.text ?? (typeof it.value === 'number' ? it.value.toLocaleString('pl-PL') : it.value)) + (it.unit && !it.text ? ' ' + this.esc(it.unit) : ''); }
  gauge(items) {
    const v = k => { const it = items.find(i => i.key === k); return it && typeof it.value === 'number' ? it.value : null; };
    const hi = v('soc_charge_cut'), lo = v('soc_discharge_cut'), bk = v('soc_backup'), soc = this._soc;
    if (hi == null || lo == null) return '';
    const cl = x => Math.max(0, Math.min(100, x)), axl = x => `<span style="left:${cl(x)}%;transform:translateX(${x >= 95 ? -100 : x <= 5 ? 0 : -50}%)">${x} %</span>`;
    return `<div class="socg"><div class="socg-bar"><div class="socg-off" style="left:0;width:${cl(lo)}%"></div><div class="socg-on" style="left:${cl(lo)}%;width:${cl(hi - lo)}%"></div>` +
      `<div class="socg-off" style="left:${cl(hi)}%;width:${100 - cl(hi)}%"></div>` + (bk != null ? `<div class="socg-bk" style="left:${cl(bk)}%"></div>` : '') +
      (soc != null ? `<div class="socg-now" style="left:${cl(soc)}%"><span>${soc.toFixed(0)} %</span></div>` : '') + `</div><div class="socg-ax">${axl(lo)}${axl(hi)}</div>` +
      `<div class="socg-leg"><span><i style="background:color-mix(in srgb, var(--sg-bat) 55%, transparent)"></i>zakres pracy ${lo}–${hi} %</span>` +
      (bk != null ? `<span><i style="background:var(--sg-grid);width:3px"></i>rezerwa ${bk} %</span>` : '') + (soc != null ? `<span><i style="background:var(--sg-text);width:3px"></i>teraz</span>` : '') + '</div></div>';
  }
  render(d) {
    const RISK = { bezpieczne: 'bezpieczne', ostroznie: 'ostrożnie', nie_ruszac: 'nie ruszać' };
    if (d.alarms) {
      const act = [];
      d.alarms.forEach(a => (a.active || []).forEach(x => { const src = a.name.split(' – ')[0], o = act.find(y => y.text === x.text);
        if (o) { if (!o.srcs.includes(src)) o.srcs.push(src); } else act.push({ ...x, srcs: [src] }); }));
      const unread = d.alarms.every(a => a.value == null), cls = !d.alarms_ts || unread ? '' : act.length ? ' bad' : ' good';
      this.$('alarms').innerHTML = `<div class="al${cls}"><div class="al-t">Alarmy instalacji i falownika</div>` +
        (!d.alarms_ts ? '<div class="al-v">czekam na odczyt…</div>' : unread ? '<div class="al-v">brak odczytu</div>' : !act.length ? '<div class="al-v">✓ Brak alarmów</div>'
          : act.map(x => `<div class="al-i">⚠ ${this.esc(x.srcs.join(' i '))}: ${this.esc(x.text)}</div>`).join('')) +
        `<div class="al-n">${d.alarms_ts ? 'odczyt ' + sgHms(d.alarms_ts) + ' · co 30 s · ' : ''}sprawdzane: DC i sieć, upływ, izolacja, AFCI (łuk elektryczny)</div></div>`;
    }
    if (d.ts) this.$('regs').innerHTML = (d.groups || []).map(g => g.group.startsWith('Magazyn – granice SOC')
      ? `<div class="grp soc"><h3>${this.esc(g.group)}</h3>${this.gauge(g.items)}<div class="socrow">` + g.items.map(it => `<div class="soccell"><div class="t">${this.esc(it.name)}</div>` +
        `<div class="v">${this.fmt(it)}</div><div class="w">działa: ${this.esc(it.when)}</div></div>`).join('') + '</div></div>'
      : `<div class="grp"><h3>${this.esc(g.group)}</h3><table><tbody>` + g.items.map(it => `<tr><td>${this.esc(it.name)}<br><span class="w">działa: ${this.esc(it.when)}</span></td>` +
        `<td class="v${it.value == null ? ' na' : ''}">${this.fmt(it)}<br><span class="risk ${it.risk}">${RISK[it.risk] || ''}</span></td></tr>`).join('') + '</tbody></table></div>').join('');
    this.$('hist').innerHTML = (d.history || []).length ? d.history.slice(0, 30).map(h => `<tr><td class="w">${new Date(h.ts * 1000).toLocaleString('pl-PL')}</td><td>${this.esc(h.name)}</td>` +
      `<td${h.alarm && h.value !== 'brak alarmów' ? ' style="color:var(--sg-err)"' : ''}>${this.esc(h.value)}</td></tr>`).join('') : '<tr><td class="w">Brak zapisów.</td></tr>';
  }
  getCardSize() { return 8; }
  getGridOptions() { return { columns: 'full', min_columns: 6, rows: 'auto' }; }
}

const SIGEN_CARDS = [
  ['sigen-live-card', SigenLiveCard, 'Sigen – teraz', 'Przepływy energii PV / dom / magazyn / sieć, SOC i strategia teraz (aplikacja Sigen Dashboard).'],
  ['sigen-prices-card', SigenPricesCard, 'Sigen – ceny energii', 'Ceny zakupu i sprzedaży dziś i jutro (PSE / Pstryk), najtańsze godziny.'],
  ['sigen-period-card', SigenPeriodCard, 'Sigen – aktualne pomiary', 'Dziś / Tydzień / Miesiąc: produkcja PV, zużycie, sieć, magazyn, autokonsumpcja, straty, wykres słupkowy.'],
  ['sigen-forecast-card', SigenForecastCard, 'Sigen – prognoza PV', 'Prognoza produkcji PV dziś i jutro: kafle, wykres źródeł i rozrzutu modeli, trafność, korekta.'],
  ['sigen-strings-card', SigenStringsCard, 'Sigen – połacie PV', 'Realna produkcja połaci (Dziś / Tydzień / Miesiąc) i produkcja w miesiącach.'],
  ['sigen-inverter-card', SigenInverterCard, 'Sigen – ustawienia falownika', 'Alarmy, granice SOC (pasek), limity przyłącza, Remote EMS i historia zmian (tylko odczyt).'],
  ['sigen-eff-card', SigenEffCard, 'Sigen – sprawność falownika', 'Sprawność magazynu (round-trip), czuwanie i tabela sprawności wg mocy: całość, ładowanie, rozładowanie.'],
  ['sigen-pstryk-card', SigenPstrykCard, 'Sigen – licznik Pstryk', 'Licznik operatora (Pstryk): dzień i miesiąc, Tarcza, depozyt, szacowana faktura, porównanie z G13.'],
  ['sigen-strategy-card', SigenStrategyCard, 'Sigen – strategia', 'Strategia osobista: kafle, sterowanie falownikiem (odczyt), wykres planu i plan godzinowy.'],
];
window.customCards = window.customCards || [];
for (const [type, cls, name, description] of SIGEN_CARDS) {
  if (!customElements.get(type)) customElements.define(type, cls);
  if (!window.customCards.some(c => c.type === type)) window.customCards.push({ type, name, description, preview: false });
}
console.info(`%c SIGEN-CARDS %c ${SIGEN_CARDS_VERSION} `, 'background:#1baf7a;color:#fff;border-radius:3px 0 0 3px', 'background:#444;color:#fff;border-radius:0 3px 3px 0');
