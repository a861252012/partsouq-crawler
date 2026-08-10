from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pymysql

from partsouq_crawler.config import DEFAULT_SEED, CrawlerConfig, PartSouqMySQLConfig
from partsouq_crawler.crawl.challenge import detect_challenge
from partsouq_crawler.crawl.engine import CrawlerEngine
from partsouq_crawler.crawl.fetcher import FetchError
from partsouq_crawler.crawl.robots import parse_robots
from partsouq_crawler.crawl.sitemap import parse_sitemap
from partsouq_crawler.crawl.transport import create_fetch_transport
from partsouq_crawler.db.repository import Repository
from partsouq_crawler.logging import CrawlLogger
from partsouq_crawler.nhtsa.api_service import NhtsaApiSyncService
from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.nhtsa.datasets import BULK_SOURCES_BY_SCOPE
from partsouq_crawler.nhtsa.repository import NhtsaMySQLRepository
from partsouq_crawler.nhtsa.service import NhtsaBulkSyncService
from partsouq_crawler.parsers.base import CatalogParser, ParseError
from partsouq_crawler.services.archive_import import ArchiveCaptureInput, ArchiveImportService
from partsouq_crawler.services.common_crawl_import import CommonCrawlImportService
from partsouq_crawler.services.export import ExportService
from partsouq_crawler.services.ingest import IngestService
from partsouq_crawler.services.monthly_sync import MonthlySourceCommand, MonthlySyncService
from partsouq_crawler.services.reparse import ReparseService
from partsouq_crawler.services.sqlite_archive_import import SQLiteArchiveImportService
from partsouq_crawler.services.wayback_import import WaybackImportService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="partsouq-crawler")
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe = subparsers.add_parser("probe", help="Fetch one URL once and store its response")
    probe.add_argument("url")
    _partsouq_mysql_arguments(probe)
    probe.add_argument("--run-id", default="partsouq-live-probe")
    probe.add_argument("--user-agent")
    probe.add_argument("--timeout", type=float, default=30.0)
    _transport_arguments(probe)

    crawl = subparsers.add_parser("crawl-all", help="Resume or start full discovery crawl")
    _partsouq_mysql_arguments(crawl)
    crawl.add_argument("--run-id", required=True)
    crawl.add_argument("--seed-url", default=DEFAULT_SEED)
    crawl.add_argument("--max-pages", type=int, default=0)
    crawl.add_argument("--max-depth", type=int, default=0)
    crawl.add_argument("--concurrency", type=int, default=1)
    crawl.add_argument("--delay", type=float, default=5.0)
    crawl.add_argument("--timeout", type=float, default=30.0)
    crawl.add_argument("--retry-count", type=int, default=3)
    crawl.add_argument("--robots-policy", choices=("require", "ignore"), default="require")
    crawl.add_argument("--user-agent")
    crawl.add_argument("--json-log", action="store_true")
    crawl.add_argument("--retry-challenges", action="store_true")
    _transport_arguments(crawl)

    status = subparsers.add_parser("crawl-status")
    _partsouq_mysql_arguments(status)
    status.add_argument("--run-id", required=True)

    db_status = subparsers.add_parser("db-status")
    _partsouq_mysql_arguments(db_status)

    problems = subparsers.add_parser("problem-urls")
    _partsouq_mysql_arguments(problems)
    problems.add_argument("--run-id", required=True)
    problems.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    problems.add_argument("--output", type=Path)

    requeue = subparsers.add_parser("requeue-problems")
    _partsouq_mysql_arguments(requeue)
    requeue.add_argument("--run-id", required=True)
    requeue.add_argument(
        "--status", action="append", choices=("failed", "parse_failed", "challenged"), required=True
    )

    dump = subparsers.add_parser("dump-response")
    _partsouq_mysql_arguments(dump)
    dump.add_argument("--response-id", type=int)
    dump.add_argument("--url")
    dump.add_argument("--sha256")
    dump.add_argument("--output", type=Path, required=True)

    reparse = subparsers.add_parser("reparse")
    _partsouq_mysql_arguments(reparse)
    reparse.add_argument("--response-id", type=int)
    reparse.add_argument("--run-id", type=int)
    reparse.add_argument("--page-type")

    export = subparsers.add_parser("export")
    _partsouq_mysql_arguments(export)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--include-compatibility-hints", action="store_true")
    export.add_argument("--include-unverified-fitments", action="store_true")
    export.add_argument("--include-sensitive-source-urls", action="store_true")

    archive_import = subparsers.add_parser(
        "archive-import",
        help="Import a lawfully obtained historical HTML capture without contacting PartSouq",
    )
    _partsouq_mysql_arguments(archive_import)
    archive_import.add_argument("--run-id", required=True)
    archive_import.add_argument("--input", type=Path, required=True)
    archive_import.add_argument("--source-url", required=True)
    archive_import.add_argument(
        "--archive-source",
        choices=("common_crawl", "wayback", "owned_export"),
        required=True,
    )
    archive_import.add_argument("--captured-at", required=True)
    archive_import.add_argument("--collection")
    archive_import.add_argument("--warc-filename")
    archive_import.add_argument("--warc-offset", type=int)
    archive_import.add_argument("--warc-length", type=int)
    archive_import.add_argument("--archive-digest")
    archive_import.add_argument("--truncation-reason")

    common_crawl_import = subparsers.add_parser(
        "common-crawl-import",
        help="Import allowlisted PartSouq WARC records from Common Crawl index files",
    )
    _partsouq_mysql_arguments(common_crawl_import)
    common_crawl_import.add_argument("--run-id", required=True)
    common_crawl_import.add_argument("--index", type=Path, action="append", required=True)
    common_crawl_import.add_argument("--max-records", type=int, default=0)
    common_crawl_import.add_argument("--delay", type=float, default=0.25)
    common_crawl_import.add_argument("--timeout", type=float, default=60.0)

    wayback_import = subparsers.add_parser(
        "wayback-import",
        help="Import allowlisted PartSouq captures from local Wayback CDX JSON files",
    )
    _partsouq_mysql_arguments(wayback_import)
    wayback_import.add_argument("--run-id", required=True)
    wayback_import.add_argument("--index", type=Path, action="append", required=True)
    wayback_import.add_argument("--max-records", type=int, default=0)
    wayback_import.add_argument("--delay", type=float, default=2.0)
    wayback_import.add_argument("--timeout", type=float, default=60.0)

    sqlite_migrate = subparsers.add_parser(
        "sqlite-archive-migrate",
        help="Migrate a legacy archive snapshot into the production MySQL schema",
    )
    _partsouq_mysql_arguments(sqlite_migrate)
    sqlite_migrate.add_argument("--source-sqlite", type=Path, required=True)
    sqlite_migrate.add_argument("--run-id")
    sqlite_migrate.add_argument("--batch-size", type=int, default=100)

    nhtsa_sync = subparsers.add_parser(
        "nhtsa-sync-bulk",
        help="Import official NHTSA bulk datasets into MySQL",
    )
    _nhtsa_arguments(nhtsa_sync)
    nhtsa_sync.add_argument("--run-id", default="nhtsa-official-bulk")
    nhtsa_sync.add_argument(
        "--scope",
        choices=tuple(BULK_SOURCES_BY_SCOPE),
        default="all",
    )

    nhtsa_api = subparsers.add_parser(
        "nhtsa-sync-api",
        help="Sync allowlisted NHTSA vPIC and CSSI APIs into MySQL",
    )
    _nhtsa_arguments(nhtsa_api)
    nhtsa_api.add_argument("--run-id", default="nhtsa-official-api")
    nhtsa_api.add_argument("--scope", choices=("all", "vpic", "cssi"), default="all")

    nhtsa_status = subparsers.add_parser("nhtsa-status")
    _nhtsa_arguments(nhtsa_status, include_runtime=False)

    monthly_sync = subparsers.add_parser(
        "monthly-sync",
        help="Run the resumable monthly NHTSA and PartSouq collection",
    )
    _partsouq_mysql_arguments(monthly_sync)
    monthly_sync.add_argument("--period", help="calendar month in YYYY-MM")
    monthly_sync.add_argument("--timezone", default="Asia/Taipei")
    monthly_sync.add_argument("--lease-seconds", type=int, default=300)
    monthly_sync.add_argument("--heartbeat-seconds", type=float, default=60)
    monthly_sync.add_argument("--max-attempts", type=int, default=3)

    monthly_status = subparsers.add_parser("monthly-status")
    _partsouq_mysql_arguments(monthly_status)
    monthly_status.add_argument("--period", help="calendar month in YYYY-MM")
    monthly_status.add_argument("--timezone", default="Asia/Taipei")
    monthly_status.add_argument("--event-limit", type=int, default=100)
    return parser


