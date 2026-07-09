# Tool resolution order

`grga` needs a handful of external binaries: `rg`, `rga`, `rga-preproc`,
`pandoc`, `pdftotext`, `fzf`. All of them are optional at install time and
resolved lazily on first use.

## Priority

Default (`managed` → `system`):

1. **Managed cache**: `$GRGA_TOOLS_DIR` / `$XDG_DATA_HOME/grga/tools/<platform>` /
   `~/.local/share/grga/tools/<platform>` (Linux/macOS) or
   `%LOCALAPPDATA%\grga\tools\<platform>` (Windows).
2. **Legacy cache**: `<site-packages>/grga/data/tools/<platform>/`, retained so
   pre-0.2.0 installations keep working. Run `grga tools migrate` to move
   these files into the XDG cache.
3. **System PATH** via `shutil.which`.

Set `GRGA_PREFER_SYSTEM=1` (or pass `--prefer-system` where accepted) to
invert priority to `system` → `managed`.

## Minimum-version gates

`toolchain.MIN_VERSIONS` maps tool name → minimum version. On resolve,
if a candidate returns an older version, the next candidate is tried.
Ships empty in 0.2.0; future point releases may add entries (e.g.
`{"rg": "13.0.0"}`).

## First-use flow

1. CLI calls `_guard_tools([...])` which invokes `toolchain.ensure_tools`.
2. If everything is resolvable, the command runs.
3. If tools are missing:
   * **Interactive (tty, not CI, not `--json`)** — user is prompted
     `Install missing tools ...? [y/N]`. `y` installs, `n` returns
     exit code 3 (`user_abort`).
   * **Non-interactive or `--json`** — the CLI emits a structured
     `missing_tools` payload and exits with code 4.
   * **`--offline` / `GRGA_OFFLINE=1`** — the CLI emits `offline_and_missing`
     with `manual_urls` and exits with code 5.

See `docs/exit-codes.md` for the full JSON payload schema.
