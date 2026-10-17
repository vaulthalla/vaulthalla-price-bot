"""Selective extraction of S3 prices from the official AWS Price List Bulk API offer files.

AWS publishes one offer file per service and region:

    https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<offerCode>/current/<region>/index.json

S3 storage, request and retrieval SKUs live in the ``AmazonS3`` offer. Internet egress
("AWS Outbound" to ``External``) moved to the separate ``AWSDataTransfer`` offer; the
``DataTransfer-Out-Bytes`` SKU still present in the AmazonS3 offer is the $0 intra-AWS line,
not internet egress, so it is deliberately not used.

The pipeline is: download each offer (streamed to a bounded spooled temp file), keep only the
SKUs Vaulthalla prices (``trim_offer``), and store that trimmed, still AWS-shaped subset as the
raw snapshot. ``extract_region_prices`` turns a trimmed (or full) offer pair into
``RegionPrices``, refusing ambiguous or missing SKUs so a format change falls back to the curated
table instead of publishing wrong numbers.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

OFFER_URL_TEMPLATE = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{offer_code}/current/{region}/index.json"
)
S3_OFFER = "AmazonS3"
DATA_TRANSFER_OFFER = "AWSDataTransfer"
OFFER_CODES = (S3_OFFER, DATA_TRANSFER_OFFER)

EXTRACT_FORMAT = "vaulthalla.aws-s3-offer-extract.v1"
CURATED_FORMAT = "vaulthalla.aws-s3-curated.v1"

DEFAULT_REGIONS = ("us-east-1", "us-east-2", "us-west-2", "eu-west-1", "eu-central-1")

# Storage classes Vaulthalla profiles. GLACIER_IR is intentionally absent: its request and
# retrieval SKUs are only published in some regional offers (e.g. us-east-1 but not us-east-2,
# us-west-2, eu-west-1 or eu-central-1 as of offer 20261015173540), so it cannot be extracted
# cleanly for every region.
STORAGE_CLASSES = ("standard", "standard-ia")

# Upper bound for a single downloaded offer file. Current regional AmazonS3 offers are ~0.5 MB
# and AWSDataTransfer ~1.5 MB; this guards against pointing at the multi-hundred-MB global file.
MAX_OFFER_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class SkuSelector:
    key: str
    offer_code: str
    product_family: str
    usagetype: str
    unit: str
    storage_class: str | None = None

    def matches(self, product: dict[str, Any], region: str) -> bool:
        attributes = product.get("attributes", {})
        if product.get("productFamily") != self.product_family:
            return False
        if self.offer_code == DATA_TRANSFER_OFFER:
            if attributes.get("fromRegionCode") != region:
                return False
            if attributes.get("toLocation") != "External":
                return False
            if attributes.get("transferType") != "AWS Outbound":
                return False
        elif attributes.get("regionCode") != region:
            return False
        return _usagetype_matches(attributes.get("usagetype", ""), self.usagetype)


SELECTORS: tuple[SkuSelector, ...] = (
    SkuSelector("storage", S3_OFFER, "Storage", "TimedStorage-ByteHrs", "GB-Mo", "standard"),
    SkuSelector("request_tier1", S3_OFFER, "API Request", "Requests-Tier1", "Requests", "standard"),
    SkuSelector("request_tier2", S3_OFFER, "API Request", "Requests-Tier2", "Requests", "standard"),
    SkuSelector(
        "storage", S3_OFFER, "Storage", "TimedStorage-SIA-ByteHrs", "GB-Mo", "standard-ia"
    ),
    SkuSelector(
        "request_tier1", S3_OFFER, "API Request", "Requests-SIA-Tier1", "Requests", "standard-ia"
    ),
    SkuSelector(
        "request_tier2", S3_OFFER, "API Request", "Requests-SIA-Tier2", "Requests", "standard-ia"
    ),
    SkuSelector("retrieval", S3_OFFER, "API Request", "Retrieval-SIA", "GB", "standard-ia"),
    SkuSelector("egress", DATA_TRANSFER_OFFER, "Data Transfer", "DataTransfer-Out-Bytes", "GB"),
)

# Documented constants that the Price List does not carry.
# https://docs.aws.amazon.com/AmazonS3/latest/userguide/storage-class-intro.html
STORAGE_CLASS_DOC_URL = "https://docs.aws.amazon.com/AmazonS3/latest/userguide/storage-class-intro.html"
DOCUMENTED_MINIMUMS: dict[str, dict[str, int]] = {
    "standard": {"minimum_storage_duration_days": 0, "minimum_billable_object_size_bytes": 0},
    "standard-ia": {
        "minimum_storage_duration_days": 30,
        "minimum_billable_object_size_bytes": 131072,
    },
}


def offer_url(offer_code: str, region: str) -> str:
    return OFFER_URL_TEMPLATE.format(offer_code=offer_code, region=region)


def _usagetype_matches(usagetype: str, base: str) -> bool:
    # us-east-1 usage types are unprefixed; other regions carry a short code such as "USE2-",
    # "USW2-", "EU-" or "EUC1-". The prefix must be a single upper-case/digit token so that
    # e.g. "Requests-Annotation-Tier1" or "USE1-Files-TimedStorage-ByteHrs" never match.
    return re.fullmatch(r"(?:[A-Z]{2,5}\d*-)?" + re.escape(base), usagetype) is not None


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


@dataclass
class RateBand:
    start: str
    end: str | None
    rate: str

    def to_json(self) -> list[str | None]:
        return [self.start, self.end, self.rate]

    @classmethod
    def from_json(cls, item: list[str | None]) -> RateBand:
        start, end, rate = item
        return cls(start=str(start), end=None if end is None else str(end), rate=str(rate))


@dataclass
class ClassPrices:
    storage_gb_month: list[RateBand]
    request_tier1_per_1000: str
    request_tier2_per_1000: str
    retrieval_gb: str | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "storage_gb_month": [band.to_json() for band in self.storage_gb_month],
            "put_copy_post_list_1000": self.request_tier1_per_1000,
            "get_head_other_1000": self.request_tier2_per_1000,
        }
        if self.retrieval_gb is not None:
            data["retrieval_gb"] = self.retrieval_gb
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ClassPrices:
        return cls(
            storage_gb_month=[RateBand.from_json(item) for item in data["storage_gb_month"]],
            request_tier1_per_1000=str(data["put_copy_post_list_1000"]),
            request_tier2_per_1000=str(data["get_head_other_1000"]),
            retrieval_gb=str(data["retrieval_gb"]) if "retrieval_gb" in data else None,
        )


@dataclass
class RegionPrices:
    region: str
    classes: dict[str, ClassPrices]
    egress_gb: list[RateBand]
    offers: dict[str, dict[str, str]] = field(default_factory=dict)
    skus: dict[str, dict[str, str]] = field(default_factory=dict)

    def prices_json(self) -> dict[str, Any]:
        """Only the price values: what the curated table stores and what tests compare."""
        return {
            "classes": {name: prices.to_json() for name, prices in sorted(self.classes.items())},
            "egress_gb": [band.to_json() for band in self.egress_gb],
        }

    @classmethod
    def from_curated(
        cls, region: str, data: dict[str, Any], offers: dict[str, dict[str, str]]
    ) -> RegionPrices:
        return cls(
            region=region,
            classes={
                name: ClassPrices.from_json(value) for name, value in data["classes"].items()
            },
            egress_gb=[RateBand.from_json(item) for item in data["egress_gb"]],
            offers=offers,
        )


class OfferExtractionError(ValueError):
    pass


def trim_offer(
    offer: dict[str, Any],
    region: str,
    *,
    url: str | None = None,
    extra_skus: Iterable[str] = (),
) -> dict[str, Any]:
    """Return an AWS-shaped copy of ``offer`` holding only the SKUs Vaulthalla prices."""
    offer_code = offer.get("offerCode")
    if offer_code not in OFFER_CODES:
        raise OfferExtractionError(f"unexpected AWS offerCode {offer_code!r}")
    selectors = [item for item in SELECTORS if item.offer_code == offer_code]
    keep = set(extra_skus)
    for sku, product in offer.get("products", {}).items():
        if any(selector.matches(product, region) for selector in selectors):
            keep.add(sku)
    on_demand = offer.get("terms", {}).get("OnDemand", {})
    trimmed: dict[str, Any] = {
        "formatVersion": offer.get("formatVersion"),
        "offerCode": offer_code,
        "version": offer.get("version"),
        "publicationDate": offer.get("publicationDate"),
        "products": {sku: offer["products"][sku] for sku in sorted(keep) if sku in offer["products"]},
        "terms": {"OnDemand": {sku: on_demand[sku] for sku in sorted(keep) if sku in on_demand}},
    }
    if url:
        trimmed["url"] = url
    return trimmed


def extract_region_prices(
    s3_offer: dict[str, Any], data_transfer_offer: dict[str, Any], region: str
) -> RegionPrices:
    """Extract Vaulthalla's S3 prices for one region from AmazonS3 + AWSDataTransfer offers."""
    offers = {S3_OFFER: s3_offer, DATA_TRANSFER_OFFER: data_transfer_offer}
    for code, offer in offers.items():
        if offer.get("offerCode") != code:
            raise OfferExtractionError(f"expected offerCode {code}, got {offer.get('offerCode')!r}")

    values: dict[tuple[str | None, str], list[RateBand]] = {}
    skus: dict[str, dict[str, str]] = {}
    for selector in SELECTORS:
        offer = offers[selector.offer_code]
        hits = [
            sku
            for sku, product in offer.get("products", {}).items()
            if selector.matches(product, region)
        ]
        label = f"{region} {selector.offer_code} {selector.usagetype}"
        if len(hits) != 1:
            raise OfferExtractionError(f"expected exactly one SKU for {label}, found {len(hits)}")
        sku = hits[0]
        values[(selector.storage_class, selector.key)] = _rate_bands(
            offer, sku, unit=selector.unit, label=label
        )
        skus.setdefault(selector.storage_class or "shared", {})[selector.key] = sku

    def single(storage_class: str, key: str) -> Decimal:
        bands = values[(storage_class, key)]
        if len(bands) != 1:
            raise OfferExtractionError(
                f"expected a flat rate for {region} {storage_class} {key}, found {len(bands)} tiers"
            )
        return Decimal(bands[0].rate)

    classes: dict[str, ClassPrices] = {}
    for storage_class in STORAGE_CLASSES:
        has_retrieval = (storage_class, "retrieval") in values
        classes[storage_class] = ClassPrices(
            storage_gb_month=values[(storage_class, "storage")],
            # The Price List quotes requests per single request; profiles use per-1,000.
            request_tier1_per_1000=_plain(single(storage_class, "request_tier1") * 1000),
            request_tier2_per_1000=_plain(single(storage_class, "request_tier2") * 1000),
            retrieval_gb=_plain(single(storage_class, "retrieval")) if has_retrieval else None,
        )

    return RegionPrices(
        region=region,
        classes=classes,
        egress_gb=values[(None, "egress")],
        offers={
            code: {
                "version": str(offer.get("version")),
                "publication_date": str(offer.get("publicationDate")),
                "url": str(offer.get("url") or offer_url(code, region)),
            }
            for code, offer in offers.items()
        },
        skus=skus,
    )