def _partsouq_mysql_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mysql-host")
    parser.add_argument("--mysql-port", type=int)
    parser.add_argument("--mysql-database")
    parser.add_argument("--mysql-user")
    parser.add_argument("--mysql-password")
    parser.add_argument("--mysql-pool-min", type=int)
    parser.add_argument("--mysql-pool-max", type=int)


def _transport_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--transport", choices=("http", "browser", "nodriver"), default="http")
    parser.add_argument("--browser-executable", type=Path)
    parser.add_argument("--browser-headless", action="store_true")
    parser.add_argument("--browser-profile-dir", type=Path)
    parser.add_argument("--browser-worker-command")
    parser.add_argument("--browser-challenge-wait", type=float)
    parser.add_argument("--browser-restart-pages", type=int)


def _nhtsa_arguments(parser: argparse.ArgumentParser, *, include_runtime: bool = True) -> None:
    parser.add_argument("--mysql-host")
    parser.add_argument("--mysql-port", type=int)
    parser.add_argument("--mysql-database")
    parser.add_argument("--mysql-user")
    parser.add_argument("--mysql-password")
    if include_runtime:
        parser.add_argument("--raw-dir", type=Path)
        parser.add_argument("--user-agent")
        parser.add_argument("--timeout", type=float)
        parser.add_argument("--api-delay", type=float)
        parser.add_argument("--json-log", action="store_true")


