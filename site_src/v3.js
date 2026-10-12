/* v3 views: overview strip, benchmarks, scheduler health, forward verification, reports, migration & archive. Data: data/v3.json (derived from the ledger). No secrets. */
const V3 = (() => {
  const v = () => (AITL.S.d.v3 || {});
  const LBL = { 'END-OF-DAY': 'ok', 'FORWARD PAPER': 'ok', 'DELAYED': 'mid', 'HISTORICAL BACKTEST': 'mid', 'LIVE': 'bad', 'DEMO': 'bad' };
  const lab = t => `<span class="chip ${LBL[t] || ''}" title="data provenance label">${esc(t)}</span>`;
  const sev = s => /VERIFIED|HEALTHY|ACTIVE/.test(s) ? 'ok' : /FAILED|ERROR|ACTION|DEGRADED|UNAVAIL/.test(s) ? 'bad' : 'warn';
  const kv = (o, ks) => ks.map(k => `<div><span class="dim">${esc(k)}:</span> ${esc(o && o[k] != null ? o[k] : '–')}</div>`).join('');
  let ccy = 'myr', rng = 'ALL';

  function verifCard() {
    const x = v().verification || {}; const n = x.consecutive_qualifying || 0, req = x.required_sessions || 20;
    return `<div class="card wide2" onclick="location.hash='#verification'" style="cursor:pointer"><div class="l">FORWARD TRADING VERIFICATION</div><div class="v ${sev(x.status || '')}">${esc(x.status || 'NOT STARTED')}</div>
      <div class="s">${n} / ${req} qualifying consecutive sessions · start session ${esc(x.start_session || 'not yet')} · failed ${x.failed_sessions || 0}</div><div class="bar"><i style="width:${Math.min(100, n / req * 100) * 1.6}px"></i></div></div>`;
  }
  function aiSub(a) {
    const i = a.indicators || {}, o = i.genuine_ai_output || {}, r = i.provider_reachable || {}, t = i.template_fallback || {}, u = i.ai_unavailable || {};
    return `provider ${r.ok ? 'reachable' : 'NOT reachable'} · accepted AI output: ${o.accepted_count ?? 0} (last ${o.window_days || 7}d)${o.last_accepted_ts ? ', last ' + esc(myt(o.last_accepted_ts)) : ''} · template fallback ${t.active ? 'ACTIVE' : 'off'}${u.active ? ' · UNAVAILABLE' : ''}`;
  }
  function aiPanel(a) {
    const i = a.indicators || {}, o = i.genuine_ai_output || {}, r = i.provider_reachable || {}, t = i.template_fallback || {}, u = i.ai_unavailable || {}, w = a.output_window || {};
    const chip = (ok, yes, no) => `<span class="chip ${ok ? 'ok' : 'bad'}">${ok ? yes : no}</span>`;
    return `<section class="wide" id="aipanel"><h2>AI status — four separate indicators <span class="tag ${sev(a.state || '')}">${esc(a.state || '–')}</span></h2><div class="note">${esc(a.label || '')}</div>
      <div class="cards k">
      <div class="card"><div class="l">1 · Provider reachable</div><div class="v">${chip(r.ok, 'YES', 'NO')}</div><div class="s">${esc(a.provider ? a.provider + '/' + a.model : '')} ${esc(r.detail || '')}</div></div>
      <div class="card"><div class="l">2 · Genuine AI output accepted (${o.window_days || 7}d)</div><div class="v ${o.accepted_count ? 'ok' : 'warn'}">${o.accepted_count ?? 0}</div><div class="s">last accepted: ${o.last_accepted_ts ? esc(myt(o.last_accepted_ts)) : 'never'} · rejected ${o.rejected_count ?? 0} · failed ${o.failed_count ?? 0}</div></div>
      <div class="card"><div class="l">3 · Template fallback active</div><div class="v">${chip(!t.active, 'NO', 'YES')}</div><div class="s">${t.template_count ?? 0} template texts in window${t.last_template_ts ? ' · last ' + esc(myt(t.last_template_ts)) : ''}</div></div>
      <div class="card"><div class="l">4 · AI unavailable / failed</div><div class="v">${chip(!u.active, 'NO', 'YES')}</div><div class="s">${u.failed_count ?? 0} failed calls in window</div></div></div>
      ${(o.rejected_reasons || []).length ? `<div class="note">Rejected / failed reasons: ${(o.rejected_reasons || []).map(x => esc(x.reason) + ' ×' + x.count).join('; ')}</div>` : ''}
      <div class="note">"VERIFIED" appears only when at least one model-written report/memo was accepted by the number validator in the last ${o.window_days || 7} days. A passing provider probe alone is NOT verification. The AI only explains; the rules engine decides.</div></section>`;
  }
  function strip() {
    const a = v().ai || {}, sc = v().scheduler || {}, md = v().market_data || {}, meta = AITL.S.d.meta || {};
    return `<section class="wide"><h2>System status <span class="tag">v${esc(v().version || '3')}</span></h2><div class="cards k">
      ${verifCard()}
      <div class="card" id="aicard"><div class="l">AI engine</div><div class="v ${sev(a.state || '')}" style="font-size:13px">${esc(a.state || '–')}</div><div class="s">${aiSub(a)}</div></div>
      <div class="card" onclick="location.hash='#health'" style="cursor:pointer"><div class="l">Scheduler health</div><div class="v ${sev(sc.status || '')}" style="font-size:13px">${esc(sc.status || '–')}</div><div class="s">missed 26h: ${sc.n_missed_26h ?? '–'} · last ok ${esc(ago((sc.last_successful_run || {}).started_ts))}</div></div>
      <div class="card"><div class="l">Dashboard refreshed</div><div class="v" style="font-size:13px">${esc(myt(meta.generated_utc))}</div><div class="s">${esc(ago(meta.generated_utc))} · run ${esc(meta.run_number || 'local')}</div></div>
      <div class="card" id="mdcard"><div class="l">Market data as of</div><div class="v" style="font-size:13px">${esc(md.spy_last_final_bar || md.exchange_session_date || '–')}</div><div class="s">exchange session (final bar) · retrieved ${esc(md.retrieved_utc ? myt(md.retrieved_utc) : 'n/a')} · ${md.stale ? 'STALE' : 'fresh'}</div></div>
    </div><div class="row">${['END-OF-DAY', 'DELAYED', 'FORWARD PAPER', 'HISTORICAL BACKTEST'].map(lab).join('')}<span class="dim">Provenance labels: LIVE / DELAYED / END-OF-DAY / HISTORICAL BACKTEST / FORWARD PAPER / DEMO. Nothing on this site is LIVE; yfinance is never labeled live.</span></div></section>`;
  }
  function norm(series, key, from) {
    const arr = (series || []).filter(r => !from || r.date >= from); if (arr.length < 1) return []; const b = arr[0][key]; return arr.map(r => ({ time: r.date, value: b ? r[key] / b * 10000 : null })).filter(x => x.value != null);
  }
  function fromDate(r, last) {
    const d = new Date(last + 'T00:00:00Z'); const sub = { '1D': 1, '1W': 7, '1M': 30, '3M': 91, '6M': 182 }[r];
    if (r === 'YTD') return d.getUTCFullYear() + '-01-01'; if (!sub) return null; d.setUTCDate(d.getUTCDate() - sub); return d.toISOString().slice(0, 10);
  }
  function benchChart(el) {
    const b = v().benchmarks || {}, S = b.series || {}; const ai = S.AI || []; if (!ai.length) { el.innerHTML = '<div class="dim">No equity history yet.</div>'; return; }
    const last = ai[ai.length - 1].date, from = fromDate(rng, last); const ch = mkchart(el, 340); const cols = { AI: '#e6edf3', SPY: '#4aa3ff', QQQ: '#ef5350' };
    Object.entries(S).forEach(([k, arr]) => { const s = ch.addLineSeries({ color: cols[k] || '#999', lineWidth: k === 'AI' ? 3 : 2, title: k, priceFormat: { type: 'custom', formatter: x => (ccy === 'myr' ? 'RM ' : '$ ') + x.toFixed(0) } }); s.setData(norm(arr, ccy, from)); });
    ch.timeScale().fitContent();
  }
  function benchControls(redraw) {
    return `<div class="row">${['1D', '1W', '1M', '3M', '6M', 'YTD', 'ALL'].map(r => `<button class="btn ${r === rng ? 'on' : ''}" data-r="${r}">${r}</button>`).join('')} <button class="btn" data-c="myr">${ccy === 'myr' ? '● ' : ''}MYR</button><button class="btn" data-c="usd">${ccy === 'usd' ? '● ' : ''}USD</button></div>`;
  }
  function bindBench(root, redraw) { $$('[data-r]', root).forEach(b => b.onclick = () => { rng = b.dataset.r; redraw(); }); $$('[data-c]', root).forEach(b => b.onclick = () => { ccy = b.dataset.c; redraw(); }); }

  function overview(root) {
    viewOverview(root); const first = root.querySelector('section'); first.insertAdjacentHTML('beforebegin', strip());
    const b = v().benchmarks || {};
    root.insertAdjacentHTML('beforeend', `<section class="wide" id="bsec"><h2>AI vs SPY vs QQQ (normalized to ${ccy === 'myr' ? 'RM' : '$'}10,000 at the common start) <span class="tag">FORWARD PAPER vs shadow benchmarks</span></h2><div id="bctl"></div><div id="bch" class="chart h340"></div><div class="note">${esc(b.note || '')}</div></section>`);
    const redraw = () => { $('#bctl').innerHTML = benchControls(); bindBench(root, redraw); $('#bch').innerHTML = ''; benchChart($('#bch')); }; redraw();
  }
  function benchmarks(root) {
    const b = v().benchmarks || {}, rows = b.rows || [], m = b.method || {};
    const cols = [['Value RM', 'value_myr', 2], ['Value USD', 'value_usd', 2], ['Total %', 'total_return_pct', 2], ['Monthly %', 'monthly_return_pct', 2], ['Weekly %', 'weekly_return_pct', 2], ['YTD %', 'ytd_return_pct', 2], ['Annualized %', 'annualized_pct', 2],
      ['Max DD %', 'max_drawdown_pct', 2], ['Sharpe', 'sharpe', 2], ['Sortino', 'sortino', 2], ['Vol %', 'volatility_pct', 2], ['Costs RM', 'costs_myr', 2], ['FX impact pp', 'fx_impact_pct', 2], ['Time invested %', 'time_invested_pct', 1], ['Cash % now', 'cash_pct_now', 1], ['Risk-adj (ret/|MDD|)', 'risk_adjusted', 2]];
    root.innerHTML = `<section class="wide"><h2>Benchmark comparison <span class="tag">AI account vs virtual SPY / QQQ (RM10,000 shadow portfolios)</span></h2><div id="bctl"></div><div id="bch" class="chart h340"></div>
    <div class="note ${/UNDERPERF/.test(b.note || '') ? 'bad' : ''}">${esc(b.note || '')}</div>
    <div class="tablewrap"><table><thead><tr><th class="l">Portfolio</th>${cols.map(c => `<th>${c[0]}</th>`).join('')}</tr></thead><tbody>${rows.map(r => `<tr><td class="l"><b>${esc(r.key)}</b> <span class="dim">${esc(r.name || '')}</span></td>${cols.map(c => `<td>${r.error ? '' : (r[c[1]] == null ? '<span class="dim">–</span>' : fmt(r[c[1]], c[2]))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
    ${rows.map(r => r.insufficient_history_note ? `<div class="note">${esc(r.key)}: ${esc(r.insufficient_history_note)}</div>` : '').join('')}</section>
    <section><h2>Method (shadow portfolios)</h2><div class="note">Virtual RM${fmt(b.start_value_myr, 0)} per benchmark, same start date as the experiment, never touching trading cash. ${esc(b.unit || '')}</div>
    ${Object.entries(m).map(([k, i]) => i && i.method ? `<div><b>${esc(k)}</b>: ${esc(i.method)}${i.error ? ' <span class="bad">' + esc(i.error) + '</span>' : ''}</div>` : (i && i.error ? `<div><b>${esc(k)}</b>: <span class="bad">${esc(i.error)}</span></div>` : '')).join('')}
    <div class="note">Exposure difference: the AI account holds cash by design (20% reserve, 80% max exposure, cash is a valid decision), so in a rising market it will lag a fully invested benchmark. This table states that plainly rather than hiding it.</div></section>`;
    const redraw = () => { $('#bctl').innerHTML = benchControls(); bindBench(root, redraw); $('#bch').innerHTML = ''; benchChart($('#bch')); }; redraw();
  }
  function health(root) {
    const s = v().scheduler || {}, md = v().market_data || {}, jobs = v().jobs || [];
    root.innerHTML = `<section><h2>Scheduler health <span class="tag ${sev(s.status || '')}">${esc(s.status)}</span></h2>
      <div>Market: <b>${esc(s.market)}</b></div><div>Last scheduled run: ${esc(myt((s.last_scheduled_run || {}).started_ts))} (${esc((s.last_scheduled_run || {}).job)}, ${esc((s.last_scheduled_run || {}).status)})</div>
      <div>Last successful run: ${esc(myt((s.last_successful_run || {}).started_ts))} (${esc((s.last_successful_run || {}).job)})</div><div>Last scan: ${esc(myt((s.last_scan || {}).started_ts))}</div>
      <div>Last reconciliation: ${esc(myt(s.last_reconciliation))}</div><div>Next expected monitor: ${esc(myt(s.next_expected_monitor_utc))}</div><div>Next expected scan: <b>${esc(s.next_expected_scan_myt)}</b></div>
      <div>Failed / skipped / success (7d): <b>${(s.counts_7d || {}).failed}</b> / <b>${(s.counts_7d || {}).skipped}</b> / <b>${(s.counts_7d || {}).success}</b></div><div>Missed runs (26h): <b class="${s.n_missed_26h ? 'bad' : 'ok'}">${s.n_missed_26h}</b></div>
      <div>Ledger writer mode: <b>${esc(s.writer_mode)}</b></div>
      <h3>Data freshness</h3>${kv(md, Object.keys(md).slice(0, 8))}<h3>Latest error</h3>${s.latest_error ? `<div class="errbox">${esc(JSON.stringify(s.latest_error)).slice(0, 400)}</div>` : '<div class="dim">none recorded</div>'}
      <div class="note">${esc(s.deployment_vs_engine)}</div><div class="note">${esc(s.cron_note)}</div></section>
    <section><h2>Job runs (heartbeat)</h2><div class="tablewrap tall"><table><thead><tr><th class="l">Started (MYT)</th><th class="l">Job</th><th>Trigger</th><th>Status</th><th class="l">Reason / error</th></tr></thead><tbody>${jobs.map(j => `<tr><td class="l">${esc(myt(j.started_ts))}</td><td class="l">${esc(j.job)}</td><td>${esc(j.trigger)}</td><td><span class="badge ${esc(String(j.status).toUpperCase())}">${esc(j.status)}</span></td><td class="l dim">${esc((j.skip_reason || j.error || '').slice(0, 140))}</td></tr>`).join('')}</tbody></table></div></section>
    <section class="wide"><h2>Recent data requests (provenance)</h2><div class="tablewrap">${table([{ h: 'Time', l: 1, v: r => r.ts, f: r => myt(r.ts) }, { h: 'Provider', l: 1, v: r => r.provider }, { h: 'Kind', v: r => r.kind }, { h: 'Symbols', v: r => r.n_symbols }, { h: 'Label', v: r => r.label }, { h: 'Complete', v: r => r.completeness, f: r => r.completeness == null ? '–' : fmt(r.completeness * 100, 0) + '%' }, { h: 'Stale', v: r => r.stale }, { h: 'Detail', l: 1, v: r => r.note || r.detail || '' }], v().data_requests || [], { id: 'dr' })}</div></section>`;
    bindTables(root, () => health(root));
  }
  function verification(root) {
    const x = v().verification || {}, rows = x.sessions || [], items = x.items || [];
    const mark = (r, k) => r[k] == null ? '<span class="dim">·</span>' : (k === 'errors' || k === 'stale_feeds' || k === 'missed_runs' ? (r[k] ? `<span class="bad">${r[k]}</span>` : '<span class="ok">0</span>') : (r[k] ? '<span class="ok">✓</span>' : '<span class="bad">✗</span>'));
    root.innerHTML = `<section class="wide"><h2>FORWARD TRADING VERIFICATION <span class="tag ${sev(x.status || '')}">${esc(x.status)}</span></h2>
      <div class="cards k"><div class="card"><div class="l">Qualifying consecutive sessions</div><div class="v">${x.consecutive_qualifying || 0} / ${x.required_sessions || 20}</div></div><div class="card"><div class="l">Total qualifying</div><div class="v">${x.total_qualifying || 0}</div></div>
      <div class="card"><div class="l">Failed sessions</div><div class="v ${x.failed_sessions ? 'bad' : ''}">${x.failed_sessions || 0}</div></div><div class="card"><div class="l">Start session</div><div class="v" style="font-size:14px">${esc(x.start_session || 'not yet')}</div><div class="s">first real NYSE session after v3 migration (${esc(myt(x.started_after_v3_migration_ts))})</div></div>
      <div class="card"><div class="l">Next session</div><div class="v" style="font-size:14px">${esc(x.next_session || '–')}</div></div></div>
      <div class="note"><b>${esc(x.honesty)}</b></div><div class="note" id="vbasis">${esc(x.counted_basis || '')} · scheduled (cron) runs recorded so far: <b>${x.scheduled_runs_recorded ?? 0}</b>.</div>
      <h3>Manual / push runs (shown separately, NOT counted)</h3>${(x.manual_runs_not_counted || []).length ? (x.manual_runs_not_counted || []).map(r => `<div class="dim">${esc(myt(r.started_ts))} · ${esc(r.job)} · event <b>${esc(r.trigger)}</b> · ${esc(r.status)} · run ${esc(r.run_id)}</div>`).join('') : '<div class="dim">none</div>'}<div class="note">A session with no trades still qualifies if every check completed. OPERATIONALLY VERIFIED needs ${x.required_sessions || 20} consecutive qualifying sessions of recorded evidence. It cannot be set manually.</div></section>
    <section class="wide"><h2>Session evidence (15 items per session)</h2>${rows.length ? `<div class="tablewrap"><table><thead><tr><th class="l">Session</th><th>Status</th>${items.map(i => `<th title="${esc(i)}">${esc(i.replace(/_/g, ' '))}</th>`).join('')}</tr></thead><tbody>${rows.slice().reverse().map(r => `<tr><td class="l">${esc(r.session_date)}</td><td><span class="badge ${esc(r.status)}">${esc(r.status)}</span></td>${items.map(i => `<td>${mark(r, i)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>` : '<div class="dim">No session recorded yet: the first qualifying session is the first real session after the v3 migration. Nothing is pre-filled.</div>'}</section>`;
  }
  function reports(root) {
    const d = v().daily_reports || [], w = v().weekly_eval, ai = v().ai || {};
    const money = (x, p = 'RM') => x == null ? '–' : p + fmt(x);
    root.innerHTML = aiPanel(ai) + `<section class="wide"><h2>Daily AI reports <span class="tag">${esc(ai.state || '')}</span></h2>${d.length ? d.map((r, i) => { const f = r.facts, p = f.portfolio; return `<details ${i === 0 ? 'open' : ''}><summary><b>${esc(f.session)}</b> · equity ${money(p.equity_myr)} · total P&amp;L ${money(p.total_pnl_myr)} (${pc(p.total_return_pct)}) · ${esc(r.commentary_mode)}</summary>
      <div class="memo">${esc(r.commentary)}</div>
      <div class="cards k"><div class="card"><div class="l">Value</div><div class="v">${money(p.equity_myr)}</div><div class="s">USD ${fmt(p.equity_usd)}</div></div><div class="card"><div class="l">Today P&amp;L</div><div class="v ${cls(p.today_pnl_myr)}">${money(p.today_pnl_myr)}</div></div><div class="card"><div class="l">Cash USD</div><div class="v">${fmt(p.cash_usd)}</div><div class="s">settled ${fmt(p.settled_cash_usd)} · unsettled ${fmt(p.unsettled_cash_usd)}</div></div><div class="card"><div class="l">Exposure</div><div class="v">${fmt(p.exposure_pct, 1)}%</div></div></div>
      <h3>Trades / order events</h3>${(f.trades_and_orders || []).map(t => `<div>${esc(t.symbol)} ${esc(t.side)} ${esc(t.qty)} · ${esc(t.status)} · ${esc(t.reason || '')}</div>`).join('') || '<div class="dim">none</div>'}
      <h3>Top opportunities (incl. rejected)</h3>${(f.top_opportunities_incl_rejected || []).map(t => `<div><b>${esc(t.symbol)}</b> ${esc(t.decision)} score ${fmt(t.score, 1)} ${t.entry ? '· entry ' + fmt(t.entry) + ' stop ' + fmt(t.stop) : ''} <span class="dim">${esc((t.rejected_because || []).join('; '))}</span></div>`).join('') || '<div class="dim">none</div>'}
      <h3>Risk review</h3><div class="dim">${esc(JSON.stringify(f.risk_review))}</div><h3>Operational report</h3><div class="dim">${esc(JSON.stringify(f.operational).slice(0, 600))}</div></details>`; }).join('') : '<div class="dim">Reports are generated at the end of each US session (the scan job). None stored yet.</div>'}</section>
    <section class="wide"><h2>Weekly evaluation <span class="tag">${esc((w || {}).week_ending_myt || 'none yet')}</span></h2>${w ? `<div class="note"><b>${esc(w.authority)}</b></div>
      <div class="cards k">${[['Trades', w.overall.n_trades], ['Win rate', w.overall.win_rate == null ? '–' : fmt(w.overall.win_rate * 100, 0) + '%'], ['Avg win $', fmt(w.overall.avg_win_usd)], ['Avg loss $', fmt(w.overall.avg_loss_usd)], ['Profit factor', w.overall.profit_factor == null ? 'n/a (insufficient sample)' : fmt(w.overall.profit_factor)], ['Expectancy $', fmt(w.overall.expectancy_usd)], ['Max DD', pc(w.account.max_drawdown_pct)], ['Exposure', fmt(w.account.exposure_pct, 1) + '%']].map(c => card(c[0], c[1])).join('')}</div>
      ${(w.overall.notes || [w.overall.note]).filter(Boolean).map(n => `<div class="note">${esc(n)}</div>`).join('')}<h3>Proposals (no live parameter is changed automatically)</h3>${(w.proposals || []).map(p => `<div><b>${esc(p.proposal)}</b> <span class="dim">${esc(p.why)} · ${esc(p.status)}</span></div>`).join('') || '<div class="dim">none</div>'}` : '<div class="dim">Generated Saturdays by the weekly job.</div>'}</section>`;
  }
  function migration(root) {
    const m = v().migration || {}, a = v().archive_v2 || [], pol = (v().policy || {}), fl = pol.flags || {};
    const snap = (a[0] || {}).payload || {};
    root.innerHTML = `<section class="wide"><h2>v3 migration &amp; strategy policy</h2><div class="note">Migration applied: <b>${esc(myt(m.applied_ts))}</b> · backup: ${esc(m.backup || 'see docs/MIGRATION_V3.md')}</div>
      <h3>Strategy flags</h3>${Object.values(fl).map(f => `<div><b>${esc(f.strategy_id)}</b>: ${f.new_entries_enabled ? '<span class="ok">NEW ENTRIES ENABLED</span>' : '<span class="bad">NEW ENTRIES DISABLED</span>'} — ${esc(f.label || '')}<div class="dim">${esc(f.explanation || '')}</div></div>`).join('')}
      <h3>Proposed migration actions (owner approval required — nothing is sold automatically)</h3>${(m.proposals || []).length ? (m.proposals || []).map(p => `<div class="errbox">#${p.id} ${esc(p.kind)} ${esc(p.strategy_id)} ${esc(p.symbol)} — <b>${esc(p.status)}</b><br>${esc(String(p.detail).slice(0, 300))}</div>`).join('') : '<div class="dim">none</div>'}
      <h3>Migration log (append-only)</h3><div class="tablewrap tall">${table([{ h: 'Time', l: 1, v: r => r.ts, f: r => myt(r.ts) }, { h: 'Step', l: 1, v: r => r.step }, { h: 'Detail', l: 1, v: r => String(r.detail).slice(0, 220) }], m.log || [], { id: 'mlog' })}</div></section>
    <section class="wide"><h2>Archived v2.0 account (read-only snapshot taken at migration)</h2><div class="note">Full v2 history stays in the append-only ledger and in git tag/branch history. This is the account state immediately BEFORE the v3 journal entries.</div><pre class="memo">${esc(JSON.stringify(snap, null, 1).slice(0, 6000))}</pre></section>`;
    bindTables(root, () => migration(root));
  }
  return { overview, benchmarks, health, verification, reports, migration, strip };
})();
window.V3VIEWS = { overview: V3.overview, benchmarks: V3.benchmarks, health: V3.health, verification: V3.verification, reports: V3.reports, migration: V3.migration };
