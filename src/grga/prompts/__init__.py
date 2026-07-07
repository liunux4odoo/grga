"""Prompt template loader for :mod:`grga.suggest`.

Templates live alongside this module as ``suggest_<intent>.txt`` files. We
keep them as plain text (rather than importable strings) so operators can
tweak them without recompiling and diff them cleanly.

``importlib.resources`` is used so templates keep working when grga is
installed from a wheel where the package directory is inside a zip.
"""
from __future__ import annotations

from functools import lru_cache
from importlib.resources import files

_INTENTS = ("expand", "rerank", "reformulate")


def available_intents() -> tuple[str, ...]:
    """Return the intent names the loader knows about."""
    return _INTENTS


@lru_cache(maxsize=None)
def load_prompt(intent: str) -> str:
    """Return the system-prompt text for ``intent``.

    Raises:
        ValueError: if ``intent`` is not one of :func:`available_intents`.
    """
    if intent not in _INTENTS:
        raise ValueError(f"unknown suggest intent: {intent!r} (expected {_INTENTS})")
    return files(__package__).joinpath(f"suggest_{intent}.txt").read_text(encoding="utf-8")
