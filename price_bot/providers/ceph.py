from __future__ import annotations

from price_bot.normalizer.rating_profile import base_profile
from price_bot.providers.base import ProviderFetcher, SourceDefinition
from price_bot.scrape.parse import require_phrases

CURATED = b"""# Ceph Object Gateway
Ceph Object Gateway provides S3-compatible object storage software. It is self-host infrastructure, not hosted metered object storage. Operator must supply storage, request, and egress costs.
"""


class CephFetcher(ProviderFetcher):
    provider_key = "ceph"
    display_name = "Ceph Object Gateway"

    def sources(self) -> list[SourceDefinition]:
        return [
            SourceDefinition(
                kind="markdown",
                url="https://docs.ceph.com/en/latest/radosgw/",
                fixture_name="ceph.md",
            )
        ]

    def curated_payload(self) -> bytes:
        return CURATED

    def build_profiles(self, payload: bytes, catalog_version: str, source_snapshot_sha256: str):
        text = payload.decode("utf-8", errors="replace")
        require_phrases(text, ["Ceph Object Gateway", "S3-compatible"])
        profile = base_profile(
            provider_id=self.provider_key,
            display_name=self.display_name,
            region="operator",
            storage_class="manual",
            catalog_version=catalog_version,
            source_snapshot_sha256=source_snapshot_sha256,
            confidence_level="low",
            confidence_score="0.20",
            confidence_reasons=["self-hosted infrastructure requires operator cost inputs"],
            confidence_unknowns=["operator storage cost", "operator egress cost", "operator request cost"],
            provenance={"provider_type": "self_hosted", "manual_required": True, "source": "Ceph RGW docs"},
        )
        return [profile]
