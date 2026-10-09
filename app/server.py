"""Start the API with Loguru configured before Uvicorn starts its server or reloader."""

import argparse

import uvicorn

from app.logging import UVICORN_LOG_CONFIG


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Start the API with unified Loguru logging")
    parser.add_argument("--host", default="127.0.0.1", help="API bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="API port (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="Reload the API when Python files change")
    parser.add_argument("--reload-dir", action="append", help="Directory to watch for reloads; may be repeated")
    parser.add_argument("--timeout-graceful-shutdown", type=int, help="Maximum seconds to wait for requests during shutdown")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.timeout_graceful_shutdown is not None and args.timeout_graceful_shutdown < 0:
        parser.error("--timeout-graceful-shutdown must not be negative")
    uvicorn.run(
        "main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=args.reload_dir,
        timeout_graceful_shutdown=args.timeout_graceful_shutdown,
        log_config=UVICORN_LOG_CONFIG,
    )


if __name__ == "__main__":
    main()
