import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from grga.paths import resolve_state_dir, resolve_tools_dir, resolve_tools_root
from grga.runtime_env import insecure_enabled, prefer_system_tools

TOOLS = ("rg", "rga", "rga-preproc", "pandoc", "pdftotext", "fzf")
PACKAGE_ROOT = Path(__file__).resolve().parent
LEGACY_TOOLS_ROOT = PACKAGE_ROOT / "data" / "tools"
MANIFEST_NAME = "tools.json"
MIGRATION_SENTINEL = ".migrated"
DOWNLOAD_TIMEOUT = 120
DOWNLOAD_RETRIES = 3


class VerifyError(RuntimeError):
    """Raised when a downloaded asset fails sha256 verification."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    command: str
    repo: str
    asset_patterns: dict
    executable_patterns: tuple


@dataclass(frozen=True)
class ToolStatus:
    name: str
    command: str
    path: Optional[str]
    source: str
    version: Optional[str]


def platform_key():
    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in ("amd64", "x64"):
        machine = "x86_64"
    if machine in ("arm64",):
        machine = "aarch64"
    if system == "darwin":
        system = "macos"
    elif system == "windows":
        system = "windows"
    elif system == "linux":
        system = "linux"
    else:
        system = re.sub(r"[^a-z0-9]+", "-", system).strip("-") or "unknown"
    return f"{system}-{machine}"


def tools_root():
    return resolve_tools_root()


def current_tools_dir():
    return resolve_tools_dir(platform_key())


def legacy_tools_root():
    """Legacy site-packages tools directory (used by ``tools migrate``)."""
    return LEGACY_TOOLS_ROOT


def legacy_current_tools_dir():
    return LEGACY_TOOLS_ROOT / platform_key()


def manifest_path():
    return current_tools_dir() / MANIFEST_NAME


def tool_specs():
    return {
        "rg": ToolSpec(
            name="rg",
            command=_exe_name("rg"),
            repo="BurntSushi/ripgrep",
            asset_patterns={
                "linux-x86_64": r"ripgrep-.*-x86_64-unknown-linux-(?:musl|gnu)\.tar\.gz$",
                "linux-aarch64": r"ripgrep-.*-aarch64-unknown-linux-(?:musl|gnu)\.tar\.gz$",
                "macos-x86_64": r"ripgrep-.*-x86_64-apple-darwin\.tar\.gz$",
                "macos-aarch64": r"ripgrep-.*-aarch64-apple-darwin\.tar\.gz$",
                "windows-x86_64": r"ripgrep-.*-x86_64-pc-windows-msvc\.zip$",
            },
            executable_patterns=("**/rg", "**/rg.exe"),
        ),
        "rga": ToolSpec(
            name="rga",
            command=_exe_name("rga"),
            repo="phiresky/ripgrep-all",
            asset_patterns={
                "linux-x86_64": r"ripgrep_all-.*-x86_64-unknown-linux-musl\.tar\.gz$",
                "linux-aarch64": r"ripgrep_all-.*-aarch64-unknown-linux-(?:musl|gnu)\.tar\.gz$",
                "macos-x86_64": r"ripgrep_all-.*-x86_64-apple-darwin\.tar\.gz$",
                "macos-aarch64": r"ripgrep_all-.*-aarch64-apple-darwin\.tar\.gz$",
                "windows-x86_64": r"ripgrep_all-.*-x86_64-pc-windows-msvc\.zip$",
            },
            executable_patterns=("**/rga", "**/rga.exe", "**/rga-preproc", "**/rga-preproc.exe"),
        ),
        "pandoc": ToolSpec(
            name="pandoc",
            command=_exe_name("pandoc"),
            repo="jgm/pandoc",
            asset_patterns={
                "linux-x86_64": r"pandoc-.*-linux-amd64\.tar\.gz$",
                "linux-aarch64": r"pandoc-.*-linux-arm64\.tar\.gz$",
                "macos-x86_64": r"pandoc-.*-x86_64-macOS\.zip$",
                "macos-aarch64": r"pandoc-.*-arm64-macOS\.zip$",
                "windows-x86_64": r"pandoc-.*-windows-x86_64\.zip$",
            },
            executable_patterns=("**/pandoc", "**/pandoc.exe"),
        ),
        "poppler": ToolSpec(
            name="poppler",
            command=_exe_name("pdftotext"),
            repo="oschwartz10612/poppler-windows",
            asset_patterns={
                "windows-x86_64": r"Release-.*\.zip$",
            },
            executable_patterns=("**/pdftotext", "**/pdftotext.exe"),
        ),
        "fzf": ToolSpec(
            name="fzf",
            command=_exe_name("fzf"),
            repo="junegunn/fzf",
            asset_patterns={
                "linux-x86_64": r"fzf-.*-linux_amd64\.tar\.gz$",
                "linux-aarch64": r"fzf-.*-linux_arm64\.tar\.gz$",
                "macos-x86_64": r"fzf-.*-darwin_amd64\.zip$",
                "macos-aarch64": r"fzf-.*-darwin_arm64\.zip$",
                "windows-x86_64": r"fzf-.*-windows_amd64\.zip$",
            },
            executable_patterns=("**/fzf", "**/fzf.exe"),
        ),
    }


def managed_bin_dirs():
    dirs = set()
    for command in TOOLS:
        path = find_managed_tool(command)
        if path:
            dirs.add(str(Path(path).parent))
    return sorted(dirs)


def add_tools_to_path():
    global _PATH_STAMPED
    if _PATH_STAMPED is not None and _PATH_STAMPED == os.environ.get("PATH", ""):
        return
    dirs = managed_bin_dirs()
    if not dirs:
        _PATH_STAMPED = os.environ.get("PATH", "")
        return
    current = os.environ.get("PATH", "")
    existing = current.split(os.pathsep) if current else []
    prefix = [item for item in dirs if item not in existing]
    if prefix:
        os.environ["PATH"] = os.pathsep.join(prefix + existing)
    _PATH_STAMPED = os.environ.get("PATH", "")


# ── rga office adapters (custom_adapters config) ─────────────────────────
#
# rga 0.10.10's built-in pandoc adapter only whitelists epub/odt/docx/fb2/
# ipynb/html/htm (upstream custom.rs frozen since 2019, before pandoc 2.18
# added xlsx/pptx readers). The result: xlsx/pptx/doc/xls/ppt are *never*
# processed — "No adapter found", never cached, retried on every scan.
#
# grga solves this by registering a custom pandoc adapter named
# ``pandoc_office`` in rga's user config file (``custom_adapters``), written
# idempotently whenever tools are checked / installed. The binary is a bare
# ``pandoc``; grga's :func:`add_tools_to_path` puts the managed pandoc dir on
# PATH and rga-preproc inherits that env, so the relative name resolves.

RGA_OFFICE_ADAPTER_NAME = "pandoc_office"
RGA_OFFICE_ADAPTERS_JSONC = r"""{
  // Auto-generated by grga (toolchain.ensure_rga_office_config).
  // Adds a custom adapter so rga can extract xlsx/pptx/doc/xls/ppt via
  // pandoc — formats rga's built-in pandoc adapter misses. The config file
  // is a single source of truth; grga merges rather than overwrites.
  "custom_adapters": [
    {
      "name": "pandoc_office",
      "description": "Extract xlsx/pptx/doc/xls/ppt via pandoc (formats rga's built-in pandoc adapter misses)",
      "version": 1,
      "extensions": ["xlsx", "pptx", "xls", "doc", "ppt"],
      "binary": "pandoc",
      "args": ["--from=$input_file_extension", "--to=plain", "--wrap=none", "--markdown-headings=atx"],
      "disabled_by_default": false
    }
  ]
}
"""


def _strip_jsonc_comments(text):
    """Strip ``//`` line and ``/* */`` block comments from JSONC text.

    String literals (double or single quoted) are respected, so URLs like
    ``http://x`` and comments inside strings survive. Used before feeding a
    user config file to :func:`json.loads` when merging ``custom_adapters``.
    """
    out = []
    i = 0
    n = len(text)
    in_string = False
    string_char = ""
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == string_char:
                in_string = False
            i += 1
            continue
        if ch in ('"', "'"):
            in_string = True
            string_char = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if nxt == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def rga_config_path():
    """Return the user config file rga reads (platform-dependent).

    rga uses the ``directories`` crate conventions:
    - Linux: ``$XDG_CONFIG_HOME/ripgrep-all/config.jsonc`` or
      ``~/.config/ripgrep-all/config.jsonc``
    - macOS: ``~/Library/Application Support/ripgrep-all/config.jsonc``
    - Windows: ``%APPDATA%\\ripgrep-all\\config.jsonc``

    The file may not exist yet — rga treats a missing config as "all
    defaults" (verified empirically). The parent dir may also be missing.
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "ripgrep-all" / "config.jsonc"
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
        return base / "ripgrep-all" / "config.jsonc"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "ripgrep-all" / "config.jsonc"
    return Path.home() / ".config" / "ripgrep-all" / "config.jsonc"


