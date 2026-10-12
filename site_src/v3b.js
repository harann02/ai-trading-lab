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
  window.V3VIEWS = base;
})();
