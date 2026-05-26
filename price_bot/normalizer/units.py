from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

GB = Decimal("1000000000")
GIB = Decimal("1073741824")
TB = Decimal("1000000000000")
TIB = Decimal("1099511627776")
BILLING_MONTH_HOURS = Decimal("730")
BILLING_MONTH_DAYS = Decimal("30")


def ceil_decimal(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_CEILING)


def bytes_to_gb(value: Decimal) -> Decimal:
    return value / GB


def bytes_to_gib(value: Decimal) -> Decimal:
    return value / GIB


def byte_hours_to_gb_month(value: Decimal) -> Decimal:
    return value / GB / BILLING_MONTH_HOURS


def byte_hours_to_gib_month(value: Decimal) -> Decimal:
    return value / GIB / BILLING_MONTH_HOURS


def byte_hours_to_tb_month(value: Decimal) -> Decimal:
    return value / TB / BILLING_MONTH_HOURS


def byte_hours_to_tib_month(value: Decimal) -> Decimal:
    return value / TIB / BILLING_MONTH_HOURS


def normalize_quantity(value: Decimal, billing_unit: str | None) -> Decimal:
    match billing_unit:
        case "gb" | "gb_month":
            return bytes_to_gb(value) if billing_unit == "gb" else byte_hours_to_gb_month(value)
        case "gib" | "gib_month":
            return bytes_to_gib(value) if billing_unit == "gib" else byte_hours_to_gib_month(value)
        case "tb_month":
            return byte_hours_to_tb_month(value)
        case "tib_month":
            return byte_hours_to_tib_month(value)
        case _:
            return value


def apply_rounding(value: Decimal, rounding_rule: str) -> Decimal:
    if rounding_rule in {
        "ceil_to_billing_unit",
        "ceil_to_gb",
        "ceil_to_gib",
        "ceil_to_tb",
        "ceil_to_provider_unit",
    }:
        return ceil_decimal(value)
    return value
