# grga exit codes (0.2.0 contract, frozen)

The CLI uses a stable numeric exit-code contract so downstream agents and
scripts can branch on failure kind without parsing text. Codes may be
added in future releases, but existing codes are never renumbered or
removed.

| Code | Kind              | When                                                                          |
| ---- | ----------------- | ----------------------------------------------------------------------------- |
| 0    | `ok`              | Success                                                                       |
| 1    | `generic`         | Unspecified runtime error                                                     |
| 2    | `usage`           | Invalid CLI arguments (Typer usage error, mutually-exclusive flags, ...)      |
| 3    | `user_abort`      | User declined an interactive prompt (e.g. tool install)                       |
| 4    | `missing_tool`    | Required tool missing and caller is non-interactive or `--json`               |
| 5    | `offline`         | Required tool missing and `--offline` (or `GRGA_OFFLINE=1`) was set           |
| 6    | `partial_install` | Some tools installed, others failed                                           |
| 7    | `download_fail`   | Network / release fetch failed                                                |
| 8    | `verify_fail`     | sha256 mismatch on downloaded asset                                           |
| 9    | `io`              | I/O error (permission, missing dir, unpack failure, read-only filesystem, ...)|

## `--json` error schema

Every failure path emits a single JSON object to *stdout* when `--json`
is passed:

```json
{
  "ok": false,
  "error": "missing_tools",
  "message": "missing tools: rga",
  "exit_code": 4,
  "missing": ["rga"],
  "install_hint": "grga tools install"
}
```

`kind` (`error`) is a stable machine-readable slug. `message` is the
human-readable summary. Additional fields depend on the error kind
(`missing`, `manual_urls`, `expected`, `actual`, `installed`, `failed`,
...). Warnings (e.g. `--no-verify`) go to *stderr* as a single JSON line
so they do not pollute stdout.

## Environment variables

- `GRGA_OFFLINE=1` — same as `--offline`; fail fast when tools missing.
- `GRGA_TOOLS_DIR=/path` — override the managed-tools cache root.
- `GRGA_STATE_DIR=/path` — override where state files (migration sentinel) live.
- `GRGA_PREFER_SYSTEM=1` — prefer system-PATH binaries over managed ones.
- `GRGA_INSECURE=1` — allow sha256 mismatches to pass with a warning.
- `CI=1` (and GitHub / GitLab / CircleCI / Travis / Azure equivalents) —
  treated as non-interactive.
