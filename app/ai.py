"""AI research layer. The LLM ONLY explains/summarises VERIFIED data we pass in (price, score breakdown, fundamentals, timestamped yfinance headlines).
It never decides, sizes or places orders: the deterministic engine keeps execution authority. Every prompt, the exact source data and the response are logged (llm_log).
If no LLM works -> deterministic template text clearly labelled 'template, no LLM'. LLM output numbers are verified against the source data; unverifiable output is discarded."""
import json, os, re
from datetime import datetime, timezone
import requests
from . import db, config, ledger

TEMPLATE_TAG = "[template, no LLM]"
SYSTEM = ("You are a careful equity research writer for a PAPER-trading lab. Use ONLY the JSON facts provided. Never invent numbers, news, prices or ratings. "
          "Do not give investment advice and do not decide trades; the rules engine already decided. Write concise plain text. "
          "If a fact is null/missing say it is unavailable. Every number you write must appear in the JSON; copy numbers exactly as written there (no thousands separators, no rounding, no new calculations). Use plain ASCII text.")

# (env var for key, base_url, default model, wire format). Order = preference. Free-first: GROQ / GEMINI / OPENROUTER (free models) / GitHub Models / keyless Pollinations come BEFORE paid keys
# only when no paid key is set; paid keys still win if present because the owner configured them deliberately.
PROVIDERS = [
    ("XAI_API_KEY", "https://api.x.ai/v1", "grok-3-mini", "openai"),
    ("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4o-mini", "openai"),
    ("ANTHROPIC_API_KEY", "https://api.anthropic.com/v1", "claude-3-5-haiku-latest", "anthropic"),
    ("AITL_LLM_API_KEY", None, "", "openai"),                       # + AITL_LLM_BASE_URL (any OpenAI-compatible endpoint)
    ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "openai"),               # FREE tier (needs a free key)
    ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash", "openai"),   # FREE tier (needs a free key)
    ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free", "openai"),    # FREE models (needs a free key)
    ("GITHUB_TOKEN", "https://models.github.ai/inference", "openai/gpt-4o-mini", "openai"),               # GitHub Models free tier (probed; see docs/AI_SETUP.md)
]
KEYLESS = ("pollinations", "https://text.pollinations.ai/openai", "openai-fast", "openai")   # GET /models lists only `openai-fast` (GPT-OSS 20B) as anonymous-tier; the old alias "openai" resolved to the same model but was mislabeled   # key-less public service: enabled only with AITL_ALLOW_KEYLESS=1, still must pass the grounded probe

class LLMError(Exception):
    """category in: auth_error | rate_limited | payment_required | server_error | network_error | timeout | non_json_response | invalid_json | ungrounded_output | http_error | disabled"""
    def __init__(self, category, msg=""): super().__init__(f"{category}: {msg}"); self.category = category

def candidates(env=None, cfg=None):
    env = env if env is not None else os.environ; llm = (cfg or {}).get("llm", {}); out = []
    if env.get("AITL_LLM_DISABLED") == "1": return out
    want = llm.get("provider", "auto")
    for var, base, model, fmt in PROVIDERS:
        if not env.get(var): continue
        name = var.replace("_API_KEY", "").replace("_TOKEN", "").lower()
        if want not in ("auto", name, var.lower()): continue
        b = env.get("AITL_LLM_BASE_URL") if var == "AITL_LLM_API_KEY" else base
        if var == "AITL_LLM_API_KEY" and not b: continue
        out.append({"name": name, "key": env[var], "base": llm.get("base_url") or b, "model": llm.get("model") or env.get("AITL_LLM_MODEL") or model, "fmt": fmt})
    if env.get("AITL_ALLOW_KEYLESS") == "1" and want in ("auto", "pollinations"):
        n, b, m, f = KEYLESS; out.append({"name": n, "key": "", "base": b, "model": env.get("AITL_KEYLESS_MODEL") or m, "fmt": f})
    return out

