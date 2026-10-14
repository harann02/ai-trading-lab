# AI engine: statuses, providers, activation

**AI never decides.** The deterministic engine alone creates, sizes and executes orders. AI text is explanation: it cannot place/modify/cancel orders or change risk limits. Numbers in LLM output are validated against the source facts; text with unsupported numbers is discarded and a labeled template is used.

| Status | Meaning |
|---|---|
| `AI DEGRADED` | model output was accepted in the window, BUT the latest attempt fell back to the template or a provider probe failed this run. Shows accepted count and last fallback time. Never labelled VERIFIED. |
| `AI ACTIVE — VERIFIED` | ≥1 model-written report accepted in the window AND the latest attempt was model-written AND the provider probe in this run passed (grounded JSON). Shows provider + model. |
| `AI TEMPLATE MODE` | no provider configured (or disabled): deterministic labeled templates `[TEMPLATE …]` |
| `AI UNAVAILABLE` | providers configured but unreachable / rate-limited / not serving JSON (`network_error`, `timeout`, `rate_limited`, `server_error`, `payment_required`, `non_json_response`) |
| `AI ERROR` | auth errors or invalid/ungrounded output (`auth_error`, `invalid_json`, `ungrounded_output`, `http_error`) |

Every probe is stored in `ai_status_log` (provider, model, error category, short detail — never keys; keys are masked even inside error text).

## Providers (first one that passes the probe wins; order in `app/ai.py`)
Paid/own: `XAI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AITL_LLM_API_KEY` (+ `AITL_LLM_BASE_URL`). **Free-first:** `GROQ_API_KEY` (llama-3.3-70b), `GEMINI_API_KEY` (gemini-2.0-flash via OpenAI-compat), `OPENROUTER_API_KEY` (`:free` models), GitHub Models (`GITHUB_TOKEN`, `models: read`), and key-less Pollinations (`AITL_ALLOW_KEYLESS=1`).
**Findings (probed from GitHub Actions, workflow `ai-probe.yml`):** GitHub Models answers HTTP 200 with the plain text `OK` instead of JSON → rejected as `non_json_response` (not usable). Pollinations returned valid grounded JSON in the CI probe and locally, so the Actions workflow enables it by default (`vars.AITL_ALLOW_KEYLESS` ≠ 0). It is a third-party public endpoint with no SLA: reliability is **not proven** — the probe decides every run, and the dashboard says so.
## Activate a free key (2 minutes)
Create a free key at console.groq.com or aistudio.google.com or openrouter.ai → GitHub repo → Settings → Secrets and variables → Actions → New secret `GROQ_API_KEY` (or `GEMINI_API_KEY` / `OPENROUTER_API_KEY`). The next run probes it; status turns `AI ACTIVE — VERIFIED` only if the probe passes. To disable key-less: variable `AITL_ALLOW_KEYLESS=0`.
## Grounded memo fields
ticker, company, strategy, price, score, bullish factors, bearish factors, entry, size, stop, target methodology, R:R, max risk, rejection reasons, sources + timestamps, model status, authority restrictions (`memos2.fields`).
