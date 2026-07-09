"""Install bundled grga skills into a target directory.

Each skill lives under ``src/grga/skills/<name>/``. The install command
copies a skill tree into ``<dest>/<name>/`` (leaving other skills alone),
optionally overwriting with ``--force``.
"""

import json
import shutil
from pathlib import Path
from typing import Dict, List, Optional

PACKAGE_ROOT = Path(__file__).resolve().parent
SKILLS_ROOT = PACKAGE_ROOT / "skills"


def skills_root() -> Path:
    return SKILLS_ROOT


def list_skills() -> List[Dict[str, str]]:
    """Return metadata for every skill shipped with the package."""
    if not SKILLS_ROOT.exists():
        return []
    result = []
    for entry in sorted(SKILLS_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            continue
        result.append({"name": entry.name, "path": str(entry), "skill_md": str(skill_md)})
    return result


def install_skill(name: str, destination: Path, force: bool = False) -> Dict[str, object]:
    """Copy a bundled skill into ``destination``.

    Returns a report dict::

        {"name": name, "status": "installed"|"exists", "source": ..., "target": ...}

    Raises :class:`FileNotFoundError` for unknown skills and
    :class:`FileExistsError` when the target already exists without
    ``force``.
    """
    source = SKILLS_ROOT / name
    if not source.exists() or not source.is_dir():
        raise FileNotFoundError(f"unknown skill: {name}")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / name
    if target.exists():
        if not force:
            raise FileExistsError(f"target already exists: {target} (use --force to overwrite)")
        shutil.rmtree(target)
    shutil.copytree(source, target)
    return {
        "name": name,
        "status": "installed",
        "source": str(source),
        "target": str(target),
    }


def install_all(destination: Path, force: bool = False) -> List[Dict[str, object]]:
    reports = []
    for entry in list_skills():
        try:
            reports.append(install_skill(entry["name"], destination, force=force))
        except FileExistsError as exc:
            reports.append({"name": entry["name"], "status": "exists", "message": str(exc)})
    return reports
