from __future__ import annotations

from decimal import Decimal

from price_bot.normalizer.models import VersioningCapability
from price_bot.normalizer.rating_profile import (
    base_profile,
    meter,
    op_map,
    storage_rule,
    unsupported_versioning,
)
from price_bot.providers.base import ProviderFetcher, SourceDefinition
from price_bot.scrape.parse import decimal_after, require_phrases

CURATED = b"""# Cloudflare R2 pricing
R2 Standard storage is $0.015 / GB-month.
R2 Standard Class A operations are $4.50 / million requests.
R2 Standard Class B operations are $0.36 / million requests.
R2 Infrequent Access storage is $0.01 / GB-month.
R2 Infrequent Access Class A operations are $9.00 / million requests.
R2 Infrequent Access Class B operations are $0.90 / million requests.
R2 Infrequent Access retrieval is $0.01 / GB.
Egress is free. Cloudflare rounds up to the next billing unit.
DeleteObject and AbortMultipartUpload are free.
"""

R2_S3_API_DOC_URL = "https://developers.cloudflare.com/r2/api/s3/api/"


def r2_versioning() -> VersioningCapability:
    # Verified 2026-10-16 against the R2 S3 API compatibility page (last updated 2026-10-14):
    # GetBucketVersioning and PutBucketVersioning are marked unimplemented, ListObjectVersions is
    # not listed, and GetObject documents no versionId parameter.
    return unsupported_versioning(
        "Cloudflare R2 does not implement S3 object versioning "
        "(PutBucketVersioning/ListObjectVersions)",
        [R2_S3_API_DOC_URL],
        notes=[
            "The R2 S3 API compatibility page lists GetBucketVersioning and PutBucketVersioning "
            "as unimplemented and does not list ListObjectVersions or a versionId parameter on "
            "GetObject.",
            "Overwrites and deletes on R2 replace or remove the only copy; there are no "
            "noncurrent versions or delete markers to bill.",
        ],
    )


class CloudflareR2Fetcher(ProviderFetcher):
    provider_key = "cloudflare-r2"
    display_name = "Cloudflare R2"

    def sources(self) -> list[SourceDefinition]:
        return [
            SourceDefinition(
                kind="markdown",
                url="https://developers.cloudflare.com/r2/platform/pricing/",
                fixture_name="cloudflare_r2.md",
            )
        ]

    def curated_payload(self) -> bytes:
        return CURATED

    def build_profiles(self, payload: bytes, catalog_version: str, source_snapshot_sha256: str):
        text = payload.decode("utf-8", errors="replace")
        require_phrases(
            text,
            [
                "Cloudflare R2",
                "Class A",
                "Class B",
                "Infrequent Access",
                "Egress is free",
            ],
        )
        standard_storage = decimal_after(r"Standard storage.*?\$([0-9.]+)", text, label="R2 Standard storage")
        standard_a = decimal_after(r"Standard Class A.*?\$([0-9.]+)", text, label="R2 Standard Class A")
        standard_b = decimal_after(r"Standard Class B.*?\$([0-9.]+)", text, label="R2 Standard Class B")
        ia_storage = decimal_after(r"Infrequent Access storage.*?\$([0-9.]+)", text, label="R2 IA storage")
        ia_a = decimal_after(r"Infrequent Access Class A.*?\$([0-9.]+)", text, label="R2 IA Class A")
        ia_b = decimal_after(r"Infrequent Access Class B.*?\$([0-9.]+)", text, label="R2 IA Class B")
        ia_retrieval = decimal_after(r"Infrequent Access retrieval.*?\$([0-9.]+)", text, label="R2 IA retrieval")
        return [
            self._profile(
                "standard",
                catalog_version,
                source_snapshot_sha256,
                standard_storage,
                standard_a,
                standard_b,
                retrieval=None,
                free_tier=True,
                min_days=None,
            ),
            self._profile(
                "infrequent-access",
                catalog_version,
                source_snapshot_sha256,
                ia_storage,
                ia_a,
                ia_b,
                retrieval=ia_retrieval,
                free_tier=False,
                min_days=30,
            ),
        ]

    def _profile(
        self,
        storage_class: str,
        catalog_version: str,
        source_snapshot_sha256: str,
        storage: Decimal,
        class_a: Decimal,
        class_b: Decimal,
        *,
        retrieval: Decimal | None,
        free_tier: bool,
        min_days: int | None,
    ):
        profile = base_profile(
            provider_id=self.provider_key,
            display_name=self.display_name,
            region="global",
            storage_class=storage_class,
            catalog_version=catalog_version,
            source_snapshot_sha256=source_snapshot_sha256,
            confidence_level="high",
            confidence_score="0.92",
            confidence_reasons=["official docs markdown/html parser sentinels passed"],
            provenance={"source": "Cloudflare R2 pricing docs"},
            versioning=r2_versioning(),
        )
        profile.meters = {
            "storage": meter(
                "storage",
                "storage_byte_hours",
                "byte_hour",
                str(storage),
                "gb_month",
                billing_unit="gb_month",
                rounding_rule="ceil_to_billing_unit",
                free_tier_amount="10" if free_tier else None,
                free_tier_unit="gb_month" if free_tier else None,
                free_tier_scope="account_month" if free_tier else "none",
            ),
            "request_class_a": meter(
                "request_class_a",
                "request",
                "operation",
                str(class_a),
                "million_operations",
                billing_unit="million_operations",
                rounding_rule="ceil_to_billing_unit",
                free_tier_amount="1" if free_tier else None,
                free_tier_unit="million_operations" if free_tier else None,
                free_tier_scope="account_month" if free_tier else "none",
            ),
            "request_class_b": meter(
                "request_class_b",
                "request",
                "operation",
                str(class_b),
                "million_operations",
                billing_unit="million_operations",
                rounding_rule="ceil_to_billing_unit",
                free_tier_amount="10" if free_tier else None,
                free_tier_unit="million_operations" if free_tier else None,
                free_tier_scope="account_month" if free_tier else "none",
            ),
            "free_operations": meter("free_operations", "request", "operation", "0", "operation"),
            "egress": meter("egress", "egress_bytes", "byte", "0", "gb", billing_unit="gb"),
        }
        if retrieval is not None:
            profile.meters["retrieval"] = meter(
                "retrieval", "retrieval_bytes", "byte", str(retrieval), "gb", billing_unit="gb"
            )
        profile.operation_map = {
            "ListObjectsV2": op_map("request_class_a"),
            "HeadObject": op_map("request_class_b"),
            "GetObject": op_map("request_class_b"),
            "PutObject": op_map("request_class_a"),
            "CopyObject": op_map("request_class_a"),
            "CreateMultipartUpload": op_map("request_class_a"),
            "UploadPart": op_map("request_class_a"),
            "UploadPartCopy": op_map("request_class_a"),
            "CompleteMultipartUpload": op_map("request_class_a"),
            "AbortMultipartUpload": op_map("free_operations"),
            "DeleteObject": op_map("free_operations"),
            "LifecycleTransition": op_map("request_class_a"),
            "RestoreObject": op_map("request_class_b"),
        }
        if min_days:
            profile.storage_rules = [storage_rule(min_days=min_days)]
        return profile