def _rate_bands(offer: dict[str, Any], sku: str, *, unit: str, label: str) -> list[RateBand]:
    terms = offer.get("terms", {}).get("OnDemand", {}).get(sku)
    if not terms or len(terms) != 1:
        raise OfferExtractionError(f"expected exactly one OnDemand term for {label}")
    term = next(iter(terms.values()))
    dimensions = sorted(
        term.get("priceDimensions", {}).values(), key=lambda item: Decimal(item["beginRange"])
    )
    if not dimensions:
        raise OfferExtractionError(f"no price dimensions for {label}")
    bands: list[RateBand] = []
    expected_start = Decimal("0")
    for dimension in dimensions:
        if dimension.get("unit") != unit:
            raise OfferExtractionError(
                f"unexpected unit {dimension.get('unit')!r} for {label}; expected {unit!r}"
            )
        usd = dimension.get("pricePerUnit", {}).get("USD")
        if usd is None:
            raise OfferExtractionError(f"no USD price for {label}")
        start = Decimal(dimension["beginRange"])
        if start != expected_start:
            raise OfferExtractionError(f"non-contiguous tiers for {label}")
        end_raw = dimension.get("endRange")
        end = None if end_raw in (None, "Inf") else Decimal(end_raw)
        bands.append(
            RateBand(
                start=_plain(start),
                end=None if end is None else _plain(end),
                rate=_plain(Decimal(usd)),
            )
        )
        if end is None:
            break
        expected_start = end
    if bands[-1].end is not None:
        raise OfferExtractionError(f"open-ended final tier missing for {label}")
    return bands


