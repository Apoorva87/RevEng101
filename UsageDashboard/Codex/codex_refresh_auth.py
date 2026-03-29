#!/usr/bin/env python3
"""Refresh Codex ChatGPT auth tokens stored in ~/.codex/auth.json."""

from __future__ import annotations

import argparse
import json
import os
import warnings
from datetime import datetime, timezone
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL 1\.1\.1\+",
)

import requests


DEFAULT_AUTH_FILE = Path.home() / ".codex" / "auth.json"
TOKEN_ENDPOINT = "https://auth0.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh the Codex ChatGPT access token using the stored refresh token."
    )
    parser.add_argument(
        "--auth-file",
        type=Path,
        default=DEFAULT_AUTH_FILE,
        help=f"Path to auth.json. Default: {DEFAULT_AUTH_FILE}",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Print the inferred refresh request details without making the network request.",
    )
    return parser.parse_args()


def load_auth(auth_file: Path) -> dict:
    return json.loads(auth_file.expanduser().read_text())


def write_auth(auth_file: Path, payload: dict) -> None:
    auth_file = auth_file.expanduser()
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text(json.dumps(payload, indent=2) + os.linesep)


def main() -> int:
    args = parse_args()
    auth_file = args.auth_file.expanduser()
    auth = load_auth(auth_file)
    tokens = auth.get("tokens") or {}
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    account_id = str(tokens.get("account_id") or "").strip()

    if not refresh_token:
        raise SystemExit(f"No refresh_token found in {auth_file}")

    if args.print_only:
        print(json.dumps(
            {
                "auth_file": str(auth_file),
                "token_endpoint": TOKEN_ENDPOINT,
                "client_id": CODEX_CLIENT_ID,
                "grant_type": "refresh_token",
                "account_id": account_id,
            },
            indent=2,
        ))
        return 0

    response = requests.post(
        TOKEN_ENDPOINT,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CODEX_CLIENT_ID,
        },
        timeout=20.0,
    )
    response.raise_for_status()
    token_payload = response.json()

    access_token = str(token_payload.get("access_token") or "").strip()
    if not access_token:
        raise SystemExit("Refresh response did not include access_token")

    tokens["access_token"] = access_token
    if account_id:
        tokens["account_id"] = account_id

    new_refresh_token = str(token_payload.get("refresh_token") or "").strip()
    if new_refresh_token:
        tokens["refresh_token"] = new_refresh_token

    new_id_token = str(token_payload.get("id_token") or "").strip()
    if new_id_token:
        tokens["id_token"] = new_id_token

    auth["tokens"] = tokens
    auth["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    write_auth(auth_file, auth)

    print(json.dumps(
        {
            "status": "ok",
            "auth_file": str(auth_file),
            "account_id": tokens.get("account_id"),
            "token_endpoint": TOKEN_ENDPOINT,
            "refresh_token_rotated": bool(new_refresh_token),
        },
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
