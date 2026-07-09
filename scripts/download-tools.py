"""Developer tool: pre-download bundled binaries and (optionally) write sha256.

This script is a *developer* utility. It is NOT invoked during
``python -m build`` — wheels ship without binaries, which are fetched at
runtime. Use this only to populate an air-gapped mirror or to refresh
``tools.json`` hashes before a release.
"""

from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from grga import toolchain
from grga.toolchain import (
    latest_release,
    platform_key,
    sha256_file,
    supported_tool_names,
    tool_specs,
)

ALL_PLATFORMS = (
    "linux-x86_64",
    "linux-aarch64",
    "macos-x86_64",
    "macos-aarch64",
    "windows-x86_64",
)


def parse_args():
    parser = argparse.ArgumentParser(description="Download bundled tools and manage sha256 hashes (developer tool).")
    parser.add_argument("names", nargs="*", help="Tool names. Defaults to all tools supported on the target platform.")
    parser.add_argument("--platform", help="Target platform key, or 'all'. Defaults to the current platform.")
    parser.add_argument("--out", help="Output directory for a mirror (defaults to the managed cache).")
    parser.add_argument("--base-url", help="Optional URL prefix for GitHub download mirrors.")
    parser.add_argument("--write-hashes", action="store_true", help="Compute and write sha256 into tools.json.")
    parser.add_argument("--force", "--update", action="store_true", help="Re-download tools even if they already exist.")
    parser.add_argument("--list", action="store_true", help="List supported tools for the current platform.")
    parser.add_argument("--yes", action="store_true", help="Auto-confirm sha256 overwrites.")
    return parser.parse_args()


def _platforms(arg):
    if not arg:
        return [platform_key()]
    if arg == "all":
        return list(ALL_PLATFORMS)
    return [arg]


def _supported_for(platform, names):
    specs = tool_specs()
    selected = names or [name for name, spec in specs.items() if platform in spec.asset_patterns]
    return [name for name in selected if platform in specs[name].asset_patterns]


def _mirror_asset(platform, name, out_dir, base_url, force, write_hashes, results):
    spec = tool_specs()[name]
    release = latest_release(spec.repo, base_url=base_url)
    asset = select_asset_for(spec, release, platform)
    if asset is None:
        results.append({"platform": platform, "name": name, "status": "no_asset"})
        return
    target_dir = out_dir / platform
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / asset["name"]
    if target.exists() and not force:
        results.append({"platform": platform, "name": name, "status": "skipped", "path": str(target)})
    else:
        _download(asset["browser_download_url"], target, base_url)
        results.append({"platform": platform, "name": name, "status": "downloaded", "path": str(target)})
    if write_hashes:
        digest = sha256_file(target)
        results[-1]["sha256"] = digest
        print(f"{platform}/{name}: sha256 {digest}")


def select_asset_for(spec, release, platform):
    import re

    pattern = spec.asset_patterns.get(platform)
    if not pattern:
        return None
    for asset in release.get("assets", []):
        if re.search(pattern, asset.get("name", ""), re.IGNORECASE):
            return asset
    return None


def _download(url, target, base_url):
    import urllib.request

    download_url = toolchain._rewrite_url(url, base_url)
    request = urllib.request.Request(download_url, headers={"User-Agent": "grga-download-tools"})
    with urllib.request.urlopen(request, timeout=toolchain.DOWNLOAD_TIMEOUT) as response:
        tmp = target.with_suffix(target.suffix + ".part")
        with open(tmp, "wb") as handle:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                handle.write(chunk)
        tmp.replace(target)
    print(f"downloaded: {target}")


def main():
    args = parse_args()
    print(f"current platform: {platform_key()}")
    if args.list:
        for name in supported_tool_names():
            print(name)
        return 0

    platforms = _platforms(args.platform)
    out_dir = Path(args.out) if args.out else None

    if out_dir is None and (args.platform and args.platform != platform_key()):
        print("error: cross-platform download requires --out <dir>", file=sys.stderr)
        return 2

    if out_dir is None:
        try:
            results = toolchain.install_tools(
                names=args.names or None,
                force=args.force,
                base_url=args.base_url,
                progress=lambda message: print(message, flush=True),
            )
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        for item in results:
            print(item)
        return 0

    results = []
    exit_code = 0
    for platform in platforms:
        names = _supported_for(platform, args.names or None)
        for name in names:
            try:
                _mirror_asset(platform, name, out_dir, args.base_url, args.force, args.write_hashes, results)
            except Exception as exc:
                results.append({"platform": platform, "name": name, "status": "error", "error": str(exc)})
                exit_code = 6
                print(f"error: {platform}/{name}: {exc}", file=sys.stderr)

    print("--- report ---")
    for item in results:
        print(item)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
