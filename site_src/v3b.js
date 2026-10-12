/* v3 Strategy Lab wrapper: separates live forward paper / research-only / historical backtest / shadow benchmarks. Loaded after app.js. */
(() => {
  const base = window.V3VIEWS || {};
  base.strategies = root => {
    viewStrategies(root); const d = AITL.S.d, v = d.v3 || {}, sv = d.strategies || [], fl = (v.policy || {}).flags || {}, bt = ((d.validation || {}).backtest) || {}, b = v.benchmarks || {};
    const sw = sv.find(x => x.id === 'swing') || {}, mr = sv.find(x => x.id === 'meanrev') || {}, bl = sv.find(x => x.id === 'baseline') || {}, it = sv.find(x => x.id === 'intraday') || {};
    const row = x => `<div class="card"><div class="l">${esc(x.name)}</div><div class="v">RM${fmt((x.sleeve || {}).equity_myr)}</div><div class="s">${(x.metrics || {}).n_trades || 0} closed trades · alloc ${fmt(x.allocation_pct, 0)}%</div></div>`;
    const sec = `<section class="wide"><h2>Strategy Lab <span class="tag">4 separate evidence classes — never mixed</span></h2>
      <h3><span class="chip ok">FORWARD PAPER</span> live forward paper strategy (the only one allowed to open trades)</h3><div class="cards k">${row(sw)}</div><div class="note">${esc((fl.swing || {}).explanation || 'Primary strategy: 100% of the active trading pool; max 80% exposure, 20% cash reserve, 20% per stock, 1% risk per trade, max 5 positions, no leverage/shorts/derivatives.')}</div>
      <h3><span class="chip bad">RESEARCH ONLY</span> ${esc((fl.meanrev || {}).label || 'RESEARCH ONLY — NEW TRADES DISABLED')}</h3><div class="note">${esc((fl.meanrev || {}).explanation || '')} Existing positions (if any) keep protective monitoring and exits. Reactivation needs owner approval + validation (see Controls / docs).</div><div class="cards k">${row(mr)}</div>
      <div class="note">Intraday: ${esc(it.label || 'research-only, disabled; no fabricated minute candles; owner enable required')}</div>
      <h3><span class="chip mid">HISTORICAL BACKTEST</span> in-sample/out-of-sample research (NOT forward evidence)</h3><div class="note">${esc(bt.note || bt.label || 'Backtests live in validation/results.json; see the Validation tab. Optimized backtests are not independent validation.')}</div>
      <h3><span class="chip">SHADOW BENCHMARK</span> virtual SPY / QQQ RM10,000 (not trading capital)</h3><div class="cards k">${(b.rows || []).filter(r => r.key !== 'AI').map(r => card(r.key + ' shadow', r.value_myr == null ? '–' : 'RM ' + fmt(r.value_myr), r.total_return_pct == null ? esc(r.error || '') : pc(r.total_return_pct) + ' since start', cls(r.total_return_pct))).join('')}</div></section>`;
    root.insertAdjacentHTML('afterbegin', sec);
  };
  base.validation = root => {
    viewValidation(root); const v = AITL.S.d.validation || {}, c = v.cadence_study, m = v.membership_check; let h = '';
    if (c) {
      const A = c.arms || {}, rows = Object.keys(A).map(k => { const o = A[k]['OOS 2024-'] || {}, i = A[k]['IS 2021-2023'] || {}, f = A[k]['FULL'] || {}; return `<tr><td class="l">${esc(k)}</td><td>${o.n_trades ?? '–'}</td><td>${fmt(i.ret_myr_pct_after_exit_fx)}%</td><td>${fmt(o.ret_myr_pct_after_exit_fx)}%</td><td>${fmt(o.max_dd_pct)}%</td><td>${o.sharpe == null ? '–' : fmt(o.sharpe)}</td><td>$${fmt(o.costs_usd)}</td></tr>`; }).join('');
      const d = c.decision || {};
      h += `<section class="wide"><h2>Entry cadence study <span class="tag warn">HISTORICAL BACKTEST</span></h2><div class="note">${esc(c.decision_rule)}</div><div class="tablewrap"><table><thead><tr><th class="l">Cadence</th><th>OOS trades</th><th>IS net ret (MYR)</th><th>OOS net ret (MYR)</th><th>OOS max DD</th><th>OOS Sharpe</th><th>OOS costs</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="note"><b>Decision: default cadence = ${esc(d.default_cadence)}.</b> ${esc(d.statement)} ${['daily', 'breakout_daily'].map(k => d[k] ? esc(k) + ': ' + Object.entries(d[k].checks).map(([n, ok]) => (ok ? '✓ ' : '✗ ') + n).join(', ') : '').join(' | ')}</div>
      <div class="note">${(c.limitations || []).slice(-2).map(esc).join(' ')}</div></section>`;
    }
    if (m) h += `<section class="wide"><h2>Survivorship / index-membership exposure (measured, not fixed)</h2><div class="note">${esc(m.reading)}</div><div>Current S&amp;P 500 members: ${m.current_members}; added after ${esc(m.backtest_start)}: <b>${m.current_members_added_after_start}</b> (${m.pct_added_after_start}%). ${esc(m.removed_names_note)}</div></section>`;
    root.insertAdjacentHTML('beforeend', h);
  };
  window.V3VIEWS = base;
})();
