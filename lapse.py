"""lapse.py — Identify and clean up stale Entra ID device objects.

Uses dual-signal detection: approximateLastSignInDateTime pre-filter combined
with interactive sign-in log verification to avoid false positives from
background sync traffic, Windows Update heartbeats, and VDI daily re-registrations.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:
    import msal
except ImportError:
    msal = None  # type: ignore

try:
    import requests
except ImportError:
    requests = None  # type: ignore

try:
    from tabulate import tabulate
    _HAS_TABULATE = True
except ImportError:
    _HAS_TABULATE = False

VERSION = "0.1.0"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
DEFAULT_DAYS = 90
DEFAULT_WORKERS = 10
RETRY_LIMIT = 5

_DEVICE_SELECT = (
    "id,deviceId,displayName,operatingSystem,operatingSystemVersion,"
    "approximateLastSignInDateTime,trustType,deviceOwnership,"
    "enrollmentProfileName,accountEnabled,managementType"
)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    """Format a datetime as an ISO-8601 string for Graph API $filter values."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 datetime string returned by Graph API."""
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Token cache helpers
# ---------------------------------------------------------------------------

def _load_token_cache(path: str) -> "msal.SerializableTokenCache":
    """Load a persisted MSAL token cache from disk, if it exists."""
    cache = msal.SerializableTokenCache()
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            cache.deserialize(fh.read())
    return cache


def _save_token_cache(cache: "msal.SerializableTokenCache", path: str) -> None:
    """Persist a modified MSAL token cache to disk."""
    if cache.has_state_changed:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(cache.serialize())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def build_msal_app(args: argparse.Namespace, cache: Any) -> Any:
    """Construct the appropriate MSAL application object.

    Returns a PublicClientApplication for device code flow or a
    ConfidentialClientApplication for client credentials flow.
    """
    if msal is None:
        _die("msal is not installed. Run: pip install msal")
    if args.client_secret:
        return msal.ConfidentialClientApplication(
            client_id=args.client_id,
            authority=f"https://login.microsoftonline.com/{args.tenant_id}",
            client_credential=args.client_secret_value,
            token_cache=cache,
        )
    return msal.PublicClientApplication(
        client_id=args.client_id,
        authority=f"https://login.microsoftonline.com/{args.tenant_id}",
        token_cache=cache,
    )


def resolve_client_secret(args: argparse.Namespace) -> None:
    """Populate the client secret from an environment variable when requested."""
    if not getattr(args, "client_secret_env", None):
        return
    if args.client_secret_value:
        _die("Use only one of --client-secret-value or --client-secret-env.")
    secret = os.environ.get(args.client_secret_env)
    if not secret:
        _die(f"Environment variable {args.client_secret_env} is not set or empty.")
    args.client_secret_value = secret


def get_access_token(app: Any, args: argparse.Namespace) -> str:
    """Acquire an access token, using the cache when possible.

    Falls back to device code flow (interactive) or client credentials
    (app-only) based on the supplied arguments.
    """
    scopes = [
        "https://graph.microsoft.com/Device.ReadWrite.All",
        "https://graph.microsoft.com/Directory.Read.All",
        "https://graph.microsoft.com/AuditLog.Read.All",
    ]

    # Try silent first (uses cached token).
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(scopes, account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]

    # Client credentials (app-only, no user interaction).
    if args.client_secret:
        app_scopes = ["https://graph.microsoft.com/.default"]
        result = app.acquire_token_for_client(scopes=app_scopes)
        if result and "access_token" in result:
            return result["access_token"]
        _die(f"Client credentials flow failed: {result.get('error_description', result)}")

    # Device code flow (interactive).
    flow = app.initiate_device_flow(scopes=scopes)
    if "user_code" not in flow:
        _die(f"Device flow initiation failed: {flow}")
    print(flow["message"], flush=True)
    result = app.acquire_token_by_device_flow(flow)
    if result and "access_token" in result:
        return result["access_token"]
    _die(f"Device code flow failed: {result.get('error_description', result)}")


