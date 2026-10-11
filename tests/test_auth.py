import base64, pytest
from fastapi.testclient import TestClient

def hdr(u, p): return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}

@pytest.fixture
def client(env, monkeypatch):
    monkeypatch.delenv("AITL_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("DASH_USER", "alice"); monkeypatch.setenv("DASH_PASS", "s3cret-pass")
    from app.main import app
    return TestClient(app)

def test_requires_auth_everywhere(client):
    for path in ("/", "/api/status", "/api/kpis", "/static/app.js"):
        r = client.get(path); assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert client.post("/api/backtest").status_code == 401
    assert client.get("/api/status", headers=hdr("alice", "wrong")).status_code == 401
    assert client.get("/api/status", headers=hdr("bob", "s3cret-pass")).status_code == 401

def test_good_credentials_work(client):
    assert client.get("/api/status", headers=hdr("alice", "s3cret-pass")).status_code == 200
    r = client.get("/", headers=hdr("alice", "s3cret-pass")); assert r.status_code == 200 and "s3cret" not in r.text
    assert "s3cret-pass" not in client.get("/static/app.js", headers=hdr("alice", "s3cret-pass")).text

def test_fails_closed_without_config(env, monkeypatch):
    monkeypatch.delenv("AITL_AUTH_DISABLED", raising=False); monkeypatch.delenv("DASH_USER", raising=False); monkeypatch.delenv("DASH_PASS", raising=False)
    from app.main import app
    assert TestClient(app).get("/api/status").status_code == 503

def test_weekly_window_check_blocks_non_friday(env, frames):
    from app import engine, db, config
    from conftest import FakeProvider, NOW_CLOSED
    import datetime as dt
    db.set_settings({"weekly_entries_only": True})
    res = engine.scan(FakeProvider(frames), NOW_CLOSED + dt.timedelta(hours=1), market="BURSA", trade=True)   # Monday session
    assert res["markets"]["BURSA"]["counts"]["BUY"] == 0
    c = db.conn(); n = c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]; c.close(); assert n == 0
    assert config.ALGO_VERSION == "ailab-1.1.0"
