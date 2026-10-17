from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from price_bot.normalizer.estimator import estimate_cost, estimate_from_files
from price_bot.normalizer.models import EstimateOptions, RatingProfile, UsageInput
from price_bot.normalizer.units import BILLING_MONTH_HOURS, GB, GIB, TB
from price_bot.providers import all_fetchers

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "providers"


def profile(provider_key: str, storage_class: str, region: str | None = None) -> RatingProfile:
    fetcher = next(item for item in all_fetchers() if item.provider_key == provider_key)
    source = fetcher.sources()[0]
    profiles = fetcher.build_profiles(
        (FIXTURE_DIR / source.fixture_name).read_bytes(), "test-catalog", "abc123"
    )
    return next(
        item
        for item in profiles
        if item.scope.storage_class == storage_class
        and (region is None or item.scope.region == region)
    )


def test_r2_standard_free_tier_example() -> None:
    result = estimate_cost(
        profile("cloudflare-r2", "standard"),
        UsageInput(
            provider_operation_counts={"PutObject": Decimal("1000"), "GetObject": Decimal("1000")},
            storage_byte_hours={"standard": Decimal("5") * GB * BILLING_MONTH_HOURS},
            egress_bytes=Decimal("10") * GB,
        ),
    )
    assert result.estimated_cost == Decimal("0E-8")
    assert result.estimate_mode == "reporting"
    assert result.free_tier_policy == "apply_free_tiers"
    assert result.free_tiers_applied is True
    assert result.free_tier_applied["storage"] == Decimal("5")


def test_r2_standard_reporting_mode_applies_free_tiers() -> None:
    result = estimate_cost(
        profile("cloudflare-r2", "standard"),
        UsageInput(storage_byte_hours={"standard": Decimal("5") * GB * BILLING_MONTH_HOURS}),
        EstimateOptions(mode="reporting"),
    )
    assert result.estimated_cost == Decimal("0E-8")
    assert result.estimate_mode == "reporting"
    assert result.free_tier_policy == "apply_free_tiers"
    assert result.free_tier_applied["storage"] == Decimal("5")


def test_r2_standard_budget_conservative_ignores_free_tiers() -> None:
    reporting = estimate_cost(
        profile("cloudflare-r2", "standard"),
        UsageInput(
            provider_operation_counts={"PutObject": Decimal("1000"), "GetObject": Decimal("1000")},
            storage_byte_hours={"standard": Decimal("5") * GB * BILLING_MONTH_HOURS},
        ),
        EstimateOptions(mode="reporting"),
    )
    conservative = estimate_cost(
        profile("cloudflare-r2", "standard"),
        UsageInput(
            provider_operation_counts={"PutObject": Decimal("1000"), "GetObject": Decimal("1000")},
            storage_byte_hours={"standard": Decimal("5") * GB * BILLING_MONTH_HOURS},
        ),
        EstimateOptions(mode="budget_conservative"),
    )

    assert reporting.estimated_cost == Decimal("0E-8")
    assert conservative.estimated_cost > reporting.estimated_cost
    assert conservative.estimate_mode == "budget_conservative"
    assert conservative.free_tier_policy == "ignore_account_wide_free_tiers"
    assert conservative.free_tiers_applied is False
    assert conservative.free_tier_applied == {}
    assert "account-level free tiers intentionally ignored for budget-safe estimate" in conservative.unknowns
    assert any(
        "free tier ignored for budget safety" in note
        for item in conservative.breakdown
        for note in item.notes
    )


def test_apply_free_tiers_false_selects_conservative_policy() -> None:
    result = estimate_cost(
        profile("cloudflare-r2", "standard"),
        UsageInput(storage_byte_hours={"standard": Decimal("5") * GB * BILLING_MONTH_HOURS}),
        EstimateOptions(apply_free_tiers=False),
    )
    assert result.estimated_cost == Decimal("0.07500000")
    assert result.estimate_mode == "budget_conservative"
    assert result.free_tier_policy == "ignore_account_wide_free_tiers"
    assert result.free_tiers_applied is False


def test_r2_ia_retrieval_and_minimum_duration_penalty() -> None:
    result = estimate_cost(
        profile("cloudflare-r2", "infrequent-access"),
        UsageInput(
            storage_byte_hours={"infrequent-access": Decimal("10") * GB * BILLING_MONTH_HOURS},
            retrieval_bytes=Decimal("100") * GB,
            early_delete_gb_days={"infrequent-access": Decimal("30")},
        ),
    )
    assert result.estimated_cost == Decimal("1.11000000")
    assert any(item.meter_key == "minimum_duration_penalty" for item in result.breakdown)


