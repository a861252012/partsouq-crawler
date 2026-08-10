from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import nodriver as uc
import websockets

CHALLENGE_TITLES = ("just a moment", "請稍候")
CHALLENGE_TEXT = (
    "enable javascript and cookies to continue",
    "verify you are human",
    "驗證您是人類",
    "正在執行安全驗證",
)


@dataclass(slots=True)
class DocumentResponse:
    request_id: str
    url: str
    status: int
    headers: dict[str, str]
    raw_body: bytes | None = None
    body_error: str | None = None
    body_ready: asyncio.Event = field(default_factory=asyncio.Event)


class RawCDPSession:
    """A target-scoped CDP client used only to preserve raw document bodies."""

    def __init__(self, websocket_url: str) -> None:
        self.websocket_url = websocket_url
        self.websocket: Any = None
        self.listener: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[dict[str, object]]] = {}
        self.capture_tasks: set[asyncio.Task[None]] = set()
        self.responses: list[DocumentResponse] = []
        self.responses_by_id: dict[str, DocumentResponse] = {}
        self.next_id = 0
        self.main_frame_id = ""

    async def start(self) -> None:
        self.websocket = await websockets.connect(
            self.websocket_url,
            max_size=64 * 1024 * 1024,
            ping_timeout=30,
        )
        self.listener = asyncio.create_task(self._listen())
        await self.send("Network.enable", {"maxTotalBufferSize": 64 * 1024 * 1024})
        frame_result = await self.send("Page.getFrameTree", {})
        frame_tree = frame_result.get("frameTree", {})
        frame = frame_tree.get("frame", {}) if isinstance(frame_tree, dict) else {}
        self.main_frame_id = str(frame.get("id", "")) if isinstance(frame, dict) else ""

    async def close(self) -> None:
        for task in self.capture_tasks:
            task.cancel()
        if self.capture_tasks:
            await asyncio.gather(*self.capture_tasks, return_exceptions=True)
        if self.websocket is not None:
            await self.websocket.close()
        if self.listener is not None:
            self.listener.cancel()
            await asyncio.gather(self.listener, return_exceptions=True)
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("raw CDP session closed"))
        self.pending.clear()

    def begin_fetch(self) -> None:
        self.responses.clear()
        self.responses_by_id.clear()

    async def send(self, method: str, params: dict[str, object]) -> dict[str, object]:
        if self.websocket is None:
            raise RuntimeError("raw CDP session is not connected")
        self.next_id += 1
        message_id = self.next_id
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self.pending[message_id] = future
        await self.websocket.send(
            json.dumps({"id": message_id, "method": method, "params": params})
        )
        result = await future
        if "error" in result:
            raise RuntimeError(str(result["error"]))
        value = result.get("result", {})
        return value if isinstance(value, dict) else {}

    async def _listen(self) -> None:
        assert self.websocket is not None
        async for raw in self.websocket:
            message = json.loads(raw)
            message_id = message.get("id")
            if isinstance(message_id, int):
                future = self.pending.pop(message_id, None)
                if future is not None and not future.done():
                    future.set_result(message)
                continue
            method = message.get("method")
            params = message.get("params", {})
            if not isinstance(params, dict):
                continue
            if method == "Network.responseReceived":
                self._record_response(params)
            elif method == "Network.loadingFinished":
                request_id = str(params.get("requestId", ""))
                if request_id in self.responses_by_id:
                    task = asyncio.create_task(self._capture_body(request_id))
                    self.capture_tasks.add(task)
                    task.add_done_callback(self.capture_tasks.discard)

    def _record_response(self, params: dict[str, object]) -> None:
        if params.get("type") != "Document" or str(params.get("frameId", "")) != self.main_frame_id:
            return
        raw_response = params.get("response")
        if not isinstance(raw_response, dict):
            return
        raw_headers = raw_response.get("headers", {})
        headers = (
            {str(key).lower(): str(value) for key, value in raw_headers.items()}
            if isinstance(raw_headers, dict)
            else {}
        )
        request_id = str(params.get("requestId", ""))
        response = DocumentResponse(
            request_id=request_id,
            url=str(raw_response.get("url", "")),
            status=int(raw_response.get("status", 0)),
            headers=headers,
        )
        self.responses.append(response)
        self.responses_by_id[request_id] = response

    async def _capture_body(self, request_id: str) -> None:
        response = self.responses_by_id.get(request_id)
        if response is None:
            return
        try:
            result = await self.send("Network.getResponseBody", {"requestId": request_id})
            body = str(result.get("body", ""))
            response.raw_body = (
                base64.b64decode(body) if result.get("base64Encoded") else body.encode("utf-8")
            )
        except Exception as error:
            response.body_error = f"{type(error).__name__}: {error}"
        finally:
            response.body_ready.set()


