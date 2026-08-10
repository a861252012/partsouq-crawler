from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass

from partsouq_crawler.db.repository import LeaseLostError, Repository
from partsouq_crawler.logging import CrawlLogger
from partsouq_crawler.models.schedule import MonthlyRunLease


@dataclass(frozen=True, slots=True)
class MonthlySourceCommand:
    source_name: str
    run_key: str
    command: tuple[str, ...]
    environment: Mapping[str, str]


class MonthlySyncService:
    def __init__(
        self,
        repository: Repository,
        *,
        lease_seconds: int = 300,
        heartbeat_seconds: float = 60,
        max_attempts: int = 3,
        logger: CrawlLogger | None = None,
        owner_id: str | None = None,
    ) -> None:
        if lease_seconds < 30:
            raise ValueError("monthly lease must be at least 30 seconds")
        if not 1 <= heartbeat_seconds < lease_seconds:
            raise ValueError("monthly heartbeat must be positive and shorter than the lease")
        if max_attempts < 1:
            raise ValueError("monthly max attempts must be at least 1")
        self.repository = repository
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.max_attempts = max_attempts
        self.logger = logger or CrawlLogger(json_mode=True)
        self.owner_id = owner_id or (f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}")
        self.stop_event = asyncio.Event()
        self.heartbeat_error: str | None = None

    async def run(
        self,
        *,
        period_key: str,
        scheduled_for: str,
        commands: Sequence[MonthlySourceCommand],
    ) -> dict[str, object]:
        self._validate_commands(commands)
        self.stop_event.clear()
        self.heartbeat_error = None
        public_config = {
            "lease_seconds": self.lease_seconds,
            "heartbeat_seconds": self.heartbeat_seconds,
            "max_attempts": self.max_attempts,
            "sources": [command.source_name for command in commands],
        }
        lease = await self.repository.acquire_monthly_run(
            period_key=period_key,
            scheduled_for=scheduled_for,
            owner_id=self.owner_id,
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
            config=public_config,
        )
        if not lease.acquired:
            return {
                "run_id": lease.run_id,
                "period": period_key,
                "status": lease.status,
                "acquired": False,
                "attempts": lease.attempts,
                "max_attempts": lease.max_attempts,
                "exit_code": 2 if lease.status in {"failed", "completed_with_gaps"} else 0,
            }

        self._install_signal_handlers()
        await self._event(
            lease,
            source_name="orchestrator",
            event_type="monthly_run_started",
            message=f"monthly sync {period_key} attempt {lease.attempts} started",
            details={"owner_id": self.owner_id},
        )
        heartbeat = asyncio.create_task(self._heartbeat(lease))
        source_statuses = self._initial_statuses(lease)
        errors: dict[str, str] = {}
        current_source: str | None = None
        try:
            for command in commands:
                current_source = command.source_name
                current_status = source_statuses[command.source_name]
                if current_status == "completed":
                    await self._event(
                        lease,
                        source_name=command.source_name,
                        event_type="source_skipped_completed",
                        message=f"{command.source_name} already completed in an earlier attempt",
                    )
                    continue
                if command.source_name == "partsouq" and current_status == "blocked":
                    await self._event(
                        lease,
                        source_name=command.source_name,
                        event_type="source_skipped_blocked",
                        message="partsouq remains blocked; no additional canary was sent",
                    )
                    continue
                if command.source_name == "partsouq":
                    existing_run = await self.repository.get_run(command.run_key)
                    if existing_run is not None and str(existing_run["status"]) == "blocked":
                        reason = str(existing_run["blocked_reason"] or "cloudflare_challenge")
                        source_statuses[command.source_name] = "blocked"
                        errors[command.source_name] = reason
                        await self.repository.update_monthly_source(
                            lease.run_id,
                            owner_id=self.owner_id,
                            fencing_token=lease.fencing_token,
                            source_name=command.source_name,
                            status="blocked",
                            run_key=command.run_key,
                            error=reason,
                        )
                        await self._event(
                            lease,
                            source_name=command.source_name,
                            event_type="source_skipped_blocked",
                            message="existing challenge found; no second canary was sent",
                        )
                        continue
                if self.stop_event.is_set():
                    source_statuses[command.source_name] = "interrupted"
                    break
                await self.repository.update_monthly_source(
                    lease.run_id,
                    owner_id=self.owner_id,
                    fencing_token=lease.fencing_token,
                    source_name=command.source_name,
                    status="running",
                    run_key=command.run_key,
                )
                await self._event(
                    lease,
                    source_name=command.source_name,
                    event_type="source_started",
                    message=f"{command.source_name} started",
                    details={"run_key": command.run_key},
                )
                return_code = await self._run_command(lease, command)
                status: str
                error: str | None
                if self.stop_event.is_set():
                    status, error = "interrupted", "child interrupted by orchestrator"
                else:
                    status, error = await self._source_result(command, return_code)
                source_statuses[command.source_name] = status
                if error:
                    errors[command.source_name] = error
                await self.repository.update_monthly_source(
                    lease.run_id,
                    owner_id=self.owner_id,
                    fencing_token=lease.fencing_token,
                    source_name=command.source_name,
                    status=status,
                    run_key=command.run_key,
                    error=error,
                )
                await self._event(
                    lease,
                    source_name=command.source_name,
                    level="error" if status in {"failed", "blocked", "interrupted"} else "info",
                    event_type="source_finished",
                    message=f"{command.source_name} finished with {status}",
                    details={"run_key": command.run_key, "return_code": return_code},
                )

            if self.heartbeat_error:
                raise LeaseLostError(self.heartbeat_error)
            if self.stop_event.is_set():
                final_status, exit_code = "interrupted", 1
            elif all(status == "completed" for status in source_statuses.values()):
                final_status, exit_code = "completed", 0
            elif (
                all(status in {"blocked", "completed"} for status in source_statuses.values())
                or lease.attempts >= lease.max_attempts
            ):
                final_status, exit_code = "completed_with_gaps", 2
            else:
                final_status, exit_code = "failed", 1
            summary = {
                "period": period_key,
                "attempt": lease.attempts,
                "sources": source_statuses,
                "errors": errors,
            }
            await self.repository.finish_monthly_run(
                lease.run_id,
                owner_id=self.owner_id,
                fencing_token=lease.fencing_token,
                status=final_status,
                summary=summary,
                error="; ".join(f"{key}: {value}" for key, value in errors.items()) or None,
            )
            return {
                "run_id": lease.run_id,
                "period": period_key,
                "status": final_status,
                "acquired": True,
                "attempts": lease.attempts,
                "max_attempts": lease.max_attempts,
                "sources": source_statuses,
                "errors": errors,
                "exit_code": exit_code,
            }
        except LeaseLostError:
            raise
        except Exception as error:
            error_text = f"{type(error).__name__}: {error}"
            if current_source is not None:
                source_statuses[current_source] = "failed"
                errors[current_source] = error_text
                with suppress(LeaseLostError):
                    await self.repository.update_monthly_source(
                        lease.run_id,
                        owner_id=self.owner_id,
                        fencing_token=lease.fencing_token,
                        source_name=current_source,
                        status="failed",
                        run_key=next(
                            command.run_key
                            for command in commands
                            if command.source_name == current_source
                        ),
                        error=error_text,
                    )
            summary = {
                "period": period_key,
                "attempt": lease.attempts,
                "sources": source_statuses,
                "errors": errors,
            }
            with suppress(LeaseLostError):
                await self._event(
                    lease,
                    source_name=current_source or "orchestrator",
                    event_type="monthly_run_failed",
                    message=error_text,
                    level="error",
                )
                await self.repository.finish_monthly_run(
                    lease.run_id,
                    owner_id=self.owner_id,
                    fencing_token=lease.fencing_token,
                    status="failed",
                    summary=summary,
                    error=error_text,
                )
            return {
                "run_id": lease.run_id,
                "period": period_key,
                "status": "failed",
                "acquired": True,
                "attempts": lease.attempts,
                "max_attempts": lease.max_attempts,
                "sources": source_statuses,
                "errors": errors,
                "exit_code": 1,
            }
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self._remove_signal_handlers()

    async def _run_command(self, lease: MonthlyRunLease, command: MonthlySourceCommand) -> int:
        environment = os.environ.copy()
        environment.update(command.environment)
        process = await asyncio.create_subprocess_exec(
            *command.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=environment,
            limit=4 * 1024 * 1024,
        )
        if process.stdout is None:
            raise RuntimeError("child process stdout is unavailable")
        reader = asyncio.create_task(self._capture_output(lease, command, process.stdout))
        waiter = asyncio.create_task(process.wait())
        stopper = asyncio.create_task(self.stop_event.wait())
        done, _ = await asyncio.wait({waiter, stopper, reader}, return_when=asyncio.FIRST_COMPLETED)
        reader_error = reader.exception() if reader in done and not reader.cancelled() else None
        if (stopper in done or reader_error is not None) and not waiter.done():
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(waiter, timeout=30)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await waiter
        elif reader in done and not waiter.done():
            await waiter
        stopper.cancel()
        await asyncio.gather(stopper, return_exceptions=True)
        await reader
        return int(waiter.result())

    async def _capture_output(
        self,
        lease: MonthlyRunLease,
        command: MonthlySourceCommand,
        stream: asyncio.StreamReader,
    ) -> None:
        events: list[dict[str, object]] = []
        while raw := await stream.readline():
            line = raw.decode("utf-8", errors="replace").rstrip()
            print(line, flush=True)
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                payload = {"line": line}
            if not isinstance(payload, dict):
                payload = {"value": payload}
            event_type = str(payload.get("event") or "child_output")
            status = str(payload.get("status") or "")
            level = "error" if status in {"failed", "blocked"} or "error" in payload else "info"
            events.append(
                {
                    "source_name": command.source_name,
                    "level": level,
                    "event_type": event_type,
                    "message": event_type if event_type != "child_output" else line,
                    "details": payload,
                }
            )
            if len(events) >= 50:
                await self.repository.append_monthly_events(
                    lease.run_id, lease.fencing_token, events
                )
                events.clear()
        await self.repository.append_monthly_events(lease.run_id, lease.fencing_token, events)

    async def _source_result(
        self, command: MonthlySourceCommand, return_code: int
    ) -> tuple[str, str | None]:
        if command.source_name == "partsouq":
            run = await self.repository.get_run(command.run_key)
            crawl_status = str(run["status"]) if run is not None else "missing"
            if crawl_status == "completed" and return_code == 0:
                return "completed", None
            if crawl_status == "blocked" or return_code == 2:
                reason = str(run["blocked_reason"] or "challenge") if run else "challenge"
                return "blocked", reason
            if return_code == 130:
                return "interrupted", "child interrupted"
            return "failed", f"crawler status={crawl_status}, exit={return_code}"
        if return_code == 0:
            return "completed", None
        if return_code == 130:
            return "interrupted", "child interrupted"
        return "failed", f"child exited with {return_code}"

    async def _heartbeat(self, lease: MonthlyRunLease) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            try:
                await self.repository.heartbeat_monthly_run(
                    lease.run_id,
                    owner_id=self.owner_id,
                    fencing_token=lease.fencing_token,
                    lease_seconds=self.lease_seconds,
                )
            except Exception as error:
                self.heartbeat_error = f"{type(error).__name__}: {error}"
                self.stop_event.set()
                return

    async def _event(
        self,
        lease: MonthlyRunLease,
        *,
        source_name: str,
        event_type: str,
        message: str,
        level: str = "info",
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.logger.event(event_type, source=source_name, message=message)
        await self.repository.append_monthly_events(
            lease.run_id,
            lease.fencing_token,
            [
                {
                    "source_name": source_name,
                    "level": level,
                    "event_type": event_type,
                    "message": message,
                    "details": dict(details or {}),
                }
            ],
        )

    @staticmethod
    def _initial_statuses(lease: MonthlyRunLease) -> dict[str, str]:
        return {
            "nhtsa_bulk": lease.nhtsa_bulk_status,
            "nhtsa_api": lease.nhtsa_api_status,
            "station": lease.station_status,
            "partsouq": lease.partsouq_status,
        }

    @staticmethod
    def _validate_commands(commands: Sequence[MonthlySourceCommand]) -> None:
        expected = {"nhtsa_bulk", "nhtsa_api", "station", "partsouq"}
        actual = [command.source_name for command in commands]
        if set(actual) != expected or len(actual) != len(expected):
            raise ValueError(
                "monthly sync requires exactly nhtsa_bulk, nhtsa_api, station, partsouq"
            )

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self.stop_event.set)
            except (NotImplementedError, RuntimeError):
                continue

    @staticmethod
    def _remove_signal_handlers() -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(signum)
            except (NotImplementedError, RuntimeError):
                continue
