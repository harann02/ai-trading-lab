"""P2: AI status honesty + validator fixes, with RECORDED real model output (tests/fixtures/llm_recorded.json, captured from Pollinations anonymous tier)."""
import os, sys, json, pytest
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); sys.path.insert(0, os.path.dirname(__file__))
from app import ai, db
FX = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "llm_recorded.json")))
CASES = {c["name"]: c for c in FX["cases"]}

class R:
    def __init__(s, code=200, body=None, ctype="application/json", text=None):
        s.status_code = code; s.headers = {"content-type": ctype}; s._b = body; s.text = text if text is not None else json.dumps(body)
    def json(s): return s._b

@pytest.fixture
def aienv(tmp_path, monkeypatch):
    monkeypatch.setenv("AITL_DB", str(tmp_path / "ai.db")); db.init(); ai._ACTIVE.clear(); monkeypatch.delenv("AITL_LLM_DISABLED", raising=False)
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN", "AITL_ALLOW_KEYLESS"): monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ai, "_sleep", lambda s: None); yield; ai._ACTIVE.clear()

def _probe_echo(content_fn):
    def post(*a, **k):
        msg = k["json"]["messages"][-1]["content"]
        if "Copy these facts" in msg: return R(200, {"model": "gpt-oss-20b", "choices": [{"message": {"content": msg[msg.index("{"):]}}]})
        return R(200, {"model": "gpt-oss-20b", "choices": [{"message": {"content": content_fn(msg)}}]})
    return post

# ---------------------------------------------------------------- validator: real false positives fixed, fabrications still rejected
def test_recorded_real_outputs_are_accepted():
    for c in FX["cases"]: assert ai.verify_numbers(c["response"], c["source_data"]) == [], c["name"]

def test_old_false_positives_each_fixed():
    src = json.dumps({"equity_myr": 9961.54, "date_myt": "2026-10-12", "spy_close": 778.57, "spy_ret_1d_pct": 0.6, "total_pnl_myr": -38.46, "regime_reason": "SMA200 21d slope +1.29%, 20d vol 10.0%", "n": 436})
    ok = ["Equity is 9,961.54 MYR.", "On 2026\u201110\u201112 the S&P\u202f500 (SPY) closed at 778.57, up 0.6\u202f%.", "Nasdaq-100 rose.", "P&L was \u201338.46 MYR", "Down 0.6% on October 12, 2026", "The 200-day SMA and 21-day slope of +1.29%", "436 names"]
    for t in ok: assert ai.verify_numbers(t, src) == [], t

def test_fabricated_numbers_still_rejected():
    src = json.dumps({"equity_myr": 9961.54, "date_myt": "2026-10-12", "spy_close": 778.57})
    for t, must in (("SPY closed at 790.12.", "790.12"), ("Equity is 9,000.00 MYR", "9000.00"), ("Report for 2026-10-13", "2026-10-13"), ("We bought 55 shares", "55"), ("a 33.3% gain", "33.3")):
        assert must in ai.verify_numbers(t, src), t

# ---------------------------------------------------------------- accepted / rejected counting and status derivation
def _explain_with(monkeypatch, content, cfgenv={"GROQ_API_KEY": "k"}):
    monkeypatch.setattr(ai.requests, "post", _probe_echo(lambda m: content)); src = {"spy_close": 778.57, "regime": "bullish"}
    return ai.explain("market_summary", "MARKET", src, "x", env=cfgenv, template_fn=lambda f: f"{ai.TEMPLATE_TAG} t")

def test_accepted_output_is_counted_and_verified(aienv, monkeypatch):
    txt, mode, lid = _explain_with(monkeypatch, "SPY closed at 778.57 in a bullish regime."); assert mode == "llm"
    st = ai.status(env={"GROQ_API_KEY": "k"}); o = st["indicators"]["genuine_ai_output"]
    assert st["state"] == "AI ACTIVE — VERIFIED" and o["accepted_count"] == 1 and o["last_accepted_ts"] and "1 model-written" in st["label"] and st["provider"] == "groq"
    assert st["model"] == "gpt-oss-20b", "model label comes from the response, not the alias we asked for"

def test_rejected_output_not_counted_and_never_verified(aienv, monkeypatch):
    txt, mode, lid = _explain_with(monkeypatch, "SPY closed at 912.34 in a bullish regime."); assert mode.startswith("template") and ai.TEMPLATE_TAG in txt
    st = ai.status(env={"GROQ_API_KEY": "k"}); o = st["indicators"]["genuine_ai_output"]
    assert o["accepted_count"] == 0 and o["rejected_count"] == 1 and o["rejected_reasons"] and "912.34" in json.dumps(o["rejected_reasons"])
    assert st["state"] == "TEMPLATE FALLBACK ACTIVE" and "VERIFIED" not in st["state"] and st["indicators"]["template_fallback"]["active"] and st["indicators"]["provider_reachable"]["ok"]

