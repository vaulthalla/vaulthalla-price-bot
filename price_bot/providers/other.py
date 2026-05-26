from __future__ import annotations

from price_bot.normalizer.rating_profile import base_profile
from price_bot.providers.base import ProviderFetcher, SourceDefinition

CURATED = b"""{"provider":"Other","manual_required":true,"note":"Operator supplied S3-compatible provider pricing."}"""


class OtherFetcher(ProviderFetcher):
    provider_key = "other"
    display_name = "Other S3-compatible"

    def sources(self) -> list[SourceDefinition]:
        return [SourceDefinition(kind="manual", fixture_name="other.json")]

    def curated_payload(self) -> bytes:
        return CURATED

    def build_profiles(self, payload: bytes, catalog_version: str, source_snapshot_sha256: str):
        profile = base_profile(
            provider_id=self.provider_key,
            display_name=self.display_name,
            region="operator",
            storage_class="manual",
            catalog_version=catalog_version,
            source_snapshot_sha256=source_snapshot_sha256,
            confidence_level="low",
            confidence_score="0.10",
            confidence_reasons=["manual profile required for unknown provider"],
            confidence_unknowns=["all provider-specific rates and billing rules"],
            provenance={"provider_type": "manual", "manual_required": True},
        )
        return [profile]
