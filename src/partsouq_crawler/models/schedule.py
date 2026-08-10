from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MonthlyRunLease:
    run_id: int
    period_key: str
    owner_id: str | None
    fencing_token: int
    attempts: int
    max_attempts: int
    status: str
    acquired: bool
    nhtsa_bulk_status: str
    nhtsa_api_status: str
    partsouq_status: str
