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
          "If a fact is null/missing say it is unavailable. Every number you write must appear in the JSON.")

# (env var for key, base_url, default model, wire format)
PROVIDERS = [
    ("XAI_API_KEY", "https://api.x.ai/v1", "grok-3-mini", "openai"),
    ("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4o-mini", "openai"),
    ("ANTHROPIC_API_KEY", "https://api.anthropic.com/v1", "claude-3-5-haiku-latest", "anthropic"),
    ("AITL_LLM_API_KEY", None, "", "openai"),                       # + AITL_LLM_BASE_URL (any OpenAI-compatible endpoint)
    ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "openai"),               # free tier (needs key)
    ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash", "openai"),   # free tier (needs key)
    ("GITHUB_TOKEN", "https://models.github.ai/inference", "openai/gpt-4.1-mini", "openai"),               # GitHub Models free tier
]

def candidates(env=None, cfg=None):
    env = env if env is not None else os.environ; llm = (cfg or {}).get("llm", {}); out = []
    want = llm.get("provider", "auto")
    for var, base, model, fmt in PROVIDERS:
        if not env.get(var): continue
        name = var.replace("_API_KEY", "").replace("_TOKEN", "").lower()
        if want not in ("auto", name, var.lower()): continue
        b = env.get("AITL_LLM_BASE_URL") if var == "AITL_LLM_API_KEY" else base
        if var == "AITL_LLM_API_KEY" and not b: continue
        out.append({"name": name, "key": env[var], "base": llm.get("base_url") or b, "model": llm.get("model") or env.get("AITL_LLM_MODEL") or model, "fmt": fmt})
    return out

def _ok(r):
    if not r.ok: raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]!r}")

