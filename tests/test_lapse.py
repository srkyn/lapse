"""Unit tests for lapse.py."""

import csv
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lapse


def _dt(days_ago: int) -> datetime:
    """Return a UTC datetime N days in the past."""
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _device(
    days_ago: int = 100,
    trust_type: str = "AzureAD",
    ownership: str = "Company",
    enrollment: str = "",
    name: str = "TEST-DEVICE",
    device_id: str = "abc-123",
    obj_id: str = "obj-001",
) -> dict:
    return {
        "id": obj_id,
        "deviceId": device_id,
        "displayName": name,
        "operatingSystem": "Windows",
        "operatingSystemVersion": "10.0",
        "approximateLastSignInDateTime": _iso(_dt(days_ago)),
        "trustType": trust_type,
        "deviceOwnership": ownership,
        "enrollmentProfileName": enrollment,
        "accountEnabled": True,
    }


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

class VersionTests(unittest.TestCase):
    def test_version_string(self):
        self.assertRegex(lapse.VERSION, r"^\d+\.\d+\.\d+$")

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as ctx:
            lapse.parse_args(["--version"])
        self.assertEqual(ctx.exception.code, 0)


# ---------------------------------------------------------------------------
# Date utilities
# ---------------------------------------------------------------------------

class DateUtilTests(unittest.TestCase):
    def test_parse_dt_z(self):
        dt = lapse._parse_dt("2024-01-15T12:00:00Z")
        self.assertEqual(dt.year, 2024)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_parse_dt_microseconds(self):
        dt = lapse._parse_dt("2024-06-01T08:30:00.000000Z")
        self.assertIsNotNone(dt)

    def test_parse_dt_none(self):
        self.assertIsNone(lapse._parse_dt(None))

    def test_parse_dt_empty(self):
        self.assertIsNone(lapse._parse_dt(""))

    def test_age_days(self):
        device = _device(days_ago=45)
        now = lapse.utc_now()
        days = lapse.age_days(device, now)
        self.assertAlmostEqual(days, 45, delta=1)

    def test_age_days_missing(self):
        device = {"approximateLastSignInDateTime": None}
        self.assertIsNone(lapse.age_days(device, lapse.utc_now()))


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