async def dispatch(args: argparse.Namespace) -> int:
    if args.command.startswith("nhtsa-"):
        return await _dispatch_nhtsa(args)
    mysql_config = PartSouqMySQLConfig.from_env(
        host=args.mysql_host,
        port=args.mysql_port,
        database=args.mysql_database,
        user=args.mysql_user,
        password=args.mysql_password,
        pool_min_size=args.mysql_pool_min,
        pool_max_size=args.mysql_pool_max,
    )
    repository = await Repository.create_mysql(mysql_config)
    try:
        if args.command == "monthly-sync":
            period_key, scheduled_for = _monthly_period(args.period, args.timezone)
            report = await MonthlySyncService(
                repository,
                lease_seconds=args.lease_seconds,
                heartbeat_seconds=args.heartbeat_seconds,
                max_attempts=args.max_attempts,
                logger=CrawlLogger(json_mode=True),
            ).run(
                period_key=period_key,
                scheduled_for=scheduled_for,
                commands=_monthly_commands(period_key, mysql_config),
            )
            _print_json(report)
            return int(str(report["exit_code"]))
        if args.command == "monthly-status":
            period_key, _ = _monthly_period(args.period, args.timezone)
            _print_json(
                await repository.monthly_run_report(period_key, event_limit=args.event_limit)
            )
            return 0
        if args.command == "probe":
            return await _probe(repository, args)
        if args.command == "crawl-all":
            config = CrawlerConfig.from_env(
                concurrency=args.concurrency,
                delay_seconds=args.delay,
                request_timeout_seconds=args.timeout,
                max_retries=args.retry_count,
                max_pages=args.max_pages,
                max_depth=args.max_depth,
                robots_policy=args.robots_policy,
                user_agent=args.user_agent,
                log_json=args.json_log,
                transport=args.transport,
                browser_executable=args.browser_executable,
                browser_headless=args.browser_headless,
                browser_profile_dir=args.browser_profile_dir,
                browser_worker_command=args.browser_worker_command,
                browser_challenge_wait_seconds=args.browser_challenge_wait,
                browser_restart_pages=args.browser_restart_pages,
                retry_challenges=args.retry_challenges,
            )
            engine = CrawlerEngine(
                repository=repository,
                config=config,
                run_key=args.run_id,
                seed_url=args.seed_url,
                logger=CrawlLogger(json_mode=args.json_log),
            )
            return await engine.run()
        if args.command == "crawl-status":
            _print_json(await repository.status_report(args.run_id))
            return 0
        if args.command == "db-status":
            _print_json(await repository.db_status())
            return 0
        if args.command == "problem-urls":
            rows = await repository.problem_urls(args.run_id)
            _write_problem_rows(rows, args.format, args.output)
            return 0
        if args.command == "requeue-problems":
            count = await repository.requeue_problems(args.run_id, args.status)
            _print_json({"requeued": count, "statuses": args.status})
            return 0
        if args.command == "dump-response":
            return await _dump_response(repository, args)
        if args.command == "reparse":
            reparse_report = await ReparseService(repository).run(
                response_id=args.response_id,
                run_id=args.run_id,
                page_type=args.page_type,
            )
            _print_json(reparse_report)
            return 0 if reparse_report["failed"] == 0 else 1
        if args.command == "export":
            count = await ExportService(repository).export(
                args.output,
                include_compatibility_hints=args.include_compatibility_hints,
                include_unverified_fitments=args.include_unverified_fitments,
                include_sensitive_source_urls=args.include_sensitive_source_urls,
            )
            _print_json({"output": str(args.output), "rows": count})
            return 0
        if args.command == "archive-import":
            archive_report = await ArchiveImportService(repository).import_html(
                run_key=args.run_id,
                capture=ArchiveCaptureInput(
                    input_path=args.input,
                    source_url=args.source_url,
                    archive_source=args.archive_source,
                    captured_at=args.captured_at,
                    collection_name=args.collection,
                    warc_filename=args.warc_filename,
                    warc_offset=args.warc_offset,
                    warc_length=args.warc_length,
                    archive_digest=args.archive_digest,
                    truncation_reason=args.truncation_reason,
                ),
            )
            _print_json(archive_report)
            return 0 if archive_report["error"] is None else 1
        if args.command == "common-crawl-import":
            common_crawl_report = await CommonCrawlImportService(repository).run(
                run_key=args.run_id,
                index_paths=args.index,
                max_records=args.max_records,
                delay_seconds=args.delay,
                timeout_seconds=args.timeout,
            )
            _print_json(common_crawl_report)
            return 0 if common_crawl_report["failed"] == 0 else 1
        if args.command == "wayback-import":
            wayback_report = await WaybackImportService(repository).run(
                run_key=args.run_id,
                index_paths=args.index,
                max_records=args.max_records,
                delay_seconds=args.delay,
                timeout_seconds=args.timeout,
            )
            _print_json(wayback_report)
            queue = wayback_report["queue"]
            return 0 if isinstance(queue, dict) and queue.get("failed", 0) == 0 else 1
        if args.command == "sqlite-archive-migrate":
            migration_report = await SQLiteArchiveImportService(repository).run(
                sqlite_path=args.source_sqlite,
                run_key=args.run_id,
                batch_size=args.batch_size,
            )
            _print_json(migration_report)
            failed = bool(
                migration_report["quarantined"]
                or migration_report["missing_provenance"]
                or migration_report["orphans"]
                or migration_report["foreign_key_violations"]
            )
            return 1 if failed else 0
        raise ValueError(f"unsupported command: {args.command}")
    finally:
        await repository.close()


