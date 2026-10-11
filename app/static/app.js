const $ = s => document.querySelector(s);
const api = async (p, opt) => { const r = await fetch('/api' + p, opt); if (!r.ok) { let t = ''; try { t = (await r.json()).detail } catch (e) {} throw new Error(t || r.status) } return r.json() };
const post = (p, b) => api(p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b || {}) });
const f2 = (v, d = 2) => v == null || isNaN(v) ? '-' : Number(v).toLocaleString('en-MY', { minimumFractionDigits: d, maximumFractionDigits: d });
const cls = v => v > 0 ? 'pos' : v < 0 ? 'neg' : '';
const esc = s => String(s ?? '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
const chartOpts = { layout: { background: { color: '#0d141c' }, textColor: '#9fb3c4' }, grid: { vertLines: { color: '#17212d' }, horzLines: { color: '#17212d' } }, rightPriceScale: { borderColor: '#1f2a37' }, timeScale: { borderColor: '#1f2a37' }, crosshair: { mode: 0 } };
const mk = (el, o = {}) => LightweightCharts.createChart(typeof el === 'string' ? $(el) : el, { ...chartOpts, autoSize: true, ...o });
let state = { range: 'ALL', sel: null, sort: {}, lastEvent: 0 };

/* ---------- generic sortable table ---------- */
function table(el, cols, rows, key, onclick, filterEl) {
  const host = $(el); const st = state.sort[key] || (state.sort[key] = { col: null, dir: 1 });
  const flt = filterEl ? $(filterEl).value.toLowerCase() : '';
  let data = rows.filter(r => !flt || JSON.stringify(r).toLowerCase().includes(flt));
  if (st.col != null) { const c = cols[st.col]; data = [...data].sort((a, b) => { const x = c.v(a), y = c.v(b); return (x == null) - (y == null) || (x > y ? 1 : x < y ? -1 : 0) * st.dir }) }
  host.innerHTML = '<div class="scroll"><table><thead><tr>' + cols.map((c, i) => `<th class="${c.l ? 'l' : ''}" data-i="${i}">${c.h}${st.col === i ? (st.dir > 0 ? ' ▲' : ' ▼') : ''}</th>`).join('') + '</tr></thead><tbody>' +
    data.map((r, j) => `<tr data-j="${j}">` + cols.map(c => `<td class="${c.l ? 'l' : ''} ${c.c ? c.c(r) : ''}">${c.f ? c.f(r) : esc(c.v(r))}</td>`).join('') + '</tr>').join('') + '</tbody></table></div>' + (data.length ? '' : '<div class="dim">no rows</div>');
  host.querySelectorAll('th').forEach(th => th.onclick = () => { const i = +th.dataset.i; st.dir = st.col === i ? -st.dir : 1; st.col = i; table(el, cols, rows, key, onclick, filterEl) });
  if (onclick) host.querySelectorAll('tbody tr').forEach(tr => tr.onclick = () => onclick(data[+tr.dataset.j]));
}

/* ---------- status / KPIs ---------- */
async function loadStatus() {
  const s = await api('/status'); state.status = s;
  const mk_ = s.markets.map(m => `<span class="${m.open ? 'pos' : 'dim'}">${m.market} ${m.open ? 'OPEN' : 'closed'} (${m.local_time} ${m.tz.split('/')[1]})</span>`).join(' &nbsp;|&nbsp; ');
  const sch = s.scheduler; const nx = Object.entries(sch.next_runs || {}).filter(([k]) => k.startsWith('scan')).map(([k, v]) => `${k}: ${v ? new Date(v).toLocaleString('en-GB', { timeZone: 'Asia/Kuala_Lumpur', hour12: false }) : '-'}`).join(', ');
  $('#statusbar').innerHTML = `${s.now_myr} | data: <b class="${s.data_mode === 'LIVE' ? 'pos' : 'warn'}">${s.data_mode}</b> (yfinance delayed/EOD) | ${mk_} | USD/MYR ${s.fx.rate ? f2(s.fx.rate, 4) : 'n/a'} <span class="dim">(${s.fx.date || '-'} ${s.fx.status})</span>`;
  const b = $('#banner'); b.classList.toggle('hidden', !s.banner); b.textContent = s.banner || '';
  const sp = $('#suspend'); sp.classList.toggle('hidden', !s.entries_suspended);
  sp.innerHTML = s.entries_suspended ? `⚠ NEW ENTRIES SUSPENDED: ${esc(s.suspend_reason)}. Manual authorization required. <button id="resumebtn">Authorize resume</button>` : '';
  if ($('#resumebtn')) $('#resumebtn').onclick = async () => { if (confirm('Authorize resuming new entries? Drawdown measurement restarts from now.')) { await post('/resume', { confirm: 'RESUME' }); refresh() } };
  const srcs = s.sources.map(x => `<span class="${x.ok ? 'pos' : 'neg'}">${x.name}: ${x.ok ? 'OK' : 'FAIL'}</span> <span class="dim">${esc(x.detail).slice(0, 90)} @${x.ts.slice(11, 19)}Z</span>`).join('<br>') || '<span class="warn">no data fetched yet</span>';
  $('#monitor').innerHTML = `<div>Last refresh (browser): ${new Date().toLocaleTimeString('en-GB', { timeZone: 'Asia/Kuala_Lumpur' })} MYT | Last scan: ${s.last_scan || 'never'} ${s.scan_running ? '<b class="warn">(scan running)</b>' : ''}</div>
   <div>Scheduler: <b class="${sch.healthy ? 'pos' : 'neg'}">${sch.healthy ? 'HEALTHY' : 'DOWN'}</b> <span class="dim">${esc(sch.detail)}</span></div><div>Next scans (MYT): ${nx || '-'}</div><div>${srcs}</div>
   <div class="row"><button id="scanall">Scan now (all)</button><button id="scanb">Bursa</button><button id="scanu">US</button><button id="mon">Run monitor/execute</button></div>`;
  $('#scanall').onclick = () => act(() => post('/scan')); $('#scanb').onclick = () => act(() => post('/scan?market=BURSA')); $('#scanu').onclick = () => act(() => post('/scan?market=US'));
  $('#mon').onclick = () => act(async () => { const r = await post('/monitor'); alert(JSON.stringify(r.log, null, 1)) });
}
async function act(fn) { try { await fn(); setTimeout(refresh, 1500) } catch (e) { alert('Error: ' + e.message) } }

async function loadKpis() {
  const k = await api('/kpis'); $('#kpilabel').textContent = k.label;
  const c = (l, v, x = '') => `<div class="card"><div class="l">${l}</div><div class="v ${x}">${v}</div></div>`;
  $('#kpis').innerHTML = c('Starting capital', 'RM ' + f2(k.starting_capital)) + c('Equity', 'RM ' + f2(k.equity)) + c('Cash', 'RM ' + f2(k.cash)) + c('Total P&L', 'RM ' + f2(k.total_pnl), cls(k.total_pnl)) +
    c("Today's P&L", 'RM ' + f2(k.todays_pnl), cls(k.todays_pnl)) + c('Return', f2(k.return_pct) + '%', cls(k.return_pct)) + c('Max drawdown', f2(k.max_drawdown_pct) + '%', k.max_drawdown_pct < 0 ? 'neg' : '') + c('Exposure', f2(k.exposure_pct) + '%') +
    (k.stale_symbols.length ? c('STALE PRICES', k.stale_symbols.join(','), 'warn') : '');
}

/* ---------- equity ---------- */
let eqChart, ddChart;
async function loadEquity() {
  $('#ranges').innerHTML = ['1D', '1W', '1M', '3M', 'YTD', 'ALL'].map(r => `<button class="${r === state.range ? 'on' : ''}" data-r="${r}">${r}</button>`).join('');
  $('#ranges').querySelectorAll('button').forEach(b => b.onclick = () => { state.range = b.dataset.r; loadEquity() });
  const d = await api('/equity?range=' + state.range); $('#benchname').textContent = d.benchmark_symbol ? 'vs ' + d.benchmark_symbol + ' (rebased)' : '';
  if (eqChart) { eqChart.remove(); ddChart.remove() } eqChart = mk('#eqchart'); ddChart = mk('#ddchart');
  const s1 = eqChart.addLineSeries({ color: '#26a69a', lineWidth: 2, title: 'Equity RM' }); s1.setData(d.equity);
  if (d.benchmark.length) eqChart.addLineSeries({ color: '#f5c542', lineWidth: 1, title: d.benchmark_symbol }).setData(d.benchmark);
  ddChart.addAreaSeries({ lineColor: '#ef5350', topColor: '#ef535055', bottomColor: '#ef535005' }).setData(d.drawdown);
  eqChart.timeScale().fitContent(); ddChart.timeScale().fitContent();
}

/* ---------- holdings / discovery ---------- */
async function loadHoldings() {
  const h = await api('/holdings');
  table('#holdings', [
    { h: 'Symbol', v: r => r.symbol, l: 1 }, { h: 'Qty', v: r => r.qty }, { h: 'Avg cost RM', v: r => r.avg_cost_myr, f: r => f2(r.avg_cost_myr, 3) }, { h: 'Last', v: r => r.last_price, f: r => f2(r.last_price, 3) },
    { h: 'Value RM', v: r => r.market_value_myr, f: r => f2(r.market_value_myr) }, { h: 'Unreal. P&L', v: r => r.unrealized_pnl_myr, f: r => f2(r.unrealized_pnl_myr) + ' (' + f2(r.unrealized_pct * 100, 1) + '%)', c: r => cls(r.unrealized_pnl_myr) },
    { h: 'Stop (local)', v: r => r.stop, f: r => f2(r.stop, 3) }, { h: 'Target', v: r => r.target, f: r => r.target ? f2(r.target, 3) : 'none' }, { h: 'AI score', v: r => r.ai_score, f: r => f2(r.ai_score, 1) },
    { h: 'Action', v: r => r.action, l: 1 }, { h: 'Data', v: r => r.data_flag || 'ok', l: 1, c: r => r.data_flag ? 'warn' : 'dim' }
  ], h, 'hold', r => selectSym(r.symbol), '#f_hold');
}
async function loadDiscovery() {
  let d = await api('/discovery' + ($('#mkt').value ? '?market=' + $('#mkt').value : '')); const df = $('#decf').value; if (df) d = d.filter(x => x.decision === df);
  table('#discovery', [
    { h: '#', v: r => r.score, f: r => '' }, { h: 'Symbol', v: r => r.symbol, l: 1 }, { h: 'Mkt', v: r => r.market, l: 1 }, { h: 'Score', v: r => r.score, f: r => f2(r.score, 1) }, { h: 'Band', v: r => r.band, l: 1 },
    { h: 'Entry', v: r => r.entry, f: r => f2(r.entry, 3) }, { h: 'Stop', v: r => r.stop, f: r => f2(r.stop, 3) }, { h: 'Target', v: r => r.target, f: r => r.target ? f2(r.target, 3) : 'none' }, { h: 'R:R', v: r => r.rr, f: r => r.rr ? f2(r.rr) : '-' },
    { h: 'Size', v: r => r.qty }, { h: 'Decision', v: r => r.decision, f: r => `<span class="badge ${r.decision}">${r.decision}</span>` }, { h: 'Blocked by', v: r => r.failed.join(','), l: 1, c: r => 'dim' }, { h: 'Scan date', v: r => r.scan_date }
  ], d, 'disc', r => { selectSym(r.symbol); showDecision(r.id) }, '#f_disc');
  document.querySelectorAll('#discovery tbody tr').forEach((tr, i) => tr.firstChild.textContent = i + 1);
}

/* ---------- candles ---------- */
let cc = null;
async function selectSym(sym) {
  state.sel = sym; $('#chartsym').textContent = sym;
  try { const d = await api('/candles/' + encodeURIComponent(sym)); drawCandles(d) } catch (e) { $('#chartmeta').textContent = 'No chart: ' + e.message }
  try { const dd = await api('/decision/' + encodeURIComponent(sym)); renderDecision(dd) } catch (e) { $('#explain').textContent = 'No decision recorded for ' + sym }
}
function drawCandles(d) {
  if (cc) cc.forEach(c => c.remove());
  $('#chartlabel').textContent = d.label; $('#chartmeta').innerHTML = `Data status: <b class="${d.status.ok ? 'pos' : 'neg'}">${d.status.status}</b> ${esc(d.status.reason || '')} | last bar ${d.status.last_bar || '-'} ${d.demo ? '<b class="warn">DEMO SYNTHETIC</b>' : ''} | <span class="dim">SMA20 blue, SMA50 yellow, SMA200 purple, EMA20 white</span>`;
  const c1 = mk('#cchart'), c2 = mk('#rchart'), c3 = mk('#mchart'); cc = [c1, c2, c3];
  const cs = c1.addCandlestickSeries({ upColor: '#26a69a', downColor: '#ef5350', borderVisible: false, wickUpColor: '#26a69a', wickDownColor: '#ef5350' }); cs.setData(d.candles);
  const v = c1.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: '' }); v.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } }); v.setData(d.volume);
  [['sma20', '#4aa3ff'], ['sma50', '#f5c542'], ['sma200', '#b388ff'], ['ema20', '#ffffff']].forEach(([k, col]) => c1.addLineSeries({ color: col, lineWidth: 1, priceLineVisible: false, lastValueVisible: false }).setData(d[k]));
  if (d.markers.length) cs.setMarkers(d.markers.sort((a, b) => a.time < b.time ? -1 : 1));
  if (d.stop) cs.createPriceLine({ price: d.stop, color: '#ef5350', lineStyle: 2, title: 'STOP' });
  if (d.target) cs.createPriceLine({ price: d.target, color: '#26a69a', lineStyle: 2, title: 'TARGET' });
  const rs = c2.addLineSeries({ color: '#4aa3ff', lineWidth: 1 }); rs.setData(d.rsi); [30, 70].forEach(p => rs.createPriceLine({ price: p, color: '#555', lineStyle: 2, title: '' }));
  c3.addHistogramSeries({}).setData(d.macd_hist); c3.addLineSeries({ color: '#4aa3ff', lineWidth: 1 }).setData(d.macd); c3.addLineSeries({ color: '#f5c542', lineWidth: 1 }).setData(d.macd_sig);
  let lock = false; cc.forEach(a => a.timeScale().subscribeVisibleLogicalRangeChange(r => { if (lock || !r) return; lock = true; cc.forEach(b => b !== a && b.timeScale().setVisibleLogicalRange(r)); lock = false }));
}
async function showDecision(id) { try { renderDecision(await api('/decision/' + id)) } catch (e) {} }
function renderDecision(d) {
  const f = d.scores && d.scores.factors || {}; const chk = (d.checks || []).map(c => `<div class="${c.pass ? 'pos' : 'neg'}">${c.pass ? '✔' : '✘'} ${esc(c.name)}: <span class="dim">${esc(c.detail)}</span></div>`).join('');
  const mvc = d.scores && d.scores.minervini ? Object.entries(d.scores.minervini.criteria).map(([k, v]) => `${v == null ? '?' : v ? '✔' : '✘'} ${k}`).join('\n') : '';
  const cs = d.scores && d.scores.canslim ? Object.entries(d.scores.canslim.criteria).map(([k, v]) => `${v == null ? '?' : v ? '✔' : '✘'} ${k}`).join('\n') : '';
  const gr = d.scores && d.scores.graham ? Object.entries(d.scores.graham.criteria).map(([k, v]) => `${v == null ? '?' : v ? '✔' : '✘'} ${k}`).join('\n') : '';
  $('#explain').innerHTML = `<b>${esc(d.symbol)}</b> <span class="badge ${d.decision}">${d.decision}</span> score ${f2(d.score, 1)} (${esc(d.band)}) | scan ${d.scan_date} | algo ${esc(d.algo_version)} | data ${d.data_mode}
<b>Factors</b> (weights .25/.20/.20/.15/.10/.10): ${Object.entries(f).map(([k, v]) => k + '=' + (v == null ? 'n/a' : v)).join(', ')}
${(d.scores && d.scores.flags || []).map(x => '⚠ ' + esc(x)).join('\n')}
<b>Checks</b>\n${chk}
<b>Minervini trend template</b> ${d.scores && d.scores.minervini ? d.scores.minervini.passed + '/8' : ''}\n${esc(mvc)}
<b>CAN SLIM-lite</b>\n${esc(cs)}
<b>Graham-style</b>\n${esc(gr)}
<b>Explanation</b>\n${esc(d.explanation)}
${d.order ? '<b>Order</b> #' + d.order.id + ' ' + d.order.side + ' ' + d.order.qty + ' status ' + d.order.status + ' - ' + esc(d.order.reason) + (d.order.status === 'pending' && d.order.needs_approval && !d.order.approved ? ` <button onclick="act(()=>post('/orders/${d.order.id}/approve'))">Approve</button>` : '') + (d.order.status === 'pending' ? ` <button onclick="act(()=>post('/orders/${d.order.id}/cancel'))">Cancel</button>` : '') : ''}
${d.memo && d.memo.text ? '\n<b>Investment memo</b>\n' + esc(d.memo.text) : ''}`;
}

