from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from time import monotonic

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Response,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)

from partsouq_crawler.crawl.challenge import detect_challenge
from partsouq_crawler.crawl.fetcher import FetchError
from partsouq_crawler.crawl.rate_limit import HostRateLimiter
from partsouq_crawler.models.crawl import FetchResult


class BrowserFetcher:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        delay_seconds: float,
        executable_path: Path | None,
        headless: bool,
        user_agent: str | None,
        challenge_wait_seconds: float,
    ) -> None:
        self.timeout_ms = timeout_seconds * 1000
        self.challenge_wait_seconds = challenge_wait_seconds
        self.rate_limiter = HostRateLimiter(delay_seconds)
        self.executable_path = executable_path
        self.headless = headless
        self.configured_user_agent = user_agent
        self.user_agent = user_agent or ""
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    async def __aenter__(self) -> BrowserFetcher:
        if self.executable_path is not None and not self.executable_path.is_file():
            raise FetchError(f"browser executable not found: {self.executable_path}")
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                executable_path=str(self.executable_path) if self.executable_path else None,
                headless=self.headless,
            )
            if self.configured_user_agent:
                self.context = await self.browser.new_context(user_agent=self.configured_user_agent)
            else:
                self.context = await self.browser.new_context()
            self.page = await self.context.new_page()
            self.page.set_default_navigation_timeout(self.timeout_ms)
            if not self.user_agent:
                self.user_agent = str(await self.page.evaluate("navigator.userAgent"))
            return self
        except PlaywrightError as error:
            await self.__aexit__()
            raise FetchError(f"{type(error).__name__}: {error}") from error

    async def __aexit__(self, *_: object) -> None:
        if self.page is not None:
            with suppress(PlaywrightError):
                await self.page.close()
        if self.context is not None:
            with suppress(PlaywrightError):
                await self.context.close()
        if self.browser is not None:
            with suppress(PlaywrightError):
                await self.browser.close()
        if self.playwright is not None:
            with suppress(PlaywrightError):
                await self.playwright.stop()

    async def fetch_once(self, url: str, *, attempt: int = 1) -> FetchResult:
        if self.page is None:
            raise RuntimeError("browser fetcher must be used as an async context manager")
        await self.rate_limiter.wait()
        started = monotonic()
        page = self.page
        documents: list[Response] = []

        def record_document(candidate: Response) -> None:
            if candidate.frame == page.main_frame and candidate.request.is_navigation_request():
                documents.append(candidate)

        page.on("response", record_document)
        try:
            initial_response = await page.goto(url, wait_until="domcontentloaded")
            if initial_response is None:
                raise FetchError("browser navigation returned no document response")
            if not any(candidate is initial_response for candidate in documents):
                documents.append(initial_response)
            body, headers = await self._body_and_headers(initial_response)
            response, body, headers, challenge_observed = await self._wait_for_challenge_resolution(
                initial_response,
                documents,
                body,
                headers,
            )
            redirect_chain = list(self._redirect_chain(response))
            if (
                challenge_observed
                and initial_response.url != response.url
                and initial_response.url not in redirect_chain
            ):
                redirect_chain.insert(0, initial_response.url)
            elapsed_ms = round((monotonic() - started) * 1000)
            return FetchResult(
                requested_url=url,
                final_url=response.url,
                status=response.status,
                headers=headers,
                body=body,
                elapsed_ms=elapsed_ms,
                attempt=attempt,
                redirect_chain=tuple(redirect_chain),
            )
        except FetchError:
            raise
        except PlaywrightError as error:
            raise FetchError(f"{type(error).__name__}: {error}") from error
        finally:
            page.remove_listener("response", record_document)

    async def _wait_for_challenge_resolution(
        self,
        initial_response: Response,
        documents: list[Response],
        initial_body: bytes,
        initial_headers: dict[str, str],
    ) -> tuple[Response, bytes, dict[str, str], bool]:
        initial_decision = detect_challenge(
            initial_response.status,
            initial_headers,
            initial_body,
        )
        if not initial_decision.challenged:
            return initial_response, initial_body, initial_headers, False

        started = monotonic()
        deadline = started + self.challenge_wait_seconds
        seen = {id(initial_response)}
        latest_response = initial_response
        latest_body = initial_body
        latest_headers = initial_headers
        latest_challenged = True
        redirect_statuses = {301, 302, 303, 307, 308}

        while monotonic() < deadline:
            for candidate in tuple(documents):
                if id(candidate) in seen:
                    continue
                seen.add(id(candidate))
                try:
                    body, headers = await self._body_and_headers(candidate)
                except PlaywrightError:
                    continue
                latest_response = candidate
                latest_body = body
                latest_headers = headers
                latest_challenged = detect_challenge(
                    candidate.status,
                    headers,
                    body,
                ).challenged
                if not latest_challenged and candidate.status not in redirect_statuses:
                    break
            if not latest_challenged and latest_response.status not in redirect_statuses:
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.1, remaining))

        headers = dict(latest_headers)
        headers["x-partsouq-challenge-observed"] = "true"
        headers["x-partsouq-final-challenge-active"] = str(latest_challenged).lower()
        headers["x-partsouq-challenge-wait-ms"] = str(round((monotonic() - started) * 1000))
        return latest_response, latest_body, headers, True

    @staticmethod
    async def _body_and_headers(response: Response) -> tuple[bytes, dict[str, str]]:
        return await response.body(), await response.all_headers()

    @staticmethod
    def _redirect_chain(response: Response) -> tuple[str, ...]:
        chain: list[str] = []
        request = response.request.redirected_from
        while request is not None:
            chain.append(request.url)
            request = request.redirected_from
        chain.reverse()
        return tuple(chain)
