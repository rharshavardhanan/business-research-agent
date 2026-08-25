import pytest

from app.models import Business
from app.workbooks import (
    InvalidPath,
    create_folder,
    create_workbook,
    delete_folder,
    delete_workbook,
    rename_or_move,
    tree,
    validate,
)


@pytest.mark.parametrize(
    "evil",
    ["../../etc/passwd", "/etc/passwd", "a/../../b.xlsx", "..", "", "   ",
     "a//b.xlsx", "a/./b.xlsx", "x" * 300],
)
def test_invalid_paths_are_rejected(evil):
    with pytest.raises(InvalidPath):
        validate(evil)


def test_validate_normalises_a_good_path():
    assert validate("dental/chennai.xlsx", require_xlsx=True) == "dental/chennai.xlsx"
    assert validate("  dental / chennai.xlsx  ") == "dental/chennai.xlsx"


def test_require_xlsx_is_enforced():
    with pytest.raises(InvalidPath, match=".xlsx"):
        validate("notes.txt", require_xlsx=True)


def test_folders_are_path_prefixes_not_rows(store):
    create_workbook(store, "dental/chennai.xlsx")
    t = tree(store)
    dental = next(c for c in t["children"] if c["name"] == "dental")
    assert dental["type"] == "folder"
    assert [c["path"] for c in dental["children"]] == ["dental/chennai.xlsx"]


def test_an_empty_folder_survives_as_a_marker_row(store):
    create_folder(store, "empty")
    assert [c["name"] for c in tree(store)["children"]] == ["empty"]


def test_create_refuses_to_clobber(store):
    create_workbook(store, "a.xlsx")
    with pytest.raises(InvalidPath, match="already exists"):
        create_workbook(store, "a.xlsx")


def test_a_folder_may_not_end_in_xlsx(store):
    with pytest.raises(InvalidPath, match=".xlsx"):
        create_folder(store, "wrong.xlsx")


def test_rename_carries_the_rows_by_cascade(store):
    create_workbook(store, "a.xlsx")
    store.upsert_many([Business(business_name="ABC")], "a.xlsx")
    assert rename_or_move(store, "a.xlsx", "dental/b.xlsx") == "dental/b.xlsx"
    assert len(store.all("dental/b.xlsx")) == 1
    assert len(store.all("a.xlsx")) == 0


def test_renaming_a_folder_moves_what_is_inside_it(store):
    create_folder(store, "old")
    create_workbook(store, "old/a.xlsx")
    store.upsert_many([Business(business_name="ABC")], "old/a.xlsx")
    rename_or_move(store, "old", "new")
    assert len(store.all("new/a.xlsx")) == 1


def test_rename_refuses_to_overwrite(store):
    create_workbook(store, "a.xlsx")
    create_workbook(store, "b.xlsx")
    with pytest.raises(InvalidPath, match="already exists"):
        rename_or_move(store, "a.xlsx", "b.xlsx")


def test_delete_workbook_soft_deletes_its_rows(store):
    create_workbook(store, "a.xlsx")
    store.upsert_many([Business(business_name="ABC")], "a.xlsx")
    assert delete_workbook(store, "a.xlsx") == 1
    assert store.all("a.xlsx") == []
    assert "a.xlsx" not in [c["path"] for c in tree(store)["children"]]
    assert store.count_deleted() == 1, "hidden, not destroyed"


def test_delete_empty_folder(store):
    create_folder(store, "empty")
    delete_folder(store, "empty")
    assert tree(store)["children"] == []


def test_delete_non_empty_folder_is_refused(store):
    create_workbook(store, "dental/chennai.xlsx")
    create_folder(store, "dental")
    with pytest.raises(InvalidPath, match="not empty"):
        delete_folder(store, "dental")
    assert len(store.all("dental/chennai.xlsx")) == 0
    assert "dental" in [c["name"] for c in tree(store)["children"]]


def test_deleting_a_missing_path_is_refused(store):
    with pytest.raises(InvalidPath, match="does not exist"):
        delete_workbook(store, "nope.xlsx")
