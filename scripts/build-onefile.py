from pathlib import Path
import argparse
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DATA_TOOLS = SRC / "grga" / "data" / "tools"
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
    parser.add_argument("--download-tools", action="store_true", help="Download bundled tools before building.")
    parser.add_argument("--clean", action="store_true", help="Remove previous PyInstaller build files before building.")
    parser.add_argument("--no-tools", action="store_true", help="Do not bundle src/grga/data/tools into the executable.")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.clean:
        clean(args.name)
    if args.download_tools:
        run([sys.executable, str(ROOT / "scripts" / "download-tools.py")])
    if not args.no_tools and not DATA_TOOLS.exists():
        print("warning: src/grga/data/tools does not exist; build will fall back to tools in PATH", file=sys.stderr)

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
    if not args.no_tools and DATA_TOOLS.exists():
        command.extend(["--add-data", f"{DATA_TOOLS}:grga/data/tools"])
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
