/* v3 Controls tab: the public Pages site is READ-ONLY. There is NO token input and no GitHub API call from this site (v2's browser-PAT flow was removed in v3).
   Owner changes are made through GitHub itself (authenticated by GitHub, needs repo write access): Actions "Run workflow" inputs, or editing control/commands.json on branch data,
   or the self-hosted authenticated dashboard. This tab only DISPLAYS current settings + status history and BUILDS the JSON to paste. */
const Controls = (() => {
  const REPO = 'harann02/ai-trading-lab';
  const WF = `https://github.com/${REPO}/actions/workflows/run.yml`, FILE = `https://github.com/${REPO}/edit/data/control/commands.json`;
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const TEMPLATES = {
    'Change a setting': { settings_json: '{"max_positions": 4}', command_json: '' },
    'Resume entries after drawdown halt': { settings_json: '', command_json: '{"type":"resume_entries"}' },
    'Keep a retired-sleeve holding (migration proposal)': { settings_json: '', command_json: '{"type":"decide_migration_proposal","proposal_id":1,"decision":"KEEP"}' },
    'Approve selling a retired-sleeve holding': { settings_json: '', command_json: '{"type":"decide_migration_proposal","proposal_id":1,"decision":"APPROVED_SELL"}' },
    'Reactivate mean reversion (needs validation to pass)': { settings_json: '', command_json: '{"type":"reactivate_strategy","strategy":"meanrev","confirm":"REACTIVATE meanrev"}' },
    'Start fresh RM10,000 experiment (archives current account)': { settings_json: '', command_json: '{"type":"start_v3_experiment","confirm":"START NEW EXPERIMENT"}' },
  };
  function render() {
    const root = document.querySelector('#view'); if (!root) return; const d = (window.AITL && AITL.S.d) || {}, c = d.control || {}, s = c.settings || {}, v3 = d.v3 || {};
    const applied = (c.applied || []).slice(0, 15), pend = c.pending_orders || [];
    const stat = x => `<span class="badge ${esc(String(x).toUpperCase())}">${esc(x)}</span>`;
    root.innerHTML = `<section class="wide"><h2>Controls <span class="tag paper">PUBLIC SITE = READ-ONLY</span></h2>
    <div class="note">This page cannot change anything and never asks for a GitHub token. Owner controls go through GitHub (which authenticates you) or the self-hosted dashboard:</div>
    <ol><li><b>GitHub Actions UI (recommended, free):</b> <a href="${WF}" target="_blank" rel="noopener">Actions → scan-monitor-deploy → Run workflow</a>; paste the JSON below into <code>settings_json</code> / <code>command_json</code>. Only users with write access can run it; the engine re-validates and audits every change.</li>
    <li><b>Edit file:</b> <a href="${FILE}" target="_blank" rel="noopener">control/commands.json on branch data</a> (applied at the next run).</li>
    <li><b>Self-hosted dashboard</b> (auth + CSRF + rate limit; writer mode must be switched to <code>selfhosted</code>): see docs/DEPLOY.md. Not active unless you deploy it.</li></ol>
    <div class="note">Ledger writer mode now: <b>${esc((v3.security || {}).writer_mode || 'actions')}</b> (exactly one writer is allowed at a time).</div></section>
    <section><h2>Command builder (copy &amp; paste, nothing is sent)</h2><div class="row"><select id="tpl">${Object.keys(TEMPLATES).map(k => `<option>${esc(k)}</option>`).join('')}</select></div>
    <label class="dim">settings_json</label><textarea id="sj" spellcheck="false"></textarea><label class="dim">command_json</label><textarea id="cj" spellcheck="false"></textarea>
    <div class="row"><button class="btn" id="cp">Copy both</button><a class="btn" href="${WF}" target="_blank" rel="noopener">Open Run workflow ↗</a><span class="dim" id="cpm"></span></div>
    <div class="note">Safety rules enforced by the engine regardless of what is pasted: retired / research-only strategies cannot receive capital; reactivation needs validation conditions AND the exact confirmation text; new experiment needs the exact confirmation; no leverage / shorts; allocation sums ≤ 100%.</div></section>
    <section><h2>Current settings (read-only)</h2><div class="tablewrap"><table><tbody>${Object.entries(s).filter(([k]) => k !== 'strategies').map(([k, v]) => `<tr><td class="l">${esc(k)}</td><td class="l">${esc(JSON.stringify(v))}</td></tr>`).join('')}</tbody></table></div>
    <h3>Sleeve allocations</h3>${Object.entries(s.strategies || {}).map(([k, v]) => `<div>${esc(k)}: ${esc(v.allocation_pct)}% · ${v.enabled ? 'enabled' : 'disabled'}</div>`).join('')}</section>
    <section><h2>Change history (pending / accepted / rejected / applied)</h2>${applied.length ? `<div class="tablewrap"><table><thead><tr><th class="l">Time</th><th class="l">Id</th><th>Status</th><th class="l">Detail</th></tr></thead><tbody>${applied.map(a => `<tr><td class="l">${esc(a.ts)}</td><td class="l">${esc(a.command_id)}</td><td>${stat(a.status || 'applied')}</td><td class="l dim">${esc(String(a.detail || '').slice(0, 160))}</td></tr>`).join('')}</tbody></table></div>` : '<div class="dim">no owner changes yet</div>'}
    <h3>Pending orders awaiting execution</h3>${pend.length ? pend.map(o => `<div>${esc(o.symbol)} ${esc(o.side)} ${esc(o.qty)} · ${esc(o.strategy_id)} · eligible ${esc(o.eligible_session || '–')}</div>`).join('') : '<div class="dim">none</div>'}
    <div class="note">A requested change is PENDING until the next workflow run validates it (ACCEPTED/APPLIED or REJECTED with a reason, shown above and in the Activity audit).</div></section>`;
    const sel = document.querySelector('#tpl'), sj = document.querySelector('#sj'), cj = document.querySelector('#cj');
    const fill = () => { const t = TEMPLATES[sel.value]; sj.value = t.settings_json; cj.value = t.command_json; }; sel.onchange = fill; fill();
    document.querySelector('#cp').onclick = async () => { try { await navigator.clipboard.writeText(`settings_json:\n${sj.value}\n\ncommand_json:\n${cj.value}`); document.querySelector('#cpm').textContent = 'copied'; } catch (e) { document.querySelector('#cpm').textContent = 'select & copy manually'; } };
  }
  return { render, isOwner: () => false, onData: () => {} };
})();