async def _dispatch_nhtsa(args: argparse.Namespace) -> int:
    config = NhtsaConfig.from_env(
        mysql_host=args.mysql_host,
        mysql_port=args.mysql_port,
        mysql_database=args.mysql_database,
        mysql_user=args.mysql_user,
        mysql_password=args.mysql_password,
        raw_dir=getattr(args, "raw_dir", None),
        user_agent=getattr(args, "user_agent", None),
        request_timeout_seconds=getattr(args, "timeout", None),
        api_delay_seconds=getattr(args, "api_delay", None),
    )
    try:
        repository = NhtsaMySQLRepository.create(config)
    except pymysql.MySQLError as error:
        _print_json({"status": "failed", "error_type": type(error).__name__, "error": str(error)})
        return 1
    try:
        if args.command == "nhtsa-status":
            _print_json(repository.status_report())
            return 0
        if args.command == "nhtsa-sync-bulk":
            sources = BULK_SOURCES_BY_SCOPE[args.scope]
            report = await NhtsaBulkSyncService(
                repository,
                config,
                logger=CrawlLogger(json_mode=args.json_log),
            ).run(
                run_key=args.run_id,
                scope_name=args.scope,
                sources=sources,
            )
            _print_json(report)
            return 0 if report["status"] == "completed" else 1
        if args.command == "nhtsa-sync-api":
            report = await NhtsaApiSyncService(
                repository,
                config,
                logger=CrawlLogger(json_mode=args.json_log),
            ).run(
                run_key=args.run_id,
                scope_name=args.scope,
            )
            _print_json(report)
            return 0 if report["status"] == "completed" else 1
        raise ValueError(f"unsupported NHTSA command: {args.command}")
    finally:
        repository.close()


