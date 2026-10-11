/* Owner controls for the static GitHub Pages site. NO secrets here.
   The owner pastes a GitHub token in the browser (kept in sessionStorage only, never stored in the repo, cleared when the tab closes).
   Commands are written to control/commands.json on the `data` branch via the GitHub Contents API, then the Actions workflow is dispatched; the engine re-validates
   everything at the next run (server-side), applies it and logs it in the ledger audit. Non-owners (or no token) see a read-only page. */
const Controls = (() => {
  const API = 'https://api.github.com', K = 'aitl_gh_token';
  const st = { owner: false, login: null, err: null, busy: false, msg: null, pendingRev: null, pendingCmds: [], sha: null, doc: null };
  const tok = () => sessionStorage.getItem(K);
  const gh = async (path, opt = {}) => { const r = await fetch(API + path, { ...opt, headers: { Accept: 'application/vnd.github+json', Authorization: 'Bearer ' + tok(), 'X-GitHub-Api-Version': '2022-11-28', ...(opt.headers || {}) } }); if (!r.ok && !opt.ok404) { let t = ''; try { t = (await r.json()).message } catch (e) { } throw new Error(r.status + ' ' + t); } return r; };
  const meta = () => (window.AITL && AITL.S.d.meta) || {};
  const repo = () => meta().repo || 'harann02/ai-trading-lab';
  const b64 = s => btoa(unescape(encodeURIComponent(s))), unb64 = s => decodeURIComponent(escape(atob(s.replace(/\n/g, ''))));
  const uid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));

  async function verify() {
    st.err = null; st.owner = false;
    if (!tok()) return;
    try {
      const u = await (await gh('/user')).json(); st.login = u.login;
      const r = await (await gh('/repos/' + repo())).json();
      st.owner = !!(r.permissions && r.permissions.push) && u.login.toLowerCase() === repo().split('/')[0].toLowerCase();
      if (!st.owner) st.err = 'Signed in as ' + u.login + ' but this account is not the repository owner with write access: read-only.';
    } catch (e) { st.err = 'Token rejected: ' + e.message; sessionStorage.removeItem(K); }
  }
  async function readDoc() {
    const r = await gh('/repos/' + repo() + '/contents/' + (meta().control_path || 'control/commands.json') + '?ref=' + (meta().control_branch || 'data'), { ok404: true });
    if (r.status === 404) { st.sha = null; st.doc = { version: 1, settings_rev: null, settings: {}, commands: [] }; return; }
    const j = await r.json(); st.sha = j.sha; let txt = unb64(j.content || '').trim(); let d = {}; try { d = txt ? JSON.parse(txt) : {}; } catch (e) { d = {}; }
    st.doc = { version: 1, settings_rev: null, settings: {}, commands: [], ...d };
  }
  async function writeDoc(doc, message) {
    doc.updated_by = st.login; doc.updated_at = new Date().toISOString(); doc.version = 1; doc.commands = (doc.commands || []).slice(-50);
    const body = { message, content: b64(JSON.stringify(doc, null, 1)), branch: meta().control_branch || 'data' }; if (st.sha) body.sha = st.sha;
    const r = await gh('/repos/' + repo() + '/contents/' + (meta().control_path || 'control/commands.json'), { method: 'PUT', body: JSON.stringify(body) }); const j = await r.json(); st.sha = j.content.sha; st.doc = doc;
  }
  async function dispatch(action) {
    await gh('/repos/' + repo() + '/actions/workflows/' + (meta().workflow || 'run.yml') + '/dispatches', { method: 'POST', body: JSON.stringify({ ref: 'main', inputs: { action } }) });
  }
  async function submit({ settings, command, action = 'control', label }) {
    if (!st.owner || st.busy) return; st.busy = true; st.msg = null; AITL.S.applying = true; AITL.chips(); render();
    try {
      await readDoc(); const doc = st.doc;
      if (settings) { doc.settings = { ...(doc.settings || {}), ...settings }; if (settings.strategies) doc.settings.strategies = { ...((st.doc.settings || {}).strategies || {}), ...settings.strategies }; doc.settings_rev = uid(); st.pendingRev = 'settings:' + doc.settings_rev; }
      if (command) { command.id = uid(); command.ts = new Date().toISOString(); doc.commands = [...(doc.commands || []), command]; st.pendingCmds.push(command.id); }
      await writeDoc(doc, 'control: ' + (label || (command && command.type) || 'settings') + ' (via dashboard)');
      await dispatch(action); st.msg = { ok: true, t: 'Saved to the data branch and workflow dispatched – waiting for the engine to apply it (updates happen on workflow runs, typically 1–3 minutes)…' };
      watch();
    } catch (e) { st.msg = { ok: false, t: 'Failed: ' + e.message }; AITL.S.applying = false; }
    st.busy = false; AITL.chips(); render();
  }
  let timer = null;
  function watch() { clearInterval(timer); let n = 0; timer = setInterval(async () => {
    n++; await AITL.poll(true);
    const ap = ((AITL.S.d.control || {}).applied || []); const ids = [...st.pendingCmds, ...(st.pendingRev ? [st.pendingRev] : [])];
    const done = ids.filter(i => ap.find(a => a.command_id === i)); const bad = done.filter(i => ap.find(a => a.command_id === i).status !== 'applied');
    if (ids.length && done.length === ids.length) { clearInterval(timer); AITL.S.applying = false; st.pendingCmds = []; st.pendingRev = null;
      st.msg = bad.length ? { ok: false, t: 'Engine REJECTED: ' + bad.map(i => ap.find(a => a.command_id === i).detail).join(' | ') } : { ok: true, t: 'Applied by the engine and logged in the audit trail ✓ (' + new Date().toLocaleTimeString() + ')' }; AITL.chips(); render(); }
    if (n > 60) { clearInterval(timer); AITL.S.applying = false; st.msg = { ok: false, t: 'Still not applied after 10 minutes – check the Actions tab on GitHub (queued/failed run?).' }; AITL.chips(); render(); }
  }, 10000); }

  function render(root) {
    root = root || document.getElementById('view'); if (!root || (AITL.S.tab !== 'controls')) return;
    const c = (AITL.S.d.control || {}), s = c.settings || {}, strat = s.strategies || {};
    const login = `<section class="wide"><h2>Owner sign-in <span class="tag ${st.owner ? '' : 'warn'}">${st.owner ? 'OWNER: controls enabled' : 'READ-ONLY'}</span></h2>
    <div class="note">This page is a public static site: everybody can read. Only the repository owner can change anything. Paste a GitHub <b>fine-grained token</b> for repo <code>${repo()}</code> with <b>Contents: read/write</b> and <b>Actions: read/write</b> (or a classic token with <code>repo</code>+<code>workflow</code>). It stays in this tab's sessionStorage only – never in the repo, never sent anywhere except api.github.com.</div>
    <div class="row">${st.owner ? `<span class="ok">Signed in as ${st.login}</span> <button id="logout">Sign out (clear token)</button>` : `<input id="tok" type="password" placeholder="GitHub token" style="width:280px" autocomplete="off"> <button id="login">Verify &amp; sign in</button>`}</div>
    ${st.err ? `<div class="errbox">${st.err}</div>` : ''}</section>`;
    const msg = st.msg ? `<div class="${st.msg.ok ? 'ok' : 'errbox'}" style="margin:6px 0">${st.msg.t}</div>` : '';
    const dis = st.owner ? '' : 'disabled';
    const sr = ['swing', 'meanrev', 'baseline'].map(id => `<tr><td class="l">${id}</td><td><input type="checkbox" data-en="${id}" ${(strat[id] || {}).enabled ? 'checked' : ''} ${dis}></td><td><input type="number" min="0" max="100" step="1" data-al="${id}" value="${(strat[id] || {}).allocation_pct ?? 0}" style="width:70px" ${dis}> %</td></tr>`).join('');
    const num = (k, l, step = 0.1, mx = 100) => `<label>${l}<input type="number" step="${step}" min="0" max="${mx}" data-k="${k}" value="${s[k] ?? ''}" ${dis}></label>`;
    const chk = (k, l) => `<label>${l}<select data-b="${k}" ${dis}><option value="true" ${s[k] ? 'selected' : ''}>ON</option><option value="false" ${s[k] ? '' : 'selected'}>OFF</option></select></label>`;
    const pend = (c.pending_orders || []).map(o => `<tr><td>#${o.id}</td><td>${o.strategy_id}</td><td>${o.symbol}</td><td>${o.qty}</td><td>$${(+o.ref_price).toFixed(2)}</td><td>${o.needs_approval ? (o.approved ? 'approved' : 'awaiting approval') : 'auto'}</td>
      <td><button data-ap="${o.id}" ${dis}>Approve</button> <button data-cx="${o.id}" ${dis}>Cancel</button></td></tr>`).join('');
    root.innerHTML = login + `<section class="wide"><h2>Live controls <span class="tag">applied on the next workflow run</span> ${AITL.S.applying ? '<span class="applying">applying…</span>' : ''}</h2>${msg}
    <div class="note">Flow: browser → <code>control/commands.json</code> on branch <code>data</code> (GitHub Contents API) → <code>workflow_dispatch</code> → engine validates, applies, audits → site re-published → this page auto-refreshes (last update ${new Date((meta().generated_utc || 0)).toLocaleTimeString()}). Not real-time ticks; data is delayed yfinance.</div>
    <h3>Strategies (enable/disable independently; allocations must sum ≤ 100%)</h3><table style="max-width:420px"><thead><tr><th class="l">Strategy</th><th>Enabled</th><th>Allocation</th></tr></thead><tbody>${sr}
    <tr><td class="l">intraday</td><td colspan="2" class="dim">locked OFF: no real-time feed (see Strategies tab)</td></tr></tbody></table>
    <h3>Risk &amp; automation</h3><div class="form">${num('risk_per_trade_pct', 'Risk per trade %', 0.1, 5)}${num('max_position_pct', 'Max single stock %', 1)}${num('max_exposure_pct', 'Max exposure %', 1)}${num('min_cash_pct', 'Cash reserve %', 1)}${num('max_positions', 'Max positions', 1, 10)}${num('max_drawdown_pct', 'Drawdown halt %', 0.5, 50)}
    ${chk('automation_on', 'Automation (auto orders)')}${chk('manual_approval', 'Manual approval of orders')}${chk('fractional_shares', 'Fractional shares')}</div>
    <div class="form"><label>Extra watchlist (comma separated tickers; still subject to every filter)<input data-l="extra_watchlist" value="${(s.extra_watchlist || []).join(',')}" ${dis}></label><label>Blocked symbols<input data-l="blocked_symbols" value="${(s.blocked_symbols || []).join(',')}" ${dis}></label></div>
    <div class="row"><button id="save" ${dis}>Save settings &amp; apply</button><button id="scannow" ${dis}>Run full scan now</button><button id="monnow" ${dis}>Refresh / monitor now</button></div>
    <div id="verr" class="errbox hidden"></div>
    <h3>Pending orders (manual approval)</h3>${pend ? `<table><thead><tr><th>#</th><th>Strategy</th><th>Symbol</th><th>Qty</th><th>Ref</th><th>State</th><th></th></tr></thead><tbody>${pend}</tbody></table>` : '<div class="dim">none pending</div>'}
    <h3>Drawdown halt</h3>${s.entries_suspended ? `<div class="errbox">Entries suspended: ${s.suspend_reason}</div><div class="row"><input id="rtxt" placeholder="type RESUME" style="width:130px" ${dis}><button id="resume" ${dis}>Resume entries</button></div>` : '<div class="ok">not halted</div>'}
    <h3>Danger zone: reset paper account</h3><div class="note">Archives the whole ledger DB first, then restarts at RM10,000. Type RESET to confirm.</div><div class="row"><input id="rst" placeholder="type RESET" style="width:130px" ${dis}><button id="reset" ${dis}>Reset account</button></div></section>
    <section class="wide"><h2>Recently applied</h2>${(c.applied || []).slice(0, 10).map(a => `<div><span class="dim">${a.ts}</span> <b class="${a.status === 'applied' ? 'ok' : 'bad'}">${a.status}</b> ${a.command_id.slice(0, 40)} <span class="dim">${String(a.detail).slice(0, 140)}</span></div>`).join('') || '<div class="dim">nothing applied yet</div>'}</section>`;
    const $ = s => root.querySelector(s);
    if ($('#login')) $('#login').onclick = async () => { sessionStorage.setItem(K, $('#tok').value.trim()); await verify(); render(); AITL.chips(); };
    if ($('#logout')) $('#logout').onclick = () => { sessionStorage.removeItem(K); st.owner = false; st.login = null; render(); AITL.chips(); };
    if (!st.owner) return;
    $('#save').onclick = () => {
      const p = { strategies: {} }; let tot = 0;
      root.querySelectorAll('[data-en]').forEach(e => { p.strategies[e.dataset.en] = { enabled: e.checked }; });
      root.querySelectorAll('[data-al]').forEach(e => { const v = +e.value; p.strategies[e.dataset.al].allocation_pct = v; tot += v; });
      root.querySelectorAll('[data-k]').forEach(e => { if (e.value !== '') p[e.dataset.k] = +e.value; });
      root.querySelectorAll('[data-b]').forEach(e => p[e.dataset.b] = e.value === 'true');
      root.querySelectorAll('[data-l]').forEach(e => p[e.dataset.l] = e.value.split(',').map(x => x.trim().toUpperCase()).filter(Boolean));
      const errs = []; if (tot > 100) errs.push('allocations sum to ' + tot + '% (>100%)'); if (p.risk_per_trade_pct > 5) errs.push('risk per trade max 5%'); if (p.max_positions < 1 || p.max_positions > 10) errs.push('max positions 1-10');
      (p.extra_watchlist || []).concat(p.blocked_symbols || []).forEach(t => { if (!/^[A-Z]{1,5}([.-][A-Z])?$/.test(t)) errs.push('bad ticker ' + t); });
      const ve = $('#verr'); if (errs.length) { ve.textContent = 'Not sent (client check): ' + errs.join('; ') + '. The engine validates again server-side.'; ve.classList.remove('hidden'); return; } ve.classList.add('hidden');
      submit({ settings: p, label: 'settings' });
    };
    $('#scannow').onclick = () => submit({ action: 'scan', label: 'run scan' }); $('#monnow').onclick = () => submit({ action: 'monitor', label: 'monitor' });
    root.querySelectorAll('[data-ap]').forEach(b => b.onclick = () => submit({ command: { type: 'approve_order', order_id: +b.dataset.ap }, label: 'approve order' }));
    root.querySelectorAll('[data-cx]').forEach(b => b.onclick = () => submit({ command: { type: 'cancel_order', order_id: +b.dataset.cx }, label: 'cancel order' }));
    if ($('#resume')) $('#resume').onclick = () => { if ($('#rtxt').value !== 'RESUME') { st.msg = { ok: false, t: 'Type RESUME exactly.' }; return render(); } submit({ command: { type: 'resume_entries', confirm: 'RESUME' }, label: 'resume entries' }); };
    $('#reset').onclick = () => { if ($('#rst').value !== 'RESET') { st.msg = { ok: false, t: 'Type RESET exactly to confirm.' }; return render(); } submit({ command: { type: 'reset_account', confirm: 'RESET' }, label: 'reset account' }); };
  }
  if (tok()) verify().then(() => { if (window.AITL) { AITL.chips(); render(); } });
  return { render, isOwner: () => st.owner, onData: () => { if (window.AITL && AITL.S.tab === 'controls' && !document.activeElement.matches('input,select')) render(); } };
})();
