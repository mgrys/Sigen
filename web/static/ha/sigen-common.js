// Sigen Dashboard – wspólny kod strony (index.html) i kart Home Assistant (sigen-cards.js) – od 1.31.
// Strona wczytuje /ha/sigen-common.js w <head>; aplikacja podaje karty HA jako sigen-common.js + sigen-cards.js (jeden plik),
// więc zmiana tutaj działa od razu w obu: kolory serii, liczby z przecinkiem, kafle, linia „teraz”.

// kolory serii (jasny / ciemny motyw) – jedyne źródło; strona dostaje je jako zmienne CSS --s-<klucz> (sgPalCss)
const SIGEN_PAL = {
  light: { pv: '#eb6834', load: '#2a78d6', bat: '#1baf7a', grid: '#eda100', loss: '#e87ba4', sell: '#008300', buy: '#e34948', err: '#e34948', ok: '#1baf7a',
    sellbat: '#39d353', buygrid: '#9e1b1e', buyhome: '#f19d9c', arb: '#facc15', fc2: '#6250d6' },
  dark: { pv: '#d95926', load: '#3987e5', bat: '#199e70', grid: '#c98500', loss: '#d55181', sell: '#2f9e2f', buy: '#e66767', err: '#e66767', ok: '#199e70',
    sellbat: '#39d353', buygrid: '#c0392b', buyhome: '#f19d9c', arb: '#facc15', fc2: '#9085e9' },
};
const SIGEN_PAL_VARS = ['pv', 'load', 'bat', 'grid', 'loss', 'sell', 'buy', 'sellbat', 'buygrid', 'buyhome', 'arb', 'fc2'];

// zmienne CSS strony z SIGEN_PAL (motyw jasny, ciemny wg systemu i wybrany ręcznie – data-theme)
function sgPalCss() {
  const v = k => SIGEN_PAL_VARS.map(x => `--s-${x}: ${SIGEN_PAL[k][x]};`).join(' ');
  const st = document.createElement('style');
  st.id = 'sigen-pal';
  st.textContent = `:root { ${v('light')} } @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { ${v('dark')} } } ` +
    `:root[data-theme="dark"] { ${v('dark')} }`;
  document.head.appendChild(st);
}

// liczby do wyświetlania z przecinkiem dziesiętnym (toFixed zostaje do SVG / CSS / obliczeń)
if (!Number.prototype.toFx) Number.prototype.toFx = function (d) { return this.toFixed(d).replace('.', ','); };

// kafle: liczba wg jednostki – kWh 2 miejsca (dzień) / kwhDec (okresy), zł 2, % i °C 1, W 0, kW 2, h 1, zł/kWh 3
const SG_DEC = { 'kWh': null, 'zł': 2, '%': 1, '°C': 1, 'W': 0, 'kW': 2, 'h': 1, 'zł/kWh': 3 };
function sgNum(v, unit, kwhDec = 2) {
  if (typeof v === 'number' && isFinite(v)) {
    const d = unit === 'kWh' ? kwhDec : SG_DEC[unit];
    return d != null ? v.toFx(d) : String(v).replace('.', ',');
  }
  if (typeof v === 'string' && /^[-+−≈~]?\d+\.\d+$/.test(v)) return v.replace('.', ',');
  return v;
}

// indeks etykiety „HH:MM” najbliższej (nie późniejszej) bieżącej godzinie – linia „teraz” na wykresach dnia
function sgNowLabelIdx(labels, isToday = true) {
  if (!isToday) return null;
  const n = new Date(), m = n.getHours() * 60 + n.getMinutes();
  let best = null, bd = 1e9;
  (labels || []).forEach((l, i) => {
    const t = /^(\d{1,2}):(\d{2})/.exec(String(l)); if (!t) return;
    const x = +t[1] * 60 + +t[2];
    if (x <= m && m - x < bd) { bd = m - x; best = i; }
  });
  return best;
}