def _monthly_period(period: str | None, timezone_name: str) -> tuple[str, str]:
    timezone = ZoneInfo(timezone_name)
    if period is None:
        now = datetime.now(timezone)
        period = f"{now.year:04d}-{now.month:02d}"
    try:
        parsed = datetime.strptime(period, "%Y-%m")
    except ValueError as error:
        raise ValueError("monthly period must use YYYY-MM") from error
    if parsed.strftime("%Y-%m") != period:
        raise ValueError("monthly period must use YYYY-MM")
    scheduled = datetime(parsed.year, parsed.month, 1, 1, tzinfo=timezone)
    return period, scheduled.astimezone(UTC).isoformat()


def _monthly_commands(
    period_key: str,
    mysql_config: PartSouqMySQLConfig,
) -> tuple[MonthlySourceCommand, ...]:
    module = (sys.executable, "-m", "partsouq_crawler")
    child_environment = {
        "PYTHONUNBUFFERED": "1",
        "PARTSOUQ_MYSQL_HOST": mysql_config.host,
        "PARTSOUQ_MYSQL_PORT": str(mysql_config.port),
        "PARTSOUQ_MYSQL_DATABASE": mysql_config.database,
        "PARTSOUQ_MYSQL_USER": mysql_config.user,
        "PARTSOUQ_MYSQL_PASSWORD": mysql_config.password,
        "PARTSOUQ_MYSQL_POOL_MIN_SIZE": str(mysql_config.pool_min_size),
        "PARTSOUQ_MYSQL_POOL_MAX_SIZE": str(mysql_config.pool_max_size),
    }
    partsouq_command = (
        *module,
        "crawl-all",
        "--run-id",
        f"monthly-{period_key}-partsouq",
        "--seed-url",
        os.getenv("PARTSOUQ_SEED_URL", DEFAULT_SEED),
        "--max-pages",
        "0",
        "--max-depth",
        "0",
        "--concurrency",
        "1",
        "--delay",
        os.getenv("PARTSOUQ_DELAY_SECONDS", "5"),
        "--timeout",
        os.getenv("PARTSOUQ_REQUEST_TIMEOUT_SECONDS", "60"),
        "--retry-count",
        os.getenv("PARTSOUQ_MAX_RETRIES", "3"),
        "--robots-policy",
        os.getenv("PARTSOUQ_ROBOTS_POLICY", "require"),
        "--transport",
        "nodriver",
        "--browser-executable",
        os.getenv("PARTSOUQ_BROWSER_EXECUTABLE", ""),
        "--browser-profile-dir",
        os.getenv("PARTSOUQ_BROWSER_PROFILE_DIR", "output/partsouq/browser-profile"),
        "--browser-worker-command",
        os.getenv("PARTSOUQ_BROWSER_WORKER_COMMAND", ""),
        "--browser-challenge-wait",
        os.getenv("PARTSOUQ_BROWSER_CHALLENGE_WAIT_SECONDS", "60"),
        "--browser-restart-pages",
        os.getenv("PARTSOUQ_BROWSER_RESTART_PAGES", "500"),
        "--retry-challenges",
        "--json-log",
    )
    return (
        MonthlySourceCommand(
            source_name="nhtsa_bulk",
            run_key=f"monthly-{period_key}-nhtsa-bulk",
            command=(
                *module,
                "nhtsa-sync-bulk",
                "--run-id",
                f"monthly-{period_key}-nhtsa-bulk",
                "--scope",
                "all",
                "--json-log",
            ),
            environment={"PYTHONUNBUFFERED": "1"},
        ),
        MonthlySourceCommand(
            source_name="nhtsa_api",
            run_key=f"monthly-{period_key}-nhtsa-api",
            command=(
                *module,
                "nhtsa-sync-api",
                "--run-id",
                f"monthly-{period_key}-nhtsa-api",
                "--scope",
                "all",
                "--json-log",
            ),
            environment={"PYTHONUNBUFFERED": "1"},
        ),
        MonthlySourceCommand(
            source_name="partsouq",
            run_key=f"monthly-{period_key}-partsouq",
            command=partsouq_command,
            environment=child_environment,
        ),
    )


