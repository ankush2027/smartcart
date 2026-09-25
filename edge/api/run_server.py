"""Run with python -m edge.api.run_server; see docs/network_boundary.md.

Uses the existing Firebase initializer and app factory, then Waitress. Importing
this module neither initializes Firebase nor opens a listening socket.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    host = os.environ.get("SMARTCART_API_HOST", "0.0.0.0").strip()
    try:
        port = int(os.environ.get("SMARTCART_API_PORT", "5000"))
        if not host or not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        logger.error("Set a nonempty SMARTCART_API_HOST and SMARTCART_API_PORT from 1 to 65535.")
        raise SystemExit(1) from None

    from edge.firebase.client import initialize
    from edge.database.database import initialize_database
    from edge.api.server import create_app
    from waitress import serve

    try:
        initialize()
        initialize_database()
    except Exception:
        # SDK errors can contain credential paths/details; do not print them.
        logger.error("Startup failed. Check Firebase environment configuration and SQLite access.")
        raise SystemExit(1) from None

    app = create_app()
    app.config["DEBUG"] = False
    logger.info("Starting SmartCart pairing API on %s:%d", host, port)
    serve(app, host=host, port=port, threads=4, expose_tracebacks=False)


if __name__ == "__main__":
    main()
