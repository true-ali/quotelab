"""Download the Binance USD-M archives named in the config. Same as ``quotelab download``.

Usage: ``uv run python scripts/download_data.py [--config PATH] [--dry-run] [--allow-large]``
"""

import sys

from quotelab.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["download", *sys.argv[1:]]))
