import pytest
from fastapi.testclient import TestClient

from app.models import Business


class _FakeProvider:
    name = "fake"

    def search(self, query, location, limit, radius_m=None):
        return [
            Business(business_name="ABC Dental", phone="+919876543210", area=location),
            Business(business_name="Smile Care", phone="+919999999999", area=location),
        ]


async def _passthrough(businesses):
    return businesses


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib

    import app.main as main
    import app.workbooks as workbooks

    # Patch the attribute; do NOT reload app.workbooks. Reloading rebuilds its
    # classes, so the InvalidPath other test modules imported at collection time
    # stops matching the InvalidPath raised here, and `pytest.raises` misses.
    monkeypatch.setattr(workbooks, "DATA_DIR", tmp_path)
    importlib.reload(main)
    monkeypatch.setattr("app.research.get_provider", lambda name=None: _FakeProvider())
    monkeypatch.setattr("app.research.enrich_all", _passthrough)
    return TestClient(main.app)


def test_health_reports_provider_and_key_status(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "provider" in body and "gemini_key_configured" in body
    assert "model" in body


def test_command_without_api_key_returns_400_with_actionable_message(client, monkeypatch):
    # Both names must go: a real key in .env would otherwise send this test to
    # the live API and burn free-tier quota.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    r = client.post("/command", json={"command": "Find dental clinics in Chromepet"})
    assert r.status_code == 400
    assert "GEMINI_API_KEY" in r.json()["detail"]


def test_search_then_save_uses_buffer(client):
    r = client.post("/search", json={"business_type": "dental clinic", "location": "Chromepet"})
    assert r.status_code == 200 and r.json()["count"] >= 1
    r2 = client.post("/businesses/save", json={})
    assert r2.json()["new"] >= 1
    assert client.get("/businesses").json()["count"] >= 1


def test_save_with_empty_buffer_is_a_clean_no_op(client):
    r = client.post("/businesses/save", json={})
    assert r.status_code == 200
    assert r.json() == {"new": 0, "updated": 0, "existing": 0, "review": 0}


def test_saving_twice_does_not_duplicate(client):
    client.post("/search", json={"business_type": "dental clinic", "location": "Chromepet"})
    client.post("/businesses/save", json={})
    first = client.get("/businesses").json()["count"]
    client.post("/search", json={"business_type": "dental clinic", "location": "Chromepet"})
    client.post("/businesses/save", json={})
    assert client.get("/businesses").json()["count"] == first


def test_save_writes_the_workbook(client, tmp_path):
    client.post("/search", json={"business_type": "dental clinic", "location": "Chromepet"})
    client.post("/businesses/save", json={})
    assert (tmp_path / "businesses.xlsx").exists()


def test_ui_is_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


def test_rate_limited_returns_429_not_500(client, monkeypatch):
    from app.parser import RateLimited

    def boom(text):
        raise RateLimited("Gemini free tier allows 5 requests per minute")

    monkeypatch.setattr("app.main.parse_command", boom)
    r = client.post("/command", json={"command": "Find dental clinics in Chromepet"})
    assert r.status_code == 429
    assert "5 requests per minute" in r.json()["detail"]


def test_parser_error_returns_502_with_reason(client, monkeypatch):
    from app.parser import ParserError

    def boom(text):
        raise ParserError("Gemini rejected the request: API key not valid")

    monkeypatch.setattr("app.main.parse_command", boom)
    r = client.post("/command", json={"command": "Find dental clinics"})
    assert r.status_code == 502
    assert "API key not valid" in r.json()["detail"]


def test_health_reports_the_model_actually_used(client):
    """/health must not carry its own copy of the default model name."""
    from app.parser import DEFAULT_MODEL

    assert client.get("/health").json()["model"] == DEFAULT_MODEL


def test_search_notices_when_it_returns_fewer_than_requested(client):
    """Asking for 150 and silently getting 60 looks like a broken app."""
    r = client.post("/search", json={"business_type": "dental clinic",
                                     "location": "Chromepet", "quantity": 150})
    body = r.json()
    assert body["count"] < 150
    assert body["notice"], "must explain the shortfall"
    assert "150" in body["notice"]


def test_no_notice_when_the_request_is_satisfied(client):
    r = client.post("/search", json={"business_type": "dental clinic",
                                     "location": "Chromepet", "quantity": 2})
    assert r.json()["notice"] is None


# --- workbook management ---------------------------------------------------


def test_workbook_tree_endpoint(client):
    r = client.get("/workbooks")
    assert r.status_code == 200 and r.json()["type"] == "folder"


def test_create_workbook_then_it_appears_in_the_tree(client):
    assert (
        client.post(
            "/workbooks", json={"path": "dental/chennai.xlsx", "kind": "workbook"}
        ).status_code
        == 200
    )
    names = [c["name"] for c in client.get("/workbooks").json()["children"]]
    assert "dental" in names


def test_traversal_path_is_rejected_with_400(client):
    r = client.post("/workbooks", json={"path": "../../evil.xlsx", "kind": "workbook"})
    assert r.status_code == 400


def test_download_rejects_traversal(client):
    r = client.get("/workbooks/download", params={"path": "../../etc/passwd"})
    assert r.status_code == 400


def test_download_streams_an_xlsx(client):
    client.post("/workbooks", json={"path": "a.xlsx", "kind": "workbook"})
    r = client.get("/workbooks/download", params={"path": "a.xlsx"})
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]


