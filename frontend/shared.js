/* ═══════════════════════════════════════════════════════════════════════
   TrendPlus shared.js — code used by BOTH the Screener and the Portfolio.

   Exports (on window):
     TP_SECTOR_MAP  — raw NSE industry string → broad sector group
     tpNormSector   — helper applying the map
     TPCharts       — tiny dependency-free canvas line/bar renderer
     TPDP           — the ONE universal stock Detail Panel (self-injecting)

   TPDP usage:
     TPDP.init({
       api: 'http://localhost:8000',
       getToken: () => AUTH_TOKEN,            // or () => ''
       headerExtra: sym => '<html>',          // optional (e.g. watchlist star)
       owned: sym => holdingObjOrNull,        // optional portfolio overlay
       onAction: (type, holding) => {},       // 'buy' | 'sell' from overlay
     });
     TPDP.open('INFY');  TPDP.close();
   ═══════════════════════════════════════════════════════════════════════ */
'use strict';

window.TP_SECTOR_MAP = {
  "Agri - Animal Feed":"Agriculture","Agri - Edible Oil":"Agriculture","Agri - Misc":"Agriculture",
  "Agri - Rice":"Agriculture","Agri - Sugar":"Agriculture","Agri - Tea/Coffee":"Agriculture",
  "Agrichem - Fertilizers":"Agriculture","Agrichem - Protection":"Agriculture",
  "Auto - 2W":"Auto & Ancillaries","Auto - CV":"Auto & Ancillaries","Auto - Misc":"Auto & Ancillaries",
  "Auto - OEM Suppliers":"Auto & Ancillaries","Auto - PV/Trucks":"Auto & Ancillaries",
  "Auto - Replacement":"Auto & Ancillaries","Auto - Tyres":"Auto & Ancillaries",
  "Automobile and Auto Components":"Auto & Ancillaries",
  "Financials - Private Bank":"Banking","Financials - PSU Bank":"Banking","Financials - Housing":"Banking",
  "Engineering":"Capital Goods","Electronic - Equipment":"Capital Goods","Industrial - Bearings":"Capital Goods",
  "Industrial - Consumables":"Capital Goods","Industrial - Pumps/Engines":"Capital Goods",
  "Chemicals - Bulk":"Chemicals","Chemicals - Misc":"Chemicals","Chemicals - Petro":"Chemicals","Chemicals - Specialty":"Chemicals",
  "Construction Materials":"Construction","Infra - Cement":"Construction","Infra - Ceramics":"Construction",
  "Infra - Construction":"Construction","Infra - Diversified":"Construction","Infra - Granites/Marbles":"Construction","Infra - Plastic Pipes":"Construction",
  "Consumption - Appliances":"Consumer Durables","Consumption - Electronics":"Consumer Durables","Consumption - Jewelry":"Consumer Durables",
  "Consumption - Misc":"Consumer Durables","Consumption - Paints":"Consumer Durables","Consumption - Retail":"Consumer Durables",
  "Leisure - Hotels":"Consumer Services","Leisure - Misc":"Consumer Services","Human Resources":"Consumer Services","Services":"Consumer Services",
  "Defense - Misc":"Defence","Defense - Ship Building":"Defence","Defense - Technology":"Defence",
  "Others":"Diversified",
  "Fast Moving Consumer Goods":"FMCG","Beverages - Alcohol":"FMCG","Consumption - FMCG":"FMCG","Consumption - Dairy":"FMCG","Consumption - Personal":"FMCG","Forest Materials":"FMCG",
  "Financials - AMC":"Financial Services","Financials - Broking":"Financial Services","Financials - Life":"Financial Services","Financials - MFI":"Financial Services",
  "Financials - Misc":"Financial Services","Financials - NBFC":"Financial Services","Financials - Non-Life":"Financial Services","Financials - Ratings":"Financial Services",
  "Healthcare":"Healthcare & Pharma","Healthcare - Biotech":"Healthcare & Pharma","Healthcare - Hospitals":"Healthcare & Pharma","Healthcare - Pharma":"Healthcare & Pharma",
  "IT - Equipments":"Information Technology","IT - Product/Platform":"Information Technology","IT - Services":"Information Technology","IT - Software/Consulting":"Information Technology",
  "Media - Films":"Media & Entertainment","Media - Publishing":"Media & Entertainment","Media - TV Broadcast":"Media & Entertainment","Media Entertainment & Publication":"Media & Entertainment",
  "Metals - Aluminium":"Metals & Mining","Metals - Casting/Forging":"Metals & Mining","Metals - Copper":"Metals & Mining","Metals - Iron & Steel":"Metals & Mining",
  "Metals - Misc":"Metals & Mining","Metals - Pipe & Tube":"Metals & Mining","Mining - Coal":"Metals & Mining",
  "Oil and Gas":"Oil, Gas & Energy","Oil Gas & Consumable Fuels":"Oil, Gas & Energy","Petro - Refineries":"Oil, Gas & Energy",
  "Paper":"Paper & Packaging","Packaging":"Paper & Packaging",
  "Power - Equipment":"Power","Power - Misc":"Power","Power - Wiring":"Power","Utilities":"Power","Utility - Gas":"Power","Utility - Misc":"Power",
  "Realty":"Real Estate","Telecommunication":"Telecom",
  "Textile - Apparels":"Textiles","Textile - Misc":"Textiles","Textile - Spinning/Yarn":"Textiles","Textiles - Polyester":"Textiles","Leather Products":"Textiles",
  "Transport - Logistics":"Transport & Logistics","Transport - Shipping":"Transport & Logistics",
};
window.tpNormSector = function (s) { s = (s || '').trim(); return window.TP_SECTOR_MAP[s] || s; };

