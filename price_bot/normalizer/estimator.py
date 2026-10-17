from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from price_bot.normalizer.models import (
    EstimateBreakdown,
    EstimateOptions,
    EstimateResult,
    Meter,
    RatingProfile,
    UsageInput,
)
from price_bot.normalizer.units import (
    BILLING_MONTH_DAYS,
    BILLING_MONTH_HOURS,
    GIB,
    apply_rounding,
    byte_hours_to_gb_month,
    normalize_quantity,
)

FREE_TIER_POLICY_APPLY = "apply_free_tiers"
FREE_TIER_POLICY_IGNORE_ACCOUNT_WIDE = "ignore_account_wide_free_tiers"
CONSERVATIVE_FREE_TIER_UNKNOWN = (
    "account-level free tiers intentionally ignored for budget-safe estimate"
)


def estimate_from_files(
    profile_path: Path,
    usage_path: Path,
    options: EstimateOptions | None = None,
) -> EstimateResult:
    profile = RatingProfile.model_validate(json.loads(profile_path.read_text()))
    usage = UsageInput.model_validate(json.loads(usage_path.read_text()))
    return estimate_cost(profile, usage, options)


def estimate_cost(
    profile: RatingProfile,
    usage: UsageInput,
    options: EstimateOptions | None = None,
) -> EstimateResult:
    breakdown: list[EstimateBreakdown] = []
    free_tier_applied: dict[str, Decimal] = {}
    rounding_applied: dict[str, str] = {}
    unknowns = list(profile.confidence.unknowns)
    estimate_mode, apply_free_tiers, free_tier_policy = _resolve_estimate_options(options)
    if not apply_free_tiers:
        unknowns.append(CONSERVATIVE_FREE_TIER_UNKNOWN)

    storage_gb_month_for_egress = Decimal("0")

    for meter in profile.meters.values():
        if not meter.tiers:
            continue
        quantity = _quantity_for_meter(profile, meter, usage)
        if meter.meter_type == "storage_byte_hours":
            storage_gb_month_for_egress += _storage_gb_month(profile, usage)
        if quantity == 0 and meter.meter_type not in {"fixed_monthly", "minimum_monthly"}:
            continue

        notes: list[str] = []
        quantity = _apply_meter_minimums(profile, meter, quantity, usage, notes)
        billable = _apply_egress_inclusion(meter, quantity, storage_gb_month_for_egress, notes)
        if apply_free_tiers and meter.free_tier_amount is not None:
            before = billable
            billable = max(Decimal("0"), billable - meter.free_tier_amount)
            applied = before - billable
            if applied > 0:
                free_tier_applied[meter.meter_key] = applied
                notes.append(f"free tier applied: {applied} {meter.free_tier_unit or meter.billing_unit}")
        elif not apply_free_tiers and meter.free_tier_amount is not None:
            notes.append(
                "free tier ignored for budget safety: "
                f"{meter.free_tier_amount} {meter.free_tier_unit or meter.billing_unit}"
            )

        rounded = apply_rounding(billable, meter.rounding_rule)
        if rounded != billable:
            rounding_applied[meter.meter_key] = meter.rounding_rule
            notes.append(f"rounded from {billable} to {rounded}")
        billable = rounded

        tier = meter.tiers[0]
        rate_quantity = _rate_quantity(billable, tier.rate_unit)
        cost = rate_quantity * tier.rate
        breakdown.append(
            EstimateBreakdown(
                meter_key=meter.meter_key,
                quantity=quantity,
                billable_quantity=billable,
                unit=meter.billing_unit or meter.unit,
                rate=tier.rate,
                rate_unit=tier.rate_unit,
                cost=cost,
                notes=notes,
            )
        )

    operation_totals = _operation_meter_quantities(profile, usage)
    for meter_key, quantity in operation_totals.items():
        meter = profile.meters.get(meter_key)
        if meter is None or not meter.tiers:
            unknowns.append(f"operation meter {meter_key} not present in profile")
            continue
        if quantity == 0:
            continue
        notes = []
        billable = quantity
        if apply_free_tiers and meter.free_tier_amount is not None:
            free_amount = _free_tier_operations(meter)
            before = billable
            billable = max(Decimal("0"), billable - free_amount)
            applied = before - billable
            if applied > 0:
                free_tier_applied[meter_key] = free_tier_applied.get(meter_key, Decimal("0")) + applied
                notes.append(f"free tier applied: {applied} operations")
        elif not apply_free_tiers and meter.free_tier_amount is not None:
            notes.append(
                "free tier ignored for budget safety: "
                f"{_free_tier_operations(meter)} operations"
            )
        tier = meter.tiers[0]
        normalized = _rate_quantity(billable, tier.rate_unit)
        rounded = apply_rounding(normalized, meter.rounding_rule)
        if rounded != normalized:
            rounding_applied[meter_key] = meter.rounding_rule
            notes.append(f"rounded from {normalized} to {rounded}")
        cost = rounded * tier.rate
        breakdown.append(
            EstimateBreakdown(
                meter_key=meter_key,
                quantity=quantity,
                billable_quantity=rounded,
                unit=tier.rate_unit,
                rate=tier.rate,
                rate_unit=tier.rate_unit,
                cost=cost,
                notes=notes,
            )
        )

    _append_early_delete_penalties(profile, usage, breakdown)
    _append_noncurrent_storage(profile, usage, breakdown, unknowns)

    total = sum((item.cost for item in breakdown), Decimal("0"))
    minimum_monthly = profile.provenance.get("minimum_monthly_usd")
    if minimum_monthly is not None and total < Decimal(str(minimum_monthly)):
        delta = Decimal(str(minimum_monthly)) - total
        breakdown.append(
            EstimateBreakdown(
                meter_key="minimum_monthly",
                quantity=Decimal("1"),
                billable_quantity=Decimal("1"),
                unit="month",
                rate=delta,
                rate_unit="month",
                cost=delta,
                notes=[f"minimum monthly charge enforced at {minimum_monthly}"],
            )
        )
        total = Decimal(str(minimum_monthly))

    if not profile.meters:
        unknowns.append("profile has no deterministic meters; operator manual pricing required")

    return EstimateResult(
        estimated_cost=total.quantize(Decimal("0.00000001")),
        currency=profile.scope.currency,
        estimate_mode=estimate_mode,
        free_tier_policy=free_tier_policy,
        free_tiers_applied=bool(free_tier_applied),
        breakdown=breakdown,
        free_tier_applied=free_tier_applied,
        rounding_applied=rounding_applied,
        confidence=profile.confidence,
        unknowns=sorted(set(unknowns)),
    )


