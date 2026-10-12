"""v3 tests: AI status/probe, reports, ci_run flow, control via dispatch, writer lease, no secrets."""
import os, sys, json
import pytest
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(__file__))
from test_v2 import env2, run_full, SYMS7, frames_us, load, patch_universe, NOW_SCAN, NOW_FILL
from conftest import FakeProvider
from conftest_v2 import IntradayProvider
from app import ai, db, control, trading, universe, ledger
from app.v3 import jobs, daily_report, weekly_eval, verification as V

class R:
    def __init__(s, code=200, body=None, ctype="application/json", text=None):
        s.status_code = code; s.headers = {"content-type": ctype}; s._b = body; s.text = text if text is not None else json.dumps(body)
    def json(s): return s._b

def _chat(content): return R(200, {"choices": [{"message": {"content": content}}]})

@pytest.fixture
def ai_env(monkeypatch):
    ai._ACTIVE.clear(); monkeypatch.delenv("AITL_LLM_DISABLED", raising=False)
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN", "AITL_ALLOW_KEYLESS"): monkeypatch.delenv(k, raising=False)
    yield; ai._ACTIVE.clear()

def test_ai_template_mode_when_no_key(env2, ai_env):
    st = ai.status(env={}); assert st["state"] == "AI TEMPLATE MODE" and st["mode"] == "template" and st["keys_exposed"] is False

def test_ai_verified_only_after_real_grounded_probe(env2, ai_env, monkeypatch):
    monkeypatch.setattr(ai.requests, "post", lambda *a, **k: _chat('{"ticker":"PROBE","price_usd":123.45,"score":71.5}'))
    st = ai.status(env={"GROQ_API_KEY": "gsk_SECRET123"}); assert st["state"] == "AI ACTIVE — VERIFIED" and st["provider"] == "groq" and st["model"]
    assert "SECRET123" not in json.dumps(st)

def test_ai_plain_ok_is_not_verified(env2, ai_env, monkeypatch):
    monkeypatch.setattr(ai.requests, "post", lambda *a, **k: _chat("OK"))                                          # the GitHub-Models failure mode seen in CI
    st = ai.status(env={"GITHUB_TOKEN": "ghs_x"}); assert st["state"] == "AI ERROR" and st["probes"][0]["category"] == "invalid_json"

def test_ai_ungrounded_json_rejected(env2, ai_env, monkeypatch):
    monkeypatch.setattr(ai.requests, "post", lambda *a, **k: _chat('{"ticker":"PROBE","price_usd":999,"score":1}'))
    st = ai.status(env={"GROQ_API_KEY": "k"}); assert st["state"] == "AI ERROR" and st["probes"][0]["category"] == "ungrounded_output"

def test_ai_unavailable_categories_logged(env2, ai_env, monkeypatch):
    monkeypatch.setattr(ai.requests, "post", lambda *a, **k: R(429, {}, text="slow down"))
    st = ai.status(env={"GROQ_API_KEY": "k"}); assert st["state"] == "AI UNAVAILABLE" and st["probes"][0]["category"] == "rate_limited"
    c = db.conn(); n = c.execute("SELECT COUNT(*) n FROM ai_status_log WHERE category='rate_limited'").fetchone()["n"]; c.close(); assert n >= 1
    ai._ACTIVE.clear(); monkeypatch.setattr(ai.requests, "post", lambda *a, **k: R(401, {}, text="bad key"))
    assert ai.status(env={"GROQ_API_KEY": "k"})["state"] == "AI ERROR"

def test_ai_fallback_labeled_and_numbers_validated(env2, ai_env):
    txt, mode, lid = ai.explain("memo", "AAA", {"ticker": "AAA", "price_usd": 10.0}, "x", env={}, cfg=None, template_fn=lambda f: f"{ai.TEMPLATE_TAG} AAA at 10.0")
    assert mode.startswith("template") and ai.TEMPLATE_TAG in txt

def test_keyless_requires_explicit_opt_in(ai_env):
    assert not ai.candidates({}); assert [c["name"] for c in ai.candidates({"AITL_ALLOW_KEYLESS": "1"})] == ["pollinations"]