def test_old_accepted_outside_window_does_not_verify(aienv, monkeypatch):
    _explain_with(monkeypatch, "SPY closed at 778.57.")
    c = db.conn(); c.execute("UPDATE llm_log SET ts=?", ((datetime.now(timezone.utc) - timedelta(days=9)).isoformat(timespec="seconds"),)); c.commit(); c.close()
    st = ai.status(env={"GROQ_API_KEY": "k"}); assert st["indicators"]["genuine_ai_output"]["accepted_count"] == 0 and st["state"] != "AI ACTIVE — VERIFIED"

def test_http_402_retried_then_reported_as_unavailable_not_verified(aienv, monkeypatch):
    calls = []
    def post(*a, **k): calls.append(1); return R(402, {}, text="{}")
    monkeypatch.setattr(ai.requests, "post", post); st = ai.status(env={"GROQ_API_KEY": "k"})
    assert len(calls) >= 2, "transient 402 is retried"; assert st["state"] == "AI UNAVAILABLE" and st["indicators"]["ai_unavailable"]["active"] and not st["indicators"]["provider_reachable"]["ok"]

def test_empty_output_is_an_error_not_acceptance(aienv, monkeypatch):
    monkeypatch.setattr(ai.requests, "post", _probe_echo(lambda m: "   ")); txt, mode, lid = ai.explain("memo", "X", {"a": 1.5}, "x", env={"GROQ_API_KEY": "k"}, template_fn=lambda f: "t")
    assert mode.startswith("template"); assert ai.output_summary()["accepted_count"] == 0 and ai.output_summary()["failed_count"] == 1

def test_probe_uses_unique_nonce_each_time(aienv, monkeypatch):
    seen = []
    def post(*a, **k): msg = k["json"]["messages"][-1]["content"]; seen.append(msg); return R(200, {"choices": [{"message": {"content": msg[msg.index("{"):]}}]})
    monkeypatch.setattr(ai.requests, "post", post); p = {"name": "x", "key": "", "base": "http://x", "model": "m", "fmt": "openai"}
    assert ai.probe_grounded(p)[0] and ai.probe_grounded(p)[0] and len(set(seen)) == 2, "fixed probes can be cache-served; ours never repeat"
    monkeypatch.setattr(ai.requests, "post", lambda *a, **k: R(200, {"choices": [{"message": {"content": '{"ticker":"PROBE","price_usd":123.45,"score":71.5,"nonce":"fixed"}'}}]}))
    assert ai.probe_grounded(p)[0] is False, "a canned/cached answer is not grounded"

def test_derive_state_pure_function_matrix():
    S = lambda **k: {"window_days": 7, "accepted_count": 0, "rejected_count": 0, "failed_count": 0, **k}
    assert ai.derive_state(True, ["g"], False, S(accepted_count=2, last_accepted_ts="t"))[0] == "AI ACTIVE — VERIFIED"
    assert ai.derive_state(True, ["g"], False, S())[0] == "TEMPLATE FALLBACK ACTIVE"
    assert ai.derive_state(False, ["g"], False, S(), [{"ok": False, "category": "timeout"}])[0] == "AI UNAVAILABLE"
    assert ai.derive_state(False, ["g"], False, S(), [{"ok": False, "category": "auth_error"}])[0] == "AI ERROR"
    assert ai.derive_state(False, [], False, S())[0] == "AI TEMPLATE MODE" and ai.derive_state(False, ["g"], True, S())[0] == "AI TEMPLATE MODE"
    for args in ((True, ["g"], False, S()), (False, ["g"], False, S()), (False, [], False, S())): assert "VERIFIED" not in " ".join(ai.derive_state(*args)[:2])

def test_exported_site_has_four_indicators_and_no_verified_without_output(tmp_path, monkeypatch):
    import subprocess
    env = {**os.environ, "AITL_DB": str(tmp_path / "e.db"), "AITL_LLM_DISABLED": "1"}; root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "export_static.py", str(tmp_path / "site")], env=env, cwd=root, capture_output=True, text=True); assert r.returncode == 0, r.stderr[-500:]
    a = json.load(open(tmp_path / "site" / "data" / "v3.json"))["ai"]
    assert set(a["indicators"]) == {"provider_reachable", "genuine_ai_output", "template_fallback", "ai_unavailable"} and "VERIFIED" not in a["state"]
