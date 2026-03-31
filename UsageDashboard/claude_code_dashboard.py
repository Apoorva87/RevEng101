#!/usr/bin/env python3
"""Compatibility shim for the renamed Claude sessions dashboard."""

from claude_sessions_dashboard import main


if __name__ == "__main__":
    raise SystemExit(main())
