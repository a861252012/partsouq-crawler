from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from partsouq_crawler.config import PartSouqMySQLConfig
from partsouq_crawler.db.repository import LeaseLostError, Repository
from partsouq_crawler.models.crawl import FetchResult
from partsouq_crawler.models.records import ParsedPage, PartRecord
from partsouq_crawler.services.ingest import IngestService
from partsouq_crawler.services.monthly_sync import MonthlySourceCommand, MonthlySyncService

pytestmark = pytest.mark.skipif(
    os.getenv("PARTSOUQ_TEST_MYSQL") != "1",
    reason="set PARTSOUQ_TEST_MYSQL=1 to run local MySQL integration tests",
)


def _config() -> PartSouqMySQLConfig:
    return PartSouqMySQLConfig.from_env(
        database=os.getenv("PARTSOUQ_TEST_MYSQL_DATABASE", "partsouq_test"),
        pool_max_size=12,
    )


def test_mysql_bulk_ingest_is_idempotent_and_keeps_provenance() -> None:
    async def scenario() -> None:
        repository = await Repository.create_mysql(_config())
        run_key = f"mysql-bulk-{uuid.uuid4().hex}"
        prefix = uuid.uuid4().hex[:12]
        try:
            run_id = await repository.create_or_get_run(run_key, [], {})
            url = f"https://example.invalid/{prefix}"
            result = FetchResult(
                requested_url=url,
                final_url=url,
                status=200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                body=b"<html></html>",
                elapsed_ms=1,
                attempt=1,
            )
            response_id, _ = await repository.store_response(
                run_id,
                None,
                result,
                challenged=False,
                challenge_reason=None,
            )
            parsed = ParsedPage(
                page_type="part_list",
                parts=[PartRecord(number_raw=f"{prefix}-{number:04d}") for number in range(500)],
            )
            service = IngestService(repository)
            first = await service.ingest(
                run_id=run_id,
                response_id=response_id,
                source_url=url,
                parsed=parsed,
            )
            second = await service.ingest(
                run_id=run_id,
                response_id=response_id,
                source_url=url,
                parsed=parsed,
            )
            parallel_prefix = f"{prefix}-parallel"
            parallel_parsed = ParsedPage(
                page_type="part_list",
                parts=[
                    PartRecord(number_raw=f"{parallel_prefix}-{number:04d}")
                    for number in range(500)
                ],
            )
            parallel_inserted = await asyncio.gather(
                service.ingest(
                    run_id=run_id,
                    response_id=response_id,
                    source_url=url,
                    parsed=parallel_parsed,
                ),
                service.ingest(
                    run_id=run_id,
                    response_id=response_id,
                    source_url=url,
                    parsed=parallel_parsed,
                ),
            )
            part_count = await repository._scalar(  # noqa: SLF001 - integration invariant
                """
                SELECT COUNT(*) FROM part_numbers
                WHERE number_raw LIKE ? AND number_raw NOT LIKE ?
                """,
                (f"{prefix}-%", f"{parallel_prefix}-%"),
            )
            provenance = await repository._scalar(  # noqa: SLF001 - integration invariant
                """
                SELECT COUNT(*) FROM record_sources
                WHERE response_id = ? AND record_type = 'part_number'
                """,
                (response_id,),
            )
            parallel_part_count = await repository._scalar(  # noqa: SLF001
                "SELECT COUNT(*) FROM part_numbers WHERE number_raw LIKE ?",
                (f"{parallel_prefix}-%",),
            )
            assert first == 500
            assert second == 0
            assert sum(parallel_inserted) == 500
            assert part_count == 500
            assert parallel_part_count == 500
            assert provenance == 1000
            assert not await repository.foreign_key_violations()
            status = await repository.db_status()
            assert int(status["database_bytes"]) >= int(status["compressed_bytes"])
        finally:
            await repository.close()

    asyncio.run(scenario())