def _scan_and_fill(monkeypatch):
    monkeypatch.setattr(universe, "candidates", lambda cfg, now=None: ({s: {"name": s, "sources": ["x"]} for s in SYMS7}, {s: {"exchange": "NASDAQ"} for s in SYMS7}, {}))
    db.set_settings({"entry_cadence": "daily"}); r = run_full(monkeypatch, syms=SYMS7); assert "aborted" not in r
    import pandas as pd
    from app import data
    fr = frames_us(); p2 = IntradayProvider(fr); data.save_bars(fr, NOW_FILL); trading.execute_pending(p2, NOW_FILL, refresh=True)

def test_daily_report_and_weekly_eval(env2, monkeypatch):
    _scan_and_fill(monkeypatch)
    from datetime import date
    b = daily_report.build(date(2026, 10, 13), NOW_FILL + timedelta(hours=8)); f = b["facts"]
    assert f["portfolio"]["equity_myr"] == pytest.approx(10000, rel=0.03) and b["commentary_mode"].startswith("template") and ai.TEMPLATE_TAG in b["commentary"]
    assert "risk_review" in f and "benchmark_comparison" in f and "operational" in f and f["trades_and_orders"] is not None
    assert daily_report.latest(1)[0]["period"] == "2026-10-13"
    w = weekly_eval.build(NOW_FILL + timedelta(days=3)); assert w["overall"]["n_trades"] == 0 and "NOT defined" in w["overall"]["note"] and "PROPOSALS ONLY" in w["authority"]
    assert w["proposals"][0]["status"].startswith("PROPOSED")

def test_stats_insufficient_sample_and_expectancy():
    t = [{"pnl_usd": 10.0, "fees_usd": 1, "cost_usd_spread_slip": 0}, {"pnl_usd": -5.0, "fees_usd": 1, "cost_usd_spread_slip": 0}]
    s = weekly_eval.stats(t); assert s["profit_factor"] is None and any("withheld" in n for n in s["notes"]) and s["expectancy_usd"] == pytest.approx(0.5 * 10 - 0.5 * 5)
    big = [{"pnl_usd": 2.0, "fees_usd": 0, "cost_usd_spread_slip": 0}] * 12; s2 = weekly_eval.stats(big); assert s2["profit_factor"] is None and any("undefined" in n for n in s2["notes"])
    mix = big + [{"pnl_usd": -1.0, "fees_usd": 0, "cost_usd_spread_slip": 0}] * 4; assert weekly_eval.stats(mix)["profit_factor"] == pytest.approx(24 / 4)

def test_writer_lease_single_writer_and_mode(env2):
    now = NOW_SCAN; ok, _ = jobs.acquire_lease("actions:1", "actions", now=now); assert ok
    ok2, why = jobs.acquire_lease("actions:2", "actions", now=now + timedelta(minutes=1)); assert not ok2 and "held" in why
    ok3, _ = jobs.acquire_lease("actions:2", "actions", now=now + timedelta(minutes=40)); assert ok3                  # expired lease can be taken
    jobs.release_lease("actions:2"); jobs.set_writer_mode("selfhosted", "owner")
    ok4, why4 = jobs.acquire_lease("actions:3", "actions", now=now + timedelta(hours=2)); assert not ok4 and "selfhosted" in why4   # never both writers

def test_idempotency_blocks_duplicates(env2):
    a = jobs.begin("scan", NOW_SCAN, "schedule", "1", session="2026-10-12"); b = jobs.begin("scan", NOW_SCAN + timedelta(minutes=2), "schedule", "2", session="2026-10-12")
    assert a and b is None

def test_dispatch_controls_validated_and_audited(env2):
    r = control.apply_dispatch(json.dumps({"strategies": {"meanrev": {"allocation_pct": 50}}}), json.dumps({"type": "reactivate_strategy", "strategy": "meanrev", "confirm": ""}), "owner", "9", NOW_SCAN)
    assert r[0]["settings"]["ok"] is False                                                                          # retired sleeve cannot get capital via settings
    assert r[1]["ok"] is False                                                                                       # reactivation needs the exact owner confirmation + validation
    from app.v3 import policy; assert policy.new_entries_allowed("meanrev") is False
    r2 = control.apply_dispatch(None, json.dumps({"type": "start_v3_experiment", "confirm": "nope"}), "owner", "10", NOW_SCAN); assert r2[0]["ok"] is False
    r3 = control.apply_dispatch("{bad", None, "owner", "11", NOW_SCAN); assert r3[0]["ok"] is False

