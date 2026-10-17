from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from price_bot.config import load_settings
from price_bot.db.models import Base, PriceProfile
from price_bot.db.store import Store
from price_bot.normalizer.rating_profile import profile_json_dict
from price_bot.providers import all_fetchers

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "providers"


@pytest.fixture
def store() -> Store:
    database_url = load_settings().database_url
    if not database_url:
        pytest.skip("DATABASE_URL not configured")
    store = Store(database_url)
    Base.metadata.create_all(store.engine)
    return store


def test_profiles_survive_db_round_trip_including_versioning(store: Store) -> None:
    catalog_version = f"roundtrip-{uuid.uuid4().hex}"
    profiles = []
    for fetcher in all_fetchers():
        payload = (FIXTURE_DIR / fetcher.sources()[0].fixture_name).read_bytes()
        profiles.extend(fetcher.build_profiles(payload, catalog_version, "abc123"))

    with store.session() as session:
        try:
            store.upsert_profiles(session, profiles)
            rows = session.scalars(
                select(PriceProfile).where(PriceProfile.catalog_version == catalog_version)
            ).all()
            restored = {row.profile_key: store.profile_from_row(row) for row in rows}
        finally:
            session.rollback()

    assert set(restored) == {profile.profile_id for profile in profiles}
    for profile in profiles:
        after = restored[profile.profile_id]
        assert after.versioning == profile.versioning, profile.profile_id
        assert "versioning" not in after.provenance
        assert profile_json_dict(after, blank_integrity=True) == profile_json_dict(
            profile, blank_integrity=True
        ), profile.profile_id
