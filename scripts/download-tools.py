from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from grga.toolchain import install_tools, platform_key, supported_tool_names


def parse_args():
    parser = argparse.ArgumentParser(description="Download bundled tools into src/grga/data/tools/<platform>.")
    parser.add_argument("names", nargs="*", help="Tool names. Defaults to all tools supported on this platform.")
    parser.add_argument("--base-url", help="Optional URL prefix for GitHub download mirrors.")
    parser.add_argument("--force", "--update", action="store_true", help="Re-download tools even if they already exist.")
    parser.add_argument("--list", action="store_true", help="List supported tools for this platform.")
    return parser.parse_args()


def main():
    args = parse_args()
    print(f"platform: {platform_key()}")
    if args.list:
        for name in supported_tool_names():
            print(name)
        return 0
    try:
        results = install_tools(names=args.names or None, force=args.force, base_url=args.base_url, progress=lambda message: print(message, flush=True))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for item in results:
        print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