def ensure_rga_office_config():
    """Idempotently register the ``pandoc_office`` custom adapter in rga's
    user config file, merging with any existing config.

    Called from tool checks / installs so the adapter is in place before
    rga-based searches run. Safe to call on every command — it no-ops once
    ``pandoc_office`` is present. Never raises: config problems degrade to
    "no office adapter" instead of breaking searches.
    """
    try:
        path = rga_config_path()
        existing = {}
        if path.exists():
            raw = path.read_text(encoding="utf-8", errors="replace")
            try:
                existing = json.loads(_strip_jsonc_comments(raw))
            except ValueError:
                # Unparseable user config — never destroy it; fall through to
                # a no-op and let rga keep using the file as-is.
                return
        adapters = existing.get("custom_adapters")
        if not isinstance(adapters, list):
            adapters = []
        if any(isinstance(a, dict) and a.get("name") == RGA_OFFICE_ADAPTER_NAME for a in adapters):
            return
        new_adapter = json.loads(_strip_jsonc_comments(RGA_OFFICE_ADAPTERS_JSONC))["custom_adapters"][0]
        adapters.append(new_adapter)
        existing["custom_adapters"] = adapters
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(existing, indent=2, ensure_ascii=False)
        path.write_text(body + "\n", encoding="utf-8")
    except (OSError, ValueError):
        # Best-effort only: config write failures must never block searches.
        return


