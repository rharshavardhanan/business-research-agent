import pytest

from app.models import Business
from app.store import DuplicateId, InvalidField
def seed(store, **kw):
    kw.setdefault("business_name", "ABC Dental")
    return store.upsert_many([Business(**kw)], "b.xlsx")[0][0]


def test_update_a_content_field(store):
    row = seed(store)
    out = store.update_fields(row.id, {"doctor_name": "Dr. Priya Kumar"})
    assert out.doctor_name == "Dr. Priya Kumar"
    assert store.all("b.xlsx")[0].doctor_name == "Dr. Priya Kumar"


def test_phone_is_normalized_on_save(store):
    row = seed(store)
    assert store.update_fields(row.id, {"phone": "98765 43210"}).phone == "+919876543210"


def test_unnormalizable_phone_keeps_the_users_typing(store):
    row = seed(store)
    assert store.update_fields(row.id, {"phone": "044 2223 4455"}).phone == "044 2223 4455"


def test_clearing_a_cell_stores_none_not_empty_string(store):
    row = seed(store, doctor_name="Dr. X")
    assert store.update_fields(row.id, {"doctor_name": ""}).doctor_name is None


def test_provenance_is_editable(store):
    """An explicit user decision: every column is editable."""
    row = seed(store, source="google_places", source_id="P1")
    out = store.update_fields(row.id, {"source_id": "P2", "source": "manual"})
    assert out.source_id == "P2" and out.source == "manual"


def test_duplicate_id_is_rejected_and_changes_nothing(store):
    a = seed(store, business_name="A", phone="+919876543210")
    b = seed(store, business_name="B", phone="+919876543211")
    with pytest.raises(DuplicateId):
        store.update_fields(b.id, {"id": a.id})
    assert {x.business_name for x in store.all("b.xlsx")} == {"A", "B"}


def test_id_can_be_changed_to_a_free_value(store):
    row = seed(store)
    out = store.update_fields(row.id, {"id": "chosen-id"})
    assert out.id == "chosen-id"
    assert [x.id for x in store.all("b.xlsx")] == ["chosen-id"]


def test_empty_id_is_rejected(store):
    row = seed(store)
    with pytest.raises(InvalidField, match="id"):
        store.update_fields(row.id, {"id": "  "})


def test_non_numeric_latitude_is_rejected(store):
    row = seed(store)
    with pytest.raises(InvalidField, match="latitude"):
        store.update_fields(row.id, {"latitude": "not a number"})


def test_blank_latitude_is_allowed(store):
    row = seed(store, latitude=12.9)
    assert store.update_fields(row.id, {"latitude": ""}).latitude is None


def test_unknown_field_is_rejected(store):
    row = seed(store)
    with pytest.raises(InvalidField, match="nonsense"):
        store.update_fields(row.id, {"nonsense": "x"})


def test_update_missing_row_raises(store):
    with pytest.raises(InvalidField, match="no such row"):
        store.update_fields("nope", {"doctor_name": "X"})


def test_delete_row(store):
    row = seed(store)
    assert store.delete_row(row.id) is True
    assert store.all("b.xlsx") == []
    assert store.delete_row(row.id) is False


def test_create_blank_row(store):
    row = store.create_blank("b.xlsx")
    assert row.id and row.workbook == "b.xlsx"
    assert row.business_name == ""
    assert len(store.all("b.xlsx")) == 1


# --- outreach tracking -----------------------------------------------------

from app.store import CALL_STATUSES, INTEREST_VALUES  # noqa: E402


def test_call_status_round_trips(store):
    row = seed(store)
    out = store.update_fields(row.id, {"call_status": "Picked up"})
    assert out.call_status == "Picked up"
    assert store.all("b.xlsx")[0].call_status == "Picked up"


def test_call_status_outside_the_allowed_set_is_rejected(store):
    row = seed(store)
    with pytest.raises(InvalidField, match="call_status"):
        store.update_fields(row.id, {"call_status": "picked up maybe"})


def test_call_status_allowed_values(store):
    row = seed(store)
    for value in CALL_STATUSES:
        assert store.update_fields(row.id, {"call_status": value}).call_status == value


def test_will_speak_further_is_constrained(store):
    row = seed(store)
    assert store.update_fields(row.id, {"will_speak_further": "Yes"}).will_speak_further == "Yes"
    with pytest.raises(InvalidField, match="will_speak_further"):
        store.update_fields(row.id, {"will_speak_further": "maybe"})
    assert INTEREST_VALUES == ("Yes", "No")


def test_blank_call_status_is_allowed_and_means_not_called(store):
    row = seed(store, call_status="Picked up")
    assert store.update_fields(row.id, {"call_status": ""}).call_status is None


def test_meeting_date_and_place(store):
    row = seed(store)
    out = store.update_fields(
        row.id, {"meeting_date": "2026-09-01", "meeting_place": "Clinic, Kodambakkam"}
    )
    assert out.meeting_date == "2026-09-01"
    assert out.meeting_place == "Clinic, Kodambakkam"


def test_setting_a_follow_up_date_sets_the_flag(store):
    row = seed(store)
    out = store.update_fields(row.id, {"follow_up_date": "2026-09-05"})
    assert out.follow_up_date == "2026-09-05"
    assert out.follow_up is True, "a scheduled follow-up is by definition needed"


def test_clearing_the_flag_clears_the_date(store):
    row = seed(store)
    store.update_fields(row.id, {"follow_up_date": "2026-09-05"})
    out = store.update_fields(row.id, {"follow_up": False})
    assert out.follow_up is False and out.follow_up_date is None


def test_flag_without_a_date_is_preserved(store):
    row = seed(store)
    out = store.update_fields(row.id, {"follow_up": True})
    assert out.follow_up is True and out.follow_up_date is None


def test_outreach_fields_do_not_disturb_the_record_status(store):
    row = seed(store)
    out = store.update_fields(row.id, {"call_status": "Picked up"})
    assert out.status == "new", "record status is not the call status"


# --- Postgres-specific behaviour -------------------------------------------


def test_types_come_back_native_not_text(store):
    store.upsert_many([Business(business_name="ABC", rating=4.9, review_count=568,
                                latitude=13.05, follow_up=True)], "b.xlsx")
    row = store.all("b.xlsx")[0]
    assert isinstance(row.rating, float) and row.rating == 4.9
    assert isinstance(row.review_count, int) and row.review_count == 568
    assert isinstance(row.latitude, float)
    assert row.follow_up is True


def test_deleted_rows_are_hidden_not_destroyed(store):
    store.upsert_many([Business(business_name="ABC")], "b.xlsx")
    row = store.all("b.xlsx")[0]
    assert store.delete_row(row.id) is True
    assert store.all("b.xlsx") == []
    assert store.count_deleted() == 1


def test_schema_init_is_idempotent(store):
    store.init_schema()
    store.init_schema()
    store.upsert_many([Business(business_name="ABC")], "b.xlsx")
    assert len(store.all()) == 1
