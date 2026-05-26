from __future__ import annotations

from price_bot.normalizer.rating_profile import base_profile
from price_bot.providers.base import ProviderFetcher, SourceDefinition
from price_bot.scrape.parse import require_phrases, text_from_html

CURATED = b"""<html><body><h1>MinIO Object Store</h1>
<p>MinIO is S3-compatible object storage software. AIStor and enterprise subscriptions are support/product plans, not deterministic per-GB hosted object storage billing.</p>
</body></html>"""


class MinioFetcher(ProviderFetcher):
    provider_key = "minio"
    display_name = "MinIO"

    def sources(self) -> list[SourceDefinition]:
        return [SourceDefinition(kind="html", url="https://min.io/pricing", fixture_name="minio.html")]

    def curated_payload(self) -> bytes:
        return CURATED

    def build_profiles(self, payload: bytes, catalog_version: str, source_snapshot_sha256: str):
        text = text_from_html(payload) if b"<html" in payload.lower() else payload.decode()
        require_phrases(text, ["MinIO", "S3-compatible"])
        profile = base_profile(
            provider_id=self.provider_key,
            display_name=self.display_name,
            region="operator",
            storage_class="manual",
            catalog_version=catalog_version,
            source_snapshot_sha256=source_snapshot_sha256,
            confidence_level="low",
            confidence_score="0.20",
            confidence_reasons=["self-hosted/software provider requires operator cost inputs"],
            confidence_unknowns=["operator storage cost", "operator egress cost", "operator request cost", "support subscription allocation"],
            provenance={"provider_type": "self_hosted", "manual_required": True, "source": "MinIO public pricing/product page"},
        )
        return [profile]
