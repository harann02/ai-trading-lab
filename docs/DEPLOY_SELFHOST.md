# Self-hosting on a persistent host (Fly.io / Render / VPS)

Not required (Actions + Pages is the free default) but gives real scheduler heartbeats, faster polling and live API controls.

## Env (`.env.example`)
```
DASH_USER=owner            # HTTP Basic user (every route; server answers 503 if unset)
DASH_PASS=change-me-long   # use a long random value
AITL_DB=/data/ailab.db     # persistent volume path
AITL_LIVE_DIR=/data/site_live
# optional: OPENAI_API_KEY / ANTHROPIC_API_KEY / XAI_API_KEY / GROQ_API_KEY / GEMINI_API_KEY / GITHUB_TOKEN (free GitHub Models)
# optional: ALPACA_API_KEY ALPACA_API_SECRET POLYGON_API_KEY
AITL_RATE_PER_MIN=120      # rate limits per IP (reads / writes)
AITL_RATE_WRITE_PER_MIN=20
```
## Run
```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080        # web + API (docs at /api/docs)
python scheduler.py                                      # separate scheduler process (heartbeat in the DB)
```
Migrations run automatically on start (`db.init` -> `migrations.apply_all`, DB copied to `archive/` first). **Never run two writers on one DB** (stop the Actions schedule or use a different DB).
## HTTPS
Fly.io/Render terminate TLS for you (`force_https`). On a VPS put Caddy/nginx in front: `caddy reverse-proxy --from ledger.example.com --to 127.0.0.1:8080`. Basic Auth is only safe over HTTPS.
## Fly.io sketch
`fly launch --no-deploy`; `fly volumes create data --size 1`; mount at `/data`; `fly secrets set DASH_USER=... DASH_PASS=...`; two processes in `fly.toml` (`web`: uvicorn, `scheduler`: python scheduler.py); `fly deploy`.
## Backups
`GET /api/backup` (SQLite online backup download) - schedule it with cron/curl; `POST /api/restore?confirm=RESTORE` (validates integrity, keeps a `.pre-restore-*` copy). Also Litestream or a nightly `sqlite3 ailab.db ".backup ..."` to object storage.
## Monitoring
`/healthz` (liveness, unauthenticated), `/readyz` (DB ok), `/api/status` (scheduler heartbeat, last scan, data sources), JSON-structured logs on stdout; alert on heartbeat age > 120 s.
