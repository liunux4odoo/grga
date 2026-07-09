# Developer release workflow

This project ships a single `py3-none-any` wheel; binaries are pulled at
first use. The release pipeline therefore looks like:

## 1. Refresh manifest hashes (optional but recommended)

```bash
scripts/download-tools.py --platform all --out ./mirror --write-hashes --yes
```

This downloads every supported asset and stamps sha256 into
`src/grga/data/tools/<platform>/tools.json` per platform. The generated
`./mirror/` tree also doubles as an air-gapped install source that end
users can consume via `grga tools install --from ./mirror`.

## 2. Build

```bash
rm -rf dist build *.egg-info
python -m build
```

Expected: exactly one wheel in `dist/`, no `data/tools/<platform>/*`
entries inside it, total size < 1 MB.

## 3. Metadata sanity

```bash
python -m twine check dist/*
```

## 4. TestPyPI smoke

Upload to TestPyPI, install in a fresh venv, then run:

```bash
grga --version
grga tools check --json          # missing tools, exit code 4 expected on json mode
grga tools install rg             # triggers first-run download
grga search --json some-term      # end-to-end
```

## 5. PyPI

Only after the TestPyPI smoke passes. Prefer OIDC trusted publishing to
avoid persisting long-lived tokens in CI.

## Notes

- `scripts/download-tools.py` is **not** a build hook; the wheel build
  never invokes it.
- The script is idempotent: assets whose sha256 already matches are
  skipped (`--force` reruns).
- `--platform all` continues on individual platform failures and exits
  with code 6 (`partial`).
