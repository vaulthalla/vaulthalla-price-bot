from __future__ import annotations

import copy
import io
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from price_bot.normalizer.rating_profile import stable_json_bytes
from price_bot.providers import aws_offer
from price_bot.providers.aws_offer import (
    DATA_TRANSFER_OFFER,
    S3_OFFER,
    OfferExtractionError,
    extract_region_prices,
    parse_payload,
)
from price_bot.providers.aws_s3 import CURATED_PRICES, AwsS3Fetcher
from price_bot.scrape.http import PriceBotHttpClient

FIXTURES = Path(__file__).parent / "fixtures"
OFFER_DIR = FIXTURES / "aws_offers"
BUNDLE = FIXTURES / "providers" / "aws_s3.json"

# meter_types that LocalEstimator prices by iterating every meter: a profile may carry each of
# these at most once, or older daemons double count.
SINGLETON_METER_TYPES = {
    "storage_byte_hours",
    "retrieval_bytes",
    "egress_bytes",
    "downloaded_bytes",
    "uploaded_bytes",
    "object_count_hours",
    "fixed_monthly",
    "minimum_monthly",
}

# Shape published before live extraction; existing daemons depend on it.
LEGACY_METERS = {
    "standard": {
        "storage": "storage_byte_hours",
        "request_write": "request",
        "request_read": "request",
        "egress": "egress_bytes",
        "free_operations": "request",
    },
    "standard-ia": {
        "storage": "storage_byte_hours",
        "request_write": "request",
        "request_read": "request",
        "egress": "egress_bytes",
        "free_operations": "request",
        "retrieval": "retrieval_bytes",
    },
}
LEGACY_OPERATION_MAP = {
    "ListObjectsV2": "request_write",
    "HeadObject": "request_read",
    "GetObject": "request_read",
    "PutObject": "request_write",
    "CopyObject": "request_write",
    "CreateMultipartUpload": "request_write",
    "UploadPart": "request_write",
    "UploadPartCopy": "request_write",
    "CompleteMultipartUpload": "request_write",
    "AbortMultipartUpload": "free_operations",
    "DeleteObject": "free_operations",
    "LifecycleTransition": "request_write",
    "RestoreObject": "request_write",
}
# us-east-1 first-tier rates published by the previous hand-written fixture.
LEGACY_US_EAST_1_RATES = {
    "standard": {"storage": "0.023", "request_write": "0.005", "request_read": "0.0004", "egress": "0.09"},
    "standard-ia": {
        "storage": "0.0125",
        "request_write": "0.01",
        "request_read": "0.001",
        "egress": "0.09",
        "retrieval": "0.01",
    },
}


def load_offer(offer_code: str, region: str) -> dict:
    return json.loads((OFFER_DIR / f"{offer_code}-{region}.json").read_text())


def test_us_east_1_offer_extraction_ignores_decoys() -> None:
    prices = extract_region_prices(
        load_offer(S3_OFFER, "us-east-1"), load_offer(DATA_TRANSFER_OFFER, "us-east-1"), "us-east-1"
    )
    standard = prices.classes["standard"]
    assert [band.to_json() for band in standard.storage_gb_month] == [
        ["0", "51200", "0.023"],
        ["51200", "512000", "0.022"],
        ["512000", None, "0.021"],
    ]
    assert standard.request_tier1_per_1000 == "0.005"
    assert standard.request_tier2_per_1000 == "0.0004"
    assert standard.retrieval_gb is None
    ia = prices.classes["standard-ia"]
    assert ia.storage_gb_month[0].rate == "0.0125"
    assert (ia.request_tier1_per_1000, ia.request_tier2_per_1000, ia.retrieval_gb) == (
        "0.01",
        "0.001",
        "0.01",
    )
    # Internet egress comes from AWSDataTransfer, not the $0 "AWS Outbound" SKU in AmazonS3
    # and not the accelerated (ABytes) SKUs.
    assert prices.egress_gb[0].to_json() == ["0", "10240", "0.09"]
    assert prices.egress_gb[-1].to_json() == ["153600", None, "0.05"]
    assert prices.skus["shared"]["egress"] == "HQEH3ZWJVT46JHRG"
    assert prices.offers[S3_OFFER]["version"] == "20261015173540"


def test_region_prefixed_usagetypes_extract() -> None:
    prices = extract_region_prices(
        load_offer(S3_OFFER, "eu-central-1"),
        load_offer(DATA_TRANSFER_OFFER, "eu-central-1"),
        "eu-central-1",
    )
    assert prices.classes["standard"].storage_gb_month[0].rate == "0.0245"
    assert prices.classes["standard"].request_tier1_per_1000 == "0.0054"
    assert prices.classes["standard"].request_tier2_per_1000 == "0.00043"
    assert prices.classes["standard-ia"].storage_gb_month[0].rate == "0.0135"