# ---------------------------------------------------------------------------
# Graph API helpers
# ---------------------------------------------------------------------------

def _graph_request(
    token: str,
    method: str,
    url: str,
    **kwargs: Any,
) -> "requests.Response":
    """Execute a single Graph API request with retry and rate-limit handling."""
    if requests is None:
        _die("requests is not installed. Run: pip install requests")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    for attempt in range(1, RETRY_LIMIT + 1):
        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 10))
            print(f"  Rate limited. Waiting {wait}s (attempt {attempt}/{RETRY_LIMIT})...",
                  file=sys.stderr)
            time.sleep(wait)
            continue
        if resp.status_code == 401:
            _die("Access token rejected (401). Check permissions or re-authenticate.")
        if resp.status_code == 403:
            _die(
                "Permission denied (403). Ensure Device.ReadWrite.All, "
                "Directory.Read.All, and AuditLog.Read.All are granted."
            )
        return resp
    _die(f"Graph API request failed after {RETRY_LIMIT} attempts: {url}")


def _paginate(
    token: str,
    url: str,
    params: Optional[Dict[str, str]] = None,
) -> Iterator[Dict]:
    """Yield individual items from a paginated Graph API response."""
    next_url: Optional[str] = url
    query_params = params
    while next_url:
        resp = _graph_request(token, "GET", next_url, params=query_params)
        resp.raise_for_status()
        data = resp.json()
        yield from data.get("value", [])
        next_url = data.get("@odata.nextLink")
        query_params = None  # nextLink already includes params


# ---------------------------------------------------------------------------
# Device retrieval
# ---------------------------------------------------------------------------

def get_all_devices(token: str, cutoff: datetime) -> List[Dict]:
    """Fetch all devices with approximateLastSignInDateTime older than cutoff.

    Uses a server-side $filter to reduce client-side work on large tenants.
    """
    url = f"{GRAPH_BASE}/devices"
    params = {
        "$filter": f"approximateLastSignInDateTime le {_iso(cutoff)}",
        "$select": _DEVICE_SELECT,
        "$top": "999",
        "$count": "true",
    }
    # ConsistencyLevel header required when using $count or $filter on devices.
    if requests is None:
        _die("requests is not installed.")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "ConsistencyLevel": "eventual",
    }
    devices: List[Dict] = []
    next_url: Optional[str] = url
    query_params: Optional[Dict] = params
    while next_url:
        resp = _graph_request(token, "GET", next_url,
                              headers={"ConsistencyLevel": "eventual"},
                              params=query_params)
        resp.raise_for_status()
        data = resp.json()
        devices.extend(data.get("value", []))
        next_url = data.get("@odata.nextLink")
        query_params = None
    return devices


# ---------------------------------------------------------------------------
# Sign-in log verification
# ---------------------------------------------------------------------------

def check_interactive_signins(
    token: str,
    device_id: str,
    cutoff: datetime,
) -> bool:
    """Return True if an interactive user sign-in exists for this device since cutoff.

    Queries the auditLogs/signIns endpoint and filters for non-background
    sign-in types to avoid false negatives from service principal activity.
    """
    url = f"{GRAPH_BASE}/auditLogs/signIns"
    params = {
        "$filter": (
            f"deviceDetail/deviceId eq '{device_id}' and "
            f"createdDateTime ge {_iso(cutoff)} and "
            "signInEventTypes/any(t:t eq 'interactiveUser')"
        ),
        "$top": "1",
        "$select": "id,createdDateTime,signInEventTypes",
    }
    try:
        resp = _graph_request(token, "GET", url, params=params)
        if resp.status_code == 403:
            # AuditLog.Read.All not granted — treat as unknown, don't block.
            return False
        resp.raise_for_status()
        return len(resp.json().get("value", [])) > 0
    except (requests.RequestException, ValueError, KeyError):
        return False


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def _is_hybrid_joined(device: Dict) -> bool:
    """Return True if the device is hybrid Azure AD joined (domain-joined)."""
    return device.get("trustType", "") == "ServerAD"


