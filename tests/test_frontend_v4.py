"""v4 beginner UI: glossary coverage, every card/heading gets a non-generic info entry, glance from real data, Learn tab, no JS console errors (playwright). Presentation only."""
import os, sys, re, json, subprocess, threading, functools, http.server, socketserver
import pytest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = next((p for p in ("/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser") if os.path.exists(p)), None)
TABS = ["overview", "learn", "discovery", "positions", "benchmarks", "strategies", "research", "reports", "activity", "health", "verification", "migration", "validation", "controls"]
REQUIRED = ["paper", "equity", "cash", "drawdown", "today_pnl", "total_pnl", "fx", "benchmark", "shadow", "swing", "meanrev", "signals", "orders",
            "verification", "ai", "scheduler", "marketdata", "backtest", "glance", "winrate"]

@pytest.fixture(scope="module")
def site(tmp_path_factory):
    d = tmp_path_factory.mktemp("fe4"); env = {**os.environ, "AITL_DB": str(d / "fe.db"), "AITL_LLM_DISABLED": "1"}
    out = d / "site"; r = subprocess.run([sys.executable, os.path.join(ROOT, "export_static.py"), str(out)], env=env, capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-800:]; return out

def test_glossary_file_exported_and_complete(site):
    assert (site / "glossary.js").exists() and (site / "help.js").exists()
    g = (site / "glossary.js").read_text(); assert "window.GLOSSARY" in g
    for k in REQUIRED: assert f"k: '{k}'" in g or f'k: "{k}"' in g, k
    assert all(x in g for x in ("what:", "read:", "good:", "match:"))

def test_glossary_matches_key_labels():
    """Regression: labels must map to the right explanation (e.g. 'Scheduler health' is not 'US market')."""
    import shutil
    node = shutil.which("node")
    if not node: pytest.skip("no node")
    js = "global.window={};eval(require('fs').readFileSync(%r,'utf8'));const f=t=>{for(const g of window.GLOSSARY)if(new RegExp(g.match,'i').test(t))return g.k};" % os.path.join(ROOT, "site_src", "glossary.js")
    cases = {"Scheduler health": "scheduler", "US market": "market", "Market data as of": "marketdata", "Portfolio (MYR)": "equity", "Max drawdown": "drawdown", "AI engine": "ai", "Total P&L": "total_pnl", "Win rate": "winrate", "SPY shadow": "shadow", "FORWARD TRADING VERIFICATION": "verification"}
    js += "console.log(JSON.stringify(%s.map(f)))" % json.dumps(list(cases))
    out = json.loads(subprocess.run([node, "-e", js], capture_output=True, text=True, check=True).stdout)
    assert out == list(cases.values()), dict(zip(cases, out))

def test_learn_tab_and_help_wired(site):
    html = (site / "index.html").read_text()
    assert 'data-t="learn"' in html and "glossary.js" in html and "help.js" in html and "Learn the basics" in html
    h = (site / "help.js").read_text()
    assert "Today at a glance" in h and "aria-expanded" in h and "Escape" in h and "helppop" in h

def test_every_card_has_info_entry(site):
    """Every card must get an ⓘ whose glossary key is not the generic fallback."""
    pw = pytest.importorskip("playwright.sync_api")
    if CHROME is None: pytest.skip("no chrome")
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a): pass
    srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(site))); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pw.sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            for vp in ({"width": 1280, "height": 900}, {"width": 390, "height": 844}):
                pg = b.new_page(viewport=vp); errs = []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "favicon" not in m.text else None)
                for t in TABS:
                    pg.goto(f"http://127.0.0.1:{port}/#{t}"); pg.reload(); pg.wait_for_selector("#view section", timeout=10000); pg.wait_for_timeout(400)
                    bad = pg.evaluate("""() => [...document.querySelectorAll('#view .card')].filter(c => {
                      const i = c.querySelector('.info'); return !i || i.dataset.k === 'general' || c.dataset.help === 'general';
                    }).map(c => (c.querySelector('.l')||c).innerText.slice(0,60))""")
                    assert not bad, (t, vp["width"], bad[:5])
                    assert pg.evaluate("document.documentElement.scrollWidth") <= vp["width"] + 20
                    if t == "overview":
                        assert pg.query_selector("#glance"), "Today at a glance missing"
                        gtxt = pg.inner_text("#glance"); assert "practice account" in gtxt.lower() and "RM" in gtxt
                    if t == "learn":
                        assert "Learn the basics" in pg.inner_text("#view") and len(pg.query_selector_all(".learn .term")) >= 20
                # tooltip open / Escape close
                pg.goto(f"http://127.0.0.1:{port}/#overview"); pg.reload(); pg.wait_for_selector("#glance button.info", timeout=8000)
                pg.click("#glance button.info"); pg.wait_for_selector("#helppop:not(.hidden)"); assert "What it is" in pg.inner_text("#helppop")
                pg.keyboard.press("Escape"); assert pg.evaluate("document.querySelector('#helppop').classList.contains('hidden')")
                assert not errs, errs[:5]; pg.close()
            b.close()
    finally:
        srv.shutdown()
