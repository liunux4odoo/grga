from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def docs_fixture(fixtures_dir: Path) -> Path:
    return fixtures_dir / "docs"


@pytest.fixture(scope="session")
def md_eval_fixture(fixtures_dir: Path) -> Path:
    return fixtures_dir / "md_eval"


@pytest.fixture
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("GRGA_"):
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture(autouse=True)
def isolate_grga_config(monkeypatch, tmp_path):
    """Keep tests hermetic: never read the developer's real ~/.config/grga.

    The user config may enable ``debug: true``, which would mix debug log
    lines into CLI stdout and break JSON parsing in tests.
    """
    fake_config = tmp_path / "grga_config" / "settings.json"
    monkeypatch.setattr("grga.cli.CONFIG_PATH", fake_config, raising=False)

