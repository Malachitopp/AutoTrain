"""Process type 1 of 4: the HTTP API (ARCHITECTURE §2).

One image, four entrypoints — this one hands the app factory to uvicorn.
Host and port come through Settings like every other piece of process
configuration, so the same image runs locally and in ECS unchanged.
"""

from __future__ import annotations

import logging
import uvicorn

from autotrain.core.config import get_settings


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    uvicorn.run(
        "autotrain.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
