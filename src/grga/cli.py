import json
import os
import sys
from pathlib import Path
from typing import List, Optional

import typer

from grga import __version__
from grga.deep_search import deep_search_directory, resolve_deep_search_options
from grga.exit_codes import (
    EXIT_DOWNLOAD_FAIL,
    EXIT_IO,
    EXIT_MISSING_TOOL,
    EXIT_OFFLINE,
    EXIT_PARTIAL_INSTALL,
    EXIT_USER_ABORT,
    EXIT_VERIFY_FAIL,
    emit_error,
    emit_warning,
)
from grga.probe import probe_directory
from grga.runtime_env import is_interactive, is_offline
from grga.search import _to_build_globs, build_rga_command_preview, check_environment, configure_logging, search_directory
from grga.toolchain import (
    MANUAL_INSTALL_URLS,
    MissingToolsError,
    OfflineError,
    PartialInstallError,
    UserAbortError,
    VerifyError,
    check_tools,
    current_tools_dir,
    ensure_rga_office_config,
    ensure_tools,
    install_tools,
    install_tools_from_dir,
    migrate_legacy_tools,
    platform_key,
    supported_tool_names,
    tool_specs,
    tools_root,
    update_tools,
)

app = typer.Typer(no_args_is_help=True, help="Search files with ripgrep-all helpers.")
tools_app = typer.Typer(no_args_is_help=True, help="Manage bundled command line tools.")
app.add_typer(tools_app, name="tools")
skill_app = typer.Typer(no_args_is_help=True, help="Install and inspect bundled grga skills.")
app.add_typer(skill_app, name="skill")
index_app = typer.Typer(no_args_is_help=True, help="Build and manage the L2 graph index.")
app.add_typer(index_app, name="index")
graph_app = typer.Typer(no_args_is_help=True, help="Inspect the shared user-scoped graph.")
app.add_typer(graph_app, name="graph")
scope_app = typer.Typer(no_args_is_help=True, help="Manage logical scope names + local directory bindings (v10).")
app.add_typer(scope_app, name="scope")
scope_tag_app = typer.Typer(no_args_is_help=True, help="Manage multi-value scope tags (v11).")
scope_app.add_typer(scope_tag_app, name="tag")
mcp_app = typer.Typer(no_args_is_help=True, help="Start an MCP (Model Context Protocol) server.")
app.add_typer(mcp_app, name="mcp")

CONFIG_PATH = Path.home() / ".config" / "grga" / "settings.json"


def _version_callback(value: bool):
    if value:
        typer.echo(f"grga {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
):
    """Search files with ripgrep-all helpers."""


def _apply_prefer_system(flag: bool) -> None:
    """Inject GRGA_PREFER_SYSTEM so downstream find_tool prefers system tools.

    Env injection keeps the change local to the process and avoids
    threading the flag through every resolver call site.
    """
    if flag:
        os.environ["GRGA_PREFER_SYSTEM"] = "1"


def _guard_tools(required, *, json_mode, offline, interactive=None):
    """Run ensure_tools and translate failures to stable exit codes."""
    interactive = is_interactive() if interactive is None else interactive
    try:
        ensure_tools(
            required,
            interactive=interactive,
            json_mode=json_mode,
            offline=offline,
            progress=(None if json_mode else (lambda message: typer.echo(message, err=True))),
        )
        if "rga" in required:
            # Register the pandoc_office custom adapter in rga's user config
            # (idempotent; needed so xlsx/pptx/doc/xls/ppt are searchable).
            ensure_rga_office_config()
    except OfflineError as exc:
        raise typer.Exit(
            emit_error(
                EXIT_OFFLINE,
                "offline_and_missing",
                str(exc),
                json_mode=json_mode,
                details={"missing": list(exc.missing), "manual_urls": {name: MANUAL_INSTALL_URLS.get(name) for name in exc.missing}},
            )
        )
    except UserAbortError as exc:
        raise typer.Exit(emit_error(EXIT_USER_ABORT, "user_abort", str(exc), json_mode=json_mode, details={"missing": list(exc.missing)}))
    except PartialInstallError as exc:
        raise typer.Exit(emit_error(EXIT_PARTIAL_INSTALL, "partial_install", str(exc), json_mode=json_mode, details={"installed": list(exc.installed), "failed": list(exc.failed)}))
    except MissingToolsError as exc:
        raise typer.Exit(
            emit_error(
                EXIT_MISSING_TOOL,
                "missing_tools",
                str(exc),
                json_mode=json_mode,
                details={"missing": list(exc.missing), "install_hint": "grga tools install"},
            )
        )



