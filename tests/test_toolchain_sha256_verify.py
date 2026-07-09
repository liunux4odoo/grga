"""sha256 verification path coverage (TASK-sha256-tests.md).

These tests cover the security-critical sha256 pipeline: hashing, strict
mismatch failure, the two escape hatches (``--no-verify`` CLI flag and
``GRGA_INSECURE=1`` env var), trust-on-first-use, and the CLI-level
contracts (exit code 8, JSON payload shape, real process return code).

All tests are fully offline: no network, no system tools. Archives are
built on the fly inside ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grga import toolchain
from grga.cli import app
from grga.exit_codes import EXIT_VERIFY_FAIL
from grga.toolchain import (
    VerifyError,
    _asset_sha256_from,
    _verify_archive,
    install_tools,
    install_tools_from_dir,
    platform_key,
    sha256_file,
    tool_specs,
)

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
WRONG_SHA256 = "0" * 64


# ---------- fixture helpers ----------


def _rg_asset_name_and_archive_bytes(spec_name="rg"):
    """Build a real archive matching the rg asset regex for the current platform.

    Returns ``(asset_name, archive_bytes, is_zip)``. The archive contains an
    executable named ``rg`` (or ``rg.exe`` on windows) so ``copy_executables``
    can succeed during install.
    """
    key = platform_key()
    spec = tool_specs()[spec_name]
    if key not in spec.asset_patterns:
        pytest.skip(f"no asset rule for {spec_name} on {key}")
    is_windows = key.startswith("windows-")
    exe_name = "rg.exe" if is_windows else "rg"
    exe_body = b"MZ\x90\x00" if is_windows else b"#!/bin/sh\necho rg $@\n"

    if key == "linux-x86_64":
        asset = "ripgrep-14.0.0-x86_64-unknown-linux-musl.tar.gz"
    elif key == "linux-aarch64":
        asset = "ripgrep-14.0.0-aarch64-unknown-linux-gnu.tar.gz"
    elif key == "macos-x86_64":
        asset = "ripgrep-14.0.0-x86_64-apple-darwin.tar.gz"
    elif key == "macos-aarch64":
        asset = "ripgrep-14.0.0-aarch64-apple-darwin.tar.gz"
    elif key == "windows-x86_64":
        asset = "ripgrep-14.0.0-x86_64-pc-windows-msvc.zip"
    else:
        pytest.skip(f"unsupported test platform: {key}")

    if asset.endswith(".zip"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"ripgrep-14.0.0/{exe_name}", exe_body)
        return asset, buf.getvalue(), True

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(f"ripgrep-14.0.0/{exe_name}")
        info.size = len(exe_body)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(exe_body))
    return asset, buf.getvalue(), False


def _write_mirror(tmp_path: Path, *, sha256_field):
    """Create a mirror layout with an rg asset and a tools.json manifest.

    ``sha256_field`` is what to write for the ``sha256`` key: a hex string,
    an empty string, or ``None`` (meaning: omit the field entirely).
    Returns ``(mirror_dir, asset_name, actual_sha256)``.
    """
    key = platform_key()
    mirror = tmp_path / "mirror"
    platform_dir = mirror / key
    platform_dir.mkdir(parents=True)
    asset_name, archive_bytes, _ = _rg_asset_name_and_archive_bytes()
    (platform_dir / asset_name).write_bytes(archive_bytes)
    actual_sha = hashlib.sha256(archive_bytes).hexdigest()

    entry = {"repo": "BurntSushi/ripgrep", "asset": asset_name, "version": "14.0.0"}
    if sha256_field is not None:
        entry["sha256"] = sha256_field
    manifest = {"platform": key, "tools": {"rg": entry}}
    (platform_dir / "tools.json").write_text(json.dumps(manifest))
    return mirror, asset_name, actual_sha


@pytest.fixture
def isolated_tools_dir(monkeypatch, tmp_path):
    """Point GRGA_TOOLS_DIR at a private tmp cache and clear related env."""
    cache = tmp_path / "tools_cache"
    cache.mkdir()
    monkeypatch.setenv("GRGA_TOOLS_DIR", str(cache))
    monkeypatch.delenv("GRGA_INSECURE", raising=False)
    monkeypatch.delenv("GRGA_PREFER_SYSTEM", raising=False)
    monkeypatch.delenv("GRGA_OFFLINE", raising=False)
    toolchain.invalidate_tool_cache()
    return cache


@pytest.fixture
def mismatch_archive(tmp_path):
    """A tmp archive whose expected (manifest) sha256 is intentionally wrong.

    Returns ``(archive_path, expected_wrong_sha, actual_sha)``.
    """
    archive = tmp_path / "asset.tar.gz"
    archive.write_bytes(b"pretend this is a downloaded release")
    actual = sha256_file(archive)
    return archive, WRONG_SHA256, actual


# ---------- sha256_file: hashing primitive ----------


def test_sha256_of_empty_file_matches_rfc_constant(tmp_path):
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert sha256_file(empty) == EMPTY_SHA256


def test_sha256_stable_across_repeated_calls(tmp_path):
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"ripgrep-all release archive placeholder")
    first = sha256_file(payload)
    second = sha256_file(payload)
    third = sha256_file(payload)
    assert first == second == third


def test_sha256_large_file_over_one_mb_is_stable(tmp_path):
    big = tmp_path / "big.bin"
    # 1.25 MB of deterministic pseudo-random data, larger than the 64 KiB
    # chunk read inside sha256_file so the streaming loop is exercised.
    chunk = bytes(range(256)) * 16  # 4096 B
    with open(big, "wb") as handle:
        for _ in range(320):  # 320 * 4096 = 1_310_720 B (~1.25 MiB)
            handle.write(chunk)
    assert big.stat().st_size > 1024 * 1024
    first = sha256_file(big)
    second = sha256_file(big)
    assert first == second
    # digest is stable independent length hex (64 chars)
    assert len(first) == 64 and int(first, 16) >= 0


# ---------- _verify_archive: strict path ----------


def test_verify_matching_hash_returns_silently(tmp_path):
    archive = tmp_path / "match.bin"
    archive.write_bytes(b"canonical release bytes")
    expected = sha256_file(archive)
    seen = []
    _verify_archive("rg", archive, expected, verify=True, progress=seen.append)
    assert any("sha256 verified" in msg for msg in seen)


def test_verify_mismatch_raises_verify_error(mismatch_archive):
    archive, wrong, _ = mismatch_archive
    with pytest.raises(VerifyError) as excinfo:
        _verify_archive("rg", archive, wrong, verify=True, progress=None)
    assert "sha256 mismatch" in str(excinfo.value)


def test_verify_missing_expected_sha_skips(tmp_path):
    archive = tmp_path / "any.bin"
    archive.write_bytes(b"content")
    seen = []
    _verify_archive("rg", archive, None, verify=True, progress=seen.append)
    _verify_archive("rg", archive, "", verify=True, progress=seen.append)
    assert seen and all("skipping verification" in msg for msg in seen)


# ---------- _asset_sha256_from: manifest parsing edge cases ----------


def test_empty_sha256_field_is_treated_as_missing():
    manifest = {"tools": {"rg": {"asset": "x.tar.gz", "sha256": ""}}}
    assert _asset_sha256_from(manifest, "rg", "x.tar.gz") is None


def test_null_sha256_field_is_treated_as_missing():
    manifest = {"tools": {"rg": {"asset": "x.tar.gz", "sha256": None}}}
    assert _asset_sha256_from(manifest, "rg", "x.tar.gz") is None


# ---------- escape hatches on the raw verify function ----------


def test_no_verify_flag_bypasses_mismatch_with_warning(mismatch_archive):
    archive, wrong, _ = mismatch_archive
    seen = []
    _verify_archive("rg", archive, wrong, verify=False, progress=seen.append)
    joined = "\n".join(seen)
    assert "WARNING sha256 mismatch ignored" in joined


def test_grga_insecure_env_bypasses_mismatch_with_warning(mismatch_archive, monkeypatch):
    archive, wrong, _ = mismatch_archive
    monkeypatch.setenv("GRGA_INSECURE", "1")
    seen = []
    # verify=True is the default; the env flag alone must open the hatch.
    _verify_archive("rg", archive, wrong, verify=True, progress=seen.append)
    joined = "\n".join(seen)
    assert "WARNING sha256 mismatch ignored" in joined


def test_no_verify_and_grga_insecure_are_equivalent(mismatch_archive, monkeypatch):
    """Same mismatch fixture, two hatch entrypoints, isomorphic outcome."""
    archive, wrong, _ = mismatch_archive

    # Path A: --no-verify (verify=False, env clean)
    monkeypatch.delenv("GRGA_INSECURE", raising=False)
    seen_a = []
    ret_a = _verify_archive("rg", archive, wrong, verify=False, progress=seen_a.append)

    # Path B: GRGA_INSECURE=1 (verify=True, env set)
    monkeypatch.setenv("GRGA_INSECURE", "1")
    seen_b = []
    ret_b = _verify_archive("rg", archive, wrong, verify=True, progress=seen_b.append)

    # Both return None (no exception).
    assert ret_a is None and ret_b is None
    # Both emit exactly one WARNING line with the same shape.
    warn_a = [m for m in seen_a if "WARNING sha256 mismatch ignored" in m]
    warn_b = [m for m in seen_b if "WARNING sha256 mismatch ignored" in m]
    assert len(warn_a) == 1 and len(warn_b) == 1
    assert warn_a == warn_b  # identical wording, expected/actual pair


def test_both_escape_hatches_together_is_idempotent(mismatch_archive, monkeypatch):
    """Setting --no-verify AND GRGA_INSECURE=1 must not double-fire or conflict."""
    archive, wrong, _ = mismatch_archive
    monkeypatch.setenv("GRGA_INSECURE", "1")
    seen = []
    _verify_archive("rg", archive, wrong, verify=False, progress=seen.append)
    warnings = [m for m in seen if "WARNING sha256 mismatch ignored" in m]
    assert len(warnings) == 1


# ---------- case-sensitivity contract point ----------


@pytest.mark.xfail(
    reason=(
        "Potential contract gap: _verify_archive uses strict '==' between "
        "hex strings, so an uppercase expected sha256 in the manifest would "
        "spuriously fail even though it semantically matches. See task "
        "TASK-sha256-tests.md 'case difference' row. Do not fix in this "
        "commit; document the gap."
    ),
    strict=False,
)
def test_uppercase_expected_sha_matches_lowercase_actual(tmp_path):
    archive = tmp_path / "match.bin"
    archive.write_bytes(b"content bytes")
    expected_upper = sha256_file(archive).upper()
    seen = []
    _verify_archive("rg", archive, expected_upper, verify=True, progress=seen.append)
    assert any("sha256 verified" in msg for msg in seen)


# ---------- offline mirror install: trust-on-first-use ----------


@pytest.mark.skipif(
    platform_key().startswith("windows-"),
    reason="mirror archive fixture uses POSIX-friendly tar.gz layout",
)
def test_manifest_without_sha256_pins_computed_hash_on_install(isolated_tools_dir, tmp_path):
    """Manifest missing the sha256 field → install still succeeds and the
    computed sha256 is written back to the runtime manifest (trust-on-first-use)."""
    mirror, asset_name, actual_sha = _write_mirror(tmp_path, sha256_field=None)
    progress = []
    report = install_tools_from_dir(
        mirror, names=["rg"], verify=True, progress=progress.append
    )
    assert report["installed"] == ["rg"], report
    written = toolchain.load_manifest()
    assert written["tools"]["rg"]["sha256"] == actual_sha
    assert any("trust-on-first-use" in msg for msg in progress)


@pytest.mark.skipif(
    platform_key().startswith("windows-"),
    reason="mirror archive fixture uses POSIX-friendly tar.gz layout",
)
def test_empty_sha256_field_falls_through_to_trust_on_first_use(isolated_tools_dir, tmp_path):
    """sha256 == '' must behave like a missing field, not like a hard fail."""
    mirror, asset_name, actual_sha = _write_mirror(tmp_path, sha256_field="")
    progress = []
    report = install_tools_from_dir(
        mirror, names=["rg"], verify=True, progress=progress.append
    )
    assert report["installed"] == ["rg"], report
    written = toolchain.load_manifest()
    assert written["tools"]["rg"]["sha256"] == actual_sha


# ---------- offline mirror install: mismatch strict / bypass ----------


@pytest.mark.skipif(
    platform_key().startswith("windows-"),
    reason="mirror archive fixture uses POSIX-friendly tar.gz layout",
)
def test_mirror_install_default_strict_mismatch_raises(isolated_tools_dir, tmp_path):
    mirror, _, _ = _write_mirror(tmp_path, sha256_field=WRONG_SHA256)
    report = install_tools_from_dir(
        mirror, names=["rg"], verify=True, progress=None
    )
    assert report["installed"] == []
    assert report["failed"] and report["failed"][0]["name"] == "rg"
    assert "sha256 mismatch" in report["failed"][0]["error"]


@pytest.mark.skipif(
    platform_key().startswith("windows-"),
    reason="mirror archive fixture uses POSIX-friendly tar.gz layout",
)
def test_mirror_install_no_verify_flag_bypasses_and_installs(isolated_tools_dir, tmp_path):
    mirror, _, _ = _write_mirror(tmp_path, sha256_field=WRONG_SHA256)
    progress = []
    report = install_tools_from_dir(
        mirror, names=["rg"], verify=False, progress=progress.append
    )
    assert report["installed"] == ["rg"], report
    assert any("WARNING sha256 mismatch ignored" in m for m in progress)


@pytest.mark.skipif(
    platform_key().startswith("windows-"),
    reason="mirror archive fixture uses POSIX-friendly tar.gz layout",
)
def test_mirror_install_grga_insecure_env_bypasses_and_installs(
    isolated_tools_dir, tmp_path, monkeypatch
):
    mirror, _, _ = _write_mirror(tmp_path, sha256_field=WRONG_SHA256)
    monkeypatch.setenv("GRGA_INSECURE", "1")
    progress = []
    report = install_tools_from_dir(
        mirror, names=["rg"], verify=True, progress=progress.append
    )
    assert report["installed"] == ["rg"], report
    assert any("WARNING sha256 mismatch ignored" in m for m in progress)


@pytest.mark.skipif(
    platform_key().startswith("windows-"),
    reason="mirror archive fixture uses POSIX-friendly tar.gz layout",
)
def test_mirror_install_escape_hatch_equivalence(isolated_tools_dir, tmp_path, monkeypatch):
    """Same mismatch fixture, two hatches (no-verify vs GRGA_INSECURE):
    identical isomorphic outcome — both install, both warn once, both
    record a sha256 in the runtime manifest."""
    # Path A: --no-verify
    monkeypatch.delenv("GRGA_INSECURE", raising=False)
    mirror_a, _, _ = _write_mirror(tmp_path, sha256_field=WRONG_SHA256)
    progress_a = []
    report_a = install_tools_from_dir(
        mirror_a, names=["rg"], verify=False, progress=progress_a.append
    )
    manifest_after_a = json.loads(json.dumps(toolchain.load_manifest()))
    sha_a = manifest_after_a["tools"]["rg"].get("sha256")
    warn_a = [m for m in progress_a if "WARNING sha256 mismatch ignored" in m]

    # Reset the tools cache dir so path B starts from a clean state.
    import shutil as _shutil
    _shutil.rmtree(isolated_tools_dir, ignore_errors=True)
    isolated_tools_dir.mkdir()
    toolchain.invalidate_tool_cache()

    # Path B: GRGA_INSECURE=1
    monkeypatch.setenv("GRGA_INSECURE", "1")
    # rebuild the mirror in a fresh location so paths don't collide
    tmp_b = tmp_path / "run_b"
    tmp_b.mkdir()
    mirror_b, _, _ = _write_mirror(tmp_b, sha256_field=WRONG_SHA256)
    progress_b = []
    report_b = install_tools_from_dir(
        mirror_b, names=["rg"], verify=True, progress=progress_b.append
    )
    manifest_after_b = json.loads(json.dumps(toolchain.load_manifest()))
    sha_b = manifest_after_b["tools"]["rg"].get("sha256")
    warn_b = [m for m in progress_b if "WARNING sha256 mismatch ignored" in m]

    # Isomorphic outcome.
    assert report_a["installed"] == ["rg"]
    assert report_b["installed"] == ["rg"]
    assert len(warn_a) == 1 and len(warn_b) == 1
    assert sha_a and sha_b  # both write back some sha256
    # The written value is the (wrong) expected one from the manifest
    # in both paths — critically, they agree with each other.
    assert sha_a == sha_b == WRONG_SHA256


# ---------- online install: trust-on-first-use write-back ----------


@pytest.mark.skipif(
    platform_key().startswith("windows-"),
    reason="mirror archive fixture uses POSIX-friendly tar.gz layout",
)
def test_online_install_trust_on_first_use_pins_computed_sha(
    isolated_tools_dir, tmp_path, monkeypatch
):
    """Online path: manifest has no sha256 for the asset → installer writes
    the freshly computed sha256 back into the runtime manifest."""
    asset_name, archive_bytes, _ = _rg_asset_name_and_archive_bytes()
    archive_path = tmp_path / asset_name
    archive_path.write_bytes(archive_bytes)
    actual_sha = hashlib.sha256(archive_bytes).hexdigest()

    def fake_latest(repo, base_url=None):
        return {
            "tag_name": "v14.0.0",
            "assets": [{"name": asset_name, "browser_download_url": "https://example/none"}],
        }

    monkeypatch.setattr(toolchain, "latest_release", fake_latest)
    monkeypatch.setattr(
        toolchain, "download_asset", lambda url, filename, base_url=None, progress=None: archive_path
    )

    results = install_tools(names=["rg"], force=True, progress=None, verify=True)
    assert results and results[0]["status"] == "installed"
    written = toolchain.load_manifest()
    assert written["tools"]["rg"]["sha256"] == actual_sha


# ---------- CLI layer: mismatch + JSON payload shape ----------


def test_cli_tools_install_json_mismatch_exits_eight_with_error_name(monkeypatch):
    """CliRunner drives the app: mismatch + --json + no hatch → exit 8,
    stdout parses as JSON, payload carries the verify_fail signal."""
    def raise_verify(**kwargs):
        raise VerifyError("sha256 mismatch for rg: expected AAA, got BBB")

    # cli.py imported the name locally; patch there.
    monkeypatch.setattr("grga.cli.install_tools", raise_verify)
    runner = CliRunner()
    result = runner.invoke(app, ["tools", "install", "--json", "rg"])
    assert result.exit_code == EXIT_VERIFY_FAIL == 8
    payload = json.loads(result.output)
    assert payload["ok"] is False
    # Two acceptable signals per the task; assert both are consistent.
    assert payload["error_name"] == "verify_fail"
    assert payload["exit_code"] == 8
    assert "sha256 mismatch" in payload["message"]


def test_cli_tools_install_no_verify_emits_stderr_warning_with_kind(monkeypatch):
    """--no-verify must warn via the ``warning [insecure]:`` shape on stderr,
    not just 'something in stderr'."""
    monkeypatch.setattr("grga.cli.install_tools", lambda **kwargs: [])
    runner = CliRunner()
    result = runner.invoke(app, ["tools", "install", "--no-verify", "rg"])
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "warning [insecure]:" in result.stderr


# ---------- Real subprocess: authoritative process exit code ----------


_SUBPROC_HARNESS = textwrap.dedent(
    """
    import os, sys, json
    sys.path.insert(0, {src!r})
    # Force offline-safe: monkeypatch install_tools before the CLI imports it.
    from grga import toolchain
    from grga.toolchain import VerifyError

    def raise_verify(**kwargs):
        raise VerifyError("sha256 mismatch for rg: expected AAA, got BBB")

    # Patch at both call sites: the module and the cli-local rebinding.
    toolchain.install_tools = raise_verify
    import grga.cli as cli
    cli.install_tools = raise_verify

    sys.argv = ["grga", "tools", "install", "--json", "rg"]
    cli.main()
    """
).strip()


def test_subprocess_cli_install_mismatch_returns_real_exit_code_eight(tmp_path):
    """End-to-end via a real Python subprocess: the process return code
    must be 8. This is distinct from asserting payload['exit_code'] == 8;
    both are required by the task."""
    src_dir = str(Path(__file__).resolve().parent.parent / "src")
    script = tmp_path / "harness.py"
    script.write_text(_SUBPROC_HARNESS.format(src=src_dir))

    env = {k: v for k, v in os.environ.items() if not k.startswith("GRGA_")}
    env["GRGA_TOOLS_DIR"] = str(tmp_path / "tools_cache")
    (tmp_path / "tools_cache").mkdir()

    completed = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    # Process-level exit code — this is the security contract for callers.
    assert completed.returncode == 8, (completed.returncode, completed.stdout, completed.stderr)
    # Payload-level check runs on top; both signals independently asserted.
    payload = json.loads(completed.stdout)
    assert payload["error_name"] == "verify_fail"
    assert payload["exit_code"] == 8
