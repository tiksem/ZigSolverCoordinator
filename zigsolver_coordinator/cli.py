"""`zigsolver-coordinator` — serve the coordinator.

    zigsolver-coordinator                       # ws://127.0.0.1:8765
    zigsolver-coordinator --host 0.0.0.0        # reachable from the LAN
    zigsolver-coordinator --origin http://box:4173 --origin http://localhost:5173
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os

from . import __version__
from .server import run

DEFAULT_PORT = 8765
DEFAULT_DATA_DIR = "~/.zigsolver-coordinator"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="zigsolver-coordinator",
        description="Prepares every table snapshot for ZigSolver and keeps the settings. "
                    "ZigSolverFront connects to it over one WebSocket.")
    ap.add_argument("--host", default="127.0.0.1",
                    help="interface to listen on (default 127.0.0.1; 0.0.0.0 for the LAN)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    ap.add_argument("--data-dir", default=os.environ.get("ZSC_DATA_DIR", DEFAULT_DATA_DIR),
                    help=f"where the settings and the solve history live (default {DEFAULT_DATA_DIR}, "
                         "or $ZSC_DATA_DIR)")
    ap.add_argument("--origin", action="append", default=None, metavar="URL",
                    help="accept front ends served from this origin only (repeatable; "
                         "default: any). Guards against another page in the same browser "
                         "driving the tables.")
    ap.add_argument("--linger", type=float, default=15.0, metavar="SECONDS",
                    help="how long a table keeps its socket and its hand after the last "
                         "front end leaves it (default 15)")
    ap.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    # Both log every request at INFO, which buries what this process has to say.
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, logging.getLogger().level))

    try:
        asyncio.run(run(host=args.host, port=args.port, data_dir=args.data_dir,
                        origins=args.origin, linger=args.linger))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
