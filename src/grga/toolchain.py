import json
import os
import platform
import re
import shutil
import stat
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

TOOLS = ("rg", "rga", "rga-preproc", "pandoc", "pdftotext", "fzf")
PACKAGE_ROOT = Path(__file__).resolve().parent
TOOLS_ROOT = PACKAGE_ROOT / "data" / "tools"
MANIFEST_NAME = "tools.json"
DOWNLOAD_TIMEOUT = 120
DOWNLOAD_RETRIES = 3


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
    return TOOLS_ROOT


def current_tools_dir():
    return TOOLS_ROOT / platform_key()


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
                "linux-aarch64": r"ripgrep_all-.*-aarch64-unknown-linux-musl\.tar\.gz$",
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
    root = current_tools_dir()
    if not root.exists():
        return []
    dirs = set()
    for command in TOOLS:
        path = find_managed_tool(command)
        if path:
            dirs.add(str(Path(path).parent))
    return sorted(dirs)


def add_tools_to_path():
    dirs = managed_bin_dirs()
    if not dirs:
        return
    current = os.environ.get("PATH", "")
    existing = current.split(os.pathsep) if current else []
    prefix = [item for item in dirs if item not in existing]
    if prefix:
        os.environ["PATH"] = os.pathsep.join(prefix + existing)


def find_tool(command):
    managed = find_managed_tool(command)
    if managed:
        return managed
    return shutil.which(command)


def find_managed_tool(command):
    name = _exe_name(command)
    for path in _candidate_tool_paths(command):
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
            try:
                resolved.relative_to(current_tools_dir())
                source = "bundled"
            except ValueError:
                source = "system"
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


def install_tools(names=None, force=False, base_url=None):
    selected = normalize_tool_names(names)
    manifest = load_manifest()
    installed = []
    for name in selected:
        installed.append(install_tool(name, force=force, base_url=base_url, manifest=manifest))
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


def install_tool(name, force=False, base_url=None, manifest=None):
    name = normalize_tool_names([name])[0]
    specs = tool_specs()
    spec = specs[name]
    target_dir = current_tools_dir() / spec.name
    if target_dir.exists() and not force:
        return {"name": name, "status": "exists", "path": _installed_path_for_spec(spec)}

    release = latest_release(spec.repo, base_url=base_url)
    asset = select_asset(spec, release)
    archive = download_asset(asset["browser_download_url"], asset["name"], base_url=base_url)
    with tempfile.TemporaryDirectory(prefix=f"grga-{name}-") as tmp:
        extracted = Path(tmp) / "extracted"
        extracted.mkdir(parents=True, exist_ok=True)
        unpack_archive(archive, extracted)
        _remove_download_archive(archive)
        replacement = Path(tmp) / spec.name
        replacement.mkdir(parents=True, exist_ok=True)
        copied = copy_executables(spec, extracted, replacement)
        if not copied:
            raise RuntimeError(f"no executable matched for {name} in {asset['name']}")
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(replacement), str(target_dir))

    if manifest is None:
        manifest = load_manifest()
    manifest.setdefault("tools", {})[name] = {
        "repo": spec.repo,
        "version": release.get("tag_name"),
        "asset": asset["name"],
        "path": _manifest_path_for_spec(spec),
    }
    return {"name": name, "status": "installed", "path": _installed_path_for_spec(spec), "version": release.get("tag_name")}


def update_tools(names=None, base_url=None):
    return install_tools(names=names, force=True, base_url=base_url)


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


def download_asset(url, filename, base_url=None):
    target_dir = current_tools_dir() / ".downloads"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    download_url = _rewrite_url(url, base_url)
    _download_file(download_url, target)
    return target


def _download_file(url, target):
    last_error = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "grga-toolchain"})
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
                with open(target, "wb") as handle:
                    shutil.copyfileobj(response, handle)
            return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            try:
                target.unlink()
            except OSError:
                pass
            if attempt < DOWNLOAD_RETRIES:
                time.sleep(attempt)
    raise RuntimeError(f"download failed: {url}: {last_error}")


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