def _classify(status, ctype, body):
    if status in (401, 403): return "auth_error"
    if status == 402: return "payment_required"
    if status == 429: return "rate_limited"
    if status >= 500: return "server_error"
    if status >= 400: return "http_error"
    if "json" not in (ctype or "").lower(): return "non_json_response"
    return None

TRANSIENT = ("payment_required", "rate_limited", "server_error", "timeout", "network_error")   # Pollinations anonymous tier answers 402/429/5xx under load: retried, then reported honestly
RETRY_SLEEP = (2, 6)
_sleep = __import__("time").sleep

def _call(p, system, user, max_tokens=1200, timeout=60, json_mode=False, retries=2):
    last = None
    for attempt in range(retries + 1):
        try: return _call_once(p, system, user, max_tokens, timeout, json_mode)
        except LLMError as e:
            last = e
            if e.category not in TRANSIENT or attempt == retries: raise
            _sleep(RETRY_SLEEP[min(attempt, len(RETRY_SLEEP) - 1)])
    raise last

def _call_once(p, system, user, max_tokens=1200, timeout=60, json_mode=False):
    hdr = {"x-api-key": p["key"], "anthropic-version": "2023-06-01"} if p["fmt"] == "anthropic" else ({"Authorization": f"Bearer {p['key']}"} if p["key"] else {})
    try:
        if p["fmt"] == "anthropic":
            r = requests.post(p["base"] + "/messages", headers=hdr, timeout=timeout, json={"model": p["model"], "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}], "temperature": 0.2})
        else:
            body = {"model": p["model"], "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "max_tokens": max_tokens, "temperature": 0.2}
            if json_mode: body["response_format"] = {"type": "json_object"}
            r = requests.post(p["base"] + "/chat/completions", headers=hdr, timeout=timeout, json=body)
    except requests.Timeout as e: raise LLMError("timeout", str(e)[:80])
    except requests.RequestException as e: raise LLMError("network_error", type(e).__name__)
    cat = _classify(r.status_code, r.headers.get("content-type"), r.text)
    if cat: raise LLMError(cat, f"HTTP {r.status_code} {r.headers.get('content-type','')[:30]} {r.text[:100]!r}".replace(p["key"] or "\0", "***"))
    try:
        j = r.json(); p["model_actual"] = j.get("model") if isinstance(j, dict) else None
        txt = j["content"][0]["text"] if p["fmt"] == "anthropic" else j["choices"][0]["message"]["content"]
    except Exception: raise LLMError("invalid_json", f"unexpected body {r.text[:80]!r}".replace(p["key"] or "\0", "***"))
    if not (txt or "").strip(): raise LLMError("empty_output", "model returned empty content (token budget consumed by reasoning?)")
    return txt

PROBE_FACTS = {"ticker": "PROBE", "price_usd": 123.45, "score": 71.5}     # kept for reference/back-compat; probes use a fresh random nonce each time
def probe_grounded(p):
    """REAL probe with a UNIQUE nonce per call (a fixed prompt can be answered from the provider's cache - audit F-3). The model must return valid JSON copying the random facts. Returns (ok, category, detail)."""
    import random, uuid
    facts = {"ticker": "PROBE", "price_usd": round(random.uniform(10, 900), 2), "score": round(random.uniform(1, 99), 1), "nonce": uuid.uuid4().hex[:10]}
    try:
        txt = _call(p, "You output strict JSON only. No prose, no markdown.", f"Copy these facts into a JSON object with exactly the keys ticker, price_usd, score, nonce and the SAME values: {json.dumps(facts)}", max_tokens=400, timeout=40, json_mode=True, retries=1)
    except LLMError as e: return False, e.category, str(e)[:200]
    m = re.search(r"\{.*\}", txt or "", re.S)
    try: j = json.loads(m.group(0) if m else txt)
    except Exception: return False, "invalid_json", f"not JSON: {(txt or '')[:80]!r}"
    try: good = j.get("ticker") == "PROBE" and abs(float(j.get("price_usd")) - facts["price_usd"]) < 1e-6 and abs(float(j.get("score")) - facts["score"]) < 1e-6 and str(j.get("nonce")) == facts["nonce"]
    except Exception: good = False
    return (True, None, "valid grounded JSON returned (unique nonce echoed)") if good else (False, "ungrounded_output", f"JSON did not match the supplied facts: {str(j)[:80]}")

_ACTIVE = {}; LAST_ERRS = []; LAST_PROBES = []
def _key(env): return json.dumps(sorted((env if env is not None else os.environ).keys() & ({v[0] for v in PROVIDERS} | {"AITL_ALLOW_KEYLESS", "AITL_LLM_DISABLED"})))

def active_provider(env=None, cfg=None, force=False):
    """First candidate whose grounded-JSON probe passes. Cached per process. The probe result (never the key) is logged in ai_status_log."""
    k = _key(env)
    if not force and k in _ACTIVE: return _ACTIVE[k]
    chosen, errs, probes = None, [], []
    for p in candidates(env, cfg):
        ok, cat, det = probe_grounded(p); probes.append({"provider": p["name"], "model": p["model"], "ok": ok, "category": cat, "detail": det})
        if ok: chosen = p; break
        errs.append(f"{p['name']}: {cat}")
    _ACTIVE[k] = chosen; _ACTIVE[k + ":errs"] = errs; _ACTIVE[k + ":probes"] = probes; globals()["LAST_ERRS"] = errs; globals()["LAST_PROBES"] = probes
    return chosen

WINDOW_DAYS = 7          # "recent window" used for every AI status count
GENUINE_KINDS = ("memo", "market_summary", "daily_report", "weekly_report")

def output_summary(window_days=WINDOW_DAYS, now=None):
    """What the model ACTUALLY produced in the recent window, straight from llm_log. Accepted = mode 'llm' AND ok=1 (the text passed the number validator and is what is stored/shown).
    Rejected = 'llm_rejected' (validator) ; failed = 'llm_error' (HTTP/timeouts/empty) ; template = deterministic fallback text."""
    from datetime import timedelta
    now = now or datetime.now(timezone.utc); since = (now - timedelta(days=window_days)).isoformat(timespec="seconds"); out = {"window_days": window_days, "since_utc": since}
    try:
        c = db.conn(); rows = [dict(r) for r in c.execute("SELECT id,ts,kind,provider,model,mode,ok,error FROM llm_log WHERE ts>=? ORDER BY id", (since,))]; c.close()
    except Exception: rows = []
    acc = [r for r in rows if r["mode"] == "llm" and r["ok"]]; rej = [r for r in rows if r["mode"] == "llm_rejected"]; err = [r for r in rows if r["mode"] == "llm_error"]; tpl = [r for r in rows if r["mode"] == "template"]
    reasons = {}
    for r in rej + err:
        k = (r["error"] or r["mode"]).split(":")[0] + (": " + r["error"].split(":", 1)[1].strip()[:80] if r["error"] and ":" in r["error"] else ""); reasons[k] = reasons.get(k, 0) + 1
    out.update({"accepted_count": len(acc), "last_accepted_ts": acc[-1]["ts"] if acc else None, "last_accepted_kind": acc[-1]["kind"] if acc else None, "last_accepted_model": (acc[-1]["provider"] or "") + "/" + (acc[-1]["model"] or "") if acc else None,
                "rejected_count": len(rej), "failed_count": len(err), "template_count": len(tpl), "rejected_reasons": [{"reason": k, "count": v} for k, v in sorted(reasons.items(), key=lambda x: -x[1])][:8],
                "last_template_ts": tpl[-1]["ts"] if tpl else None, "last_attempt_ts": rows[-1]["ts"] if rows else None, "last_attempt_mode": rows[-1]["mode"] if rows else None, "attempts": len(rows)})
    return out

def derive_state(provider_reachable, configured, disabled, summ, probes=()):
    """Pure function: the four SEPARATE indicators and the headline. 'AI ACTIVE — VERIFIED' only if >=1 model-written output was accepted in the window."""
    acc = summ.get("accepted_count", 0)
    cats = {x.get("category") for x in probes if not x.get("ok")}
    transient = bool(cats) and cats <= {"network_error", "timeout", "rate_limited", "server_error", "payment_required", "non_json_response", "empty_output"}
    ind = {"provider_reachable": {"ok": bool(provider_reachable), "configured": configured, "detail": "latest grounded probe passed (unique nonce)" if provider_reachable else ("not configured / disabled" if (disabled or not configured) else "latest probe failed: " + ", ".join(sorted(c for c in cats if c)))},
           "genuine_ai_output": {"accepted_count": acc, "last_accepted_ts": summ.get("last_accepted_ts"), "window_days": summ.get("window_days"), "rejected_count": summ.get("rejected_count", 0), "failed_count": summ.get("failed_count", 0), "rejected_reasons": summ.get("rejected_reasons", [])},
           "template_fallback": {"active": acc == 0 or summ.get("last_attempt_mode") not in (None, "llm"), "template_count": summ.get("template_count", 0), "last_template_ts": summ.get("last_template_ts")},
           "ai_unavailable": {"active": bool(configured) and not provider_reachable and not disabled, "failed_count": summ.get("failed_count", 0)}}
    w = summ.get("window_days", WINDOW_DAYS)
    if acc >= 1:
        state = "AI ACTIVE — VERIFIED"; label = f"AI ACTIVE — VERIFIED: {acc} model-written report(s) accepted in the last {w} days (last accepted {summ.get('last_accepted_ts')}); rejected {summ.get('rejected_count', 0)}, failed {summ.get('failed_count', 0)}"
        if ind["template_fallback"]["active"]: label += "; latest output used the template fallback"
    elif disabled: state, label = "AI TEMPLATE MODE", "AI TEMPLATE MODE: LLM disabled by AITL_LLM_DISABLED; labeled deterministic templates are used"
    elif not configured: state, label = "AI TEMPLATE MODE", "AI TEMPLATE MODE: no LLM provider configured (add a free key, see docs/AI_SETUP.md); labeled deterministic templates are used"
    elif provider_reachable: state, label = "TEMPLATE FALLBACK ACTIVE", f"TEMPLATE FALLBACK ACTIVE: provider reachable but 0 model-written reports accepted in the last {w} days (rejected {summ.get('rejected_count', 0)}, failed {summ.get('failed_count', 0)}); labeled templates are used"
    elif transient or not cats: state, label = "AI UNAVAILABLE", "AI UNAVAILABLE: configured provider(s) unreachable/rate-limited/not serving JSON; TEMPLATE FALLBACK ACTIVE; labeled templates are used"
    else: state, label = "AI ERROR", "AI ERROR: configured provider(s) answered with auth errors or invalid/ungrounded output; TEMPLATE FALLBACK ACTIVE; labeled templates are used"
    return state, label, ind

def status(env=None, cfg=None, now=None):
    """v3 AI status = four separate indicators (provider reachable / genuine output accepted / template fallback / unavailable). Reachability = a real probe with a unique nonce THIS run;
    'VERIFIED' needs accepted model output in the recent window (llm_log), NOT just a passing probe. Never exposes keys."""
    e = env if env is not None else os.environ; p = active_provider(env, cfg); k = _key(env); probes = _ACTIVE.get(k + ":probes", []); cands = candidates(env, cfg)
    summ = output_summary(now=now); disabled = e.get("AITL_LLM_DISABLED") == "1"
    state, label, ind = derive_state(bool(p), [c["name"] for c in cands], disabled, summ, probes)
    out = {"state": state, "mode": "LLM" if (p and summ["accepted_count"] > 0) else "template", "provider": p["name"] if p else None, "model": (p.get("model_actual") or p["model"]) if p else None, "label": label, "template_tag": TEMPLATE_TAG,
           "indicators": ind, "output_window": summ, "probes": probes, "errors": _ACTIVE.get(k + ":errs", []), "configured": [c["name"] for c in cands], "keys_exposed": False, "probe": "grounded JSON copy-facts probe with unique nonce"}
    try:
        c = db.conn(); c.execute("CREATE TABLE IF NOT EXISTS ai_status_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, state TEXT, provider TEXT, model TEXT, category TEXT, detail TEXT)")
        now_s = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not probes: c.execute("INSERT INTO ai_status_log(ts,state,provider,model,category,detail) VALUES(?,?,?,?,?,?)", (now_s, state, None, None, "no_provider_configured" if state == "AI TEMPLATE MODE" else None, label))
        for x in probes: c.execute("INSERT INTO ai_status_log(ts,state,provider,model,category,detail) VALUES(?,?,?,?,?,?)", (now_s, state, x["provider"], x["model"], x["category"], x["detail"]))
        c.commit(); c.close()
    except Exception: pass
    return out

# ------------------------------------------------------------------ facts
def _num(x, n=2): return None if x is None else round(float(x), n)

def memo_fields(decision, order=None, fx=None, news=None, company=None, strategy_name="", fundamentals=None):
    """Builds the REQUIRED memo fields from stored decision data only (no free text)."""
    snap = json.loads(decision["snapshot"] or "{}") if isinstance(decision.get("snapshot"), str) else (decision.get("snapshot") or {})
    plan = json.loads(decision["plan"]) if isinstance(decision.get("plan"), str) else (decision.get("plan") or {})
    scores = json.loads(decision["scores"]) if isinstance(decision.get("scores"), str) else (decision.get("scores") or {})
    checks = json.loads(decision["checks"]) if isinstance(decision.get("checks"), str) else (decision.get("checks") or [])
    f = snap.get("features") or {}; fund = fundamentals or snap.get("fundamentals") or {}; reg = snap.get("regime") or {}
    fxd = fx or snap.get("fx") or {}; rate = fxd.get("rate")
    bull, bear = [], []
    if f.get("close") and f.get("sma50") and f["close"] > f["sma50"]: bull.append(f"close {_num(f['close'])} above SMA50 {_num(f['sma50'])}")
    if f.get("close") and f.get("sma200") and f["close"] > f["sma200"]: bull.append(f"close above SMA200 {_num(f['sma200'])}")
    if f.get("r126") is not None and f["r126"] > 0: bull.append(f"126-day return {_num(f['r126']*100,1)}%")
    ex = snap.get("extra") or {}
    if ex.get("rs_vs_spy_126") is not None: (bull if ex["rs_vs_spy_126"] > 0 else bear).append(f"126-day return vs SPY {_num(ex['rs_vs_spy_126']*100,1)}pp")
    if ex.get("rs_vs_qqq_126") is not None: (bull if ex["rs_vs_qqq_126"] > 0 else bear).append(f"126-day return vs QQQ {_num(ex['rs_vs_qqq_126']*100,1)}pp")
    if f.get("rsi14") is not None and f["rsi14"] > 70: bear.append(f"RSI14 {_num(f['rsi14'],1)} overbought")
    if f.get("rsi2") is not None and f["rsi2"] <= 10: bull.append(f"RSI2 {_num(f['rsi2'],1)} deeply oversold (mean-reversion setup)")
    if f.get("atr_pct") is not None and f["atr_pct"] > 0.04: bear.append(f"ATR {_num(f['atr_pct']*100,1)}% of price: volatile")
    if reg.get("regime") and reg["regime"] != "bullish": bear.append(f"market regime is {reg['regime']}")
    for k in checks:
        if not k["pass"] and k["name"] not in ("automation_on",): bear.append(f"check failed: {k['name']} - {k['detail']}")
    if fund.get("pe") and fund["pe"] > 30: bear.append(f"trailing P/E {_num(fund['pe'],1)} is high")
    if fund.get("revenue_growth") is not None and fund["revenue_growth"] > 0.1: bull.append(f"revenue growth {_num(fund['revenue_growth']*100,1)}%")
    trig = next((k["detail"] for k in checks if k["name"] in ("entry_trigger", "RSI2<=10")), None) or "n/a"
    qty = plan.get("qty"); entry = plan.get("entry")
    return {"company": company or fund.get("name"), "ticker": decision["symbol"], "strategy": strategy_name or decision.get("strategy_id"), "price_usd": _num(entry or f.get("close")),
            "price_bar_date": snap.get("bar_date"), "score": decision.get("score"), "score_breakdown": (scores.get("factors") or {}), "regime": reg.get("regime"), "regime_reason": reg.get("reason"),
            "trigger": trig, "stop_usd": _num(plan.get("stop")), "target_usd": _num(plan.get("target")), "target_source": plan.get("target_source"), "size_shares": qty,
            "max_loss_usd": _num(plan.get("max_loss_usd")), "max_loss_myr": _num(plan.get("max_loss_myr")), "usdmyr": rate, "reward_risk": _num(plan.get("rr")),
            "bullish_evidence": bull[:8], "bearish_evidence": bear[:10], "signal_status": decision.get("decision"),
            "execution_status": (f"order #{order['id']} {order['status']}" + (f" ({order['reason']})" if order.get("reason") else "")) if order else "no order created (status " + str(decision.get("decision")) + ")",
            "data_sources": {"prices": snap.get("data_label") or "yfinance daily (EOD/delayed)", "provider": snap.get("provider") or "yfinance", "last_bar": snap.get("bar_date"), "decision_ts_utc": decision.get("ts"),
                             "fx": f"{fxd.get('source')} rate_ts {fxd.get('rate_ts')}", "fundamentals_fetched_at": fund.get("fetched_at")},
            "news": news or [],
            "rejection_reasons": [k["name"] + ": " + k["detail"] for k in checks if not k["pass"] and k["name"] not in ("automation_on",)][:10],
            "target_methodology": plan.get("target_source") or ("no credible target: ATR trailing-stop exit" if not plan.get("target") else "n/a"),
            "model_status": None, "authority": "AI/LLM text is explanation only. It cannot place, size, modify or cancel orders, change risk limits, or override any deterministic check; the rules engine has sole execution authority."}

def template_text(fl):
    L = [f"{fl['ticker']} ({fl.get('company') or 'company n/a'}) - {fl['strategy']} - signal {fl['signal_status']}  {TEMPLATE_TAG}",
         f"Price ${fl['price_usd']} (bar {fl['price_bar_date']}); score {fl['score']}; regime {fl['regime']}.",
         f"Trigger: {fl['trigger']}.", f"Plan: stop ${fl['stop_usd']}, target {('$' + str(fl['target_usd'])) if fl['target_usd'] else 'none credible'}, size {fl['size_shares']} sh, max loss ${fl['max_loss_usd']} (RM{fl['max_loss_myr']}), R:R {fl['reward_risk']}.",
         "Bullish evidence: " + ("; ".join(fl["bullish_evidence"]) or "none flagged") + ".", "Bearish evidence: " + ("; ".join(fl["bearish_evidence"]) or "none flagged") + ".",
         f"Execution: {fl['execution_status']}.", "Rejection reasons: " + ("; ".join(fl.get("rejection_reasons") or []) or "none") + ".", "Authority: " + fl["authority"],
         "Sources: " + json.dumps(fl["data_sources"]) + (". Headlines: " + " | ".join(f"{n['published']} {n['title']}" for n in fl["news"]) if fl["news"] else ". No headlines available.")]
    return "\n".join(L)

_DASH = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212"), "-"); _SPACE = dict.fromkeys(map(ord, "\u202f\u00a0\u2009\u2007"), " ")
MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|august|september|october|november|december"
_ALLOW = [re.compile(p, re.I) for p in (
    r"s\s*&\s*p\s*-?\s*500", r"nasdaq\s*-?\s*100", r"dow\s*30", r"russell\s*(?:1000|2000)",                    # index NAMES, not data
    r"\b(?:sma|ema|rsi|atr|adx|macd)\s*[-(]?\s*\d{1,3}\)?", r"\b\d{1,3}\s*-?\s*(?:day|days|d|week|weeks|wk|month|months|session|sessions|bar|bars)\b",   # indicator names / window lengths
    r"\b(?:q[1-4]|fy)\s*\d{0,4}\b", r"\b52\s*-?\s*weeks?\b", r"\b(?:t\+1|t\+2)\b")]
_NUM = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")

def _norm_text(t):
    t = (t or "").translate(_DASH).translate(_SPACE)
    return re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t)          # 9,961.54 -> 9961.54

def _src_numbers(source_json):
    t = _norm_text(source_json); out = set()
    for d in re.findall(r"\d{4}-\d{2}-\d{2}", t): out.update(float(x) for x in d.split("-"))      # date components are legitimate numbers, not negatives
    for m in _NUM.findall(re.sub(r"\d{4}-\d{2}-\d{2}", " ", t)): out.add(abs(float(m)))
    return out

def verify_numbers(text, source_json):
    """Every DATA number in the LLM text must be present in the source facts. False positives fixed (audit F-3): thousands separators (9,961.54), unicode hyphens/minus/spaces,
    ISO dates and 'October 12, 2026' style dates, index names (S&P 500, Nasdaq-100), indicator names/windows (SMA200, 20-day), sign-insensitive (down 0.6% vs -0.6).
    Numbers NOT in the source are still rejected. Returns the list of unverifiable numbers."""
    src = _src_numbers(source_json); t = _norm_text(text); bad = []
    for d in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", t):                                   # ISO dates must be dates we gave it (or be date components of them)
        if d not in _norm_text(source_json) and not all(float(x) in src for x in d.split("-")): bad.append(d)
    t = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", t)
    t = re.sub(rf"\b(?:{MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s*\d{{4}})?", " ", t, flags=re.I)  # 'October 12, 2026'
    t = re.sub(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\.?(?:,?\s*\d{{4}})?", " ", t, flags=re.I)  # '12 October 2026'
    for a in _ALLOW: t = a.sub(" ", t)
    for m in _NUM.findall(t):
        v = abs(float(m))
        if v <= 10 and float(int(v)) == v: continue          # small integers (counts, ordinals)
        if 1900 <= v <= 2100: continue                       # years
        if not any(abs(v - s) <= max(0.011, abs(s) * 0.006) for s in src): bad.append(m)
    return bad

def log_llm(kind, symbol, prov, mode, prompt, source, response, ok, error=None):
    c = db.conn(); cur = c.execute("INSERT INTO llm_log(ts,kind,symbol,provider,model,mode,prompt,source_data,response,ok,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), kind, symbol, prov and prov["name"], prov and (prov.get("model_actual") or prov["model"]), mode, prompt, source, response, int(ok), error)); c.commit(); i = cur.lastrowid; c.close(); return i

def explain(kind, symbol, facts: dict, instruction: str, env=None, cfg=None, template_fn=None):
    """Returns (text, mode, llm_log_id). LLM used only if available AND its numbers verify; otherwise labelled template."""
    src = json.dumps(facts, default=str, sort_keys=True); prompt = f"{instruction}\n\nFACTS (JSON):\n{src}"
    p = active_provider(env, cfg)
    tmpl = template_fn(facts) if template_fn else json.dumps(facts, default=str)[:2000]
    if p:
        try:
            out = _call(p, SYSTEM, prompt); bad = verify_numbers(out, src)
            if bad:
                lid = log_llm(kind, symbol, p, "llm_rejected", prompt, src, out, False, f"unverifiable numbers: {bad[:6]}"); return tmpl, "template (LLM output rejected: unverifiable numbers)", lid
            lid = log_llm(kind, symbol, p, "llm", prompt, src, out, True); return out.strip() + f"\n[LLM: {p['name']}/{p['model']} - explanation only; rules engine decided]", "llm", lid
        except Exception as e:
            lid = log_llm(kind, symbol, p, "llm_error", prompt, src, "", False, str(e)[:200].replace(p["key"] or "\0", "***")); return tmpl, "template (LLM call failed)", lid
    why = ("; ".join(LAST_ERRS)[:400] if candidates(env, cfg) else ("AITL_LLM_DISABLED" if (env if env is not None else os.environ).get("AITL_LLM_DISABLED") == "1" else "no LLM API key configured")); lid = log_llm(kind, symbol, None, "template", prompt, src, tmpl, True, why); return tmpl, "template", lid

MEMO_INSTR = ("Write a short investment-research memo (max ~170 words) for this PAPER trade candidate: thesis from bullish evidence, risks from bearish evidence, the plan "
              "(stop/target/size/max loss in USD and MYR/R:R), the execution status, and one line citing the data sources and timestamps (including any headline with its timestamp). Plain text.")

def make_memo(decision_row, order=None, news=None, company=None, strategy_name="", env=None, cfg=None):
    fl = memo_fields(decision_row, order, news=news, company=company, strategy_name=strategy_name)
    text, mode, lid = explain("memo", fl["ticker"], fl, MEMO_INSTR, env, cfg, template_fn=template_text)
    fl["model_status"] = mode
    c = db.conn(); c.execute("INSERT INTO memos2(ts,symbol,strategy_id,decision_id,mode,fields,text,llm_log_id) VALUES(?,?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), fl["ticker"], decision_row.get("strategy_id"), decision_row.get("id"), mode, json.dumps(fl, default=str), text, lid)); c.commit(); c.close()
    return {"fields": fl, "text": text, "mode": mode}

def market_summary(facts, env=None, cfg=None):
    t = lambda f: f"Daily market summary {TEMPLATE_TAG}: regime {f.get('regime')} ({f.get('regime_reason')}); SPY close {f.get('spy_close')} ({f.get('spy_ret_1d_pct')}% 1d), QQQ {f.get('qqq_close')} ({f.get('qqq_ret_1d_pct')}% 1d); USD/MYR {f.get('usdmyr')} (rate date {f.get('fx_date')}); universe {f.get('universe_passed')} stocks passed filters; signals {f.get('signal_counts')}; portfolio RM{f.get('equity_myr')}."
    return explain("market_summary", "MARKET", facts, "Write a 4-6 sentence daily US market summary from these facts: index moves, regime, FX, what the engine found, and portfolio status. Plain text, no advice.", env, cfg, t)

def weekly_report(facts, env=None, cfg=None):
    t = lambda f: f"Weekly strategy report {TEMPLATE_TAG}: " + "; ".join(f"{k}: return on allocated {v.get('return_on_allocated_pct')}%, trades {v.get('n_trades')}, win rate {v.get('win_rate')}, max DD {v.get('max_drawdown_pct')}%, costs ${v.get('costs_usd')}" for k, v in (f.get("strategies") or {}).items()) + f". Benchmarks (MYR): SPY {f.get('spy_week_pct')}%, QQQ {f.get('qqq_week_pct')}%. {f.get('sample_note')}"
    return explain("weekly_report", "WEEKLY", facts, "Write a concise weekly strategy report comparing strategies after costs, mention sample-size limits (no winner declared on thin evidence), and benchmarks. Plain text.", env, cfg, t)
