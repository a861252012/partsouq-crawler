from __future__ import annotations

import re
from dataclasses import dataclass

UNAMBIGUOUS_RANGES = (
    re.compile(r"^(\d{4}-\d{2})(?:-\d{2})?\s*(?:～|~|—|–|\s+-\s+)\s*(\d{4}-\d{2})(?:-\d{2})?$"),
    re.compile(r"^(\d{4}-\d{2})(?:-\d{2})?\s*(?:～|~|—|–|\s+-\s*)$"),
)


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
    match = UNAMBIGUOUS_RANGES[0].match(value)
    if match:
        return DateRange(match.group(1), match.group(2), "month", 1.0, "generic")
    match = UNAMBIGUOUS_RANGES[1].match(value)
    if match:
        return DateRange(match.group(1), None, "month", 1.0, "generic")
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return DateRange(value, value, "month", 1.0, "generic")
    return DateRange(None, None, "unknown", 0.0, "generic")


def is_assembly_name(name: str | None) -> tuple[bool, str | None]:
    if name and re.search(r"\b(?:ASSY|ASSEMBLY|SUB-ASSY)\b", name, re.IGNORECASE):
        return True, "name_keyword"
    return False, None
