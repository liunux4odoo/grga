"""Sidecar cache for structure documents (Phase 2.3).

Structure indexes are stored under a `.grga/structure/` directory
(configurable via ``GRGA_SIDECAR_ROOT``) with hashed filenames so long paths
(and Windows path-length limits) are avoided. Each sidecar records the
source mtime; a stale mtime triggers a lazy rebuild.

The public entry point is :func:`load_or_build`. All exceptions are caught
and turned into empty StructureDoc instances, matching the design's degrade
philosophy (§4.1).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional, Tuple

from grga.structure.md import StructureDoc, parse_markdown


def default_sidecar_root() -> Path:
    """Resolve the sidecar root, honouring ``GRGA_SIDECAR_ROOT`` when set."""
    override = os.environ.get("GRGA_SIDECAR_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.cwd() / ".grga" / "structure"


def _hash_key(source: Path) -> str:
    resolved = str(source.resolve())
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:16]


def sidecar_path_for(source: Path, root: Optional[Path] = None) -> Path:
    root = Path(root) if root else default_sidecar_root()
    key = _hash_key(source)
    stem = source.stem[:32] if source.stem else "doc"
    return root / f"{stem}.{key}.json"


def _read_sidecar(sidecar: Path) -> Optional[StructureDoc]:
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return StructureDoc.from_dict(payload)
    except Exception:
        return None


def _write_sidecar(sidecar: Path, doc: StructureDoc) -> None:
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
    tmp.write_text(json.dumps(doc.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, sidecar)


def load_or_build(source: str | Path, root: Optional[Path] = None) -> Tuple[StructureDoc, bool]:
    """Load a cached StructureDoc or (re)build it if stale.

    Returns ``(doc, from_cache)``. ``from_cache`` is ``True`` when the on-disk
    sidecar was reused, ``False`` when the parser ran.
    """
    src = Path(source)
    try:
        source_mtime = src.stat().st_mtime
    except OSError:
        return parse_markdown(src), False

    sidecar = sidecar_path_for(src, root=root)
    cached = _read_sidecar(sidecar) if sidecar.exists() else None
    if cached is not None and abs(cached.mtime - source_mtime) < 1e-6 and cached.error is None:
        return cached, True

    doc = parse_markdown(src)
    if doc.error is None:
        try:
            _write_sidecar(sidecar, doc)
        except OSError:
            pass
    return doc, False