def test_save_targets_the_named_workbook(client):
    client.post("/workbooks", json={"path": "a.xlsx", "kind": "workbook"})
    client.post(
        "/search",
        json={"business_type": "dental clinic", "location": "Chromepet", "workbook": "a.xlsx"},
    )
    assert client.post("/businesses/save", json={"workbook": "a.xlsx"}).json()["new"] >= 1
    assert client.get("/businesses", params={"workbook": "a.xlsx"}).json()["count"] >= 1
    assert client.get("/businesses", params={"workbook": "b.xlsx"}).json()["count"] == 0


def test_rename_carries_the_rows(client):
    client.post("/workbooks", json={"path": "a.xlsx", "kind": "workbook"})
    client.post(
        "/search",
        json={"business_type": "dental clinic", "location": "Chromepet", "workbook": "a.xlsx"},
    )
    client.post("/businesses/save", json={"workbook": "a.xlsx"})
    before = client.get("/businesses", params={"workbook": "a.xlsx"}).json()["count"]
    assert before > 0

    assert client.patch("/workbooks", json={"src": "a.xlsx", "dst": "b.xlsx"}).status_code == 200
    assert client.get("/businesses", params={"workbook": "b.xlsx"}).json()["count"] == before
    assert client.get("/businesses", params={"workbook": "a.xlsx"}).json()["count"] == 0


def test_delete_workbook_removes_its_rows(client):
    client.post("/workbooks", json={"path": "a.xlsx", "kind": "workbook"})
    client.post(
        "/search",
        json={"business_type": "dental clinic", "location": "Chromepet", "workbook": "a.xlsx"},
    )
    client.post("/businesses/save", json={"workbook": "a.xlsx"})
    r = client.request("DELETE", "/workbooks", json={"path": "a.xlsx", "kind": "workbook"})
    assert r.status_code == 200 and r.json()["trashed_as"].endswith("a.xlsx")
    assert client.get("/businesses", params={"workbook": "a.xlsx"}).json()["count"] == 0


def test_delete_non_empty_folder_returns_400(client):
    client.post("/workbooks", json={"path": "dental/x.xlsx", "kind": "workbook"})
    r = client.request("DELETE", "/workbooks", json={"path": "dental", "kind": "folder"})
    assert r.status_code == 400 and "not empty" in r.json()["detail"]


# --- workbook editor -------------------------------------------------------


