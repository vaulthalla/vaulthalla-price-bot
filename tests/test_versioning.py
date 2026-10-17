from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
import zstandard as zstd

from price_bot.config import load_settings
from price_bot.normalizer.estimator import estimate_cost
from price_bot.normalizer.models import RatingProfile, UsageInput
from price_bot.normalizer.rating_profile import profile_json_dict
from price_bot.normalizer.units import BILLING_MONTH_HOURS, GB
from price_bot.providers import all_fetchers
from price_bot.publish.artifacts import publish_catalog, validate_manifest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "providers"

AWS_CONTRACT_KEYS = {
    "supported",
    "status",
    "noncurrent_storage_billing",
    "delete_marker_billing",
    "noncurrent_expiration_lifecycle",
    "lifecycle_expiration_request_charge",
    "minimum_storage_duration_days",
    "minimum_billable_object_size_bytes",
    "irreversible_enable",
    "documentation_urls",
    "notes",
}


def fixture_profiles() -> list[RatingProfile]:
    profiles: list[RatingProfile] = []
    for fetcher in all_fetchers():
        payload = (FIXTURE_DIR / fetcher.sources()[0].fixture_name).read_bytes()
        profiles.extend(fetcher.build_profiles(payload, "test-catalog", "abc123"))
    return profiles


def versioning_json(profile: RatingProfile) -> dict:
    return profile_json_dict(profile)["versioning"]


def test_every_profile_has_a_versioning_block() -> None:
    profiles = fixture_profiles()
    assert len(profiles) == 24
    for profile in profiles:
        block = versioning_json(profile)
        assert isinstance(block["supported"], bool), profile.profile_id
        assert block["status"] in {"supported", "unsupported", "unverified"}
        assert block["supported"] is (block["status"] == "supported")
        if not block["supported"]:
            assert block["reason"], profile.profile_id


@pytest.mark.parametrize(
    ("storage_class", "min_days", "min_bytes"),
    [("standard", 0, 0), ("standard-ia", 30, 131072)],
)
def test_aws_versioning_contract(storage_class: str, min_days: int, min_bytes: int) -> None:
    profiles = [
        p
        for p in fixture_profiles()
        if p.provider.id == "aws-s3" and p.scope.storage_class == storage_class
    ]
    assert len(profiles) == 5
    for profile in profiles:
        block = versioning_json(profile)
        assert set(block) == AWS_CONTRACT_KEYS
        assert block["supported"] is True
        assert block["noncurrent_storage_billing"] == "storage_class_rate"
        assert block["delete_marker_billing"] == "key_name_bytes"
        assert block["noncurrent_expiration_lifecycle"] is True
        assert block["lifecycle_expiration_request_charge"] is False
        assert block["irreversible_enable"] is True
        assert block["minimum_storage_duration_days"] == min_days
        assert block["minimum_billable_object_size_bytes"] == min_bytes
        assert "https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html" in block[
            "documentation_urls"
        ]
        assert (
            "Noncurrent versions are billed at the storage rate of their storage class."
            in block["notes"]
        )


def test_r2_versioning_unsupported() -> None:
    profiles = [p for p in fixture_profiles() if p.provider.id == "cloudflare-r2"]
    assert len(profiles) == 2
    for profile in profiles:
        block = versioning_json(profile)
        assert block["supported"] is False
        assert block["status"] == "unsupported"
        assert block["reason"] == (
            "Cloudflare R2 does not implement S3 object versioning "
            "(PutBucketVersioning/ListObjectVersions)"
        )
        assert block["documentation_urls"] == ["https://developers.cloudflare.com/r2/api/s3/api/"]
        assert "noncurrent_storage_billing" not in block
        # R2 never maps versioning operations.
        assert "ListObjectVersions" not in profile.operation_map


def test_unverified_providers_do_not_claim_support() -> None:
    for profile in fixture_profiles():
        if profile.provider.id in {"aws-s3", "cloudflare-r2"}:
            continue
        block = versioning_json(profile)
        assert block == {
            "supported": False,
            "status": "unverified",
            "reason": block["reason"],
            "documentation_urls": [],
            "notes": [],
        }
        assert profile.provider.display_name in block["reason"]


