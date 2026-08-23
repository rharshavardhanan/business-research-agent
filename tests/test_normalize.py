import pytest

from app.normalize import (
    normalize_address,
    normalize_name,
    normalize_phone,
    normalize_url,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+91 98765 43210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("9876543210", "+919876543210"),
        ("+91-98765-43210", "+919876543210"),
        ("91 9876543210", "+919876543210"),
        ("044 2223 4455", None),  # landline, leading digit not 6-9
        ("12345", None),  # too short
        ("", None),
        (None, None),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://example.com/", "example.com"),
        ("http://example.com", "example.com"),
        ("https://WWW.Example.com/about", "example.com/about"),
        ("example.com", "example.com"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_normalize_name_strips_generic_suffixes():
    assert normalize_name("ABC Dental Clinic") == normalize_name("ABC Dental")
    assert normalize_name("ABC Dental Centre") == normalize_name("ABC Dental")
    assert normalize_name("A.B.C. Dental Pvt. Ltd.") == normalize_name("ABC Dental")


def test_normalize_name_keeps_distinct_names_distinct():
    assert normalize_name("ABC Dental Clinic") != normalize_name("XYZ Dental Clinic")
    # "Care" is a real name token, not a generic suffix - must NOT collapse
    assert normalize_name("ABC Dental Care") != normalize_name("ABC Dental")


def test_normalize_address_collapses_whitespace_and_case():
    a = normalize_address("  12,  GST Road,\n Chromepet,  Chennai - 600044 ")
    b = normalize_address("12 GST Road Chromepet Chennai 600044")
    assert a == b
