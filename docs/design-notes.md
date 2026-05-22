# Design Notes

## The Problem

Entra ID accumulates device objects silently. A VDI lab of 30 thin clients produces hundreds of registrations within weeks. Ex-employee phones and laptops persist indefinitely after offboarding. The directory fills with objects that carry real costs — Entra ID P1 licensing per device — and real risk, since stale objects may still hold valid refresh tokens.

## Why ApproximateLastSignInDateTime Is Not Enough

The `approximateLastSignInDateTime` property on the Graph API devices endpoint updates on background traffic: Windows Update heartbeats, policy sync, MDM check-ins. A device that has not been touched by a human in 18 months can still appear fresh because its system processes keep phoning home. Filtering on this property alone produces a significant false-positive rate. In practice, a naive filter flagging 500+ devices often contains fewer than 100 truly abandoned ones.

## Dual-Signal Detection

lapse runs two checks:

1. **Approximate sign-in filter** — server-side `$filter` on `approximateLastSignInDateTime le {cutoff}`. This is a wide net, not a verdict. It eliminates devices that have not even had background activity in the threshold window, reducing the candidate set before the more expensive second check.

2. **Interactive sign-in verification** — for each candidate, lapse queries `auditLogs/signIns` filtered to `signInEventTypes/any(t:t eq 'interactiveUser')`. An interactive sign-in requires a human authenticating through a browser, app, or OS login prompt — not a background service. If any such sign-in exists within the threshold window, the device is excluded.

A device is marked truly stale only when **both** signals confirm inactivity.

## Filtering Logic

Three exclusion categories keep the candidate set clean before verification:

- **Hybrid-joined devices** (`trustType == "ServerAD"`) — domain-joined machines authenticate through on-premises AD and may appear inactive in Entra without actually being dead. Excluding them by default prevents accidental cleanup of production workstations.
- **Personal devices** (`deviceOwnership == "Personal"`, optional via `--company-only`) — BYOD devices may be infrequently registered and have irregular sign-in patterns. Skipping them reduces noise in environments with heavy BYOD use.
- **VDI registrations** (`--skip-vdi`) — non-persistent desktop pools register new device objects on each session. These are legitimate but should be managed through the pool lifecycle, not individual cleanup.

## Performance Decisions

Sign-in log checks are the expensive operation. Each call hits a separate Graph endpoint, and a tenant with 2,000 candidates would take 30+ minutes sequentially. lapse uses `concurrent.futures.ThreadPoolExecutor` to run up to `--workers` checks in parallel (default 10). On most tenants this reduces total runtime to under two minutes.

Rate limiting is handled with `Retry-After` backoff on HTTP 429. The retry loop runs up to `RETRY_LIMIT` attempts before aborting the request.

## Action Model

lapse follows a staged model to avoid irreversible mistakes:

| Mode | What happens |
|------|-------------|
| *(no flag)* | Scan and report only. No changes. |
| `--dry-run` | Explicit no-op. Same output, nothing written to Graph. |
| `--disable` | Sets `accountEnabled = false`. Reversible. Device still exists. |
| `--delete` | Permanently removes the device. Requires confirmation unless `--force`. |

The recommended path for a new deployment is to run in report mode first, review the CSV, then use `--disable` with a clear rollback window before considering any permanent delete workflow.

## Authentication

Two flows are supported via MSAL:

- **Device code flow** — user authenticates interactively through a browser. Appropriate for ad-hoc runs and development. Token is cached to disk.
- **Client credentials** — app-only flow using client ID and secret. Appropriate for scheduled automation. No user interaction. Prefer `--client-secret-env` so the secret does not appear in shell history or process listings.

Token cache is serialized to disk between runs to avoid re-authentication. The cache path is configurable via `--token-cache`. Treat the cache file as sensitive authentication material and keep it outside source control with restrictive filesystem permissions.

## What lapse Does Not Do

- Does not inspect scripts or software on the device.
- Does not evaluate Intune compliance state.
- Does not handle on-premises Active Directory (different system, different APIs).
- Does not resolve every edge case in sign-in log coverage (AuditLog.Read.All may have retention limits depending on license tier).
