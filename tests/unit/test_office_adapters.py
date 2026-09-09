"""Unit tests for the pandoc_office custom adapter wiring.

Design: docs/design/plan-2026-09-09-office-adapters.md
"""
from __future__ import annotations

import json
from inspect import signature
from pathlib import Path

from typer.testing import CliRunner

from grga.cli import app
from grga.search import _compile_search_query, _shared_rga_options
from grga.toolchain import (
    RGA_OFFICE_ADAPTER_NAME,
    _strip_jsonc_comments,
    ensure_rga_office_config,
    rga_config_path,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# _strip_jsonc_comments
# ---------------------------------------------------------------------------


def test_strip_jsonc_comments_plain_json_unchanged():
    text = '{"a": 1, "b": [true, null]}'
    assert _strip_jsonc_comments(text) == text


def test_strip_jsonc_line_comments():
    text = '{\n  // header\n  "a": 1 // trailing\n}'
    stripped = _strip_jsonc_comments(text)
    assert "//" not in stripped
    assert json.loads(stripped) == {"a": 1}


def test_strip_jsonc_block_comments():
    text = '{\n  /* block\n     across lines */\n  "a": 1\n}'
    stripped = _strip_jsonc_comments(text)
    assert "/*" not in stripped and "*/" not in stripped
    assert json.loads(stripped) == {"a": 1}


def test_strip_jsonc_preserves_comment_in_string():
    text = '{"url": "http://example.com/path", "note": "// not a comment"}'
    stripped = _strip_jsonc_comments(text)
    assert "http://example.com/path" in stripped
    assert json.loads(stripped)["note"] == "// not a comment"


def test_strip_jsonc_preserves_escaped_quotes_in_string():
    text = '{"msg": "he said \\"// x\\"", "n": 1}'
    stripped = _strip_jsonc_comments(text)
    assert json.loads(stripped) == {"msg": 'he said "// x"', "n": 1}


def test_strip_jsonc_mixed_strings_and_comments():
    text = (
        "{\n"
        "  // doc\n"
        '  "a": "x/*y*/", /* real */\n'
        '  "b": "z//w",\n'
        "  \"c\": 3\n"
        "}"
    )
    stripped = _strip_jsonc_comments(text)
    assert json.loads(stripped) == {"a": "x/*y*/", "b": "z//w", "c": 3}


def test_strip_jsonc_respects_single_quoted_strings():
    # The stripper honors single-quoted strings too (JSONC dialects); the
    # comment markers inside them must survive even though this isn't strict
    # JSON (so no json.loads here).
    text = "x = '// keep' /* strip */ y = 'z//w'"
    stripped = _strip_jsonc_comments(text)
    assert "// keep" in stripped
    assert "z//w" in stripped
    assert "/* strip */" not in stripped


def test_strip_jsonc_empty_input():
    assert _strip_jsonc_comments("") == ""
    assert _strip_jsonc_comments("// only a comment\n") == "\n"


# ---------------------------------------------------------------------------
# rga_config_path
# ---------------------------------------------------------------------------


def test_rga_config_path_linux_xdg(monkeypatch, tmp_path: Path):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr("grga.toolchain.sys.platform", "linux")
    assert rga_config_path() == xdg / "ripgrep-all" / "config.jsonc"


def test_rga_config_path_linux_fallback(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr("grga.toolchain.sys.platform", "linux")
    monkeypatch.setattr("grga.toolchain.os.name", "posix")
    monkeypatch.setattr(
        "grga.toolchain.Path.home",
        staticmethod(lambda: tmp_path / "home"),
    )
    assert rga_config_path() == tmp_path / "home" / ".config" / "ripgrep-all" / "config.jsonc"


# ---------------------------------------------------------------------------
# ensure_rga_office_config
# ---------------------------------------------------------------------------


def test_ensure_rga_office_config_writes_new_file(monkeypatch, tmp_path: Path):
    cfg = tmp_path / "config.jsonc"
    monkeypatch.setattr("grga.toolchain.rga_config_path", lambda: cfg)
    ensure_rga_office_config()
    assert cfg.exists()
    parsed = json.loads(_strip_jsonc_comments(cfg.read_text(encoding="utf-8")))
    names = [a["name"] for a in parsed["custom_adapters"]]
    assert RGA_OFFICE_ADAPTER_NAME in names


def test_ensure_rga_office_config_merges_existing_keys(monkeypatch, tmp_path: Path):
    cfg = tmp_path / "config.jsonc"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('{\n  // user settings\n  "accurate": false,\n  "cache": true\n}\n', encoding="utf-8")
    monkeypatch.setattr("grga.toolchain.rga_config_path", lambda: cfg)
    ensure_rga_office_config()
    parsed = json.loads(_strip_jsonc_comments(cfg.read_text(encoding="utf-8")))
    assert parsed["accurate"] is False
    assert parsed["cache"] is True
    names = [a["name"] for a in parsed["custom_adapters"]]
    assert RGA_OFFICE_ADAPTER_NAME in names


def test_ensure_rga_office_config_is_idempotent(monkeypatch, tmp_path: Path):
    cfg = tmp_path / "config.jsonc"
    monkeypatch.setattr("grga.toolchain.rga_config_path", lambda: cfg)
    ensure_rga_office_config()
    first = cfg.read_text(encoding="utf-8")
    ensure_rga_office_config()
    ensure_rga_office_config()
    assert cfg.read_text(encoding="utf-8") == first
    parsed = json.loads(_strip_jsonc_comments(first))
    assert len(parsed["custom_adapters"]) == 1


def test_ensure_rga_office_config_keeps_existing_adapters(monkeypatch, tmp_path: Path):
    cfg = tmp_path / "config.jsonc"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        '{\n  "custom_adapters": [\n    {"name": "user_adapter", "binary": "x"}\n  ]\n}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("grga.toolchain.rga_config_path", lambda: cfg)
    ensure_rga_office_config()
    parsed = json.loads(_strip_jsonc_comments(cfg.read_text(encoding="utf-8")))
    names = [a["name"] for a in parsed["custom_adapters"]]
    assert "user_adapter" in names
    assert RGA_OFFICE_ADAPTER_NAME in names


def test_ensure_rga_office_config_skips_broken_config(monkeypatch, tmp_path: Path):
    cfg = tmp_path / "config.jsonc"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("{ this is not valid jsonc at all", encoding="utf-8")
    monkeypatch.setattr("grga.toolchain.rga_config_path", lambda: cfg)
    ensure_rga_office_config()  # must not raise
    assert cfg.read_text(encoding="utf-8") == "{ this is not valid jsonc at all"


def test_ensure_rga_office_config_missing_adapter_gets_added(monkeypatch, tmp_path: Path):
    cfg = tmp_path / "config.jsonc"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('{"custom_adapters": [{"name": "other", "binary": "y"}]}\n', encoding="utf-8")
    monkeypatch.setattr("grga.toolchain.rga_config_path", lambda: cfg)
    ensure_rga_office_config()
    parsed = json.loads(_strip_jsonc_comments(cfg.read_text(encoding="utf-8")))
    assert len(parsed["custom_adapters"]) == 2


# ---------------------------------------------------------------------------
# _shared_rga_options office_adapters toggle
# ---------------------------------------------------------------------------


def test_shared_rga_options_default_has_no_adapter_flag():
    query = _compile_search_query("foo", "auto")
    assert "--rga-adapters=-pandoc_office" not in _shared_rga_options(query)


def test_shared_rga_options_office_adapters_false_excludes_adapter():
    query = _compile_search_query("foo", "auto")
    options = _shared_rga_options(query, office_adapters=False)
    assert "--rga-adapters=-pandoc_office" in options


def test_shared_rga_options_office_adapters_signature_default_true():
    params = signature(_shared_rga_options).parameters
    assert params["office_adapters"].default is True


# ---------------------------------------------------------------------------
# CLI --no-office-adapters flag
# ---------------------------------------------------------------------------


def test_cli_help_exposes_no_office_adapters_on_search_and_probe():
    search_help = runner.invoke(app, ["search", "--help"]).output
    assert "--no-office-adapters" in search_help
    probe_help = runner.invoke(app, ["probe", "--help"]).output
    assert "--no-office-adapters" in probe_help
    preview_help = runner.invoke(app, ["preview", "--help"]).output
    assert "--no-office-adapters" in preview_help


def test_cli_preview_flag_toggles_office_adapters():
    with_default = runner.invoke(app, ["preview", "hello"])
    assert "--rga-adapters=-pandoc_office" not in with_default.output

    without = runner.invoke(app, ["preview", "--no-office-adapters", "hello"])
    assert "--rga-adapters=-pandoc_office" in without.output


# ---------------------------------------------------------------------------
# toolchain fingerprint (build-time warning source)
# ---------------------------------------------------------------------------


def test_toolchain_fingerprint_stable_across_calls():
    # A no-op incremental build never reads files, so PATH stays unstamped;
    # the fingerprint must still resolve the same managed tool versions as
    # the data-bearing build before it (regression: spurious "toolchain
    # fingerprint changed" warning on every no-op build).
    from grga.graph.build import _compute_toolchain_fingerprint

    a = _compute_toolchain_fingerprint()
    b = _compute_toolchain_fingerprint()
    assert a == b

