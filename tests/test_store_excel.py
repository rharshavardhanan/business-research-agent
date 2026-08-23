import pytest

from app.models import Business
from app.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.db")


def B(**kw):
    kw.setdefault("business_name", "X")
    return Business(**kw)


def test_upsert_new_then_existing(store):
    tagged = store.upsert_many([B(business_name="ABC Dental", phone="+919876543210")])
    assert [t for _, t in tagged] == ["new"]
    tagged = store.upsert_many([B(business_name="ABC Dental", phone="+919876543210")])
    assert [t for _, t in tagged] == ["existing"]
    assert len(store.all()) == 1


def test_upsert_updates_when_new_info_arrives(store):
    store.upsert_many([B(business_name="ABC Dental", phone="+919876543210")])
    tagged = store.upsert_many(
        [
            B(
                business_name="ABC Dental",
                phone="+919876543210",
                website="https://abcdental.com",
            )
        ]
    )
    assert [t for _, t in tagged] == ["updated"]
    rows = store.all()
    assert len(rows) == 1 and rows[0].website == "https://abcdental.com"


def test_persists_across_instances(tmp_path):
    p = tmp_path / "test.db"
    Store(p).upsert_many([B(business_name="ABC Dental")])
    assert len(Store(p).all()) == 1


def test_filter_without_website(store):
    store.upsert_many(
        [
            B(business_name="Has Site", phone="+919876543210", website="https://a.com"),
            B(business_name="No Site", phone="+919876543211"),
        ]
    )
    assert [b.business_name for b in store.filter("without_website")] == ["No Site"]


def test_sources_json_round_trips(store):
    store.upsert_many(
        [B(business_name="ABC", sources={"doctor_name": "https://a.com/about"})]
    )
    assert store.all()[0].sources == {"doctor_name": "https://a.com/about"}


def test_duplicates_within_one_batch_collapse(store):
    tagged = store.upsert_many(
        [
            B(business_name="ABC Dental", phone="+919876543210"),
            B(business_name="ABC Dental Clinic", phone="098765 43210"),
        ]
    )
    assert [t for _, t in tagged] == ["new", "existing"]
    assert len(store.all()) == 1


def test_dedupe_existing_collapses_high_confidence_only(store):
    store.upsert_many([B(business_name="ABC Dental", phone="+919876543210")])
    # Sneak a duplicate past upsert by writing with a different phone, then
    # giving it the same website so a later dedupe pass finds it.
    store.upsert_many([B(business_name="ABC Dental Care", area="Chromepet")])
    assert len(store.all()) == 2
    removed = store.dedupe_existing()
    assert removed == 0, "MEDIUM-confidence pairs must never be auto-deleted"
    assert len(store.all()) == 2


# --- Excel -----------------------------------------------------------------

from openpyxl import load_workbook  # noqa: E402

from app.excel import COLUMNS, sync  # noqa: E402


def test_creates_workbook_with_headers(tmp_path):
    p = tmp_path / "businesses.xlsx"
    sync([B(business_name="ABC Dental")], p)
    ws = load_workbook(p)["Businesses"]
    assert [c.value for c in ws[1]] == COLUMNS
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref is not None


def test_updates_in_place_without_duplicating_rows(tmp_path):
    p = tmp_path / "businesses.xlsx"
    b = B(business_name="ABC Dental", id="fixed-id", phone="+919876543210")
    sync([b], p)
    b2 = b.model_copy(update={"website": "https://abcdental.com"})
    sync([b2], p)
    ws = load_workbook(p)["Businesses"]
    assert ws.max_row == 2, "second sync must update the row, not append one"
    assert (
        ws.cell(row=2, column=COLUMNS.index("Website") + 1).value
        == "https://abcdental.com"
    )


def test_preserves_manual_column_width_on_resync(tmp_path):
    p = tmp_path / "businesses.xlsx"
    sync([B(business_name="ABC", id="fixed-id")], p)
    wb = load_workbook(p)
    wb["Businesses"].column_dimensions["A"].width = 99
    wb.save(p)
    sync([B(business_name="ABC", id="fixed-id")], p)
    assert load_workbook(p)["Businesses"].column_dimensions["A"].width == 99


