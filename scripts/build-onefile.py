"""Build grga as a single standalone executable with PyInstaller.

External tools (rg / rga / pandoc / fzf) are **not** bundled into the
executable — they are resolved via ``grga.toolchain`` at runtime, either
from ``$PATH`` or from the managed XDG cache. Users can prime the cache
before shipping by running ``grga tools install`` on the target machine,
or bundling their own PATH.
"""

from pathlib import Path
import argparse
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DIST = ROOT / "dist"
BUILD = ROOT / "build" / "pyinstaller"
EXCLUDED_MODULES = (
    "IPython",
    "PIL",
    "black",
    "cv2",
    "jedi",
    "matplotlib",
    "numpy",
    "onnxruntime",
    "openpyxl",
    "pandas",
    "pyarrow",
    "scipy",
    "sklearn",
    "sympy",
    "torch",
    "torchvision",
    "transformers",
    "triton",
)


def parse_args():
    parser = argparse.ArgumentParser(description="Build grga as a single standalone executable with PyInstaller.")
    parser.add_argument("--name", default="grga", help="Executable name. Defaults to grga.")
    parser.add_argument("--gui", action="store_true", help="Build the GUI entry instead of the CLI entry.")
    parser.add_argument("--clean", action="store_true", help="Remove previous PyInstaller build files before building.")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.clean:
        clean(args.name)

    entry = "grga.gui" if args.gui else "grga.cli"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--name",
        args.name,
        "--paths",
        str(SRC),
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
        "--specpath",
        str(BUILD),
    ]
    for module in EXCLUDED_MODULES:
        command.extend(["--exclude-module", module])
    if args.gui:
        command.append("--windowed")
    command.append(str(SRC / "grga" / "__main__.py") if not args.gui else module_entry(entry))
    run(command)
    print(DIST / args.name)
    return 0


def module_entry(module):
    target = BUILD / f"{module.replace('.', '_')}_entry.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"from {module} import main\n\nif __name__ == '__main__':\n    main()\n", encoding="utf-8")
    return str(target)


def clean(name):
    shutil.rmtree(BUILD, ignore_errors=True)
    target = DIST / name
    if target.exists():
        target.unlink()


def run(command):
    try:
        subprocess.run(command, cwd=str(ROOT), check=True)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc


if __name__ == "__main__":
    raise SystemExit(main())