// ---- 1.31: wspolne rysowanie strony i kart HA ------------------------------------------------------------------------
// chwilowy podzial przeplywow jak _flows w app.py: PV -> dom, magazyn, siec; dom z magazynu, potem z sieci [kW]
function sgFlowSplit(d) {
  const pv = Math.max(d.pv || 0, 0), load = Math.max(d.load || 0, 0), chg = Math.max(d.bat || 0, 0), dis = Math.max(-(d.bat || 0), 0);
  const imp = Math.max(d.grid || 0, 0), exp = Math.max(-(d.grid || 0), 0);
  const pl = Math.min(pv, load), pb = Math.min(pv - pl, chg), pg = Math.min(pv - pl - pb, exp);
  const bl = Math.min(dis, load - pl), gl = Math.min(imp, load - pl - bl), gb = Math.min(imp - gl, chg - pb), bg = Math.min(dis - bl, exp - pg);
  return { 'pv-load': pl, 'pv-grid': pg, 'pv-bat': pb, 'grid-load': gl, 'grid-bat': gb, 'bat-load': bl, 'bat-grid': bg };
}
const SG_FLOW_NAMES = { pv: 'PV', load: 'dom', grid: 'sieć', bat: 'magazyn' };
// galezie schematu „Teraz” (SVG, viewBox 400 x 312): luki zrodlo -> odbior, widoczne tylko przy przeplywie; kolor = zrodlo
function sgBranchPaths(cv) {     // cv: nazwa zmiennej CSS dla klucza, np. k => `var(--s-${k})`
  return [['pv-load', 'M214 64 Q300 66 334 132'], ['pv-grid', 'M186 64 Q100 66 66 132'], ['pv-bat', 'M200 64 L200 232'],
    ['grid-load', 'M66 150 L334 150'], ['grid-bat', 'M66 168 Q100 234 186 236'], ['bat-load', 'M214 236 Q300 234 334 168'],
    ['bat-grid', 'M186 236 Q100 234 66 168']]
    .map(([id, d]) => `<path id="b-${id}" class="line br" d="${d}" stroke="${cv(id.split('-')[0])}"><title></title></path>`).join('');
}
// linia z kropkami: grubosc i predkosc wg mocy (0,1 kW cienko i wolno, 5 kW i wiecej grubo i szybko)
function sgSetLine(el, value, inward) {
  if (!el) return;
  const kw = Math.abs(value || 0), on = kw >= 0.01;
  el.classList.toggle('on', on);
  el.classList.toggle('rev', on && !inward);
  el.style.strokeWidth = on ? (3 + Math.min(5, kw * 1.1)).toFixed(1) : '';
  el.style.setProperty('--fd', on ? Math.max(0.35, 1.8 - 0.3 * kw).toFixed(2) + 's' : '1s');
}
function sgBranches(get, d) {    // get: id -> element
  const F = sgFlowSplit(d);
  for (const [id, v] of Object.entries(F)) {
    const el = get('b-' + id); if (!el) continue;
    sgSetLine(el, v, true);
    const [a, b] = id.split('-');
    if (el.firstElementChild) el.firstElementChild.textContent = `${SG_FLOW_NAMES[a]} → ${SG_FLOW_NAMES[b]}: ${v >= 1 ? v.toFx(2) + ' kW' : Math.round(v * 1000) + ' W'}`;
  }
}

// pasek SOC z 10 segmentow: kolor wg pochodzenia energii (/api/bat_origin), kreski granic SOC; col = {pv, grid, other}
function sgSocSeg(soc, B, col) {
  const s = Math.max(0, Math.min(100, soc ?? (B ? B.soc : 0)));
  const sh = B ? B.pct : { pv: 0, grid: 0, other: 100 };
  const pvEnd = s * (sh.pv || 0) / 100, grEnd = pvEnd + s * (sh.grid || 0) / 100;
  let bar = '';
  for (let i = 0; i < 10; i++) {
    const a = i * 10, fill = Math.max(0, Math.min(10, s - a)) / 10, mid = a + Math.min(10, Math.max(0, s - a)) / 2;
    const c = mid < pvEnd ? col.pv : mid < grEnd ? col.grid : col.other;
    bar += `<i>${fill > 0 ? `<b style="width:${(fill * 100).toFixed(0)}%;background:${c}"></b>` : ''}</i>`;
  }
  const lim = v => v == null ? '' : `<span class="lim" style="left:calc(${v}% - 1px)" title="granica SOC ${v} %"></span>`;
  if (B) bar += lim(B.soc_min) + (B.soc_max != null && B.soc_max < 100 ? lim(B.soc_max) : '');
  const f = v => (v ?? 0).toFx(1);
  const leg = B ? `<span><em style="background:${col.pv}"></em>z PV ${f(B.kwh.pv)} kWh</span>` +
    `<span><em style="background:${col.grid}"></em>z sieci ${f(B.kwh.grid)} kWh</span>` +
    (B.kwh.other >= 0.05 ? `<span><em style="background:${col.other}"></em>wcześniej ${f(B.kwh.other)} kWh</span>` : '') +
    `<span title="ładowanie z PV / z sieci liczone z pomiarów z ostatnich ${B.days} dni, rozładowanie zabiera po równo">ⓘ</span>` : '';
  return { bar, leg };
}