/* ---------- ledger ---------- */
async function loadLedger() {
  const l = await api('/ledger');
  table('#ledger', [
    { h: 'ID', v: r => r.id }, { h: 'Time (MYT)', v: r => r.ts_myt, l: 1 }, { h: 'Type', v: r => r.kind, l: 1 }, { h: 'Symbol', v: r => r.symbol, l: 1 }, { h: 'Qty', v: r => r.qty }, { h: 'Price', v: r => r.price_local, f: r => f2(r.price_local, 4) + ' ' + (r.ccy || '') },
    { h: 'FX', v: r => r.fx, f: r => f2(r.fx, 4) }, { h: 'Gross RM', v: r => r.gross_myr, f: r => f2(r.gross_myr) }, { h: 'Fees RM', v: r => r.fees_myr, f: r => f2(r.fees_myr) }, { h: 'Slip RM', v: r => r.slippage_myr, f: r => f2(r.slippage_myr) },
    { h: 'Cash Δ RM', v: r => r.cash_delta_myr, f: r => f2(r.cash_delta_myr), c: r => cls(r.cash_delta_myr) }, { h: 'Realized RM', v: r => r.realized_pnl_myr, f: r => f2(r.realized_pnl_myr), c: r => cls(r.realized_pnl_myr) }, { h: 'Note', v: r => r.note, l: 1 }
  ], l, 'ledger');
}