def test_mysql_queue_claims_1000_once_and_rejects_stale_worker() -> None:
    async def scenario() -> None:
        repository = await Repository.create_mysql(_config())
        run_key = f"mysql-queue-{uuid.uuid4().hex}"
        try:
            run_id = await repository.create_or_get_run(run_key, [], {})
            inserted = await asyncio.gather(
                *(
                    repository.enqueue(
                        run_id,
                        f"https://example.invalid/{run_key}/{number}",
                        parent_url=None,
                        depth=0,
                    )
                    for number in range(1000)
                )
            )
            assert sum(inserted) == 1000
            claimed_ids: list[int] = []
            claimed_lock = asyncio.Lock()

            async def worker(number: int) -> None:
                worker_id = f"worker-{number}-{uuid.uuid4().hex}"
                while True:
                    item = await repository.acquire_next(
                        run_id,
                        worker_id=worker_id,
                        lease_seconds=30,
                        max_depth=0,
                    )
                    if item is None:
                        return
                    await repository.finish_queue(
                        item.id,
                        "done",
                        worker_id=item.worker_id,
                        fencing_token=item.fencing_token,
                    )
                    async with claimed_lock:
                        claimed_ids.append(item.id)

            await asyncio.gather(*(worker(number) for number in range(8)))
            assert len(claimed_ids) == 1000
            assert len(set(claimed_ids)) == 1000
            assert (await repository.queue_counts(run_id))["done"] == 1000

            stale_url = f"https://example.invalid/{run_key}/stale"
            assert await repository.enqueue(
                run_id,
                stale_url,
                parent_url=None,
                depth=0,
                priority=100,
            )
            old = await repository.acquire_next(
                run_id,
                worker_id="old-worker",
                lease_seconds=30,
                max_depth=0,
            )
            assert old is not None
            async with repository.transaction() as connection:
                await connection.execute(
                    """
                    UPDATE crawl_queue
                    SET lease_expires_at = DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 1 SECOND)
                    WHERE id = ?
                    """,
                    (old.id,),
                )
            assert await repository.recover_expired_leases(run_id) == 1
            new = await repository.acquire_next(
                run_id,
                worker_id="new-worker",
                lease_seconds=30,
                max_depth=0,
            )
            assert new is not None and new.id == old.id
            await repository.finish_queue(
                new.id,
                "done",
                worker_id=new.worker_id,
                fencing_token=new.fencing_token,
            )
            with pytest.raises(LeaseLostError):
                await repository.finish_queue(
                    old.id,
                    "failed",
                    worker_id=old.worker_id,
                    fencing_token=old.fencing_token,
                )
        finally:
            await repository.close()

    asyncio.run(scenario())