// „Obecny tryb”: kto steruje i co dalej (c - /api/inverter/ctl, mode / cls - z sgNowMode albo renderStNow, strat - /api/strategy)
function sgCurMode(c, mode, cls, strat) {
  const now = Date.now() / 1000, hm = ts => new Date(ts * 1000).toLocaleTimeString('pl-PL', { hour: '2-digit', minute: '2-digit' });
  const left = ts => { const s = Math.max(0, ts - now); return s >= 3600 ? `${Math.floor(s / 3600)} h ${Math.floor(s % 3600 / 60)} min` : `${Math.max(1, Math.round(s / 60))} min`; };
  const paused = c.pause_until && c.pause_until > now;
  const remote = (c.manual || (c.tryb === 'wl' && !paused)) && c.desired;
  const x = strat && strat.next && strat.next.ts > now ? strat.next : null;
  let m = mode, src, next = '', k = (cls || '').replace('m-stby', '');
  if (c.manual) {
    src = 'ręcznie · Zdalny tryb EMS'; k = 'm-man';
    next = c.manual.do ? `koniec za ${left(c.manual.do)} (${hm(c.manual.do)}) → strategia` : 'do anulowania';
  } else if (paused) {
    m = 'Tryb falownika'; src = 'pauza strategii – steruje falownik (mySigen)'; k = '';
    next = `wznowienie za ${left(c.pause_until)} (${hm(c.pause_until)})`;
  } else if (remote) {
    src = 'Zdalny tryb EMS · wg strategii' + (c.applied ? '' : ' · wysyłam…');
    if (x) next = `zmiana za ${left(x.ts)} → ${x.label}`;
  } else {
    m = c.owned ? 'Oddaję sterowanie…' : 'Tryb falownika'; k = '';
    src = c.tryb === 'probny' ? 'steruje falownik (mySigen) · strategia próbnie, bez zapisu' : 'steruje falownik (mySigen) · strategia tylko symulacją';
    if (x) next = `plan: ${String(mode).toLowerCase()} · zmiana za ${left(x.ts)} → ${x.label}`;
  }
  if ((c.mismatch && c.mismatch.alert) || c.error) { k = 'm-warn'; src += c.error ? ' · błąd zapisu' : ' · falownik robi co innego!'; }
  return { mode: m, src, next, cls: k };
}

// porownanie z poprzednim okresem (C6): strzalka przy wartosci kafla + opis do dymku
const SG_CMP_GOOD = { pv: 1, load: -1, imp: -1, exp: 0, chg: 0, dis: 0, autokonsumpcja: 1, samowystarczalnosc: 1 };
function sgCmpDelta(cur, ref, k) {
  if (cur == null || ref == null) return null;
  if (k === 'autokonsumpcja' || k === 'samowystarczalnosc') return { v: cur - ref, txt: Math.abs(cur - ref).toFx(1) + ' pp' };
  if (!(ref > 0.05)) return null;
  const p = 100 * (cur - ref) / ref;
  return { v: p, txt: Math.abs(p).toFx(Math.abs(p) < 10 ? 1 : 0) + ' %' };
}
function sgCmp(e, C, k, unit, kwhDec, esc = s => s) {
  let badge = '';
  const extra = [];
  (C || []).forEach((c, i) => {
    const cur = (c.cur || e)[k], ref = c.energy && c.energy[k], x = sgCmpDelta(cur, ref, k);
    if (ref != null) extra.push(`${c.label}: ${sgNum(ref, unit, kwhDec)} ${unit}` + (x ? ` (${x.v >= 0 ? '↑' : '↓'} ${x.txt})` : ''));
    if (i === 0 && x && Math.abs(x.v) >= 0.5) {
      const g = SG_CMP_GOOD[k] * Math.sign(x.v), cls = g > 0 ? 'good' : g < 0 ? 'bad' : '';
      badge = `<span class="dl ${cls}" title="${esc(c.label)}">${x.v >= 0 ? '↑' : '↓'}${x.txt}</span>`;
    }
  });
  return { badge, extra };
}

