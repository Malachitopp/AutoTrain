"""Process type 1 of 4: the HTTP API (ARCHITECTURE §2).

One image, four entrypoints — this one hands the app factory to uvicorn.
Host and port come through Settings like every other piece of process
configuration, so the same image runs locally and in ECS unchanged.
"""

from __future__ import annotations

import uvicorn

from autotrain.core.config import get_settings
from autotrain.core.observability import setup_logging


def main() -> None:
    settings = get_settings()
    setup_logging("api", level=settings.log_level, fmt=settings.log_format)
    uvicorn.run(
        "autotrain.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        # RequestIdMiddleware writes the one line per request, with the id.
        access_log=False,
    )


if __name__ == "__main__":
    main()
