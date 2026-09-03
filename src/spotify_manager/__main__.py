"""Entry point for both `spotify-manager` and `python -m spotify_manager`."""

from __future__ import annotations

import sys

from .cli import main as _main


def main() -> int:
    return _main()


if __name__ == "__main__":
    sys.exit(main())
