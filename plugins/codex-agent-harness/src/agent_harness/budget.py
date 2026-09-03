"""Review-budget contracts and completion status."""

from __future__ import annotations

from typing import Any, Mapping

from .util import InputError, require_string, sanitize_text


DEFAULT_EXPECTED_MIN = 0
DEFAULT_EXPECTED_MAX = 700
DEFAULT_MAX_PRODUCTION_LINES = 700
MAX_EXPECTED_PRODUCTION_LINES = 10_000_000
REVIEW_BUDGET_MODES = {
    "advisory",
    "enforced",
    "report_only",
    "legacy_report_only",
}


def _non_negative_integer(value: Any, name: str, *, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > maximum
    ):
        raise InputError(f"{name} must be an integer between 0 and {maximum}")
    return value


def normalize_review_budget(
    value: Any,
) -> dict[str, Any]:
    """Validate and freeze a review budget for a newly created contract."""

    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise InputError("review_budget must be an object")
    unknown = sorted(
        set(value)
        - {
            "expected_production_lines",
            "max_production_lines",
            "exception_reason",
        }
    )
    if unknown:
        raise InputError(
            "review_budget contains unsupported fields: " + ", ".join(unknown)
        )

    production_limit = value.get(
        "max_production_lines", DEFAULT_MAX_PRODUCTION_LINES
    )
    if (
        not isinstance(production_limit, int)
        or isinstance(production_limit, bool)
        or not 1 <= production_limit <= DEFAULT_MAX_PRODUCTION_LINES
    ):
        raise InputError(
            "review_budget.max_production_lines must be between 1 and 700"
        )

    expected = value.get("expected_production_lines", {})
    if not isinstance(expected, Mapping):
        raise InputError("review_budget.expected_production_lines must be an object")
    expected_unknown = sorted(set(expected) - {"min", "max"})
    if expected_unknown:
        raise InputError(
            "review_budget.expected_production_lines contains unsupported fields: "
            + ", ".join(expected_unknown)
        )
    minimum = _non_negative_integer(
        expected.get("min", DEFAULT_EXPECTED_MIN),
        "review_budget.expected_production_lines.min",
        maximum=MAX_EXPECTED_PRODUCTION_LINES,
    )
    maximum = _non_negative_integer(
        expected.get("max", min(DEFAULT_EXPECTED_MAX, production_limit)),
        "review_budget.expected_production_lines.max",
        maximum=MAX_EXPECTED_PRODUCTION_LINES,
    )
    if minimum > maximum:
        raise InputError(
            "review_budget.expected_production_lines.min must not exceed max"
        )

    raw_exception = value.get("exception_reason")
    exception_reason = None
    if raw_exception is not None:
        exception_reason = sanitize_text(
            require_string(
                raw_exception,
                "review_budget.exception_reason",
                maximum=2_000,
            ),
            maximum=2_000,
        )
    if maximum > production_limit and exception_reason is None:
        raise InputError(
            "an expected diff above max_production_lines requires exception_reason"
        )

    return {
        "expected_production_lines": {"min": minimum, "max": maximum},
        "max_production_lines": production_limit,
        "exception_reason": exception_reason,
    }


def validate_review_budget_mode(value: Any) -> str:
    mode = value if value is not None else "advisory"
    if mode not in REVIEW_BUDGET_MODES:
        raise InputError(
            "review_budget_mode must be advisory, enforced, report_only, or "
            "legacy_report_only"
        )
    return str(mode)


def effective_review_budget(contract: Mapping[str, Any]) -> dict[str, Any]:
    value = contract.get("review_budget")
    if isinstance(value, Mapping):
        return {
            "expected_production_lines": dict(
                value.get(
                    "expected_production_lines",
                    {"min": DEFAULT_EXPECTED_MIN, "max": DEFAULT_EXPECTED_MAX},
                )
            ),
            "max_production_lines": value.get(
                "max_production_lines", DEFAULT_MAX_PRODUCTION_LINES
            ),
            "exception_reason": value.get("exception_reason"),
        }
    return normalize_review_budget(None)


def review_budget_status(
    contract: Mapping[str, Any],
    diff_stats: Mapping[str, Any],
) -> str:
    """Return the gate status for one current diff."""

    if "review_budget" not in contract:
        return "legacy_report_only"
    mode = contract.get("review_budget_mode", "advisory")
    if mode == "legacy_report_only":
        return "legacy_report_only"
    if mode == "report_only":
        return "report_only"
    budget = effective_review_budget(contract)
    production = diff_stats.get("production", {})
    total = production.get("total", 0) if isinstance(production, Mapping) else 0
    if not isinstance(total, int):
        raise InputError("diff_stats.production.total must be an integer")
    if total <= int(budget["max_production_lines"]):
        return "within_budget"
    if budget.get("exception_reason"):
        return "approved_exception"
    return "over_soft_limit"