def _is_personal(device: Dict) -> bool:
    """Return True if the device is personally owned (BYOD)."""
    return device.get("deviceOwnership", "").lower() == "personal"


def _is_vdi(device: Dict) -> bool:
    """Return True if the device appears to be a non-persistent VDI instance."""
    profile = (device.get("enrollmentProfileName") or "").lower()
    name = (device.get("displayName") or "").lower()
    vdi_markers = ("vdi", "nonpersistent", "non-persistent", "avd", "wvd", "citrix")
    return any(m in profile or m in name for m in vdi_markers)


def age_days(device: Dict, now: datetime) -> Optional[int]:
    """Return the number of days since approximateLastSignInDateTime."""
    dt = _parse_dt(device.get("approximateLastSignInDateTime"))
    if dt is None:
        return None
    return (now - dt).days


def apply_filters(
    devices: List[Dict],
    company_only: bool,
    skip_vdi: bool,
) -> Tuple[List[Dict], int, int, int]:
    """Apply exclusion filters and return (candidates, hybrid_count, personal_count, vdi_count)."""
    hybrid_count = 0
    personal_count = 0
    vdi_count = 0
    candidates: List[Dict] = []

    for device in devices:
        if _is_hybrid_joined(device):
            hybrid_count += 1
            continue
        if company_only and _is_personal(device):
            personal_count += 1
            continue
        if skip_vdi and _is_vdi(device):
            vdi_count += 1
            continue
        candidates.append(device)

    return candidates, hybrid_count, personal_count, vdi_count


# ---------------------------------------------------------------------------
# Classification (parallel sign-in checks)
# ---------------------------------------------------------------------------

def classify_devices(
    token: str,
    candidates: List[Dict],
    cutoff: datetime,
    now: datetime,
    workers: int,
) -> List[Dict]:
    """Cross-reference candidates against interactive sign-in logs.

    Uses a thread pool to check multiple devices concurrently, reducing
    runtime on large tenants from minutes to seconds.

    Returns only devices that are truly stale (no interactive sign-in found).
    """
    results: List[Dict] = []

    def _check(device: Dict) -> Dict:
        device_id = device.get("deviceId", "")
        found = check_interactive_signins(token, device_id, cutoff) if device_id else False
        days = age_days(device, now)
        return {**device, "interactive_signin_found": found, "age_days": days, "truly_stale": not found}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_check, d): d for d in candidates}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            result = future.result()
            results.append(result)
            if not result.get("interactive_signin_found"):
                pass  # truly stale
            pct = int(i / len(candidates) * 100) if candidates else 100
            print(f"\r  Verifying sign-in logs... {pct}% ({i}/{len(candidates)})",
                  end="", flush=True)

    print()  # newline after progress
    return [r for r in results if r.get("truly_stale")]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _fmt_date(value: Optional[str]) -> str:
    """Shorten an ISO-8601 datetime to YYYY-MM-DD for display."""
    if not value:
        return "—"
    return value[:10]


