from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from price_bot.normalizer.models import RatingProfile
from price_bot.normalizer.rating_profile import (
    base_profile,
    meter,
    op_map,
    stable_json_bytes,
    storage_rule,
    tier,
    utc_now_iso,
)
from price_bot.providers import aws_offer
from price_bot.providers.aws_offer import (
    CURATED_FORMAT,
    DEFAULT_REGIONS,
    DOCUMENTED_MINIMUMS,
    EXTRACT_FORMAT,
    STORAGE_CLASS_DOC_URL,
    RateBand,
    RegionPrices,
)
from price_bot.providers.base import FetchResult, ProviderFetcher, SourceDefinition
from price_bot.scrape.http import PriceBotHttpClient

log = logging.getLogger(__name__)

PRICING_PAGE_URL = "https://aws.amazon.com/s3/pricing/"

# Curated fallback, used when live fetching is disabled or fails. It must equal what
# aws_offer.extract_region_prices returns for the committed offer fixtures
# (tests/test_aws_offer.py enforces this), so refresh both together.
_US_STANDARD_STORAGE = [
    ["0", "51200", "0.023"],
    ["51200", "512000", "0.022"],
    ["512000", None, "0.021"],
]
_EGRESS = [
    ["0", "10240", "0.09"],
    ["10240", "51200", "0.085"],
    ["51200", "153600", "0.07"],
    ["153600", None, "0.05"],
]
_US_CLASSES = {
    "standard": {
        "storage_gb_month": _US_STANDARD_STORAGE,
        "put_copy_post_list_1000": "0.005",
        "get_head_other_1000": "0.0004",
    },
    "standard-ia": {
        "storage_gb_month": [["0", None, "0.0125"]],
        "put_copy_post_list_1000": "0.01",
        "get_head_other_1000": "0.001",
        "retrieval_gb": "0.01",
    },
}
CURATED_PRICES: dict[str, Any] = {
    "format": CURATED_FORMAT,
    "retrieved_at": "2026-10-16T23:59:54Z",
    "source": (
        "Curated from the AWS Price List Bulk API per-region offer files "
        "(AmazonS3 version 20261015173540, AWSDataTransfer version 20260916132208)"
    ),
    "offers": {
        "AmazonS3": {
            "version": "20261015173540",
            "publication_date": "2026-10-15T17:35:40Z",
            "url": aws_offer.OFFER_URL_TEMPLATE.format(offer_code="AmazonS3", region="{region}"),
        },
        "AWSDataTransfer": {
            "version": "20260916132208",
            "publication_date": "2026-09-16T13:22:08Z",
            "url": aws_offer.OFFER_URL_TEMPLATE.format(
                offer_code="AWSDataTransfer", region="{region}"
            ),
        },
    },
    "regions": {
        "us-east-1": {"classes": _US_CLASSES, "egress_gb": _EGRESS},
        "us-east-2": {"classes": _US_CLASSES, "egress_gb": _EGRESS},
        "us-west-2": {"classes": _US_CLASSES, "egress_gb": _EGRESS},
        "eu-west-1": {"classes": _US_CLASSES, "egress_gb": _EGRESS},
        "eu-central-1": {
            "classes": {
                "standard": {
                    "storage_gb_month": [
                        ["0", "51200", "0.0245"],
                        ["51200", "512000", "0.0235"],
                        ["512000", None, "0.0225"],
                    ],
                    "put_copy_post_list_1000": "0.0054",
                    "get_head_other_1000": "0.00043",
                },
                "standard-ia": {
                    "storage_gb_month": [["0", None, "0.0135"]],
                    "put_copy_post_list_1000": "0.01",
                    "get_head_other_1000": "0.001",
                    "retrieval_gb": "0.01",
                },
            },
            "egress_gb": _EGRESS,
        },
    },
}