/* ── TPCharts: minimal canvas renderer (no Chart.js dependency) ───────── */
window.TPCharts = (function () {
  function prep(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || canvas.width, h = canvas.clientHeight || canvas.height;
    canvas.width = w * dpr; canvas.height = h * dpr;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  function line(canvas, values, opts = {}) {
    const { ctx, w, h } = prep(canvas);
    const vals = (values || []).filter(v => v != null && !isNaN(v)).map(Number);
    if (vals.length < 2) return;
    const pad = opts.pad != null ? opts.pad : 2;
    const min = Math.min(...vals), max = Math.max(...vals);
    const span = (max - min) || 1;
    const x = i => pad + (w - 2 * pad) * i / (vals.length - 1);
    const y = v => h - pad - (h - 2 * pad) * (v - min) / span;
    const up = vals[vals.length - 1] >= vals[0];
    const color = opts.color || (up ? '#10b981' : '#ef4444');

    if (opts.baseline != null && opts.baseline >= min && opts.baseline <= max) {
      ctx.strokeStyle = 'rgba(160,170,190,0.35)'; ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(pad, y(opts.baseline)); ctx.lineTo(w - pad, y(opts.baseline)); ctx.stroke();
      ctx.setLineDash([]);
    }
    if (opts.fill !== false) {
      const grad = ctx.createLinearGradient(0, 0, 0, h);
      grad.addColorStop(0, color + '33'); grad.addColorStop(1, color + '00');
      ctx.fillStyle = grad;
      ctx.beginPath(); ctx.moveTo(x(0), y(vals[0]));
      vals.forEach((v, i) => ctx.lineTo(x(i), y(v)));
      ctx.lineTo(x(vals.length - 1), h); ctx.lineTo(x(0), h); ctx.closePath(); ctx.fill();
    }
    ctx.strokeStyle = color; ctx.lineWidth = opts.lineWidth || 1.5;
    ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    ctx.beginPath(); ctx.moveTo(x(0), y(vals[0]));
    vals.forEach((v, i) => ctx.lineTo(x(i), y(v)));
    ctx.stroke();
    return { min, max, up };
  }

  // Multiple series on one scale: [{values, color, dash, fill}]
  function lines(canvas, seriesArr, opts = {}) {
    const { ctx, w, h } = prep(canvas);
    const all = [];
    seriesArr.forEach(s => (s.values || []).forEach(v => { if (v != null && !isNaN(v)) all.push(Number(v)); }));
    if (all.length < 2) return;
    const pad = opts.pad != null ? opts.pad : 4;
    const min = Math.min(...all), max = Math.max(...all);
    const span = (max - min) || 1;
    seriesArr.forEach(s => {
      const vals = (s.values || []).map(v => (v == null || isNaN(v)) ? null : Number(v));
      const n = vals.length;
      if (n < 2) return;
      const x = i => pad + (w - 2 * pad) * i / (n - 1);
      const y = v => h - pad - (h - 2 * pad) * (v - min) / span;
      if (s.fill) {
        const grad = ctx.createLinearGradient(0, 0, 0, h);
        grad.addColorStop(0, s.color + '2e'); grad.addColorStop(1, s.color + '00');
        ctx.fillStyle = grad;
        ctx.beginPath();
        let started = false;
        vals.forEach((v, i) => { if (v == null) return; started ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)); started = true; });
        ctx.lineTo(x(n - 1), h); ctx.lineTo(x(0), h); ctx.closePath(); ctx.fill();
      }
      ctx.strokeStyle = s.color; ctx.lineWidth = s.width || 1.5;
      ctx.setLineDash(s.dash || []);
      ctx.lineJoin = 'round'; ctx.lineCap = 'round';
      ctx.beginPath();
      let started = false;
      vals.forEach((v, i) => { if (v == null) return; started ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)); started = true; });
      ctx.stroke();
      ctx.setLineDash([]);
    });
    return { min, max };
  }

  function bars(canvas, values, opts = {}) {
    const { ctx, w, h } = prep(canvas);
    const vals = (values || []).map(v => (v == null || isNaN(v)) ? 0 : Number(v));
    if (!vals.length) return;
    const maxAbs = Math.max(...vals.map(Math.abs)) || 1;
    const zero = h / 2;
    const bw = Math.max(1, (w / vals.length) - 1);
    vals.forEach((v, i) => {
      const bh = Math.abs(v) / maxAbs * (h / 2 - 2);
      ctx.fillStyle = v >= 0 ? (opts.pos || '#10b981') : (opts.neg || '#ef4444');
      ctx.fillRect(i * (w / vals.length), v >= 0 ? zero - bh : zero, bw, Math.max(bh, 1));
    });
    ctx.strokeStyle = 'rgba(160,170,190,0.3)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, zero); ctx.lineTo(w, zero); ctx.stroke();
  }

  return { line, lines, bars };
})();

/* ── TPCharts interactive extension: hover crosshair + tooltip + markers ── */
(function () {
  function tipEl() {
    let el = document.getElementById('tp-charttip');
    if (!el) {
      el = document.createElement('div');
      el.id = 'tp-charttip';
      el.style.cssText = 'position:fixed;z-index:9999;pointer-events:none;background:var(--bg3,#27272a);border:1px solid var(--border2,#3f3f46);border-radius:6px;padding:6px 9px;font:11px/1.5 "JetBrains Mono",monospace;color:var(--t0,#fff);box-shadow:0 8px 24px rgba(0,0,0,0.5);display:none;white-space:nowrap';
      document.body.appendChild(el);
    }
    return el;
  }

  // Draws a line chart and makes it hoverable. opts:
  //   values (req), dates, baseline, markers:[{i,kind:'buy'|'sell'|'start'}],
  //   fill, lineWidth, color, tooltip(i)->html
  TPCharts.interactiveLine = function (canvas, opts) {
    const vals = (opts.values || []).map(Number);
    if (vals.length < 2) { TPCharts.line(canvas, vals, opts); return; }
    const pad = 3;
    canvas.__tpil = { opts, vals, pad };

    function geometry() {
      const w = canvas.clientWidth || canvas.width, h = canvas.clientHeight || canvas.height;
      let min = Math.min(...vals), max = Math.max(...vals);
      if (opts.baseline != null) { min = Math.min(min, opts.baseline); max = Math.max(max, opts.baseline); }
      const span = (max - min) || 1;
      return { w, h,
        x: i => pad + (w - 2 * pad) * i / (vals.length - 1),
        y: v => h - pad - (h - 2 * pad) * (v - min) / span };
    }

    function base(hoverI) {
      TPCharts.line(canvas, vals, Object.assign({}, opts, { pad }));
      const g = geometry();
      const ctx = canvas.getContext('2d');
      const MK = { buy: '#3b82f6', sell: '#f59e0b', start: '#a1a1aa' };
      (opts.markers || []).forEach(m => {
        if (m.i == null || m.i < 0 || m.i >= vals.length) return;
        ctx.beginPath();
        ctx.arc(g.x(m.i), g.y(vals[m.i]), m.kind === 'start' ? 2.5 : 3, 0, Math.PI * 2);
        ctx.fillStyle = MK[m.kind] || '#fff';
        ctx.fill();
        if (m.kind === 'start') { ctx.strokeStyle = '#fff'; ctx.lineWidth = 1; ctx.stroke(); }
      });
      if (hoverI != null) {
        ctx.strokeStyle = 'rgba(255,255,255,0.35)'; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(g.x(hoverI), 0); ctx.lineTo(g.x(hoverI), g.h); ctx.stroke();
        ctx.beginPath(); ctx.arc(g.x(hoverI), g.y(vals[hoverI]), 3.2, 0, Math.PI * 2);
        ctx.fillStyle = '#fff'; ctx.fill();
      }
    }
    base(null);

    if (!canvas.__tpilBound) {
      canvas.__tpilBound = true;
      canvas.addEventListener('mousemove', ev => {
        const st = canvas.__tpil; if (!st) return;
        const rect = canvas.getBoundingClientRect();
        const n = st.vals.length;
        const i = Math.max(0, Math.min(n - 1,
          Math.round((ev.clientX - rect.left - st.pad) / (rect.width - 2 * st.pad) * (n - 1))));
        base(i);
        const tip = tipEl();
        tip.innerHTML = st.opts.tooltip ? st.opts.tooltip(i)
          : `${st.opts.dates ? st.opts.dates[i] + '<br>' : ''}${st.vals[i]}`;
        tip.style.display = 'block';
        const tw = tip.offsetWidth;
        tip.style.left = Math.min(window.innerWidth - tw - 8, ev.clientX + 12) + 'px';
        tip.style.top = (ev.clientY - 34) + 'px';
      });
      canvas.addEventListener('mouseleave', () => { base(null); tipEl().style.display = 'none'; });
    }
  };
})();

