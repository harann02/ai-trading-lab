"""Diagnostic: which FREE LLM endpoints answer valid JSON from this environment? Prints status/content-type/short body only (never the token)."""
import os, sys, json, requests
TOK = os.environ.get("GITHUB_TOKEN", "")
PROMPT = 'Return ONLY this JSON object with the number 42 as value: {"answer": 42}'
def attempt(name, url, model, headers, json_mode):
    body = {"model": model, "messages": [{"role": "system", "content": "You output strict JSON only."}, {"role": "user", "content": PROMPT}], "max_tokens": 40, "temperature": 0}
    if json_mode: body["response_format"] = {"type": "json_object"}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=45)
        txt = r.text.replace(TOK, "***") if TOK else r.text
        ok = False
        try: ok = json.loads(r.json()["choices"][0]["message"]["content"]).get("answer") == 42
        except Exception: pass
        print(f"{name:34s} {model:34s} json_mode={json_mode!s:5} HTTP {r.status_code} ct={r.headers.get('content-type','')[:30]!r} valid_grounded_json={ok} body={txt[:160]!r}")
    except Exception as e: print(f"{name:34s} {model:34s} ERROR {type(e).__name__}: {str(e)[:120]}")
gh = {"Authorization": f"Bearer {TOK}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"}
for m in ["openai/gpt-4o-mini", "openai/gpt-4.1-mini", "openai/gpt-4.1-nano", "meta/Llama-3.3-70B-Instruct", "microsoft/Phi-4"]:
    for jm in (False, True): attempt("models.github.ai", "https://models.github.ai/inference/chat/completions", m, gh, jm)
for m in ["gpt-4o-mini", "Meta-Llama-3.1-8B-Instruct"]:
    attempt("models.inference.ai.azure.com", "https://models.inference.ai.azure.com/chat/completions", m, gh, False)
for m in ["openai", "openai-fast"]:
    attempt("pollinations (keyless)", "https://text.pollinations.ai/openai", m, {"Content-Type": "application/json"}, False)
try:
    r = requests.get("https://models.github.ai/catalog/models", headers=gh, timeout=30); print("catalog", r.status_code, r.headers.get("content-type"), r.text[:200].replace(TOK, "***"))
except Exception as e: print("catalog error", e)
