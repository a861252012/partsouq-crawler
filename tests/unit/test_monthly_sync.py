from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from typing import cast

from partsouq_crawler.cli import _monthly_commands, _monthly_period, build_parser
from partsouq_crawler.config import PartSouqMySQLConfig
from partsouq_crawler.db.repository import Repository
from partsouq_crawler.models.schedule import MonthlyRunLease
from partsouq_crawler.services.monthly_sync import MonthlySourceCommand, MonthlySyncService


class FakeMonthlyRepository:
    def __init__(self, lease: MonthlyRunLease) -> None:
        self.lease = lease
        self.events: list[dict[str, object]] = []
        self.source_updates: list[tuple[str, str, str]] = []
        self.requeued: list[tuple[str, tuple[str, ...]]] = []
        self.finished: dict[str, object] | None = None
        self.partsouq_status = "completed"
        self.fail_child_events = False

    async def acquire_monthly_run(self, **_kwargs: object) -> MonthlyRunLease:
        return self.lease

    async def heartbeat_monthly_run(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def update_monthly_source(
        self,
        _run_id: int,
        *,
        source_name: str,
        status: str,
        run_key: str,
        **_kwargs: object,
    ) -> None:
        self.source_updates.append((source_name, status, run_key))

    async def append_monthly_events(
        self,
        _run_id: int,
        _fencing_token: int,
        events: Sequence[Mapping[str, object]],
    ) -> None:
        if self.fail_child_events and any(
            event.get("event_type") == "child_output" for event in events
        ):
            raise RuntimeError("simulated monthly event storage failure")
        self.events.extend(dict(event) for event in events)

    async def finish_monthly_run(
        self,
        _run_id: int,
        *,
        status: str,
        summary: Mapping[str, object],
        error: str | None = None,
        **_kwargs: object,
    ) -> None:
        self.finished = {"status": status, "summary": dict(summary), "error": error}

    async def get_run(self, _run_key: str) -> dict[str, object]:
        return {"status": self.partsouq_status, "blocked_reason": None}

    async def requeue_problems(self, run_key: str, statuses: Sequence[str]) -> int:
        self.requeued.append((run_key, tuple(statuses)))
        return 1


def _lease(
    *, acquired: bool = True, attempts: int = 1, partsouq_status: str = "pending"
) -> MonthlyRunLease:
    return MonthlyRunLease(
        run_id=11,
        period_key="2099-01",
        owner_id="worker",
        fencing_token=4,
        attempts=attempts,
        max_attempts=3,
        status="running" if acquired else "completed",
        acquired=acquired,
        nhtsa_bulk_status="pending",
        nhtsa_api_status="pending",
        station_status="pending",
        partsouq_status=partsouq_status,
    )


def _command(source_name: str, run_key: str, *, exit_code: int = 0) -> MonthlySourceCommand:
    payload = json.dumps({"event": "progress", "source": source_name})
    return MonthlySourceCommand(
        source_name=source_name,
        run_key=run_key,
        command=(sys.executable, "-c", f"print({payload!r}); raise SystemExit({exit_code})"),
        environment={"PYTHONUNBUFFERED": "1"},
    )


def test_monthly_sync_captures_child_logs_and_completes() -> None:
    import asyncio

    async def scenario() -> None:
        fake = FakeMonthlyRepository(_lease())
        service = MonthlySyncService(
            cast(Repository, fake),
            lease_seconds=30,
            heartbeat_seconds=1,
            owner_id="worker",
        )
        report = await service.run(
            period_key="2099-01",
            scheduled_for="2098-12-31T17:00:00+00:00",
            commands=(
                _command("nhtsa_bulk", "bulk"),
                _command("nhtsa_api", "api"),
                _command("station", "station"),
                _command("partsouq", "partsouq"),
            ),
        )

        assert report["status"] == "completed"
        assert report["exit_code"] == 0
        assert fake.finished is not None and fake.finished["status"] == "completed"
        assert sum(event["event_type"] == "progress" for event in fake.events) == 4
        assert {update[:2] for update in fake.source_updates} >= {
            ("nhtsa_bulk", "completed"),
            ("nhtsa_api", "completed"),
            ("station", "completed"),
            ("partsouq", "completed"),
        }

    asyncio.run(scenario())


def test_monthly_sync_records_spawn_failure_instead_of_losing_the_run() -> None:
    import asyncio

    async def scenario() -> None:
        fake = FakeMonthlyRepository(_lease())
        missing = MonthlySourceCommand(
            source_name="nhtsa_bulk",
            run_key="bulk",
            command=("/definitely/missing/monthly-command",),
            environment={},
        )
        report = await MonthlySyncService(
            cast(Repository, fake),
            lease_seconds=30,
            heartbeat_seconds=1,
            owner_id="worker",
        ).run(
            period_key="2099-01",
            scheduled_for="2098-12-31T17:00:00+00:00",
            commands=(
                missing,
                _command("nhtsa_api", "api"),
                _command("station", "station"),
                _command("partsouq", "partsouq"),
            ),
        )

        assert report["status"] == "failed"
        assert report["exit_code"] == 1
        assert fake.finished is not None and fake.finished["status"] == "failed"
        assert any(event["event_type"] == "monthly_run_failed" for event in fake.events)

    asyncio.run(scenario())


def test_monthly_sync_seals_blocked_source_without_another_canary() -> None:
    import asyncio

    async def run_attempt(attempts: int) -> tuple[dict[str, object], FakeMonthlyRepository]:
        fake = FakeMonthlyRepository(
            _lease(
                attempts=attempts,
                partsouq_status="blocked" if attempts > 1 else "pending",
            )
        )
        fake.partsouq_status = "blocked"
        report = await MonthlySyncService(
            cast(Repository, fake),
            lease_seconds=30,
            heartbeat_seconds=1,
            max_attempts=3,
            owner_id="worker",
        ).run(
            period_key="2099-01",
            scheduled_for="2098-12-31T17:00:00+00:00",
            commands=(
                _command("nhtsa_bulk", "bulk"),
                _command("nhtsa_api", "api"),
                _command("station", "station"),
                _command("partsouq", "partsouq", exit_code=2),
            ),
        )
        return report, fake

    first, first_repo = asyncio.run(run_attempt(1))
    resumed, resumed_repo = asyncio.run(run_attempt(2))

    assert first["status"] == "completed_with_gaps"
    assert first["exit_code"] == 2
    assert first_repo.finished is not None
    assert first_repo.finished["status"] == "completed_with_gaps"
    assert ("partsouq", "blocked", "partsouq") in first_repo.source_updates
    assert not any(
        event.get("event_type") == "progress" and event.get("source_name") == "partsouq"
        for event in first_repo.events
    )
    assert resumed["status"] == "completed_with_gaps"
    assert resumed["exit_code"] == 2
    assert resumed_repo.finished is not None
    assert resumed_repo.finished["status"] == "completed_with_gaps"
    assert first_repo.requeued == []
    assert resumed_repo.requeued == []
    assert any(event["event_type"] == "source_skipped_blocked" for event in resumed_repo.events)
    assert not any(update[0] == "partsouq" for update in resumed_repo.source_updates)


def test_monthly_sync_does_not_start_when_period_is_already_owned() -> None:
    import asyncio

    async def scenario() -> None:
        fake = FakeMonthlyRepository(_lease(acquired=False))
        report = await MonthlySyncService(
            cast(Repository, fake),
            lease_seconds=30,
            heartbeat_seconds=1,
        ).run(
            period_key="2099-01",
            scheduled_for="2098-12-31T17:00:00+00:00",
            commands=(
                _command("nhtsa_bulk", "bulk"),
                _command("nhtsa_api", "api"),
                _command("station", "station"),
                _command("partsouq", "partsouq"),
            ),
        )

        assert report == {
            "run_id": 11,
            "period": "2099-01",
            "status": "completed",
            "acquired": False,
            "attempts": 1,
            "max_attempts": 3,
            "exit_code": 0,
        }
        assert not fake.events

    asyncio.run(scenario())


def test_monthly_sync_terminates_active_child_when_stop_is_requested() -> None:
    import asyncio

    async def scenario() -> None:
        fake = FakeMonthlyRepository(_lease())
        service = MonthlySyncService(
            cast(Repository, fake),
            lease_seconds=30,
            heartbeat_seconds=1,
            owner_id="worker",
        )
        slow = MonthlySourceCommand(
            source_name="nhtsa_bulk",
            run_key="slow",
            command=(
                sys.executable,
                "-c",
                "import time; print('started', flush=True); time.sleep(60)",
            ),
            environment={"PYTHONUNBUFFERED": "1"},
        )
        task = asyncio.create_task(
            service.run(
                period_key="2099-01",
                scheduled_for="2098-12-31T17:00:00+00:00",
                commands=(
                    slow,
                    _command("nhtsa_api", "api"),
                    _command("station", "station"),
                    _command("partsouq", "partsouq"),
                ),
            )
        )
        await asyncio.sleep(0.2)
        service.stop_event.set()
        report = await asyncio.wait_for(task, timeout=5)

        assert report["status"] == "interrupted"
        assert report["exit_code"] == 1
        assert ("nhtsa_bulk", "interrupted", "slow") in fake.source_updates

    asyncio.run(scenario())


def test_monthly_sync_terminates_child_when_log_persistence_fails() -> None:
    import asyncio

    async def scenario() -> None:
        fake = FakeMonthlyRepository(_lease())
        fake.fail_child_events = True
        service = MonthlySyncService(
            cast(Repository, fake),
            lease_seconds=30,
            heartbeat_seconds=1,
            owner_id="worker",
        )
        noisy = MonthlySourceCommand(
            source_name="nhtsa_bulk",
            run_key="noisy",
            command=(
                sys.executable,
                "-c",
                ("import time; [print('line', flush=True) for _ in range(50)]; time.sleep(60)"),
            ),
            environment={"PYTHONUNBUFFERED": "1"},
        )
        report = await asyncio.wait_for(
            service.run(
                period_key="2099-01",
                scheduled_for="2098-12-31T17:00:00+00:00",
                commands=(
                    noisy,
                    _command("nhtsa_api", "api"),
                    _command("station", "station"),
                    _command("partsouq", "partsouq"),
                ),
            ),
            timeout=5,
        )

        assert report["status"] == "failed"
        assert "simulated monthly event storage failure" in str(report["errors"])

    asyncio.run(scenario())


def test_monthly_cli_uses_taipei_schedule_and_low_frequency_browser_canary(monkeypatch) -> None:
    monkeypatch.setenv("PARTSOUQ_BROWSER_EXECUTABLE", "/usr/bin/google-chrome")
    monkeypatch.setenv("PARTSOUQ_BROWSER_HEADLESS", "1")
    monkeypatch.setenv("PARTSOUQ_TRANSPORT", "browser")
    monkeypatch.setenv("PARTSOUQ_USER_AGENT", "monthly-crawler (contact: ops@example.com)")
    monkeypatch.setenv("PARTSOUQ_DELAY_SECONDS", "0.1")
    monkeypatch.setenv("PARTSOUQ_MAX_RETRIES", "9")
    monkeypatch.setenv("NHTSA_API_DELAY_SECONDS", "0.1")
    period, scheduled = _monthly_period("2026-08", "Asia/Taipei")
    commands = _monthly_commands(
        period,
        PartSouqMySQLConfig(password="not-on-process-list"),
    )
    partsouq = commands[-1]

    assert scheduled == "2026-07-31T17:00:00+00:00"
    assert "--transport" in partsouq.command
    assert partsouq.command[partsouq.command.index("--transport") + 1] == "browser"
    assert "--browser-headless" in partsouq.command
    assert "--retry-challenges" not in partsouq.command
    assert "--browser-worker-command" not in partsouq.command
    assert partsouq.command[partsouq.command.index("--delay") + 1] == "30.0"
    assert partsouq.command[partsouq.command.index("--retry-count") + 1] == "1"
    assert partsouq.command[partsouq.command.index("--robots-policy") + 1] == "require"
    assert partsouq.command[partsouq.command.index("--user-agent") + 1] == (
        "monthly-crawler (contact: ops@example.com)"
    )
    assert commands[0].environment["NHTSA_API_DELAY_SECONDS"] == "1.0"
    assert commands[1].environment["NHTSA_API_DELAY_SECONDS"] == "1.0"
    assert commands[2].environment["NHTSA_API_DELAY_SECONDS"] == "1.0"
    assert "not-on-process-list" not in partsouq.command
    assert partsouq.environment["PARTSOUQ_MYSQL_PASSWORD"] == "not-on-process-list"
    args = build_parser().parse_args(["monthly-sync", "--period", "2026-08"])
    assert args.period == "2026-08"
    status_args = build_parser().parse_args(
        ["monthly-status", "--period", "2026-08", "--event-limit", "250"]
    )
    assert status_args.event_limit == 250