def test_website_cell_is_hyperlinked(tmp_path):
    p = tmp_path / "businesses.xlsx"
    sync([B(business_name="ABC", id="i", website="https://abcdental.com")], p)
    ws = load_workbook(p)["Businesses"]
    assert ws.cell(row=2, column=COLUMNS.index("Website") + 1).hyperlink is not None


def test_null_fields_render_blank_not_the_string_none(tmp_path):
    p = tmp_path / "businesses.xlsx"
    sync([B(business_name="ABC", id="i")], p)
    ws = load_workbook(p)["Businesses"]
    doctor = ws.cell(row=2, column=COLUMNS.index("Doctor Name") + 1).value
    assert doctor in (None, ""), f"expected blank, got {doctor!r}"


# --- workbook scoping ------------------------------------------------------

from app.store import DEFAULT_WORKBOOK  # noqa: E402


def test_same_business_in_two_workbooks_is_two_rows(store):
    b = B(business_name="ABC Dental", phone="+919876543210")
    assert [t for _, t in store.upsert_many([b], "a.xlsx")] == ["new"]
    assert [t for _, t in store.upsert_many([b], "b.xlsx")] == ["new"]
    assert len(store.all()) == 2
    assert len(store.all("a.xlsx")) == 1


def test_same_business_twice_in_one_workbook_is_one_row(store):
    b = B(business_name="ABC Dental", phone="+919876543210")
    store.upsert_many([b], "a.xlsx")
    assert [t for _, t in store.upsert_many([b], "a.xlsx")] == ["existing"]
    assert len(store.all("a.xlsx")) == 1


def test_filter_is_scoped_to_one_workbook(store):
    store.upsert_many([B(business_name="No Site A", phone="+919876543210")], "a.xlsx")
    store.upsert_many([B(business_name="No Site B", phone="+919876543211")], "b.xlsx")
    assert [x.business_name for x in store.filter("without_website", "a.xlsx")] == [
        "No Site A"
    ]


def test_move_rows_follows_a_renamed_workbook(store):
    store.upsert_many([B(business_name="ABC")], "a.xlsx")
    assert store.move_rows("a.xlsx", "dental/b.xlsx") == 1
    assert len(store.all("a.xlsx")) == 0
    assert len(store.all("dental/b.xlsx")) == 1


def test_delete_rows_removes_only_that_workbook(store):
    store.upsert_many([B(business_name="A")], "a.xlsx")
    store.upsert_many([B(business_name="B")], "b.xlsx")
    assert store.delete_rows("a.xlsx") == 1
    assert len(store.all()) == 1


def test_dedupe_existing_is_scoped_to_one_workbook(store):
    b = B(business_name="ABC Dental", phone="+919876543210")
    store.upsert_many([b], "a.xlsx")
    store.upsert_many([b], "b.xlsx")
    assert store.dedupe_existing("a.xlsx") == 0, "cross-workbook rows are not duplicates"
    assert len(store.all()) == 2


def test_migration_backfills_rows_written_before_the_column_existed(tmp_path):
    import sqlite3

    from app.store import Store

    p = tmp_path / "legacy.db"
    cols = [f for f in Business.model_fields if f != "workbook"]
    with sqlite3.connect(p) as conn:
        conn.execute(
            "CREATE TABLE businesses ("
            + ", ".join(
                f"{c} TEXT PRIMARY KEY" if c == "id" else f"{c} TEXT" for c in cols
            )
            + ")"
        )
        conn.execute("INSERT INTO businesses (id, business_name) VALUES ('1', 'Legacy')")

    rows = Store(p).all()
    assert len(rows) == 1
    assert rows[0].workbook == DEFAULT_WORKBOOK, "existing rows must not orphan"


def test_sync_removes_rows_no_longer_in_the_store(tmp_path):
    """After dedupe drops a row, the sheet must not keep showing it."""
    p = tmp_path / "businesses.xlsx"
    keep = B(business_name="Keeper", id="keep-id")
    gone = B(business_name="Dropped", id="gone-id")
    sync([keep, gone], p)
    assert load_workbook(p)["Businesses"].max_row == 3

    sync([keep], p)
    ws = load_workbook(p)["Businesses"]
    names = [ws.cell(r, COLUMNS.index("Business Name") + 1).value
             for r in range(2, ws.max_row + 1)]
    assert names == ["Keeper"], f"stale row survived: {names}"


