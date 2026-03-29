#!/usr/bin/env python3
"""Live terminal dashboard for Codex/ChatGPT usage windows."""

from __future__ import annotations

import argparse
import configparser
import curses
import json
import locale
import os
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL 1\.1\.1\+",
)

import requests


USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_DIR / ".local" / "accounts.ini"
DEFAULT_AUTH_FILE = Path.home() / ".codex" / "auth.json"
TOKEN_ENDPOINT = "https://auth0.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_INTERVAL = 15.0
SECONDS_PER_HOUR = 3600


@dataclass
class AccountConfig:
    name: str
    access_token: str
    account_id: str
    sessions_dir: Optional[Path] = None
    auth_file: Optional[Path] = None


@dataclass
class SessionSummary:
    total_files: int = 0
    recent_5h_files: int = 0
    recent_7d_files: int = 0
    latest_mtime: Optional[float] = None
    error: Optional[str] = None


@dataclass
class AccountState:
    config: AccountConfig
    last_refresh_started_at: Optional[float] = None
    last_refresh_finished_at: Optional[float] = None
    last_success_at: Optional[float] = None
    payload: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    sessions: Optional[SessionSummary] = None
    auth_status: Optional[str] = None

    @property
    def plan_type(self) -> str:
        if not self.payload:
            return "-"
        return str(self.payload.get("plan_type") or "-")

    @property
    def rate_limit(self) -> dict[str, Any]:
        if not self.payload:
            return {}
        return self.payload.get("rate_limit") or {}

    @property
    def code_review_rate_limit(self) -> dict[str, Any]:
        if not self.payload:
            return {}
        return self.payload.get("code_review_rate_limit") or {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Track Codex/ChatGPT account usage windows in a terminal dashboard."
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Path to the plain text account file. Default: {DEFAULT_CONFIG}",
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"Polling interval in seconds. Default: {DEFAULT_INTERVAL}",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Fetch once and print JSON summaries instead of opening the live dashboard.",
    )
    parser.add_argument(
        "--print-auth-help",
        action="store_true",
        help="Print instructions for extracting access_token and account_id.",
    )
    return parser.parse_args()


def load_accounts(config_path: Path) -> list[AccountConfig]:
    parser = configparser.ConfigParser()
    expanded_path = config_path.expanduser()
    if not expanded_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {expanded_path}. Copy Codex/accounts.example.ini to this path first."
        )
    parser.read(expanded_path)
    accounts: list[AccountConfig] = []
    for section in parser.sections():
        access_token = parser.get(section, "access_token", fallback="").strip()
        account_id = parser.get(section, "account_id", fallback="").strip()
        sessions_dir_raw = parser.get(section, "sessions_dir", fallback="").strip()
        auth_file_raw = parser.get(section, "auth_file", fallback="").strip()
        if not access_token or not account_id:
            raise ValueError(
                f"Section [{section}] must include both access_token and account_id."
            )
        sessions_dir = Path(sessions_dir_raw).expanduser() if sessions_dir_raw else None
        auth_file = Path(auth_file_raw).expanduser() if auth_file_raw else DEFAULT_AUTH_FILE
        accounts.append(
            AccountConfig(
                name=section,
                access_token=access_token,
                account_id=account_id,
                sessions_dir=sessions_dir,
                auth_file=auth_file,
            )
        )
    if not accounts:
        raise ValueError(f"No account sections were found in {expanded_path}.")
    return accounts


