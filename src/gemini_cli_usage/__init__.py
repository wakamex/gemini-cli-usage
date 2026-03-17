#!/usr/bin/env python3
"""gemini-cli-usage - Gemini CLI quota monitor.

Fetches Gemini Code Assist quota data using the OAuth credentials
stored in ~/.gemini/oauth_creds.json.

Usage:
    gemini-cli-usage
    gemini-cli-usage status
    gemini-cli-usage json
    gemini-cli-usage daemon
    gemini-cli-usage statusline
    gemini-cli-usage refresh
    gemini-cli-usage install
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

_NATIVE_GEMINI_DIR = Path.home() / ".gemini"


def _wsl_gemini_dirs() -> list[Path]:
    """Return candidate .gemini dirs from WSL distros (Windows only)."""
    if sys.platform != "win32":
        return []
    import subprocess

    try:
        result = subprocess.run(
            ["wsl", "-l", "-q"],
            capture_output=True,
            timeout=5,
        )
        raw = result.stdout.decode("utf-16-le", errors="replace")
        distros = [d.strip() for d in raw.splitlines() if d.strip()]
    except Exception:
        return []

    dirs: list[Path] = []
    for distro in distros:
        wsl_home = Path(f"//wsl$/{distro}/home")
        try:
            if wsl_home.is_dir():
                for user_dir in wsl_home.iterdir():
                    candidate = user_dir / ".gemini"
                    if candidate.is_dir():
                        dirs.append(candidate)
        except OSError:
            continue
    return dirs


def _find_gemini_file(filename: str) -> Path:
    """Return the first existing .gemini/<filename> from native + WSL paths."""
    native = _NATIVE_GEMINI_DIR / filename
    if native.exists():
        return native
    for wsl_dir in _wsl_gemini_dirs():
        candidate = wsl_dir / filename
        if candidate.exists():
            return candidate
    return native  # default even if missing


GEMINI_DIR = _NATIVE_GEMINI_DIR
OAUTH_FILE = _find_gemini_file("oauth_creds.json")
SETTINGS_FILE = _find_gemini_file("settings.json")
DEFAULT_USAGE_FILE = _find_gemini_file("usage-limits.json")

DAEMON_INTERVAL = 300  # 5 minutes
CACHE_MAX_AGE = 300

CODE_ASSIST_BASE_URL = "https://cloudcode-pa.googleapis.com/v1internal"
TOKEN_URL = "https://oauth2.googleapis.com/token"

AUTH_LABELS = {
    "oauth-personal": "Google login",
    "gemini-api-key": "Gemini API key",
    "vertex-ai": "Vertex AI",
    "cloud-shell": "Cloud Shell",
    "compute-default-credentials": "Compute ADC",
    "gateway": "Gateway",
}

OAUTH_CLIENT_ID_PATTERN = re.compile(r"const OAUTH_CLIENT_ID = '([^']+)';")
OAUTH_CLIENT_SECRET_PATTERN = re.compile(r"const OAUTH_CLIENT_SECRET = '([^']+)';")

# ANSI color codes — disabled when stdout is not a terminal (e.g. piped or redirected).
_TTY = sys.stdout.isatty()
_RED = "\033[0;31m" if _TTY else ""
_YELLOW = "\033[0;33m" if _TTY else ""
_GREEN = "\033[0;32m" if _TTY else ""
_DIM = "\033[0;90m" if _TTY else ""
_RESET = "\033[0m" if _TTY else ""


def _read_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_auth_type_from_settings(path: Path) -> str | None:
    settings = _read_json(path)
    if not isinstance(settings, dict):
        return None
    security = settings.get("security")
    if not isinstance(security, dict):
        return None
    auth = security.get("auth")
    if not isinstance(auth, dict):
        return None
    selected = auth.get("selectedType")
    return selected if isinstance(selected, str) else None


def _get_env_auth_type() -> str | None:
    if os.environ.get("GOOGLE_GENAI_USE_GCA") == "true":
        return "oauth-personal"
    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI") == "true":
        return "vertex-ai"
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini-api-key"
    if (
        os.environ.get("CLOUD_SHELL") == "true"
        or os.environ.get("GEMINI_CLI_USE_COMPUTE_ADC") == "true"
    ):
        return "compute-default-credentials"
    return None


def get_auth_type(project_root: Path | None = None) -> str | None:
    env_auth = _get_env_auth_type()
    if env_auth:
        return env_auth

    if project_root:
        workspace_auth = _read_auth_type_from_settings(
            project_root.resolve() / ".gemini" / "settings.json"
        )
        if workspace_auth:
            return workspace_auth

    return _read_auth_type_from_settings(SETTINGS_FILE)


def get_auth_label(auth_type: str | None) -> str:
    return AUTH_LABELS.get(auth_type or "", auth_type or "unknown")


def get_oauth_credentials() -> dict | None:
    data = _read_json(OAUTH_FILE)
    return data if isinstance(data, dict) else None


def _write_oauth_credentials(creds: dict):
    try:
        OAUTH_FILE.write_text(json.dumps(creds, indent=2) + "\n")
    except OSError:
        # Best effort only; the refreshed token can still be used in-memory.
        pass


def _get_gemini_cli_oauth2_path() -> Path | None:
    oauth2_rels = [
        Path("node_modules") / "@google" / "gemini-cli-core" / "dist" / "src" / "code_assist" / "oauth2.js",
        Path("node_modules") / "@google" / "gemini-cli" / "node_modules" / "@google" / "gemini-cli-core" / "dist" / "src" / "code_assist" / "oauth2.js",
    ]

    candidates: list[Path] = []

    # 1. Try shutil.which
    gemini_bin = shutil.which("gemini")
    if gemini_bin:
        resolved = Path(gemini_bin).resolve()
        candidates.extend([resolved.parent, resolved.parent.parent])

    # 2. On Windows, scan fnm node version directories
    if sys.platform == "win32":
        fnm_dir = Path.home() / "AppData" / "Roaming" / "fnm" / "node-versions"
        if fnm_dir.exists():
            for version_dir in fnm_dir.iterdir():
                candidates.append(version_dir / "installation")

        # Also check AppData/Local fnm multishells
        fnm_multi = Path.home() / "AppData" / "Local" / "fnm_multishells"
        if fnm_multi.exists():
            for shell_dir in fnm_multi.iterdir():
                candidates.append(shell_dir)

    # 3. Unix: check common global npm/nvm paths
    else:
        for p in [
            Path("/usr/lib"),
            Path("/usr/local/lib"),
            Path.home() / ".nvm" / "versions",
        ]:
            if p.exists():
                if "nvm" in str(p):
                    for v in p.rglob("node"):
                        candidates.append(v / "lib")
                else:
                    candidates.append(p)

    for root in candidates:
        for oauth2_rel in oauth2_rels:
            oauth2_path = root / oauth2_rel
            if oauth2_path.exists():
                return oauth2_path
            oauth2_path = root / "lib" / oauth2_rel
            if oauth2_path.exists():
                return oauth2_path

    return None


def _get_gemini_cli_oauth_client_credentials() -> tuple[str, str] | None:
    oauth2_path = _get_gemini_cli_oauth2_path()
    if not oauth2_path:
        return None

    try:
        source = oauth2_path.read_text()
    except OSError:
        return None

    client_id_match = OAUTH_CLIENT_ID_PATTERN.search(source)
    client_secret_match = OAUTH_CLIENT_SECRET_PATTERN.search(source)
    if not client_id_match or not client_secret_match:
        return None

    return client_id_match.group(1), client_secret_match.group(1)


def _get_oauth_client_credentials(creds: dict | None = None) -> tuple[str, str]:
    client_id = os.environ.get("GEMINI_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GEMINI_OAUTH_CLIENT_SECRET")
    if client_id and client_secret:
        return client_id, client_secret

    creds = creds or get_oauth_credentials() or {}
    client_id = creds.get("client_id")
    client_secret = creds.get("client_secret")
    if isinstance(client_id, str) and isinstance(client_secret, str):
        if client_id and client_secret:
            return client_id, client_secret

    live_credentials = _get_gemini_cli_oauth_client_credentials()
    if live_credentials:
        return live_credentials

    raise RuntimeError(
        "OAuth access token expired and no Gemini CLI OAuth client metadata "
        "was found. Set GEMINI_OAUTH_CLIENT_ID and "
        "GEMINI_OAUTH_CLIENT_SECRET, or run `gemini` and retry."
    )


def refresh_access_token(creds: dict) -> dict:
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("No refresh token in ~/.gemini/oauth_creds.json")

    client_id, client_secret = _get_oauth_client_credentials(creds)
    payload = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode()

    req = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())

    updated = dict(creds)
    updated["access_token"] = result["access_token"]
    updated["token_type"] = result.get("token_type", updated.get("token_type", "Bearer"))
    updated["scope"] = result.get("scope", updated.get("scope"))
    updated["expiry_date"] = int(time.time() * 1000 + int(result.get("expires_in", 3600)) * 1000)
    if result.get("id_token"):
        updated["id_token"] = result["id_token"]
    if result.get("refresh_token"):
        updated["refresh_token"] = result["refresh_token"]
    _write_oauth_credentials(updated)
    return updated


def get_access_token() -> str:
    creds = get_oauth_credentials()
    if not creds:
        raise RuntimeError("No OAuth credentials at ~/.gemini/oauth_creds.json")

    expiry_date = int(creds.get("expiry_date", 0) or 0)
    if time.time() * 1000 >= expiry_date - 60_000:
        creds = refresh_access_token(creds)

    token = creds.get("access_token")
    if not token:
        raise RuntimeError("No access token in ~/.gemini/oauth_creds.json")
    return token


def _code_assist_post(method: str, payload: dict, access_token: str) -> dict:
    req = urllib.request.Request(
        f"{CODE_ASSIST_BASE_URL}:{method}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "gemini-cli-usage/0.1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _load_code_assist(access_token: str) -> dict:
    project_id = (
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT_ID")
        or None
    )
    metadata = {
        "ideType": "IDE_UNSPECIFIED",
        "platform": "PLATFORM_UNSPECIFIED",
        "pluginType": "GEMINI",
    }
    if project_id:
        metadata["duetProject"] = project_id
    return _code_assist_post(
        "loadCodeAssist",
        {
            "cloudaicompanionProject": project_id,
            "metadata": metadata,
        },
        access_token,
    )


def _parse_quota_buckets(buckets: list[dict]) -> list[dict]:
    parsed = []
    for bucket in buckets:
        remaining = None
        limit = None
        used_pct = None
        remaining_fraction = bucket.get("remainingFraction")

        try:
            if bucket.get("remainingAmount") is not None:
                remaining = int(bucket["remainingAmount"])
        except (TypeError, ValueError):
            remaining = None

        if isinstance(remaining_fraction, int | float):
            used_pct = (1 - float(remaining_fraction)) * 100
        if remaining is not None and isinstance(remaining_fraction, int | float):
            if remaining_fraction > 0:
                limit = round(remaining / float(remaining_fraction))

        parsed.append(
            {
                "model": bucket.get("modelId"),
                "remaining": remaining,
                "limit": limit,
                "used_pct": used_pct,
                "remaining_fraction": remaining_fraction,
                "reset_time": bucket.get("resetTime"),
                "token_type": bucket.get("tokenType"),
            }
        )
    return parsed


def _select_summary_bucket(quota: dict) -> dict | None:
    buckets = quota.get("buckets") or []
    if not isinstance(buckets, list):
        return None
    scored = [bucket for bucket in buckets if bucket.get("used_pct") is not None]
    if scored:
        return max(scored, key=lambda bucket: bucket["used_pct"])
    return buckets[0] if buckets else None


def fetch_quota(project_root: Path | None = None) -> dict:
    auth_type = get_auth_type(project_root)
    if auth_type != "oauth-personal":
        raise RuntimeError(
            "Quota lookup requires Google login; current auth is "
            f"{get_auth_label(auth_type)}"
        )

    access_token = get_access_token()
    load_res = _load_code_assist(access_token)

    env_project = (
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT_ID")
        or None
    )
    project_id = load_res.get("cloudaicompanionProject") or env_project
    if not project_id:
        raise RuntimeError(
            "No Code Assist project ID available. Set GOOGLE_CLOUD_PROJECT if your account requires it."
        )

    quota_res = _code_assist_post(
        "retrieveUserQuota", {"project": project_id}, access_token
    )
    current_tier = load_res.get("currentTier") or {}
    paid_tier = load_res.get("paidTier") or {}
    result = {
        "project_id": project_id,
        "user_tier": paid_tier.get("id") or current_tier.get("id"),
        "user_tier_name": paid_tier.get("name") or current_tier.get("name"),
        "buckets": _parse_quota_buckets(quota_res.get("buckets") or []),
    }
    result["summary_bucket"] = _select_summary_bucket(result)
    return result


def build_usage_json(project_root: Path | None = None) -> dict:
    root = (project_root or Path.cwd()).resolve()
    auth_type = get_auth_type(root)

    result = {
        "project_root": str(root),
        "auth_type": auth_type,
        "auth_label": get_auth_label(auth_type),
        "source": [],
        "updated_at": _iso_now(),
    }

    try:
        result["account_quota"] = fetch_quota(root)
        result["source"].append("quota_api")
    except Exception as exc:
        result["quota_error"] = str(exc)

    return result


def get_usage_file() -> Path:
    override = os.environ.get("GEMINI_CLI_USAGE_FILE") or os.environ.get(
        "GEMINI_USAGE_FILE"
    )
    return Path(override).expanduser() if override else DEFAULT_USAGE_FILE


def write_usage_file(data: dict):
    usage_file = get_usage_file()
    usage_file.parent.mkdir(parents=True, exist_ok=True)
    usage_file.write_text(json.dumps(data, indent=2) + "\n")


def _format_duration_until(iso_timestamp: str | None) -> str:
    reset = _parse_iso(iso_timestamp)
    if not reset:
        return ""
    seconds = int((reset - datetime.now(UTC)).total_seconds())
    if seconds <= 0:
        return ""
    minutes = seconds // 60
    if minutes >= 60:
        return f"{minutes // 60}h{minutes % 60}m"
    return f"{minutes}m"


def _color_pct(pct: float | int | None) -> str:
    if pct is None:
        return "?"
    p = float(pct)
    color = _RED if p >= 70 else _YELLOW if p >= 40 else _GREEN
    return f"{color}{_format_pct(p)}{_RESET}"


def _format_pct(pct: float | int | None) -> str:
    if pct is None:
        return "?"
    p = float(pct)
    if p >= 1:
        return f"{p:.1f}%"
    return f"{p:.2f}%"


def _print_status(data: dict):
    print(f"Project: {Path(data['project_root']).name}")
    print(f"Auth: {data.get('auth_label', 'unknown')}")

    quota = data.get("account_quota")
    if quota:
        bucket_names = [
            (bucket.get("model") or "unknown")
            for bucket in quota.get("buckets", [])
            if isinstance(bucket, dict)
        ]
        name_width = max(map(len, bucket_names), default=len("Quota"))

        def print_bucket_line(label: str, bucket: dict):
            reset_time = _format_duration_until(bucket.get("reset_time"))
            reset_part = f"  resets {reset_time}" if reset_time else ""
            remaining = bucket.get("remaining")
            limit = bucket.get("limit")
            remain_part = (
                f"  {remaining} / {limit} remaining"
                if remaining is not None and limit is not None
                else ""
            )
            print(
                f"  {label:{name_width}s} {_color_pct(bucket.get('used_pct'))} used"
                f"{remain_part}{_DIM}{reset_part}{_RESET}"
            )

        for bucket in quota.get("buckets", []):
            model = bucket.get("model") or "unknown"
            print_bucket_line(model, bucket)
    elif data.get("quota_error"):
        print(f"  {'Quota':20s} {_DIM}{data['quota_error']}{_RESET}")


def _statusline_text(data: dict) -> str:
    parts = []
    quota = data.get("account_quota")
    if quota and quota.get("summary_bucket"):
        summary_bucket = quota["summary_bucket"]
        if summary_bucket.get("used_pct") is not None:
            parts.append(f"q:{_format_pct(summary_bucket['used_pct'])}")
        reset_time = _format_duration_until(summary_bucket.get("reset_time"))
        if reset_time:
            parts.append(f"reset:{reset_time}")
    elif data.get("quota_error"):
        parts.append("q:err")
    return " ".join(parts)


def _get_cached_usage(
    project_root: Path | None = None,
    max_age: int = CACHE_MAX_AGE,
    force_refresh: bool = False,
) -> dict:
    usage_file = get_usage_file()
    if not force_refresh:
        try:
            cached = json.loads(usage_file.read_text())
            updated = _parse_iso(cached.get("updated_at"))
            root = str((project_root or Path.cwd()).resolve())
            if updated and cached.get("project_root") == root:
                age = (datetime.now(UTC) - updated).total_seconds()
                if age < max_age and "quota_api" in cached.get("source", []):
                    return cached
        except Exception:
            pass

    try:
        fresh = build_usage_json(project_root)
        write_usage_file(fresh)
        return fresh
    except Exception:
        try:
            return json.loads(usage_file.read_text())
        except Exception:
            return build_usage_json(project_root)


def cmd_status(args):
    data = build_usage_json(project_root=Path(args.root).resolve() if args.root else None)
    _print_status(data)


def cmd_json(args):
    data = build_usage_json(project_root=Path(args.root).resolve() if args.root else None)
    print(json.dumps(data, indent=2))


def cmd_daemon(args):
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    root = Path(args.root).resolve() if args.root else None
    usage_file = get_usage_file()

    print(f"gemini-cli-usage daemon started (refreshing every {args.interval}s)")
    print(f"Writing to {usage_file}")

    while True:
        try:
            data = build_usage_json(project_root=root)
            write_usage_file(data)
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"{_statusline_text(data)}"
            )
        except Exception as exc:
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Error: {exc}",
                file=sys.stderr,
            )
        time.sleep(args.interval)


def cmd_statusline(args):
    data = _get_cached_usage(
        project_root=Path(args.root).resolve() if args.root else None,
        max_age=args.max_age,
        force_refresh=args.refresh,
    )
    print(_statusline_text(data))


def cmd_refresh(args):
    data = build_usage_json(project_root=Path(args.root).resolve() if args.root else None)
    write_usage_file(data)
    _print_status(data)


def cmd_install(_args):
    print(
        "Install with:\n"
        "  uv tool install gemini-cli-usage\n\n"
        "For local development:\n"
        "  uv tool install .\n\n"
        "Then run:\n"
        "  gemini-cli-usage\n"
        "  gemini-cli-usage statusline\n"
        "  gemini-cli-usage refresh\n"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gemini CLI quota monitor")
    parser.add_argument(
        "command",
        nargs="?",
        default="status",
        choices=["status", "json", "daemon", "statusline", "refresh", "install"],
    )
    parser.add_argument(
        "--root",
        help="Project root to inspect (default: current working directory)",
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=int,
        default=DAEMON_INTERVAL,
        help="Daemon refresh interval in seconds",
    )
    parser.add_argument(
        "--max-age",
        type=int,
        default=CACHE_MAX_AGE,
        help="Maximum cache age in seconds for statusline",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore cache and force a fresh fetch where applicable",
    )
    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()

    commands = {
        "status": cmd_status,
        "json": cmd_json,
        "daemon": cmd_daemon,
        "statusline": cmd_statusline,
        "refresh": cmd_refresh,
        "install": cmd_install,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
