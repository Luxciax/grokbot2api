#!/usr/bin/env python3
"""Windows desktop client entry for the local grokbot2api gateway."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_WINDOWS_DIR = Path(__file__).resolve().parent
_ROOT = _WINDOWS_DIR.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_WINDOWS_DIR) not in sys.path:
    sys.path.insert(0, str(_WINDOWS_DIR))

from windows import __version__
from windows.gateway_service import DEFAULT_HOST, DEFAULT_PORT


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="grokbot2api Windows desktop client")
    parser.add_argument("--host", default=DEFAULT_HOST, help="gateway listen address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="gateway listen port")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    from windows.tray_ui import ClientApp

    app = ClientApp(host=args.host, port=args.port)
    app.run()


if __name__ == "__main__":
    main()