/* ── TPDP: the universal Detail Panel (tabbed, Bloomberg-style) ───────── */
window.TPDP = (function () {
  let cfg = { api: '', getToken: () => '', headerExtra: null, owned: null, onAction: null };
  let currentSym = null;
  let state = {};              // per-open-symbol scratch: {d, price, txns}
  let tf = localStorage.getItem('tpdp_tf') || '1M';
  let tab = localStorage.getItem('tpdp_tab') || 'overview';
  const TF_SESSIONS = { '12D': 12, '1M': 22, '6M': 126, '1Y': 252 };
  const TABS = [
    ['overview', 'Overview'], ['technical', 'Technical'], ['financials', 'Financials'],
    ['portfolio', 'Portfolio'], ['news', 'News'], ['ai', 'AI'], ['txns', 'Trades'],
  ];

  const f1 = v => (v == null || isNaN(v)) ? '—' : Number(v).toFixed(1);
  const f2 = v => (v == null || isNaN(v)) ? '—' : Number(v).toFixed(2);
  const fPct = v => (v == null || isNaN(v)) ? '—' : (v >= 0 ? '+' : '') + Number(v).toFixed(2) + '%';
  const inr = v => (v == null || isNaN(v)) ? '—' : '₹' + Number(v).toLocaleString('en-IN', { maximumFractionDigits: 2 });
  const inr0 = v => (v == null || isNaN(v)) ? '—' : '₹' + Number(v).toLocaleString('en-IN', { maximumFractionDigits: 0 });
  const fNum = v => (v == null || isNaN(v)) ? '—' : Number(v).toLocaleString('en-IN');
  const pCls = v => (v == null || isNaN(v)) ? 'neu' : Number(v) > 0 ? 'pos' : Number(v) < 0 ? 'neg' : 'neu';
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const VERDICT_CLASS = { HOLD: 'action-HOLD', TRIM: 'action-TRIM', ADD_MORE: 'action-ADD_MORE', EXIT: 'action-EXIT_ALL' };
  const BREAKDOWN_LABELS = { momentum: 'Momentum', relative_strength: 'Relative strength', trend: 'Trend strength',
    volume: 'Volume quality', price_structure: 'Price structure', drawdown_risk: 'Drawdown risk',
    near_high: '52-wk proximity', sector: 'Sector strength' };

  function tpill(d) {
    if (d == null) return '—';
    const n = Number(d);
    return `<span class="tp ${n >= 9 ? 'tp-hi' : n >= 5 ? 'tp-med' : 'tp-lo'}">${n}/12</span>`;
  }
  function mbar(s) {
    if (s == null || isNaN(s)) return '—';
    const n = Math.round(Number(s));
    const col = n >= 70 ? '#10b981' : n >= 40 ? '#f59e0b' : '#ef4444';
    return `<div class="mbar"><span class="mono" style="font-size:11.5px;font-weight:700;color:${col};min-width:22px">${n}</span><div class="mbar-bg"><div class="mbar-fg" style="width:${n}%;background:${col}"></div></div></div>`;
  }
  function emaBadge(sig) {
    if (!sig) return '—';
    const map = { golden_cross: ['ema-gc', 'Golden ✓'], above_200: ['ema-a2', 'Above 200'], approaching: ['ema-ap', 'Near 200'] };
    const [cls, label] = map[sig] || ['', sig];
    return `<span class="ema ${cls}">${label}</span>`;
  }
  function macdCell(hist) {
    if (hist == null || isNaN(hist)) return '—';
    const n = Number(hist);
    return `<span class="${n > 0 ? 'macd-bull' : 'macd-bear'}">${n > 0 ? '▲' : '▼'} ${f2(n)}</span>`;
  }
  function capBadge(c) {
    if (!c) return '';
    const k = c === 'Large Cap' ? 'L' : c === 'Mid Cap' ? 'M' : 'S';
    return `<span class="cap cap-${k}">${c.replace(' Cap', '')}</span>`;
  }
  function mboxes(pairs) {
    return `<div class="metric-grid">${pairs.map(([l, v, span2]) =>
      `<div class="mbox${span2 ? ' span2' : ''}"><div class="mbox-l">${l}</div><div class="mbox-v">${v}</div></div>`).join('')}</div>`;
  }

  async function api(path, options = {}) {
    const headers = Object.assign({}, options.headers || {});
    const token = cfg.getToken && cfg.getToken();
    if (token) headers.Authorization = 'Bearer ' + token;
    if (options.body) headers['Content-Type'] = 'application/json';
    const r = await fetch(cfg.api + path, Object.assign({}, options, { headers }));
    if (!r.ok) { const t = await r.text().catch(() => r.statusText); throw new Error(`${r.status}: ${t}`); }
    return r.json();
  }

  const CSS = `
.dp{position:fixed;right:0;top:0;bottom:0;width:560px;max-width:96vw;background:var(--bg1);border-left:1px solid var(--border);box-shadow:-4px 0 24px rgba(0,0,0,0.4);transform:translateX(100%);transition:transform .24s cubic-bezier(0.4,0,0.2,1);z-index:600;overflow-y:auto;display:flex;flex-direction:column}
@media (prefers-reduced-motion: reduce){.dp{transition:none}}
.dp.open{transform:translateX(0)}
.dp-hdr{padding:14px 18px 0;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg1);z-index:10}
.dp-hdr-row{display:flex;align-items:flex-start;gap:12px}
.dp-hdr-info{flex:1;min-width:0}
.dp-sym{font-family:var(--font-mono);font-size:21px;font-weight:700;color:var(--t0);display:inline-flex;align-items:center;gap:10px;text-decoration:none;cursor:pointer}
.dp-sym:hover{color:var(--blue)}
.dp-name{font-size:12px;color:var(--t1);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:500}
.dp-price-row{display:flex;align-items:baseline;gap:10px;margin-top:5px}
.dp-price{font-family:var(--font-mono);font-size:22px;font-weight:700;color:var(--t0)}
.dp-close{background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--t1);padding:5px 11px;font-size:13px;flex-shrink:0;cursor:pointer}
.dp-close:hover{color:var(--t0);border-color:var(--t2)}
.dp-ownstrip{display:flex;gap:14px;font:11px var(--font-mono);color:var(--t2);margin-top:6px;flex-wrap:wrap}
.dp-ownstrip b{color:var(--t0)}
.dp-tabs{display:flex;gap:2px;margin-top:10px;overflow-x:auto}
.dp-tabs button{font-size:11px;font-weight:600;color:var(--t2);padding:7px 11px;border-bottom:2px solid transparent;white-space:nowrap;transition:color .12s}
.dp-tabs button:hover{color:var(--t0)}
.dp-tabs button.active{color:var(--t0);border-bottom-color:var(--blue)}
.dp-body{padding:14px 18px;flex:1}
.dp-sec-ttl{font-size:10.5px;font-weight:700;color:var(--t2);text-transform:uppercase;letter-spacing:1px;margin:16px 0 10px;display:flex;align-items:center;justify-content:space-between;gap:8px}
.dp-sec-ttl:first-child{margin-top:0}
.dp .metric-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.dp .mbox{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:9px 11px}
.dp .mbox.span2{grid-column:span 2}
.dp .mbox-l{font-size:10px;color:var(--t2);margin-bottom:4px;font-weight:500}
.dp .mbox-v{font-family:var(--font-mono);font-size:14.5px;font-weight:700;color:var(--t0)}
.dp .mat-detail{display:flex;gap:4px;flex-wrap:wrap}
.dp .mdc,.dp .mdc-dg{width:36px;height:44px;border-radius:6px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px}
.dp .mdc.t{background:var(--green-bg);border:1px solid rgba(16,185,129,0.3)}
.dp .mdc.f{background:var(--red-bg);border:1px solid rgba(239,68,68,0.3)}
.dp .mdc.n{background:var(--bg2);border:1px solid var(--border)}
.dp .mdc-dg{background:#065f46;border:1px solid #10b981}
.dp .mdc-date{font-size:9px;color:var(--t2);font-weight:600}
.dp .mdc-dg .mdc-date{color:#a7f3d0}
.dp .mdc-val{font-size:11px;font-weight:700;font-family:var(--font-mono)}
.dp .mdc.t .mdc-val{color:var(--green)}.dp .mdc.f .mdc-val{color:var(--red)}.dp .mdc-dg .mdc-val{color:#fff}
.dp .peers-tbl{width:100%;border-collapse:collapse;font-size:12px}
.dp .peers-tbl th{color:var(--t2);font-weight:600;padding:6px 8px;text-align:right;border-bottom:1px solid var(--border);text-transform:uppercase;font-size:10px;letter-spacing:0.5px}
.dp .peers-tbl th:first-child{text-align:left}
.dp .peers-tbl td{padding:6px 8px;text-align:right;border-bottom:1px solid var(--border);color:var(--t1)}
.dp .peers-tbl td:first-child{text-align:left;font-family:var(--font-mono);font-weight:700;color:var(--blue);cursor:pointer}
.dp .peers-tbl tr:hover td{background:var(--bg2)}
.dp .peers-tbl tr.self td{background:var(--bg3)}
.dp .tp{font-family:var(--font-mono);font-weight:700;font-size:12px;border-radius:5px;padding:2px 8px}
.dp .tp-hi{color:#10b981;background:rgba(16,185,129,0.15)}
.dp .tp-med{color:#f59e0b;background:rgba(245,158,11,0.15)}
.dp .tp-lo{color:var(--t2);background:var(--bg3,#27272a)}
.dp .mbar{display:flex;align-items:center;gap:8px}
.dp .mbar-bg{flex:1;height:5px;background:var(--bg3,#27272a);border-radius:99px;overflow:hidden;min-width:48px}
.dp .mbar-fg{height:100%;border-radius:99px}
.dp .ema{font-size:10.5px;font-weight:700;border-radius:4px;padding:3px 7px;white-space:nowrap}
.dp .ema-gc{color:#10b981;background:rgba(16,185,129,0.15)}
.dp .ema-a2{color:#3b82f6;background:rgba(59,130,246,0.15)}
.dp .ema-ap{color:#f59e0b;background:rgba(245,158,11,0.15)}
.dp .macd-bull{color:#10b981;font-family:var(--font-mono);font-weight:700;font-size:12px}
.dp .macd-bear{color:#ef4444;font-family:var(--font-mono);font-weight:700;font-size:12px}
.dp .cap{font-size:10px;font-weight:700;border-radius:4px;padding:2px 6px;margin-left:6px}
.dp .cap-L{color:#3b82f6;background:rgba(59,130,246,0.15)}
.dp .cap-M{color:#a855f7;background:rgba(168,85,247,0.15)}
.dp .cap-S{color:#f59e0b;background:rgba(245,158,11,0.15)}
.dp .ld{display:flex;align-items:center;gap:10px;color:var(--t2);font-size:13px;padding:12px 0}
.dp .spin{width:14px;height:14px;border:2px solid var(--bg3,#27272a);border-top-color:var(--blue);border-radius:50%;animation:tpdp-spin .7s linear infinite}
@keyframes tpdp-spin{to{transform:rotate(360deg)}}
.dp .pos{color:var(--green)}.dp .neg{color:var(--red)}.dp .neu{color:var(--t2)}
.dp .mono{font-family:var(--font-mono);font-variant-numeric:tabular-nums}
.dp-tf{display:flex;gap:4px}
.dp-tf button{font-size:10.5px;font-weight:700;font-family:var(--font-mono);color:var(--t2);border:1px solid var(--border);border-radius:5px;padding:3px 8px;cursor:pointer;background:transparent;letter-spacing:0;text-transform:none}
.dp-tf button.active{color:#fff;background:var(--blue-d,#2563eb);border-color:var(--blue-d,#2563eb)}
.dp .action-badge{font-weight:700;font-size:11px;display:inline-flex;border-radius:4px;padding:3px 8px;white-space:nowrap}
.dp .action-HOLD{color:#3b82f6;background:rgba(59,130,246,0.15)}
.dp .action-TRIM{color:#f59e0b;background:rgba(245,158,11,0.15)}
.dp .action-ADD_MORE{color:#10b981;background:rgba(16,185,129,0.15)}
.dp .action-EXIT_ALL{color:#ef4444;background:rgba(239,68,68,0.15)}
.dp .risk-badge{font-weight:600;font-size:10.5px;display:inline-flex;border-radius:4px;padding:2px 7px;border:1px solid var(--border)}
.dp .risk-Low{color:#10b981;border-color:rgba(16,185,129,0.3)}
.dp .risk-Moderate{color:#3b82f6;border-color:rgba(59,130,246,0.3)}
.dp .risk-Elevated{color:#f59e0b;border-color:rgba(245,158,11,0.3)}
.dp .risk-High{color:#ef4444;border-color:rgba(239,68,68,0.3)}
.dp .vreasons{margin:8px 0 0;padding-left:16px;font-size:12px;color:var(--t2);line-height:1.55}
.dp .txnrow{display:flex;justify-content:space-between;gap:10px;font-size:12px;padding:7px 0;border-bottom:1px dashed var(--border);color:var(--t1)}
.dp .txnrow:last-child{border-bottom:none}
.dp .txn-BUY{color:#10b981;font-weight:700}.dp .txn-SELL{color:#ef4444;font-weight:700}
.dp-notes{width:100%;background:var(--bg0,#09090b);border:1px solid var(--border);border-radius:6px;color:var(--t0);font-size:12.5px;font-family:var(--font);padding:8px 10px;min-height:64px;resize:vertical}
.dp-notes-save{margin-top:6px;font-size:11.5px;font-weight:700;color:var(--blue);cursor:pointer;background:none;border:none;padding:0}
.dp-own-actions{display:flex;gap:8px;margin-top:12px}
.dp-own-actions button{flex:1;font-size:12px;font-weight:700;padding:7px 0;border-radius:6px;cursor:pointer;border:1px solid var(--border);background:var(--bg2);color:var(--t0)}
.dp-own-actions .dp-buy:hover{border-color:#10b981;color:#10b981}
.dp-own-actions .dp-sell:hover{border-color:#ef4444;color:#ef4444}
.dp-links{display:flex;flex-direction:column;gap:8px;margin-top:8px}
.dp-links a{display:flex;justify-content:space-between;align-items:center;background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-size:12.5px;color:var(--t0);text-decoration:none;font-weight:600}
.dp-links a:hover{border-color:var(--blue)}
.dp-links a span{color:var(--t3);font-weight:400;font-size:11.5px}
.dp-note{font-size:12px;color:var(--t3);line-height:1.6}
.dp-mkr{display:inline-flex;align-items:center;gap:5px;font-size:10.5px;color:var(--t3);margin-right:10px}
.dp-mkr i{width:7px;height:7px;border-radius:50%;display:inline-block}
`;

  const MARKUP = `
  <div class="dp-hdr">
    <div class="dp-hdr-row">
      <div class="dp-hdr-info">
        <a class="dp-sym" id="dpSym" target="_blank" rel="noopener" title="Open in TradingView">—</a>
        <div class="dp-name" id="dpName"></div>
        <div class="dp-price-row"><span class="dp-price" id="dpPrice"></span><span id="dpChg" class="mono" style="font-size:14px"></span></div>
        <div id="dpMeta" style="font-size:11.5px;color:var(--t2);margin-top:4px"></div>
        <div class="dp-ownstrip" id="dpOwnStrip"></div>
      </div>
      <button class="dp-close" id="dpCloseBtn" aria-label="Close panel">✕</button>
    </div>
    <div class="dp-tabs" id="dpTabs"></div>
  </div>
  <div class="dp-body" id="dpBody"><div class="ld"><div class="spin"></div> Loading…</div></div>`;

  function ensureDom() {
    if (document.getElementById('dp')) return;
    const style = document.createElement('style');
    style.id = 'tpdp-styles';
    style.textContent = CSS;
    document.head.appendChild(style);
    const el = document.createElement('div');
    el.className = 'dp'; el.id = 'dp';
    el.innerHTML = MARKUP;
    document.body.appendChild(el);
    el.querySelector('#dpCloseBtn').addEventListener('click', close);
  }

  function renderTabs() {
    document.getElementById('dpTabs').innerHTML = TABS.map(([k, l]) =>
      `<button data-tab="${k}" class="${k === tab ? 'active' : ''}">${l}</button>`).join('');
    document.querySelectorAll('#dpTabs button').forEach(b => b.addEventListener('click', () => {
      tab = b.dataset.tab;
      localStorage.setItem('tpdp_tab', tab);
      renderTabs(); renderBody();
    }));
  }

  function owned() { return cfg.owned ? cfg.owned(currentSym) : null; }

  function header(d) {
    const info = (d && d.info) || {};
    const lat = (d && d.latest) || {};
    const symEl = document.getElementById('dpSym');
    symEl.textContent = currentSym;
    symEl.href = 'https://www.tradingview.com/chart/?symbol=NSE%3A' + encodeURIComponent(currentSym);
    document.getElementById('dpName').textContent = info.company_name || '';
    document.getElementById('dpPrice').textContent = lat.close_price != null ? inr(lat.close_price) : '—';
    const chgEl = document.getElementById('dpChg');
    chgEl.textContent = fPct(lat.chg_1d);
    chgEl.className = 'mono ' + pCls(lat.chg_1d);
    document.getElementById('dpMeta').innerHTML =
      `${cfg.headerExtra ? cfg.headerExtra(currentSym) : ''}<span style="font-weight:600;color:var(--t1)">${esc(info.sector || '')}</span> ${capBadge(info.cap_category)}`;
    const h = owned();
    document.getElementById('dpOwnStrip').innerHTML = h
      ? `<span><b>${fNum(h.quantity)}</b> @ <b>${inr(h.avg_buy_price)}</b></span>` +
        `<span>P&L <b class="${pCls(h.unrealized_pnl)}">${inr0(h.unrealized_pnl)}</b> <b class="${pCls(h.gain_pct)}">${fPct(h.gain_pct)}</b></span>` +
        `<span>alloc <b>${h.portfolio_contribution != null ? f1(h.portfolio_contribution) + '%' : '—'}</b></span>` +
        (h.verdict ? `<span class="action-badge ${VERDICT_CLASS[h.verdict] || 'action-HOLD'}" style="align-self:center">${esc(h.verdict_label || h.verdict)}</span>` : '')
      : '';
  }

  /* ── tabs ─────────────────────────────────────────────────────── */

  function priceSlice() {
    const rows = state.price || [];
    return rows.slice(-TF_SESSIONS[tf]);
  }

  function drawOverviewChart() {
    const cv = document.getElementById('dpChart');
    if (!cv) return;
    const slice = priceSlice();
    const closes = slice.map(r => Number(r.close_price));
    const dates = slice.map(r => r.trade_date);
    const note = document.getElementById('dpChartNote');
    if (closes.length < 2) { if (note) note.textContent = 'Not enough price history.'; return; }
    const h = owned();
    const markers = [];
    if (h && state.txns) {
      const idx = Object.fromEntries(dates.map((d, i) => [d, i]));
      state.txns.forEach(t => {
        const i = idx[t.txn_date];
        if (i != null) markers.push({ i, kind: t.txn_type === 'BUY' ? 'buy' : 'sell' });
      });
      if (h.buy_date && idx[h.buy_date] != null) markers.push({ i: idx[h.buy_date], kind: 'start' });
    }
    TPCharts.interactiveLine(cv, {
      values: closes, dates, markers,
      baseline: h && h.avg_buy_price != null ? Number(h.avg_buy_price) : null,
      tooltip: i => {
        const mk = markers.filter(m => m.i === i).map(m => m.kind === 'buy' ? '▲ bought' : m.kind === 'sell' ? '▼ sold' : '● first buy');
        const vsCost = h && h.avg_buy_price ? `<br><span style="color:#a1a1aa">vs cost ${fPct((closes[i] - h.avg_buy_price) / h.avg_buy_price * 100)}</span>` : '';
        const day = i ? `<span class="${pCls(closes[i] - closes[i - 1])}">${fPct((closes[i] - closes[i - 1]) / closes[i - 1] * 100)}</span>` : '';
        return `${dates[i]}<br><b>${inr(closes[i])}</b> ${day}${vsCost}${mk.length ? '<br>' + mk.join(' · ') : ''}`;
      },
    });
    const chg = (closes[closes.length - 1] - closes[0]) / closes[0] * 100;
    if (note) note.innerHTML =
      `${dates[0]} → ${dates[dates.length - 1]} · <span class="${pCls(chg)} mono">${fPct(chg)}</span>` +
      (h ? ` <span class="dp-mkr" style="margin-left:10px"><i style="background:#3b82f6"></i>buy</span><span class="dp-mkr"><i style="background:#f59e0b"></i>sell</span><span class="dp-mkr"><i style="background:#a1a1aa;border:1px solid #fff"></i>first buy</span><span class="dp-mkr">┄ avg cost</span>` : '');
  }

  function tabOverview() {
    const lat = (state.d && state.d.latest) || {};
    return `
      <div class="dp-sec-ttl"><span>Price</span><span class="dp-tf" id="dpTf">${Object.keys(TF_SESSIONS).map(k =>
        `<button data-tf="${k}" class="${k === tf ? 'active' : ''}">${k}</button>`).join('')}</span></div>
      <canvas id="dpChart" style="width:100%;height:150px"></canvas>
      <div id="dpChartNote" style="font-size:11.5px;color:var(--t2);margin-top:6px"></div>
      <div class="dp-sec-ttl">Key metrics</div>
      ${mboxes([
        ['Trending days', tpill(lat.trending_days)],
        ['Momentum score', mbar(lat.momentum_score)],
        ['RS score', `<span style="color:var(--blue)">${f1(lat.rs_score)}</span>`],
        ['Weighted RPI', `<span style="color:#c084fc">${f1(lat.weighted_rpi)}</span>`],
        ['RSI 14', `<span class="${Number(lat.rsi_14) > 60 ? 'pos' : Number(lat.rsi_14) < 40 ? 'neg' : ''}">${f1(lat.rsi_14)}</span>`],
        ['ADX 14', f1(lat.adx_14)],
        ['12d change', `<span class="${pCls(lat.chg_12d)}">${fPct(lat.chg_12d)}</span>`],
        ['52W high dist.', `<span class="${pCls(lat.pct_from_high)}">${fPct(lat.pct_from_high)}</span>`],
      ])}`;
  }

  function tabTechnical() {
    const lat = (state.d && state.d.latest) || {};
    const pm = lat.pct_matrix || {}, bm = lat.bool_matrix || {};
    const keys = Object.keys(pm).length ? Object.keys(pm).slice(0, 12) : Object.keys(bm).slice(0, 12);
    const aboveEma = lat.ema_50 != null && lat.ema_200 != null && Number(lat.ema_50) > Number(lat.ema_200);
    const matrix = keys.length ? [...keys].sort().reverse().map(k => {
      const raw = pm[k]; const v = bm[k];
      const pv = raw != null ? ((Number(raw) >= 0 ? '+' : '') + Number(raw).toFixed(1) + '%') : '?';
      const cls = v === true ? (aboveEma ? 'mdc-dg' : 'mdc t') : v === false ? 'mdc f' : 'mdc n';
      return `<div class="${cls}"><div class="mdc-date">${k.slice(5)}</div><div class="mdc-val">${pv}</div></div>`;
    }).join('') : '<div class="dp-note">No 12-day matrix stored for this date.</div>';
    const peers = (state.d && state.d.peers) || [];
    return `
      <div class="dp-sec-ttl">MACD &amp; EMA</div>
      ${mboxes([
        ['MACD hist', macdCell(lat.macd_hist)],
        ['MACD line / signal', `<span style="font-size:12.5px">${f2(lat.macd_line)} / ${f2(lat.macd_signal)}</span>`],
        ['EMA 21', inr(lat.ema_21)],
        ['EMA 50', inr(lat.ema_50)],
        ['EMA 200', inr(lat.ema_200)],
        ['EMA signal', emaBadge(lat.ema_signal)],
        ['RSI 1D / 1W', `<span style="font-size:12.5px">${f1(lat.rsi_1d)} / ${f1(lat.rsi_1w)}</span>`],
        ['RPI 2W / 3M / 6M', `<span style="font-size:12.5px">${f1(lat.rpi_2w)} / ${f1(lat.rpi_3m)} / ${f1(lat.rpi_6m)}</span>`],
      ])}
      <div class="dp-sec-ttl">12-day matrix</div>
      <div class="mat-detail">${matrix}</div>
      <div class="dp-sec-ttl">Sector peers</div>
      ${peers.length ? `<table class="peers-tbl"><thead><tr><th>Symbol</th><th>12d%</th><th>RSI</th><th>EMA</th><th>Mom</th></tr></thead>
        <tbody>${peers.map(p => `
          <tr class="${p.symbol === currentSym ? 'self' : ''}">
            <td data-peer="${esc(p.symbol)}">${esc(p.symbol)}</td>
            <td class="${pCls(p.chg_12d)} mono" style="font-weight:600">${fPct(p.chg_12d)}</td>
            <td class="mono">${f1(p.rsi_14)}</td><td>${emaBadge(p.ema_signal)}</td><td>${mbar(p.momentum_score)}</td>
          </tr>`).join('')}</tbody></table>` : '<div class="dp-note">No peers found.</div>'}`;
  }

  function tabFinancials() {
    const info = (state.d && state.d.info) || {};
    const lat = (state.d && state.d.latest) || {};
    return `
      <div class="dp-sec-ttl">Market profile</div>
      ${mboxes([
        ['Market cap band', capBadge(info.cap_category) || '—'],
        ['Industry', `<span style="font-size:12px">${esc(info.sector || '—')}</span>`],
        ['52-week high', inr(lat.high_52w)],
        ['From 52W high', `<span class="${pCls(lat.pct_from_high)}">${fPct(lat.pct_from_high)}</span>`],
        ['Day volume', fNum(lat.volume)],
        ['Day trades', fNum(lat.total_trades)],
      ])}
      <div class="dp-sec-ttl">Fundamentals research</div>
      <div class="dp-note">TrendPlus doesn't ingest financial statements yet — these open the primary sources in a new tab.</div>
      <div class="dp-links">
        <a href="https://www.screener.in/company/${encodeURIComponent(currentSym)}/" target="_blank" rel="noopener">Screener.in <span>ratios, P&amp;L, balance sheet ↗</span></a>
        <a href="https://www.nseindia.com/get-quotes/equity?symbol=${encodeURIComponent(currentSym)}" target="_blank" rel="noopener">NSE quote page <span>filings &amp; corporate info ↗</span></a>
        <a href="https://www.tradingview.com/chart/?symbol=NSE%3A${encodeURIComponent(currentSym)}" target="_blank" rel="noopener">TradingView <span>full charting ↗</span></a>
      </div>`;
  }

  function tabPortfolio() {
    const h = owned();
    if (!h) return `<div class="dp-note" style="padding:8px 0">You don't hold ${esc(currentSym)}. Add it from the Holdings table to track it here.</div>`;
    const invested = (Number(h.quantity) || 0) * (Number(h.avg_buy_price) || 0);
    const rank = h.position_rank ? `${h.position_rank} of ${h.position_rank_total}` : '—';
    return `
      <div class="dp-sec-ttl">Your position</div>
      ${mboxes([
        ['Quantity', fNum(h.quantity)],
        ['Average cost', inr(h.avg_buy_price)],
        ['Invested', inr0(invested)],
        ['Current value', inr0(h.current_value)],
        ["Today's P&L", `<span class="${pCls(h.today_change)}">${h.today_change != null ? inr0(h.today_change) : '—'}</span>`],
        ['Overall P&L', `<span class="${pCls(h.unrealized_pnl)}">${inr0(h.unrealized_pnl)}</span>`],
        ['Return', `<span class="${pCls(h.gain_pct)}">${fPct(h.gain_pct)}</span>`],
        ['Allocation', h.portfolio_contribution != null ? f1(h.portfolio_contribution) + '%' : '—'],
        ['Holding period', (h.days_held ?? '—') + ' days'],
        ['Position rank', `<span style="font-size:12.5px">${rank}</span>`],
        ['Risk rating', h.risk_level ? `<span class="risk-badge risk-${h.risk_level}">${h.risk_level}</span>` : '—'],
        ['Status', `<span style="font-size:12px">${h.status === 'PARTIAL' ? 'Partial exit' : 'Active'}${h.realized_pnl ? ` · realized <span class="${pCls(h.realized_pnl)}">${inr0(h.realized_pnl)}</span>` : ''}</span>`],
      ])}
      <div class="dp-own-actions">
        <button class="dp-buy" id="dpBuyBtn">Buy more</button>
        <button class="dp-sell" id="dpSellBtn">Sell</button>
      </div>
      <div class="dp-sec-ttl">Notes</div>
      <textarea class="dp-notes" id="dpNotes" placeholder="Why you hold this, your target, your stop…">${esc(h.notes || '')}</textarea>
      <button class="dp-notes-save" id="dpNotesSave">Save note</button>`;
  }

  function tabNews() {
    const info = (state.d && state.d.info) || {};
    const q = encodeURIComponent((info.company_name || currentSym) + ' stock');
    return `
      <div class="dp-sec-ttl">News</div>
      <div class="dp-note">TrendPlus doesn't ingest a news feed yet — these searches open the latest coverage in a new tab.</div>
      <div class="dp-links">
        <a href="https://news.google.com/search?q=${q}" target="_blank" rel="noopener">Google News <span>latest coverage ↗</span></a>
        <a href="https://www.nseindia.com/get-quotes/equity?symbol=${encodeURIComponent(currentSym)}" target="_blank" rel="noopener">NSE announcements <span>exchange filings ↗</span></a>
        <a href="https://www.screener.in/company/${encodeURIComponent(currentSym)}/#announcements" target="_blank" rel="noopener">Screener.in announcements <span>curated filings ↗</span></a>
      </div>`;
  }

  function tabAI() {
    const h = owned();
    if (!h) return `<div class="dp-note" style="padding:8px 0">AI &amp; rule-engine analysis runs on your portfolio holdings. ${esc(currentSym)} isn't in your portfolio.</div>`;
    const b = h.score_breakdown || {};
    const bars = Object.keys(b).map(k => {
      const v = Number(b[k]) || 0;
      const col = v >= 70 ? '#10b981' : v >= 40 ? '#f59e0b' : '#ef4444';
      return `<div style="display:grid;grid-template-columns:120px 1fr 34px;gap:10px;align-items:center;font-size:11.5px;color:var(--t2);padding:2px 0">
        <span>${BREAKDOWN_LABELS[k] || k}</span>
        <span class="mbar-bg" style="display:block"><span class="mbar-fg" style="display:block;width:${v}%;background:${col}"></span></span>
        <span class="mono" style="text-align:right;color:var(--t1)">${Math.round(v)}</span></div>`;
    }).join('');
    return `
      <div class="dp-sec-ttl">Rule-engine verdict</div>
      <div>
        <span class="action-badge ${VERDICT_CLASS[h.verdict] || 'action-HOLD'}">${esc(h.verdict_label || 'Hold')}</span>
        <span style="font-size:11.5px;color:var(--t2);margin-left:8px">${h.verdict_confidence ?? '—'}% confidence${h.verdict_since ? ' · in effect since ' + h.verdict_since : ''}</span>
      </div>
      <ul class="vreasons">${(h.verdict_reasons || []).map(r => `<li>${esc(r.detail)}</li>`).join('') || '<li>No rules fired.</li>'}</ul>
      ${h.capital_flag_reason ? `<div class="dp-note" style="margin-top:8px">${esc(h.capital_flag_reason)}</div>` : ''}
      <div class="dp-sec-ttl">Position score — ${h.position_score ?? '—'}</div>
      ${bars || '<div class="dp-note">Score breakdown unavailable.</div>'}`;
  }

  function tabTxns() {
    const h = owned();
    if (!h) return `<div class="dp-note" style="padding:8px 0">No trades — ${esc(currentSym)} isn't in your portfolio.</div>`;
    if (!state.txns) return '<div class="ld"><div class="spin"></div> Loading ledger…</div>';
    if (!state.txns.length) return '<div class="dp-note">No transactions recorded.</div>';
    return `<div class="dp-sec-ttl">Transaction ledger</div>` + state.txns.map(t => `
      <div class="txnrow">
        <span><span class="txn-${t.txn_type}">${t.txn_type}</span> <span class="mono">${fNum(t.quantity)}</span> @ <span class="mono">${inr(t.price)}</span></span>
        <span style="color:var(--t3)">${t.txn_date}${t.charges ? ' · fee ' + inr(t.charges) : ''}${t.realized_pnl != null ? ` · <span class="${pCls(t.realized_pnl)} mono">${inr0(t.realized_pnl)}</span>` : ''}${t.notes ? ' · ' + esc(t.notes) : ''}</span>
      </div>`).join('');
  }

  function renderBody() {
    const body = document.getElementById('dpBody');
    if (!state.d) { body.innerHTML = '<div class="ld"><div class="spin"></div> Loading…</div>'; return; }
    const fns = { overview: tabOverview, technical: tabTechnical, financials: tabFinancials,
                  portfolio: tabPortfolio, news: tabNews, ai: tabAI, txns: tabTxns };
    body.innerHTML = (fns[tab] || tabOverview)();

    if (tab === 'overview') {
      drawOverviewChart();
      document.querySelectorAll('#dpTf button').forEach(b => b.addEventListener('click', () => {
        tf = b.dataset.tf;
        localStorage.setItem('tpdp_tf', tf);
        renderBody();
      }));
    }
    if (tab === 'technical') {
      document.querySelectorAll('#dpBody td[data-peer]').forEach(td =>
        td.addEventListener('click', () => open(td.dataset.peer)));
    }
    if (tab === 'portfolio') {
      const h = owned();
      const buyBtn = document.getElementById('dpBuyBtn');
      if (buyBtn && h) {
        buyBtn.addEventListener('click', () => cfg.onAction && cfg.onAction('buy', h));
        document.getElementById('dpSellBtn').addEventListener('click', () => cfg.onAction && cfg.onAction('sell', h));
        document.getElementById('dpNotesSave').addEventListener('click', async () => {
          const btn = document.getElementById('dpNotesSave');
          try {
            btn.textContent = 'Saving…';
            const notes = document.getElementById('dpNotes').value;
            await api(`/api/portfolio/holdings/${h.id}`, { method: 'PATCH', body: JSON.stringify({ notes }) });
            h.notes = notes;
            btn.textContent = 'Saved ✓'; setTimeout(() => btn.textContent = 'Save note', 1600);
          } catch (e) { btn.textContent = 'Save failed — ' + String(e.message).slice(0, 60); }
        });
      }
    }
  }

  async function loadData(sym) {
    state = {};
    const h = cfg.owned ? cfg.owned(sym) : null;
    const jobs = [
      api(`/api/symbol/${encodeURIComponent(sym)}`).then(d => { if (currentSym === sym) { state.d = d; header(d); renderBody(); } }),
      api(`/api/symbol/${encodeURIComponent(sym)}/price?days=252`).then(d => {
        if (currentSym === sym) { state.price = (d.data || []).slice().reverse(); if (tab === 'overview') renderBody(); }
      }).catch(() => {}),
    ];
    if (h) {
      jobs.push(api(`/api/portfolio/holdings/${h.id}/transactions`).then(d => {
        if (currentSym === sym) { state.txns = d.data || []; if (tab === 'overview' || tab === 'txns') renderBody(); }
      }).catch(() => { state.txns = []; }));
    }
    try { await Promise.all(jobs); }
    catch (e) {
      if (currentSym === sym) {
        document.getElementById('dpBody').innerHTML =
          `<div style="color:var(--red);padding:12px;font-size:13px;background:var(--red-bg);border:1px solid var(--red);border-radius:8px">Could not load ${esc(sym)}: ${esc(String(e.message).slice(0, 140))}</div>`;
      }
    }
  }

  function open(sym) {
    ensureDom();
    currentSym = sym;
    document.getElementById('dp').classList.add('open');
    document.getElementById('dpSym').textContent = sym;
    document.getElementById('dpName').textContent = '';
    document.getElementById('dpPrice').textContent = '—';
    document.getElementById('dpChg').textContent = '';
    document.getElementById('dpOwnStrip').innerHTML = '';
    renderTabs();
    document.getElementById('dpBody').innerHTML = '<div class="ld"><div class="spin"></div> Loading…</div>';
    loadData(sym);
  }

  function close() {
    const dp = document.getElementById('dp');
    if (dp) dp.classList.remove('open');
    currentSym = null;
  }

  return {
    init(options) { cfg = Object.assign(cfg, options || {}); ensureDom(); },
    open, close,
    refreshOwned(sym) { if (currentSym === sym && state.d) { header(state.d); renderBody(); } },
    isOpen: () => !!document.querySelector('#dp.open'),
  };
})();
