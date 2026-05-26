from __future__ import annotations

from pathlib import Path

from price_bot.providers import all_fetchers

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "providers"


def test_all_provider_fixture_parsers_build_profiles() -> None:
    profile_counts = {}
    for fetcher in all_fetchers():
        source = fetcher.sources()[0]
        payload = (FIXTURE_DIR / source.fixture_name).read_bytes()
        profiles = fetcher.build_profiles(payload, "test-catalog", "abc123")
        assert profiles
        profile_counts[fetcher.provider_key] = len(profiles)
        for profile in profiles:
            assert profile.provider.id == fetcher.provider_key
            assert profile.catalog_version == "test-catalog"
            assert profile.provenance["source_snapshot_sha256"] == "abc123"

    assert profile_counts == {
        "aws-s3": 2,
        "cloudflare-r2": 2,
        "wasabi": 1,
        "backblaze-b2": 2,
        "digitalocean-spaces": 2,
        "storj": 4,
        "minio": 1,
        "ceph": 1,
        "other": 1,
    }


def test_manual_required_profiles_do_not_fake_hosted_rates() -> None:
    for provider_key in {"minio", "ceph", "other"}:
        fetcher = next(item for item in all_fetchers() if item.provider_key == provider_key)
        source = fetcher.sources()[0]
        profile = fetcher.build_profiles(
            (FIXTURE_DIR / source.fixture_name).read_bytes(), "test-catalog", "abc123"
        )[0]
        assert profile.confidence.level == "low"
        assert profile.provenance["manual_required"] is True
        assert profile.meters == {}
