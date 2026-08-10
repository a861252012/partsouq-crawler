from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

from partsouq_crawler.crawl.retries import rate_limit_delay, retry_delay
from partsouq_crawler.parsers.common import (
    is_assembly_name,
    normalize_part_number,
    parse_unambiguous_range,
)


def test_retry_after_seconds() -> None:
    assert retry_delay(1, "7") == 7


def test_retry_after_http_date() -> None:
    future = datetime.now(UTC) + timedelta(seconds=10)
    assert 8 <= retry_delay(1, format_datetime(future)) <= 10


def test_rate_limit_uses_six_hour_minimum_and_honors_longer_retry_after() -> None:
    assert rate_limit_delay(1, "0") == 6 * 60 * 60
    assert rate_limit_delay(1, "86400") == 86400


def test_unambiguous_month_range() -> None:
    result = parse_unambiguous_range("2006-05 ～ 2011-12")
    assert (result.start, result.end, result.precision) == ("2006-05", "2011-12", "month")


def test_part_souq_day_range_is_normalized() -> None:
    result = parse_unambiguous_range("15.01.2002 - 24.11.2003")
    assert (result.start, result.end, result.precision) == (
        "2002-01-15",
        "2003-11-24",
        "day",
    )


def test_compact_year_range_is_normalized_without_confusing_year_month() -> None:
    years = parse_unambiguous_range("1998-2000")
    month = parse_unambiguous_range("2024-05")

    assert (years.start, years.end, years.precision) == ("1998", "2000", "year")
    assert (month.start, month.end, month.precision) == ("2024-05", "2024-05", "month")


def test_invalid_or_reversed_range_is_not_accepted() -> None:
    assert parse_unambiguous_range("31.02.2002 - 24.11.2003").start is None
    assert parse_unambiguous_range("2024-05 - 2024-01").start is None
    assert parse_unambiguous_range("2024-2020").start is None


def test_ambiguous_short_range_is_not_guessed() -> None:
    result = parse_unambiguous_range("0605-")
    assert result.start is None and result.end is None and result.precision == "unknown"


def test_part_number_normalization_preserves_raw_leading_zero() -> None:
    raw = "00123-AB"
    assert raw == "00123-AB"
    assert normalize_part_number(raw) == "00123AB"


def test_assembly_inference_is_explicit() -> None:
    assert is_assembly_name("PUMP ASSY") == (True, "name_keyword")
    assert is_assembly_name("PUMP") == (False, None)