def find_managed_tool(command):
    key = _cache_key(command)
    if key in _MANAGED_TOOL_CACHE:
        return _MANAGED_TOOL_CACHE[key]
    result = _resolve_managed_tool(command)
    _MANAGED_TOOL_CACHE[key] = result
    return result


def _resolve_managed_tool(command):
    name = _exe_name(command)
    for path in _candidate_tool_paths(command):
        if path.name == name and _is_executable_file(path):
            return str(path)
    for path in _legacy_candidate_tool_paths(command):
        if path.name == name and _is_executable_file(path):
            return str(path)
    return None


def check_tools(include_system=True):
    result = {}
    for command in TOOLS:
        path = find_tool(command) if include_system else find_managed_tool(command)
        source = "missing"
        if path:
            try:
                resolved = Path(path).resolve()
            except OSError:
                resolved = Path(path)
            source = "system"
            for base in (current_tools_dir(), legacy_current_tools_dir()):
                try:
                    resolved.relative_to(base.resolve())
                    source = "bundled"
                    break
                except (OSError, ValueError):
                    continue
        result[command] = {
            "path": path,
            "source": source,
            "version": tool_version(path) if path else None,
        }
    return {
        "platform": platform_key(),
        "tools_dir": str(current_tools_dir()),
        "tools": result,
    }


def tool_version(path):
    if not path:
        return None
    try:
        completed = subprocess_run([path, "--version"])
    except OSError:
        return None
    if completed["returncode"] != 0:
        return None
    first_line = completed["stdout"].splitlines()[0:1]
    return first_line[0] if first_line else None


def install_tools(names=None, force=False, base_url=None, progress=None, verify=True):
    selected = normalize_tool_names(names)
    if selected:
        _emit_progress(progress, "tools: " + ", ".join(selected))
    manifest = load_manifest()
    installed = []
    total = len(selected)
    for index, name in enumerate(selected, 1):
        _emit_progress(progress, f"[{index}/{total}] {name}: start")
        installed.append(install_tool(name, force=force, base_url=base_url, manifest=manifest, progress=progress, verify=verify))
        save_manifest(manifest)
    return installed


def normalize_tool_names(names=None):
    aliases = {
        "ripgrep": "rg",
        "ripgrep-all": "rga",
        "ripgrep_all": "rga",
        "repgrep-all": "rga",
        "rga-preproc": "rga",
        "pdftotext": "poppler",
    }
    selected = names or supported_tool_names()
    normalized = []
    for name in selected:
        key = aliases.get(name, name)
        if key not in tool_specs():
            raise ValueError(f"unknown tool: {name}")
        if key not in normalized:
            normalized.append(key)
    return normalized


def supported_tool_names():
    key = platform_key()
    return [name for name, spec in tool_specs().items() if key in spec.asset_patterns]


