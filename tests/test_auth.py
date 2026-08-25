import base64
import importlib

import pytest
from fastapi.testclient import TestClient


def build(monkeypatch, dsn, password=None, username=None):
    monkeypatch.setenv("DATABASE_URL", dsn)
    if password is None:
        monkeypatch.delenv("APP_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("APP_PASSWORD", password)
    if username is None:
        monkeypatch.delenv("APP_USERNAME", raising=False)
    else:
        monkeypatch.setenv("APP_USERNAME", username)

    import app.main as main

    importlib.reload(main)
    return TestClient(main.app)


def creds(user, pw):
    raw = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def test_no_password_configured_means_no_auth(monkeypatch, dsn):
    """The app is a local tool by default; a password is opt-in for deployment."""
    client = build(monkeypatch, dsn, password=None)
    assert client.get("/health").status_code == 200


def test_password_configured_blocks_anonymous_requests(monkeypatch, dsn):
    client = build(monkeypatch, dsn, password="s3cret")
    r = client.get("/health")
    assert r.status_code == 401
    assert "Basic" in r.headers.get("www-authenticate", "")


def test_correct_credentials_are_accepted(monkeypatch, dsn):
    client = build(monkeypatch, dsn, password="s3cret")
    assert client.get("/health", headers=creds("admin", "s3cret")).status_code == 200


def test_wrong_password_is_rejected(monkeypatch, dsn):
    client = build(monkeypatch, dsn, password="s3cret")
    assert client.get("/health", headers=creds("admin", "wrong")).status_code == 401


def test_wrong_username_is_rejected(monkeypatch, dsn):
    client = build(monkeypatch, dsn, password="s3cret")
    assert client.get("/health", headers=creds("nobody", "s3cret")).status_code == 401


def test_username_is_configurable(monkeypatch, dsn):
    client = build(monkeypatch, dsn, password="s3cret", username="harshav")
    assert client.get("/health", headers=creds("harshav", "s3cret")).status_code == 200
    assert client.get("/health", headers=creds("admin", "s3cret")).status_code == 401


def test_the_ui_itself_is_protected_not_just_the_api(monkeypatch, dsn):
    """Static files must be behind the gate too, or the page loads and only its
    requests fail — which looks like a broken app, not a locked one."""
    client = build(monkeypatch, dsn, password="s3cret")
    assert client.get("/").status_code == 401
    assert client.get("/", headers=creds("admin", "s3cret")).status_code == 200


def test_a_write_route_is_protected(monkeypatch, dsn):
    client = build(monkeypatch, dsn, password="s3cret")
    r = client.post("/workbooks", json={"path": "x.xlsx", "kind": "workbook"})
    assert r.status_code == 401


def test_malformed_authorization_header_is_rejected(monkeypatch, dsn):
    client = build(monkeypatch, dsn, password="s3cret")
    for header in ({"Authorization": "Basic not-base64!!"},
                   {"Authorization": "Bearer token"},
                   {"Authorization": "Basic"}):
        assert client.get("/health", headers=header).status_code == 401