def _resolve_estimate_options(options: EstimateOptions | None) -> tuple[str, bool, str]:
    resolved = options or EstimateOptions()
    budget_conservative = (
        resolved.mode == "budget_conservative" or resolved.apply_free_tiers is False
    )
    if budget_conservative:
        return "budget_conservative", False, FREE_TIER_POLICY_IGNORE_ACCOUNT_WIDE
    return "reporting", True, FREE_TIER_POLICY_APPLY


def _quantity_for_meter(profile: RatingProfile, meter: Meter, usage: UsageInput) -> Decimal:
    match meter.meter_type:
        case "fixed_monthly":
            return Decimal("1")
        case "storage_byte_hours":
            byte_hours = usage.storage_byte_hours.get(
                profile.scope.storage_class,
                usage.storage_byte_hours.get("default", Decimal("0")),
            )
            return normalize_quantity(byte_hours, meter.billing_unit)
        case "retrieval_bytes":
            return normalize_quantity(usage.retrieval_bytes, meter.billing_unit)
        case "egress_bytes":
            egress = usage.egress_bytes if usage.egress_bytes else usage.downloaded_bytes
            return normalize_quantity(egress, meter.billing_unit)
        case "downloaded_bytes":
            return normalize_quantity(usage.downloaded_bytes, meter.billing_unit)
        case "uploaded_bytes":
            return normalize_quantity(usage.uploaded_bytes, meter.billing_unit)
        case "object_count_hours":
            hours = usage.object_count_hours.get(
                profile.scope.storage_class,
                usage.object_count_hours.get("default", Decimal("0")),
            )
            if meter.billing_unit == "segment_month":
                return hours / BILLING_MONTH_HOURS
            return hours
        case _:
            return Decimal("0")


def _storage_gb_month(profile: RatingProfile, usage: UsageInput) -> Decimal:
    byte_hours = usage.storage_byte_hours.get(
        profile.scope.storage_class,
        usage.storage_byte_hours.get("default", Decimal("0")),
    )
    return byte_hours_to_gb_month(byte_hours)


def _apply_meter_minimums(
    profile: RatingProfile,
    meter: Meter,
    quantity: Decimal,
    usage: UsageInput,
    notes: list[str],
) -> Decimal:
    if meter.meter_type == "storage_byte_hours":
        for rule in profile.storage_rules:
            if rule.minimum_billable_object_size_bytes and usage.object_count_hours:
                object_hours = usage.object_count_hours.get(
                    profile.scope.storage_class,
                    usage.object_count_hours.get("default", Decimal("0")),
                )
                min_byte_hours = object_hours * Decimal(rule.minimum_billable_object_size_bytes)
                min_quantity = normalize_quantity(min_byte_hours, meter.billing_unit)
                if min_quantity > quantity:
                    notes.append(
                        f"minimum billable object size applied: {rule.minimum_billable_object_size_bytes} bytes"
                    )
                    quantity = min_quantity
    if meter.meter_type == "retrieval_bytes":
        minimum = meter.rules.get("minimum_retrieval_object_size_bytes")
        if minimum and usage.retrieval_object_count:
            min_bytes = Decimal(str(minimum)) * usage.retrieval_object_count
            min_quantity = normalize_quantity(min_bytes, meter.billing_unit)
            if min_quantity > quantity:
                notes.append(f"minimum retrieval object size applied: {minimum} bytes")
                quantity = min_quantity
    return quantity