def install_tool(name, force=False, base_url=None, manifest=None, progress=None, verify=True):
    name = normalize_tool_names([name])[0]
    specs = tool_specs()
    spec = specs[name]
    target_dir = current_tools_dir() / spec.name
    installed_path = _installed_path_for_spec(spec)
    if installed_path and not force:
        _emit_progress(progress, f"{name}: already installed")
        return {"name": name, "status": "exists", "path": installed_path}

    _emit_progress(progress, f"{name}: fetching release metadata from {spec.repo}")
    release = latest_release(spec.repo, base_url=base_url)
    _emit_progress(progress, f"{name}: selecting asset for {platform_key()}")
    asset = select_asset(spec, release)
    expected_sha = _manifest_asset_sha256(name, asset.get("name"))
    _emit_progress(progress, f"{name}: downloading {asset['name']}")
    archive = download_asset(asset["browser_download_url"], asset["name"], base_url=base_url, progress=progress)
    _verify_archive(name, archive, expected_sha, verify=verify, progress=progress)
    computed_sha = sha256_file(archive)
    with tempfile.TemporaryDirectory(prefix=f"grga-{name}-") as tmp:
        extracted = Path(tmp) / "extracted"
        extracted.mkdir(parents=True, exist_ok=True)
        _emit_progress(progress, f"{name}: extracting {asset['name']}")
        unpack_archive(archive, extracted)
        _remove_download_archive(archive)
        replacement = Path(tmp) / spec.name
        replacement.mkdir(parents=True, exist_ok=True)
        _emit_progress(progress, f"{name}: collecting executables")
        copied = copy_executables(spec, extracted, replacement)
        if not copied:
            raise RuntimeError(f"no executable matched for {name} in {asset['name']}")
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(replacement), str(target_dir))
    invalidate_tool_cache()

    if manifest is None:
        manifest = load_manifest()
    tool_entry = manifest.setdefault("tools", {}).setdefault(name, {})
    tool_entry.update({
        "repo": spec.repo,
        "version": release.get("tag_name"),
        "asset": asset["name"],
        "path": _manifest_path_for_spec(spec),
        "sha256": expected_sha or computed_sha,
    })
    if not expected_sha:
        _emit_progress(progress, f"{name}: pinned sha256 {computed_sha} (trust-on-first-use)")
    result = {"name": name, "status": "installed", "path": _installed_path_for_spec(spec), "version": release.get("tag_name")}
    _emit_progress(progress, f"{name}: installed")
    return result


def install_tools_from_dir(src, names=None, verify=True, progress=None, fallback_online=False, base_url=None):
    """Install tools from a local mirror directory (offline).

    Layout accepted:
      * ``<src>/<platform>/<asset-filename>``
      * flat ``<src>/<asset-filename>``

    Returns a report dict with ``installed`` / ``failed`` / ``skipped``.
    """
    src = Path(src)
    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(f"source directory not found: {src}")

    selected = normalize_tool_names(names)
    manifest = load_manifest()
    key = platform_key()
    search_dirs = [src / key, src]
    mirror_manifest = _load_mirror_manifest(src, key)
    report = {"installed": [], "failed": [], "skipped": []}

    any_asset_present = any(
        d.exists() and any(p.is_file() for p in d.iterdir())
        for d in search_dirs
    )
    if not any_asset_present:
        raise FileNotFoundError(f"no assets found under {src}")

    for name in selected:
        spec = tool_specs()[name]
        pattern = spec.asset_patterns.get(key)
        if not pattern:
            report["skipped"].append({"name": name, "reason": "unsupported_platform"})
            continue
        asset_path = _find_local_asset(search_dirs, pattern)
        if asset_path is None:
            if fallback_online:
                try:
                    install_tool(name, force=True, base_url=base_url, manifest=manifest, progress=progress, verify=verify)
                    save_manifest(manifest)
                    report["installed"].append(name)
                    continue
                except Exception as exc:
                    report["failed"].append({"name": name, "error": f"online fallback failed: {exc}"})
                    continue
            report["failed"].append({"name": name, "error": "platform_mismatch"})
            continue
        try:
            _install_local_asset(name, spec, asset_path, manifest, verify=verify, progress=progress, mirror_manifest=mirror_manifest)
            save_manifest(manifest)
            report["installed"].append(name)
        except Exception as exc:
            report["failed"].append({"name": name, "error": str(exc)})
    return report


def _load_mirror_manifest(src, platform):
    """Return a dict for the mirror's tools.json, if any.

    Tries ``<src>/<platform>/tools.json`` first, then ``<src>/tools.json``.
    """
    for candidate in (src / platform / MANIFEST_NAME, src / MANIFEST_NAME):
        data = _read_manifest_at(candidate)
        if data.get("tools"):
            return data
    return {}


def _find_local_asset(search_dirs, pattern):
    for directory in search_dirs:
        if not directory.exists():
            continue
        for path in sorted(directory.iterdir()):
            if path.is_file() and re.search(pattern, path.name, re.IGNORECASE):
                return path
    return None


