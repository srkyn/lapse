![lapse project banner](docs/assets/lapse-banner.svg)

# lapse

Identify and clean up stale Entra ID device objects before they become a problem.

lapse is a Python tool for auditing Microsoft Entra ID (Azure AD) device registrations using dual-signal detection. It cross-references `approximateLastSignInDateTime` with actual interactive sign-in logs to eliminate the false positives that make every basic cleanup script unreliable.

![Release](https://img.shields.io/github/v/release/srkyn/lapse?style=flat-square)
![CI](https://img.shields.io/github/actions/workflow/status/srkyn/lapse/ci.yml?branch=main&style=flat-square)
![Python](https://img.shields.io/badge/python-3.8%2B-1f6feb?style=flat-square)
![License](https://img.shields.io/github/license/srkyn/lapse?style=flat-square)

## The Problem

Entra ID accumulates device objects silently. VDI pools register a new object on every session. Offboarded employees leave phones and laptops in the directory indefinitely. The `approximateLastSignInDateTime` property — the standard signal for device activity — also updates on background sync traffic, Windows Update heartbeats, and MDM check-ins. A device untouched by a human for a year can still appear active because its system processes keep phoning home.

The result is directories full of stale objects incurring real costs: Entra ID P1 licensing per device, bloated audit reports, and ghost objects that may still hold valid refresh tokens.

## The Fix

lapse uses two signals, not one:

1. **Approximate sign-in filter** — server-side `$filter` on `approximateLastSignInDateTime` to pull initial candidates from Graph API.
2. **Interactive sign-in verification** — each candidate is cross-checked against `auditLogs/signIns` filtered to `signInEventTypes eq 'interactiveUser'`. Background sync does not count. Only human authentication does.

A device is marked truly stale only when both signals confirm inactivity.

## At A Glance

- Dual-signal detection eliminates false positives from background sync traffic.
- Excludes hybrid-joined (domain-joined) devices by default — they are not Entra-managed.
- Optional `--company-only` flag excludes personal BYOD devices.
- Optional `--skip-vdi` flag excludes non-persistent VDI registrations by name and enrollment profile.
- `--disable` mode sets `accountEnabled = false` without deleting — reversible.
- `--delete` mode permanently removes stale devices; requires confirmation unless `--force`.
- `--dry-run` mode produces a full report with zero changes.
- JSON and CSV output for review workflows and audit records.
- Parallel sign-in log checks via `concurrent.futures` — handles large tenants in seconds, not minutes.
- Rate-limit handling with `Retry-After` backoff.
- Token cache persisted to disk; supports both device code flow and client credentials.

## Required Permissions

Register an application in Entra ID and grant the following API permissions:

| Permission | Why |
|---|---|
| `Device.ReadWrite.All` | Read device list; disable or delete devices. |
| `Directory.Read.All` | Read directory properties. |
| `AuditLog.Read.All` | Read interactive sign-in logs for secondary verification. |

For read-only audits, `Device.Read.All` is sufficient in place of `Device.ReadWrite.All`.

## Usage

```bash
# Report only — no changes
lapse --client-id <id> --tenant-id <tenant> --days 90 --dry-run

# Exclude personal devices and VDI registrations
lapse --client-id <id> --tenant-id <tenant> --company-only --skip-vdi

# Write JSON and CSV reports
lapse --client-id <id> --tenant-id <tenant> --output results.json --output-csv results.csv

# Disable stale devices (reversible)
lapse --client-id <id> --tenant-id <tenant> --disable

# Delete stale devices (requires confirmation)
lapse --client-id <id> --tenant-id <tenant> --delete

# App-only (automated / scheduled)
lapse --client-secret --client-id <id> --tenant-id <tenant> --client-secret-value <secret> --disable

# Check version
lapse --version
```

## Deployment Stages

Running `--delete` on day one is how bad cleanup tools create support tickets. The recommended path:

| Stage | Command | Checkpoint |
|---|---|---|
| Audit | `--dry-run` | Review report for a week. Check for false positives. |
| Review workflow | `--output-csv` | Human approves CSV before any action. |
| Disable-only | `--disable` | Run for two weeks. Confirm no legitimate device is affected. |
| Full purge | `--delete` | Schedule as weekly automation once confident. |

## Output Fields

JSON and CSV output includes: `displayName`, `operatingSystem`, `operatingSystemVersion`, `approximateLastSignInDateTime`, `age_days`, `trustType`, `deviceOwnership`, `enrollmentProfileName`, `accountEnabled`, `interactive_signin_found`, `truly_stale`, `id`, `deviceId`.

## Installation

```bash
git clone https://github.com/srkyn/lapse.git
cd lapse
pip install .
lapse --version
```

Or run directly without installing:

```bash
pip install msal requests tabulate
python lapse.py --client-id <id> --tenant-id <tenant> --dry-run
```

## Files

- `lapse.py`: the scanner CLI
- `tests/test_lapse.py`: unit tests for filtering, output, and dry-run behavior
- `docs/design-notes.md`: detection approach, design decisions, and limitations
- `CHANGELOG.md`: release history

## Limitations

- Does not inspect device software, scripts, or Intune compliance state.
- Sign-in log retention depends on Entra ID license tier; short retention windows may affect secondary verification accuracy.
- Does not handle on-premises Active Directory — that is a separate system requiring different tooling.
- May miss devices in tenants where the current credentials lack read access to sign-in logs.

## Testing

```bash
python -m py_compile lapse.py
python -m unittest discover -s tests -v
lapse --version
```
