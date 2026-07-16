"""Developer tool: orchestrate a grga version release.

This mirrors the five-step flow in ``docs/dev-release.md`` (refresh hashes →
build → twine check → TestPyPI → PyPI) and adds semantic version bumping.
The version is written to **both** truth sources and kept in sync:

    * ``pyproject.toml``          -> ``[project].version``
    * ``src/grga/__init__.py``    -> ``__version__``  (read by ``grga --version``)

This is a *developer* utility and is NOT invoked during ``python -m build``.

Examples
--------
    # bump patch + build + twine check (default, no upload)
    python scripts/release.py --bump patch

    # major +1 (0.2.0 -> 1.0.0)
    python scripts/release.py --bump major

    # pin a version, refresh hashes, ship to TestPyPI
    python scripts/release.py --set 1.4.2 --refresh-hashes --testpypi

    # full release: minor +1, commit, publish, tag
    python scripts/release.py --bump minor --commit --publish --tag

    # preview only, touch nothing
    python scripts/release.py --bump patch --dry-run

Environment
-----------
    PYTHON            interpreter used for subprocesses (default: current)
    TWINE_USERNAME    usually ``__token__``
    TWINE_PASSWORD    PyPI / TestPyPI API token
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from enum import Enum
from pathlib import Path
from typing import List, Optional

import typer

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT_PY = REPO_ROOT / "src" / "grga" / "__init__.py"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

PYTHON = os.environ.get("PYTHON", sys.executable)
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Cut a grga release: bump version, build, verify and publish.",
)


class Part(str, Enum):
    major = "major"
    minor = "minor"
    patch = "patch"


# --------------------------------------------------------------------------- #
# pretty output helpers (styled after cli.py's typer.echo/secho usage)
# --------------------------------------------------------------------------- #
def _step(index: int, title: str) -> None:
    typer.secho(f"\n[{index}] {title}", fg=typer.colors.CYAN, bold=True)


def _info(msg: str) -> None:
    typer.secho(f"    {msg}", fg=typer.colors.BRIGHT_BLACK)


def _ok(msg: str) -> None:
    typer.secho(f"  \u2713 {msg}", fg=typer.colors.GREEN)


def _warn(msg: str) -> None:
    typer.secho(f"  ! {msg}", fg=typer.colors.YELLOW, err=True)


def _die(msg: str) -> None:
    typer.secho(f"  \u2717 {msg}", fg=typer.colors.RED, bold=True, err=True)
    raise typer.Exit(1)


def _run(cmd: List[str], *, dry: bool) -> None:
    typer.secho("    $ " + " ".join(cmd), fg=typer.colors.BRIGHT_BLACK)
    if dry:
        return
    try:
        subprocess.run(cmd, cwd=REPO_ROOT, check=True)
    except subprocess.CalledProcessError as exc:
        _die(f"command failed (exit {exc.returncode}): {' '.join(cmd)}")


# --------------------------------------------------------------------------- #
# version handling
# --------------------------------------------------------------------------- #
def _read_versions() -> tuple[str, str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    pyproject_ver = data["project"]["version"]
    m = re.search(
        r'^__version__\s*=\s*"([^"]+)"', INIT_PY.read_text(encoding="utf-8"), re.M
    )
    if not m:
        _die("could not locate __version__ in src/grga/__init__.py")
    return pyproject_ver, m.group(1)


def _bump(version: str, part: Part) -> str:
    m = SEMVER_RE.match(version)
    if not m:
        _die(f"current version {version!r} is not semantic X.Y.Z; cannot bump")
    major, minor, patch = (int(x) for x in m.groups())
    if part is Part.major:
        major, minor, patch = major + 1, 0, 0
    elif part is Part.minor:
        minor, patch = minor + 1, 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


def _write_versions(new: str, *, dry: bool) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r'(?m)^(version\s*=\s*)"[^"]+"', rf'\g<1>"{new}"', text, count=1
    )
    if n != 1:
        _die("unexpected number of version lines in pyproject.toml")
    if not dry:
        PYPROJECT.write_text(new_text, encoding="utf-8")
    _ok(f"pyproject.toml version -> {new}")

    itext = INIT_PY.read_text(encoding="utf-8")
    new_itext, n = re.subn(
        r'(?m)^(__version__\s*=\s*)"[^"]+"', rf'\g<1>"{new}"', itext, count=1
    )
    if n != 1:
        _die("unexpected number of __version__ lines in src/grga/__init__.py")
    if not dry:
        INIT_PY.write_text(new_itext, encoding="utf-8")
    _ok(f"src/grga/__init__.py __version__ -> {new}")


# --------------------------------------------------------------------------- #
# checks & stages
# --------------------------------------------------------------------------- #
def _ensure_clean_git(skip: bool) -> None:
    if skip:
        _warn("git cleanliness check skipped (--skip-git-check)")
        return
    out = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if out.strip():
        typer.echo(out)
        _die("working tree is dirty; commit or stash first (--skip-git-check to force)")
    _ok("working tree clean")


def _check_changelog(version: str) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    if not re.search(rf"(?m)^## \[({re.escape(version)}|Unreleased)\]", text):
        _die(f"CHANGELOG.md has no [{version}] or [Unreleased] section")
    _ok("CHANGELOG.md section present")


def _build(dry: bool, clean: bool) -> None:
    if clean:
        _run(
            [
                PYTHON,
                "-c",
                "import shutil,glob;"
                "[shutil.rmtree(p,ignore_errors=True) for p in ['dist','build']];"
                "[shutil.rmtree(p,ignore_errors=True) for p in glob.glob('*.egg-info')+glob.glob('src/*.egg-info')]",
            ],
            dry=dry,
        )
    _run([PYTHON, "-m", "build"], dry=dry)


def _check_wheel(dry: bool) -> None:
    if dry:
        _info("skip wheel inspection in --dry-run")
        return
    wheels = list((REPO_ROOT / "dist").glob("*.whl"))
    if len(wheels) != 1:
        _die(f"expected exactly 1 wheel in dist/, found {len(wheels)}: {wheels}")
    listing = subprocess.run(
        [PYTHON, "-m", "zipfile", "-l", str(wheels[0])],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if re.search(
        r"data/tools/(linux|macos|windows)/[^/]+/(rg|rga|pandoc|fzf)", listing
    ):
        _die("wheel unexpectedly bundles managed binaries (see docs/dev-release.md §2)")
    _ok(f"wheel clean: {wheels[0].name}")


def _dist_files() -> List[str]:
    return [str(p) for p in sorted((REPO_ROOT / "dist").glob("*")) if p.is_file()]


def _wheel_files() -> List[str]:
    return [str(p) for p in sorted((REPO_ROOT / "dist").glob("*.whl")) if p.is_file()]


def _twine_check(dry: bool) -> None:
    files = _dist_files()
    if not files and not dry:
        _die("no artifacts in dist/ to check")
    _run([PYTHON, "-m", "twine", "check", *files], dry=dry)


def _twine_upload(dry: bool, *, testpypi: bool) -> None:
    files = _wheel_files()
    if not files and not dry:
        _die("no wheel (*.whl) in dist/ to upload")
    cmd = [PYTHON, "-m", "twine", "upload"]
    if testpypi:
        cmd += ["--repository", "testpypi"]
    _run([*cmd, *files], dry=dry)


def _tag(version: str, dry: bool) -> None:
    tag = f"v{version}"
    exists = (
        subprocess.run(
            ["git", "rev-parse", tag], cwd=REPO_ROOT, capture_output=True
        ).returncode
        == 0
    )
    if exists:
        _warn(f"tag {tag} already exists; skipping")
        return
    _run(["git", "tag", "-a", tag, "-m", f"Release {tag}"], dry=dry)
    _run(["git", "push", "origin", tag], dry=dry)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
@app.command()
def release(
    bump: Optional[Part] = typer.Option(
        None, "--bump", help="Bump version: major | minor | patch.", case_sensitive=False
    ),
    set_version: Optional[str] = typer.Option(
        None, "--set", metavar="X.Y.Z", help="Set an explicit version instead of bumping."
    ),
    refresh_hashes: bool = typer.Option(
        False, "--refresh-hashes", help="Run download-tools.py to refresh tools.json sha256."
    ),
    testpypi: bool = typer.Option(False, "--testpypi", help="Upload to TestPyPI."),
    publish: bool = typer.Option(False, "--publish", help="Upload to the real PyPI."),
    tag: bool = typer.Option(False, "--tag", help="Create and push git tag v<version>."),
    commit: bool = typer.Option(False, "--commit", help="Commit the version bump."),
    no_clean: bool = typer.Option(False, "--no-clean", help="Do not wipe dist/build before build."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview everything; change nothing."),
    skip_git_check: bool = typer.Option(False, "--skip-git-check", help="Skip the clean-tree guard."),
):
    """Bump, build, verify and (optionally) publish a grga release.

    Defaults are safe: with no upload flags it only bumps (if asked), builds
    and runs ``twine check``. Uploading requires an explicit ``--testpypi`` or
    ``--publish``.
    """
    if bump is not None and set_version is not None:
        _die("--bump and --set are mutually exclusive")

    typer.secho(f"grga release", fg=typer.colors.MAGENTA, bold=True)
    _info(f"python:  {PYTHON}")
    _info(f"dry-run: {dry_run}")

    _step(0, "Preflight")
    _ensure_clean_git(skip_git_check)
    pyproject_ver, init_ver = _read_versions()
    if pyproject_ver != init_ver:
        _die(f"version mismatch: pyproject={pyproject_ver} __init__={init_ver}; align first")
    _ok(f"current version: {pyproject_ver}")

    if set_version is not None:
        if not SEMVER_RE.match(set_version):
            _die("--set must be X.Y.Z")
        target = set_version
    elif bump is not None:
        target = _bump(pyproject_ver, bump)
    else:
        target = pyproject_ver

    changed = target != pyproject_ver
    typer.secho(
        f"    version: {pyproject_ver} -> {target}"
        + ("" if changed else "  (unchanged)"),
        fg=typer.colors.WHITE,
        bold=True,
    )

    if changed:
        _step(1, "Write version")
        _write_versions(target, dry=dry_run)

    _step(2, "Changelog check")
    _check_changelog(target)

    if refresh_hashes:
        _step(3, "Refresh tool hashes")
        _run(
            [
                PYTHON,
                "scripts/download-tools.py",
                "--platform",
                "all",
                "--out",
                "./mirror",
                "--write-hashes",
                "--yes",
            ],
            dry=dry_run,
        )

    _step(4, "Build")
    _build(dry_run, clean=not no_clean)
    _check_wheel(dry_run)

    _step(5, "twine check")
    _twine_check(dry_run)

    if commit:
        if not changed:
            _warn("--commit given but version unchanged; nothing to commit")
        else:
            _step(6, "Commit version bump")
            _run(["git", "add", str(PYPROJECT), str(INIT_PY)], dry=dry_run)
            _run(["git", "commit", "-m", f"chore(release): v{target}"], dry=dry_run)

    if testpypi:
        _step(7, "Upload TestPyPI")
        _twine_upload(dry_run, testpypi=True)
        _info("smoke test hint:")
        _info(
            "  pip install -i https://test.pypi.org/simple/ "
            "--extra-index-url https://pypi.org/simple/ "
            f"grga=={target}"
        )

    if publish:
        _step(8, "Upload PyPI")
        _twine_upload(dry_run, testpypi=False)

    if tag:
        _step(9, "Git tag")
        _tag(target, dry=dry_run)

    typer.secho(
        f"\n\u2713 release flow complete (version {target}, dry-run={dry_run})",
        fg=typer.colors.GREEN,
        bold=True,
    )


if __name__ == "__main__":
    app()