def build_extract_bundle(
    regions: Iterable[str],
    load_offer: Callable[[str, str], dict[str, Any]],
    *,
    retrieved_at: str,
) -> dict[str, Any]:
    """Download (via ``load_offer(offer_code, region)``), trim and validate every region.

    Validation runs here so a format drift fails the live fetch, not the later normalize step.
    """
    bundle: dict[str, Any] = {"format": EXTRACT_FORMAT, "retrieved_at": retrieved_at, "regions": {}}
    for region in regions:
        trimmed = {
            code: trim_offer(load_offer(code, region), region, url=offer_url(code, region))
            for code in OFFER_CODES
        }
        extract_region_prices(trimmed[S3_OFFER], trimmed[DATA_TRANSFER_OFFER], region)
        bundle["regions"][region] = trimmed
    return bundle


def parse_payload(payload: bytes) -> tuple[dict[str, Any], list[RegionPrices]]:
    """Parse a stored AWS snapshot (live extract bundle or curated table) into region prices."""
    data = json.loads(payload)
    payload_format = data.get("format")
    if payload_format == EXTRACT_FORMAT:
        regions = [
            extract_region_prices(offers[S3_OFFER], offers[DATA_TRANSFER_OFFER], region)
            for region, offers in sorted(data["regions"].items())
        ]
    elif payload_format == CURATED_FORMAT:
        regions = [
            RegionPrices.from_curated(region, values, data.get("offers", {}))
            for region, values in sorted(data["regions"].items())
        ]
    elif payload_format is None and data.get("serviceCode") == "AmazonS3":
        regions = [_legacy_region(data)]
    else:
        raise OfferExtractionError(f"unsupported AWS S3 snapshot format {payload_format!r}")
    if not regions:
        raise OfferExtractionError("AWS S3 snapshot contains no regions")
    return data, regions


