from __future__ import annotations

import asyncio
import signal
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from lxml.etree import XMLSyntaxError

from partsouq_crawler.config import CrawlerConfig
from partsouq_crawler.crawl.challenge import detect_challenge
from partsouq_crawler.crawl.discovery import is_in_scope, normalize_url
from partsouq_crawler.crawl.fetcher import FetchError
from partsouq_crawler.crawl.retries import RETRYABLE_STATUS, rate_limit_delay, retry_delay
from partsouq_crawler.crawl.robots import RobotsRules, parse_robots
from partsouq_crawler.crawl.sitemap import parse_sitemap
from partsouq_crawler.crawl.transport import FetchTransport, create_fetch_transport
from partsouq_crawler.db.repository import LeaseLostError, Repository
from partsouq_crawler.logging import CrawlLogger
from partsouq_crawler.models.crawl import FetchResult, QueueItem
from partsouq_crawler.parsers.base import CatalogParser, ParseError
from partsouq_crawler.services.archive_queue import redact_error, redact_sensitive_url
from partsouq_crawler.services.ingest import IngestService


class CrawlBlocked(RuntimeError):
    pass


class CrawlerEngine:
    def __init__(
        self,
        *,
        repository: Repository,
        config: CrawlerConfig,
        run_key: str,
        seed_url: str,
        logger: CrawlLogger | None = None,
    ) -> None:
        self.repository = repository
        self.config = config
        self.run_key = run_key
        self.seed_url = normalize_url(seed_url)
        self.logger = logger or CrawlLogger(json_mode=config.log_json)
        self.parser = CatalogParser()
        self.ingest = IngestService(repository)
        self.stop_event = asyncio.Event()
        self.blocked_event = asyncio.Event()
        self.processed = 0
        self._counter_lock = asyncio.Lock()
        self.robots: RobotsRules | None = None
        self.run_id = 0
        self.worker_instance = uuid.uuid4().hex

    async def run(self) -> int:
        self.run_id = await self.repository.create_or_get_run(
            self.run_key, [self.seed_url], self.config.public_dict()
        )
        await self.repository.recover_expired_leases(self.run_id)
        await self.repository.set_run_status(self.run_id, "running")
        self.logger.event(
            "crawl_policy",
            run_id=self.run_key,
            concurrency=self.config.concurrency,
            delay_seconds=self.config.delay_seconds,
            max_retries=self.config.max_retries,
            robots_policy=self.config.robots_policy,
            transport=self.config.transport,
        )
        self._install_signal_handlers()
        async with create_fetch_transport(self.config) as fetcher:
            user_agent = fetcher.user_agent
            if self.config.robots_policy == "require":
                try:
                    self.robots = await self._ensure_robots(fetcher, user_agent)
                except CrawlBlocked as error:
                    await self.repository.set_run_status(
                        self.run_id, "blocked", blocked_reason=str(error), ended=True
                    )
                    return 2

            await self.repository.enqueue(
                self.run_id,
                self.seed_url,
                parent_url=None,
                depth=0,
                priority=100,
                discovery_method="seed",
            )
            if self.robots:
                for sitemap_url in self.robots.sitemaps:
                    if is_in_scope(sitemap_url, self.seed_url):
                        await self.repository.enqueue(
                            self.run_id,
                            sitemap_url,
                            parent_url=self.robots.url,
                            depth=0,
                            page_type_hint="sitemap",
                            priority=90,
                            discovery_method="robots_sitemap",
                        )

            workers = [
                asyncio.create_task(self._worker(fetcher, f"{self.worker_instance}-{index + 1}"))
                for index in range(self.config.concurrency)
            ]
            await asyncio.gather(*workers)

        await self.repository.refresh_run_counters(self.run_id)
        return await self._finalize()

    async def _ensure_robots(self, fetcher: FetchTransport, user_agent: str) -> RobotsRules:
        parts = urlsplit(self.seed_url)
        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        saved = await self.repository.latest_response_for_url(self.run_id, robots_url)
        if saved is not None:
            row, body = saved
            if row["is_cloudflare_challenge"]:
                if not self.config.retry_challenges:
                    raise CrawlBlocked(str(row["challenge_reason"] or "cloudflare_challenge"))
            else:
                if int(row["http_status"]) != 200:
                    raise CrawlBlocked("robots_unavailable")
                return parse_robots(robots_url, body, row["charset"] or "utf-8")

        try:
            result = await fetcher.fetch_once(robots_url)
        except FetchError as error:
            raise CrawlBlocked("robots_unavailable") from error
        decision = detect_challenge(result.status, result.headers, result.body)
        response_id, sha256 = await self.repository.store_response(
            self.run_id,
            None,
            result,
            challenged=decision.challenged,
            challenge_reason=decision.reason,
        )
        if decision.challenged:
            raise CrawlBlocked(decision.reason or "cloudflare_challenge")
        if result.status != 200:
            raise CrawlBlocked("robots_unavailable")
        await self.repository.add_robots_snapshot(
            run_id=self.run_id,
            response_id=response_id,
            user_agent=user_agent,
            body_sha256=sha256,
        )
        return parse_robots(robots_url, result.body, result.charset or "utf-8")

    async def _worker(self, fetcher: FetchTransport, worker_id: str) -> None:
        while not self.stop_event.is_set():
            if not await self._reserve_page():
                return
            item = await self.repository.acquire_next(
                self.run_id,
                worker_id=worker_id,
                lease_seconds=self.config.lease_seconds,
                max_depth=self.config.max_depth,
            )
            if item is None:
                await self._unreserve_page()
                counts = await self.repository.queue_counts(self.run_id)
                if self.stop_event.is_set():
                    return
                if counts["in_progress"] > 0:
                    await asyncio.sleep(0.01)
                    continue
                if counts["pending"] > 0:
                    runnable = await self.repository.runnable_queue_count(
                        self.run_id,
                        max_depth=self.config.max_depth,
                    )
                    if runnable == 0:
                        await self.repository.set_run_status(self.run_id, "paused")
                        self.stop_event.set()
                        return
                    await asyncio.sleep(0.01)
                    continue
                return
            try:
                heartbeat = asyncio.create_task(self._lease_heartbeat(item))
                try:
                    await self._process_item(fetcher, item)
                finally:
                    heartbeat.cancel()
                    with suppress(asyncio.CancelledError, LeaseLostError):
                        await heartbeat
            except asyncio.CancelledError:
                await self.repository.release_in_progress(self.run_id, worker_id)
                raise
            except LeaseLostError:
                self.logger.event(
                    "page_lease_lost",
                    run_id=self.run_key,
                    queue_id=item.id,
                    url=redact_sensitive_url(item.requested_url),
                    status="lease_lost",
                    attempt=item.attempts,
                )
            except Exception as error:
                try:
                    await self._finish_item(item, "failed", error=str(error))
                except LeaseLostError:
                    continue
                self.logger.event(
                    "page_failed",
                    run_id=self.run_key,
                    queue_id=item.id,
                    url=redact_sensitive_url(item.requested_url),
                    status="failed",
                    attempt=item.attempts,
                )

    async def _lease_heartbeat(self, item: QueueItem) -> None:
        interval = max(1.0, self.config.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            if item.worker_id is None:
                return
            await self.repository.renew_queue_lease(
                item.id,
                worker_id=item.worker_id,
                fencing_token=item.fencing_token,
                lease_seconds=self.config.lease_seconds,
            )

    async def _finish_item(
        self,
        item: QueueItem,
        status: str,
        *,
        error: str | None = None,
        next_attempt_at: str | None = None,
    ) -> None:
        await self.repository.finish_queue(
            item.id,
            status,
            error=error,
            next_attempt_at=next_attempt_at,
            worker_id=item.worker_id,
            fencing_token=item.fencing_token,
        )

    async def _reserve_page(self) -> bool:
        async with self._counter_lock:
            if self.config.max_pages and self.processed >= self.config.max_pages:
                self.stop_event.set()
                return False
            self.processed += 1
            return True

    async def _unreserve_page(self) -> None:
        async with self._counter_lock:
            self.processed = max(0, self.processed - 1)

    async def _process_item(self, fetcher: FetchTransport, item: QueueItem) -> None:
        if self.robots and not self.robots.allows(
            self.config.user_agent or "*", item.requested_url
        ):
            await self._finish_item(item, "skipped_robots", error="robots disallow")
            return

        last_error: str | None = None
        for attempt in range(1, self.config.max_retries + 2):
            try:
                result = await fetcher.fetch_once(item.requested_url, attempt=attempt)
            except FetchError as error:
                last_error = redact_error(error, item.requested_url)
                if attempt <= self.config.max_retries:
                    delay = retry_delay(attempt)
                    self.logger.event(
                        "request_retry_scheduled",
                        run_id=self.run_key,
                        queue_id=item.id,
                        url=redact_sensitive_url(item.requested_url),
                        status="transport_error",
                        attempt=attempt,
                        delay_seconds=round(delay, 3),
                        error=last_error,
                    )
                    await asyncio.sleep(delay)
                    continue
                await self._finish_item(item, "failed", error=last_error)
                self.logger.event(
                    "request_retry_exhausted",
                    run_id=self.run_key,
                    queue_id=item.id,
                    url=redact_sensitive_url(item.requested_url),
                    status="failed",
                    attempt=attempt,
                    error=last_error,
                )
                return

            decision = detect_challenge(result.status, result.headers, result.body)
            retry_after = next(
                (value for key, value in result.headers.items() if key.casefold() == "retry-after"),
                None,
            )
            response_id, _ = await self.repository.store_response(
                self.run_id,
                item.id,
                result,
                challenged=decision.challenged,
                challenge_reason=decision.reason,
                worker_id=item.worker_id,
                fencing_token=item.fencing_token,
            )
            self.logger.event(
                "response_stored",
                run_id=self.run_key,
                queue_id=item.id,
                url=redact_sensitive_url(item.requested_url),
                status=result.status,
                attempt=attempt,
                elapsed_ms=result.elapsed_ms,
            )
            if decision.challenged:
                await self._finish_item(item, "challenged", error=decision.reason)
                await self.repository.set_run_status(
                    self.run_id,
                    "blocked",
                    blocked_reason=decision.reason or "cloudflare_challenge",
                    ended=True,
                )
                self.blocked_event.set()
                self.stop_event.set()
                self.logger.event(
                    "challenge_circuit_breaker_open",
                    run_id=self.run_key,
                    queue_id=item.id,
                    url=redact_sensitive_url(item.requested_url),
                    status="blocked",
                    attempt=attempt,
                    reason=decision.reason,
                )
                return
            if result.status in RETRYABLE_STATUS:
                last_error = f"HTTP {result.status}"
                if attempt <= self.config.max_retries:
                    delay = retry_delay(attempt, retry_after)
                    self.logger.event(
                        "request_retry_scheduled",
                        run_id=self.run_key,
                        queue_id=item.id,
                        url=redact_sensitive_url(item.requested_url),
                        status=result.status,
                        attempt=attempt,
                        delay_seconds=round(delay, 3),
                    )
                    await asyncio.sleep(delay)
                    continue
                await self._finish_item(item, "failed", error=last_error)
                self.logger.event(
                    "request_retry_exhausted",
                    run_id=self.run_key,
                    queue_id=item.id,
                    url=redact_sensitive_url(item.requested_url),
                    status=result.status,
                    attempt=attempt,
                )
                return
            if result.status == 429:
                delay = rate_limit_delay(attempt, retry_after)
                next_attempt = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
                await self._finish_item(
                    item,
                    "pending",
                    error="HTTP 429",
                    next_attempt_at=next_attempt,
                )
                await self.repository.set_run_status(self.run_id, "paused")
                self.stop_event.set()
                self.logger.event(
                    "source_rate_limited",
                    run_id=self.run_key,
                    queue_id=item.id,
                    url=redact_sensitive_url(item.requested_url),
                    status="paused",
                    attempt=attempt,
                    delay_seconds=round(delay, 3),
                    retry_after_present=retry_after is not None,
                )
                return
            if result.status in {404, 410}:
                await self._finish_item(item, "gone")
                return
            if result.status >= 400:
                await self._finish_item(item, "failed", error=f"HTTP {result.status}")
                return
            await self._parse_and_discover(item, response_id, result)
            return

    async def _parse_and_discover(
        self, item: QueueItem, response_id: int, result: FetchResult
    ) -> None:
        content_type = result.content_type or ""
        is_sitemap = (
            item.page_type_hint == "sitemap"
            or item.requested_url.endswith((".xml", ".xml.gz"))
            or "xml" in content_type
        )
        if is_sitemap:
            try:
                sitemap = parse_sitemap(result.body, compressed=item.requested_url.endswith(".gz"))
            except (ValueError, OSError, XMLSyntaxError) as error:
                await self.repository.add_parse_failure(response_id, "sitemap", "sitemap", error)
                await self._finish_item(item, "parse_failed", error=str(error))
                return
            for url in (*sitemap.nested_sitemaps, *sitemap.urls):
                if is_in_scope(url, self.seed_url):
                    await self.repository.enqueue(
                        self.run_id,
                        url,
                        parent_url=item.requested_url,
                        depth=item.depth + 1,
                        page_type_hint="sitemap" if url.endswith((".xml", ".xml.gz")) else None,
                        priority=80 if url.endswith((".xml", ".xml.gz")) else 0,
                        discovery_method="sitemap",
                        source_response_id=response_id,
                    )
            await self._finish_item(item, "done")
            return

        try:
            parsed = self.parser.parse(result.final_url, result.body, result.charset or "utf-8")
            for url in parsed.links:
                if is_in_scope(url, self.seed_url):
                    await self.repository.enqueue(
                        self.run_id,
                        url,
                        parent_url=item.requested_url,
                        depth=item.depth + 1,
                        discovery_method="html",
                        source_response_id=response_id,
                    )
            await self.ingest.ingest(
                run_id=self.run_id,
                response_id=response_id,
                source_url=result.final_url,
                parsed=parsed,
            )
        except ParseError as error:
            await self.repository.add_parse_failure(
                response_id, "catalog_parser", item.page_type_hint or "unknown", error
            )
            await self._finish_item(item, "parse_failed", error=str(error))
            return
        await self._finish_item(item, "done")

    async def _finalize(self) -> int:
        if self.blocked_event.is_set():
            return 2
        counts = await self.repository.queue_counts(self.run_id)
        if self.stop_event.is_set() and counts["pending"]:
            await self.repository.set_run_status(self.run_id, "paused")
            return 0
        has_gaps = any(
            counts[status] for status in ("failed", "challenged", "skipped_robots", "parse_failed")
        )
        status = "completed_with_gaps" if has_gaps else "completed"
        await self.repository.set_run_status(self.run_id, status, ended=True)
        return 1 if has_gaps else 0

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(signum, self.stop_event.set)
