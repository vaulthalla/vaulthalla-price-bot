from __future__ import annotations

from price_bot.normalizer.rating_profile import base_profile, meter, op_map, storage_rule
from price_bot.providers.base import ProviderFetcher, SourceDefinition
from price_bot.scrape.parse import decimal_after, require_phrases, text_from_html

CURATED = b"""# DigitalOcean Spaces pricing
Standard Spaces subscription is $5/month and includes 250 GiB storage across buckets and 1,024 GiB outbound transfer.
Additional Standard storage is $0.02/GiB-month.
Additional outbound transfer is $0.01/GiB.
Inbound bandwidth is free. CDN is included and transfer allowance is shared.
Standard minimum billable object size is 4 KiB.
Cold Storage costs $0.007/GiB-month. Retrieval costs $0.01/GiB.
Cold Storage has a 30-day minimum and 128 KiB minimum object/retrieval charge.
"""


class DigitalOceanSpacesFetcher(ProviderFetcher):
    provider_key = "digitalocean-spaces"
    display_name = "DigitalOcean Spaces"

    def sources(self) -> list[SourceDefinition]:
        return [
            SourceDefinition(
                kind="markdown",
                url="https://docs.digitalocean.com/products/spaces/details/pricing/index.html.md",
                fixture_name="digitalocean_spaces.md",
            )
        ]

    def curated_payload(self) -> bytes:
        return CURATED

    def build_profiles(self, payload: bytes, catalog_version: str, source_snapshot_sha256: str):
        text = payload.decode("utf-8", errors="replace")
        if "<html" in text.lower():
            text = text_from_html(payload)
        require_phrases(text, ["DigitalOcean Spaces", "250 GiB", "1,024 GiB", "Cold Storage", "128 KiB"])
        base_fee = decimal_after(r"subscription is \$([0-9.]+)\s*/?\s*month", text, label="DO base")
        storage = decimal_after(r"Additional Standard storage is \$([0-9.]+)", text, label="DO storage")
        egress = decimal_after(r"Additional outbound transfer is \$([0-9.]+)", text, label="DO egress")
        cold_storage = decimal_after(r"Cold Storage costs \$([0-9.]+)", text, label="DO cold storage")
        retrieval = decimal_after(r"Retrieval costs \$([0-9.]+)", text, label="DO cold retrieval")
        return [
            self._standard(catalog_version, source_snapshot_sha256, str(base_fee), str(storage), str(egress)),
            self._cold(catalog_version, source_snapshot_sha256, str(cold_storage), str(retrieval)),
        ]

    def _standard(self, catalog_version: str, source_snapshot_sha256: str, base_fee: str, storage: str, egress: str):
        profile = base_profile(
            provider_id=self.provider_key,
            display_name=self.display_name,
            region="global",
            storage_class="standard",
            catalog_version=catalog_version,
            source_snapshot_sha256=source_snapshot_sha256,
            confidence_level="high",
            confidence_score="0.90",
            confidence_reasons=["official docs markdown/html parser sentinels passed"],
            provenance={"source": "DigitalOcean Spaces pricing docs"},
        )
        profile.meters = {
            "subscription": meter(
                "subscription", "fixed_monthly", "month", base_fee, "month", billing_unit="month"
            ),
            "storage": meter(
                "storage",
                "storage_byte_hours",
                "byte_hour",
                storage,
                "gib_month",
                billing_unit="gib_month",
                free_tier_amount="250",
                free_tier_unit="gib_month",
                free_tier_scope="account_month",
            ),
            "egress": meter(
                "egress",
                "egress_bytes",
                "byte",
                egress,
                "gib",
                billing_unit="gib",
                free_tier_amount="1024",
                free_tier_unit="gib",
                free_tier_scope="account_month",
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
        profile.storage_rules = [storage_rule(min_object_bytes=4096)]
        return profile

    def _cold(self, catalog_version: str, source_snapshot_sha256: str, storage: str, retrieval: str):
        profile = base_profile(
            provider_id=self.provider_key,
            display_name=self.display_name,
            region="global",
            storage_class="cold",
            catalog_version=catalog_version,
            source_snapshot_sha256=source_snapshot_sha256,
            confidence_level="high",
            confidence_score="0.90",
            confidence_reasons=["official docs markdown/html parser sentinels passed"],
            provenance={"source": "DigitalOcean Spaces Cold Storage docs"},
        )
        profile.meters = {
            "storage": meter(
                "storage",
                "storage_byte_hours",
                "byte_hour",
                storage,
                "gib_month",
                billing_unit="gib_month",
            ),
            "retrieval": meter(
                "retrieval",
                "retrieval_bytes",
                "byte",
                retrieval,
                "gib",
                billing_unit="gib",
                rules={"minimum_retrieval_object_size_bytes": 131072},
            ),
            "egress": meter("egress", "egress_bytes", "byte", "0.01", "gib", billing_unit="gib"),
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
        profile.storage_rules = [
            storage_rule(
                min_days=30,
                min_object_bytes=131072,
                retrieval_rule={"minimum_retrieval_object_size_bytes": 131072},
            )
        ]
        return profile