class MemoryStore:
    def __init__(self, profiles: list[RatingProfile]) -> None:
        self.profiles = profiles
        self.published: dict = {}

    def latest_profiles(self, session) -> list[RatingProfile]:
        return [p.model_copy(deep=True) for p in self.profiles]

    def record_publish(self, session, **kwargs) -> None:
        self.published = kwargs


def test_publish_manifest_signature_round_trip_keeps_versioning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRICE_BOT_SIGNING_PRIVATE_KEY_PATH", str(tmp_path / "keys" / "ed25519.key"))
    monkeypatch.setenv("PRICE_BOT_SIGNING_PUBLIC_KEY_PATH", str(tmp_path / "keys" / "ed25519.pub"))
    settings = load_settings()
    out = tmp_path / "public" / "v1"
    store = MemoryStore(fixture_profiles())
    manifest = publish_catalog(None, store, out, settings)
    validate_manifest(out / "manifest.json", settings)
    assert manifest.profile_count == 24
    assert store.published["profile_count"] == 24

    for ref in manifest.profiles:
        published = json.loads((out / ref.href).read_bytes())
        assert "versioning" in published, ref.href
        RatingProfile.model_validate(published)
    aws = json.loads((out / "profiles/aws-s3/us-east-1/standard-ia.json").read_bytes())
    assert aws["versioning"]["minimum_billable_object_size_bytes"] == 131072
    assert aws["operation_map"]["ListObjectVersions"]["meter_key"] == "request_write"
    r2 = json.loads((out / "profiles/cloudflare-r2/global/standard.json").read_bytes())
    assert r2["versioning"]["supported"] is False

    full = json.loads(zstd.ZstdDecompressor().decompress((out / "catalog/full.json.zst").read_bytes()))
    assert all("versioning" in item for item in full["profiles"])

    schema = json.loads((out / "schemas/rating-profile-v1.schema.json").read_bytes())
    assert "versioning" in schema["properties"]
    assert "VersioningCapability" in schema["$defs"]
    assert set(schema["$defs"]["VersioningCapability"]["properties"]) >= AWS_CONTRACT_KEYS | {"reason"}


def _aws(storage_class: str) -> RatingProfile:
    return next(
        p
        for p in fixture_profiles()
        if p.profile_id == f"aws-s3/us-east-1/{storage_class}"
    )


def test_noncurrent_storage_priced_at_class_storage_rate() -> None:
    base = UsageInput(storage_byte_hours={"standard": Decimal("100") * GB * BILLING_MONTH_HOURS})
    with_versions = base.model_copy(
        update={"noncurrent_storage_byte_hours": {"standard": Decimal("50") * GB * BILLING_MONTH_HOURS}}
    )
    plain = estimate_cost(_aws("standard"), base)
    versioned = estimate_cost(_aws("standard"), with_versions)
    assert plain.estimated_cost == Decimal("2.30000000")
    assert versioned.estimated_cost == Decimal("3.45000000")
    line = next(item for item in versioned.breakdown if item.meter_key == "noncurrent_storage")
    assert line.rate == Decimal("0.023")
    assert line.billable_quantity == Decimal("50")


def test_noncurrent_storage_not_priced_without_versioning_support() -> None:
    r2 = next(p for p in fixture_profiles() if p.profile_id == "cloudflare-r2/global/standard")
    usage = UsageInput(noncurrent_storage_byte_hours={"standard": Decimal("50") * GB * BILLING_MONTH_HOURS})
    result = estimate_cost(r2, usage)
    assert result.estimated_cost == Decimal("0E-8")
    assert not any(item.meter_key == "noncurrent_storage" for item in result.breakdown)
    assert any("versioning status is unsupported" in item for item in result.unknowns)


def test_empty_noncurrent_key_changes_nothing() -> None:
    usage = UsageInput(
        storage_byte_hours={"standard-ia": Decimal("10") * GB * BILLING_MONTH_HOURS},
        provider_operation_counts={"PutObject": Decimal("2000"), "GetObject": Decimal("5000")},
        retrieval_bytes=Decimal("3") * GB,
    )
    profile = _aws("standard-ia")
    before = estimate_cost(profile, usage)
    after = estimate_cost(
        profile, usage.model_copy(update={"noncurrent_storage_byte_hours": {"standard": Decimal("1")}})
    )
    assert before.model_dump() == after.model_dump()
