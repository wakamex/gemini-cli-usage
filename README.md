# gemini-cli-usage

Gemini CLI quota monitor. Fetches live Code Assist quota data from Google's
backend when Gemini is using Google login.

## Example output

`gemini-cli-usage` command:

```text
Project: gemini-cli-usage
Auth: Google login
  Quota summary         3.5% used  gemini-2.5-pro  resets 19h38m
  gemini-2.5-flash-lite 0.07% used  resets 19h37m
```

`gemini-cli-usage statusline` command:

```text
q:3.5% reset:19h38m
```

## Install

```bash
uv tool install gemini-cli-usage
```

For local development from a checkout:

```bash
uv tool install .
```

Then run:

```bash
# Check usage once
gemini-cli-usage

# Raw JSON
gemini-cli-usage json

# Compact shell/statusline output
gemini-cli-usage statusline

# Force a fresh cache rebuild and print full status
gemini-cli-usage refresh

# Keep ~/.gemini/usage-limits.json fresh
gemini-cli-usage daemon
```

## Commands

| Command | Description |
|---------|-------------|
| `gemini-cli-usage` | Show current usage (colored terminal output) |
| `gemini-cli-usage status` | Same as above |
| `gemini-cli-usage json` | Print raw JSON |
| `gemini-cli-usage daemon [-i SECS]` | Run in foreground, refresh every 5 min |
| `gemini-cli-usage statusline` | Compact statusline (reads cache, refreshes if stale) |
| `gemini-cli-usage refresh` | Force a fresh fetch, rewrite cache, and print status |
| `gemini-cli-usage install` | Print setup instructions |

## Data source

### `account_quota`

When Gemini CLI is configured for Google login (`oauth-personal`), it calls
Google's internal Code Assist API:

- `loadCodeAssist`
- `retrieveUserQuota`

This tool mirrors that flow using the OAuth credentials in
`~/.gemini/oauth_creds.json`.

## Notes

- Quota fetches are best-effort. If auth is not Google login, or quota lookup
  fails, the tool reports the auth state plus the quota error.
- If the Google OAuth access token expires, the tool reuses Gemini CLI's
  installed OAuth client metadata when available. If Gemini is installed in a
  nonstandard location, set `GEMINI_OAUTH_CLIENT_ID` and
  `GEMINI_OAUTH_CLIENT_SECRET`, or rerun `gemini` and retry.
- `status` and `json` always build fresh data.
- `statusline` reads the cache by default; use `--refresh` or `--max-age 0` to
  force a live refresh.
- `refresh` is a convenience command that rebuilds the cache and prints the full
  status output.
- Absolute quota counts are only shown when Google's response includes both
  `remainingAmount` and a usable fraction. Otherwise the tool reports `% used`
  plus reset time.
- Auth detection follows Gemini CLI precedence: environment variables first,
  then workspace `.gemini/settings.json`, then global `~/.gemini/settings.json`.

## Options

```text
usage: gemini-cli-usage [-h] [--root ROOT] [--interval INTERVAL]
                        [--max-age MAX_AGE] [--refresh]
                        {status,json,daemon,statusline,refresh,install}
```

- `--root ROOT`: inspect a different project root instead of the current
  directory
- `--max-age MAX_AGE`: cache TTL for `statusline`
- `--refresh`: ignore the cache and rebuild fresh data where applicable
