from __future__ import annotations

from price_bot.normalizer.rating_profile import base_profile, meter, op_map
from price_bot.providers.base import ProviderFetcher, SourceDefinition
from price_bot.scrape.parse import decimal_after, require_phrases, text_from_html

CURATED = b"""<html><body><h1>Wasabi Pricing</h1>
<p>Pay-as-you-go storage starting at $6.99/TB/month.</p>
<p>No egress fees. No API request fees.</p>
<p>Price increase to $7.99/TB/month on July 1, 2026.</p>
</body></html>"""


class WasabiFetcher(ProviderFetcher):
    provider_key = "wasabi"
    display_name = "Wasabi"

    def sources(self) -> list[SourceDefinition]:
        return [
            SourceDefinition(
                kind="html",
                url="https://wasabi.com/pricing",
                fixture_name="wasabi.html",
            )
        ]

    def curated_payload(self) -> bytes:
        return CURATED

    def build_profiles(self, payload: bytes, catalog_version: str, source_snapshot_sha256: str):
        text = text_from_html(payload) if b"<html" in payload.lower() else payload.decode()
        require_phrases(text, ["Wasabi", "No egress", "No API request", "July 1, 2026"])
        storage = decimal_after(r"\$([0-9.]+)\s*/?\s*TB/month", text, label="Wasabi TB-month")
        future = decimal_after(r"\$([0-9.]+)\s*/?\s*TB/month.*?July 1, 2026", text, label="Wasabi future price")
        profile = base_profile(
            provider_id=self.provider_key,
            display_name=self.display_name,
            region="global",
            storage_class="pay-as-you-go",
            catalog_version=catalog_version,
            source_snapshot_sha256=source_snapshot_sha256,
            confidence_level="medium-high",
            confidence_score="0.78",
            confidence_reasons=["official pricing page parser sentinels passed"],
            confidence_unknowns=[
                "regional variations / contractual plans",
                "reserved capacity",
                "minimum storage duration if documented outside pricing page",
            ],
            provenance={
                "source": "Wasabi pricing page",
                "future_price_increase": {"effective": "2026-07-01", "tb_month": str(future)},
            },
        )
        profile.meters = {
            "storage": meter(
                "storage",
                "storage_byte_hours",
                "byte_hour",
                str(storage),
                "tb_month",
                billing_unit="tb_month",
            ),
            "egress": meter("egress", "egress_bytes", "byte", "0", "gb", billing_unit="gb"),
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
        return [profile]