def _install_local_asset(name, spec, asset_path, manifest, verify=True, progress=None, mirror_manifest=None):
    target_dir = current_tools_dir() / spec.name
    expected_sha = _asset_sha256_from(mirror_manifest, name, asset_path.name) if mirror_manifest else None
    if not expected_sha:
        expected_sha = _manifest_asset_sha256(name, asset_path.name)
    _verify_archive(name, asset_path, expected_sha, verify=verify, progress=progress)
    computed_sha = sha256_file(asset_path)
    with tempfile.TemporaryDirectory(prefix=f"grga-{name}-") as tmp:
        extracted = Path(tmp) / "extracted"
        extracted.mkdir(parents=True, exist_ok=True)
        _emit_progress(progress, f"{name}: extracting {asset_path.name}")
        try:
            unpack_archive(asset_path, extracted)
        except Exception as exc:
            raise RuntimeError(f"unpack_failed: {exc}") from exc
        replacement = Path(tmp) / spec.name
        replacement.mkdir(parents=True, exist_ok=True)
        copied = copy_executables(spec, extracted, replacement)
        if not copied:
            raise RuntimeError(f"no executable matched for {name} in {asset_path.name}")
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(replacement), str(target_dir))
    invalidate_tool_cache()
    tool_entry = manifest.setdefault("tools", {}).setdefault(name, {})
    tool_entry.update({
        "repo": spec.repo,
        "asset": asset_path.name,
        "path": _manifest_path_for_spec(spec),
        "sha256": expected_sha or computed_sha,
    })
    mirror_version = _asset_version_from(mirror_manifest, name) if mirror_manifest else None
    if mirror_version:
        tool_entry["version"] = mirror_version
    if not expected_sha:
        _emit_progress(progress, f"{name}: pinned sha256 {computed_sha} (trust-on-first-use)")
    _emit_progress(progress, f"{name}: installed from {asset_path}")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_sha256_from(manifest, name, asset_name):
    entry = manifest.get("tools", {}).get(name) if isinstance(manifest, dict) else None
    if not isinstance(entry, dict):
        return None
    if entry.get("asset") == asset_name and entry.get("sha256"):
        return entry.get("sha256")
    return None


def _asset_version_from(manifest, name):
    entry = manifest.get("tools", {}).get(name) if isinstance(manifest, dict) else None
    if not isinstance(entry, dict):
        return None
    return entry.get("version")


def _manifest_asset_sha256(name, asset_name):
    return _asset_sha256_from(load_manifest(), name, asset_name)


def _verify_archive(name, archive, expected_sha, verify=True, progress=None):
    if not expected_sha:
        _emit_progress(progress, f"{name}: no sha256 in manifest, skipping verification")
        return
    actual = sha256_file(archive)
    if actual == expected_sha:
        _emit_progress(progress, f"{name}: sha256 verified")
        return
    if not verify or insecure_enabled():
        _emit_progress(progress, f"{name}: WARNING sha256 mismatch ignored (expected={expected_sha} actual={actual})")
        return
    raise VerifyError(f"sha256 mismatch for {name}: expected {expected_sha}, got {actual}")


def update_tools(names=None, base_url=None, progress=None, verify=True):
    return install_tools(names=names, force=True, base_url=base_url, progress=progress, verify=verify)


MIN_VERSIONS = {}

_MANAGED_TOOL_CACHE = {}
_PATH_STAMPED = None


def _cache_key(command):
    return (command, str(current_tools_dir()), str(legacy_current_tools_dir()))


def invalidate_tool_cache():
    """Drop cached managed-tool lookups.

    Call whenever the on-disk layout of the tools cache changes
    (install / update / migrate).
    """
    global _PATH_STAMPED
    _MANAGED_TOOL_CACHE.clear()
    _PATH_STAMPED = None


def find_tool(command):
    """Resolve a tool path preferring managed → system by default.

    ``GRGA_PREFER_SYSTEM=1`` flips priority to system → managed.

    Candidates are resolved lazily so the common case (a valid
    higher-priority hit) avoids the extra filesystem/``PATH`` scan.
    """
    prefer_system = prefer_system_tools()
    if prefer_system:
        resolvers = (lambda: shutil.which(command), lambda: find_managed_tool(command))
    else:
        resolvers = (lambda: find_managed_tool(command), lambda: shutil.which(command))

    candidates = []
    for resolve in resolvers:
        candidate = resolve()
        if candidate:
            candidates.append(candidate)
            if _passes_min_version(command, candidate):
                return candidate
    return candidates[0] if candidates else None



def _passes_min_version(command, path):
    minimum = MIN_VERSIONS.get(command)
    if not minimum:
        return True
    version = tool_version(path)
    if not version:
        return True
    match = re.search(r"(\d+(?:\.\d+)+)", version)
    if not match:
        return True
    return _version_ge(match.group(1), minimum)


TOOL_DEPENDENCIES = {
    "rga": ("rg",),
    "rga-preproc": ("rg",),
}

MANUAL_INSTALL_URLS = {
    "rg": "https://github.com/BurntSushi/ripgrep/releases",
    "rga": "https://github.com/phiresky/ripgrep-all/releases",
    "rga-preproc": "https://github.com/phiresky/ripgrep-all/releases",
    "pandoc": "https://github.com/jgm/pandoc/releases",
    "pdftotext": "https://poppler.freedesktop.org/",
    "fzf": "https://github.com/junegunn/fzf/releases",
}