def test_mysql_monthly_run_deduplicates_and_fences_stale_owner() -> None:
    async def scenario() -> None:
        repository = await Repository.create_mysql(_config())
        period = "2099-12"
        try:
            async with repository.transaction() as connection:
                await connection.execute(
                    "DELETE FROM monthly_sync_runs WHERE period_key = ?", (period,)
                )
            first = await repository.acquire_monthly_run(
                period_key=period,
                scheduled_for="2099-11-30T17:00:00+00:00",
                owner_id="first-owner",
                lease_seconds=30,
                max_attempts=3,
                config={"test": True},
            )
            assert first.acquired and first.attempts == 1
            duplicate = await repository.acquire_monthly_run(
                period_key=period,
                scheduled_for="2099-11-30T17:00:00+00:00",
                owner_id="duplicate-owner",
                lease_seconds=30,
                max_attempts=3,
                config={"test": True},
            )
            assert not duplicate.acquired and duplicate.fencing_token == first.fencing_token

            await repository.update_monthly_source(
                first.run_id,
                owner_id="first-owner",
                fencing_token=first.fencing_token,
                source_name="nhtsa_bulk",
                status="completed",
                run_key="bulk",
            )
            await repository.append_monthly_events(
                first.run_id,
                first.fencing_token,
                [{"source_name": "nhtsa_bulk", "event_type": "progress", "message": "ok"}],
            )
            async with repository.transaction() as connection:
                await connection.execute(
                    """
                    UPDATE monthly_sync_runs
                    SET status = 'failed', lease_expires_at = NULL
                    WHERE id = ?
                    """,
                    (first.run_id,),
                )
            second = await repository.acquire_monthly_run(
                period_key=period,
                scheduled_for="2099-11-30T17:00:00+00:00",
                owner_id="second-owner",
                lease_seconds=30,
                max_attempts=3,
                config={"test": True},
            )
            assert second.acquired and second.attempts == 2
            assert second.fencing_token > first.fencing_token
            assert second.nhtsa_bulk_status == "completed"

            with pytest.raises(LeaseLostError):
                await repository.update_monthly_source(
                    first.run_id,
                    owner_id="first-owner",
                    fencing_token=first.fencing_token,
                    source_name="nhtsa_api",
                    status="failed",
                    run_key="stale",
                )
            with pytest.raises(LeaseLostError):
                await repository.append_monthly_events(
                    first.run_id,
                    first.fencing_token,
                    [{"event_type": "stale", "message": "must not persist"}],
                )

            await repository.finish_monthly_run(
                second.run_id,
                owner_id="second-owner",
                fencing_token=second.fencing_token,
                status="completed",
                summary={"test": "complete"},
            )
            terminal = await repository.acquire_monthly_run(
                period_key=period,
                scheduled_for="2099-11-30T17:00:00+00:00",
                owner_id="third-owner",
                lease_seconds=30,
                max_attempts=3,
                config={"test": True},
            )
            assert not terminal.acquired and terminal.status == "completed"
            report = await repository.monthly_run_report(period)
            assert report["run"]["attempts"] == 2
            assert report["run"]["summary_json"] == {"test": "complete"}
            assert report["event_counts"] == [
                {"source_name": "nhtsa_bulk", "level": "info", "count": 1}
            ]
            assert len(report["latest_events"]) == 1
            assert report["latest_events"][0]["event_type"] == "progress"
            assert report["latest_events"][0]["details_json"] == {}
        finally:
            async with repository.transaction() as connection:
                await connection.execute(
                    "DELETE FROM monthly_sync_runs WHERE period_key = ?", (period,)
                )
            await repository.close()

    asyncio.run(scenario())


def test_mysql_monthly_run_finalizes_an_exhausted_inactive_lease() -> None:
    async def scenario() -> None:
        repository = await Repository.create_mysql(_config())
        period = "2099-11"
        try:
            async with repository.transaction() as connection:
                await connection.execute(
                    "DELETE FROM monthly_sync_runs WHERE period_key = ?", (period,)
                )
            first = await repository.acquire_monthly_run(
                period_key=period,
                scheduled_for="2099-10-31T17:00:00+00:00",
                owner_id="crashed-owner",
                lease_seconds=30,
                max_attempts=1,
                config={"test": True},
            )
            assert first.acquired
            async with repository.transaction() as connection:
                await connection.execute(
                    """
                    UPDATE monthly_sync_runs
                    SET lease_expires_at = DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 1 SECOND)
                    WHERE id = ?
                    """,
                    (first.run_id,),
                )
            exhausted = await repository.acquire_monthly_run(
                period_key=period,
                scheduled_for="2099-10-31T17:00:00+00:00",
                owner_id="recovery-owner",
                lease_seconds=30,
                max_attempts=1,
                config={"test": True},
            )
            report = await repository.monthly_run_report(period)

            assert not exhausted.acquired
            assert exhausted.status == "completed_with_gaps"
            assert report["run"]["ended_at"] is not None
            assert report["run"]["last_error"] == ("attempt limit exhausted after inactive lease")
        finally:
            async with repository.transaction() as connection:
                await connection.execute(
                    "DELETE FROM monthly_sync_runs WHERE period_key = ?", (period,)
                )
            await repository.close()

    asyncio.run(scenario())


