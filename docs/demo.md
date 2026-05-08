# lapse Demo

This demo uses sanitized device names and IDs. It shows the intended review
workflow: identify candidates, verify interactive sign-in evidence, then report
without making changes by default.

![Sanitized lapse terminal output](assets/lapse-sample-output.svg)

## What To Notice

- The first count is only the Graph timestamp candidate set.
- The second step checks interactive sign-in logs before marking devices stale.
- `--dry-run` gives the operator a safe review point before disable or delete.

## Example Command

```bash
lp --client-id <id> --tenant-id <tenant> --days 120 --company-only --skip-vdi --dry-run
```

## Example Interpretation

The output separates directory age from interactive sign-in evidence. That
distinction is the point of lapse: stale-device review should not rely on a
single timestamp that may be refreshed by background activity.
