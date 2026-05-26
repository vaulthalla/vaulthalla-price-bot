from __future__ import annotations

from decimal import Decimal

from price_bot.normalizer.rating_profile import base_profile, meter, op_map, storage_rule
from price_bot.providers.base import ProviderFetcher, SourceDefinition
from price_bot.scrape.parse import decimal_after, require_phrases, text_from_html

CURATED = b"""# Storj pricing
Global Collaboration is $15/TB/month, includes 1X egress, and additional egress is $0.02/GB. Minimum object size is 50KB. No minimum duration.
Regional Workflows is $10/TB/month, includes 1X egress, and additional egress is $0.01/GB. Minimum object size is 50KB. No minimum duration.
Active Archive is $6/TB/month and egress is $0.02/GB. Minimum object size is 100KB and minimum duration is 30-day.
Usage is rounded up to nearest whole GB per tier and usage type. Minimum monthly charge is $5.
Legacy object storage is $0.004/GB-month. Legacy egress is $0.007/GB. Segment fee is $0.0000088/segment-month with 64MB default segment size. Projects created before Nov 1 2025 retain legacy during transition.
"""


class StorjFetcher(ProviderFetcher):
    provider_key = "storj"
    display_name = "Storj"

    def sources(self) -> list[SourceDefinition]:
        return [
            SourceDefinition(
                kind="markdown",
                url="https://docs.storj.io/dcs/pricing",
                fixture_name="storj.md",
            )
        ]

    def curated_payload(self) -> bytes:
        return CURATED

    def build_profiles(self, payload: bytes, catalog_version: str, source_snapshot_sha256: str):
        text = payload.decode("utf-8", errors="replace")
        if "<html" in text.lower():
            text = text_from_html(payload)
        require_phrases(text, ["Global Collaboration", "Regional Workflows", "Active Archive", "Legacy", "Minimum monthly"])
        global_tb = decimal_after(r"Global Collaboration is \$([0-9.]+)\s*/?\s*TB/month", text, label="Storj global")
        global_egress = decimal_after(r"Global Collaboration.*?additional egress is \$([0-9.]+)", text, label="Storj global egress")
        regional_tb = decimal_after(r"Regional Workflows is \$([0-9.]+)\s*/?\s*TB/month", text, label="Storj regional")
        regional_egress = decimal_after(r"Regional Workflows.*?additional egress is \$([0-9.]+)", text, label="Storj regional egress")
        archive_tb = decimal_after(r"Active Archive is \$([0-9.]+)\s*/?\s*TB/month", text, label="Storj archive")
        archive_egress = decimal_after(r"Active Archive.*?egress is \$([0-9.]+)", text, label="Storj archive egress")
        legacy_storage = decimal_after(r"Legacy object storage is \$([0-9.]+)", text, label="Storj legacy storage")
        legacy_egress = decimal_after(r"Legacy egress is \$([0-9.]+)", text, label="Storj legacy egress")
        segment = decimal_after(r"Segment fee is \$([0-9.]+)", text, label="Storj segment")
        return [
            self._tier_profile("global-collaboration", catalog_version, source_snapshot_sha256, global_tb / Decimal("1000"), global_egress, 50000, None, True),
            self._tier_profile("regional-workflows", catalog_version, source_snapshot_sha256, regional_tb / Decimal("1000"), regional_egress, 50000, None, True),
            self._tier_profile("active-archive", catalog_version, source_snapshot_sha256, archive_tb / Decimal("1000"), archive_egress, 100000, 30, False),
            self._legacy(catalog_version, source_snapshot_sha256, legacy_storage, legacy_egress, segment),
        ]

    def _tier_profile(
        self,
        storage_class: str,
        catalog_version: str,
        source_snapshot_sha256: str,
        storage_gb_rate: Decimal,
        egress_rate: Decimal,
        min_object_bytes: int,
        min_days: int | None,
        included_egress: bool,
    ):
        profile = base_profile(
            provider_id=self.provider_key,
            display_name=self.display_name,
            region="global",
            storage_class=storage_class,
            catalog_version=catalog_version,
            source_snapshot_sha256=source_snapshot_sha256,
            confidence_level="high",
            confidence_score="0.88",
            confidence_reasons=["official docs parser sentinels passed"],
            provenance={"source": "Storj pricing docs", "minimum_monthly_usd": "5"},
        )
        profile.meters = {
            "storage": meter(
                "storage",
                "storage_byte_hours",
                "byte_hour",
                str(storage_gb_rate),
                "gb_month",
                billing_unit="gb_month",
                rounding_rule="ceil_to_gb",
            ),
            "egress": meter(
                "egress",
                "egress_bytes",
                "byte",
                str(egress_rate),
                "gb",
                billing_unit="gb",
                rounding_rule="ceil_to_gb",
                rules={"included_egress_multiple_of_storage": "1"} if included_egress else {},
            ),
            "free_operations": meter("free_operations", "request", "operation", "0", "operation"),
        }
        profile.operation_map = {key: op_map("free_operations") for key in [
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
        ]}
        profile.storage_rules = [storage_rule(min_days=min_days, min_object_bytes=min_object_bytes)]
        return profile

    def _legacy(
        self,
        catalog_version: str,
        source_snapshot_sha256: str,
        storage: Decimal,
        egress: Decimal,
        segment: Decimal,
    ):
        profile = base_profile(
            provider_id=self.provider_key,
            display_name=self.display_name,
            region="global",
            storage_class="legacy",
            catalog_version=catalog_version,
            source_snapshot_sha256=source_snapshot_sha256,
            confidence_level="medium-high",
            confidence_score="0.82",
            confidence_reasons=["official legacy pricing parser sentinels passed"],
            provenance={"source": "Storj legacy pricing docs", "default_segment_size_bytes": 67108864},
        )
        profile.meters = {
            "storage": meter("storage", "storage_byte_hours", "byte_hour", str(storage), "gb_month", billing_unit="gb_month"),
            "egress": meter("egress", "egress_bytes", "byte", str(egress), "gb", billing_unit="gb"),
            "segment": meter("segment", "object_count_hours", "object_hour", str(segment), "segment_month", billing_unit="segment_month"),
            "free_operations": meter("free_operations", "request", "operation", "0", "operation"),
        }
        profile.operation_map = {key: op_map("free_operations") for key in [
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
        ]}
        profile.storage_rules = [storage_rule(min_object_bytes=50000)]
        return profile