def _apply_egress_inclusion(
    meter: Meter,
    quantity: Decimal,
    storage_gb_month_for_egress: Decimal,
    notes: list[str],
) -> Decimal:
    multiple = meter.rules.get("included_egress_multiple_of_storage")
    if meter.meter_type != "egress_bytes" or multiple is None:
        return quantity
    included = storage_gb_month_for_egress * Decimal(str(multiple))
    billable = max(Decimal("0"), quantity - included)
    if included > 0:
        notes.append(f"included egress applied: {included} GB")
    return billable


def _operation_meter_quantities(
    profile: RatingProfile, usage: UsageInput
) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for operation, count in usage.provider_operation_counts.items():
        mapping = profile.operation_map.get(operation)
        if mapping is None:
            continue
        totals[mapping.meter_key] = totals.get(mapping.meter_key, Decimal("0")) + (
            count * mapping.multiplier
        )
    return totals


def _free_tier_operations(meter: Meter) -> Decimal:
    if meter.free_tier_amount is None:
        return Decimal("0")
    if meter.free_tier_unit == "million_operations":
        return meter.free_tier_amount * Decimal("1000000")
    if meter.free_tier_unit == "request_1000":
        return meter.free_tier_amount * Decimal("1000")
    return meter.free_tier_amount


def _rate_quantity(quantity: Decimal, rate_unit: str) -> Decimal:
    match rate_unit:
        case "request_1000":
            return quantity / Decimal("1000")
        case "request_10000":
            return quantity / Decimal("10000")
        case "million_operations":
            return quantity / Decimal("1000000")
        case _:
            return quantity


def _append_noncurrent_storage(
    profile: RatingProfile,
    usage: UsageInput,
    breakdown: list[EstimateBreakdown],
    unknowns: list[str],
) -> None:
    """Price noncurrent object versions at the class storage rate (first tier, no free tier).

    Only runs when the caller sends ``noncurrent_storage_byte_hours``; inputs without it are
    estimated exactly as before.
    """
    if not usage.noncurrent_storage_byte_hours:
        return
    byte_hours = usage.noncurrent_storage_byte_hours.get(
        profile.scope.storage_class,
        usage.noncurrent_storage_byte_hours.get("default", Decimal("0")),
    )
    if byte_hours == 0:
        return
    versioning = profile.versioning
    if versioning is None or not versioning.supported:
        status = versioning.status if versioning is not None else "absent"
        unknowns.append(
            f"noncurrent version storage reported but profile versioning status is {status}; "
            "not priced"
        )
        return
    if versioning.noncurrent_storage_billing != "storage_class_rate":
        unknowns.append(
            "noncurrent version storage billing "
            f"{versioning.noncurrent_storage_billing!r} not understood; not priced"
        )
        return
    storage_meter = profile.meters.get("storage")
    if storage_meter is None or not storage_meter.tiers:
        unknowns.append("noncurrent version storage reported but profile has no storage meter")
        return
    quantity = normalize_quantity(byte_hours, storage_meter.billing_unit)
    tier = storage_meter.tiers[0]
    breakdown.append(
        EstimateBreakdown(
            meter_key="noncurrent_storage",
            quantity=quantity,
            billable_quantity=quantity,
            unit=storage_meter.billing_unit or storage_meter.unit,
            rate=tier.rate,
            rate_unit=tier.rate_unit,
            cost=_rate_quantity(quantity, tier.rate_unit) * tier.rate,
            notes=["noncurrent object versions billed at the storage class rate"],
        )
    )


def _append_early_delete_penalties(
    profile: RatingProfile, usage: UsageInput, breakdown: list[EstimateBreakdown]
) -> None:
    if not usage.early_delete_gb_days:
        return
    storage_meter = profile.meters.get("storage")
    if storage_meter is None or not storage_meter.tiers:
        return
    gb_days = usage.early_delete_gb_days.get(
        profile.scope.storage_class,
        usage.early_delete_gb_days.get("default", Decimal("0")),
    )
    if gb_days == 0:
        return
    rate = storage_meter.tiers[0].rate
    if storage_meter.tiers[0].rate_unit in {"tb_month", "tib_month"}:
        rate = rate / Decimal("1000")
    if storage_meter.tiers[0].rate_unit == "gib_month":
        quantity = (gb_days * Decimal("1000000000") / GIB) / BILLING_MONTH_DAYS
        unit = "gib_month"
    else:
        quantity = gb_days / BILLING_MONTH_DAYS
        unit = "gb_month"
    cost = quantity * rate
    breakdown.append(
        EstimateBreakdown(
            meter_key="minimum_duration_penalty",
            quantity=gb_days,
            billable_quantity=quantity,
            unit=unit,
            rate=rate,
            rate_unit=storage_meter.tiers[0].rate_unit,
            cost=cost,
            notes=["early delete gb-days converted to remaining storage charge"],
        )
    )
