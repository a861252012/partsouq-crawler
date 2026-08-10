from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

DATE_TOKEN = (
    r"(?:\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}|"
    r"\d{2}\.\d{4}|\d{6}|\d{4})"
)
RANGE_SEPARATOR = r"(?:～|~|—|–|\s+-\s+)"
COMPACT_YEAR_RANGE = re.compile(r"^(?P<start>\d{4})-(?P<end>\d{4})$")
FULL_RANGE = re.compile(rf"^(?P<start>{DATE_TOKEN})\s*{RANGE_SEPARATOR}\s*(?P<end>{DATE_TOKEN})$")
OPEN_RANGE = re.compile(rf"^(?P<start>{DATE_TOKEN})\s*{RANGE_SEPARATOR}\s*$")
SINGLE_DATE = re.compile(rf"^(?P<date>{DATE_TOKEN})$")


@dataclass(frozen=True, slots=True)
class DateRange:
    start: str | None
    end: str | None
    precision: str
    confidence: float
    parser: str


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def normalize_part_number(value: str) -> str:
    return re.sub(r"[\s-]+", "", value).upper()


def parse_unambiguous_range(raw: str | None) -> DateRange:
    if not raw:
        return DateRange(None, None, "unknown", 0.0, "generic")
    value = clean_text(raw) or ""
    compact_years = COMPACT_YEAR_RANGE.match(value)
    if compact_years:
        start = _normalize_date_token(compact_years.group("start"))
        end = _normalize_date_token(compact_years.group("end"))
        if start and end and start[0] <= end[0]:
            return DateRange(start[0], end[0], "year", 1.0, "generic")
        return DateRange(None, None, "unknown", 0.0, "generic")
    match = FULL_RANGE.match(value)
    if match:
        start = _normalize_date_token(match.group("start"))
        end = _normalize_date_token(match.group("end"))
        if start and end and start[1] == end[1] and start[0] <= end[0]:
            return DateRange(start[0], end[0], start[1], 1.0, "generic")
        return DateRange(None, None, "unknown", 0.0, "generic")
    match = OPEN_RANGE.match(value)
    if match:
        start = _normalize_date_token(match.group("start"))
        if start:
            return DateRange(start[0], None, start[1], 1.0, "generic")
    match = SINGLE_DATE.match(value)
    if match:
        date = _normalize_date_token(match.group("date"))
        if date:
            return DateRange(date[0], date[0], date[1], 1.0, "generic")
    return DateRange(None, None, "unknown", 0.0, "generic")


def _normalize_date_token(value: str) -> tuple[str, str] | None:
    formats = (
        (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d", "%Y-%m-%d", "day"),
        (r"\d{2}\.\d{2}\.\d{4}", "%d.%m.%Y", "%Y-%m-%d", "day"),
        (r"\d{4}-\d{2}", "%Y-%m", "%Y-%m", "month"),
        (r"\d{2}\.\d{4}", "%m.%Y", "%Y-%m", "month"),
        (r"\d{6}", "%Y%m", "%Y-%m", "month"),
        (r"\d{4}", "%Y", "%Y", "year"),
    )
    for pattern, input_format, output_format, precision in formats:
        if not re.fullmatch(pattern, value):
            continue
        try:
            normalized = datetime.strptime(value, input_format).strftime(output_format)
        except ValueError:
            return None
        return normalized, precision
    return None


def is_assembly_name(name: str | None) -> tuple[bool, str | None]:
    if name and re.search(r"\b(?:ASSY|ASSEMBLY|SUB-ASSY)\b", name, re.IGNORECASE):
        return True, "name_keyword"
    return False, None