def test_sync_preserves_hand_added_rows_without_an_id(tmp_path):
    """The user edits this file. A row they typed has no ID and is not ours to delete."""
    p = tmp_path / "businesses.xlsx"
    sync([B(business_name="Keeper", id="keep-id")], p)

    wb = load_workbook(p)
    ws = wb["Businesses"]
    ws.cell(row=ws.max_row + 1, column=COLUMNS.index("Business Name") + 1,
            value="Typed By Hand")
    wb.save(p)

    sync([B(business_name="Keeper", id="keep-id")], p)
    ws = load_workbook(p)["Businesses"]
    names = [ws.cell(r, COLUMNS.index("Business Name") + 1).value
             for r in range(2, ws.max_row + 1)]
    assert "Typed By Hand" in names, "a hand-added row must survive"


# --- read-back and adoption ------------------------------------------------

from app.excel import read_rows  # noqa: E402


def test_read_rows_round_trips_a_written_sheet(tmp_path):
    p = tmp_path / "b.xlsx"
    sync([B(business_name="ABC Dental", id="i1", phone="+919876543210",
            sources={"doctor_name": "https://x.com/about"})], p)
    rows = read_rows(p)
    assert len(rows) == 1
    assert rows[0]["business_name"] == "ABC Dental"
    assert rows[0]["id"] == "i1"
    assert rows[0]["sources"] == {"doctor_name": "https://x.com/about"}


def test_read_rows_reports_hand_typed_rows_with_no_id(tmp_path):
    p = tmp_path / "b.xlsx"
    sync([B(business_name="From App", id="i1")], p)
    wb = load_workbook(p)
    ws = wb["Businesses"]
    ws.cell(row=3, column=COLUMNS.index("Business Name") + 1, value="Typed By Hand")
    ws.cell(row=3, column=COLUMNS.index("Phone") + 1, value="+919812345678")
    wb.save(p)

    orphans = [r for r in read_rows(p) if not r.get("id")]
    assert [r["business_name"] for r in orphans] == ["Typed By Hand"]


def test_adopt_imports_hand_typed_rows(store, tmp_path):
    p = tmp_path / "b.xlsx"
    store.upsert_many([B(business_name="From App", phone="+919876543210")], "b.xlsx")
    sync(store.all("b.xlsx"), p)

    wb = load_workbook(p)
    ws = wb["Businesses"]
    ws.cell(row=ws.max_row + 1, column=COLUMNS.index("Business Name") + 1,
            value="Typed By Hand")
    wb.save(p)

    assert store.adopt("b.xlsx", p) == 1
    assert sorted(x.business_name for x in store.all("b.xlsx")) == ["From App", "Typed By Hand"]
    assert all(x.id for x in store.all("b.xlsx")), "adopted rows get an ID"


def test_adopt_is_idempotent(store, tmp_path):
    p = tmp_path / "b.xlsx"
    sync([], p)
    wb = load_workbook(p)
    wb["Businesses"].cell(row=2, column=COLUMNS.index("Business Name") + 1,
                          value="Typed By Hand")
    wb.save(p)

    assert store.adopt("b.xlsx", p) == 1
    sync(store.all("b.xlsx"), p)
    assert store.adopt("b.xlsx", p) == 0, "an adopted row must not be adopted twice"
    assert len(store.all("b.xlsx")) == 1


def test_adopted_duplicate_merges_rather_than_doubling(store, tmp_path):
    p = tmp_path / "b.xlsx"
    store.upsert_many([B(business_name="ABC Dental", phone="+919876543210")], "b.xlsx")
    sync(store.all("b.xlsx"), p)

    wb = load_workbook(p)
    ws = wb["Businesses"]
    r = ws.max_row + 1
    ws.cell(row=r, column=COLUMNS.index("Business Name") + 1, value="ABC Dental Clinic")
    ws.cell(row=r, column=COLUMNS.index("Phone") + 1, value="+919876543210")
    wb.save(p)

    store.adopt("b.xlsx", p)
    assert len(store.all("b.xlsx")) == 1, "same phone is the same lead"