def test_ci_run_scan_records_job_and_is_idempotent(env2, monkeypatch, tmp_path):
    import ci_run
    monkeypatch.setattr(universe, "candidates", lambda cfg, now=None: ({s: {"name": s, "sources": ["x"]} for s in SYMS7}, {s: {"exchange": "NASDAQ"} for s in SYMS7}, {}))
    monkeypatch.setattr(ci_run.shadow, "fetch_dividends", lambda **k: {}); monkeypatch.setattr(ci_run, "YFinanceProvider", lambda: FakeProvider(frames_us())); monkeypatch.setenv("GITHUB_RUN_ID", "111"); monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out.txt")); ci_run.main("scan")
    c = db.conn(); rows = [dict(r) for r in c.execute("SELECT job,status FROM job_runs")]; c.close()
    assert any(r["job"] == "scan" and r["status"] in ("success", "failed") for r in rows)
    assert "engine_status=" in (tmp_path / "out.txt").read_text()
    ci_run.main("scan"); c = db.conn(); n2 = c.execute("SELECT COUNT(*) n FROM job_runs WHERE job='scan' AND status='success'").fetchone()["n"]; c.close(); assert n2 == 1, rows
    ci_run.main("export")                                                                                            # lease released properly: next run can write

def test_snapshot_has_no_secrets(env2, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_TOPSECRET999"); monkeypatch.setenv("GITHUB_TOKEN", "ghp_TOPSECRET888"); ai._ACTIVE.clear()
    monkeypatch.setattr(ai.requests, "post", lambda *a, **k: R(401, {}, text="bad key gsk_TOPSECRET999"))
    st = ai.status(); blob = json.dumps(st) + json.dumps(daily_report.build(NOW_FILL.date(), NOW_FILL, store=False), default=str)
    c = db.conn(); blob += json.dumps([dict(r) for r in c.execute("SELECT * FROM ai_status_log")]); c.close()
    assert "TOPSECRET" not in blob

# ---------------------------------------------------------------- self-hosted backend: auth + CSRF + writer mode
import base64
from fastapi.testclient import TestClient
def _h(): return {"Authorization": "Basic " + base64.b64encode(b"alice:s3cret-pass").decode(), "X-Requested-With": "ailab"}

@pytest.fixture
def api(env2, monkeypatch):
    monkeypatch.delenv("AITL_AUTH_DISABLED", raising=False); monkeypatch.setenv("DASH_USER", "alice"); monkeypatch.setenv("DASH_PASS", "s3cret-pass")
    from app.main import app
    return TestClient(app)

def test_backend_read_only_unless_selfhosted_writer(api):
    assert api.get("/healthz").status_code == 200 and api.get("/api/v3").status_code == 401
    assert api.get("/api/v3", headers=_h()).status_code == 200
    r = api.post("/api/command", headers=_h(), json={"type": "resume_entries"}); assert r.status_code == 409 and "READ-ONLY" in r.text      # default writer mode = actions
    jobs.set_writer_mode("selfhosted", "owner")
    assert api.post("/api/command", headers={k: v for k, v in _h().items() if k != "X-Requested-With"}, json={"type": "resume_entries"}).status_code == 403   # CSRF
    r = api.post("/api/command", headers=_h(), json={"type": "reactivate_strategy", "strategy": "meanrev", "confirm": "wrong"}); assert r.status_code == 400
    assert api.post("/api/command", headers=_h(), json={"type": "start_v3_experiment", "confirm": "no"}).status_code == 400

def test_selfhosted_scheduler_respects_writer_mode(env2, monkeypatch):
    from app import jobs as sj
    monkeypatch.setattr(sj.cal, "is_open", lambda *a, **k: True)
    sj.monitor("US"); c = db.conn(); r = c.execute("SELECT status,skip_reason FROM job_runs WHERE job='monitor'").fetchone(); c.close()
    assert r and r["status"] == "skipped" and "not the writer" in r["skip_reason"]                                    # mode=actions: the self-hosted scheduler must not write
