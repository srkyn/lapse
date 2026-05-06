# Changelog

## Unreleased

Initial release.

- Dual-signal stale device detection: `approximateLastSignInDateTime` pre-filter combined with interactive sign-in log verification.
- Filters for hybrid-joined devices, personal (BYOD) devices, and non-persistent VDI registrations.
- `--disable` mode sets `accountEnabled = false` without deleting.
- `--delete` mode permanently removes stale devices; requires confirmation unless `--force` is passed.
- `--dry-run` mode: produces full report with no changes.
- JSON and CSV output via `--output` and `--output-csv`.
- Parallel sign-in log checks using `concurrent.futures.ThreadPoolExecutor`.
- Rate-limit handling with `Retry-After` backoff on HTTP 429.
- Token cache persisted to disk to avoid re-authentication on repeat runs.
- Device code flow (`--interactive`) and client credentials flow (`--client-secret`).
- `tabulate` table output with graceful fallback if not installed.
- Summary line: total scanned, truly stale candidates, zero-interactive-signin count.
