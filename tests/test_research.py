from app.models import Business, Command
from app.research import research


class _FakeProvider:
    name = "fake"

    def search(self, query, location, limit, radius_m=None):
        return [
            Business(business_name="ABC Dental", phone="+919876543210"),
            Business(business_name="New Clinic", phone="+919999999999"),
        ]


async def test_research_tags_against_store_without_writing(store, monkeypatch):
    store.upsert_many([Business(business_name="ABC Dental", phone="+919876543210")])

    monkeypatch.setattr("app.research.get_provider", lambda name=None: _FakeProvider())

    out = await research(
        Command(action="search", business_type="dental clinic", location="Chromepet"),
        store,
    )
    tags = {t.business.business_name: t.tag for t in out}
    assert tags["ABC Dental"] == "existing"
    assert tags["New Clinic"] == "new"
    assert len(store.all()) == 1, "research must not write to the store"


async def test_research_collapses_duplicates_within_one_batch(store, monkeypatch):
    class DupProvider:
        name = "dup"

        def search(self, query, location, limit, radius_m=None):
            return [
                Business(business_name="Same Clinic", phone="+919876543210"),
                Business(business_name="Same Clinic Pvt Ltd", phone="098765 43210"),
            ]

    monkeypatch.setattr("app.research.get_provider", lambda name=None: DupProvider())

    out = await research(
        Command(action="search", business_type="x", location="y"), store
    )
    assert len(out) == 1, "one search returning the same clinic twice yields one row"


async def test_research_normalizes_provider_output(store, monkeypatch):
    class RawProvider:
        name = "raw"

        def search(self, query, location, limit, radius_m=None):
            return [Business(business_name="Raw", phone="098765 43210")]

    monkeypatch.setattr("app.research.get_provider", lambda name=None: RawProvider())

    out = await research(
        Command(action="search", business_type="x", location="y"), store
    )
    assert out[0].business.phone == "+919876543210"