class BrowserWorker:
    def __init__(self) -> None:
        executable = os.environ.get("PARTSOUQ_BROWSER_EXECUTABLE", "")
        profile = os.environ.get("PARTSOUQ_BROWSER_PROFILE_DIR", "")
        if not executable or not profile:
            raise ValueError("browser executable and profile directory are required")
        self.executable = Path(executable)
        self.profile_dir = Path(profile)
        self.challenge_wait_seconds = float(
            os.environ.get("PARTSOUQ_BROWSER_CHALLENGE_WAIT_SECONDS", "60")
        )
        self.request_timeout_seconds = float(
            os.environ.get("PARTSOUQ_BROWSER_REQUEST_TIMEOUT_SECONDS", "30")
        )
        self.sandbox = os.environ.get("PARTSOUQ_BROWSER_SANDBOX", "1").lower() not in {
            "0",
            "false",
            "no",
        }
        self.session_id = uuid.uuid4().hex
        self.browser: Any = None
        self.page: Any = None
        self.capture: RawCDPSession | None = None

    async def start(self) -> str:
        if not self.executable.is_file():
            raise FileNotFoundError(f"browser executable not found: {self.executable}")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.browser = await uc.start(
            headless=False,
            browser_executable_path=self.executable,
            user_data_dir=self.profile_dir,
            sandbox=self.sandbox,
        )
        self.page = await self.browser.get("about:blank")
        targets = await self.browser._http.get("list")
        target_id = str(self.page.target.target_id)
        target = next(item for item in targets if str(item.get("id")) == target_id)
        self.capture = RawCDPSession(str(target["webSocketDebuggerUrl"]))
        await self.capture.start()
        return str(await self._evaluate("navigator.userAgent"))

    async def stop(self) -> None:
        if self.capture is not None:
            await self.capture.close()
            self.capture = None
        if self.browser is not None:
            self.browser.stop()
            self.browser = None

    async def fetch(self, url: str, attempt: int) -> dict[str, object]:
        if self.page is None:
            raise RuntimeError("browser page is not initialized")
        if self.capture is None:
            raise RuntimeError("raw CDP capture is not initialized")
        self.capture.begin_fetch()
        self._log("fetch_started", url_sha256=hashlib.sha256(url.encode()).hexdigest())
        await asyncio.wait_for(self.page.get(url), timeout=self.request_timeout_seconds)
        html, challenge_active = await self._wait_for_final_document()
        if not self.capture.responses:
            raise RuntimeError("no document response was observed")
        final = self.capture.responses[-1]
        body, body_kind = await self._response_body(final, html)
        challenge_observed = any(
            response.headers.get("cf-mitigated", "").lower() == "challenge"
            for response in self.capture.responses
        )
        headers = dict(final.headers)
        headers.update(
            {
                "x-partsouq-collector": "nodriver/0.50.3",
                "x-partsouq-collector-session": self.session_id,
                "x-partsouq-body-kind": body_kind,
                "x-partsouq-challenge-observed": str(challenge_observed).lower(),
                "x-partsouq-final-challenge-active": str(challenge_active).lower(),
            }
        )
        final_url = str(await self._evaluate("location.href"))
        redirect_chain = [
            response.url for response in self.capture.responses[:-1] if response.url != final.url
        ]
        self._log(
            "fetch_finished",
            status=final.status,
            response_bytes=len(body),
            challenge_observed=challenge_observed,
            challenge_active=challenge_active,
        )
        return {
            "type": "result",
            "final_url": final_url,
            "status": final.status,
            "headers": headers,
            "body_base64": base64.b64encode(body).decode("ascii"),
            "attempt": attempt,
            "redirect_chain": redirect_chain,
        }

    async def _wait_for_final_document(self) -> tuple[str, bool]:
        deadline = asyncio.get_running_loop().time() + self.challenge_wait_seconds
        last_digest: str | None = None
        stable_since: float | None = None
        latest_html = ""
        latest_challenge = True
        while True:
            await asyncio.sleep(0.5)
            try:
                latest_html = await self.page.get_content()
                title = str(await self._evaluate("document.title")).strip().lower()
                ready_state = str(await self._evaluate("document.readyState"))
                visible = str(
                    await self._evaluate("document.body ? document.body.innerText : ''")
                ).lower()
            except Exception:
                if asyncio.get_running_loop().time() >= deadline:
                    raise
                continue
            response = self.capture.responses[-1] if self.capture.responses else None
            latest_challenge = bool(
                title.startswith(CHALLENGE_TITLES)
                or any(marker in visible for marker in CHALLENGE_TEXT)
                or (
                    response is not None
                    and response.headers.get("cf-mitigated", "").lower() == "challenge"
                )
            )
            response_ok = response is not None and response.status < 400
            now = asyncio.get_running_loop().time()
            if (
                response_ok
                and not latest_challenge
                and ready_state == "complete"
                and len(latest_html) >= 200
            ):
                digest = hashlib.sha256(latest_html.encode()).hexdigest()
                if digest != last_digest:
                    last_digest = digest
                    stable_since = now
                elif stable_since is not None and now - stable_since >= 3:
                    return latest_html, False
            else:
                stable_since = None
                last_digest = None
            if now >= deadline:
                return latest_html, latest_challenge

    async def _response_body(
        self, response: DocumentResponse, rendered_html: str
    ) -> tuple[bytes, str]:
        if response.raw_body is not None:
            return response.raw_body, "raw-http"
        with suppress(TimeoutError):
            await asyncio.wait_for(response.body_ready.wait(), timeout=5)
        if response.raw_body is not None:
            return response.raw_body, "raw-http"
        if response.body_error:
            self._log(
                "raw_body_unavailable",
                error=response.body_error[:500],
            )
        return rendered_html.encode("utf-8"), "rendered-dom"

    async def _evaluate(self, expression: str) -> object:
        value = await self.page.evaluate(expression, return_by_value=True)
        return getattr(value, "value", value)

    def _log(self, event: str, **fields: object) -> None:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "component": "partsouq_browser_worker",
            "event": event,
            "session_id": self.session_id,
            **fields,
        }
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


async def run() -> None:
    worker = BrowserWorker()
    try:
        user_agent = await worker.start()
        print(
            json.dumps(
                {
                    "type": "ready",
                    "user_agent": user_agent,
                    "session_id": worker.session_id,
                }
            ),
            flush=True,
        )
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            request: dict[str, object] = json.loads(line)
            if request.get("type") == "shutdown":
                break
            request_id = request.get("id")
            try:
                result = await worker.fetch(str(request["url"]), int(request.get("attempt", 1)))
                result["id"] = request_id
                print(json.dumps(result), flush=True)
            except Exception as error:
                print(
                    json.dumps(
                        {
                            "type": "error",
                            "id": request_id,
                            "error": f"{type(error).__name__}: {error}",
                        }
                    ),
                    flush=True,
                )
    finally:
        await worker.stop()


if __name__ == "__main__":
    uc.loop().run_until_complete(run())