def test_offer_for_wrong_region_is_rejected() -> None:
    with pytest.raises(OfferExtractionError, match="exactly one SKU"):
        extract_region_prices(
            load_offer(S3_OFFER, "us-east-1"), load_offer(DATA_TRANSFER_OFFER, "us-east-1"), "us-west-2"
        )


def test_ambiguous_sku_is_rejected() -> None:
    offer = load_offer(S3_OFFER, "us-east-1")
    sku = next(
        sku
        for sku, product in offer["products"].items()
        if product["attributes"]["usagetype"] == "Requests-Tier1"
    )
    offer["products"]["DUPLICATE"] = copy.deepcopy(offer["products"][sku])
    offer["terms"]["OnDemand"]["DUPLICATE"] = copy.deepcopy(offer["terms"]["OnDemand"][sku])
    with pytest.raises(OfferExtractionError, match="found 2"):
        extract_region_prices(offer, load_offer(DATA_TRANSFER_OFFER, "us-east-1"), "us-east-1")


def test_unit_drift_is_rejected() -> None:
    offer = load_offer(S3_OFFER, "us-east-1")
    for term in offer["terms"]["OnDemand"].values():
        for item in term.values():
            for dimension in item["priceDimensions"].values():
                if dimension["unit"] == "Requests":
                    dimension["unit"] = "Requests-1K"
    with pytest.raises(OfferExtractionError, match="unexpected unit"):
        extract_region_prices(offer, load_offer(DATA_TRANSFER_OFFER, "us-east-1"), "us-east-1")


def test_curated_fallback_matches_live_extraction_fixture() -> None:
    _, extracted = parse_payload(BUNDLE.read_bytes())
    assert {item.region for item in extracted} == set(aws_offer.DEFAULT_REGIONS)
    for region_prices in extracted:
        assert region_prices.prices_json() == json.loads(
            json.dumps(CURATED_PRICES["regions"][region_prices.region])
        ), region_prices.region
    bundle = json.loads(BUNDLE.read_text())
    offers = bundle["regions"]["us-east-1"]
    assert CURATED_PRICES["offers"][S3_OFFER]["version"] == offers[S3_OFFER]["version"]
    assert (
        CURATED_PRICES["offers"][DATA_TRANSFER_OFFER]["version"]
        == offers[DATA_TRANSFER_OFFER]["version"]
    )


def test_raw_offer_fixtures_match_bundle() -> None:
    _, extracted = parse_payload(BUNDLE.read_bytes())
    by_region = {item.region: item for item in extracted}
    for region in ("us-east-1", "eu-central-1"):
        raw = extract_region_prices(
            load_offer(S3_OFFER, region), load_offer(DATA_TRANSFER_OFFER, region), region
        )
        assert raw.prices_json() == by_region[region].prices_json()


@pytest.mark.parametrize("source", ["bundle", "curated"])
def test_aws_profiles_keep_legacy_shape(source: str) -> None:
    fetcher = AwsS3Fetcher()
    payload = BUNDLE.read_bytes() if source == "bundle" else fetcher.curated_payload()
    profiles = fetcher.build_profiles(payload, "test-catalog", "abc123")
    assert {(p.scope.region, p.scope.storage_class) for p in profiles} == {
        (region, storage_class)
        for region in aws_offer.DEFAULT_REGIONS
        for storage_class in ("standard", "standard-ia")
    }
    for profile in profiles:
        storage_class = profile.scope.storage_class
        assert profile.profile_id == f"aws-s3/{profile.scope.region}/{storage_class}"
        assert {key: item.meter_type for key, item in profile.meters.items()} == LEGACY_METERS[
            storage_class
        ]
        meter_types = [item.meter_type for item in profile.meters.values()]
        for meter_type in SINGLETON_METER_TYPES:
            assert meter_types.count(meter_type) <= 1
        for operation, meter_key in LEGACY_OPERATION_MAP.items():
            assert profile.operation_map[operation].meter_key == meter_key
            assert profile.operation_map[operation].multiplier == Decimal("1")
        assert set(profile.operation_map) == set(LEGACY_OPERATION_MAP)
        if profile.scope.region == "us-east-1":
            rates = {key: str(item.tiers[0].rate) for key, item in profile.meters.items() if key != "free_operations"}
            assert rates == LEGACY_US_EAST_1_RATES[storage_class]
        if storage_class == "standard-ia":
            rule = profile.storage_rules[0]
            assert rule.minimum_storage_duration_days == 30
            assert rule.minimum_billable_object_size_bytes == 131072
        else:
            assert profile.storage_rules == []
        assert profile.provenance["documented_constants"]["documentation_url"].startswith(
            "https://docs.aws.amazon.com/"
        )
        assert profile.provenance["source_mode"] == (
            "live_extract" if source == "bundle" else "curated"
        )
        assert profile.provenance["retrieved_at"]