/* ---------- analytics ---------- */
let anCharts = [];
function bars(el, rows, fmt = v => f2(v)) {
  const mx = Math.max(1e-9, ...rows.map(r => Math.abs(r.value)));
  $(el).innerHTML = rows.map(r => `<div class="bar"><span style="width:90px">${esc(r.name)}</span><i class="${r.value < 0 ? 'n' : ''}" style="width:${Math.abs(r.value) / mx * 140}px"></i><span class="${cls(r.value)}">${fmt(r.value)}</span></div>`).join('') || '<span class="dim">no data</span>';
}
async function loadAnalytics() {
  const a = await api('/analytics'); anCharts.forEach(c => c.remove()); anCharts = [];
  const hist = (el, rows) => { const c = mk(el); anCharts.push(c); c.addHistogramSeries({}).setData(rows.map(r => ({ time: r.period, value: r.ret_pct, color: r.ret_pct >= 0 ? '#26a69a' : '#ef5350' }))); c.timeScale().fitContent() };
  hist('#monthly', a.monthly); hist('#weekly', a.weekly);
  const fc = mk('#fees'); anCharts.push(fc); const m = new Map(); a.fees_slippage.forEach(r => m.set(r.time, r));
  const arr = [...m.values()]; fc.addLineSeries({ color: '#f5c542', title: 'fees' }).setData(arr.map(r => ({ time: r.time, value: r.fees }))); fc.addLineSeries({ color: '#b388ff', title: 'slip' }).setData(arr.map(r => ({ time: r.time, value: r.slippage }))); fc.timeScale().fitContent();
  bars('#winloss', [{ name: 'wins', value: a.win_loss.wins }, { name: 'losses', value: -a.win_loss.losses }], v => Math.abs(v));
  bars('#bystock', a.profit_by_symbol); bars('#alloc', a.allocation);
  const p = await api('/performance'); const rows = Object.entries(p).filter(([k, v]) => typeof v !== 'object' && k !== 'label').map(([k, v]) => `<tr><td class="l">${k}</td><td>${typeof v === 'number' ? f2(v, 3) : esc(v)}</td></tr>`).join('');
  $('#perf').innerHTML = `<div class="dim">${esc(p.label)}. Risk-free ${p.assumptions.risk_free_rate_annual * 100}% p.a., 252 trading days/yr. ${esc(p.assumptions.note)}</div><div class="scroll"><table><tbody>${rows}</tbody></table></div>`;
}

