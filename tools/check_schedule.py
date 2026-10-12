"""Read-only check of unattended scheduling. Usage: GITHUB_TOKEN=... python tools/check_schedule.py [owner/repo]
Prints: workflow states (active vs disabled_inactivity), count/list of runs with event=schedule, last data-branch manifest. Never dispatches anything."""
import os, sys, json, urllib.request
repo = sys.argv[1] if len(sys.argv) > 1 else "harann02/ai-trading-lab"
def api(path):
    req = urllib.request.Request("https://api.github.com" + path, headers={"Accept": "application/vnd.github+json", **({"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"]} if os.environ.get("GITHUB_TOKEN") else {})})
    return json.load(urllib.request.urlopen(req, timeout=30))
r = api(f"/repos/{repo}"); print("default branch:", r["default_branch"], "| pushed_at", r["pushed_at"])
for w in api(f"/repos/{repo}/actions/workflows")["workflows"]: print(f"workflow {w['name']:<22} state={w['state']:<20} {w['path']}")
runs = api(f"/repos/{repo}/actions/runs?event=schedule&per_page=50"); print("schedule runs total:", runs["total_count"])
for x in runs["workflow_runs"][:30]: print(" ", x["created_at"], x["name"], x["status"], x["conclusion"], x["head_branch"], x["id"])
try: print("manifest:", json.dumps(json.load(urllib.request.urlopen(urllib.request.Request(f"https://raw.githubusercontent.com/{repo}/data/status/ledger_manifest.json"), timeout=30))))
except Exception as e: print("manifest: n/a", e)
