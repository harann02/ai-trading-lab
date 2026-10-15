/* v4 beginner layer (presentation only): info icons + accessible popovers, plain-language labels, "Today at a glance", "Learn the basics" tab.
   Texts come from glossary.js. Reads the same already-loaded JSON as the rest of the page; never invents numbers; changes no data. */
const Help = (() => {
  const G = window.GLOSSARY || [], FB = window.GLOSSARY_FALLBACK, BY = Object.fromEntries(G.map(g => [g.k, g]));
  const RX = G.map(g => [new RegExp(g.match, 'i'), g]);
  const e = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const find = t => { t = String(t || '').replace(/\s+/g, ' ').trim(); for (const [r, g] of RX) if (r.test(t)) return g; return null; };
  const textOf = el => { const c = el.cloneNode(true); c.querySelectorAll('.tag,.chip,.info,.tech,.btn,a').forEach(x => x.remove()); return c.textContent.replace(/\s+/g, ' ').trim(); };
  let pop, cur = null, pinned = false, hideT;
  const fine = () => window.matchMedia && matchMedia('(hover: hover) and (pointer: fine)').matches;

  function btn(g, label) {
    const b = document.createElement('button'); b.type = 'button'; b.className = 'info'; b.dataset.k = g.k; b.textContent = 'ⓘ';
    b.setAttribute('aria-label', 'What does "' + (label || g.term) + '" mean?'); b.setAttribute('aria-expanded', 'false'); b.setAttribute('aria-controls', 'helppop'); b.setAttribute('aria-haspopup', 'dialog');
    return b;
  }
  function ensurePop() {
    if (pop) return pop; pop = document.createElement('div'); pop.id = 'helppop'; pop.className = 'helppop hidden'; pop.setAttribute('role', 'dialog'); pop.setAttribute('aria-modal', 'false'); pop.tabIndex = -1;
    pop.addEventListener('mouseenter', () => clearTimeout(hideT)); pop.addEventListener('mouseleave', () => { if (!pinned) hideT = setTimeout(close, 200); });
    document.body.appendChild(pop); return pop;
  }
  function body(g) {
    return `<div class="hp-h"><b>${e(g.plain || g.term)}</b>${g.plain ? ` <span class="tech">${e(g.term)}</span>` : ''}<button type="button" class="hp-x" aria-label="Close explanation">×</button></div>
      <p><b>What it is:</b> ${e(g.what)}</p><p><b>How to read it:</b> ${e(g.read)}</p><p><b>Good or bad?</b> ${e(g.good)}</p>`;
  }
  function open(b, pin) {
    const g = BY[b.dataset.k] || FB; ensurePop(); clearTimeout(hideT);
    if (cur && cur !== b) cur.setAttribute('aria-expanded', 'false');
    cur = b; pinned = !!pin; pop.innerHTML = body(g); pop.setAttribute('aria-label', g.plain || g.term); pop.classList.remove('hidden'); b.setAttribute('aria-expanded', 'true');
    pop.querySelector('.hp-x').onclick = () => close(true);
    const r = b.getBoundingClientRect(), w = Math.min(340, window.innerWidth - 16); pop.style.width = w + 'px';
    let left = Math.min(Math.max(8, r.left + window.scrollX - w / 2 + r.width / 2), window.scrollX + window.innerWidth - w - 8);
    pop.style.left = left + 'px'; pop.style.top = (r.bottom + window.scrollY + 6) + 'px';
    const ph = pop.offsetHeight; if (r.bottom + ph + 12 > window.innerHeight && r.top > ph + 12) pop.style.top = (r.top + window.scrollY - ph - 6) + 'px';
  }
  function close(refocus) {
    if (!pop || pop.classList.contains('hidden')) return; pop.classList.add('hidden'); pinned = false;
    if (cur) { cur.setAttribute('aria-expanded', 'false'); if (refocus) cur.focus(); } cur = null;
  }
  document.addEventListener('click', ev => {
    const b = ev.target.closest && ev.target.closest('button.info');
    if (b) { ev.preventDefault(); ev.stopPropagation(); if (cur === b && pinned) close(); else { open(b, true); } return; }
    if (pop && !pop.contains(ev.target)) close();
  }, true);
  document.addEventListener('keydown', ev => { if (ev.key === 'Escape') close(true); });
  document.addEventListener('mouseover', ev => { const b = ev.target.closest && ev.target.closest('button.info'); if (b && fine() && !pinned) open(b, false); });
  document.addEventListener('mouseout', ev => { const b = ev.target.closest && ev.target.closest('button.info'); if (b && !pinned) hideT = setTimeout(close, 250); });
  window.addEventListener('resize', () => close());

  /* ---- decorate whatever the existing views rendered ---- */
  function add(host, g, label, where) { if (host.querySelector(':scope > .info')) return; const b = btn(g, label); where === 'start' ? host.prepend(b) : host.appendChild(b); }
  function decorate() {
    const view = document.querySelector('#view'); if (!view) return;
    view.querySelectorAll('.card > .l:not([data-ih])').forEach(l => {
      l.dataset.ih = '1'; let t = textOf(l), g = find(t);
      if (!g && !t) { g = BY.sleeve || FB; t = 'Strategy sleeve'; }
      if (!g) g = FB;
      if (g.plain && t && !t.toLowerCase().includes(g.plain.toLowerCase()) && g.k !== 'sleeve')
        l.innerHTML = `<span class="plain">${e(g.plain)}</span> <span class="tech">${l.innerHTML}</span>`;
      l.closest('.card').dataset.help = g.k; add(l, g, t || g.term);
    });
    // Cards that somehow have no .l at all still get an info button
    view.querySelectorAll('.card:not([data-help])').forEach(c => {
      const g = FB; c.dataset.help = g.k; const host = c.querySelector('.l') || c; add(host, g, 'this card', 'start');
    });
    view.querySelectorAll('section h2:not([data-ih]), section > h3:not([data-ih])').forEach(h => {
      h.dataset.ih = '1'; const t = textOf(h), g = find(t); if (!g) return;
      const b = btn(g, t), first = h.querySelector('.tag,.chip,a.btn'); first ? h.insertBefore(b, first) : h.appendChild(b); h.dataset.help = g.k;
    });
    view.querySelectorAll('h2 .tag:not([data-ih]), h3 > .chip:not([data-ih])').forEach(s => {
      s.dataset.ih = '1'; const g = find(s.textContent) || BY.provenance; s.insertAdjacentElement('afterend', btn(g, s.textContent.trim()));
    });
    view.querySelectorAll('thead th:not([data-ih])').forEach(th => {
      th.dataset.ih = '1'; const g = find(th.textContent.replace(/[▲▼]/g, '')); if (!g) return;
      const seen = th.closest('tr').querySelectorAll(`.info[data-k="${g.k}"]`).length; if (!seen) th.appendChild(btn(g, th.textContent.replace(/[▲▼]/g, '').trim()));
    });
    view.querySelectorAll('.chart:not([data-ih])').forEach(c => { c.dataset.ih = '1'; c.setAttribute('role', 'img'); c.setAttribute('aria-label', 'Chart, see the ⓘ next to the heading for how to read it'); });
    if ((location.hash || '#overview').match(/^#?(overview)?$/) && !view.querySelector('#glance') && view.querySelector('section')) { view.insertAdjacentHTML('afterbegin', glance()); decorate(); }
    document.querySelectorAll('#chips .chip:not([data-ih]), header .tag.paper:not([data-ih])').forEach(c => {
      c.dataset.ih = '1'; const g = find(c.textContent) || BY.provenance; c.insertAdjacentElement('afterend', btn(g, c.textContent.trim()));
    });
  }

  /* ---- Today at a glance: only real values; missing data is said to be missing ---- */
  function glance() {
    const d = (window.AITL && AITL.S.d) || {}, k = d.kpis || {}, v = d.v3 || {}, x = v.verification || {}, ai = v.ai || {}, md = v.market_data || {}, o = d.orders || [], h = d.holdings || [];
    const rm = n => 'RM' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }), has = n => n != null && !isNaN(n);
    const L = [];
    if (has(k.equity_myr)) {
      const st = has(k.starting_capital_myr) ? k.starting_capital_myr : null, p = k.total_pnl_myr;
      L.push(`Your ${st != null ? rm(st).replace('.00', '') + ' ' : ''}practice account is worth <b>${rm(k.equity_myr)}</b>` + (has(p) ? ` — <span class="${p > 0 ? 'pos' : p < 0 ? 'neg' : 'dim'}">${p > 0 ? 'up' : p < 0 ? 'down' : 'unchanged at'} ${rm(Math.abs(p))}${has(k.total_return_pct) ? ' (' + (k.total_return_pct >= 0 ? '+' : '') + k.total_return_pct.toFixed(2) + '%)' : ''}</span> since the start.` : '.'));
    } else L.push('Account value is not available in this snapshot yet.');
    const fills = o.filter(r => /fill/i.test(r.status || '')).length, pend = o.filter(r => r.status === 'pending').length, canc = o.filter(r => /cancel|reject/i.test(r.status || '')).length;
    if (!h.length && !fills) L.push(`No trades yet — the bot hasn't found an opportunity that passes all its rules, so the money is sitting in cash.${pend ? ` ${pend} order(s) are waiting for the next US market open.` : ''}${canc ? ` ${canc} earlier order(s) were canceled/rejected (no money lost on those).` : ''}`);
    else L.push(`The account owns <b>${h.length}</b> stock position(s)${pend ? ` and has ${pend} pending order(s)` : ''}.`);
    if (!h.length && has(k.total_pnl_myr) && k.total_pnl_myr < 0 && has(k.currency_pnl_myr) && Math.abs(k.stock_pnl_myr || 0) < 0.005)
      L.push(`The small loss comes from changing ringgit into US dollars (exchange-rate fee ${has(k.fx_spread_paid_myr) ? rm(k.fx_spread_paid_myr) : ''} and rate moves), not from stocks.`);
    const bm = ((v.benchmarks || {}).rows || []).filter(r => r.key !== 'AI' && has(r.total_return_pct));
    if (bm.length) L.push('For comparison, simply buying the market would have returned ' + bm.map(r => `${e(r.key)} ${r.total_return_pct >= 0 ? '+' : ''}${r.total_return_pct.toFixed(2)}%`).join(' and ') + ' over the same time.');
    const sys = [];
    if (x.status) sys.push(`Reliability check: <b>${e(x.status)}</b> (${x.consecutive_qualifying || 0} of ${x.required_sessions || 20} good days in a row${x.failed_sessions ? `, ${x.failed_sessions} failed` : ''})`);
    if (ai.state) sys.push(`AI writer: <b>${e(ai.state)}</b>${/DEGRAD|UNAVAIL|FAIL/.test(ai.state) ? ' (only the written explanations are affected, not trading)' : ''}`);
    if (md.spy_last_final_bar || md.exchange_session_date) sys.push(`prices as of US close <b>${e(md.spy_last_final_bar || md.exchange_session_date)}</b>${md.stale ? ' — <span class="neg">STALE, trading paused</span>' : ''}`);
    return `<section class="wide glance" id="glance" aria-labelledby="glance-h"><h2 id="glance-h">Today at a glance</h2>${L.map(s => `<p>${s}</p>`).join('')}${sys.length ? `<p class="dim">${sys.join(' · ')}.</p>` : ''}
      <p class="note">Practice (paper) trading with virtual money only. Not investment advice. New here? Tap any ⓘ or open <a href="#learn">Learn the basics</a>.</p></section>`;
  }

  /* ---- Learn the basics tab ---- */
  function learn(root) {
    const groups = [...new Set(G.map(g => g.group))];
    root.innerHTML = `<section class="wide"><h2>Learn the basics</h2><p>Zero trading knowledge needed. This page is a practice (paper) trading robot: it pretends to invest RM10,000 in US stocks using fixed rules, and shows honestly how it is doing. Here is every term used on the site, in plain English.</p></section>` +
      groups.map(gr => `<section class="wide learn"><h2>${e(gr)}</h2><dl>${G.filter(g => g.group === gr).map(g => `<div class="term" id="learn-${g.k}"><dt>${e(g.plain || g.term)}${g.plain ? ` <span class="tech">${e(g.term)}</span>` : ''}</dt><dd><p>${e(g.what)}</p><p><b>How to read it:</b> ${e(g.read)}</p><p><b>Good or bad?</b> ${e(g.good)}</p></dd></div>`).join('')}</dl></section>`).join('');
  }
  window.V3VIEWS = { ...(window.V3VIEWS || {}), learn };

  let busy = false; const run = () => { if (busy) return; busy = true; try { decorate(); } catch (err) { console.warn('help decorate', err); } busy = false; };
  const mo = new MutationObserver(() => { if (!busy) requestAnimationFrame(run); });
  const start = () => { ['#view', '#chips'].forEach(s => { const el = document.querySelector(s); if (el) mo.observe(el, { childList: true, subtree: true }); }); run(); };
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', start) : start();
  window.addEventListener('hashchange', () => close());
  return { find, decorate, glance, entries: G };
})();
