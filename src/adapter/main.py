"""Adapter service: Chrome driver + job runner + API on 127.0.0.1 (spec §7)."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import uvicorn

from adapter.api import AdapterService, create_app
from adapter.driver import ChromeDriver
from adapter.runner import JobRunner


async def run() -> None:
    service: AdapterService | None = None

    def on_crash(detail: str) -> None:
        # a crashed or closed page cannot recover in-process; exit so the container restarts with a fresh Chrome
        logging.getLogger("adapter").error("%s: exiting for a restart", detail)
        if service is not None:
            service.mark_error(detail)
        os._exit(1)

    driver = ChromeDriver(os.environ["SPOTIFY_SP_DC"], Path(os.environ.get("ADAPTER_PROFILE", "/profile")), on_crash)
    await driver.start()
    runner = JobRunner(driver, asyncio.sleep)
    service = AdapterService(driver, runner, driver.logged_in, driver.token, driver.refresh_token)
    app = create_app(service, os.environ["FEEDBACK_API_TOKEN"], background=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=int(os.environ.get("ADAPTER_PORT", "8791")),
                                           log_level="info"))
    try:
        await server.serve()
    finally:
        await driver.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if os.environ.get("ADAPTER_DEBUG"):
        logging.getLogger("adapter").setLevel(logging.DEBUG)
    asyncio.run(run())


if __name__ == "__main__":
    main()