def test_rating_round_trips_through_the_store(store):
    store.upsert_many([B(business_name="ABC", rating=4.9, review_count=568,
                         business_status="OPERATIONAL")], "b.xlsx")
    row = store.all("b.xlsx")[0]
    assert row.rating == 4.9 and row.review_count == 568
    assert row.business_status == "OPERATIONAL"


def test_rating_columns_appear_in_the_workbook(tmp_path):
    p = tmp_path / "b.xlsx"
    sync([B(business_name="ABC", id="i", rating=4.9, review_count=568,
            business_status="OPERATIONAL")], p)
    ws = load_workbook(p)["Businesses"]
    assert "Rating" in COLUMNS and "Reviews" in COLUMNS
    assert ws.cell(row=2, column=COLUMNS.index("Rating") + 1).value == 4.9
    assert ws.cell(row=2, column=COLUMNS.index("Reviews") + 1).value == 568


def test_migration_adds_any_missing_column(tmp_path):
    import sqlite3

    from app.store import Store

    p = tmp_path / "legacy.db"
    cols = [f for f in Business.model_fields
            if f not in ("rating", "review_count", "business_status")]
    with sqlite3.connect(p) as conn:
        conn.execute("CREATE TABLE businesses ("
                     + ", ".join(f"{c} TEXT PRIMARY KEY" if c == "id" else f"{c} TEXT"
                                 for c in cols) + ")")
        conn.execute("INSERT INTO businesses (id, business_name, workbook) "
                     "VALUES ('1', 'Legacy', 'businesses.xlsx')")

    rows = Store(p).all()
    assert len(rows) == 1 and rows[0].rating is None


def test_outreach_columns_in_the_workbook(tmp_path):
    p = tmp_path / "b.xlsx"
    sync([B(business_name="ABC", id="i", call_status="Picked up",
            will_speak_further="Yes", meeting_date="2026-09-01",
            meeting_place="Clinic", follow_up=True, follow_up_date="2026-09-05")], p)
    ws = load_workbook(p)["Businesses"]
    for name in ("Call Status", "Will Speak Further", "Meeting Date",
                 "Meeting Place", "Follow Up", "Follow Up Date"):
        assert name in COLUMNS
    assert ws.cell(row=2, column=COLUMNS.index("Call Status") + 1).value == "Picked up"
    assert ws.cell(row=2, column=COLUMNS.index("Follow Up") + 1).value == "Yes"


def test_follow_up_false_renders_blank_not_false(tmp_path):
    p = tmp_path / "b.xlsx"
    sync([B(business_name="ABC", id="i", follow_up=False)], p)
    ws = load_workbook(p)["Businesses"]
    assert ws.cell(row=2, column=COLUMNS.index("Follow Up") + 1).value in (None, "")


def test_outreach_survives_a_re_search(store):
    """A re-search must never wipe the user's own observations."""
    store.upsert_many([B(business_name="ABC", phone="+919876543210")], "b.xlsx")
    row = store.all("b.xlsx")[0]
    store.update_fields(row.id, {"call_status": "Picked up", "will_speak_further": "Yes"})
    store.upsert_many([B(business_name="ABC Dental", phone="+919876543210")], "b.xlsx")
    after = store.all("b.xlsx")[0]
    assert after.call_status == "Picked up" and after.will_speak_further == "Yes"


def test_read_rows_reverses_the_follow_up_rendering(tmp_path):
    """read_rows must undo what _write_row did, or the model rejects the value."""
    p = tmp_path / "b.xlsx"
    sync([B(business_name="Yes Row", id="i1", follow_up=True),
          B(business_name="No Row", id="i2", follow_up=False)], p)
    rows = {r["business_name"]: r for r in read_rows(p)}
    assert rows["Yes Row"]["follow_up"] is True
    assert rows["No Row"]["follow_up"] is False


def test_short_info_columns_in_the_workbook(tmp_path):
    p = tmp_path / "b.xlsx"
    sync([B(business_name="ABC", id="i", short_info="A dental clinic in Chennai.",
            short_info_source="Website")], p)
    ws = load_workbook(p)["Businesses"]
    assert "Short Info" in COLUMNS and "Info Source" in COLUMNS
    assert ws.cell(row=2, column=COLUMNS.index("Short Info") + 1).value == \
        "A dental clinic in Chennai."
    assert ws.cell(row=2, column=COLUMNS.index("Info Source") + 1).value == "Website"