// przeplyw energii (Sankey) za okres: F - energy.flows z /api/period, W - szerokosc [px], col - {pv, bat, grid, load, loss}
// zwraca SVG albo '' (brak danych); klasy sk-link / sk-anim / sk-n / sk-v / sk-u / sk-p - style po stronie strony i kart
function sgSankeySvg(F, W, col) {
  const kw = v => v >= 100 ? v.toFx(0) : v >= 10 ? v.toFx(1) : v.toFx(2);
  const S = [{ id: 'pv', n: 'Słońce', c: col.pv }, { id: 'bat', n: 'Magazyn', c: col.bat }, { id: 'grid', n: 'Sieć', c: col.grid }];
  const T = [{ id: 'load', n: 'Dom', c: col.load }, { id: 'bat', n: 'Magazyn', c: col.bat }, { id: 'grid', n: 'Sieć', c: col.grid },
    { id: 'loss', n: 'Straty', c: col.loss }];
  const L = [['pv', 'load'], ['pv', 'bat'], ['pv', 'grid'], ['bat', 'load'], ['bat', 'grid'], ['grid', 'load'], ['grid', 'bat']]
    .map(([a, b]) => ({ a, b, v: +((F || {})[a + '_' + b] || 0) })).filter(x => x.v >= 0.005);
  const outS = id => L.filter(l => l.a === id).reduce((x, l) => x + l.v, 0);
  if ((F.loss || 0) >= 0.005) {
    const tot = S.reduce((x, n) => x + outS(n.id), 0) || 1;
    const lossBy = {}; S.forEach(n => { lossBy[n.id] = F.loss * outS(n.id) / tot; });
    S.forEach(n => { if (lossBy[n.id] >= 0.005) L.push({ a: n.id, b: 'loss', v: lossBy[n.id] }); });
  }
  const out = id => L.filter(l => l.a === id).reduce((x, l) => x + l.v, 0), inn = id => L.filter(l => l.b === id).reduce((x, l) => x + l.v, 0);
  const sN = S.filter(n => out(n.id) > 0), tN = T.filter(n => inn(n.id) > 0);
  const tot = Math.max(sN.reduce((x, n) => x + out(n.id), 0), tN.reduce((x, n) => x + inn(n.id), 0));
  if (!(tot > 0.01)) return '';
  W = Math.max(320, Math.round(W || 640));
  const narrow = W < 560, NW = Math.round(Math.min(150, Math.max(92, W * 0.22))), GAP = 8, MINH = 54, X0 = 0, X1 = W - NW;
  const H = narrow ? 380 : 420, tot2 = Math.max(sN.length, tN.length);
  const k = (H - GAP * (tot2 - 1) - MINH * tot2 * 0.25) / tot;
  const place = arr => { let y = 0; return arr.map(n => { const v = arr === sN ? out(n.id) : inn(n.id), h = Math.max(1, v * k), bh = Math.max(h, MINH), r = { ...n, y, h, bh };
    y += bh + GAP; return r; }); };
  const sP = place(sN), tP = place(tN);
  const sOff = {}, tOff = {}; sP.forEach(n => sOff[n.id] = n.y + (n.bh - n.h) / 2); tP.forEach(n => tOff[n.id] = n.y + (n.bh - n.h) / 2);
  const order = [...L].sort((p, q) => sP.findIndex(n => n.id === p.a) - sP.findIndex(n => n.id === q.a) || tP.findIndex(n => n.id === p.b) - tP.findIndex(n => n.id === q.b));
  const srcTot = sN.reduce((x, n) => x + out(n.id), 0);
  const pct = v => srcTot > 0 ? (100 * v / srcTot).toFx(v / srcTot < 0.1 ? 1 : 0) + ' %' : '';
  let g = '', anim = '';
  order.forEach(l => {
    const sn = sP.find(n => n.id === l.a), tn = tP.find(n => n.id === l.b), h = Math.max(1, l.v * k);
    const y0 = sOff[l.a] + h / 2, y1 = tOff[l.b] + h / 2; sOff[l.a] += h; tOff[l.b] += h;
    const xa = X0 + NW, xb = X1, xm = (xa + xb) / 2, d = `M${xa},${y0} C${xm},${y0} ${xm},${y1} ${xb},${y1}`;
    g += `<path class="sk-link" d="${d}" stroke="${sn.c}" stroke-width="${h}"><title>${sn.n} → ${tn.n}: ${kw(l.v)} kWh (${pct(l.v)})</title></path>`;
    if (h >= 1.5) anim += `<path class="sk-anim" d="${d}" stroke-width="${Math.min(Math.max(h * 0.5, 2), 4).toFixed(1)}" style="animation-duration:${(0.9 + 1.2 * Math.random()).toFixed(2)}s"/>`;
  });
  const node = (n, x, val) => {
    const cx = x + NW / 2, my = n.y + n.bh / 2, tall = n.bh >= 78;
    return `<rect x="${x}" y="${n.y}" width="${NW}" height="${n.bh}" rx="8" fill="${n.c}"><title>${n.n}: ${kw(val)} kWh</title></rect>` +
      `<text class="sk-n" x="${cx}" y="${tall ? my - 16 : my - 7}" text-anchor="middle">${n.n.toUpperCase()}</text>` +
      `<text class="sk-v" x="${cx}" y="${tall ? my + 6 : my + 12}" text-anchor="middle">${kw(val)}<tspan class="sk-u"> kWh</tspan></text>` +
      (tall ? `<text class="sk-p" x="${cx}" y="${my + 24}" text-anchor="middle">${pct(val)}</text>` : '');
  };
  g += anim + sP.map(n => node(n, X0, out(n.id))).join('') + tP.map(n => node(n, X1, inn(n.id))).join('');
  const hh = Math.max(...[...sP, ...tP].map(n => n.y + n.bh));
  return `<svg viewBox="0 0 ${W} ${hh}" style="width:100%;height:auto;display:block" role="img" aria-label="Przepływ energii">${g}</svg>`;
}