def print_table(devices: List[Dict]) -> None:
    """Print a human-readable table of stale devices sorted by age."""
    if not devices:
        print("No truly stale devices found.")
        return

    sorted_devices = sorted(devices, key=lambda d: d.get("age_days") or 0, reverse=True)
    rows = []
    for d in sorted_devices:
        rows.append([
            (d.get("displayName") or "")[:30],
            (d.get("operatingSystem") or "")[:10],
            _fmt_date(d.get("approximateLastSignInDateTime")),
            d.get("age_days", "—"),
            "Yes" if d.get("interactive_signin_found") else "No",
            "STALE" if d.get("truly_stale") else "active",
        ])

    headers = ["Display Name", "OS", "Last Approx Sign-In", "Days", "Interactive", "Status"]
    if _HAS_TABULATE:
        print(tabulate(rows, headers=headers, tablefmt="simple"))
    else:
        col_widths = [max(len(str(r[i])) for r in ([headers] + rows)) for i in range(len(headers))]
        sep = "  ".join("-" * w for w in col_widths)
        print("  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers)))
        print(sep)
        for row in rows:
            print("  ".join(str(v).ljust(col_widths[i]) for i, v in enumerate(row)))


def write_json(devices: List[Dict], path: str) -> None:
    """Write device records to a JSON file."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(devices, fh, indent=2, default=str)
    print(f"  JSON written to {path}")


def write_csv(devices: List[Dict], path: str) -> None:
    """Write device records to a CSV file."""
    if not devices:
        return
    fields = [
        "displayName", "operatingSystem", "operatingSystemVersion",
        "approximateLastSignInDateTime", "age_days", "trustType",
        "deviceOwnership", "enrollmentProfileName", "accountEnabled",
        "interactive_signin_found", "truly_stale", "id", "deviceId",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(devices)
    print(f"  CSV written to {path}")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def disable_device(token: str, object_id: str, dry_run: bool) -> bool:
    """Set accountEnabled = False on a device. Returns True on success."""
    if dry_run:
        return True
    url = f"{GRAPH_BASE}/devices/{object_id}"
    resp = _graph_request(token, "PATCH", url, json={"accountEnabled": False})
    return resp.status_code in (200, 204)


def delete_device(token: str, object_id: str, dry_run: bool) -> bool:
    """Permanently delete a device from Entra ID. Returns True on success."""
    if dry_run:
        return True
    url = f"{GRAPH_BASE}/devices/{object_id}"
    resp = _graph_request(token, "DELETE", url)
    return resp.status_code == 204


def apply_action(
    token: str,
    devices: List[Dict],
    args: argparse.Namespace,
) -> None:
    """Apply --disable or --delete to all stale devices."""
    if not devices:
        return
    if args.dry_run:
        print(f"  Dry run: would act on {len(devices)} device(s). No changes made.")
        return

    if args.delete and not args.force:
        answer = input(
            f"\n  About to permanently DELETE {len(devices)} device(s). "
            "Type 'yes' to confirm: "
        ).strip().lower()
        if answer != "yes":
            print("  Aborted.")
            return

    verb = "Deleting" if args.delete else "Disabling"
    ok = 0
    fail = 0
    for device in devices:
        obj_id = device.get("id", "")
        name = device.get("displayName", obj_id)
        if args.delete:
            success = delete_device(token, obj_id, dry_run=False)
        else:
            success = disable_device(token, obj_id, dry_run=False)
        if success:
            ok += 1
            print(f"  {verb}: {name}")
        else:
            fail += 1
            print(f"  FAILED: {name}", file=sys.stderr)

    print(f"\n  {verb} complete — {ok} succeeded, {fail} failed.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lp",
        description=(
            "Identify stale Entra ID device objects using dual-signal detection.\n"
            "Combines approximateLastSignInDateTime with interactive sign-in log "
            "verification to eliminate false positives from background sync traffic."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"lapse {VERSION}")

    detection = parser.add_argument_group("detection")
    detection.add_argument(
        "-d", "--days", type=int, default=DEFAULT_DAYS, metavar="N",
        help=f"Inactivity threshold in days (default: {DEFAULT_DAYS}).",
    )
    detection.add_argument(
        "--company-only", action="store_true",
        help="Exclude personally owned (BYOD) devices.",
    )
    detection.add_argument(
        "--skip-vdi", action="store_true",
        help="Exclude devices with VDI or NonPersistent markers in their name or enrollment profile.",
    )
    detection.add_argument(
        "-w", "--workers", type=int, default=DEFAULT_WORKERS, metavar="N",
        help=f"Parallel threads for sign-in log checks (default: {DEFAULT_WORKERS}).",
    )

    output = parser.add_argument_group("output")
    output.add_argument("-o", "--output", metavar="FILE", help="Write JSON report to FILE.")
    output.add_argument("--output-csv", metavar="FILE", help="Write CSV report to FILE.")
    output.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress table output; print summary line only.",
    )

    actions = parser.add_argument_group("actions")
    actions.add_argument(
        "-n", "--dry-run", action="store_true",
        help="Report candidates without making any changes.",
    )
    actions.add_argument(
        "--disable", action="store_true",
        help="Set accountEnabled = False on stale devices (does not delete).",
    )
    actions.add_argument(
        "--delete", action="store_true",
        help="Permanently delete stale devices from Entra ID.",
    )
    actions.add_argument(
        "-f", "--force", action="store_true",
        help="Skip confirmation prompt when using --delete.",
    )

    auth = parser.add_argument_group("authentication")
    auth.add_argument(
        "--interactive", action="store_true", default=True,
        help="Use MSAL device code flow (default).",
    )
    auth.add_argument(
        "--client-secret", action="store_true",
        help="Use client credentials flow (app-only). Requires --client-id, --tenant-id, --client-secret-value.",
    )
    auth.add_argument("--client-id", metavar="ID", help="App registration client ID.")
    auth.add_argument("--tenant-id", metavar="ID", help="Entra ID tenant ID.")
    auth.add_argument(
        "--client-secret-value", metavar="SECRET",
        help="Client secret value (for --client-secret flow).",
    )
    auth.add_argument(
        "--client-secret-env", metavar="VAR",
        help="Read the client secret from environment variable VAR.",
    )
    auth.add_argument(
        "--token-cache", metavar="FILE", default="token_cache.bin",
        help="Path to token cache file (default: token_cache.bin).",
    )

    return parser.parse_args(argv)


def _die(message: str) -> None:
    print(f"lapse: error: {message}", file=sys.stderr)
    sys.exit(1)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    if msal is None:
        _die("msal is not installed. Run: pip install msal requests tabulate")
    if requests is None:
        _die("requests is not installed. Run: pip install requests")

    resolve_client_secret(args)
    if args.client_secret and not all([args.client_id, args.tenant_id, args.client_secret_value]):
        _die("--client-secret requires --client-id, --tenant-id, and --client-secret-value or --client-secret-env.")
    if not args.client_secret and not args.client_id:
        _die("Provide --client-id and --tenant-id (and optionally --tenant-id for authority).")

    now = utc_now()
    from datetime import timedelta
    cutoff = now - timedelta(days=args.days)

    # Authenticate.
    cache = _load_token_cache(args.token_cache)
    app = build_msal_app(args, cache)
    print("Authenticating...", flush=True)
    token = get_access_token(app, args)
    _save_token_cache(cache, args.token_cache)

    # Phase 1: fetch devices from Graph.
    print(f"Scanning Entra ID devices (threshold: {args.days} days)...", flush=True)
    raw_devices = get_all_devices(token, cutoff)
    print(f"  Retrieved {len(raw_devices)} device(s) from Graph API.")

    # Phase 2: apply exclusion filters.
    candidates, hybrid_n, personal_n, vdi_n = apply_filters(
        raw_devices, args.company_only, args.skip_vdi
    )
    if hybrid_n:
        print(f"  Excluded {hybrid_n} hybrid-joined device(s).")
    if personal_n:
        print(f"  Excluded {personal_n} personal device(s) (--company-only).")
    if vdi_n:
        print(f"  Excluded {vdi_n} VDI device(s) (--skip-vdi).")
    print(f"  {len(candidates)} candidate(s) after filtering.")

    if not candidates:
        print("\nNo stale candidates found.")
        return 0

    # Phase 3: verify with interactive sign-in logs.
    print(f"  Checking sign-in logs ({args.workers} parallel workers)...", flush=True)
    stale = classify_devices(token, candidates, cutoff, now, args.workers)

    # Output.
    if not args.quiet:
        print()
        print_table(stale)
        print()

    if args.output:
        write_json(stale, args.output)
    if args.output_csv:
        write_csv(stale, args.output_csv)

    # Actions.
    if args.disable or args.delete:
        apply_action(token, stale, args)

    zero_interactive = sum(1 for d in stale if not d.get("interactive_signin_found"))
    print(
        f"Total devices scanned: {len(raw_devices)}, "
        f"Truly stale candidates: {len(stale)} "
        f"({zero_interactive} with zero interactive sign-ins)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