def _legacy_region(data: dict[str, Any]) -> RegionPrices:
    """Pre-extraction snapshots (single region, hand-written shape) stored by older releases."""
    standard = data["standard"]
    ia = data["standard_ia"]
    return RegionPrices(
        region=data["region"],
        classes={
            "standard": ClassPrices(
                storage_gb_month=[RateBand("0", None, standard["storage_gb_month"])],
                request_tier1_per_1000=standard["put_copy_post_list_1000"],
                request_tier2_per_1000=standard["get_head_1000"],
            ),
            "standard-ia": ClassPrices(
                storage_gb_month=[RateBand("0", None, ia["storage_gb_month"])],
                request_tier1_per_1000=ia["put_copy_post_list_1000"],
                request_tier2_per_1000=ia["get_head_1000"],
                retrieval_gb=ia["retrieval_gb"],
            ),
        },
        egress_gb=[RateBand("0", None, standard["egress_gb"])],
    )


def download_offer_json(client, url: str, *, max_bytes: int = MAX_OFFER_BYTES) -> dict[str, Any]:
    """Stream ``url`` into a bounded spooled temp file and parse it.

    The response is never held as one in-memory ``bytes`` object; it spills to disk past 8 MiB
    and is rejected past ``max_bytes``.
    """
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as buffer:
        client.download_to(url, buffer, max_bytes=max_bytes)
        buffer.seek(0)
        return json.load(buffer)