async def _probe(repository: Repository, args: argparse.Namespace) -> int:
    config = CrawlerConfig.from_env(
        request_timeout_seconds=args.timeout,
        max_retries=0,
        user_agent=args.user_agent,
        delay_seconds=0,
        transport=args.transport,
        browser_executable=args.browser_executable,
        browser_headless=args.browser_headless,
        browser_profile_dir=args.browser_profile_dir,
        browser_worker_command=args.browser_worker_command,
        browser_challenge_wait_seconds=args.browser_challenge_wait,
        browser_restart_pages=args.browser_restart_pages,
    )
    run_id = await repository.create_or_get_run(args.run_id, [args.url], config.public_dict())
    await repository.set_run_status(run_id, "running")
    try:
        async with create_fetch_transport(config, delay_seconds=0) as fetcher:
            result = await fetcher.fetch_once(args.url)
    except FetchError as error:
        await repository.set_run_status(run_id, "failed", ended=True)
        _print_json({"url": args.url, "error": str(error)})
        return 1

    challenge = detect_challenge(result.status, result.headers, result.body)
    response_id, sha256 = await repository.store_response(
        run_id,
        None,
        result,
        challenged=challenge.challenged,
        challenge_reason=challenge.reason,
    )
    parsed_ok = False
    records = 0
    if not challenge.challenged and result.status < 400:
        try:
            if result.final_url.endswith("/robots.txt"):
                parse_robots(result.final_url, result.body, result.charset or "utf-8")
            elif result.final_url.endswith((".xml", ".xml.gz")) or "xml" in (
                result.content_type or ""
            ):
                parse_sitemap(result.body, compressed=result.final_url.endswith(".gz"))
            elif "html" in (result.content_type or ""):
                parsed = CatalogParser().parse(
                    result.final_url, result.body, result.charset or "utf-8"
                )
                records = await IngestService(repository).ingest(
                    run_id=run_id,
                    response_id=response_id,
                    source_url=result.final_url,
                    parsed=parsed,
                )
            else:
                raise ParseError("unsupported probe content type")
            parsed_ok = True
        except (OSError, ParseError, ValueError) as error:
            await repository.add_parse_failure(response_id, "catalog_parser", "probe", error)
    status = "blocked" if challenge.challenged else "completed"
    await repository.set_run_status(run_id, status, blocked_reason=challenge.reason, ended=True)
    _print_json(
        {
            "url": args.url,
            "http_status": result.status,
            "content_type": result.content_type,
            "cf_mitigated": result.headers.get("cf-mitigated"),
            "cloudflare_challenge": challenge.challenged,
            "challenge_reason": challenge.reason,
            "body_sha256": sha256,
            "response_id": response_id,
            "parsed": parsed_ok,
            "normalized_records_inserted": records,
        }
    )
    return 2 if challenge.challenged else (0 if result.status < 400 else 1)


async def _dump_response(repository: Repository, args: argparse.Namespace) -> int:
    provided = sum(value is not None for value in (args.response_id, args.url, args.sha256))
    if provided != 1:
        raise ValueError("provide exactly one of --response-id, --url, or --sha256")
    rows = await repository.find_responses(
        response_id=args.response_id, url=args.url, sha256=args.sha256
    )
    if not rows:
        raise KeyError("response not found")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(repository.restore_body(rows[-1]))
    _print_json({"response_id": rows[-1]["id"], "output": str(args.output)})
    return 0


def _write_problem_rows(
    rows: list[dict[str, object]], format_name: str, output: Path | None
) -> None:
    if format_name == "jsonl":
        text = "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows)
    else:
        import io

        buffer = io.StringIO()
        if rows:
            writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        text = buffer.getvalue()
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        code = asyncio.run(dispatch(args))
    except (KeyError, ValueError) as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
