"""Command-line entry point for an installed Codex Partner package."""

from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

from . import APP_VERSION


def main() -> None:
    parser = argparse.ArgumentParser(prog="codex-partner", description="Start the Codex Partner web service.")
    parser.add_argument("--host", help="Override CODEX_DASHBOARD_HOST.")
    parser.add_argument("--port", type=int, help="Override CODEX_DASHBOARD_PORT.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    args = parser.parse_args()

    # An installed CLI reads configuration from the directory it is launched in.
    load_dotenv(Path.cwd() / ".env", override=False)

    import uvicorn

    from app import DASHBOARD_HOST, DASHBOARD_PORT, app

    host = args.host or DASHBOARD_HOST
    port = args.port or DASHBOARD_PORT
    if not 1 <= port <= 65535:
        parser.error("port must be between 1 and 65535")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