def fetch_usage(session: requests.Session, config: AccountConfig, timeout: float = 20.0) -> dict[str, Any]:
    response = session.get(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {config.access_token}",
            "ChatGPT-Account-Id": config.account_id,
            "Accept": "application/json",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def should_try_auth_recovery(exc: requests.RequestException) -> bool:
    response = getattr(exc, "response", None)
    if response is None:
        return False
    return response.status_code in {401, 403}


def load_local_auth_tokens(auth_file: Optional[Path]) -> Optional[dict[str, Any]]:
    if auth_file is None:
        return None
    expanded = auth_file.expanduser()
    if not expanded.exists():
        return None
    data = json.loads(expanded.read_text())
    tokens = data.get("tokens") or {}
    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    account_id = str(tokens.get("account_id") or "").strip()
    if not access_token or not account_id:
        return None
    return {
        "path": expanded,
        "raw": data,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": account_id,
        "id_token": str(tokens.get("id_token") or "").strip(),
    }


def account_matches_auth_file(config: AccountConfig, auth_tokens: dict[str, Any]) -> bool:
    auth_account_id = str(auth_tokens.get("account_id") or "")
    auth_access_token = str(auth_tokens.get("access_token") or "")
    return config.account_id == auth_account_id or config.access_token == auth_access_token


def reload_account_from_auth_file(state: AccountState) -> bool:
    auth_tokens = load_local_auth_tokens(state.config.auth_file)
    if not auth_tokens or not account_matches_auth_file(state.config, auth_tokens):
        return False
    changed = (
        state.config.access_token != auth_tokens["access_token"]
        or state.config.account_id != auth_tokens["account_id"]
    )
    state.config.access_token = auth_tokens["access_token"]
    state.config.account_id = auth_tokens["account_id"]
    if changed:
        state.auth_status = f"reloaded token from {auth_tokens['path']}"
    return changed


def write_auth_tokens(auth_file: Path, auth_data: dict[str, Any]) -> None:
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(auth_data, indent=2)
    auth_file.write_text(payload + os.linesep)


def refresh_access_token_from_auth_file(
    session: requests.Session, state: AccountState, timeout: float = 20.0
) -> bool:
    auth_tokens = load_local_auth_tokens(state.config.auth_file)
    if not auth_tokens or not account_matches_auth_file(state.config, auth_tokens):
        return False
    refresh_token = auth_tokens.get("refresh_token") or ""
    if not refresh_token:
        state.auth_status = "auth.json has no refresh_token"
        return False

    response = session.post(
        TOKEN_ENDPOINT,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CODEX_CLIENT_ID,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    token_payload = response.json()

    new_access_token = str(token_payload.get("access_token") or "").strip()
    if not new_access_token:
        raise ValueError("refresh response did not include access_token")

    auth_data = auth_tokens["raw"]
    tokens = auth_data.setdefault("tokens", {})
    tokens["access_token"] = new_access_token
    tokens["account_id"] = auth_tokens["account_id"]

    new_refresh_token = str(token_payload.get("refresh_token") or "").strip()
    if new_refresh_token:
        tokens["refresh_token"] = new_refresh_token

    new_id_token = str(token_payload.get("id_token") or "").strip()
    if new_id_token:
        tokens["id_token"] = new_id_token

    auth_data["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    write_auth_tokens(auth_tokens["path"], auth_data)

    state.config.access_token = tokens["access_token"]
    state.config.account_id = tokens["account_id"]
    state.auth_status = f"refreshed token via {TOKEN_ENDPOINT}"
    return True


def scan_sessions(sessions_dir: Optional[Path], now: Optional[float] = None) -> Optional[SessionSummary]:
    if sessions_dir is None:
        return None
    summary = SessionSummary()
    now = now or time.time()
    directory = sessions_dir.expanduser()
    if not directory.exists():
        summary.error = f"missing: {directory}"
        return summary
    try:
        for path in directory.rglob("*.jsonl"):
            summary.total_files += 1
            stat = path.stat()
            modified_at = stat.st_mtime
            if summary.latest_mtime is None or modified_at > summary.latest_mtime:
                summary.latest_mtime = modified_at
            age = now - modified_at
            if age <= 5 * SECONDS_PER_HOUR:
                summary.recent_5h_files += 1
            if age <= 7 * 24 * SECONDS_PER_HOUR:
                summary.recent_7d_files += 1
    except OSError as exc:
        summary.error = str(exc)
    return summary


def refresh_account(session: requests.Session, state: AccountState) -> None:
    state.last_refresh_started_at = time.time()
    state.auth_status = None
    try:
        state.payload = fetch_usage(session, state.config)
        state.error = None
        state.last_success_at = time.time()
    except requests.RequestException as exc:
        recovery_errors: list[str] = [str(exc)]
        recovered = False
        if should_try_auth_recovery(exc):
            if reload_account_from_auth_file(state):
                try:
                    state.payload = fetch_usage(session, state.config)
                    state.error = None
                    state.last_success_at = time.time()
                    recovered = True
                except requests.RequestException as reload_exc:
                    recovery_errors.append(f"reload retry: {reload_exc}")
            if not recovered:
                try:
                    if refresh_access_token_from_auth_file(session, state):
                        state.payload = fetch_usage(session, state.config)
                        state.error = None
                        state.last_success_at = time.time()
                        recovered = True
                except (requests.RequestException, ValueError, OSError, json.JSONDecodeError) as refresh_exc:
                    recovery_errors.append(f"refresh retry: {refresh_exc}")
        if not recovered:
            state.error = " | ".join(recovery_errors)
    state.sessions = scan_sessions(state.config.sessions_dir)
    state.last_refresh_finished_at = time.time()


def format_countdown(seconds: Optional[int]) -> str:
    if seconds is None:
        return "-"
    if seconds < 0:
        seconds = 0
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}h {minutes:02d}m"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_timestamp(epoch_seconds: Optional[float]) -> str:
    if not epoch_seconds:
        return "-"
    local_dt = datetime.fromtimestamp(epoch_seconds).astimezone()
    return local_dt.strftime("%Y-%m-%d %H:%M:%S")


def format_relative_seconds(epoch_seconds: Optional[float], now: Optional[float] = None) -> str:
    if not epoch_seconds:
        return "-"
    now = now or time.time()
    delta = max(0, int(now - epoch_seconds))
    if delta < 60:
        return f"{delta}s ago"
    minutes, seconds = divmod(delta, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago"


def format_reset_at(epoch_seconds: Optional[int]) -> str:
    if not epoch_seconds:
        return "-"
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).astimezone().strftime("%m-%d %H:%M")


def clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1] + ">"


def draw_bar(percent: Optional[float], width: int) -> str:
    if width <= 0:
        return ""
    if percent is None:
        return "-" * width
    clamped = max(0.0, min(100.0, float(percent)))
    filled = int(round((clamped / 100.0) * width))
    return "#" * filled + "-" * max(0, width - filled)


def get_color_for_percent(percent: Optional[float]) -> int:
    if percent is None:
        return curses.color_pair(0)
    if percent >= 90:
        return curses.color_pair(3)
    if percent >= 70:
        return curses.color_pair(2)
    return curses.color_pair(1)


def safe_addstr(stdscr: "curses._CursesWindow", y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    available = max(0, width - x)
    if available <= 0:
        return
    try:
        stdscr.addstr(y, x, clip(text, available), attr)
    except curses.error:
        return


def extract_window(payload: dict[str, Any], path: str) -> dict[str, Any]:
    current: Any = payload
    for key in path.split("."):
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def aggregate_summary(states: list[AccountState]) -> tuple[int, int]:
    ok_count = sum(1 for state in states if state.payload and not state.error)
    error_count = sum(1 for state in states if state.error)
    return ok_count, error_count


def draw_dashboard(
    stdscr: "curses._CursesWindow",
    states: list[AccountState],
    selected_index: int,
    config_path: Path,
    interval: float,
    next_refresh_at: float,
    status_message: str,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    now = time.time()
    ok_count, error_count = aggregate_summary(states)

    safe_addstr(stdscr, 0, 0, "codex-nerve", curses.A_BOLD)
    safe_addstr(
        stdscr,
        1,
        0,
        clip(
            f"config={config_path.expanduser()}  accounts={len(states)}  ok={ok_count}  errors={error_count}  refresh={interval:.0f}s  next={format_countdown(int(max(0, next_refresh_at - now)))}",
            width,
        ),
    )
    safe_addstr(
        stdscr,
        2,
        0,
        clip("keys: q quit  r refresh now  up/down move  +/- change refresh interval  h auth help", width),
        curses.A_DIM,
    )

    header_y = 4
    safe_addstr(
        stdscr,
        header_y,
        0,
        clip("Acct             Plan   5h Usage               Weekly Usage           5h Reset     Wk Reset     Sessions(5h/7d/all)   Status", width),
        curses.A_BOLD | curses.A_UNDERLINE,
    )

    row_y = header_y + 1
    detail_top = min(height - 9, row_y + len(states) + 1)
    if detail_top <= row_y:
        detail_top = row_y

    for index, state in enumerate(states):
        if row_y + index >= detail_top:
            break
        payload = state.payload or {}
        primary = extract_window(payload, "rate_limit.primary_window")
        secondary = extract_window(payload, "rate_limit.secondary_window")
        sessions = state.sessions

        primary_percent = primary.get("used_percent")
        secondary_percent = secondary.get("used_percent")
        sessions_text = "-"
        if sessions is not None:
            if sessions.error:
                sessions_text = "err"
            else:
                sessions_text = f"{sessions.recent_5h_files}/{sessions.recent_7d_files}/{sessions.total_files}"

        auth_suffix = f" [{state.auth_status}]" if state.auth_status else ""
        status_text = "ok" + auth_suffix if state.payload and not state.error else ((state.error or "waiting") + auth_suffix)
        line = (
            f"{state.config.name[:15]:15}  "
            f"{state.plan_type[:5]:5}  "
            f"{draw_bar(primary_percent, 18)} {str(primary_percent or 0).rjust(3)}%  "
            f"{draw_bar(secondary_percent, 18)} {str(secondary_percent or 0).rjust(3)}%  "
            f"{format_countdown(primary.get('reset_after_seconds')):11}  "
            f"{format_countdown(secondary.get('reset_after_seconds')):11}  "
            f"{sessions_text:20}  "
            f"{status_text}"
        )
        attr = curses.A_REVERSE if index == selected_index else curses.A_NORMAL
        safe_addstr(stdscr, row_y + index, 0, clip(line, width), attr)

    detail_y = detail_top + 1
    if 0 <= selected_index < len(states) and detail_y < height - 1:
        state = states[selected_index]
        payload = state.payload or {}
        primary = extract_window(payload, "rate_limit.primary_window")
        secondary = extract_window(payload, "rate_limit.secondary_window")
        review_primary = extract_window(payload, "code_review_rate_limit.primary_window")
        credits = payload.get("credits") or {}
        spend_control = payload.get("spend_control") or {}
        sessions = state.sessions

        safe_addstr(stdscr, detail_y, 0, f"Selected: {state.config.name}", curses.A_BOLD)
        safe_addstr(
            stdscr,
            detail_y + 1,
            0,
            clip(
                f"plan={payload.get('plan_type', '-')}  allowed={extract_window(payload, 'rate_limit').get('allowed', '-')}  limit_reached={extract_window(payload, 'rate_limit').get('limit_reached', '-')}",
                width,
            ),
        )
        safe_addstr(
            stdscr,
            detail_y + 2,
            0,
            clip(
                f"5h: {primary.get('used_percent', '-')}%  reset_in={format_countdown(primary.get('reset_after_seconds'))}  reset_at={format_reset_at(primary.get('reset_at'))}",
                width,
            ),
            get_color_for_percent(primary.get("used_percent")),
        )
        safe_addstr(
            stdscr,
            detail_y + 3,
            0,
            clip(
                f"weekly: {secondary.get('used_percent', '-')}%  reset_in={format_countdown(secondary.get('reset_after_seconds'))}  reset_at={format_reset_at(secondary.get('reset_at'))}",
                width,
            ),
            get_color_for_percent(secondary.get("used_percent")),
        )
        safe_addstr(
            stdscr,
            detail_y + 4,
            0,
            clip(
                f"code-review window: {review_primary.get('used_percent', '-')}%  reset_in={format_countdown(review_primary.get('reset_after_seconds'))}",
                width,
            ),
        )
        safe_addstr(
            stdscr,
            detail_y + 5,
            0,
            clip(
                f"credits: has_credits={credits.get('has_credits', '-')} unlimited={credits.get('unlimited', '-')} balance={credits.get('balance', '-')}",
                width,
            ),
        )
        if sessions is not None:
            session_line = (
                f"local sessions: 5h={sessions.recent_5h_files}  7d={sessions.recent_7d_files}  all={sessions.total_files}  latest={format_timestamp(sessions.latest_mtime)}"
            )
            if sessions.error:
                session_line = f"local sessions: error={sessions.error}"
            safe_addstr(stdscr, detail_y + 6, 0, clip(session_line, width))
        safe_addstr(
            stdscr,
            detail_y + 7,
            0,
            clip(
                f"last success={format_timestamp(state.last_success_at)} ({format_relative_seconds(state.last_success_at, now)})  last error={state.error or '-'}",
                width,
            ),
        )
        safe_addstr(
            stdscr,
            detail_y + 8,
            0,
            clip(f"auth recovery: {state.auth_status or '-'}", width),
        )

    safe_addstr(stdscr, height - 1, 0, clip(status_message, width), curses.A_DIM)
    stdscr.refresh()


AUTH_HELP = """How to get access_token and account_id

If you already use Codex CLI on a machine, they are usually in:
  ~/.codex/auth.json

Show them with:
  jq -r '.tokens.access_token' ~/.codex/auth.json
  jq -r '.tokens.account_id' ~/.codex/auth.json

You can then paste them into Codex/.local/accounts.ini:

  [my-account]
  access_token=PASTE_ACCESS_TOKEN_HERE
  account_id=PASTE_ACCOUNT_ID_HERE
  sessions_dir=~/.codex/sessions
  auth_file=~/.codex/auth.json

Repeat with more sections if you want to track multiple accounts.

Default config path:
  Codex/.local/accounts.ini
"""


def print_auth_help() -> None:
    print(AUTH_HELP.strip())


def run_once(states: list[AccountState]) -> int:
    session = requests.Session()
    summaries: list[dict[str, Any]] = []
    exit_code = 0
    for state in states:
        refresh_account(session, state)
        if state.error:
            exit_code = 1
        payload = state.payload or {}
        summaries.append(
            {
                "name": state.config.name,
                "plan_type": payload.get("plan_type"),
                "five_hour_used_percent": extract_window(payload, "rate_limit.primary_window").get("used_percent"),
                "five_hour_reset_after_seconds": extract_window(payload, "rate_limit.primary_window").get("reset_after_seconds"),
                "weekly_used_percent": extract_window(payload, "rate_limit.secondary_window").get("used_percent"),
                "weekly_reset_after_seconds": extract_window(payload, "rate_limit.secondary_window").get("reset_after_seconds"),
                "code_review_used_percent": extract_window(payload, "code_review_rate_limit.primary_window").get("used_percent"),
                "sessions": None
                if state.sessions is None
                else {
                    "recent_5h_files": state.sessions.recent_5h_files,
                    "recent_7d_files": state.sessions.recent_7d_files,
                    "total_files": state.sessions.total_files,
                    "latest_mtime": state.sessions.latest_mtime,
                    "error": state.sessions.error,
                },
                "auth_status": state.auth_status,
                "error": state.error,
            }
        )
    print(json.dumps(summaries, indent=2))
    return exit_code


def tui(stdscr: "curses._CursesWindow", states: list[AccountState], config_path: Path, interval: float) -> int:
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_RED, -1)
    stdscr.nodelay(True)
    stdscr.keypad(True)

    session = requests.Session()
    selected_index = 0
    status_message = "Loading account usage..."
    next_refresh_at = 0.0

    while True:
        now = time.time()
        if now >= next_refresh_at:
            for state in states:
                refresh_account(session, state)
            next_refresh_at = time.time() + interval
            status_message = f"Last refresh finished at {format_timestamp(time.time())}"

        draw_dashboard(stdscr, states, selected_index, config_path, interval, next_refresh_at, status_message)

        key = stdscr.getch()
        if key == -1:
            time.sleep(0.1)
            continue
        if key in (ord("q"), ord("Q")):
            return 0
        if key in (curses.KEY_UP, ord("k")):
            selected_index = max(0, selected_index - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected_index = min(len(states) - 1, selected_index + 1)
        elif key in (ord("r"), ord("R")):
            next_refresh_at = 0.0
            status_message = "Manual refresh requested."
        elif key in (ord("+"), ord("=")):
            interval = min(300.0, interval + 5.0)
            next_refresh_at = min(next_refresh_at, time.time() + interval)
            status_message = f"Refresh interval set to {interval:.0f}s."
        elif key == ord("-"):
            interval = max(5.0, interval - 5.0)
            next_refresh_at = min(next_refresh_at, time.time() + interval)
            status_message = f"Refresh interval set to {interval:.0f}s."
        elif key in (ord("h"), ord("H")):
            status_message = "Auth help: run `python3 codex_nerve.py --print-auth-help` in another terminal."


def main() -> int:
    locale.setlocale(locale.LC_ALL, "")
    args = parse_args()
    if args.print_auth_help:
        print_auth_help()
        return 0

    try:
        accounts = load_accounts(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc))
        print("Tip: copy accounts.example.ini to accounts.ini and fill in your values.")
        return 1

    states = [AccountState(config=account) for account in accounts]
    if args.once:
        return run_once(states)

    return curses.wrapper(lambda stdscr: tui(stdscr, states, args.config, max(5.0, args.interval)))


if __name__ == "__main__":
    raise SystemExit(main())