def _call(p, system, user, max_tokens=700, timeout=45):
    if p["fmt"] == "anthropic":
        r = requests.post(p["base"] + "/messages", headers={"x-api-key": p["key"], "anthropic-version": "2023-06-01"}, timeout=timeout,
                          json={"model": p["model"], "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}], "temperature": 0.2})
        _ok(r); return r.json()["content"][0]["text"]
    r = requests.post(p["base"] + "/chat/completions", headers={"Authorization": f"Bearer {p['key']}"}, timeout=timeout,
                      json={"model": p["model"], "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "max_tokens": max_tokens, "temperature": 0.2})
    _ok(r)
    try: return r.json()["choices"][0]["message"]["content"]
    except Exception: raise RuntimeError(f"unexpected response HTTP {r.status_code} {r.headers.get('content-type')}: {r.text[:120]!r}")

_ACTIVE = {}; LAST_ERRS = []
def active_provider(env=None, cfg=None, force=False):
    """First candidate whose tiny test call succeeds. Cached per process. Returns provider dict (without leaking the key in logs) or None."""
    k = json.dumps(sorted((env if env is not None else os.environ).keys() & {v[0] for v in PROVIDERS}))
    if not force and k in _ACTIVE: return _ACTIVE[k]
    chosen, errs = None, []
    for p in candidates(env, cfg):
        try: _call(p, "Reply with the single word OK.", "ping", max_tokens=5, timeout=25); chosen = p; break
        except Exception as e: errs.append(f"{p['name']}: {str(e)[:80].replace(p['key'], '***')}")
    _ACTIVE[k] = chosen; _ACTIVE[k + ":errs"] = errs; globals()["LAST_ERRS"] = errs
    return chosen

def status(env=None, cfg=None):
    p = active_provider(env, cfg)
    return {"mode": "LLM" if p else "template", "provider": p["name"] if p else None, "model": p["model"] if p else None,
            "label": (f"LLM: {p['name']}/{p['model']} (explains verified data only)" if p else f"{TEMPLATE_TAG} - no working LLM key"),
            "errors": _ACTIVE.get(json.dumps(sorted((env if env is not None else os.environ).keys() & {v[0] for v in PROVIDERS})) + ":errs", [])}

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
            "news": news or []}

def template_text(fl):
    L = [f"{fl['ticker']} ({fl.get('company') or 'company n/a'}) - {fl['strategy']} - signal {fl['signal_status']}  {TEMPLATE_TAG}",
         f"Price ${fl['price_usd']} (bar {fl['price_bar_date']}); score {fl['score']}; regime {fl['regime']}.",
         f"Trigger: {fl['trigger']}.", f"Plan: stop ${fl['stop_usd']}, target {('$' + str(fl['target_usd'])) if fl['target_usd'] else 'none credible'}, size {fl['size_shares']} sh, max loss ${fl['max_loss_usd']} (RM{fl['max_loss_myr']}), R:R {fl['reward_risk']}.",
         "Bullish evidence: " + ("; ".join(fl["bullish_evidence"]) or "none flagged") + ".", "Bearish evidence: " + ("; ".join(fl["bearish_evidence"]) or "none flagged") + ".",
         f"Execution: {fl['execution_status']}.", "Sources: " + json.dumps(fl["data_sources"]) + (". Headlines: " + " | ".join(f"{n['published']} {n['title']}" for n in fl["news"]) if fl["news"] else ". No headlines available.")]
    return "\n".join(L)

def verify_numbers(text, source_json):
    """Every number in the LLM text must be present in the source facts (rounded forms accepted). Returns list of unverifiable numbers."""
    src = set(re.findall(r"-?\d+(?:\.\d+)?", source_json)); srcf = []
    for s in src:
        try: srcf.append(float(s))
        except Exception: pass
    bad = []
    for m in re.findall(r"-?\d+(?:\.\d+)?", text):
        v = float(m)
        if abs(v) <= 10 and float(int(v)) == v: continue          # small integers (counts, ordinals)
        if 1900 <= v <= 2100: continue
        if not any(abs(v - s) <= max(0.011, abs(s) * 0.006) for s in srcf): bad.append(m)
    return bad

def log_llm(kind, symbol, prov, mode, prompt, source, response, ok, error=None):
    c = db.conn(); cur = c.execute("INSERT INTO llm_log(ts,kind,symbol,provider,model,mode,prompt,source_data,response,ok,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), kind, symbol, prov and prov["name"], prov and prov["model"], mode, prompt, source, response, int(ok), error)); c.commit(); i = cur.lastrowid; c.close(); return i

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
            lid = log_llm(kind, symbol, p, "llm_error", prompt, src, "", False, str(e)[:200].replace(p["key"], "***")); return tmpl, "template (LLM call failed)", lid
    why = ("; ".join(LAST_ERRS)[:400] if candidates(env, cfg) else "no LLM API key configured"); lid = log_llm(kind, symbol, None, "template", prompt, src, tmpl, True, why); return tmpl, "template", lid

MEMO_INSTR = ("Write a short investment-research memo (max ~170 words) for this PAPER trade candidate: thesis from bullish evidence, risks from bearish evidence, the plan "
              "(stop/target/size/max loss in USD and MYR/R:R), the execution status, and one line citing the data sources and timestamps (including any headline with its timestamp). Plain text.")

def make_memo(decision_row, order=None, news=None, company=None, strategy_name="", env=None, cfg=None):
    fl = memo_fields(decision_row, order, news=news, company=company, strategy_name=strategy_name)
    text, mode, lid = explain("memo", fl["ticker"], fl, MEMO_INSTR, env, cfg, template_fn=template_text)
    c = db.conn(); c.execute("INSERT INTO memos2(ts,symbol,strategy_id,decision_id,mode,fields,text,llm_log_id) VALUES(?,?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), fl["ticker"], decision_row.get("strategy_id"), decision_row.get("id"), mode, json.dumps(fl, default=str), text, lid)); c.commit(); c.close()
    return {"fields": fl, "text": text, "mode": mode}

def market_summary(facts, env=None, cfg=None):
    t = lambda f: f"Daily market summary {TEMPLATE_TAG}: regime {f.get('regime')} ({f.get('regime_reason')}); SPY close {f.get('spy_close')} ({f.get('spy_ret_1d_pct')}% 1d), QQQ {f.get('qqq_close')} ({f.get('qqq_ret_1d_pct')}% 1d); USD/MYR {f.get('usdmyr')} (rate date {f.get('fx_date')}); universe {f.get('universe_passed')} stocks passed filters; signals {f.get('signal_counts')}; portfolio RM{f.get('equity_myr')}."
    return explain("market_summary", "MARKET", facts, "Write a 4-6 sentence daily US market summary from these facts: index moves, regime, FX, what the engine found, and portfolio status. Plain text, no advice.", env, cfg, t)

def weekly_report(facts, env=None, cfg=None):
    t = lambda f: f"Weekly strategy report {TEMPLATE_TAG}: " + "; ".join(f"{k}: return on allocated {v.get('return_on_allocated_pct')}%, trades {v.get('n_trades')}, win rate {v.get('win_rate')}, max DD {v.get('max_drawdown_pct')}%, costs ${v.get('costs_usd')}" for k, v in (f.get("strategies") or {}).items()) + f". Benchmarks (MYR): SPY {f.get('spy_week_pct')}%, QQQ {f.get('qqq_week_pct')}%. {f.get('sample_note')}"
    return explain("weekly_report", "WEEKLY", facts, "Write a concise weekly strategy report comparing strategies after costs, mention sample-size limits (no winner declared on thin evidence), and benchmarks. Plain text.", env, cfg, t)
