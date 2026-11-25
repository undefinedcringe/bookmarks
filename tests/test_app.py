"""Tests. Config tests always run; API tests need a reachable PostgreSQL.

Run against any Postgres (the API tests TRUNCATE the bookmarks table):
    DB_HOST=127.0.0.1 DB_PORT=5432 DB_NAME=bookmarks_test DB_USER=... DB_PASSWORD=... pytest -q
"""

import os

import pytest

from app.config import ConfigError, load_settings

BASE_ENV = {"DB_HOST": "db", "DB_NAME": "b", "DB_USER": "u", "DB_PASSWORD": "p"}


# ---------- configuration ----------

def test_config_defaults():
    s = load_settings(BASE_ENV)
    assert (s.db_port, s.db_pool_size, s.log_level) == (5432, 5, "info")


def test_config_reports_every_missing_variable():
    with pytest.raises(ConfigError) as exc:
        load_settings({"DB_HOST": "db"})
    msg = str(exc.value)
    for name in ("DB_NAME", "DB_USER", "DB_PASSWORD"):
        assert f"{name} is required" in msg
    assert "DB_HOST" not in msg


@pytest.mark.parametrize("key,value", [("DB_PORT", "abc"), ("DB_POOL_SIZE", "0"), ("LOG_LEVEL", "loud")])
def test_config_rejects_invalid_values(key, value):
    with pytest.raises(ConfigError, match=key):
        load_settings({**BASE_ENV, key: value})


def test_config_allowed_hosts():
    assert load_settings({**BASE_ENV, "ALLOWED_HOSTS": " Book.Example , ,lan.local"}).allowed_hosts == ("book.example", "lan.local")
    with pytest.raises(ConfigError, match="ALLOWED_HOSTS"):
        load_settings({**BASE_ENV, "ALLOWED_HOSTS": "https://book.example/"})


# ---------- API (integration) ----------

needs_db = pytest.mark.skipif(not os.environ.get("DB_HOST"), reason="set DB_* env vars to run API tests")


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, base_url="http://localhost") as c:  # runs lifespan: connect + migrate
        with app.state.pool.connection() as conn:
            conn.execute("TRUNCATE bookmarks RESTART IDENTITY")
        yield c


@needs_db
def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


@needs_db
def test_health_503_when_db_unreachable(client):
    from psycopg_pool import ConnectionPool

    from app.main import app

    real_pool = app.state.pool
    # A pool pointing at a port nothing listens on behaves like a down database.
    dead_pool = ConnectionPool("host=127.0.0.1 port=1 connect_timeout=1", min_size=0, max_size=1, open=True)
    app.state.pool = dead_pool
    try:
        r = client.get("/health")
        assert r.status_code == 503 and r.json()["database"] == "unreachable"
    finally:
        app.state.pool = real_pool
        dead_pool.close()
    assert client.get("/health").status_code == 200


@needs_db
def test_create_search_tag_delete(client):
    r = client.post("/api/bookmarks", json={
        "url": "https://www.postgresql.org/docs/16/",
        "title": "Postgres docs",
        "tags": ["DB", " docs ", "db"],
    })
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["tags"] == ["db", "docs"]

    client.post("/api/bookmarks", json={"url": "https://fastapi.tiangolo.com", "title": "FastAPI", "tags": ["python"]})

    assert [b["title"] for b in client.get("/api/bookmarks", params={"q": "postgres"}).json()] == ["Postgres docs"]
    assert [b["title"] for b in client.get("/api/bookmarks", params={"tag": "python"}).json()] == ["FastAPI"]
    assert client.get("/api/bookmarks", params={"q": "100%_"}).json() == []  # LIKE wildcards are escaped
    assert {t["tag"] for t in client.get("/api/tags").json()} == {"db", "docs", "python"}

    r = client.patch(f"/api/bookmarks/{created['id']}", json={"tags": ["reference"]})
    assert r.status_code == 200 and r.json()["tags"] == ["reference"]

    assert client.delete(f"/api/bookmarks/{created['id']}").status_code == 204
    assert client.get(f"/api/bookmarks/{created['id']}").status_code == 404
    assert client.delete(f"/api/bookmarks/{created['id']}").status_code == 404


@needs_db
def test_rejects_bad_input(client):
    assert client.post("/api/bookmarks", json={"url": "javascript:alert(1)"}).status_code == 422
    assert client.post("/api/bookmarks", json={"url": "not a url"}).status_code == 422
    assert client.post("/api/bookmarks", json={"url": "https://x.test", "tags": ["a,b"]}).status_code == 422


@needs_db
def test_duplicate_url_conflicts(client):
    assert client.post("/api/bookmarks", json={"url": "https://dup.test"}).status_code == 201
    assert client.post("/api/bookmarks", json={"url": "https://dup.test"}).status_code == 409


@needs_db
def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>Bookmarks</title>" in r.text


@needs_db
@pytest.mark.parametrize("method,path,kwargs", [
    ("get", "/api/bookmarks", {"params": {"q": "a\x00b"}}),
    ("get", "/api/bookmarks", {"params": {"tag": "a\x00b"}}),
    ("post", "/api/bookmarks", {"json": {"url": "https://nul.test/\x00"}}),
    ("post", "/api/bookmarks", {"json": {"url": "https://nul.test", "title": "a\x00b"}}),
    ("post", "/api/bookmarks", {"json": {"url": "https://nul.test", "description": "a\x00b"}}),
    ("post", "/api/bookmarks", {"json": {"url": "https://nul.test", "tags": ["a\x00b"]}}),
    ("post", "/api/bookmarks", {"json": {"url": "https://nul.test", "tags": ["x"] * 101}}),
    ("get", "/api/bookmarks", {"params": {"offset": 2**64}}),
    ("get", f"/api/bookmarks/{2**64}", {}),
    ("delete", "/api/bookmarks/0", {}),
])
def test_hostile_input_is_422_not_500(client, method, path, kwargs):
    assert getattr(client, method)(path, **kwargs).status_code == 422


@needs_db
def test_unknown_host_rejected(client):
    # DNS rebinding: a foreign site pointed at 127.0.0.1 still sends its own Host header.
    assert client.get("/api/tags", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/tags", headers={"Host": "127.0.0.1:8080"}).status_code == 200
    assert client.get("/api/tags", headers={"Host": "[::1]:8080"}).status_code == 200


@needs_db
def test_security_headers(client):
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "content-security-policy" not in client.get("/docs").headers  # Swagger UI loads from a CDN
