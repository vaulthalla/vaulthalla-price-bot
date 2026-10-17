from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import orjson

from price_bot.normalizer.models import (
    Confidence,
    Integrity,
    Meter,
    OperationMapEntry,
    ProfileScope,
    ProviderInfo,
    RateTier,
    RatingProfile,
    StorageRule,
    VersioningCapability,
)

DEFAULT_UNKNOWN_EXCLUSIONS = [
    "taxes",
    "enterprise discounts",
    "account-wide free-tier usage outside Vaulthalla",
    "promotional credits",
    "provider invoice rounding",
    "provider-specific private deals",
    "destination-specific egress exceptions",
]

OPERATION_KEYS = [
    "ListObjectsV2",
    "HeadObject",
    "GetObject",
    "PutObject",
    "CopyObject",
    "CreateMultipartUpload",
    "UploadPart",
    "UploadPartCopy",
    "CompleteMultipartUpload",
    "AbortMultipartUpload",
    "DeleteObject",
    "LifecycleTransition",
    "RestoreObject",
]


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_json_bytes(data: Any) -> bytes:
    return orjson.dumps(
        data,
        option=orjson.OPT_SORT_KEYS | orjson.OPT_APPEND_NEWLINE,
    )


def profile_json_dict(profile: RatingProfile, *, blank_integrity: bool = False) -> dict[str, Any]:
    data = profile.model_dump(mode="json", exclude_none=True)
    if blank_integrity:
        data = deepcopy(data)
        data.setdefault("integrity", {})
        data["integrity"]["content_sha256"] = ""
        data["integrity"]["signature_ref"] = ""
    return data


def profile_json_bytes(profile: RatingProfile, *, blank_integrity: bool = False) -> bytes:
    return stable_json_bytes(profile_json_dict(profile, blank_integrity=blank_integrity))


def compute_profile_sha256(profile: RatingProfile) -> str:
    return hashlib.sha256(profile_json_bytes(profile, blank_integrity=True)).hexdigest()


def finalize_profile_integrity(profile: RatingProfile, signature_ref: str = "") -> RatingProfile:
    profile.integrity.signature_ref = signature_ref
    profile.integrity.content_sha256 = compute_profile_sha256(profile)
    return profile


def money(value: str | int | Decimal) -> Decimal:
    return Decimal(str(value))


def tier(rate: str, rate_unit: str, start: str = "0", end: str | None = None) -> RateTier:
    return RateTier(
        tier_start=money(start),
        tier_end=money(end) if end is not None else None,
        rate=money(rate),
        rate_unit=rate_unit,
    )


def meter(
    key: str,
    meter_type: str,
    unit: str,
    rate: str,
    rate_unit: str,
    *,
    billing_unit: str | None = None,
    rounding_rule: str = "none",
    free_tier_amount: str | None = None,
    free_tier_unit: str | None = None,
    free_tier_scope: str = "none",
    rules: dict[str, Any] | None = None,
) -> Meter:
    return Meter(
        meter_key=key,
        meter_type=meter_type,
        unit=unit,
        billing_unit=billing_unit,
        rounding_rule=rounding_rule,
        free_tier_amount=money(free_tier_amount) if free_tier_amount is not None else None,
        free_tier_unit=free_tier_unit,
        free_tier_scope=free_tier_scope,
        tiers=[tier(rate, rate_unit)],
        rules=rules or {},
    )


def op_map(meter_key: str, multiplier: str = "1", **rules: Any) -> OperationMapEntry:
    return OperationMapEntry(meter_key=meter_key, multiplier=money(multiplier), rules=rules)


def base_profile(
    *,
    provider_id: str,
    display_name: str,
    region: str,
    storage_class: str,
    catalog_version: str,
    source_snapshot_sha256: str | None,
    confidence_level: str,
    confidence_score: str,
    confidence_reasons: list[str],
    confidence_unknowns: list[str] | None = None,
    provenance: dict[str, Any] | None = None,
    versioning: VersioningCapability | None = None,
) -> RatingProfile:
    generated_at = utc_now_iso()
    profile_id = f"{provider_id}/{region}/{storage_class}"
    return RatingProfile(
        profile_id=profile_id,
        catalog_version=catalog_version,
        generated_at=generated_at,
        effective_at=generated_at,
        provider=ProviderInfo(id=provider_id, display_name=display_name),
        scope=ProfileScope(region=region, storage_class=storage_class),
        confidence=Confidence(
            level=confidence_level,
            score=money(confidence_score),
            reasons=confidence_reasons,
            unknowns=confidence_unknowns or DEFAULT_UNKNOWN_EXCLUSIONS.copy(),
        ),
        known_exclusions=DEFAULT_UNKNOWN_EXCLUSIONS.copy(),
        provenance={
            "source_snapshot_sha256": source_snapshot_sha256,
            **(provenance or {}),
        },
        versioning=versioning or unverified_versioning(display_name),
        integrity=Integrity(),
    )


def unverified_versioning(
    display_name: str, documentation_urls: list[str] | None = None
) -> VersioningCapability:
    """Default block: price-bot has not verified this provider's versioning billing.

    ``supported`` is false so consumers never price noncurrent versions from a guess; ``status``
    tells them the provider may still implement versioning and the cost is simply unknown.
    """
    return VersioningCapability(
        supported=False,
        status="unverified",
        reason=(
            f"price-bot has not verified {display_name} object versioning billing semantics; "
            "treat noncurrent-version and delete-marker cost as unknown"
        ),
        documentation_urls=documentation_urls or [],
    )


def unsupported_versioning(
    reason: str, documentation_urls: list[str], notes: list[str] | None = None
) -> VersioningCapability:
    """The provider is verified not to implement S3 object versioning."""
    return VersioningCapability(
        supported=False,
        status="unsupported",
        reason=reason,
        documentation_urls=documentation_urls,
        notes=notes or [],
    )


def zero_operation_map() -> dict[str, OperationMapEntry]:
    return {operation: op_map("free_operations") for operation in OPERATION_KEYS}


def storage_rule(
    *,
    min_days: int | None = None,
    min_object_bytes: int | None = None,
    overhead_bytes: int | None = None,
    retrieval_rule: dict[str, Any] | None = None,
    early_delete_penalty_rule: dict[str, Any] | None = None,
) -> StorageRule:
    return StorageRule(
        minimum_storage_duration_days=min_days,
        minimum_billable_object_size_bytes=min_object_bytes,
        metadata_overhead_bytes_per_object=overhead_bytes,
        retrieval_rule=retrieval_rule,
        early_delete_penalty_rule=early_delete_penalty_rule,
    )