/* ---------- feed / SSE with polling fallback ---------- */
function addEvents(evs) {
  const box = $('#feed'); evs.forEach(e => { if (e.id <= state.lastEvent) return; state.lastEvent = e.id;
    const d = document.createElement('div'); d.className = e.level === 'ERROR' || e.level === 'CRITICAL' ? 'neg' : e.level === 'WARN' ? 'warn' : '';
    d.textContent = `${e.ts.replace('T', ' ').slice(5, 19)}Z [${e.source}] ${e.message}`; box.prepend(d) });
}
let poll = null;
function startFeed() {
  api('/feed?limit=60').then(addEvents).then(() => {
    try {
      const es = new EventSource('/api/stream?after=' + state.lastEvent);
      es.addEventListener('activity', e => { addEvents([JSON.parse(e.data)]); $('#sse').textContent = 'live (SSE)' });
      es.addEventListener('tick', () => $('#sse').textContent = 'live (SSE)');
      es.onerror = () => { $('#sse').textContent = 'polling fallback'; es.close(); if (!poll) poll = setInterval(() => api('/feed?after=' + state.lastEvent).then(addEvents), 10000) };
    } catch (e) { $('#sse').textContent = 'polling fallback'; poll = setInterval(() => api('/feed?after=' + state.lastEvent).then(addEvents), 10000) }
  });
}