def test_mysql_monthly_service_runs_children_persists_logs_and_deduplicates() -> None:
    async def scenario() -> None:
        repository = await Repository.create_mysql(_config())
        period = "2098-11"
        partsouq_run_key = f"monthly-service-partsouq-{uuid.uuid4().hex}"
        commands = tuple(
            MonthlySourceCommand(
                source_name=source_name,
                run_key=(partsouq_run_key if source_name == "partsouq" else source_name),
                command=(
                    sys.executable,
                    "-c",
                    (
                        "import json; "
                        f"print(json.dumps({{'event': 'e2e_progress', "
                        f"'source': {source_name!r}}}))"
                    ),
                ),
                environment={"PYTHONUNBUFFERED": "1"},
            )
            for source_name in ("nhtsa_bulk", "nhtsa_api", "partsouq")
        )
        try:
            async with repository.transaction() as connection:
                await connection.execute(
                    "DELETE FROM monthly_sync_runs WHERE period_key = ?", (period,)
                )
            partsouq_run_id = await repository.create_or_get_run(
                partsouq_run_key, [], {"test": True}
            )
            await repository.set_run_status(partsouq_run_id, "completed", ended=True)
            service = MonthlySyncService(
                repository,
                lease_seconds=30,
                heartbeat_seconds=1,
                owner_id="mysql-monthly-service-e2e",
            )
            first = await service.run(
                period_key=period,
                scheduled_for="2098-10-31T17:00:00+00:00",
                commands=commands,
            )
            second = await service.run(
                period_key=period,
                scheduled_for="2098-10-31T17:00:00+00:00",
                commands=commands,
            )
            report = await repository.monthly_run_report(period)

            assert first["status"] == "completed"
            assert second["acquired"] is False
            assert second["status"] == "completed"
            assert report["run"]["attempts"] == 1
            assert (
                sum(event["event_type"] == "e2e_progress" for event in report["latest_events"]) == 3
            )
        finally:
            async with repository.transaction() as connection:
                await connection.execute(
                    "DELETE FROM monthly_sync_runs WHERE period_key = ?", (period,)
                )
                await connection.execute(
                    "DELETE FROM crawl_runs WHERE run_key = ?", (partsouq_run_key,)
                )
            await repository.close()

    asyncio.run(scenario())


def test_mysql_monthly_service_resumes_only_incomplete_sources(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = await Repository.create_mysql(_config())
        period = "2098-10"
        partsouq_run_key = f"monthly-resume-partsouq-{uuid.uuid4().hex}"
        marker = tmp_path / "nhtsa-api-first-attempt"
        pass_command = (sys.executable, "-c", 'print(\'{"event":"passed"}\')')
        fail_once_command = (
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                f"p=Path({str(marker)!r}); "
                "exists=p.exists(); p.write_text('attempted'); "
                'print(\'{"event":"api_attempt"}\'); '
                "raise SystemExit(0 if exists else 1)"
            ),
        )
        commands = (
            MonthlySourceCommand("nhtsa_bulk", "bulk", pass_command, {}),
            MonthlySourceCommand("nhtsa_api", "api", fail_once_command, {}),
            MonthlySourceCommand("partsouq", partsouq_run_key, pass_command, {}),
        )
        try:
            async with repository.transaction() as connection:
                await connection.execute(
                    "DELETE FROM monthly_sync_runs WHERE period_key = ?", (period,)
                )
            partsouq_run_id = await repository.create_or_get_run(
                partsouq_run_key, [], {"test": True}
            )
            await repository.set_run_status(partsouq_run_id, "completed", ended=True)

            first = await MonthlySyncService(
                repository,
                lease_seconds=30,
                heartbeat_seconds=1,
                owner_id="mysql-monthly-resume-1",
            ).run(
                period_key=period,
                scheduled_for="2098-09-30T17:00:00+00:00",
                commands=commands,
            )
            second = await MonthlySyncService(
                repository,
                lease_seconds=30,
                heartbeat_seconds=1,
                owner_id="mysql-monthly-resume-2",
            ).run(
                period_key=period,
                scheduled_for="2098-09-30T17:00:00+00:00",
                commands=commands,
            )
            report = await repository.monthly_run_report(period, event_limit=1000)

            assert first["status"] == "failed"
            assert second["status"] == "completed"
            assert report["run"]["attempts"] == 2
            skipped = [
                event["source_name"]
                for event in report["latest_events"]
                if event["event_type"] == "source_skipped_completed"
            ]
            assert skipped == ["nhtsa_bulk", "partsouq"]
        finally:
            async with repository.transaction() as connection:
                await connection.execute(
                    "DELETE FROM monthly_sync_runs WHERE period_key = ?", (period,)
                )
                await connection.execute(
                    "DELETE FROM crawl_runs WHERE run_key = ?", (partsouq_run_key,)
                )
            await repository.close()

    asyncio.run(scenario())