def test_legacy_snapshot_still_normalizes() -> None:
    legacy = {
        "serviceCode": "AmazonS3",
        "region": "us-east-1",
        "standard": {
            "storage_gb_month": "0.023",
            "put_copy_post_list_1000": "0.005",
            "get_head_1000": "0.0004",
            "egress_gb": "0.09",
        },
        "standard_ia": {
            "storage_gb_month": "0.0125",
            "put_copy_post_list_1000": "0.01",
            "get_head_1000": "0.001",
            "retrieval_gb": "0.01",
            "egress_gb": "0.09",
        },
    }
    profiles = AwsS3Fetcher().build_profiles(json.dumps(legacy).encode(), "test", "abc")
    assert [p.profile_id for p in profiles] == ["aws-s3/us-east-1/standard", "aws-s3/us-east-1/standard-ia"]
    assert profiles[0].provenance["source_mode"] == "legacy"


class FixtureClient:
    """Stands in for PriceBotHttpClient, serving the trimmed offer fixtures by URL."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.closed = False

    def download_to(self, url: str, sink, *, max_bytes: int) -> int:
        self.urls.append(url)
        for offer_code in aws_offer.OFFER_CODES:
            for region in ("us-east-1", "eu-central-1"):
                if url == aws_offer.offer_url(offer_code, region):
                    payload = (OFFER_DIR / f"{offer_code}-{region}.json").read_bytes()
                    sink.write(payload)
                    return len(payload)
        raise httpx.ConnectError(f"no fixture for {url}")

    def close(self) -> None:
        self.closed = True


def test_live_fetch_extracts_and_trims() -> None:
    client = FixtureClient()
    fetcher = AwsS3Fetcher(regions=("us-east-1", "eu-central-1"), client_factory=lambda: client)
    result = fetcher.fetch(live=True, fixture_dir=None)
    assert result.metadata["mode"] == "live"
    assert client.closed
    assert sorted(client.urls) == sorted(
        aws_offer.offer_url(code, region)
        for code in aws_offer.OFFER_CODES
        for region in ("us-east-1", "eu-central-1")
    )
    bundle = json.loads(result.payload)
    assert bundle["format"] == aws_offer.EXTRACT_FORMAT
    # Only the SKUs that are priced survive trimming (decoys are dropped).
    assert len(bundle["regions"]["us-east-1"][S3_OFFER]["products"]) == 7
    assert len(bundle["regions"]["us-east-1"][DATA_TRANSFER_OFFER]["products"]) == 1
    profiles = fetcher.build_profiles(result.payload, "live-catalog", "abc")
    assert len(profiles) == 4
    assert all(p.provenance["source_mode"] == "live_extract" for p in profiles)
    assert profiles[0].provenance["offers"][S3_OFFER]["url"].endswith("/eu-central-1/index.json")


def test_live_fetch_failure_falls_back_to_curated() -> None:
    client = FixtureClient()
    fetcher = AwsS3Fetcher(regions=("us-east-1", "ap-south-1"), client_factory=lambda: client)
    result = fetcher.fetch(live=True, fixture_dir=None)
    assert result.metadata["mode"] == "curated-fallback"
    assert "ConnectError" in result.metadata["live_error"]
    assert client.closed
    assert result.payload == stable_json_bytes(CURATED_PRICES)
    assert len(fetcher.build_profiles(result.payload, "c", "abc")) == 10


def test_fixture_dir_takes_precedence_over_live() -> None:
    def explode():
        raise AssertionError("live client must not be created when a fixture is used")

    fetcher = AwsS3Fetcher(client_factory=explode)
    result = fetcher.fetch(live=True, fixture_dir=FIXTURES / "providers")
    assert result.metadata["mode"] == "fixture"


def test_download_to_streams_and_enforces_size_cap() -> None:
    body = json.dumps({"offerCode": "AmazonS3"}).encode()
    client = PriceBotHttpClient()
    client.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
    try:
        sink = io.BytesIO()
        assert client.download_to("https://example.invalid/offer.json", sink, max_bytes=1024) == len(body)
        assert sink.getvalue() == body
        with pytest.raises(ValueError, match="exceeded"):
            client.download_to("https://example.invalid/offer.json", io.BytesIO(), max_bytes=4)
    finally:
        client.close()
