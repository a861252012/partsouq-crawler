from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

from partsouq_crawler.crawl.browser_fetcher import BrowserFetcher


class FakeRequest:
    resource_type = "document"

    def __init__(self, url: str, redirected_from: FakeRequest | None = None) -> None:
        self.url = url
        self.redirected_from = redirected_from

    def is_navigation_request(self) -> bool:
        return True


class FakeResponse:
    def __init__(
        self,
        *,
        url: str,
        status: int,
        body: bytes,
        headers: dict[str, str],
        redirected_from: str | None = None,
    ) -> None:
        self.url = url
        self.status = status
        self._body = body
        self._headers = headers
        previous = FakeRequest(redirected_from) if redirected_from else None
        self.request = FakeRequest(url, previous)
        self.frame: object | None = None

    async def body(self) -> bytes:
        return self._body

    async def all_headers(self) -> dict[str, str]:
        return dict(self._headers)


class FakePage:
    def __init__(self, responses: list[FakeResponse], *, followup_delay: float = 0) -> None:
        self.responses = responses
        self.followup_delay = followup_delay
        self.main_frame = object()
        self.listener: Any = None
        for response in responses:
            response.frame = self.main_frame

    def on(self, event: str, listener: Any) -> None:
        assert event == "response"
        self.listener = listener

    def remove_listener(self, event: str, listener: Any) -> None:
        assert event == "response"
        assert self.listener is listener
        self.listener = None

    async def goto(self, url: str, *, wait_until: str) -> FakeResponse:
        assert url == "https://example.test/start"
        assert wait_until == "domcontentloaded"
        assert self.listener is not None
        self.listener(self.responses[0])
        for index, response in enumerate(self.responses[1:], start=1):
            asyncio.create_task(self._emit_after(response, self.followup_delay * index))
        return self.responses[0]

    async def _emit_after(self, response: FakeResponse, delay: float) -> None:
        await asyncio.sleep(delay)
        if self.listener is not None:
            self.listener(response)


def make_fetcher(page: FakePage, *, challenge_wait_seconds: float = 1) -> BrowserFetcher:
    fetcher = BrowserFetcher(
        timeout_seconds=3,
        delay_seconds=0,
        executable_path=Path("/not-used-by-fetch-once"),
        headless=True,
        user_agent=None,
        challenge_wait_seconds=challenge_wait_seconds,
    )
    fetcher.page = cast(Any, page)
    return fetcher


def test_browser_fetcher_returns_raw_document_response() -> None:
    async def scenario() -> None:
        response = FakeResponse(
            url="https://example.test/final",
            status=200,
            body=b"<html>catalog</html>",
            headers={"content-type": "text/html; charset=utf-8"},
            redirected_from="https://example.test/start",
        )
        result = await make_fetcher(FakePage([response])).fetch_once("https://example.test/start")

        assert result.final_url == "https://example.test/final"
        assert result.status == 200
        assert result.body == b"<html>catalog</html>"
        assert result.content_type == "text/html"
        assert result.redirect_chain == ("https://example.test/start",)
        assert "x-partsouq-challenge-observed" not in result.headers

    asyncio.run(scenario())


def test_browser_fetcher_waits_for_automatic_challenge_navigation() -> None:
    async def scenario() -> None:
        challenged = FakeResponse(
            url="https://example.test/start",
            status=403,
            body=b"<html><title>Just a moment</title></html>",
            headers={"server": "cloudflare", "cf-mitigated": "challenge"},
        )
        resolved = FakeResponse(
            url="https://example.test/final",
            status=200,
            body=b"<html>catalog</html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )
        page = FakePage([challenged, resolved], followup_delay=0.01)

        result = await make_fetcher(page, challenge_wait_seconds=0.2).fetch_once(
            "https://example.test/start"
        )

        assert result.status == 200
        assert result.final_url == "https://example.test/final"
        assert result.body == b"<html>catalog</html>"
        assert result.headers["x-partsouq-challenge-observed"] == "true"
        assert result.headers["x-partsouq-final-challenge-active"] == "false"
        assert result.redirect_chain == ("https://example.test/start",)

    asyncio.run(scenario())


def test_browser_fetcher_returns_unresolved_challenge_after_timeout() -> None:
    async def scenario() -> None:
        challenged = FakeResponse(
            url="https://example.test/start",
            status=403,
            body=b"<html><title>Just a moment</title></html>",
            headers={"server": "cloudflare", "cf-mitigated": "challenge"},
        )

        result = await make_fetcher(FakePage([challenged]), challenge_wait_seconds=0.01).fetch_once(
            "https://example.test/start"
        )

        assert result.status == 403
        assert result.headers["x-partsouq-challenge-observed"] == "true"
        assert result.headers["x-partsouq-final-challenge-active"] == "true"

    asyncio.run(scenario())
