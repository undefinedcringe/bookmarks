"""Entrypoint: validate configuration first, then start the HTTP server."""

import sys

import uvicorn

from app.config import ConfigError, load_settings


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"FATAL: {exc}", file=sys.stderr, flush=True)
        sys.exit(2)

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        log_level=settings.log_level,
        access_log=settings.log_level == "debug" or settings.log_level == "info",
    )


if __name__ == "__main__":
    main()
