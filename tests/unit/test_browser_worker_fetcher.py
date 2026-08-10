from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path

from partsouq_crawler.crawl.browser_worker_fetcher import BrowserWorkerFetcher

FAKE_WORKER = r"""
import base64
import json
import os
import sys
from pathlib import Path

profile = Path(os.environ["PARTSOUQ_BROWSER_PROFILE_DIR"])
counter = profile / "starts"
starts = int(counter.read_text() if counter.exists() else "0") + 1
counter.write_text(str(starts))
print(json.dumps({"type": "ready", "user_agent": "fake-browser"}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request["type"] == "shutdown":
        break
    body = ("catalog:" + request["url"] + ":" + ("x" * 70000)).encode()
    print(json.dumps({
        "type": "result",
        "id": request["id"],
        "final_url": request["url"],
        "status": 200,
        "headers": {"content-type": "text/html; charset=utf-8"},
        "body_base64": base64.b64encode(body).decode(),
        "redirect_chain": [],
    }), flush=True)
"""


def test_browser_worker_fetcher_reads_large_result_and_restarts(tmp_path: Path) -> None:
    async def scenario() -> None:
        worker_path = tmp_path / "fake_worker.py"
        worker_path.write_text(FAKE_WORKER, encoding="utf-8")
        executable = tmp_path / "chrome"
        executable.write_text("fake", encoding="utf-8")
        profile = tmp_path / "profile"
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(worker_path))}"
        fetcher = BrowserWorkerFetcher(
            command=command,
            executable_path=executable,
            profile_dir=profile,
            timeout_seconds=2,
            challenge_wait_seconds=1,
            delay_seconds=0,
            restart_pages=1,
        )

        async with fetcher:
            first = await fetcher.fetch_once("https://example.test/one")
            second = await fetcher.fetch_once("https://example.test/two")

        assert first.status == 200
        assert first.body.startswith(b"catalog:https://example.test/one")
        assert len(first.body) > 65_536
        assert second.body.startswith(b"catalog:https://example.test/two")
        assert fetcher.user_agent == "fake-browser"
        assert (profile / "starts").read_text() == "2"

    asyncio.run(scenario())