def test_mysql_sqlite_import_manifest_resumes_failed_cursor_and_replays_completed() -> None:
    async def scenario() -> None:
        repository = await Repository.create_mysql(_config())
        snapshot_sha256 = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        body_sha256 = hashlib.sha256(b"legacy body").hexdigest()
        try:
            first = await repository.create_or_get_sqlite_import_manifest(
                source_snapshot_sha256=snapshot_sha256,
                source_schema_version=1,
                source_bytes=123,
                source_counts={"archive_captures": 2},
            )
            import_id = int(first["id"])
            assert first["resume_after_capture_id"] == 0
            await repository.record_sqlite_import_item(
                import_id=import_id,
                source_capture_id=1,
                source_response_id=101,
                body_sha256=body_sha256,
                target_response_id=None,
                status="skipped",
                error_message=None,
            )
            await repository.finish_sqlite_import_manifest(
                import_id,
                status="failed",
                target_counts={},
                error_message="simulated interruption",
            )

            resumed = await repository.create_or_get_sqlite_import_manifest(
                source_snapshot_sha256=snapshot_sha256,
                source_schema_version=1,
                source_bytes=123,
                source_counts={"archive_captures": 2},
            )
            assert resumed["previous_status"] == "failed"
            assert resumed["resume_after_capture_id"] == 1
            await repository.finish_sqlite_import_manifest(
                import_id,
                status="completed",
                target_counts={"archive_captures": 2},
            )

            replayed = await repository.create_or_get_sqlite_import_manifest(
                source_snapshot_sha256=snapshot_sha256,
                source_schema_version=1,
                source_bytes=123,
                source_counts={"archive_captures": 2},
            )
            assert replayed["previous_status"] == "completed"
            assert replayed["resume_after_capture_id"] == 0
        finally:
            await repository.close()

    asyncio.run(scenario())


def test_mysql_archive_manifest_refreshes_metadata_on_resume() -> None:
    async def scenario() -> None:
        repository = await Repository.create_mysql(_config())
        run_key = f"mysql-archive-manifest-{uuid.uuid4().hex}"
        try:
            run_id = await repository.create_or_get_run(run_key, [], {})
            manifest_key = hashlib.sha256(run_key.encode()).hexdigest()
            manifest_id = await repository.create_or_get_archive_import_manifest(
                run_id=run_id,
                archive_source="common_crawl",
                manifest_key=manifest_key,
                metadata={"selected_record_count": 5},
            )
            resumed_id = await repository.create_or_get_archive_import_manifest(
                run_id=run_id,
                archive_source="common_crawl",
                manifest_key=manifest_key,
                metadata={"selected_record_count": 3165},
            )
            assert resumed_id == manifest_id
            cursor = await repository.connection.execute(
                "SELECT metadata_json FROM archive_import_manifests WHERE id = ?",
                (manifest_id,),
            )
            row = await cursor.fetchone()
            assert row is not None
            metadata = row["metadata_json"]
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            assert metadata == {"selected_record_count": 3165}
        finally:
            await repository.close()

    asyncio.run(scenario())