def _get_config_debug() -> bool:
    """Read debug setting from config file."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        return bool(config.get("debug", False))
    except (OSError, ValueError, TypeError):
        return False


def _resolve_debug(debug: bool, json_output: bool) -> bool:
    """Resolve the effective debug flag.

    In ``--json`` mode debug output is forced off unless it was explicitly
    requested via the ``--debug`` command-line flag, so that stdout carries
    JSON content only and the config-file ``debug`` setting is ignored.
    """
    if debug:
        return True
    if json_output:
        return False
    return _get_config_debug()


def _join_keywords(keywords):
    return " ".join(item for item in keywords if item.strip()).strip()


def _validate_pattern_mode(pattern_mode):
    if pattern_mode not in ("auto", "regex", "glob"):
        typer.echo("pattern mode must be one of: auto, regex, glob", err=True)
        raise typer.Exit(2)
    return pattern_mode


def _format_metadata(item):
    parts = []
    idx = item.get("occurrence_index")
    total = item.get("total_occurrences")
    if isinstance(idx, int) and isinstance(total, int) and total > 0:
        parts.append(f"[{idx}/{total}]")
    ratio = item.get("line_ratio")
    if isinstance(ratio, (int, float)) and ratio > 0:
        parts.append(f"ratio={ratio:.2f}")
    nearby = item.get("nearby_hits")
    if isinstance(nearby, int) and nearby > 0:
        parts.append(f"nearby={nearby}")
    heading_path = item.get("heading_path")
    if isinstance(heading_path, list) and heading_path:
        parts.append("heading=" + ">".join(heading_path))
    if not parts:
        return ""
    return " ".join(parts) + " "


def _print_result(result):
    if not result.get("ok"):
        typer.echo(result.get("error", "search failed"), err=True)
        raise typer.Exit(1)

    if result.get("warning"):
        typer.echo(f"warning: {result['warning']}", err=True)
    skipped = result.get("skipped_files") or []
    if skipped:
        typer.echo(f"skipped: {len(skipped)} records", err=True)

    answer = result.get("answer")
    if isinstance(answer, dict) and answer.get("text"):
        typer.echo(answer["text"])
        typer.echo("")

    matches = result.get("matches", [])
    for item in matches:
        path = item.get("path", "")
        line_number = item.get("line_number")
        text = item.get("text", "")
        separator = "-" if item.get("is_context") else ":"
        meta = ""
        if not item.get("is_context") and item.get("total_occurrences"):
            meta = _format_metadata(item)
        if isinstance(line_number, int):
            typer.echo(f"{path}{separator}{line_number}{separator}{meta}{text}")
        else:
            typer.echo(f"{path}{separator}{meta}{text}")

    match_count = sum(1 for item in matches if not item.get("is_context"))
    file_count = len({item.get("path", "") for item in matches})
    typer.echo(f"{match_count} matches in {file_count} files", err=True)


@app.command("search")
def search_command(
    keywords: List[str] = typer.Argument(..., help="Search keywords or query expression."),
    directory: Path = typer.Option(Path("."), "--directory", "-d", exists=True, file_okay=False, dir_okay=True, resolve_path=True, help="Directory to search."),
    pattern_mode: str = typer.Option("auto", "--mode", "-m", help="Pattern mode: auto, regex, or glob."),
    case_sensitive: bool = typer.Option(False, "--case-sensitive/--ignore-case", help="Enable or disable case-sensitive search."),
    max_count: Optional[int] = typer.Option(None, "--max-count", "-n", help="Maximum matches per file (rga --max-count)."),
    max_total: Optional[int] = typer.Option(100, "--max-total", "-N", help="Global cap on total real matches; results are truncated when exceeded (default 100, set 0 to disable)."),
    strict_cap: bool = typer.Option(False, "--strict-cap", help="Fail with too_many_matches when --max-total is exceeded instead of truncating."),
    timeout: float = typer.Option(600.0, "--timeout", "-t", help="Search timeout in seconds."),
    before_context: int = typer.Option(3, "--before-context", "-B", help="Lines of leading context."),
    after_context: int = typer.Option(3, "--after-context", "-A", help="Lines of trailing context."),
    multiline: bool = typer.Option(False, "--multiline", help="Enable multiline search."),
    file_globs: Optional[List[str]] = typer.Option(None, "--iglob", "-g", help="Case-insensitive file glob. Can be used multiple times."),
    path_only: bool = typer.Option(False, "--path-only", help="Search relative file paths only."),
    no_default_exclude: bool = typer.Option(False, "--no-default-exclude", help="Do not apply the default archive/binary exclusion globs (user -g globs still apply)."),
    no_office_adapters: bool = typer.Option(False, "--no-office-adapters", help="Do not use the pandoc_office custom adapter (xlsx/pptx/doc/xls/ppt not searchable)."),
    implicit_and: bool = typer.Option(False, "--implicit-and", help="Force implicit adjacency to AND (default). Suppresses the notice."),
    implicit_or: bool = typer.Option(False, "--implicit-or", help="Restore v1 behaviour: implicit adjacency means OR."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
    metadata: bool = typer.Option(True, "--metadata/--no-metadata", help="Attach Layer 2 hit metadata (occurrence, ratio, nearby)."),
    offline: bool = typer.Option(False, "--offline", help="Fail fast instead of downloading missing tools."),
    prefer_system: bool = typer.Option(False, "--prefer-system", help="Prefer system-installed tools over the managed cache."),
    debug: bool = typer.Option(False, "--debug", help="Print debug information during search."),
):
    """Structured ripgrep-all search with Layer-2 hit metadata.

    Wraps `rga` and returns typed hits (path, line, text) plus optional
    occurrence / line_ratio / heading_path annotations. Use `--json` for
    machine-readable output; combine `--implicit-and/--implicit-or` to pin
    how adjacent keywords are joined.

    Each match carries a ``path`` field (the relative file path). Full
    JSON schema and sample output in ``docs/api.md``.

    Minimal ``--json`` match shape::

        {"path": "docs/foo.md", "line_number": 42, "text": "...",
         "is_context": false, "occurrence_index": 1, "total_occurrences": 3}
    """
    query = _join_keywords(keywords)
    if not query:
        typer.echo("keywords cannot be empty", err=True)
        raise typer.Exit(2)

    if implicit_and and implicit_or:
        typer.echo("--implicit-and and --implicit-or are mutually exclusive", err=True)
        raise typer.Exit(2)
    implicit_operator = "or" if implicit_or else "and"
    implicit_explicit = implicit_and or implicit_or
    effective_max_total = max_total if (max_total is None or max_total > 0) else None

    effective_debug = _resolve_debug(debug, json_output)
    configure_logging(debug=effective_debug)

    _apply_prefer_system(prefer_system)
    _guard_tools(["rga"], json_mode=json_output, offline=is_offline(offline))


    try:
        raw = search_directory(
            str(directory),
            query,
            pattern_mode=_validate_pattern_mode(pattern_mode),
            case_sensitive=case_sensitive,
            max_count=max_count,
            timeout=timeout,
            before_context=before_context,
            after_context=after_context,
            multiline=multiline,
            glob_patterns=file_globs,
            path_only=path_only,
            debug=effective_debug,
            max_total=effective_max_total,
            strict_cap=strict_cap,
            implicit_operator=implicit_operator,
            default_exclude=not no_default_exclude,
            office_adapters=not no_office_adapters,
        )
        result = json.loads(raw)
    except Exception as exc:
        if json_output:
            typer.echo(json.dumps({"ok": False, "error": str(exc), "matches": []}, ensure_ascii=False, indent=2))
        else:
            typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    notice = result.get("implicit_notice")
    if notice and not implicit_explicit:
        typer.echo(f"[grga] {notice}", err=True)

    if metadata:
        from grga.meta import annotate_result_inplace, annotate_structure_inplace

        annotate_result_inplace(result, base_dir=str(directory))
        annotate_structure_inplace(result, base_dir=str(directory))

    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        raise typer.Exit(0 if result.get("ok") else 1)

    _print_result(result)
    if result.get("truncated"):
        typer.echo(f"truncated: hit cap of --max-total={effective_max_total}", err=True)


@app.command("deep-search")
def deep_search_command(
    keywords: List[str] = typer.Argument(..., help="Natural language query for LLM-assisted deep search."),
    scope: str = typer.Option(..., "--scope", "-s", help="Logical scope name (v10). Required — v10 does not auto-derive a scope from the directory. Use the same name you passed to `grga index build`."),
    directory: Path = typer.Option(Path("."), "--directory", "-d", exists=True, file_okay=False, dir_okay=True, resolve_path=True, help="Directory to search on this machine (physical corpus location)."),
    pattern_mode: Optional[str] = typer.Option(None, "--mode", "-m", help="Pattern mode: auto, regex, or glob."),
    case_sensitive: Optional[bool] = typer.Option(None, "--case-sensitive/--ignore-case", help="Enable or disable case-sensitive search."),
    max_count: Optional[int] = typer.Option(None, "--max-count", "-n", help="Maximum matches per search iteration."),
    timeout: Optional[float] = typer.Option(None, "--timeout", "-t", help="Search timeout in seconds."),
    before_context: Optional[int] = typer.Option(None, "--before-context", "-B", help="Lines of leading context."),
    after_context: Optional[int] = typer.Option(None, "--after-context", "-A", help="Lines of trailing context."),
    multiline: Optional[bool] = typer.Option(None, "--multiline/--no-multiline", help="Enable multiline search."),
    file_globs: Optional[List[str]] = typer.Option(None, "--iglob", "-g", help="Case-insensitive file glob. Can be used multiple times."),
    path_only: Optional[bool] = typer.Option(None, "--path-only/--content-search", help="Search relative file paths only."),
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url", help="OpenAI-compatible API base URL."),
    llm_api_key: Optional[str] = typer.Option(None, "--llm-api-key", help="OpenAI-compatible API key."),
    llm_model: Optional[str] = typer.Option(None, "--llm-model", help="OpenAI-compatible model name."),
    max_related_keywords: Optional[int] = typer.Option(None, "--max-related-keywords", help="Maximum related keywords selected for follow-up iterations."),
    repeat_count: Optional[int] = typer.Option(None, "--repeat-count", help="Number of follow-up search iterations."),
    max_extract_segments: Optional[int] = typer.Option(None, "--max-extract-segments", help="Maximum matched segments sent to LLM per iteration."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite keyword library path."),
    decay_factor: Optional[float] = typer.Option(None, "--decay-factor", help="Decay multiplier applied to keyword counts before each run (0-1)."),
    min_keyword_count: Optional[float] = typer.Option(None, "--min-keyword-count", help="Drop keyword records whose count falls below this threshold."),
    global_library: Optional[bool] = typer.Option(None, "--global-library/--directory-library", help="Use the whole keyword library or only the current directory scope."),
    enable_tiered_search: Optional[bool] = typer.Option(None, "--tiered-search/--no-tiered-search", help="Run queries from strict to loose and stop once enough results are found."),
    min_results: Optional[int] = typer.Option(None, "--min-results", help="Minimum unique matches before tiered search stops relaxing."),
    max_tiers: Optional[int] = typer.Option(None, "--max-tiers", help="Maximum number of tiered query expressions to run."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
    metadata: bool = typer.Option(True, "--metadata/--no-metadata", help="Attach Layer 2 hit metadata (occurrence, ratio, nearby)."),
    offline: bool = typer.Option(False, "--offline", help="Fail fast instead of downloading missing tools."),
    prefer_system: bool = typer.Option(False, "--prefer-system", help="Prefer system-installed tools over the managed cache."),
    debug: bool = typer.Option(False, "--debug", help="Print debug information during search."),
):
    """LLM-assisted iterative search with keyword library refeed.

    Runs `rga` in tiered rounds, using an OpenAI-compatible LLM to widen
    or refine queries between rounds. Records keyword usage into the
    shared v1 keyword library so subsequent runs bias toward proven
    keywords. Every LLM-related knob supports overriding the value from
    `~/.config/grga/settings.json`.
    """
    query = _join_keywords(keywords)
    if not query:
        typer.echo("query cannot be empty", err=True)
        raise typer.Exit(2)

    effective_debug = _resolve_debug(debug, json_output)
    configure_logging(debug=effective_debug)

    _apply_prefer_system(prefer_system)
    _guard_tools(["rga"], json_mode=json_output, offline=is_offline(offline))


    options = resolve_deep_search_options(
        {
            "llm_base_url": llm_base_url,
            "llm_api_key": llm_api_key,
            "llm_model_name": llm_model,
            "pattern_mode": pattern_mode,
            "case_sensitive": case_sensitive,
            "max_count": max_count,
            "timeout": timeout,
            "before_context": before_context,
            "after_context": after_context,
            "multiline": multiline,
            "path_only": path_only,
            "max_related_keywords": max_related_keywords,
            "repeat_count": repeat_count,
            "max_extract_segments": max_extract_segments,
            "db_path": str(db_path) if db_path else None,
            "decay_factor": decay_factor,
            "min_keyword_count": min_keyword_count,
            "global_library": global_library,
            "enable_tiered_search": enable_tiered_search,
            "min_results": min_results,
            "max_tiers": max_tiers,
        }
    )

    raw = deep_search_directory(
        str(directory),
        query,
        scope=scope,
        llm_base_url=options["llm_base_url"],
        llm_api_key=options["llm_api_key"],
        llm_model_name=options["llm_model_name"],
        pattern_mode=_validate_pattern_mode(options["pattern_mode"]),
        case_sensitive=options["case_sensitive"],
        max_count=options["max_count"],
        timeout=options["timeout"],
        before_context=options["before_context"],
        after_context=options["after_context"],
        multiline=options["multiline"],
        glob_patterns=file_globs,
        path_only=options["path_only"],
        max_related_keywords=options["max_related_keywords"],
        repeat_count=options["repeat_count"],
        max_extract_segments=options["max_extract_segments"],
        db_path=options["db_path"],
        decay_factor=options["decay_factor"],
        min_keyword_count=options["min_keyword_count"],
        global_library=options["global_library"],
        enable_tiered_search=options["enable_tiered_search"],
        min_results=options["min_results"],
        max_tiers=options["max_tiers"],
        debug=effective_debug,
    )
    result = json.loads(raw)
    if metadata:
        from grga.meta import annotate_result_inplace, annotate_structure_inplace

        annotate_result_inplace(result, base_dir=str(directory))
        annotate_structure_inplace(result, base_dir=str(directory))
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        raise typer.Exit(0 if result.get("ok") else 1)
    _print_result(result)



@app.command("preview")
def preview_command(
    keywords: List[str] = typer.Argument(..., help="Search keywords or query expression."),
    pattern_mode: str = typer.Option("auto", "--mode", "-m", help="Pattern mode: auto, regex, or glob."),
    case_sensitive: bool = typer.Option(False, "--case-sensitive/--ignore-case", help="Enable or disable case-sensitive search."),
    max_count: Optional[int] = typer.Option(None, "--max-count", "-n", help="Maximum number of matches."),
    before_context: int = typer.Option(3, "--before-context", "-B", help="Lines of leading context."),
    after_context: int = typer.Option(3, "--after-context", "-A", help="Lines of trailing context."),
    multiline: bool = typer.Option(False, "--multiline", help="Enable multiline search."),
    file_globs: Optional[List[str]] = typer.Option(None, "--iglob", "-g", help="Case-insensitive file glob. Can be used multiple times."),
    path_only: bool = typer.Option(False, "--path-only", help="Preview path-only search command."),
    no_default_exclude: bool = typer.Option(False, "--no-default-exclude", help="Preview the command without the default archive/binary exclusion globs."),
    no_office_adapters: bool = typer.Option(False, "--no-office-adapters", help="Preview the command without the pandoc_office custom adapter."),
):
    """Print the exact `rga` command that `grga search` would run.

    Read-only, no filesystem walk, no tool download. Useful for pasting
    into a shell to inspect ripgrep-all behaviour outside of grga's JSON
    envelope, or for building integrations that shell out directly.
    """
    query = _join_keywords(keywords)
    if not query:
        typer.echo("keywords cannot be empty", err=True)
        raise typer.Exit(2)
    command = build_rga_command_preview(
        query,
        pattern_mode=_validate_pattern_mode(pattern_mode),
        case_sensitive=case_sensitive,
        max_count=max_count,
        before_context=before_context,
        after_context=after_context,
        multiline=multiline,
        glob_patterns=file_globs,
        path_only=path_only,
        default_exclude=not no_default_exclude,
        office_adapters=not no_office_adapters,
    )
    typer.echo(command)


@app.command("probe")
def probe_command(
    keywords: List[str] = typer.Argument(..., help="Search keywords or query expression."),
    directory: Path = typer.Option(Path("."), "--directory", "-d", exists=True, file_okay=False, dir_okay=True, resolve_path=True, help="Directory to probe."),
    pattern_mode: str = typer.Option("auto", "--mode", "-m", help="Pattern mode: auto, regex, or glob."),
    case_sensitive: bool = typer.Option(False, "--case-sensitive/--ignore-case", help="Enable or disable case-sensitive search."),
    timeout: float = typer.Option(180.0, "--timeout", "-t", help="Probe timeout in seconds."),
    multiline: bool = typer.Option(False, "--multiline", help="Enable multiline search."),
    file_globs: Optional[List[str]] = typer.Option(None, "--iglob", "-g", help="Case-insensitive file glob. Can be used multiple times."),
    path_only: bool = typer.Option(False, "--path-only", help="Probe relative file paths only."),
    no_default_exclude: bool = typer.Option(False, "--no-default-exclude", help="Do not apply the default archive/binary exclusion globs (user -g globs still apply)."),
    no_office_adapters: bool = typer.Option(False, "--no-office-adapters", help="Do not use the pandoc_office custom adapter (xlsx/pptx/doc/xls/ppt not searchable)."),
    implicit_and: bool = typer.Option(False, "--implicit-and", help="Force implicit adjacency to AND (default)."),
    implicit_or: bool = typer.Option(False, "--implicit-or", help="Restore v1 behaviour: implicit adjacency means OR."),
    sample: int = typer.Option(0, "--sample", help="Sample up to N matched lines to estimate average line bytes (0 disables)."),
    per_file: bool = typer.Option(False, "--per-file/--no-per-file", help="Include per-file match counts in the JSON output."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
    offline: bool = typer.Option(False, "--offline", help="Fail fast instead of downloading missing tools."),
    prefer_system: bool = typer.Option(False, "--prefer-system", help="Prefer system-installed tools over the managed cache."),
    debug: bool = typer.Option(False, "--debug", help="Print debug information during probe."),
):
    """Cheap, read-only probe of query scale before running a full search.

    Runs ``rga --count-matches`` (or ``rg --files`` for --path-only) and an
    optional sampling pass to estimate matched-text size. Never fetches
    bodies or context, never rewrites the query. Intended to be composed by
    a caller (human or agent) that decides whether to tighten or loosen the
    query before invoking ``grga search``.
    """
    query = _join_keywords(keywords)
    if not query:
        typer.echo("keywords cannot be empty", err=True)
        raise typer.Exit(2)

    if implicit_and and implicit_or:
        typer.echo("--implicit-and and --implicit-or are mutually exclusive", err=True)
        raise typer.Exit(2)
    implicit_operator = "or" if implicit_or else "and"

    effective_debug = _resolve_debug(debug, json_output)
    configure_logging(debug=effective_debug)

    _apply_prefer_system(prefer_system)
    _guard_tools(["rg" if path_only else "rga"], json_mode=json_output, offline=is_offline(offline))


    try:
        raw = probe_directory(
            str(directory),
            query,
            pattern_mode=_validate_pattern_mode(pattern_mode),
            case_sensitive=case_sensitive,
            timeout=timeout,
            multiline=multiline,
            glob_patterns=file_globs,
            path_only=path_only,
            sample=max(0, sample),
            per_file=per_file,
            debug=effective_debug,
            implicit_operator=implicit_operator,
            default_exclude=not no_default_exclude,
            office_adapters=not no_office_adapters,
        )
        result = json.loads(raw)
    except Exception as exc:
        if json_output:
            typer.echo(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        raise typer.Exit(0 if result.get("ok") else 1)

    if not result.get("ok"):
        typer.echo(result.get("error", "probe failed"), err=True)
        raise typer.Exit(1)

    if result.get("warning"):
        typer.echo(f"warning: {result['warning']}", err=True)
    skipped = result.get("skipped_files") or []
    if skipped:
        typer.echo(f"skipped: {len(skipped)} records", err=True)

    total_lines = result.get("total_lines")
    file_count = result.get("file_count")
    typer.echo(f"total_lines: {total_lines}")
    if file_count is not None:
        typer.echo(f"file_count:  {file_count}")

    sample_info = result.get("sample")
    if sample_info:
        typer.echo(
            f"sample:      {sample_info['sampled_lines']} lines, "
            f"avg {sample_info['avg_line_bytes']} B/line, "
            f"est ~{sample_info['est_total_bytes']} B total"
        )

    per_file_list = result.get("per_file") or []
    for item in per_file_list:
        typer.echo(f"  {item['matches']:>6}  {item['path']}")

    neighbors = result.get("neighbors")
    if neighbors:
        parts = [f"{n['kw']}({round(float(n['npmi']), 2)})" for n in neighbors]
        typer.echo(f"neighbors:   {', '.join(parts)}")

    notice = result.get("implicit_notice")
    if notice:
        typer.echo(f"[grga] {notice}", err=True)


@app.command("check")
def check_command(json_output: bool = typer.Option(False, "--json", help="Print raw JSON result.")):
    """Report resolved paths, versions and warnings for required tools.

    Exits non-zero when a required tool (rga, rg, ...) is missing or
    unusable. Complements `grga tools check` by focusing on the search
    stack rather than every managed binary.
    """
    result = check_environment()
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        raise typer.Exit(0 if result.get("ok") else 1)

    for name, path in result.get("paths", {}).items():
        typer.echo(f"{name}: {path or 'not found'}")
    for warning in result.get("warnings", []):
        typer.echo(f"warning: {warning}", err=True)
    for error in result.get("errors", []):
        typer.echo(f"error: {error}", err=True)
    raise typer.Exit(0 if result.get("ok") else 1)


@tools_app.command("status")
def tools_status_command(
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
    prefer_system: bool = typer.Option(False, "--prefer-system", help="Prefer system-installed tools over the managed cache."),
):
    """Report which managed tools are installed and where they resolve from."""
    _apply_prefer_system(prefer_system)
    result = check_tools()
    if result["tools"].get("rga", {}).get("path"):
        # Tool check is a good moment to (idempotently) register the
        # pandoc_office custom adapter in rga's user config.
        ensure_rga_office_config()
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return
    typer.echo(f"platform: {result['platform']}")
    typer.echo(f"tools_dir: {result['tools_dir']}")
    for name, info in result["tools"].items():
        typer.echo(f"{name}: {info['path'] or 'missing'} ({info['source']})")
        if info.get("version"):
            typer.echo(f"  {info['version']}")


@tools_app.command("check")
def tools_check_deprecated(
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
    prefer_system: bool = typer.Option(False, "--prefer-system", help="Prefer system-installed tools over the managed cache."),
):
    """Deprecated alias for `grga tools status` (prints a warning to stderr, then delegates)."""
    typer.echo("[grga] `tools check` is deprecated — use `tools status` instead.", err=True)
    tools_status_command(json_output=json_output, prefer_system=prefer_system)


@tools_app.command("path")
def tools_path_command():
    """Print the platform-specific tools directory currently in use."""
    typer.echo(str(current_tools_dir()))


@tools_app.command("root")
def tools_root_command():
    """Print the root tools directory (parent of all platform subdirs)."""
    typer.echo(str(tools_root()))


@tools_app.command("list")
def tools_list_command():
    """List every known tool and whether the current platform is supported."""
    supported = set(supported_tool_names())
    for name, spec in tool_specs().items():
        status = "supported" if name in supported else "unsupported"
        typer.echo(f"{name}: {spec.repo} ({status})")


@tools_app.command("install")
def tools_install_command(
    names: Optional[List[str]] = typer.Argument(None, help="Tool names to install. Defaults to all supported tools."),
    force: bool = typer.Option(False, "--force", help="Reinstall tools even if they already exist."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Optional URL prefix for GitHub download mirrors."),
    from_dir: Optional[Path] = typer.Option(None, "--from", help="Install from a local mirror directory instead of downloading."),
    fallback_online: bool = typer.Option(False, "--fallback-online", help="With --from, fall back to online download when an asset is missing locally."),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip sha256 verification (insecure)."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
):
    """Install managed tools into the tools cache directory.

    Downloads and verifies (sha256) the requested binaries from the
    upstream release assets, or copies them from a local mirror via
    `--from`. Fails with structured exit codes on network / verify /
    partial-install errors (see `docs/exit-codes.md`).
    """
    verify = not no_verify
    if no_verify:
        emit_warning("insecure", "sha256 verification disabled via --no-verify", json_mode=json_output)

    if from_dir is not None:
        try:
            report = install_tools_from_dir(
                from_dir,
                names=names,
                verify=verify,
                progress=(None if json_output else (lambda message: typer.echo(message))),
                fallback_online=fallback_online,
                base_url=base_url,
            )
        except FileNotFoundError as exc:
            raise typer.Exit(emit_error(EXIT_IO, "no_assets_found", str(exc), json_mode=json_output))
        except VerifyError as exc:
            raise typer.Exit(emit_error(EXIT_VERIFY_FAIL, "verify_failed", str(exc), json_mode=json_output))
        if json_output:
            typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            for name in report["installed"]:
                typer.echo(f"installed: {name}")
            for item in report["failed"]:
                typer.echo(f"failed: {item['name']} ({item['error']})", err=True)
            for item in report["skipped"]:
                typer.echo(f"skipped: {item['name']} ({item['reason']})", err=True)
        if report["failed"]:
            raise typer.Exit(EXIT_PARTIAL_INSTALL if report["installed"] else EXIT_IO)
        # Tool install is a good moment to register the pandoc_office
        # custom adapter in rga's user config (idempotent).
        ensure_rga_office_config()
        return

    try:
        result = install_tools(names=names, force=force, base_url=base_url, verify=verify, progress=lambda message: typer.echo(message))
    except VerifyError as exc:
        raise typer.Exit(emit_error(EXIT_VERIFY_FAIL, "verify_failed", str(exc), json_mode=json_output))
    except Exception as exc:
        raise typer.Exit(emit_error(EXIT_DOWNLOAD_FAIL, "download_failed", str(exc), json_mode=json_output))
    for item in result:
        typer.echo(json.dumps(item, ensure_ascii=False))
    ensure_rga_office_config()


@tools_app.command("update")
def tools_update_command(
    names: Optional[List[str]] = typer.Argument(None, help="Tool names to update. Defaults to all supported tools."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Optional URL prefix for GitHub download mirrors."),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip sha256 verification (insecure)."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
):
    """Update managed tools to the latest supported release."""
    verify = not no_verify
    if no_verify:
        emit_warning("insecure", "sha256 verification disabled via --no-verify", json_mode=json_output)
    try:
        result = update_tools(names=names, base_url=base_url, verify=verify, progress=lambda message: typer.echo(message))
    except VerifyError as exc:
        raise typer.Exit(emit_error(EXIT_VERIFY_FAIL, "verify_failed", str(exc), json_mode=json_output))
    except Exception as exc:
        raise typer.Exit(emit_error(EXIT_DOWNLOAD_FAIL, "download_failed", str(exc), json_mode=json_output))
    for item in result:
        typer.echo(json.dumps(item, ensure_ascii=False))
    ensure_rga_office_config()


@tools_app.command("migrate")
def tools_migrate_command(
    dry_run: bool = typer.Option(False, "--dry-run", help="List the migration plan without copying."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
):
    """Copy tools from the legacy site-packages cache into the XDG data dir."""
    try:
        report = migrate_legacy_tools(dry_run=dry_run, progress=(None if json_output else (lambda message: typer.echo(message))))
    except OSError as exc:
        raise typer.Exit(emit_error(EXIT_IO, "readonly", str(exc), json_mode=json_output))
    except Exception as exc:
        raise typer.Exit(emit_error(EXIT_IO, "migrate_failed", str(exc), json_mode=json_output))
    if json_output:
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if report["errors"]:
        raise typer.Exit(EXIT_IO)



@tools_app.command("platform")
def tools_platform_command():
    """Print the platform key used to select managed tool binaries."""
    typer.echo(platform_key())


@skill_app.command("list")
def skill_list_command(json_output: bool = typer.Option(False, "--json", help="Print raw JSON result.")):
    """List bundled skills shipped with grga."""
    from grga.skill_install import list_skills

    entries = list_skills()
    if json_output:
        typer.echo(json.dumps(entries, ensure_ascii=False, indent=2))
        return
    if not entries:
        typer.echo("no skills bundled", err=True)
        return
    for item in entries:
        typer.echo(f"{item['name']}: {item['skill_md']}")


@skill_app.command("path")
def skill_path_command():
    """Print the directory that ships bundled skill sources."""
    from grga.skill_install import skills_root

    typer.echo(str(skills_root()))


@skill_app.command("install")
def skill_install_command(
    to: Path = typer.Option(..., "--to", help="Destination directory (each skill goes under <to>/<name>)."),
    name: Optional[str] = typer.Option(None, "--name", help="Install a single skill. Default: install every bundled skill."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing skill directories."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
):
    """Install bundled skill(s) into ``--to`` as a stable copy.

    Each skill is copied, not symlinked, so the installed tree is a
    frozen snapshot that ``pip install -U grga`` cannot silently mutate.

    Want a symlink instead (e.g. developing a skill and wanting edits
    to the source to take effect live)? Derive it yourself:
    ``ln -s "$(grga skill path)/<name>" <to>/<name>``. The tradeoff is
    that a symlink follows the installed package, so upgrading or
    uninstalling grga will change or break the link — the default copy
    avoids exactly that.

    Exit code contract:

    * ``0`` — every skill in scope was freshly installed.
    * ``3`` (``user_abort``) — one or more targets already existed and
      ``--force`` was not passed. The report array (JSON) still lists
      per-skill ``status`` so agents can pick apart *which* skills
      collided; the exit code is the only success/exists discriminator.
    * ``9`` — unknown skill (``unknown_skill``) or I/O error.
    """
    from grga.skill_install import install_all, install_skill

    reports = []
    try:
        if name:
            reports = [install_skill(name, to, force=force)]
        else:
            reports = install_all(to, force=force)
    except FileNotFoundError as exc:
        raise typer.Exit(emit_error(EXIT_IO, "unknown_skill", str(exc), json_mode=json_output))
    except FileExistsError as exc:
        raise typer.Exit(emit_error(EXIT_USER_ABORT, "exists", str(exc), json_mode=json_output))
    except OSError as exc:
        raise typer.Exit(emit_error(EXIT_IO, "io", str(exc), json_mode=json_output))
    if json_output:
        typer.echo(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for item in reports:
            typer.echo(f"{item['name']}: {item['status']} -> {item.get('target', '')}")
    statuses = {r.get("status") for r in reports}
    if "exists" in statuses and not force:
        raise typer.Exit(EXIT_USER_ABORT)


@app.command("gui")
def gui_command():
    """Launch the Tk desktop GUI over the same search backend."""
    from grga.gui import main as gui_main

    gui_main()


@app.command("expand")
def expand_command(
    target: str = typer.Argument(..., help="Hit target as 'PATH:LINE' (e.g. docs/foo.md:42)."),
    level: str = typer.Option("chapter", "--level", "-l", help="Expansion level: lines | paragraph | chapter | document | toc."),
    size: int = typer.Option(3, "--size", "-s", help="Line window size for --level lines (+/- size)."),
    directory: Path = typer.Option(Path("."), "--directory", "-d", exists=True, file_okay=False, dir_okay=True, resolve_path=True, help="Base directory the hit path is relative to."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON context."),
):
    """Expand a search hit into a wider context block.

    Levels degrade predictably: chapter -> paragraph -> lines(+/-size) -> lines(+/-0);
    toc -> chapter (when the file has no heading tree). The returned ``level``
    field reflects the level actually achieved.

    ``--level toc`` returns the single-document outline (one line per heading,
    the hit's chapter marked with ``>``) instead of body text. Like
    ``document`` it returns the whole outline and only truncates — centred on
    the hit's chapter — when it would exceed the safety cap.
    """
    from grga.expand import expand as expand_hit
    from grga.models import Hit

    if ":" not in target:
        typer.echo("target must look like 'PATH:LINE'", err=True)
        raise typer.Exit(2)
    path_part, _, line_part = target.rpartition(":")
    try:
        line_number = int(line_part)
    except ValueError:
        typer.echo(f"invalid line number: {line_part!r}", err=True)
        raise typer.Exit(2)
    if not path_part or line_number <= 0:
        typer.echo("target must look like 'PATH:LINE' with LINE > 0", err=True)
        raise typer.Exit(2)
    if level not in ("lines", "paragraph", "chapter", "document", "toc"):
        typer.echo(f"level must be one of: lines, paragraph, chapter, document, toc", err=True)
        raise typer.Exit(2)

    hit = Hit(path=path_part, line_number=line_number, text="")
    context = expand_hit(hit, level=level, size=size, base_dir=str(directory))

    if json_output:
        typer.echo(json.dumps(context.to_dict(), ensure_ascii=False, indent=2))
        return

    typer.echo(f"# {path_part}:{context.range[0]}-{context.range[1]}  level={context.level}" + ("  truncated" if context.truncated else ""), err=True)
    typer.echo(context.text)


def _make_index_build_progress():
    """Return ``(on_event, close)`` for a pretty ``rich`` progress bar.

    Falls back to ``(None, None)`` when stderr is not a TTY or ``rich`` is
    unavailable; the caller then uses the plain-string ``progress`` callback
    instead. The bar is drawn on stderr so ``--json`` / piped stdout stays
    machine-clean (this helper is only invoked in the human, non-JSON path).
    """
    if not sys.stderr.isatty():
        return None, None
    try:
        from rich.console import Console
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
        )
    except Exception:
        return None, None

    console = Console(stderr=True)
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("[dim]{task.fields[note]}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )

    def _short(path: str, width: int = 40) -> str:
        name = Path(path).name
        return name if len(name) <= width else "…" + name[-(width - 1):]

    state: dict = {}
    progress.start()

    def on_event(event: dict) -> None:
        phase = event.get("phase")
        if phase == "scan_start":
            state["scan"] = progress.add_task(
                "[cyan]扫描 / 读取文件", total=None, note=""
            )
        elif phase == "scan_advance":
            progress.update(
                state.get("scan"),
                completed=event["current"],
                note=_short(event.get("path", "")),
            )
        elif phase == "scan_end":
            task = state.get("scan")
            if task is not None:
                progress.update(
                    task,
                    total=event["total_docs"],
                    completed=event["total_docs"],
                    note=f"{event['total_chunks']} chunks",
                )
        elif phase == "batch_start":
            key = "batch_progress"
            if key not in state:
                state[key] = progress.add_task(
                    "[cyan]批量处理", total=event.get("total"), note=""
                )
            progress.update(
                state[key], total=event.get("total"), completed=event.get("batch", 0),
                note=f"批 {event['batch']}/{event['total']}  · {event.get('batch_files', 0)} 文件",
            )
        elif phase == "batch_end":
            task = state.get("batch_progress")
            if task is not None:
                progress.update(
                    task, completed=event.get("batch", 0),
                    note=f"批 {event['batch']}/{event['total']}  · 完成",
                )
        elif phase == "ngram_iter_start":
            key = "ngram"
            if key not in state:
                state[key] = progress.add_task(
                    "[magenta]n-gram 分词", total=event["chunks"] or None, note=""
                )
            progress.update(
                state[key],
                total=event["chunks"] or None,
                completed=0,
                note=f"第 {event['iter']}/{event['iters']} 轮 · {event['chunks']} chunks",
            )
        elif phase == "ngram_iter_advance":
            task = state.get("ngram")
            if task is not None:
                progress.update(
                    task,
                    total=event["total"] or None,
                    completed=event["current"],
                    note=f"ngram {event['current']}/{event['total']}",
                )
        elif phase == "ngram_iter_end":
            task = state.get("ngram")
            if task is not None:
                progress.update(
                    task,
                    completed=event["chunks"],
                    note=f"ngram 完成 · {event['phrases']} 短语",
                )
        elif phase == "ngram_detect":
            # Detector internal phases: unigram / window / filter / sort.
            # Reuse the ngram task bar but switch to the sub-phase's own
            # counter so the user sees movement during the (previously
            # silent) post-tokenise analysis.
            task = state.get("ngram")
            if task is None:
                pass
            else:
                sub = event.get("stage", "")
                it = event.get("iter", 0)
                its = event.get("iters", 0)
                _labels = {
                    "unigram": "词频",
                    "window": "ngram 枚举",
                    "filter": "过滤",
                    "salvage": "打捞",
                    "sort": "排序",
                    "agg_freq": "聚合",
                    "agg_neighbors": "邻居",
                }
                base = sub.rsplit("_", 1)[0]
                label = _labels.get(base, base)
                if sub.endswith("_start"):
                    total = (
                        event.get("lines")
                        or event.get("windows")
                        or event.get("phrases")
                        or None
                    )
                    progress.update(
                        task, total=total, completed=0,
                        note=f"第 {it}/{its} 轮 · {label}…",
                    )
                elif sub.endswith("_advance"):
                    cur = event.get("current", 0)
                    tot = event.get("total") or None
                    extra = ""
                    if "windows" in event:
                        extra = f" · {event['windows']} 窗口"
                    elif "solid_pass" in event:
                        extra = f" · 过实心门 {event['solid_pass']}"
                    elif "kept" in event:
                        extra = f" · 已留 {event['kept']}"
                    progress.update(
                        task, total=tot, completed=cur,
                        note=f"第 {it}/{its} 轮 · {label} {cur}/{tot}{extra}"
                        if tot else f"第 {it}/{its} 轮 · {label}…",
                    )
                elif sub.endswith("_end"):
                    kept = event.get("kept")
                    tail = f" · 留 {kept} 短语" if kept is not None else ""
                    progress.update(
                        task,
                        note=f"第 {it}/{its} 轮 · {label}完成{tail}",
                    )
        elif phase == "ngram_finalize_start":
            key = "ngram"
            if key not in state:
                state[key] = progress.add_task(
                    "[magenta]n-gram 分词", total=event["chunks"] or None, note=""
                )
            progress.update(
                state[key],
                total=event["chunks"] or None,
                completed=0,
                note=f"最终分词 · {event['phrases']} 短语",
            )
        elif phase == "ngram_finalize_advance":
            task = state.get("ngram")
            if task is not None:
                progress.update(
                    task,
                    total=event["total"] or None,
                    completed=event["current"],
                    note=f"最终分词 · {event['current']}/{event['total']} chunks",
                )
        elif phase == "ngram_finalize_end":
            task = state.get("ngram")
            if task is not None:
                progress.update(
                    task,
                    completed=event["chunks"],
                    note=f"最终分词完成 · {event['phrases']} 短语",
                )
        elif phase == "materialise_start":
            total_chunks = event.get("total_chunks") or None
            state["mat"] = progress.add_task(
                "[green]写入 chunks / 关键词",
                total=total_chunks if total_chunks else event["total_docs"],
                note="",
            )
        elif phase == "materialise_advance":
            total_chunks = event.get("total_chunks")
            chunks_done = event.get("chunks_done")
            if total_chunks and chunks_done is not None:
                # Chunk-level granularity: fill by chunk count, annotate
                # with the doc position + current file.
                progress.update(
                    state.get("mat"),
                    total=total_chunks,
                    completed=chunks_done,
                    note=f"文档 {event['current']}/{event['total']} · "
                         f"{chunks_done}/{total_chunks} chunks · "
                         f"{_short(event.get('path', ''))}",
                )
            else:
                # Back-compat: doc-level only (older event shape).
                progress.update(
                    state.get("mat"),
                    completed=event["current"],
                    note=_short(event.get("path", "")),
                )
        elif phase == "pairs_start":
            state["pairs"] = progress.add_task(
                "[yellow]生成共现对",
                total=None,
                note=f"{event.get('candidate_pairs', 0)} 关键词 · 扫描自连接…",
            )
        elif phase == "pairs_scan_start":
            task = state.get("pairs")
            if task is not None:
                progress.update(task, total=None, note="扫描 keyword_chunks 自连接…")
        elif phase == "pairs_scan_end":
            task = state.get("pairs")
            if task is not None:
                progress.update(
                    task,
                    total=event["surviving"] or None,
                    completed=0,
                    note=f"{event['surviving']} 候选对",
                )
        elif phase == "pairs_advance":
            task = state.get("pairs")
            if task is not None:
                tot = event.get("total")
                cur = event.get("current", 0)
                note = f"{cur}/{tot} 对" if tot else f"自连接… ({cur})"
                progress.update(
                    task, total=tot or None, completed=cur, note=note,
                )
        elif phase == "pairs_batch_advance":
            task = state.get("pairs")
            if task is not None:
                progress.update(
                    task,
                    total=event.get("total") or None,
                    completed=event.get("current", 0),
                    note=f"批 {event['batch']}/{event['n_batches']} · 累积 {event['cumulative_pairs']} 对",
                )
        elif phase == "pairs_done":
            task = state.get("pairs")
            if task is not None:
                progress.update(task, note=f"{event['pairs_written']} 对写入")

    def close() -> None:
        try:
            progress.stop()
        except Exception:
            pass

    return on_event, close


@index_app.command("build")
def index_build_command(
    scope: str = typer.Option(..., "--scope", "-s", help="Logical scope name (v10). Portable across machines: the same name identifies the same knowledge domain regardless of where the corpus lives on this machine. Required — v10 does not auto-derive a name from the directory."),
    directory: Path = typer.Option(Path("."), "--directory", "-d", exists=True, resolve_path=True, help="Physical directory (or file) to index on this machine (defaults to the current directory). Recorded in `scope_directories` so caller follow-up can locate source files."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path (defaults to shared deep-search DB)."),
    chunk_mode: str = typer.Option("line", "--chunk-mode", help="Chunk granularity: line (1 line = 1 chunk) or chars (multi-line char window)."),
    chunk_max_chars: int = typer.Option(600, "--chunk-max-chars", help="Target upper bound on chars per chunk (chars mode)."),
    chunk_min_chars: int = typer.Option(300, "--chunk-min-chars", help="Target lower bound on chars per chunk (chars mode)."),
    min_pair_count: Optional[int] = typer.Option(None, "--min-pair-count", help="Minimum chunk co-occurrence count for a pair to enter the knowledge asset. Default (unset): scope-adaptive ladder (≤200 chunks→2, ≤2000→3, ≤20000→4, >20000→5); pass an explicit int to override. Independence-based artefact filtering (co<max(df_a,df_b)) is always on and unaffected by this."),
    min_npmi_build: float = typer.Option(0.2, "--min-npmi-build", help="Minimum npmi for a pair to enter the knowledge asset (v8 storage contract default: 0.2). Pairs below are permanently discarded at build time; ``related --min-npmi`` at query time can only tighten this, never loosen. Set to 0.0 to disable the write-time quality gate (not recommended in production)."),
    max_file_kb: int = typer.Option(0, "--max-file-kb", help="Skip files larger than this many KiB. 0 (default) = no size limit."),
    ngram_iters: int = typer.Option(2, "--ngram-iters", help="Auto n-gram detection passes (used only by `rebuild`; `build` ignores this and does at most one detection iteration on changed files). Note: the default batched pipeline (--batch-chunks>0) runs a single detection iteration per batch and ignores this value; it only takes effect with --batch-chunks 0."),
    max_ngram_n: int = typer.Option(4, "--max-ngram-n", help="Longest n-gram considered by auto-phrase detection (default 4, covers e.g. 联合国行星防御理事会 / 赫尔辛根默斯肯 / PDC常任理事国)."),
    refresh_ngram: str = typer.Option("append", "--refresh-ngram", help="Vocabulary policy for build: `append` runs single-iter detection on changed files and unions new phrases into the dict; `never` skips detection and reuses the existing dict as-is."),
    seed_ngram_from: Optional[str] = typer.Option(None, "--seed-ngram-from", help="Cold-start the tokeniser with another scope's auto n-gram dictionary (v10). Useful after `grga index import` when the target scope has knowledge but no local phrases yet. The seed is layered UNDER the target's own entries (target wins on collision). No-op if the seed scope is missing or equals the target."),
    tags: List[str] = typer.Option([], "--tag", "-t", help="Attach a multi-value scope tag (schema v11). Repeatable — e.g. `--tag 科幻 --tag 小说`. Written to `scope_tags` (knowledge layer, portable across machines). Accumulate-only: existing tags survive; remove with `grga scope tag rm`."),
    append: bool = typer.Option(False, "--append", help="[deprecated] Same effect as the new default (`build` is incremental and never clears the vocabulary). Prints a deprecation warning."),
    replace: bool = typer.Option(False, "--replace", help="[deprecated] Wipe the scope's asset + index + derived layers and rebuild from scratch. Use `grga index forget --all` followed by `grga index build`, or `grga index rebuild --refresh-ngram=replace` instead."),
    include_glob: List[str] = typer.Option([], "--include-glob", help="Only index files whose path relative to --directory matches any of these loose, case-insensitive fnmatch patterns (`*` crosses `/`). Repeatable. Empty = accept all. Files excluded here are NOT removed from the index (weak semantics): they simply do not participate in this build."),
    exclude_glob: List[str] = typer.Option([], "--exclude-glob", help="Skip files whose path relative to --directory matches any of these loose, case-insensitive fnmatch patterns (`*` crosses `/`). Repeatable. Exclude wins over include. Files excluded here are NOT removed from the index (weak semantics): they simply do not participate in this build."),
    no_default_exclude: bool = typer.Option(False, "--no-default-exclude", help="Do not apply the default exclusion globs (archives, executables, node_modules/__pycache__/... that search also skips). The build's own transient-dir safety net (ignore_dirs: .git/.grga/.venv/node_modules/__pycache__) still applies."),
    batch_chunks: int = typer.Option(50000, "--batch-chunks", help="Enable batched build with target N chunks per batch. Default 50000 (batched is the default pipeline). 0 = disabled = legacy single-pass (bit-exact, no per-batch phrase drift / top-N kw cap, but higher peak memory). >0 processes files in RAM-bounded batches with per-batch phrase detection + top-N per-chunk kw cap (see --top-n-kw). refresh_ngram=append (default) unions new phrases; =never reuses the dict as-is. See experiments/batched_build_2026_07_16/FINDINGS.md."),
    top_n_kw: Optional[int] = typer.Option(None, "--top-n-kw", help="Only used with --batch-chunks>0. Per-chunk keyword cap for pair enumeration (higher=more pairs, more RAM). Default via env GRGA_BATCH_TOP_N (default 64)."),
    no_filter_comp_segregation: bool = typer.Option(False, "--no-filter-comp-segregation", help="Disable complementary-pair segregation filtering. By default, pairs whose negative npmi is driven mainly by file-type isolation (different document genres) are filtered out from complementary_pairs."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON stats."),
):
    """Incrementally update the graph index for a scope.

    Only files whose ``(size, mtime)`` differ from the DB metadata are
    read; among those, only ones whose rga output hash actually changed
    are re-tokenised. Files that disappeared from disk have their rows
    removed. The n-gram vocabulary (``auto_ngram_dict``) is
    monotonic-add — nothing here removes phrases. Use ``grga index
    forget`` for that. See ``docs/design/plan-2026-07-11-build-vs-rebuild.md``.

    ``keyword_pairs`` is always fully recomputed from the current
    ``keyword_chunks`` (cheap: single SQL scan + npmi arithmetic).
    """
    from grga.graph.build import (
        VALID_CHUNK_MODES, VALID_REFRESH_NGRAM,
    )

    if chunk_mode not in VALID_CHUNK_MODES:
        typer.echo(f"--chunk-mode must be one of: {', '.join(VALID_CHUNK_MODES)}", err=True)
        raise typer.Exit(2)
    build_refresh_choices = tuple(v for v in VALID_REFRESH_NGRAM if v != "replace")
    if refresh_ngram not in build_refresh_choices:
        typer.echo(
            f"--refresh-ngram for `build` must be one of: "
            f"{', '.join(build_refresh_choices)} "
            f"(got {refresh_ngram!r}); use `grga index rebuild --refresh-ngram=replace` "
            "to clear the vocabulary.",
            err=True,
        )
        raise typer.Exit(2)
    if max_ngram_n < 2:
        typer.echo("--max-ngram-n must be >= 2", err=True)
        raise typer.Exit(2)

    # Deprecated flag translation.
    mode = "build"
    if append:
        typer.echo(
            "[deprecated] --append is now the default `build` behaviour; the "
            "flag is a no-op and will be removed in a future release.",
            err=True,
        )
    if replace:
        typer.echo(
            "[deprecated] `build --replace` is replaced by "
            "`grga index rebuild --refresh-ngram=replace`. Falling back to "
            "the equivalent rebuild for now.",
            err=True,
        )
        mode = "rebuild"
        refresh_ngram = "replace"

    rich_on_event = None
    rich_close = None
    text_progress = None
    if not json_output:
        rich_on_event, rich_close = _make_index_build_progress()
        if rich_on_event is None:
            text_progress = lambda message: typer.echo(message, err=True)

    try:
        stats = _run_index_build(
            scope=scope,
            directory=directory,
            mode=mode,
            refresh_ngram=refresh_ngram,
            db_path=db_path,
            chunk_mode=chunk_mode,
            chunk_max_chars=chunk_max_chars,
            chunk_min_chars=chunk_min_chars,
            min_pair_count=min_pair_count,
            min_npmi=min_npmi_build,
            max_file_kb=max_file_kb,
            ngram_iters=ngram_iters,
            max_ngram_n=max_ngram_n,
            seed_ngram_from=seed_ngram_from,
            tags=tags,
            include_globs=include_glob,
            exclude_globs=exclude_glob,
            default_exclude=not no_default_exclude,
            batch_chunks=batch_chunks,
            top_n_kw=top_n_kw,
            text_progress=text_progress,
            rich_on_event=rich_on_event,
            filter_comp_segregation=not no_filter_comp_segregation,
        )
    finally:
        if rich_close is not None:
            rich_close()
    if json_output:
        typer.echo(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
        return
    _echo_build_summary(stats, max_file_kb=max_file_kb)


@index_app.command("rebuild")
def index_rebuild_command(
    scope: str = typer.Option(..., "--scope", "-s", help="Logical scope name to rebuild (v10). Same semantics as `grga index build`."),
    directory: Path = typer.Option(Path("."), "--directory", "-d", exists=True, resolve_path=True, help="Physical directory (or file) to rebuild from on this machine (defaults to the current directory)."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path (defaults to shared deep-search DB)."),
    chunk_mode: str = typer.Option("line", "--chunk-mode", help="Chunk granularity."),
    chunk_max_chars: int = typer.Option(600, "--chunk-max-chars"),
    chunk_min_chars: int = typer.Option(300, "--chunk-min-chars"),
    min_pair_count: Optional[int] = typer.Option(None, "--min-pair-count", help="Minimum chunk co-occurrence count for a pair to enter the knowledge asset. Default (unset): scope-adaptive ladder (≤200 chunks→2, ≤2000→3, ≤20000→4, >20000→5); pass an explicit int to override. Independence-based artefact filtering (co<max(df_a,df_b)) is always on and unaffected by this."),
    min_npmi_build: float = typer.Option(0.2, "--min-npmi-build", help="Minimum npmi for a pair to enter the knowledge asset (v8 storage contract default: 0.2). Set to 0.0 to disable the write-time quality gate."),
    max_file_kb: int = typer.Option(0, "--max-file-kb"),
    ngram_iters: int = typer.Option(2, "--ngram-iters", help="Full-scope detection passes. Default 2 (the frozen convergence point from experiments/autongram_2026_07_11). Note: the default batched pipeline (--batch-chunks>0) runs a single detection iteration per batch and ignores this value; it only takes effect with --batch-chunks 0."),
    max_ngram_n: int = typer.Option(4, "--max-ngram-n"),
    refresh_ngram: str = typer.Option("append", "--refresh-ngram", help="Vocabulary policy: `append` keeps the existing dict and adds newly detected phrases (default); `never` skips detection and uses the current dict as-is; `replace` clears the dict first, then relearns from the full scope."),
    seed_ngram_from: Optional[str] = typer.Option(None, "--seed-ngram-from", help="Cold-start the tokeniser with another scope's auto n-gram dictionary (v10). Useful after `grga index import`. Target-scope entries take precedence on collision."),
    tags: List[str] = typer.Option([], "--tag", help="Attach a multi-value scope tag (schema v11). Repeatable — e.g. `--tag 科幻 --tag 小说`. Accumulate-only (existing tags survive a rebuild); remove with `grga scope tag rm`."),
    keep_dict: bool = typer.Option(False, "--keep-dict", help="Preserve `auto_ngram_dict` across the rebuild (same effect as --refresh-ngram=never but expressed as a knowledge-preservation flag). Mutually exclusive with --refresh-ngram=replace."),
    keep_pairs: bool = typer.Option(False, "--keep-pairs", help="Preserve keyword pairs + complementary pairs + keyword_stats + scope_stats across the rebuild. New docs' pairs are merged in via UPSERT-add (cooccur_count accumulates); npmi is frozen unless --recompute-pairs is also passed."),
    keep_assets: bool = typer.Option(False, "--keep-assets", help="Shorthand: --keep-dict + --keep-pairs (preserve all knowledge-layer assets)."),
    recompute_pairs: bool = typer.Option(False, "--recompute-pairs", help="After the rebuild, recompute npmi / chunk_df for ALL pairs in this scope. Only meaningful with --keep-pairs or --keep-assets."),
    include_glob: List[str] = typer.Option([], "--include-glob", help="Only index files whose path relative to --directory matches any of these loose, case-insensitive fnmatch patterns (`*` crosses `/`). Repeatable. Empty = accept all. Files excluded here are NOT removed from the index (weak semantics): they simply do not participate in this build."),
    exclude_glob: List[str] = typer.Option([], "--exclude-glob", help="Skip files whose path relative to --directory matches any of these loose, case-insensitive fnmatch patterns (`*` crosses `/`). Repeatable. Exclude wins over include. Files excluded here are NOT removed from the index (weak semantics): they simply do not participate in this build."),
    no_default_exclude: bool = typer.Option(False, "--no-default-exclude", help="Do not apply the default exclusion globs (archives, executables, node_modules/__pycache__/... that search also skips). The build's own transient-dir safety net (ignore_dirs: .git/.grga/.venv/node_modules/__pycache__) still applies."),
    batch_chunks: int = typer.Option(50000, "--batch-chunks", help="Enable batched build with target N chunks per batch. Default 50000 (batched is the default pipeline). 0 = disabled = legacy single-pass (bit-exact, no per-batch phrase drift / top-N kw cap, but higher peak memory). >0 activates a fundamentally different pipeline: per-batch phrase detection + top-N per-chunk kw cap (see --top-n-kw). Faster + lower peak on large corpora BUT produces a different phrase/pair set (per-batch phrase drift ~27%; top-N discards low-rank kws). Use for corpora too large for single-pass. See experiments/batched_build_2026_07_16/FINDINGS.md."),
    top_n_kw: Optional[int] = typer.Option(None, "--top-n-kw", help="Only used with --batch-chunks>0. Per-chunk keyword cap for pair enumeration (higher=more pairs, more RAM). Default via env GRGA_BATCH_TOP_N (default 64)."),
    no_filter_comp_segregation: bool = typer.Option(False, "--no-filter-comp-segregation", help="Disable complementary-pair segregation filtering. By default, pairs whose negative npmi is driven mainly by file-type isolation (different document genres) are filtered out from complementary_pairs."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON stats."),
):
    """Full-scope re-scan: clear evidence layers, then rebuild.

    By default clears the evidence layer (docs / keyword_docs /
    heading_docs) and rebuilds from ``--directory``, while preserving
    the n-gram vocabulary (``--refresh-ngram=append``). Use
    ``--refresh-ngram=replace`` to also clear the vocabulary.

    Use ``--keep-pairs`` / ``--keep-assets`` to preserve knowledge-layer
    assets (keyword pairs, stats) across the rebuild — new evidence is
    merged in, and npmi can be refreshed with ``--recompute-pairs``.
    See design doc §3.2.
    """
    from grga.graph.build import (
        VALID_CHUNK_MODES, VALID_REFRESH_NGRAM,
    )

    if chunk_mode not in VALID_CHUNK_MODES:
        typer.echo(f"--chunk-mode must be one of: {', '.join(VALID_CHUNK_MODES)}", err=True)
        raise typer.Exit(2)
    if refresh_ngram not in VALID_REFRESH_NGRAM:
        typer.echo(f"--refresh-ngram must be one of: {', '.join(VALID_REFRESH_NGRAM)}", err=True)
        raise typer.Exit(2)
    if ngram_iters < 1:
        typer.echo("--ngram-iters must be >= 1", err=True)
        raise typer.Exit(2)
    if max_ngram_n < 2:
        typer.echo("--max-ngram-n must be >= 2", err=True)
        raise typer.Exit(2)

    if keep_assets:
        keep_dict = True
        keep_pairs = True

    if keep_dict and refresh_ngram == "replace":
        typer.echo(
            "rebuild: --keep-dict and --refresh-ngram=replace are mutually "
            "exclusive. Use --keep-dict to preserve the vocabulary, or "
            "drop --keep-dict to let replace clear it.",
            err=True,
        )
        raise typer.Exit(2)

    if recompute_pairs and not keep_pairs:
        typer.echo(
            "rebuild: --recompute-pairs requires --keep-pairs or --keep-assets "
            "(otherwise no pre-existing pairs remain to recompute)",
            err=True,
        )
        raise typer.Exit(2)

    rich_on_event = None
    rich_close = None
    text_progress = None
    if not json_output:
        rich_on_event, rich_close = _make_index_build_progress()
        if rich_on_event is None:
            text_progress = lambda message: typer.echo(message, err=True)

    try:
        stats = _run_index_build(
            scope=scope,
            directory=directory,
            mode="rebuild",
            refresh_ngram=refresh_ngram,
            db_path=db_path,
            chunk_mode=chunk_mode,
            chunk_max_chars=chunk_max_chars,
            chunk_min_chars=chunk_min_chars,
            min_pair_count=min_pair_count,
            min_npmi=min_npmi_build,
            max_file_kb=max_file_kb,
            ngram_iters=ngram_iters,
            max_ngram_n=max_ngram_n,
            seed_ngram_from=seed_ngram_from,
            tags=tags,
            include_globs=include_glob,
            exclude_globs=exclude_glob,
            default_exclude=not no_default_exclude,
            batch_chunks=batch_chunks,
            top_n_kw=top_n_kw,
            text_progress=text_progress,
            rich_on_event=rich_on_event,
            keep_dict=keep_dict,
            keep_pairs=keep_pairs,
            recompute_pairs=recompute_pairs,
            filter_comp_segregation=not no_filter_comp_segregation,
        )
    finally:
        if rich_close is not None:
            rich_close()
    if json_output:
        typer.echo(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
        return
    _echo_build_summary(stats, max_file_kb=max_file_kb)


def _run_index_build(
    *,
    scope: str,
    directory: Path,
    mode: str,
    refresh_ngram: str,
    db_path: Optional[Path],
    chunk_mode: str,
    chunk_max_chars: int,
    chunk_min_chars: int,
    min_pair_count: Optional[int],
    min_npmi: float = 0.2,
    max_file_kb: int,
    ngram_iters: int,
    max_ngram_n: int,
    seed_ngram_from: Optional[str] = None,
    tags: Optional[List[str]] = None,
    include_globs: Optional[List[str]] = None,
    exclude_globs: Optional[List[str]] = None,
    default_exclude: bool = True,
    batch_chunks: int = 50000,
    top_n_kw: Optional[int] = None,
    text_progress,
    rich_on_event,
    keep_dict: bool = False,
    keep_pairs: bool = False,
    recompute_pairs: bool = False,
    filter_comp_segregation: bool = True,
):
    """Shared runner for `index build` and `index rebuild`.

    Owns the single call site into ``build_graph_index`` so both CLI
    commands stay in sync. ``scope`` is the logical scope name (v10),
    ``directory`` is the physical path to index; ``build_graph_index``
    records the binding in ``scope_directories``. ``tags`` (v11) are
    accumulate-only labels UPSERTed into ``scope_tags``.

    ``default_exclude`` (``--no-default-exclude``) applies the same
    ``DEFAULT_EXCLUDE_GLOBS`` the search path uses, translated to the
    build walk's fnmatch dialect via :func:`grga.search._to_build_globs`,
    so build and search share one exclusion source. User ``--exclude-glob``
    patterns are appended after the defaults; the fnmatch matcher is
    "exclude wins", so explicit user excludes always take effect.
    """
    from grga.graph.build import build_graph_index

    if default_exclude:
        effective_excludes = list(_to_build_globs())
    else:
        effective_excludes = []
    for glob in exclude_globs or ():
        if glob not in effective_excludes:
            effective_excludes.append(glob)

    return build_graph_index(
        scope,
        directory=directory,
        db_path=str(db_path) if db_path else None,
        chunk_mode=chunk_mode,
        chunk_max_chars=chunk_max_chars,
        chunk_min_chars=chunk_min_chars,
        min_pair_count=min_pair_count,
        min_npmi=min_npmi,
        max_file_bytes=max_file_kb * 1024,
        ngram_iters=ngram_iters,
        max_ngram_n=max_ngram_n,
        seed_ngram_from=seed_ngram_from,
        tags=tuple(tags or ()),
        include_globs=tuple(include_globs or ()),
        exclude_globs=tuple(effective_excludes),
        mode=mode,
        refresh_ngram=refresh_ngram,
        batch_chunks=batch_chunks,
        top_n_kw=top_n_kw,
        progress=text_progress,
        on_event=rich_on_event,
        keep_dict=keep_dict,
        keep_pairs=keep_pairs,
        recompute_pairs=recompute_pairs,
        filter_comp_segregation=filter_comp_segregation,
    )


@index_app.command("forget")
def index_forget_command(
    scope: str = typer.Option(..., "--scope", "-s", help="Logical scope name (v10) whose asset layer to touch. Missing scopes are a no-op."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path (defaults to shared deep-search DB)."),
    phrase: List[str] = typer.Option([], "--phrase", "-p", help="Phrase to forget from `auto_ngram_dict`. Repeatable. Cascades: removes `keyword_chunks` rows for the matching keyword id in this scope and `keyword_pairs` edges with the phrase on either endpoint, then re-derives `keyword_pairs` (npmi depends on scope-global n_chunks / chunk_df)."),
    all_phrases: bool = typer.Option(False, "--all", help="[deprecated] Wipe the whole scope: `auto_ngram_dict` + index layer three tables + `keyword_pairs.chunk_cooccur`. `llm_promoted` pairs are preserved. Follow with `grga index build` to rebuild from scratch. Now the default behaviour (pass no --phrase)."),
    keep_dict: bool = typer.Option(False, "--keep-dict", help="When wiping the whole scope, preserve `auto_ngram_dict` (the n-gram tokeniser vocabulary). Evidence layer (docs / keyword_docs / heading_docs) is still cleared."),
    keep_pairs: bool = typer.Option(False, "--keep-pairs", help="When wiping the whole scope, preserve keyword pairs + complementary pairs + keyword_stats + scope_stats (all knowledge-layer pair assets). npmi is left frozen at its current value."),
    keep_assets: bool = typer.Option(False, "--keep-assets", help="Shorthand: --keep-dict + --keep-pairs (preserve all knowledge-layer assets)."),
    recompute_pairs: bool = typer.Option(False, "--recompute-pairs", help="Recompute npmi / chunk_df for all preserved pairs after the wipe. Only meaningful with --keep-pairs or --keep-assets."),
    min_pair_count: int = typer.Option(5, "--min-pair-count", help="[Deprecated in v9 — accepted for backward CLI compatibility but not used. Per-phrase forget in v9 does not recompute pairs (unaffected pairs' cooccur / chunk_df / npmi are stable).]", hidden=True),

    json_output: bool = typer.Option(False, "--json", help="Print raw JSON stats."),
):
    """Remove phrases or wipe a scope's evidence / knowledge layers.

    Default (no --phrase): wipe the whole scope's evidence layer +
    auto-generated knowledge assets.  ``llm_promoted`` pairs and
    ``scope_directories`` bindings are preserved.  Use ``--keep-dict``,
    ``--keep-pairs``, or ``--keep-assets`` to preserve specific
    knowledge-layer tables.

    With ``--phrase``: remove specific phrases from the vocabulary with
    cascading cleanup.  This is the manual, minimum-viable form of the
    future decay mechanism.  Design doc §3.3-3.4.
    """
    from grga.graph.build import forget_scope_phrases

    if keep_assets:
        keep_dict = True
        keep_pairs = True

    if phrase:
        if keep_dict or keep_pairs or keep_assets or recompute_pairs:
            typer.echo(
                "forget: --keep-* / --recompute-pairs only apply to the "
                "full-wipe mode (no --phrase)",
                err=True,
            )
            raise typer.Exit(2)
    else:
        if all_phrases:
            emit_warning(
                "deprecated",
                "--all is deprecated; forgetting the whole scope is now the "
                "default behaviour. The flag is a no-op and will be removed "
                "in a future release.",
                json_mode=json_output,
            )
        all_phrases = True  # default = full wipe

    if recompute_pairs and not keep_pairs:
        typer.echo(
            "forget: --recompute-pairs requires --keep-pairs or --keep-assets "
            "(otherwise no pairs remain to recompute)",
            err=True,
        )
        raise typer.Exit(2)

    try:
        stats = forget_scope_phrases(
            scope,
            db_path=str(db_path) if db_path else None,
            phrases=phrase if phrase else None,
            all_phrases=all_phrases,
            keep_dict=keep_dict,
            keep_pairs=keep_pairs,
            recompute_pairs=recompute_pairs,
        )
    except ValueError as exc:
        typer.echo(f"forget: {exc}", err=True)
        raise typer.Exit(2)

    if json_output:
        typer.echo(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
        return
    if stats.all_wiped:
        parts = [f"{stats.keyword_chunks_removed} keyword_docs rows"]
        if not stats.keep_dict:
            parts.append(f"{stats.ngram_dict_removed} auto_ngram phrases")
        if not stats.keep_pairs:
            parts.append("keyword_pairs + stats")
        else:
            if recompute_pairs:
                parts.append(f"pair npmi recomputed ({stats.pairs_recomputed} pairs)")
            else:
                parts.append(f"pairs preserved frozen ({stats.pairs_recomputed} pairs)")
        if stats.keep_dict:
            parts.append("dict preserved")
        typer.echo(
            f"forget: cleared {', '.join(parts)} for "
            f"{stats.scope} in {stats.elapsed_seconds:.2f}s"
        )
    else:
        typer.echo(
            f"forget: removed {stats.ngram_dict_removed} phrases, "
            f"{stats.keyword_chunks_removed} keyword_chunks rows, "
            f"re-derived {stats.pairs_recomputed} pair rows "
            f"in {stats.elapsed_seconds:.2f}s"
        )
        if stats.phrases_missing:
            typer.echo(
                f"  {len(stats.phrases_missing)} phrase(s) not found: "
                + ", ".join(stats.phrases_missing[:5])
                + (" ..." if len(stats.phrases_missing) > 5 else "")
            )


def _echo_build_summary(stats, *, max_file_kb: int) -> None:
    """Shared human-readable summary for `build` / `rebuild`."""
    typer.echo(
        f"[{stats.mode}] indexed {stats.files_indexed}/{stats.files_scanned} files, "
        f"{stats.keyword_chunks_written} keyword-doc rows, "
        f"{stats.pairs_written} pair rows, "
        f"{stats.heading_docs_written} heading rows "
        f"({stats.md_files_indexed} md), "
        f"{stats.ngram_phrases_written} auto-ngram phrases "
        f"({stats.ngram_iters_run} iters) in {stats.elapsed_seconds:.2f}s"
    )
    if stats.mode == "build":
        typer.echo(
            f"  incremental: +{stats.docs_added} added, "
            f"~{stats.docs_updated} updated, "
            f"={stats.docs_unchanged} unchanged, "
            f"-{stats.docs_removed} removed, "
            f"{stats.docs_metadata_only} metadata-only"
        )
    if stats.ngram_dict_added:
        typer.echo(
            f"  vocabulary: +{stats.ngram_dict_added} new phrases "
            f"(dict size: {stats.ngram_dict_size})"
        )
    for warning in stats.warnings:
        typer.echo(f"warning: {warning}", err=True)
    if stats.files_scanned == 0:
        reasons = stats.reasons
        too_large = reasons.get("too_large", 0)
        bad_suffix = reasons.get("bad_suffix", 0)
        parts: List[str] = []
        if too_large:
            parts.append(f"{too_large} file(s) exceeded --max-file-kb={max_file_kb}")
        if bad_suffix:
            parts.append(f"{bad_suffix} file(s) had unindexed suffixes")
        if parts:
            if too_large:
                relax = "pass --max-file-kb 0 for no size limit or raise it"
            else:
                relax = "narrow --scope or add supported suffixes"
            typer.echo(
                "hint: no files were indexed; " + ", ".join(parts) + ". "
                f"Try {relax}.",
                err=True,
            )
        else:
            typer.echo(
                "hint: no text files found under --scope; "
                "check the path or add supported suffixes.",
                err=True,
            )


@index_app.command("vacuum")
def index_vacuum_command(
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path (defaults to shared deep-search DB)."),
    json_output: bool = typer.Option(False, "--json", help="Print size delta as JSON."),
):
    """Reclaim disk space in the graph DB by running ``VACUUM``.

    The schema v5 migration leaves a large freelist (tables were renamed +
    rewritten in-place); ``VACUUM`` compacts the file back to its packed
    size. Safe to run at any time and generally a no-op on a fresh DB.
    """
    from grga.graph.schema import vacuum_db
    from grga.deep_search import DEFAULT_DB_PATH

    target = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    if not target.exists():
        typer.echo(f"no DB at {target}", err=True)
        raise typer.Exit(2)
    result = vacuum_db(target)
    if json_output:
        typer.echo(json.dumps({"db_path": str(target), **result}, ensure_ascii=False))
        return
    mb = 1024 * 1024
    typer.echo(
        f"vacuum {target}: "
        f"{result['before_bytes'] // mb} MB → "
        f"{result['after_bytes'] // mb} MB "
        f"(freed {result['freed_bytes'] // mb} MB)"
    )


@index_app.command("promote")
def index_promote_command(
    threshold: float = typer.Option(3, "--threshold", "-t", help="Minimum vote_count for a pair to be promoted."),
    scope: Optional[str] = typer.Option(None, "--scope", "-s", help="Logical scope name (v10). Restrict promotion to votes recorded for this scope. Omit to promote across every scope."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path (defaults to shared deep-search DB)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be promoted without writing."),
    repromote: bool = typer.Option(False, "--repromote", help="Promote qualifying pairs again even if already promoted."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON stats."),
):
    """Merge high-vote LLM keyword pairs into ``keyword_pairs`` (L3 -> L2 refeed).

    Only pairs with ``vote_count >= threshold`` graduate. The
    ``llm_extension_votes`` records are never deleted, so the refeed stays
    auditable and reversible.
    """
    from grga.graph.promote import promote_votes

    stats = promote_votes(
        threshold=threshold,
        scope=scope,
        db_path=str(db_path) if db_path else None,
        dry_run=dry_run,
        repromote=repromote,
    )
    if json_output:
        typer.echo(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
        return
    verb = "would promote" if dry_run else "promoted"
    typer.echo(
        f"{verb} {stats.promoted}/{stats.candidates} candidate pairs "
        f"(threshold={stats.threshold:g}, skipped {stats.skipped_already_promoted} already promoted)"
    )
    for pair in stats.pairs:
        mark = " [already]" if pair.already_promoted else ""
        typer.echo(f"  {pair.vote_count:g}  {pair.keyword_a} <-> {pair.keyword_b}{mark}")


# ---------------------------------------------------------------------------
# grga index export / import — knowledge pack (v10, plan-2026-07-14 §5)
# ---------------------------------------------------------------------------


@index_app.command("export")
def index_export_command(
    out: Path = typer.Option(..., "--out", "-o", resolve_path=True, help="Destination path. With --scope: a .grgapkg file (overwritten atomically via same-directory tempfile + os.replace). With --all: a directory (one <scope>.grgapkg per scope written into it)."),
    scope: Optional[str] = typer.Option(None, "--scope", "-s", help="Single scope name to export. Mutually exclusive with --all."),
    all_scopes: bool = typer.Option(False, "--all", help="Export every scope in the DB. Writes one pack per scope into --out (which must be a directory). The `1 pack = 1 scope` invariant keeps `grga index import` deterministic — pre-review this flag silently produced multi-scope packs that its own import path then rejected."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="Source SQLite graph library path."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON stats."),
):
    """Write portable knowledge pack(s) (SQLite ``.grgapkg``).

    A pack carries only the **knowledge layer** (scopes / keywords /
    keyword_pairs / keyword_stats / scope_stats / auto_ngram_dict /
    llm_extension_votes / meta). Evidence (docs / keyword_docs / etc.) is
    stripped so the pack is machine-portable — reuse it on another host
    via ``grga index import`` and rebuild local evidence with
    ``grga index build``.

    Design: docs/design/plan-2026-07-14-scope-directory-split.md §5.
    """
    from grga.graph.pack import export_all_scopes, export_knowledge_pack

    if bool(scope) == bool(all_scopes):
        typer.echo(
            "export: pass exactly one of --scope <name> or --all",
            err=True,
        )
        raise typer.Exit(2)

    try:
        if all_scopes:
            per_scope_stats = export_all_scopes(
                out_dir=out,
                db_path=str(db_path) if db_path else None,
            )
        else:
            per_scope_stats = [export_knowledge_pack(
                out_path=out,
                scope=scope,
                db_path=str(db_path) if db_path else None,
            )]
    except LookupError as exc:
        typer.echo(f"export: {exc}", err=True)
        raise typer.Exit(1)
    except ValueError as exc:
        typer.echo(f"export: {exc}", err=True)
        raise typer.Exit(2)

    if json_output:
        payload = (
            {"packs": [s.to_dict() for s in per_scope_stats]}
            if all_scopes
            else per_scope_stats[0].to_dict()
        )
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if all_scopes:
        typer.echo(f"exported {len(per_scope_stats)} pack(s) into {out}")
        for stats in per_scope_stats:
            name = stats.scopes_included[0] if stats.scopes_included else "?"
            typer.echo(
                f"  {name}: pairs={stats.n_pairs} keywords={stats.n_keywords} "
                f"stats={stats.n_stats} ngram={stats.n_auto_ngram} "
                f"votes={stats.n_votes}  ({stats.elapsed_seconds:.2f}s)"
            )
    else:
        stats = per_scope_stats[0]
        typer.echo(
            f"exported scope {stats.scopes_included[0]!r} → {stats.out_path}\n"
            f"  keywords={stats.n_keywords} pairs={stats.n_pairs} "
            f"stats={stats.n_stats} ngram={stats.n_auto_ngram} "
            f"votes={stats.n_votes}  ({stats.elapsed_seconds:.2f}s)"
        )


@index_app.command("export-candidates")
def index_export_candidates_command(
    seeds: Optional[str] = typer.Option(
        None, "--seeds",
        help="Initial concept set, comma-separated (e.g. '韩立,李化元'). "
             "Mutually exclusive with --auto-seed.",
    ),
    auto_seed: Optional[int] = typer.Option(
        None, "--auto-seed",
        help="Cold-start: auto-select N seeds from the scope's vocabulary "
             "(high chunk_df, non-stopword). Mutually exclusive with --seeds.",
    ),
    top: int = typer.Option(
        20, "--top", "-n",
        help="Per-seed neighborhood size (npmi-ordered candidates).",
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", resolve_path=True,
        help="Output JSONL path (written atomically). Default stdout.",
    ),
    max_context_chars: int = typer.Option(
        2000, "--max-context-chars",
        help="Per-candidate original-text context cap (characters).",
    ),
    scopes: List[str] = typer.Option(
        [], "--scope", "-s",
        help="Logical scope name. Repeatable — pass multiple `--scope` flags "
             "to union across them. At least one scope is required unless "
             "--cross-scope is set.",
    ),
    cross_scope: bool = typer.Option(
        False, "--cross-scope",
        help="Union across every indexed scope (ignores --scope).",
    ),
    db_path: Optional[Path] = typer.Option(
        None, "--db-path", "--db", help="SQLite graph library path.",
    ),
):
    """Export a dream raw-material candidate package (JSONL).

    grga only moves material here — it never runs an LLM, never writes to
    the graph DB, and never changes search/probe/expand/related behaviour.
    The caller's LLM turns the package into entities + relations (see
    docs/design/plan-2026-10-14-dream-ontology.md).
    """
    from grga.export_candidates import export_candidates, write_jsonl

    if (seeds is not None and seeds.strip()) and auto_seed is not None:
        typer.echo(
            "export-candidates: --seeds and --auto-seed are mutually exclusive",
            err=True,
        )
        raise typer.Exit(2)
    if not cross_scope and not [s for s in scopes if s and s.strip()]:
        typer.echo(
            "export-candidates: pass --scope <name> (repeatable) or --cross-scope",
            err=True,
        )
        raise typer.Exit(2)
    if top <= 0:
        typer.echo("export-candidates: --top must be >= 1", err=True)
        raise typer.Exit(2)
    if max_context_chars <= 0:
        typer.echo(
            "export-candidates: --max-context-chars must be >= 1", err=True,
        )
        raise typer.Exit(2)

    seed_list = [s for s in (seeds or "").split(",") if s and s.strip()]
    if not seed_list and auto_seed is None:
        typer.echo(
            "export-candidates: pass --seeds <terms> or --auto-seed N",
            err=True,
        )
        raise typer.Exit(2)
    scope_list = [s for s in scopes if s and s.strip()]

    try:
        export = export_candidates(
            seed_list,
            top=top,
            max_context_chars=max_context_chars,
            db_path=str(db_path) if db_path else None,
            scopes=scope_list,
            cross_scope=cross_scope,
            auto_seed=auto_seed,
        )
    except LookupError as exc:
        typer.echo(f"export-candidates: {exc}", err=True)
        raise typer.Exit(1)
    except ValueError as exc:
        typer.echo(f"export-candidates: {exc}", err=True)
        raise typer.Exit(2)

    write_jsonl(export.rows, out)
    if out is not None:
        typer.echo(
            f"exported {len(export.rows)} candidate(s) → {out}",
            err=True,
        )
    if export.unknown_seeds:
        typer.echo(
            f"unknown seeds (not in index): {', '.join(export.unknown_seeds)}",
            err=True,
        )
    if export.isolated_seeds:
        typer.echo(
            f"indexed but no candidates (hub / isolated / min-npmi too high): "
            f"{', '.join(export.isolated_seeds)}",
            err=True,
        )
    typer.echo(
        f"{len(export.rows)} candidates from seeds {export.seeds or '(auto)'} "
        f"(top {top}, max-context {max_context_chars})",
        err=True,
    )


@index_app.command("import")
def index_import_command(
    pack: Path = typer.Argument(..., exists=True, dir_okay=False, resolve_path=True, help="Knowledge pack (.grgapkg) to import."),
    as_scope: str = typer.Option(..., "--as", "-a", help="Target scope name to create in the destination DB. Must not collide with an existing scope."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="Destination SQLite graph library path."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON stats."),
):
    """Import a knowledge pack under a new scope name.

    The pack must contain exactly one scope (single-scope export). The
    caller re-labels it via ``--as`` to avoid collisions. Evidence layer
    is not touched — run ``grga index build --scope <as> --directory
    <path>`` on this machine afterwards to attach local corpus.

    ``--merge-into`` is not implemented this round (npmi merge policy
    undecided; see design doc §5.2 / D9).
    """
    from grga.graph.pack import import_knowledge_pack

    try:
        stats = import_knowledge_pack(
            pack,
            as_scope=as_scope,
            db_path=str(db_path) if db_path else None,
        )
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"import: {exc}", err=True)
        raise typer.Exit(1)
    except NotImplementedError as exc:
        typer.echo(f"import: {exc}", err=True)
        raise typer.Exit(2)

    if json_output:
        typer.echo(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
        return
    typer.echo(
        f"imported {stats.pack_path} → scope {stats.into_scope!r}\n"
        f"  new keywords={stats.n_keywords_new} pairs={stats.n_pairs} "
        f"stats={stats.n_stats} ngram={stats.n_auto_ngram} "
        f"votes={stats.n_votes} scope_stats={stats.scope_stats_written} "
        f"({stats.elapsed_seconds:.2f}s)"
    )


@app.command("suggest")
def suggest_command(
    query: List[str] = typer.Argument(..., help="Original query (natural language or keyword expression)."),
    intent: str = typer.Option("expand", "--intent", "-i", help="Suggest intent: expand | rerank | reformulate."),
    scope: Optional[str] = typer.Option(None, "--scope", "-s", help="Logical scope name (v10) to attribute recorded keyword-pair votes to. Omit to skip vote recording."),
    hits_json: Optional[Path] = typer.Option(None, "--hits", exists=True, dir_okay=False, help="JSON file with prior hits (a list, or a search result object with 'matches')."),
    max_hits: int = typer.Option(20, "--max-hits", help="Maximum hits sent to the LLM."),
    no_votes: bool = typer.Option(False, "--no-votes", help="Do not record keyword-pair votes (read-only)."),
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url", help="OpenAI-compatible API base URL."),
    llm_api_key: Optional[str] = typer.Option(None, "--llm-api-key", help="OpenAI-compatible API key."),
    llm_model: Optional[str] = typer.Option(None, "--llm-model", help="OpenAI-compatible model name."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path (defaults to shared deep-search DB)."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
    debug: bool = typer.Option(False, "--debug", help="Print debug information."),
):
    """L3 LLM suggestion: expand keywords, rerank hits, or reformulate a query.

    Emitted keyword pairs are voted into ``llm_extension_votes`` unless
    ``--no-votes`` is set; run ``grga index promote`` later to graduate them.
    """
    from grga.suggest import VALID_INTENTS, suggest as run_suggest

    if intent not in VALID_INTENTS:
        typer.echo(f"intent must be one of: {', '.join(VALID_INTENTS)}", err=True)
        raise typer.Exit(2)
    text = _join_keywords(query)
    if not text:
        typer.echo("query cannot be empty", err=True)
        raise typer.Exit(2)

    hits: List = []
    if hits_json is not None:
        try:
            with open(hits_json, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, ValueError) as exc:
            typer.echo(f"failed to read hits: {exc}", err=True)
            raise typer.Exit(2)
        if isinstance(loaded, dict):
            hits = loaded.get("matches") or []
        elif isinstance(loaded, list):
            hits = loaded

    try:
        suggestion = run_suggest(
            text,
            hits,
            intent=intent,
            scope=scope,
            max_hits=max_hits,
            record_votes_enabled=not no_votes,
            db_path=str(db_path) if db_path else None,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            llm_model_name=llm_model,
            debug=debug,
        )
    except Exception as exc:
        if json_output:
            typer.echo(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    if json_output:
        typer.echo(json.dumps(suggestion.to_dict(), ensure_ascii=False, indent=2))
        return
    if suggestion.reason:
        typer.echo(f"# {suggestion.reason}", err=True)
    if suggestion.keywords:
        typer.echo("keywords: " + ", ".join(suggestion.keywords))
    if suggestion.queries:
        for q in suggestion.queries:
            typer.echo(f"query: {q}")
    if suggestion.ranking:
        typer.echo("ranking: " + ", ".join(str(i) for i in suggestion.ranking))
    if suggestion.keyword_pairs:
        typer.echo("pairs: " + ", ".join(f"{a}<->{b}" for a, b in suggestion.keyword_pairs))
    if suggestion.votes:
        typer.echo(
            f"votes recorded: {suggestion.votes['pairs_recorded']} pairs "
            f"(+{suggestion.votes['votes_added']:g})",
            err=True,
        )
    typer.echo(f"tokens: {suggestion.tokens_used} ({suggestion.model})", err=True)






@app.command("related")
def related_command(
    keywords: List[str] = typer.Argument(..., help="Seed keywords."),
    mode: str = typer.Option("graph", "--mode", "-m", help="Relation mode: `graph` (candidate keywords via co-occurrence) or `heading` (docs sharing a Markdown chapter with a seed-containing doc)."),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum number of results (per-seed for graph mode)."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path (defaults to shared deep-search DB)."),
    scopes: List[str] = typer.Option([], "--scope", "-s", help="Logical scope name (v10). Repeatable — pass multiple `--scope` flags to union across scopes. Omit to search across every indexed scope (cross-scope union). When combined with `--tag`, the two filters are intersected: only scopes matching BOTH the name list and the tag filter contribute pairs."),
    tags: List[str] = typer.Option([], "--tag", "-t", help="Scope tag label (schema v11). Repeatable — e.g. `--tag 科幻 --tag 小说`. Resolves to the set of scopes whose `scope_tags` match under `--tag-logic`, then feeds the resulting scope_id set into the same `keyword_pairs.scope_id IN (...)` filter as `--scope`. Combine with `--scope` for name ∩ tag."),
    tag_logic: str = typer.Option("or", "--tag-logic", help="How multiple `--tag` values combine: `or` (default; scopes with any listed tag) or `and` (scopes carrying every listed tag)."),
    same_scope: bool = typer.Option(False, "--same-scope", help="mode=heading only: restrict heading matches to the given --scope (single scope required)."),
    min_npmi: Optional[float] = typer.Option(None, "--min-npmi", help="mode=graph only: read-side floor on chunk_cooccur pair npmi. Can only tighten the v8 write-time floor (0.2). Default 0.2, or $GRGA_PPR_MIN_NPMI."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
):
    """List related keywords (graph) or documents (heading) for the seeds.

    v10 (plan-2026-07-14): the scope filter is a **multi-select**
    ``--scope <name>`` (repeatable). No filter = cross-scope union.

    * ``--mode graph`` returns *candidate keywords* + association evidence
      (npmi, cooccur_count, chunk_df, via_seed).
    * ``--mode heading`` returns documents that share a Markdown
      ``heading_path`` with any seed-containing document.
    """
    from grga.related import VALID_MODES, VALID_TAG_LOGIC, related as related_docs

    if mode not in ("graph",) + tuple(VALID_MODES):
        typer.echo(
            f"mode must be one of: graph, {', '.join(VALID_MODES)}",
            err=True,
        )
        raise typer.Exit(2)

    seeds = [item for item in keywords if item.strip()]
    if not seeds:
        typer.echo("keywords cannot be empty", err=True)
        raise typer.Exit(2)
    scope_list = [s for s in scopes if s and s.strip()]
    tag_list = [t for t in tags if t and t.strip()]
    if tag_logic not in VALID_TAG_LOGIC:
        typer.echo(
            f"--tag-logic must be one of: {', '.join(VALID_TAG_LOGIC)}",
            err=True,
        )
        raise typer.Exit(2)
    if tag_list and mode != "graph":
        typer.echo("--tag only applies to --mode graph", err=True)
        raise typer.Exit(2)
    if same_scope and mode != "heading":
        typer.echo("--same-scope only applies to --mode heading", err=True)
        raise typer.Exit(2)
    if same_scope and len(scope_list) != 1:
        typer.echo("--same-scope requires exactly one --scope", err=True)
        raise typer.Exit(2)
    if min_npmi is not None and mode != "graph":
        typer.echo("--min-npmi only applies to --mode graph", err=True)
        raise typer.Exit(2)

    if mode == "graph":
        from grga.related import related_keywords

        kw_result = related_keywords(
            seeds,
            limit_per_seed=limit,
            db_path=str(db_path) if db_path else None,
            scopes=scope_list or None,
            tags=tag_list or None,
            tag_logic=tag_logic,
            min_npmi=min_npmi,
        )
        kw_results = kw_result.results
        if json_output:
            typer.echo(json.dumps(kw_result.to_dict(), ensure_ascii=False, indent=2))
            return
        if not kw_results:
            scope_label = ", ".join(scope_list) if scope_list else "all scopes"
            typer.echo(f"no signal in scope '{scope_label}'")
        else:
            for r in kw_results:
                typer.echo(
                    f"{r.npmi:.4f}  {r.kw}"
                    f"  (via {r.via_seed}, co={int(r.cooccur_count)}, df={r.chunk_df})"
                )
        produced = {r.via_seed for r in kw_results}
        unknown = [s.seed for s in kw_result.resolved_seeds if not s.in_index]
        isolated = [
            s.seed
            for s in kw_result.resolved_seeds
            if s.in_index and s.seed not in produced
        ]
        if unknown:
            typer.echo(f"unknown seeds (not in index): {', '.join(unknown)}", err=True)
        if isolated:
            typer.echo(
                f"indexed but no candidates (hub / isolated / min-npmi too high): "
                f"{', '.join(isolated)}",
                err=True,
            )
        typer.echo(
            f"{len(kw_results)} related keywords ({mode})", err=True
        )
        return

    # mode == 'heading' — doc-returning path via related() (single scope only).
    heading_scope = scope_list[0] if scope_list else None
    results = related_docs(
        seeds,
        mode=mode,
        limit=limit,
        db_path=str(db_path) if db_path else None,
        scope=heading_scope,
        same_scope=same_scope,
    )
    if json_output:
        out = {"results": [r.to_dict() for r in results]}
        typer.echo(json.dumps(out, ensure_ascii=False, indent=2))
        return
    if not results:
        scope_label = heading_scope or "all scopes"
        typer.echo(f"no signal in scope '{scope_label}'")
        raise typer.Exit(0)
    for item in results:
        kw = ("  <- " + ", ".join(item.keywords)) if item.keywords else ""
        typer.echo(f"{item.score:.4f}  {item.doc_path}{kw}")
    typer.echo(f"{len(results)} related documents ({mode})", err=True)


@graph_app.command("inspect")
def graph_inspect_command(
    keyword: str = typer.Argument(..., help="Keyword to inspect."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path (defaults to shared deep-search DB)."),
    limit: int = typer.Option(20, "--limit", "-n", help="Cap top_cooccurring_keywords returned."),
    similar_limit: int = typer.Option(10, "--similar-limit", help="Cap similar_keywords in the not-indexed branch (vocab substring/superstring probe)."),
    docs_limit: int = typer.Option(5, "--docs-limit", help="Cap top_docs returned in the indexed branch (documents holding the keyword, sorted by occurrence density). 0 disables the field."),
    scopes: List[str] = typer.Option([], "--scope", "-s", help="Logical scope name (v10). Repeatable — restrict every field (docs_by_scope / top_docs / top_cooccurring_keywords / knowledge_scopes) to the given scopes. Omit for cross-scope union. Combined with --tag, the two filters intersect (name ∩ tag)."),
    tags: List[str] = typer.Option([], "--tag", "-t", help="Scope tag label (schema v11). Repeatable. Resolves to the set of scopes whose scope_tags match under --tag-logic, then narrows the inspect the same way --scope does. Combine with --scope for name ∩ tag."),
    tag_logic: str = typer.Option("or", "--tag-logic", help="How multiple --tag values combine: `or` (default; scopes with any listed tag) or `and` (scopes carrying every listed tag)."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
):
    """Inspect a keyword's distribution across scopes and polysemy signals.

    Pass ``--scope`` / ``--tag`` to narrow the report to specific corpora
    (same filter semantics as ``grga related``); omit both for a
    cross-scope view.
    """
    from grga.graph.inspect import inspect_keyword
    from grga.related import VALID_TAG_LOGIC

    if tag_logic not in VALID_TAG_LOGIC:
        typer.echo(
            f"--tag-logic must be one of: {', '.join(VALID_TAG_LOGIC)}",
            err=True,
        )
        raise typer.Exit(code=2)

    report = inspect_keyword(
        keyword,
        db_path=str(db_path) if db_path else None,
        limit=limit,
        similar_limit=similar_limit,
        docs_limit=docs_limit,
        scope_names=scopes or None,
        tag_names=tags or None,
        tag_logic=tag_logic,
    )
    if json_output:
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
        return
    typer.echo(f"keyword: {report['keyword']}")
    typer.echo(f"total_docs: {report['total_docs']}")
    if report["docs_by_scope"]:
        typer.echo("docs_by_scope:")
        for scope, count in report["docs_by_scope"].items():
            typer.echo(f"  {count:>4}  {scope}")
    top_docs = report.get("top_docs") or []
    if top_docs:
        suffix = " (truncated)" if report.get("top_docs_truncated") else ""
        typer.echo(f"top_docs{suffix}:")
        for entry in top_docs:
            typer.echo(
                f"  occ={entry['occurrence_count']:<5} "
                f"chunks={entry['chunk_count']:<4} "
                f"[{entry['scope']}] {entry['doc_path']}"
            )
    if report["top_cooccurring_keywords"]:
        typer.echo("top_cooccurring_keywords:")
        for entry in report["top_cooccurring_keywords"]:
            scopes_str = ", ".join(entry["shared_scopes"])
            typer.echo(f"  {entry['weight']:8.3f}  {entry['keyword']}  [{scopes_str}]")
    # An explicit --scope / --tag that matched zero scopes returns a
    # dedicated ``scope_filter_hint`` (distinct from the not-indexed
    # ``index_hint``): the keyword may well be indexed, just not under the
    # requested filter.
    if report.get("scope_filter_hint"):
        typer.echo(f"scope_filter_hint: {report['scope_filter_hint']}", err=True)
    # The not-indexed branch (§1.5.1 naming ironclad) publishes an
    # ``index_hint`` instead of ``polysemy_hint`` and drops
    # ``suspected_polysemy`` altogether — use ``.get`` so the CLI stays
    # branch-agnostic and never KeyErrors on the missing polysemy fields.
    if report.get("index_hint"):
        typer.echo(f"index_hint: {report['index_hint']}", err=True)
        similar = report.get("similar_keywords") or []
        if similar:
            typer.echo("similar_keywords:", err=True)
            for entry in similar:
                typer.echo(
                    f"  {entry['chunk_df']:>6}  {entry['kw']}  [{entry['kind']}]",
                    err=True,
                )
    if report.get("suspected_polysemy"):
        typer.echo(f"suspected_polysemy: {report['polysemy_hint']}", err=True)


# ---------------------------------------------------------------------------
# grga scope — logical scope management (v10)
# ---------------------------------------------------------------------------


@scope_app.command("list")
def scope_list_command(
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path (defaults to shared deep-search DB)."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
):
    """List every logical scope with its local directory bindings + sizes.

    Empty ``directories`` means the scope carries knowledge only (typical
    after ``grga index import`` before a local re-``build``).
    """
    from grga.graph.scope import list_scopes

    scopes = list_scopes(db_path=str(db_path) if db_path else None)
    if json_output:
        typer.echo(json.dumps(
            [s.to_dict() for s in scopes], ensure_ascii=False, indent=2
        ))
        return
    if not scopes:
        typer.echo("no scopes in this DB — creating scopes via `grga index build --scope <name> --directory <path>` is a user-side offline task", err=True)
        return
    for s in scopes:
        dirs = ", ".join(d.directory for d in s.directories) or "(no local bindings)"
        tag_suffix = f"  tags=[{', '.join(s.tags)}]" if s.tags else ""
        typer.echo(
            f"{s.name}  pairs={s.n_pairs}  keywords={s.n_keywords}  docs={s.n_docs}{tag_suffix}\n"
            f"  {dirs}"
        )


@scope_app.command("bind")
def scope_bind_command(
    scope: str = typer.Argument(..., help="Logical scope name."),
    directory: Path = typer.Argument(..., exists=True, resolve_path=True, help="Local directory to bind to the scope."),
    role: Optional[str] = typer.Option(None, "--role", help="Optional binding role tag (e.g. `primary` / `mirror`)."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path."),
):
    """Bind a local directory to a logical scope (evidence-layer metadata).

    Idempotent — repeat calls refresh ``indexed_at``. Creates the scope
    on demand if it does not exist yet. Does not touch corpus files
    (use `grga index build` for that).
    """
    from grga.graph.scope import bind_directory

    bind_directory(
        scope, directory,
        role=role,
        db_path=str(db_path) if db_path else None,
    )
    typer.echo(f"bound {directory} → scope {scope!r}")


@scope_app.command("unbind")
def scope_unbind_command(
    scope: str = typer.Argument(..., help="Logical scope name."),
    directory: Optional[Path] = typer.Argument(None, resolve_path=True, help="Directory to unbind (omit to unbind every directory for this scope)."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path."),
):
    """Remove one (or every) directory binding for a scope.

    Metadata-only — does not delete corpus files, indexed docs, or the
    scope's knowledge assets. Use `grga scope rm` for that.
    """
    from grga.graph.scope import unbind_directory

    n = unbind_directory(
        scope, directory,
        db_path=str(db_path) if db_path else None,
    )
    if n == 0:
        typer.echo(f"no matching binding for scope {scope!r}", err=True)
        raise typer.Exit(0)
    typer.echo(f"unbound {n} directory binding(s) from scope {scope!r}")


@scope_app.command("rename")
def scope_rename_command(
    old_name: str = typer.Argument(..., help="Current scope name."),
    new_name: str = typer.Argument(..., help="New scope name."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path."),
):
    """Rename a scope. Knowledge assets survive intact (single-row UPDATE)."""
    from grga.graph.scope import rename_scope

    try:
        rename_scope(
            old_name, new_name,
            db_path=str(db_path) if db_path else None,
        )
    except LookupError as exc:
        typer.echo(f"scope rename: {exc}", err=True)
        raise typer.Exit(1)
    except FileExistsError as exc:
        typer.echo(f"scope rename: {exc}", err=True)
        raise typer.Exit(1)
    except ValueError as exc:
        typer.echo(f"scope rename: {exc}", err=True)
        raise typer.Exit(2)
    typer.echo(f"renamed scope {old_name!r} → {new_name!r}")


@scope_app.command("rm")
def scope_rm_command(
    scope: str = typer.Argument(..., help="Logical scope name to remove."),
    knowledge: bool = typer.Option(False, "--knowledge", help="Also delete the scope's knowledge assets (keyword_pairs / keyword_stats / scope_stats / auto_ngram_dict / llm_extension_votes) AND the scope row itself. Without this flag only the evidence layer (bindings + docs + doc-level indexes) is removed; the scope's memorised knowledge stays."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON stats."),
):
    """Remove a scope's evidence (default) or evidence + knowledge (``--knowledge``).

    Default: wipe ``scope_directories`` + ``docs`` + ``keyword_docs`` +
    ``heading_docs`` + v1 heat counters. The scope's knowledge (pairs /
    stats / ngram dict / votes) is left memorised — this matches design
    D6 (`grow-and-refine`: knowledge is never destroyed by evidence loss).

    ``--knowledge``: additionally purge knowledge assets and delete the
    ``scopes`` row itself. Not reversible without a knowledge-pack backup.
    """
    from grga.graph.scope import remove_scope

    if not yes:
        action = "evidence + knowledge (irreversible)" if knowledge else "evidence only"
        typer.echo(
            f"About to remove scope {scope!r} — {action}. "
            "Re-run with --yes to confirm.",
            err=True,
        )
        raise typer.Exit(1)

    stats = remove_scope(
        scope,
        purge_knowledge=knowledge,
        db_path=str(db_path) if db_path else None,
    )
    if json_output:
        typer.echo(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
        return
    # Determine "not found" from a POSITIVE signal (scope_found), not by
    # inferring absence from zero side-effect counts. A knowledge-only
    # scope (freshly imported, no local build yet) legitimately has 0
    # docs / 0 bindings but MUST be reported as existing.
    if not stats.scope_found:
        typer.echo(f"scope {scope!r} not found (no-op)", err=True)
        raise typer.Exit(0)
    if knowledge:
        typer.echo(
            f"scope {scope!r} removed (evidence + knowledge): "
            f"bindings={stats.directory_bindings_removed} "
            f"docs={stats.docs_removed} "
            f"keyword_docs={stats.keyword_docs_removed} "
            f"heading_docs={stats.heading_docs_removed}\n"
            f"  knowledge purged: pairs={stats.pairs_removed} "
            f"stats={stats.stats_rows_removed} "
            f"votes={stats.votes_removed} "
            f"ngram={stats.ngram_rows_removed} "
            f"scope_row={stats.scope_row_removed}"
        )
    else:
        # Evidence-only: matches design D6 (knowledge is preserved).
        typer.echo(
            f"scope {scope!r} evidence cleared (knowledge retained): "
            f"bindings={stats.directory_bindings_removed} "
            f"docs={stats.docs_removed} "
            f"keyword_docs={stats.keyword_docs_removed} "
            f"heading_docs={stats.heading_docs_removed}"
        )


@scope_tag_app.command("add")
def scope_tag_add_command(
    scope: str = typer.Argument(..., help="Logical scope name."),
    tags: List[str] = typer.Argument(..., help="One or more tag labels to attach."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path."),
):
    """Attach multi-value tag labels to a scope (creates the scope on demand).

    Accumulate-only (``INSERT OR IGNORE``): re-adding an existing tag is a
    no-op. Tags are knowledge-layer metadata (they travel in a knowledge
    pack). Use them to filter `grga related --tag ... --tag-logic and|or`.
    """
    from grga.graph.scope import add_tags

    clean = [t for t in tags if t and t.strip()]
    if not clean:
        typer.echo("scope tag add: at least one non-empty tag is required", err=True)
        raise typer.Exit(2)
    n = add_tags(scope, clean, db_path=str(db_path) if db_path else None)
    typer.echo(f"added {n} new tag(s) to scope {scope!r} ({len(clean)} requested)")


@scope_tag_app.command("rm")
def scope_tag_rm_command(
    scope: str = typer.Argument(..., help="Logical scope name."),
    tags: Optional[List[str]] = typer.Argument(None, help="Tag labels to remove (omit to remove EVERY tag on the scope)."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path."),
):
    """Remove tag labels from a scope (or all of them when none are given).

    Metadata-only — does not touch the scope's knowledge assets or
    evidence. Missing scope / tags is a no-op.
    """
    from grga.graph.scope import remove_tags

    clean = [t for t in (tags or []) if t and t.strip()]
    n = remove_tags(
        scope,
        clean or None,
        db_path=str(db_path) if db_path else None,
    )
    if n == 0:
        typer.echo(f"no matching tag(s) for scope {scope!r}", err=True)
        raise typer.Exit(0)
    typer.echo(f"removed {n} tag(s) from scope {scope!r}")


@scope_tag_app.command("list")
def scope_tag_list_command(
    scope: str = typer.Argument(..., help="Logical scope name."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
):
    """List every tag attached to a scope."""
    from grga.graph.scope import list_tags

    tags = list_tags(scope, db_path=str(db_path) if db_path else None)
    if json_output:
        typer.echo(json.dumps({"scope": scope, "tags": tags}, ensure_ascii=False))
        return
    if not tags:
        typer.echo(f"scope {scope!r} has no tags", err=True)
        return
    for tag in tags:
        typer.echo(tag)


@scope_tag_app.command("mv")
def scope_tag_mv_command(
    scope: str = typer.Argument(..., help="Logical scope name."),
    old_tag: str = typer.Argument(..., help="Current tag label."),
    new_tag: str = typer.Argument(..., help="New tag label."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path."),
):
    """Rename a tag on a scope (single-row UPDATE — knowledge intact).

    Strict: refuses to overwrite an existing tag on the same scope. If
    the target label already exists there, drop the old one first with
    `grga scope tag rm <scope> <old>`. Only affects this scope — other
    scopes' identically-named tags are left alone.
    """
    from grga.graph.scope import rename_tag

    try:
        rename_tag(
            scope, old_tag, new_tag,
            db_path=str(db_path) if db_path else None,
        )
    except LookupError as exc:
        typer.echo(f"scope tag mv: {exc}", err=True)
        raise typer.Exit(1)
    except FileExistsError as exc:
        typer.echo(f"scope tag mv: {exc}", err=True)
        raise typer.Exit(1)
    except ValueError as exc:
        typer.echo(f"scope tag mv: {exc}", err=True)
        raise typer.Exit(2)
    typer.echo(
        f"renamed tag on scope {scope!r}: {old_tag!r} -> {new_tag!r}"
    )


@mcp_app.command("start")
def mcp_start_command(
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path (defaults to $GRGA_DB_PATH or shared deep-search DB)."),
    directory: Optional[Path] = typer.Option(None, "--directory", "-d", exists=True, file_okay=False, dir_okay=True, resolve_path=True, help="Default search directory. probe / search / expand fall back to it when the caller omits `directory` / `base_dir`."),
):
    """Start the MCP server in stdio mode.

    Exposes probe / search / expand / related / suggest / scope_list as
    MCP tools for any local MCP client (Claude Desktop, Cline, Cursor, ...).
    Requires the optional dependency: ``pip install grga[mcp]``.

    ``--directory`` sets a server-wide default search directory so callers
    can omit the per-call ``directory`` / ``base_dir`` argument.
    """
    try:
        from grga.mcp.server import run_server
    except ImportError as exc:
        typer.echo(
            "grga mcp requires the optional 'mcp' dependency. "
            "Install it with: pip install \"grga[mcp]\"",
            err=True,
        )
        if exc.name not in ("mcp",) and exc.name is not None:
            typer.echo(f"detail: {exc}", err=True)
        raise typer.Exit(1)

    if db_path is not None:
        os.environ["GRGA_DB_PATH"] = str(db_path.expanduser())
    if directory is not None:
        os.environ["GRGA_MCP_DEFAULT_DIRECTORY"] = str(directory)

    run_server()


def main():
    from grga.env_loader import ensure_env

    ensure_env()
    app()
