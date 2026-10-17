from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class DecimalModel(BaseModel):
    pass


class ProviderInfo(DecimalModel):
    id: str
    display_name: str
    api_family: str = "s3-compatible"


class ProfileScope(DecimalModel):
    region: str
    storage_class: str
    currency: str = "USD"


class Confidence(DecimalModel):
    level: str
    score: Decimal
    reasons: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


class Integrity(DecimalModel):
    content_sha256: str = ""
    signature_alg: str = "Ed25519"
    signature_ref: str = ""


class RateTier(DecimalModel):
    tier_start: Decimal = Decimal("0")
    tier_end: Decimal | None = None
    rate: Decimal
    rate_unit: str


class Meter(DecimalModel):
    meter_key: str
    meter_type: str
    unit: str
    billing_unit: str | None = None
    rounding_rule: str = "none"
    free_tier_amount: Decimal | None = None
    free_tier_unit: str | None = None
    free_tier_scope: str = "none"
    tiers: list[RateTier] = Field(default_factory=list)
    rules: dict[str, Any] = Field(default_factory=dict)


class OperationMapEntry(DecimalModel):
    meter_key: str
    multiplier: Decimal = Decimal("1")
    rules: dict[str, Any] = Field(default_factory=dict)


class StorageRule(DecimalModel):
    minimum_storage_duration_days: int | None = None
    minimum_billable_object_size_bytes: int | None = None
    metadata_overhead_bytes_per_object: int | None = None
    early_delete_penalty_rule: dict[str, Any] | None = None
    retrieval_rule: dict[str, Any] | None = None


class VersioningCapability(DecimalModel):
    """Additive, provider-level description of S3 object versioning cost semantics.

    Consumers (the Vaulthalla daemon) read this block to price noncurrent versions and delete
    markers locally. ``supported`` is the only key that is always present. ``status`` separates
    "the provider does not implement versioning" (``unsupported``) from "price-bot has not
    verified this provider's versioning billing" (``unverified``); both carry ``supported=false``.
    """

    supported: bool
    status: Literal["supported", "unsupported", "unverified"]
    reason: str | None = None
    noncurrent_storage_billing: str | None = None
    delete_marker_billing: str | None = None
    noncurrent_expiration_lifecycle: bool | None = None
    lifecycle_expiration_request_charge: bool | None = None
    minimum_storage_duration_days: int | None = None
    minimum_billable_object_size_bytes: int | None = None
    irreversible_enable: bool | None = None
    documentation_urls: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class RatingProfile(DecimalModel):
    schema_version: str = "1.0"
    kind: Literal["vaulthalla.rating_profile"] = "vaulthalla.rating_profile"
    profile_id: str
    catalog_version: str
    generated_at: str
    effective_at: str
    expires_at: str | None = None
    provider: ProviderInfo
    scope: ProfileScope
    confidence: Confidence
    operation_map: dict[str, OperationMapEntry] = Field(default_factory=dict)
    meters: dict[str, Meter] = Field(default_factory=dict)
    storage_rules: list[StorageRule] = Field(default_factory=list)
    known_exclusions: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    versioning: VersioningCapability | None = None
    integrity: Integrity = Field(default_factory=Integrity)


class ManifestProfileRef(DecimalModel):
    profile_id: str
    provider: str
    region: str
    storage_class: str
    currency: str
    href: str
    sha256: str
    signature: str
    confidence_level: str
    effective_at: str
    expires_at: str | None = None


class Manifest(DecimalModel):
    schema_version: str = "1.0"
    kind: Literal["vaulthalla.price_manifest"] = "vaulthalla.price_manifest"
    catalog_version: str
    generated_at: str
    profile_count: int
    profiles: list[ManifestProfileRef]
    full_catalog: dict[str, str]
    schemas: dict[str, str]
    integrity: Integrity = Field(default_factory=Integrity)


class UsageInput(DecimalModel):
    provider_operation_counts: dict[str, Decimal] = Field(default_factory=dict)
    storage_byte_hours: dict[str, Decimal] = Field(default_factory=dict)
    object_count_hours: dict[str, Decimal] = Field(default_factory=dict)
    uploaded_bytes: Decimal = Decimal("0")
    downloaded_bytes: Decimal = Decimal("0")
    retrieval_bytes: Decimal = Decimal("0")
    retrieval_object_count: Decimal = Decimal("0")
    egress_bytes: Decimal = Decimal("0")
    object_count: Decimal = Decimal("0")
    early_delete_gb_days: dict[str, Decimal] = Field(default_factory=dict)
    noncurrent_storage_byte_hours: dict[str, Decimal] = Field(default_factory=dict)


class EstimateOptions(DecimalModel):
    apply_free_tiers: bool = True
    mode: Literal["reporting", "budget_conservative"] | None = None


class EstimateBreakdown(DecimalModel):
    meter_key: str
    quantity: Decimal
    billable_quantity: Decimal
    unit: str
    rate: Decimal
    rate_unit: str
    cost: Decimal
    notes: list[str] = Field(default_factory=list)


class EstimateResult(DecimalModel):
    estimated_cost: Decimal
    currency: str
    estimate_mode: Literal["reporting", "budget_conservative"] = "reporting"
    free_tier_policy: str = "apply_free_tiers"
    free_tiers_applied: bool = False
    breakdown: list[EstimateBreakdown]
    free_tier_applied: dict[str, Decimal] = Field(default_factory=dict)
    rounding_applied: dict[str, str] = Field(default_factory=dict)
    confidence: Confidence
    unknowns: list[str] = Field(default_factory=list)