def test_backblaze_egress_under_and_over_three_x_storage() -> None:
    b2 = profile("backblaze-b2", "standard")
    under = estimate_cost(
        b2,
        UsageInput(
            storage_byte_hours={"standard": Decimal("100") * GB * BILLING_MONTH_HOURS},
            egress_bytes=Decimal("250") * GB,
        ),
    )
    over = estimate_cost(
        b2,
        UsageInput(
            storage_byte_hours={"standard": Decimal("100") * GB * BILLING_MONTH_HOURS},
            egress_bytes=Decimal("350") * GB,
        ),
    )
    under_egress = next(item for item in under.breakdown if item.meter_key == "egress")
    over_egress = next(item for item in over.breakdown if item.meter_key == "egress")
    assert under_egress.cost == Decimal("0")
    assert over_egress.cost == Decimal("0.50")


def test_digitalocean_standard_base_subscription_and_overage() -> None:
    result = estimate_cost(
        profile("digitalocean-spaces", "standard"),
        UsageInput(
            storage_byte_hours={"standard": Decimal("300") * GIB * BILLING_MONTH_HOURS},
            egress_bytes=Decimal("1100") * GIB,
        ),
    )
    assert result.estimated_cost == Decimal("6.76000000")


def test_digitalocean_cold_128kib_min_retrieval() -> None:
    result = estimate_cost(
        profile("digitalocean-spaces", "cold"),
        UsageInput(retrieval_bytes=Decimal("1"), retrieval_object_count=Decimal("1")),
    )
    retrieval = next(item for item in result.breakdown if item.meter_key == "retrieval")
    assert retrieval.billable_quantity == Decimal("0.0001220703125")
    assert retrieval.cost == Decimal("0.000001220703125")


def test_storj_global_one_x_egress_included() -> None:
    result = estimate_cost(
        profile("storj", "global-collaboration"),
        UsageInput(
            storage_byte_hours={"global-collaboration": Decimal("1000") * GB * BILLING_MONTH_HOURS},
            egress_bytes=Decimal("1000") * GB,
        ),
    )
    egress = next(item for item in result.breakdown if item.meter_key == "egress")
    assert egress.cost == Decimal("0")
    assert result.estimated_cost == Decimal("15.00000000")


def test_storj_archive_minimum_duration_penalty() -> None:
    result = estimate_cost(
        profile("storj", "active-archive"),
        UsageInput(early_delete_gb_days={"active-archive": Decimal("30")}),
    )
    assert any(item.meter_key == "minimum_duration_penalty" for item in result.breakdown)
    assert result.estimated_cost == Decimal("5.00000000")


def test_wasabi_storage_only() -> None:
    result = estimate_cost(
        profile("wasabi", "pay-as-you-go"),
        UsageInput(storage_byte_hours={"pay-as-you-go": Decimal("2") * TB * BILLING_MONTH_HOURS}),
    )
    assert result.estimated_cost == Decimal("13.98000000")


def test_aws_s3_basic_standard_request_storage_fixture() -> None:
    result = estimate_cost(
        profile("aws-s3", "standard", "us-east-1"),
        UsageInput(
            storage_byte_hours={"standard": Decimal("100") * GB * BILLING_MONTH_HOURS},
            provider_operation_counts={"PutObject": Decimal("1000"), "GetObject": Decimal("1000")},
        ),
    )
    assert result.estimated_cost == Decimal("2.30540000")


def test_aws_ia_min_object_and_min_duration_fixture() -> None:
    result = estimate_cost(
        profile("aws-s3", "standard-ia", "us-east-1"),
        UsageInput(
            storage_byte_hours={"standard-ia": Decimal("1")},
            object_count_hours={"standard-ia": Decimal("730")},
            early_delete_gb_days={"standard-ia": Decimal("1")},
        ),
    )
    storage = next(item for item in result.breakdown if item.meter_key == "storage")
    assert storage.billable_quantity == Decimal("0.000131072")
    assert any(item.meter_key == "minimum_duration_penalty" for item in result.breakdown)


def test_estimate_from_fixture_files() -> None:
    result = estimate_from_files(
        Path("tests/fixtures/profiles/r2-standard.json"),
        Path("tests/fixtures/usage/r2-standard-free-tier.json"),
    )
    assert result.estimated_cost == Decimal("0E-8")