class FilterTests(unittest.TestCase):
    def test_hybrid_joined_excluded(self):
        devices = [_device(trust_type="ServerAD")]
        candidates, hybrid_n, personal_n, vdi_n = lapse.apply_filters(
            devices, company_only=False, skip_vdi=False
        )
        self.assertEqual(len(candidates), 0)
        self.assertEqual(hybrid_n, 1)

    def test_azure_ad_joined_included(self):
        devices = [_device(trust_type="AzureAD")]
        candidates, hybrid_n, _, _ = lapse.apply_filters(
            devices, company_only=False, skip_vdi=False
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(hybrid_n, 0)

    def test_personal_excluded_when_company_only(self):
        devices = [_device(ownership="Personal")]
        candidates, _, personal_n, _ = lapse.apply_filters(
            devices, company_only=True, skip_vdi=False
        )
        self.assertEqual(len(candidates), 0)
        self.assertEqual(personal_n, 1)

    def test_personal_included_without_company_only(self):
        devices = [_device(ownership="Personal")]
        candidates, _, personal_n, _ = lapse.apply_filters(
            devices, company_only=False, skip_vdi=False
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(personal_n, 0)

    def test_vdi_excluded_when_skip_vdi(self):
        devices = [_device(enrollment="VDI-NonPersistent-Profile", name="AVD-Session-001")]
        candidates, _, _, vdi_n = lapse.apply_filters(
            devices, company_only=False, skip_vdi=True
        )
        self.assertEqual(len(candidates), 0)
        self.assertEqual(vdi_n, 1)

    def test_vdi_name_marker(self):
        devices = [_device(name="VDI-HOST-07")]
        candidates, _, _, vdi_n = lapse.apply_filters(
            devices, company_only=False, skip_vdi=True
        )
        self.assertEqual(vdi_n, 1)

    def test_vdi_included_without_skip_flag(self):
        devices = [_device(enrollment="NonPersistent")]
        candidates, _, _, vdi_n = lapse.apply_filters(
            devices, company_only=False, skip_vdi=False
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(vdi_n, 0)

    def test_multiple_exclusion_types(self):
        devices = [
            _device(trust_type="ServerAD"),
            _device(ownership="Personal"),
            _device(enrollment="VDI"),
            _device(),  # clean
        ]
        candidates, hybrid_n, personal_n, vdi_n = lapse.apply_filters(
            devices, company_only=True, skip_vdi=True
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(hybrid_n, 1)
        self.assertEqual(personal_n, 1)
        self.assertEqual(vdi_n, 1)

    def test_is_hybrid_joined(self):
        self.assertTrue(lapse._is_hybrid_joined({"trustType": "ServerAD"}))
        self.assertFalse(lapse._is_hybrid_joined({"trustType": "AzureAD"}))

    def test_is_personal(self):
        self.assertTrue(lapse._is_personal({"deviceOwnership": "Personal"}))
        self.assertFalse(lapse._is_personal({"deviceOwnership": "Company"}))

    def test_is_vdi_enrollment(self):
        self.assertTrue(lapse._is_vdi({"enrollmentProfileName": "NonPersistent", "displayName": ""}))

    def test_is_vdi_avd_name(self):
        self.assertTrue(lapse._is_vdi({"enrollmentProfileName": "", "displayName": "avd-pool-host"}))

    def test_is_vdi_clean(self):
        self.assertFalse(lapse._is_vdi({"enrollmentProfileName": "", "displayName": "LAPTOP-ABC123"}))


# ---------------------------------------------------------------------------
# Dry-run (no API calls made)
# ---------------------------------------------------------------------------

class DryRunTests(unittest.TestCase):
    def test_disable_dry_run_returns_true(self):
        result = lapse.disable_device("fake-token", "obj-id", dry_run=True)
        self.assertTrue(result)

    def test_delete_dry_run_returns_true(self):
        result = lapse.delete_device("fake-token", "obj-id", dry_run=True)
        self.assertTrue(result)

    def test_apply_action_dry_run_no_calls(self):
        args = MagicMock()
        args.dry_run = True
        args.disable = True
        args.delete = False
        devices = [_device()]
        # Should not raise or call any API.
        with patch.object(lapse, "disable_device") as mock_disable:
            lapse.apply_action("token", devices, args)
            mock_disable.assert_not_called()


# ---------------------------------------------------------------------------
# Output (JSON and CSV)
# ---------------------------------------------------------------------------

class JsonOutputTests(unittest.TestCase):
    def test_write_json_structure(self):
        devices = [
            {**_device(), "age_days": 100, "interactive_signin_found": False, "truly_stale": True}
        ]
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as fh:
            path = fh.name
        try:
            lapse.write_json(devices, path)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIsInstance(data, list)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["displayName"], "TEST-DEVICE")
        finally:
            os.unlink(path)

    def test_write_json_empty(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as fh:
            path = fh.name
        try:
            lapse.write_json([], path)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data, [])
        finally:
            os.unlink(path)


class TokenCacheTests(unittest.TestCase):
    def test_save_token_cache_creates_parent_directory(self):
        cache = MagicMock()
        cache.has_state_changed = True
        cache.serialize.return_value = "cache-data"

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "nested", "token_cache.bin")
            lapse._save_token_cache(cache, path)

            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "cache-data")


class CsvOutputTests(unittest.TestCase):
    def test_write_csv_columns(self):
        devices = [
            {**_device(), "age_days": 120, "interactive_signin_found": False, "truly_stale": True}
        ]
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w", newline="") as fh:
            path = fh.name
        try:
            lapse.write_csv(devices, path)
            with open(path, encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                rows = list(reader)
            self.assertEqual(len(rows), 1)
            self.assertIn("displayName", rows[0])
            self.assertIn("age_days", rows[0])
            self.assertIn("truly_stale", rows[0])
        finally:
            os.unlink(path)

    def test_write_csv_empty_no_file_written(self):
        # write_csv should return early for empty input.
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as fh:
            path = fh.name
        os.unlink(path)
        lapse.write_csv([], path)
        self.assertFalse(os.path.exists(path))


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

class ArgParseTests(unittest.TestCase):
    def test_defaults(self):
        args = lapse.parse_args(["--client-id", "x", "--tenant-id", "y"])
        self.assertEqual(args.days, lapse.DEFAULT_DAYS)
        self.assertFalse(args.company_only)
        self.assertFalse(args.skip_vdi)
        self.assertFalse(args.dry_run)
        self.assertFalse(args.delete)
        self.assertFalse(args.force)
        self.assertEqual(args.workers, lapse.DEFAULT_WORKERS)

    def test_custom_days(self):
        args = lapse.parse_args(["--client-id", "x", "--tenant-id", "y", "--days", "180"])
        self.assertEqual(args.days, 180)

    def test_dry_run_flag(self):
        args = lapse.parse_args(["--client-id", "x", "--tenant-id", "y", "--dry-run"])
        self.assertTrue(args.dry_run)

    def test_company_only_flag(self):
        args = lapse.parse_args(["--client-id", "x", "--tenant-id", "y", "--company-only"])
        self.assertTrue(args.company_only)

    def test_skip_vdi_flag(self):
        args = lapse.parse_args(["--client-id", "x", "--tenant-id", "y", "--skip-vdi"])
        self.assertTrue(args.skip_vdi)

    def test_output_flag(self):
        args = lapse.parse_args(["--client-id", "x", "--tenant-id", "y", "--output", "out.json"])
        self.assertEqual(args.output, "out.json")

    def test_output_csv_flag(self):
        args = lapse.parse_args(["--client-id", "x", "--tenant-id", "y", "--output-csv", "out.csv"])
        self.assertEqual(args.output_csv, "out.csv")

    def test_delete_and_force(self):
        args = lapse.parse_args(["--client-id", "x", "--tenant-id", "y", "--delete", "--force"])
        self.assertTrue(args.delete)
        self.assertTrue(args.force)

    def test_client_secret_flags(self):
        args = lapse.parse_args([
            "--client-id", "cid", "--tenant-id", "tid",
            "--client-secret", "--client-secret-value", "secret",
        ])
        self.assertTrue(args.client_secret)
        self.assertEqual(args.client_secret_value, "secret")

    def test_client_secret_env_flag(self):
        args = lapse.parse_args([
            "--client-id", "cid", "--tenant-id", "tid",
            "--client-secret", "--client-secret-env", "LAPSE_CLIENT_SECRET",
        ])
        self.assertTrue(args.client_secret)
        self.assertEqual(args.client_secret_env, "LAPSE_CLIENT_SECRET")

    def test_resolve_client_secret_from_env(self):
        args = lapse.parse_args([
            "--client-id", "cid", "--tenant-id", "tid",
            "--client-secret", "--client-secret-env", "LAPSE_CLIENT_SECRET",
        ])
        with unittest.mock.patch.dict(os.environ, {"LAPSE_CLIENT_SECRET": "secret-value"}):
            lapse.resolve_client_secret(args)
        self.assertEqual(args.client_secret_value, "secret-value")

    def test_rejects_multiple_client_secret_sources(self):
        args = lapse.parse_args([
            "--client-id", "cid", "--tenant-id", "tid",
            "--client-secret", "--client-secret-value", "inline",
            "--client-secret-env", "LAPSE_CLIENT_SECRET",
        ])
        with self.assertRaises(SystemExit):
            lapse.resolve_client_secret(args)


if __name__ == "__main__":
    unittest.main()