class AwsS3Fetcher(ProviderFetcher):
    provider_key = "aws-s3"
    display_name = "AWS S3"

    def __init__(
        self,
        *,
        regions: tuple[str, ...] = DEFAULT_REGIONS,
        client_factory: Callable[[], Any] = PriceBotHttpClient,
    ) -> None:
        self.regions = regions
        self.client_factory = client_factory

    def sources(self) -> list[SourceDefinition]:
        return [
            SourceDefinition(
                kind="api",
                url=aws_offer.OFFER_URL_TEMPLATE.format(offer_code="AmazonS3", region="{region}"),
                fixture_name="aws_s3.json",
            )
        ]

    def curated_payload(self) -> bytes:
        return stable_json_bytes(CURATED_PRICES)

    def fetch(self, *, live: bool, fixture_dir: Path | None) -> FetchResult:
        if fixture_dir is not None or not live:
            return super().fetch(live=False, fixture_dir=fixture_dir)
        source = self.sources()[0]
        try:
            payload = stable_json_bytes(self.fetch_live_bundle())
        except Exception as exc:  # any live failure (network, size, format drift) -> curated
            log.warning("AWS Price List live extraction failed; using curated table: %s", exc)
            result = super().fetch(live=False, fixture_dir=None)
            return FetchResult(
                source=result.source,
                payload=result.payload,
                content_type=result.content_type,
                metadata={**result.metadata, "live_error": f"{type(exc).__name__}: {exc}"[:500]},
            )
        return FetchResult(
            source=source,
            payload=payload,
            content_type="application/json",
            metadata={
                "mode": "live",
                "url": source.url,
                "regions": list(self.regions),
                "offers": list(aws_offer.OFFER_CODES),
            },
            http_status=200,
        )

    def fetch_live_bundle(self) -> dict[str, Any]:
        client = self.client_factory()
        try:
            return aws_offer.build_extract_bundle(
                self.regions,
                lambda code, region: aws_offer.download_offer_json(
                    client, aws_offer.offer_url(code, region)
                ),
                retrieved_at=utc_now_iso(),
            )
        finally:
            client.close()

    def build_profiles(self, payload: bytes, catalog_version: str, source_snapshot_sha256: str):
        data, regions = aws_offer.parse_payload(payload)
        return [
            self._profile(region_prices, storage_class, data, catalog_version, source_snapshot_sha256)
            for region_prices in regions
            for storage_class in sorted(region_prices.classes)
        ]

    def _profile(
        self,
        region_prices: RegionPrices,
        storage_class: str,
        data: dict[str, Any],
        catalog_version: str,
        source_snapshot_sha256: str,
    ) -> RatingProfile:
        prices = region_prices.classes[storage_class]
        payload_format = data.get("format")
        live = payload_format == EXTRACT_FORMAT
        if live:
            source_mode = "live_extract"
            source = "AWS Price List Bulk API per-region offer files (AmazonS3 + AWSDataTransfer)"
        elif payload_format == CURATED_FORMAT:
            source_mode = "curated"
            source = data["source"]
        else:
            source_mode = "legacy"
            source = "Legacy single-region AWS S3 snapshot"
        minimums = DOCUMENTED_MINIMUMS[storage_class]
        provenance: dict[str, Any] = {
            "source": source,
            "source_mode": source_mode,
            "retrieved_at": data.get("retrieved_at"),
            "pricing_page": PRICING_PAGE_URL,
            "offers": region_prices.offers or data.get("offers", {}),
            "documented_constants": {**minimums, "documentation_url": STORAGE_CLASS_DOC_URL},
            "notes": [
                "Request rates are converted from the Price List's per-request USD to per-1,000.",
                "Storage and egress carry the Price List volume tiers; estimators use the first tier.",
                "The AWS 100 GB/month account-wide data transfer free tier is not modeled.",
            ],
        }
        if region_prices.skus:
            provenance["skus"] = {
                **region_prices.skus.get(storage_class, {}),
                **region_prices.skus.get("shared", {}),
            }
        profile = base_profile(
            provider_id=self.provider_key,
            display_name=self.display_name,
            region=region_prices.region,
            storage_class=storage_class,
            catalog_version=catalog_version,
            source_snapshot_sha256=source_snapshot_sha256,
            confidence_level="high",
            confidence_score="0.93" if live else "0.86",
            confidence_reasons=[
                "prices extracted from official AWS Price List Bulk API offer files"
                if live
                else "curated table copied from official AWS Price List Bulk API offer files",
                "minimum storage duration and object size are documented constants",
            ],
            provenance=provenance,
        )
        storage_meter = meter(
            "storage",
            "storage_byte_hours",
            "byte_hour",
            prices.storage_gb_month[0].rate,
            "gb_month",
            billing_unit="gb_month",
        )
        storage_meter.tiers = _tiers(prices.storage_gb_month, "gb_month")
        egress_meter = meter(
            "egress", "egress_bytes", "byte", region_prices.egress_gb[0].rate, "gb", billing_unit="gb"
        )
        egress_meter.tiers = _tiers(region_prices.egress_gb, "gb")
        profile.meters = {
            "storage": storage_meter,
            "request_write": meter(
                "request_write",
                "request",
                "operation",
                prices.request_tier1_per_1000,
                "request_1000",
                billing_unit="request_1000",
            ),
            "request_read": meter(
                "request_read",
                "request",
                "operation",
                prices.request_tier2_per_1000,
                "request_1000",
                billing_unit="request_1000",
            ),
            "egress": egress_meter,
            "free_operations": meter("free_operations", "request", "operation", "0", "operation"),
        }
        if prices.retrieval_gb is not None:
            profile.meters["retrieval"] = meter(
                "retrieval", "retrieval_bytes", "byte", prices.retrieval_gb, "gb", billing_unit="gb"
            )
        profile.operation_map = {
            "ListObjectsV2": op_map("request_write"),
            "HeadObject": op_map("request_read"),
            "GetObject": op_map("request_read"),
            "PutObject": op_map("request_write"),
            "CopyObject": op_map("request_write"),
            "CreateMultipartUpload": op_map("request_write"),
            "UploadPart": op_map("request_write"),
            "UploadPartCopy": op_map("request_write"),
            "CompleteMultipartUpload": op_map("request_write"),
            "AbortMultipartUpload": op_map("free_operations"),
            "DeleteObject": op_map("free_operations"),
            # This class's Tier1 rate is AWS's lifecycle transition rate *into* the class
            # (transitions into S3 Standard do not exist), so the mapping is left as is.
            "LifecycleTransition": op_map("request_write"),
            "RestoreObject": op_map("request_write"),
        }
        if minimums["minimum_storage_duration_days"] or minimums["minimum_billable_object_size_bytes"]:
            profile.storage_rules = [
                storage_rule(
                    min_days=minimums["minimum_storage_duration_days"] or None,
                    min_object_bytes=minimums["minimum_billable_object_size_bytes"] or None,
                )
            ]
        return profile


def _tiers(bands: list[RateBand], rate_unit: str):
    return [tier(band.rate, rate_unit, start=band.start, end=band.end) for band in bands]
