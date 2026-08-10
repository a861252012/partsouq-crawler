from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
from pathlib import Path
from time import monotonic

from partsouq_crawler.crawl.fetcher import FetchError
from partsouq_crawler.crawl.rate_limit import HostRateLimiter
from partsouq_crawler.models.crawl import FetchResult


class BrowserWorkerFetcher:
    """Fetch through an isolated, line-delimited JSON browser worker process."""

    def __init__(
        self,
        *,
        command: str,
        executable_path: Path,
        profile_dir: Path,
        timeout_seconds: float,
        challenge_wait_seconds: float,
        delay_seconds: float,
        restart_pages: int,
    ) -> None:
        self.command = tuple(shlex.split(command))
        self.executable_path = executable_path
        self.profile_dir = profile_dir
        self.timeout_seconds = timeout_seconds
        self.challenge_wait_seconds = challenge_wait_seconds
        self.rate_limiter = HostRateLimiter(delay_seconds)
        self.restart_pages = restart_pages
        self.process: asyncio.subprocess.Process | None = None
        self.user_agent = ""
        self._request_id = 0
        self._page_count = 0

    async def __aenter__(self) -> BrowserWorkerFetcher:
        if not self.command:
            raise FetchError("browser worker command is empty")
        if not self.executable_path.is_file():
            raise FetchError(f"browser executable not found: {self.executable_path}")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        await self._start_worker()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._stop_worker()

    async def fetch_once(self, url: str, *, attempt: int = 1) -> FetchResult:
        await self.rate_limiter.wait()
        if self._page_count >= self.restart_pages:
            await self._stop_worker()
        await self._ensure_worker()
        process = self._required_process()
        if process.stdin is None or process.stdout is None:
            raise FetchError("browser worker pipes are unavailable")

        self._request_id += 1
        request_id = self._request_id
        request = {"type": "fetch", "id": request_id, "url": url, "attempt": attempt}
        started = monotonic()
        try:
            process.stdin.write((json.dumps(request) + "\n").encode())
            await process.stdin.drain()
            timeout = self.timeout_seconds + self.challenge_wait_seconds + 30
            raw = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
        except (BrokenPipeError, ConnectionError, TimeoutError) as error:
            await self._stop_worker(force=True)
            raise FetchError(f"browser worker failed: {type(error).__name__}: {error}") from error
        if not raw:
            return_code = await process.wait()
            self.process = None
            raise FetchError(f"browser worker exited unexpectedly: {return_code}")

        try:
            response = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FetchError("browser worker returned invalid JSON") from error
        if response.get("id") != request_id:
            raise FetchError("browser worker response id mismatch")
        if response.get("type") == "error":
            raise FetchError(str(response.get("error") or "browser worker fetch failed"))
        if response.get("type") != "result":
            raise FetchError("browser worker returned an unsupported message")

        try:
            body = base64.b64decode(str(response["body_base64"]), validate=True)
            headers = {str(key): str(value) for key, value in response["headers"].items()}
            result = FetchResult(
                requested_url=url,
                final_url=str(response["final_url"]),
                status=int(response["status"]),
                headers=headers,
                body=body,
                elapsed_ms=round((monotonic() - started) * 1000),
                attempt=attempt,
                redirect_chain=tuple(str(item) for item in response.get("redirect_chain", ())),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise FetchError("browser worker result is incomplete") from error
        self._page_count += 1
        return result

    async def _ensure_worker(self) -> None:
        if self.process is None or self.process.returncode is not None:
            await self._start_worker()

    async def _start_worker(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "PARTSOUQ_BROWSER_EXECUTABLE": str(self.executable_path),
                "PARTSOUQ_BROWSER_PROFILE_DIR": str(self.profile_dir),
                "PARTSOUQ_BROWSER_CHALLENGE_WAIT_SECONDS": str(self.challenge_wait_seconds),
                "PARTSOUQ_BROWSER_REQUEST_TIMEOUT_SECONDS": str(self.timeout_seconds),
            }
        )
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=None,
                env=environment,
                limit=64 * 1024 * 1024,
            )
        except OSError as error:
            raise FetchError(f"cannot start browser worker: {error}") from error
        process = self._required_process()
        if process.stdout is None:
            raise FetchError("browser worker stdout is unavailable")
        try:
            raw = await asyncio.wait_for(process.stdout.readline(), timeout=30)
            ready = json.loads(raw)
        except (TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
            await self._stop_worker(force=True)
            raise FetchError("browser worker did not become ready") from error
        if ready.get("type") != "ready":
            await self._stop_worker(force=True)
            raise FetchError(str(ready.get("error") or "browser worker startup failed"))
        self.user_agent = str(ready.get("user_agent") or "")
        self._page_count = 0

    async def _stop_worker(self, *, force: bool = False) -> None:
        process, self.process = self.process, None
        if process is None or process.returncode is not None:
            return
        if not force and process.stdin is not None:
            try:
                process.stdin.write(b'{"type":"shutdown"}\n')
                await process.stdin.drain()
                await asyncio.wait_for(process.wait(), timeout=10)
                return
            except (BrokenPipeError, ConnectionError, TimeoutError):
                pass
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.wait()

    def _required_process(self) -> asyncio.subprocess.Process:
        if self.process is None:
            raise FetchError("browser worker is not running")
        return self.process
