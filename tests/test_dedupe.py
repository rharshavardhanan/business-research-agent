from app.dedupe import find_match, merge
from app.models import Business


def B(**kw):
    kw.setdefault("business_name", "X")
    return Business(**kw)


def test_tier2_same_phone_different_name_is_high():
    existing = [B(business_name="ABC Dental", phone="+919876543210")]
    cand = B(business_name="ABC Dental Clinic", phone="098765 43210")
    m, conf, tier = find_match(cand, existing)
    assert m is existing[0] and conf == "high" and tier == "phone"


def test_spec_required_three_variants_collapse_on_shared_phone():
    existing = [B(business_name="ABC Dental", phone="+919876543210")]
    for name in ["ABC Dental Clinic", "ABC Dental Clinic, Chromepet"]:
        m, conf, _ = find_match(B(business_name=name, phone="+919876543210"), existing)
        assert m is not None and conf == "high"


def test_abc_dental_clinic_vs_abc_dental_care_does_not_auto_merge():
    existing = [B(business_name="ABC Dental Clinic", area="Chromepet")]
    cand = B(business_name="ABC Dental Care", area="Chromepet")
    m, conf, _ = find_match(cand, existing)
    assert conf != "high", "must not auto-merge on name similarity alone"


def test_tier1_source_id_wins():
    existing = [B(source="osm", source_id="node/1", phone=None)]
    m, conf, tier = find_match(B(source="osm", source_id="node/1"), existing)
    assert conf == "high" and tier == "source_id"


def test_tier3_same_website_is_high():
    existing = [B(website="https://abcdental.com/")]
    m, conf, tier = find_match(B(website="http://www.abcdental.com"), existing)
    assert conf == "high" and tier == "website"


def test_no_match_returns_none():
    m, conf, _ = find_match(B(business_name="Totally New"), [B(business_name="Other")])
    assert m is None and conf == "none"


def test_merge_fills_none_fields_only():
    existing = B(business_name="ABC Dental", phone="+919876543210", website=None)
    new = B(
        business_name="ABC Dental Clinic",
        phone="+919999999999",
        website="https://abcdental.com",
    )
    merged, changed = merge(existing, new)
    assert changed is True
    assert merged.website == "https://abcdental.com"  # None filled
    assert merged.phone == "+919876543210"  # existing NOT overwritten
    assert merged.business_name == "ABC Dental"  # display name preserved


def test_merge_reports_no_change_when_nothing_new():
    existing = B(business_name="ABC", phone="+919876543210")
    merged, changed = merge(existing, B(business_name="ABC", phone="+919876543210"))
    assert changed is False


def test_merge_refreshes_a_changed_rating():
    existing = B(business_name="ABC", rating=4.5, review_count=100)
    merged, changed = merge(existing, B(business_name="ABC", rating=4.9, review_count=568))
    assert changed is True
    assert merged.rating == 4.9 and merged.review_count == 568


def test_merge_refreshes_business_status():
    existing = B(business_name="ABC", business_status="OPERATIONAL")
    merged, _ = merge(existing, B(business_name="ABC", business_status="CLOSED_PERMANENTLY"))
    assert merged.business_status == "CLOSED_PERMANENTLY"


def test_merge_still_refuses_to_overwrite_identity_data():
    existing = B(business_name="ABC", phone="+919876543210", rating=4.5)
    merged, _ = merge(existing, B(business_name="ABC", phone="+919999999999", rating=4.9))
    assert merged.phone == "+919876543210", "a corrected phone must survive"
    assert merged.rating == 4.9


def test_merge_does_not_clear_a_rating_when_the_new_record_lacks_one():
    existing = B(business_name="ABC", rating=4.9)
    merged, changed = merge(existing, B(business_name="ABC"))
    assert merged.rating == 4.9 and changed is False
