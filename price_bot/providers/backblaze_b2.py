from __future__ import annotations

from decimal import Decimal

from price_bot.normalizer.rating_profile import base_profile, meter, op_map
from price_bot.providers.base import ProviderFetcher, SourceDefinition
from price_bot.scrape.parse import decimal_after, require_phrases, text_from_html

CURATED = b"""<html><body><h1>Backblaze B2 Cloud Storage Pricing</h1>
<p>Pay-as-you-go storage is $6.95/TB/month. The first 10GB storage is free.</p>
<p>Class A, B, and C transactions are free. Class D transactions are $0.004 per 10,000 after the first 2,500 per day.</p>
<p>Egress is free up to 3x average monthly storage, then $0.01/GB.</p>
<p>B2 Overdrive is $15/TB/month with unlimited egress.</p>
<p>No minimum file size or duration.</p>
</body></html>"""


class BackblazeB2Fetcher(ProviderFetcher):
    provider_key = "backblaze-b2"
    display_name = "Backblaze B2"

    def sources(self) -> list[SourceDefinition]:
        return [
            SourceDefinition(
                kind="html",
                url="https://www.backblaze.com/cloud-storage/pricing",
                fixture_name="backblaze_b2.html",
            )
        ]

    def curated_payload(self) -> bytes:
        return CURATED

    def build_profiles(self, payload: bytes, catalog_version: str, source_snapshot_sha256: str):
        text = text_from_html(payload) if b"<html" in payload.lower() else payload.decode()
        require_phrases(text, ["Backblaze B2", "first 10GB", "Class D", "3x average", "Overdrive"])
        storage_tb = decimal_after(r"storage is \$([0-9.]+)\s*/?\s*TB/month", text, label="B2 storage")
        class_d = decimal_after(r"Class D.*?\$([0-9.]+)\s+per\s+10,000", text, label="B2 Class D")
        egress = decimal_after(r"then \$([0-9.]+)\s*/?\s*GB", text, label="B2 egress overage")
        overdrive_tb = decimal_after(r"Overdrive is \$([0-9.]+)\s*/?\s*TB/month", text, label="B2 Overdrive")
        return [
            self._profile(
                "standard",
                catalog_version,
                source_snapshot_sha256,
                storage_tb / Decimal("1000"),
                class_d,
                egress,
                overdrive=False,
            ),
            self._profile(
                "overdrive",
                catalog_version,
                source_snapshot_sha256,
                overdrive_tb / Decimal("1000"),
                class_d,
                Decimal("0"),
                overdrive=True,
            ),
        ]

    def _profile(
        self,
        storage_class: str,
        catalog_version: str,
        source_snapshot_sha256: str,
        storage_gb_rate: Decimal,
        class_d_rate: Decimal,
        egress_rate: Decimal,
        *,
        overdrive: bool,
    ):
        profile = base_profile(
            provider_id=self.provider_key,
            display_name=self.display_name,
            region="global",
            storage_class=storage_class,
            catalog_version=catalog_version,
            source_snapshot_sha256=source_snapshot_sha256,
            confidence_level="medium-high",
            confidence_score="0.80",
            confidence_reasons=["official pricing page parser sentinels passed"],
            provenance={
                "source": "Backblaze B2 pricing page",
                "no_minimum_file_size_or_duration": True,
            },
        )
        profile.meters = {
            "storage": meter(
                "storage",
                "storage_byte_hours",
                "byte_hour",
                str(storage_gb_rate),
                "gb_month",
                billing_unit="gb_month",
                free_tier_amount=None if overdrive else "10",
                free_tier_unit=None if overdrive else "gb_month",
                free_tier_scope="none" if overdrive else "account_month",
            ),
            "egress": meter(
                "egress",
                "egress_bytes",
                "byte",
                str(egress_rate),
                "gb",
                billing_unit="gb",
                rules={} if overdrive else {"included_egress_multiple_of_storage": "3"},
            ),
            "free_operations": meter("free_operations", "request", "operation", "0", "operation"),
            "class_d": meter(
                "class_d",
                "request",
                "operation",
                str(class_d_rate),
                "request_10000",
                billing_unit="request_10000",
                free_tier_amount="2500",
                free_tier_unit="operation",
                free_tier_scope="account_day",
            ),
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
        return profile