class MissingToolsError(RuntimeError):
    def __init__(self, missing, message=None):
        self.missing = tuple(missing)
        super().__init__(message or f"missing tools: {', '.join(self.missing)}")


class OfflineError(RuntimeError):
    def __init__(self, missing):
        self.missing = tuple(missing)
        super().__init__(f"offline mode: cannot install missing tools: {', '.join(self.missing)}")


class UserAbortError(RuntimeError):
    def __init__(self, missing):
        self.missing = tuple(missing)
        super().__init__("user declined to install missing tools")


class PartialInstallError(RuntimeError):
    def __init__(self, installed, failed):
        self.installed = tuple(installed)
        self.failed = tuple(failed)
        super().__init__(f"partial install: installed={list(installed)} failed={list(failed)}")


def _expand_dependencies(required):
    """Expand a required tool list to include implicit dependencies.

    Dependencies are placed before the tool that requires them, and the
    result is de-duplicated while preserving first-seen order.
    """
    expanded = []
    seen = set()
    for name in required:
        for candidate in list(TOOL_DEPENDENCIES.get(name, ())) + [name]:
            if candidate not in seen:
                seen.add(candidate)
                expanded.append(candidate)
    return expanded


# Backwards-compatible alias.
_expand_required = _expand_dependencies


def ensure_tools(required, *, interactive=False, json_mode=False, offline=False, base_url=None, progress=None, prompt=None):
    """Guarantee that every tool in ``required`` (and its deps) is resolvable.

    Returns the list of missing tools *after* an install attempt (empty on
    success). Raises :class:`MissingToolsError`, :class:`OfflineError`,
    :class:`UserAbortError`, or :class:`PartialInstallError` on failure so
    the CLI can map them to stable exit codes.

    Empty ``required`` is a no-op.
    """
    if not required:
        return []

    maybe_migrate_legacy_tools(progress=progress)
    add_tools_to_path()
    expanded = _expand_dependencies(required)
    missing = [name for name in expanded if find_tool(name) is None]
    if not missing:
        return []

    if offline:
        raise OfflineError(missing)

    if not interactive or json_mode:
        raise MissingToolsError(missing)

    installable = _installable_from_missing(missing)
    if not installable:
        raise MissingToolsError(missing, message=f"tools not installable on this platform: {', '.join(missing)}")

    prompt = prompt or (lambda names: input(f"Install missing tools {', '.join(names)}? [y/N] ").strip().lower() == "y")
    try:
        approved = bool(prompt(installable))
    except (EOFError, KeyboardInterrupt):
        approved = False
    if not approved:
        raise UserAbortError(missing)

    installed = []
    failed = []
    for name in installable:
        try:
            install_tools(names=[name], base_url=base_url, progress=progress)
            installed.append(name)
        except Exception as exc:
            failed.append({"name": name, "error": str(exc)})
    still_missing = [name for name in expanded if find_tool(name) is None]
    if still_missing:
        raise PartialInstallError(installed, failed or still_missing)
    return []


def _installable_from_missing(missing):
    """Translate the resolved tool names back to installable spec names."""
    installable = []
    seen = set()
    for command in missing:
        try:
            spec_name = normalize_tool_names([command])[0]
        except ValueError:
            aliases = {
                "rga-preproc": "rga",
                "pdftotext": "poppler",
            }
            fallback = aliases.get(command)
            if not fallback:
                continue
            spec_name = fallback
        if spec_name in seen:
            continue
        if spec_name not in supported_tool_names():
            continue
        seen.add(spec_name)
        installable.append(spec_name)
    return installable


def _migration_sentinel_path():
    return resolve_state_dir() / MIGRATION_SENTINEL


