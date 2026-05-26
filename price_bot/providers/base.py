from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from price_bot.scrape.http import PriceBotHttpClient


@dataclass(frozen=True)
class SourceDefinition:
    kind: str
    url: str | None = None
    fixture_name: str | None = None
    region: str | None = None
    enabled: bool = True
    priority: int = 100


@dataclass(frozen=True)
class FetchResult:
    source: SourceDefinition
    payload: bytes
    content_type: str | None
    metadata: dict = field(default_factory=dict)
    http_status: int | None = None


class ProviderFetcher:
    provider_key: str
    display_name: str
    api_family = "s3-compatible"

    def sources(self) -> list[SourceDefinition]:
        raise NotImplementedError

    def curated_payload(self) -> bytes:
        raise NotImplementedError

    def fetch(
        self,
        *,
        live: bool,
        fixture_dir: Path | None,
    ) -> FetchResult:
        source = self.sources()[0]
        if fixture_dir is not None and source.fixture_name:
            fixture_path = fixture_dir / source.fixture_name
            if fixture_path.exists():
                return FetchResult(
                    source=source,
                    payload=fixture_path.read_bytes(),
                    content_type=_content_type(source.fixture_name),
                    metadata={"mode": "fixture", "fixture": str(fixture_path)},
                )
        if live:
            if not source.url:
                raise RuntimeError(f"{self.provider_key} has no live URL configured")
            client = PriceBotHttpClient()
            try:
                response = client.get(source.url)
            finally:
                client.close()
            return FetchResult(
                source=source,
                payload=response.content,
                content_type=response.headers.get("content-type"),
                metadata={"mode": "live", "url": source.url},
                http_status=response.status_code,
            )
        return FetchResult(
            source=source,
            payload=self.curated_payload(),
            content_type=_content_type(source.fixture_name or ""),
            metadata={"mode": "curated-fallback"},
        )

    def build_profiles(self, payload: bytes, catalog_version: str, source_snapshot_sha256: str):
        raise NotImplementedError


def make_catalog_version() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _content_type(name: str) -> str:
    if name.endswith(".json"):
        return "application/json"
    if name.endswith(".md"):
        return "text/markdown"
    if name.endswith(".html"):
        return "text/html"
    return "application/octet-stream"
