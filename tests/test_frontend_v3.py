"""Frontend tests (headless Chrome via playwright): every tab renders without JS errors, desktop+mobile, figures match backend JSON, no token input, no secrets in exported snapshot.
Skips if chrome/playwright are unavailable (CI installs playwright chromium)."""
import os, sys, re, json, subprocess, threading, functools, http.server, socketserver, shutil
import pytest
sys.path.insert(0, os.path.dirname(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = next((p for p in ("/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser") if os.path.exists(p)), None)
TABS = ["overview", "discovery", "positions", "benchmarks", "strategies", "research", "reports", "activity", "health", "verification", "migration", "validation", "controls"]

@pytest.fixture(scope="module")
def site(tmp_path_factory):
    d = tmp_path_factory.mktemp("fe"); env = {**os.environ, "AITL_DB": str(d / "fe.db"), "AITL_LLM_DISABLED": "1"}
    out = d / "site"; r = subprocess.run([sys.executable, os.path.join(ROOT, "export_static.py"), str(out)], env=env, capture_output=True, text=True, cwd=ROOT); assert r.returncode == 0, r.stderr[-800:]
    return out

def test_snapshot_contains_no_secrets_or_token_ui(site):
    bad = re.compile(r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gsk_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_\-]{30,}|BEGIN [A-Z ]*PRIVATE KEY)")
    for p in site.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".js", ".html", ".css", ".csv", ".md"):
            t = p.read_text(errors="ignore"); assert not bad.search(t), p
    js = (site / "controls.js").read_text() + (site / "app.js").read_text() + (site / "v3.js").read_text()
    assert "sessionStorage" not in js and "Authorization" not in js and "api.github.com" not in js and 'id="tok"' not in js          # v3: no browser-PAT flow

def test_v3_json_matches_backend(site):
    v3 = json.loads((site / "data" / "v3.json").read_text()); k = json.loads((site / "data" / "kpis.json").read_text())
    assert v3["version"] == "3.0.0" and v3["verification"]["status"] == "NOT STARTED" and v3["verification"]["required_sessions"] == 20
    assert v3["verification"]["consecutive_qualifying"] == 0
    assert v3["policy"]["flags"]["meanrev"]["new_entries_enabled"] in (0, False)
    assert v3["security"]["writer_mode"] in ("actions", "selfhosted") and "READ-ONLY" in v3["security"]["pages"]

@pytest.mark.skipif(CHROME is None, reason="no chrome")
def test_tabs_render_desktop_and_mobile(site):
    pw = pytest.importorskip("playwright.sync_api")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    class Q(handler.func):
        def log_message(self, *a): pass
    srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Q, directory=str(site))); port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pw.sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            for vp in ({"width": 1440, "height": 1000}, {"width": 390, "height": 844}):
                pg = b.new_page(viewport=vp); errs = []; pg.on("pageerror", lambda e: errs.append(str(e))); pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "favicon" not in m.text else None)
                for t in TABS:
                    pg.goto(f"http://127.0.0.1:{port}/#{t}"); pg.wait_for_timeout(500); pg.reload(); pg.wait_for_selector("#view section", timeout=8000)
                    txt = pg.inner_text("#view"); assert len(txt) > 40 and "UI error" not in txt, (t, txt[:200])
                    assert pg.evaluate("document.documentElement.scrollWidth") <= vp["width"] + 20, (t, vp, pg.evaluate("document.documentElement.scrollWidth"))
                pg.goto(f"http://127.0.0.1:{port}/#verification"); pg.reload(); pg.wait_for_selector("#view section"); assert "FORWARD TRADING VERIFICATION" in pg.inner_text("#view") and "NOT STARTED" in pg.inner_text("#view")
                pg.goto(f"http://127.0.0.1:{port}/#controls"); pg.reload(); pg.wait_for_selector("#view section"); assert pg.query_selector("input[type=password]") is None and pg.query_selector("#tok") is None and "READ-ONLY" in pg.inner_text("#view")
                assert not errs, errs[:5]; pg.close()
            b.close()
    finally: srv.shutdown()