def migrate_legacy_tools(dry_run=False, progress=None):
    """Copy tools from legacy site-packages dir into the XDG cache.

    Returns a report dict::

        {"migrated": [...], "skipped": [...], "errors": [...], "source": str,
         "target": str, "legacy_exists": bool}

    Never deletes the legacy source (pip may still manage it). Copies files
    (not symlinks) to survive cross-fs moves and uninstalls.
    """
    legacy = legacy_current_tools_dir()
    target = current_tools_dir()
    report = {
        "migrated": [],
        "skipped": [],
        "errors": [],
        "source": str(legacy),
        "target": str(target),
        "legacy_exists": legacy.exists(),
    }
    if not legacy.exists():
        _emit_progress(progress, "migrate: no legacy tools directory, nothing to do")
        return report

    legacy_manifest = _read_manifest_at(legacy / MANIFEST_NAME)
    target_manifest = _read_manifest_at(target / MANIFEST_NAME)

    for entry in sorted(legacy.iterdir()):
        if entry.name == MANIFEST_NAME:
            continue
        if not entry.is_dir():
            continue
        name = entry.name
        dest = target / name
        legacy_ver = _manifest_version(legacy_manifest, name)
        target_ver = _manifest_version(target_manifest, name)
        if dest.exists():
            if target_ver and legacy_ver and target_ver == legacy_ver:
                report["skipped"].append({"name": name, "reason": "same_version"})
                _emit_progress(progress, f"migrate: skip {name} (same version {target_ver})")
                continue
            if target_ver and legacy_ver and _version_ge(target_ver, legacy_ver):
                report["skipped"].append({"name": name, "reason": "target_newer"})
                _emit_progress(progress, f"migrate: skip {name} (target {target_ver} >= legacy {legacy_ver})")
                continue
            if not target_ver and not legacy_ver:
                report["skipped"].append({"name": name, "reason": "exists"})
                _emit_progress(progress, f"migrate: skip {name} (already present)")
                continue
        if dry_run:
            report["migrated"].append({"name": name, "dry_run": True})
            _emit_progress(progress, f"migrate: would copy {name} -> {dest}")
            continue
        try:
            target.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(entry, dest)
            for path in dest.rglob("*"):
                if path.is_file():
                    _ensure_executable(path)
            report["migrated"].append({"name": name, "path": str(dest)})
            _emit_progress(progress, f"migrate: copied {name} -> {dest}")
        except OSError as exc:
            report["errors"].append({"name": name, "error": str(exc)})
            _emit_progress(progress, f"migrate: error {name}: {exc}")

    if not dry_run and legacy_manifest.get("tools"):
        merged = target_manifest if target_manifest.get("tools") else {}
        merged.setdefault("platform", platform_key())
        merged.setdefault("tools", {})
        for name, meta in legacy_manifest.get("tools", {}).items():
            merged["tools"].setdefault(name, meta)
        try:
            save_manifest(merged)
        except OSError as exc:
            report["errors"].append({"name": MANIFEST_NAME, "error": str(exc)})

    if not dry_run and not report["errors"]:
        _write_migration_sentinel()
    if not dry_run and report["migrated"]:
        invalidate_tool_cache()
    return report


def maybe_migrate_legacy_tools(progress=None):
    """Lazy one-shot migration guarded by a sentinel file."""
    sentinel = _migration_sentinel_path()
    if sentinel.exists():
        return None
    if not legacy_current_tools_dir().exists():
        _write_migration_sentinel()
        return None
    return migrate_legacy_tools(dry_run=False, progress=progress)


def _write_migration_sentinel():
    try:
        sentinel = _migration_sentinel_path()
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(platform_key(), encoding="utf-8")
    except Exception:
        pass


def _read_manifest_at(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _manifest_version(manifest, name):
    tools = manifest.get("tools") if isinstance(manifest, dict) else None
    if not isinstance(tools, dict):
        return None
    entry = tools.get(name)
    if isinstance(entry, dict):
        return entry.get("version")
    return None


def _version_tuple(version):
    if not version:
        return ()
    cleaned = str(version).lstrip("vV")
    parts = re.split(r"[.\-+]", cleaned)
    numbers = []
    for part in parts:
        match = re.match(r"^(\d+)", part)
        if not match:
            break
        numbers.append(int(match.group(1)))
    return tuple(numbers)


def _version_ge(left, right):
    lt = _version_tuple(left)
    rt = _version_tuple(right)
    if not lt or not rt:
        return left == right
    return lt >= rt


def load_manifest():
    path = manifest_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("platform", platform_key())
    data.setdefault("tools", {})
    return data


def save_manifest(manifest):
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)


def latest_release(repo, base_url=None):
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    data = _read_url_json(_rewrite_url(url, base_url))
    if not isinstance(data, dict):
        raise RuntimeError(f"invalid GitHub release response for {repo}")
    return data


def select_asset(spec, release):
    key = platform_key()
    pattern = spec.asset_patterns.get(key)
    if not pattern:
        raise RuntimeError(f"{spec.name} has no bundled asset rule for {key}")
    assets = release.get("assets", [])
    for asset in assets:
        name = asset.get("name", "")
        if re.search(pattern, name, re.IGNORECASE):
            return asset
    names = ", ".join(asset.get("name", "") for asset in assets)
    raise RuntimeError(f"no release asset matched {spec.name} for {key}: {names}")


def download_asset(url, filename, base_url=None, progress=None):
    target_dir = current_tools_dir() / ".downloads"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    download_url = _rewrite_url(url, base_url)
    _download_file(download_url, target, progress=progress, label=filename)
    return target


