"""Start the operator console: ``python -m pilgrim.tui`` (or ``make tui``)."""
from __future__ import annotations

import asyncio
from contextlib import suppress

from pilgrim.config import load_config
from pilgrim.tui.app import DjConsole
from pilgrim.tui.client import DjClient


def main() -> None:
    cfg = load_config()
    client = DjClient(cfg)
    app = DjConsole(cfg=cfg, client=client)
    try:
        app.run()
    finally:
        with suppress(Exception):
            asyncio.run(client.close())


if __name__ == "__main__":
    main()