def _saved(client, workbook="a.xlsx"):
    client.post("/workbooks", json={"path": workbook, "kind": "workbook"})
    client.post("/search", json={"business_type": "dental clinic",
                                 "location": "Chromepet", "workbook": workbook})
    client.post("/businesses/save", json={"workbook": workbook})
    return client.get("/businesses", params={"workbook": workbook}).json()["businesses"]


def test_patch_updates_a_cell_and_the_workbook(client, tmp_path):
    from openpyxl import load_workbook

    from app.excel import COLUMNS

    rows = _saved(client)
    r = client.patch(f"/businesses/{rows[0]['id']}",
                     json={"changes": {"doctor_name": "Dr. Priya Kumar"},
                           "workbook": "a.xlsx"})
    assert r.status_code == 200
    assert r.json()["business"]["doctor_name"] == "Dr. Priya Kumar"

    ws = load_workbook(tmp_path / "a.xlsx")["Businesses"]
    col = COLUMNS.index("Doctor Name") + 1
    values = [ws.cell(row=i, column=col).value for i in range(2, ws.max_row + 1)]
    assert "Dr. Priya Kumar" in values, "the sheet must be re-rendered"


def test_patch_normalizes_a_phone(client):
    rows = _saved(client)
    r = client.patch(f"/businesses/{rows[0]['id']}",
                     json={"changes": {"phone": "98765 43210"}, "workbook": "a.xlsx"})
    assert r.json()["business"]["phone"] == "+919876543210"


def test_patch_duplicate_id_returns_400(client):
    rows = _saved(client)
    assert len(rows) >= 2
    r = client.patch(f"/businesses/{rows[1]['id']}",
                     json={"changes": {"id": rows[0]["id"]}, "workbook": "a.xlsx"})
    assert r.status_code == 400 and "already used" in r.json()["detail"]


def test_patch_bad_latitude_returns_400(client):
    rows = _saved(client)
    r = client.patch(f"/businesses/{rows[0]['id']}",
                     json={"changes": {"latitude": "abc"}, "workbook": "a.xlsx"})
    assert r.status_code == 400


def test_delete_row_endpoint(client):
    rows = _saved(client)
    before = len(rows)
    assert client.request("DELETE", f"/businesses/{rows[0]['id']}",
                          json={"workbook": "a.xlsx"}).status_code == 200
    after = client.get("/businesses", params={"workbook": "a.xlsx"}).json()["count"]
    assert after == before - 1


def test_add_blank_row_endpoint(client):
    _saved(client)
    r = client.post("/businesses", json={"workbook": "a.xlsx"})
    assert r.status_code == 200 and r.json()["business"]["id"]
    assert r.json()["business"]["workbook"] == "a.xlsx"


def test_open_adopts_hand_typed_rows(client, tmp_path):
    from openpyxl import load_workbook

    from app.excel import COLUMNS

    _saved(client)
    wb = load_workbook(tmp_path / "a.xlsx")
    ws = wb["Businesses"]
    ws.cell(row=ws.max_row + 1, column=COLUMNS.index("Business Name") + 1,
            value="Typed By Hand")
    wb.save(tmp_path / "a.xlsx")

    r = client.post("/workbooks/open", json={"workbook": "a.xlsx"})
    assert r.status_code == 200 and r.json()["adopted"] == 1
    assert "Typed By Hand" in [b["business_name"] for b in r.json()["businesses"]]


def test_open_is_idempotent_through_the_api(client, tmp_path):
    from openpyxl import load_workbook

    from app.excel import COLUMNS

    _saved(client)
    wb = load_workbook(tmp_path / "a.xlsx")
    ws = wb["Businesses"]
    ws.cell(row=ws.max_row + 1, column=COLUMNS.index("Business Name") + 1,
            value="Typed By Hand")
    wb.save(tmp_path / "a.xlsx")

    first = client.post("/workbooks/open", json={"workbook": "a.xlsx"}).json()
    second = client.post("/workbooks/open", json={"workbook": "a.xlsx"}).json()
    assert second["adopted"] == 0
    assert second["count"] == first["count"]
