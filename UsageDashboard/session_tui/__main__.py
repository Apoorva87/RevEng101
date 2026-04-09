"""Entry point: python -m session_tui"""

from __future__ import annotations

import argparse
from pathlib import Path

from .app import SessionBrowserApp


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Session Browser TUI")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.home() / ".claude",
        help="Claude root directory (default: ~/.claude)",
    )
    parser.add_argument(
        "-t", "--truncate",
        type=int,
        default=200,
        help="Max characters for message preview (default: 200)",
    )
    parser.add_argument(
        "-n", "--last-n",
        type=int,
        default=10,
        help="Number of recent records to show initially (default: 10)",
    )
    args = parser.parse_args()

    app = SessionBrowserApp(
        claude_root=args.root,
        truncate_at=args.truncate,
        last_n=args.last_n,
    )
    app.run()


if __name__ == "__main__":
    main()
