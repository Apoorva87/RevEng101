# codex-nerve

`codex-nerve` is a lightweight terminal dashboard for watching Codex/ChatGPT usage windows live across one or more accounts.

It tracks:

- 5-hour usage percentage
- weekly usage percentage
- reset countdowns and reset timestamps
- plan type
- code review window usage
- credits and spend-control flags
- optional local Codex session counts if you point an account at a `~/.codex/sessions` directory

## Why "sessions" is optional

The live usage endpoint exposes usage windows and limit state, but it does not expose a true account-level "session count". This tool can show local session-file counts when you provide `sessions_dir` for an account.

## Files

- [`../codex_nerve.py`](../codex_nerve.py)
- [`../accounts.example.ini`](../accounts.example.ini)
- [`../codex_bootstrap_accounts.sh`](../codex_bootstrap_accounts.sh)
- [`../codex_refresh_auth.py`](../codex_refresh_auth.py)
- [`../openai_keychain.sh`](../openai_keychain.sh)
- [`codex_reverse_engineering.html`](codex_reverse_engineering.html)
- [`codex_auth_guide.html`](codex_auth_guide.html)

## Quick Start

1. Copy the example file:

```bash
mkdir -p Codex/.local
cp Codex/accounts.example.ini Codex/.local/accounts.ini
```

2. Fill in your accounts in `Codex/.local/accounts.ini`.

3. Run the live dashboard:

```bash
python3 Codex/codex_nerve.py
```

4. Or fetch once as JSON:

```bash
python3 Codex/codex_nerve.py --once
```

## Same-Machine Bootstrap

If this is the same machine where Codex already works, you can generate `Codex/.local/accounts.ini` directly from `~/.codex/auth.json`:

```bash
chmod +x Codex/codex_bootstrap_accounts.sh
./Codex/codex_bootstrap_accounts.sh
```

Optional arguments:

```bash
./Codex/codex_bootstrap_accounts.sh my-account ./Codex/.local/accounts.ini
```

This auto-fills:

- `access_token`
- `account_id`
- `sessions_dir=~/.codex/sessions`
- `auth_file=~/.codex/auth.json`

## Account File Format

The file is plain text INI. Each section is one tracked account.

```ini
[personal]
access_token=YOUR_ACCESS_TOKEN
account_id=YOUR_ACCOUNT_ID
sessions_dir=~/.codex/sessions
auth_file=~/.codex/auth.json

[work]
access_token=YOUR_OTHER_ACCESS_TOKEN
account_id=YOUR_OTHER_ACCOUNT_ID
sessions_dir=
auth_file=
```

## Token Rotation

If the copied `access_token` in `Codex/.local/accounts.ini` goes stale, `codex_nerve.py` now tries two recovery steps for accounts that point at an `auth_file`:

1. Reload the latest token from `~/.codex/auth.json`
2. If that still fails, use the stored `refresh_token` to refresh via the OpenAI OAuth token endpoint and retry the usage request

You can also run the refresh flow directly:

```bash
python3 Codex/codex_refresh_auth.py --print-only
python3 Codex/codex_refresh_auth.py
```

## How To Get `access_token` And `account_id`

If you already use Codex CLI on a machine, they are usually in:

```bash
~/.codex/auth.json
```

Print them with:

```bash
jq -r '.tokens.access_token' ~/.codex/auth.json
jq -r '.tokens.account_id' ~/.codex/auth.json
```

You can also print the same instructions from the tool:

```bash
python3 Codex/codex_nerve.py --print-auth-help
```

## Keyboard Controls

- `q`: quit
- `r`: refresh now
- `Up` / `Down`: move between accounts
- `+` / `-`: change refresh interval
- `h`: remind yourself how to print the auth help

## Notes

- The usage endpoint used here is `https://chatgpt.com/backend-api/wham/usage`.
- The tool sends the same two headers Codex uses for this account-level view:
  - `Authorization: Bearer <access_token>`
  - `ChatGPT-Account-Id: <account_id>`
- If a token expires or becomes invalid, the row will show the HTTP/network error.
- For same-machine accounts, the dashboard can reload or refresh the token using `auth_file` before giving up.
- Live credential files belong under `Codex/.local/`, which is gitignored in this repo.