def _download_file(url, target, progress=None, label=None):
    last_error = None
    label = label or target.name
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "grga-toolchain"})
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
                total = _content_length(response)
                downloaded = 0
                last_reported = -1
                with open(target, "wb") as handle:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        handle.write(chunk)
                        downloaded += len(chunk)
                        last_reported = _report_download(progress, label, downloaded, total, last_reported)
            _emit_progress(progress, f"{label}: download complete ({_format_size(downloaded)})")
            return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            try:
                target.unlink()
            except OSError:
                pass
            if attempt < DOWNLOAD_RETRIES:
                _emit_progress(progress, f"{label}: retry {attempt}/{DOWNLOAD_RETRIES - 1} after error: {exc}")
                time.sleep(attempt)
    raise RuntimeError(f"download failed: {url}: {last_error}")


def _content_length(response):
    try:
        return int(response.headers.get("Content-Length", 0)) or None
    except (TypeError, ValueError):
        return None


def _report_download(progress, label, downloaded, total, last_reported):
    if progress is None:
        return last_reported
    if total:
        percent = int(downloaded * 100 / total)
        if percent >= last_reported + 5 or percent == 100:
            _emit_progress(progress, f"{label}: {percent}% ({_format_size(downloaded)}/{_format_size(total)})")
            return percent
        return last_reported
    step = downloaded // (1024 * 1024)
    if step != last_reported:
        _emit_progress(progress, f"{label}: {_format_size(downloaded)} downloaded")
        return step
    return last_reported


def _format_size(num):
    if not num:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    value = float(num)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{num} B"


def _emit_progress(progress, message):
    if progress is None:
        return
    try:
        progress(message)
    except Exception:
        pass


def unpack_archive(archive, target):
    suffixes = "".join(archive.suffixes).lower()
    if suffixes.endswith(".tar.gz") or suffixes.endswith(".tgz") or suffixes.endswith(".tar.xz"):
        with tarfile.open(archive, "r:*") as handle:
            _safe_extract_tar(handle, target)
        return
    if suffixes.endswith(".zip"):
        with zipfile.ZipFile(archive) as handle:
            _safe_extract_zip(handle, target)
        return
    raise RuntimeError(f"unsupported archive format: {archive.name}")


def _safe_extract_tar(handle, target):
    target = target.resolve()
    for member in handle.getmembers():
        destination = (target / member.name).resolve()
        if not _is_within(target, destination):
            raise RuntimeError(f"unsafe path in archive: {member.name}")
    handle.extractall(target)


def _safe_extract_zip(handle, target):
    target = target.resolve()
    for name in handle.namelist():
        destination = (target / name).resolve()
        if not _is_within(target, destination):
            raise RuntimeError(f"unsafe path in archive: {name}")
    handle.extractall(target)


def _is_within(root, path):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False



def _remove_download_archive(path):
    try:
        path.unlink()
    except OSError:
        pass


def copy_executables(spec, source, target):
    copied = []
    seen = set()
    for pattern in spec.executable_patterns:
        for path in source.glob(pattern):
            if not path.is_file():
                continue
            name = path.name
            if name in seen:
                continue
            destination = target / name
            shutil.copy2(path, destination)
            _ensure_executable(destination)
            copied.append(str(destination))
            seen.add(name)
    return copied


def subprocess_run(command):
    import subprocess

    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
    return {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}


def _candidate_tool_paths(command):
    root = current_tools_dir()
    if not root.exists():
        return []
    name = _exe_name(command)
    return list(root.glob(f"**/{name}"))


def _legacy_candidate_tool_paths(command):
    root = legacy_current_tools_dir()
    if not root.exists():
        return []
    name = _exe_name(command)
    return list(root.glob(f"**/{name}"))


def _installed_path_for_spec(spec):
    return find_managed_tool(spec.command)


def _manifest_path_for_spec(spec):
    path = find_managed_tool(spec.command)
    if not path:
        return None
    try:
        return str(Path(path).resolve().relative_to(current_tools_dir().resolve()))
    except ValueError:
        return path


def _exe_name(command):
    if platform.system().lower() == "windows" and not command.endswith(".exe"):
        return f"{command}.exe"
    return command


def _is_executable_file(path):
    if not path.is_file():
        return False
    if platform.system().lower() == "windows":
        return path.suffix.lower() == ".exe"
    if os.access(path, os.X_OK):
        return True
    try:
        _ensure_executable(path)
    except OSError:
        return False
    return os.access(path, os.X_OK)


def _ensure_executable(path):
    if platform.system().lower() == "windows":
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _read_url_json(url):
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "grga-toolchain"})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"download metadata failed: {url}: {exc}") from exc


def _rewrite_url(url, base_url):
    if not base_url:
        return url
    return base_url.rstrip("/") + "/" + url