/* ---------- settings ---------- */
async function loadSettings() {
  const s = await api('/settings');
  const num = (k, l) => `<label>${l}<input data-k="${k}" type="number" step="any" value="${s[k]}"></label>`;
  const chk = (k, l) => `<label>${l}<select data-k="${k}" data-bool="1"><option value="true" ${s[k] ? 'selected' : ''}>ON</option><option value="false" ${!s[k] ? 'selected' : ''}>OFF</option></select></label>`;
  $('#settings').innerHTML = `<div class="form">${num('risk_per_trade_pct', 'Risk per trade %')}${num('max_position_pct', 'Max per stock %')}${num('max_exposure_pct', 'Max exposure %')}${num('min_cash_pct', 'Min cash %')}${num('max_positions', 'Max positions')}${num('max_drawdown_pct', 'Max drawdown % (suspends entries)')}${num('atr_stop_mult', 'ATR stop multiple')}${num('min_rr', 'Min reward:risk (if target)')}${num('buy_min_score', 'Min score for BUY')}${num('risk_free_rate_annual', 'Risk-free rate (annual, decimal)')}${chk('auto_trade', 'Auto-trade (paper)')}${chk('manual_approval', 'Manual approval mode')}
  <label>Equity benchmark<input data-k="equity_benchmark" value="${esc(s.equity_benchmark)}"></label></div>
  <div class="row"><button id="savest">Save settings</button><span class="dim">Starting capital ${f2(s.starting_capital)} (changes only via full reset). Entries suspended: ${s.entries_suspended}</span></div>
  <h3>Watchlists / universe (JSON; Bursa symbols end .KL) &amp; markets enabled</h3><textarea id="wl">${esc(JSON.stringify({ watchlist: s.watchlist, markets_enabled: s.markets_enabled, benchmarks: s.benchmarks }, null, 1))}</textarea><div class="row"><button id="savewl">Save watchlists</button></div>
  <h3>Deposit / withdraw (virtual)</h3><div class="row"><input id="cashamt" type="number" placeholder="RM amount"><button id="dep">Deposit</button><button id="wd">Withdraw</button></div>
  <h3>Manual overrides</h3><div class="row"><input id="ovsym" placeholder="symbol e.g. 1155.KL"><button data-ov="block">Block symbol</button><button data-ov="unblock">Unblock</button><button data-ov="close">Close position (at next open)</button><input id="ovstop" type="number" step="any" placeholder="new stop"><button data-ov="raise_stop">Raise stop</button></div><div class="dim">Blocked: ${esc(s.blocked_symbols.join(', ') || 'none')}</div>
  <h3>Export / import / reset</h3><div class="row"><a class="btn" href="/api/export/settings" target="_blank">Export settings (JSON)</a><input type="file" id="impf" accept=".json"><button id="impbtn">Import settings</button><button id="reset" style="border-color:#ef5350;color:#ef5350">FULL RESET</button></div>`;
  $('#savest').onclick = () => act(async () => { const b = {}; document.querySelectorAll('#settings [data-k]').forEach(i => b[i.dataset.k] = i.dataset.bool ? i.value === 'true' : i.type === 'number' ? +i.value : i.value); await api('/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) }); alert('saved') });
  $('#savewl').onclick = () => act(async () => { await api('/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: $('#wl').value }); alert('saved') });
  const cash = k => act(async () => { await post('/cash', { kind: k, amount: +$('#cashamt').value }) });
  $('#dep').onclick = () => cash('DEPOSIT'); $('#wd').onclick = () => cash('WITHDRAW');
  document.querySelectorAll('[data-ov]').forEach(b => b.onclick = () => act(async () => { const r = await post('/override', { action: b.dataset.ov, symbol: $('#ovsym').value, stop: +$('#ovstop').value }); alert(JSON.stringify(r)); loadSettings() }));
  $('#impbtn').onclick = () => act(async () => { const t = await $('#impf').files[0].text(); await post('/import/settings', JSON.parse(t)); alert('imported'); loadSettings() });
  $('#reset').onclick = () => { const c = prompt('This archives the current database and restarts from virtual capital. Type RESET to confirm:'); if (c === 'RESET') act(async () => { await post('/reset', { confirm: 'RESET' }); location.reload() }) };
}

/* ---------- backtest / report ---------- */
async function loadBacktest() {
  const b = await api('/backtest'); const r = b.result; $('#btrun').disabled = b.running; $('#btrun').textContent = b.running ? 'Running...' : 'Run backtest (takes minutes)';
  if (!r) { $('#bt').innerHTML = '<span class="dim">No backtest run yet.</span>'; return }
  const row = (n, s) => s ? `<tr><td class="l">${n}</td><td>${f2(s.total_return_pct)}</td><td>${f2(s.cagr_pct)}</td><td>${f2(s.max_drawdown_pct)}</td><td>${f2(s.sharpe)}</td><td>${f2(s.sortino)}</td><td>${s.n_trades ?? '-'}</td><td>${f2(s.win_rate_pct, 1)}</td><td>${f2(s.profit_factor)}</td><td>${f2(s.avg_holding_days, 1)}</td><td>${s.avg_exposure != null ? f2(s.avg_exposure * 100, 1) : '-'}</td></tr>` : '';
  const sp = r.splits; const bm = r.benchmarks_buy_and_hold_myr.test;
  $('#bt').innerHTML = `<div class="tag warn">${esc(r.label)}</div> Period ${r.period} (${f2(r.years, 1)} yrs), params ${JSON.stringify(r.selected_params)} (${r.selection_rule}). Train ${sp.train.join('..')}, validation ${sp.validation.join('..')}, test ${sp.test.join('..')}.
  <div class="scroll"><table><thead><tr><th class="l">Run</th><th>Return %</th><th>CAGR %</th><th>MDD %</th><th>Sharpe</th><th>Sortino</th><th>Trades</th><th>Win %</th><th>PF</th><th>Avg hold d</th><th>Exposure %</th></tr></thead><tbody>
  ${row('STRATEGY test (out-of-sample)', r.test_result)}${row('STRATEGY full period', r.full_period_result_selected_params)}${row('Walk-forward stitched OOS', r.walk_forward.stitched_oos)}${Object.entries(bm).map(([k, v]) => row('B&H ' + k + ' (test, MYR)', v)).join('')}</tbody></table></div>
  <ul>${r.caveats.map(c => '<li class="warn">' + esc(c) + '</li>').join('')}</ul>`;
}
$('#btrun').onclick = () => act(async () => { await post('/backtest'); setTimeout(loadBacktest, 1000) });
async function loadReport() { try { const r = await api('/reports/daily'); $('#report').textContent = r.narrative + '\n\nfacts hash: ' + r.hash + '\n' + JSON.stringify(r.facts, null, 1) } catch (e) { $('#report').textContent = 'no report yet' } }

async function refresh() {
  const jobs = [loadStatus, loadKpis, loadEquity, loadHoldings, loadDiscovery, loadLedger, loadAnalytics, loadReport, loadBacktest];
  await Promise.all(jobs.map(j => j().catch(e => console.error(j.name, e))));
}
['f_hold', 'f_disc'].forEach(id => $('#' + id).oninput = () => (id === 'f_hold' ? loadHoldings() : loadDiscovery()));
$('#mkt').onchange = loadDiscovery; $('#decf').onchange = loadDiscovery;
refresh().then(async () => { await loadSettings(); const d = await api('/discovery').catch(() => []); if (d.length) selectSym(d[0].symbol); startFeed() });
setInterval(() => { loadStatus(); loadKpis(); loadHoldings() }, 20000); setInterval(refresh, 120000);
