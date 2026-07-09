import json
import os
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
from grga.search import build_rga_command_preview, check_environment, configure_logging, search_directory
from grga.toolchain import (
    MANUAL_INSTALL_URLS,
    MissingToolsError,
    OfflineError,
    PartialInstallError,
    UserAbortError,
    VerifyError,
    check_tools,
    current_tools_dir,
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
    max_total: Optional[int] = typer.Option(1000, "--max-total", "-N", help="Global cap on total real matches; results are truncated when exceeded (default 1000, set 0 to disable)."),
    strict_cap: bool = typer.Option(False, "--strict-cap", help="Fail with too_many_matches when --max-total is exceeded instead of truncating."),
    timeout: float = typer.Option(600.0, "--timeout", "-t", help="Search timeout in seconds."),
    before_context: int = typer.Option(3, "--before-context", "-B", help="Lines of leading context."),
    after_context: int = typer.Option(3, "--after-context", "-A", help="Lines of trailing context."),
    multiline: bool = typer.Option(False, "--multiline", help="Enable multiline search."),
    file_globs: Optional[List[str]] = typer.Option(None, "--iglob", "-g", help="Case-insensitive file glob. Can be used multiple times."),
    path_only: bool = typer.Option(False, "--path-only", help="Search relative file paths only."),
    implicit_and: bool = typer.Option(False, "--implicit-and", help="Force implicit adjacency to AND (default). Suppresses the notice."),
    implicit_or: bool = typer.Option(False, "--implicit-or", help="Restore v1 behaviour: implicit adjacency means OR."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
    metadata: bool = typer.Option(True, "--metadata/--no-metadata", help="Attach Layer 2 hit metadata (occurrence, ratio, nearby)."),
    offline: bool = typer.Option(False, "--offline", help="Fail fast instead of downloading missing tools."),
    prefer_system: bool = typer.Option(False, "--prefer-system", help="Prefer system-installed tools over the managed cache."),
    debug: bool = typer.Option(False, "--debug", help="Print debug information during search."),
):
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

    effective_debug = debug or _get_config_debug()
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
    directory: Path = typer.Option(Path("."), "--directory", "-d", exists=True, file_okay=False, dir_okay=True, resolve_path=True, help="Directory to search."),
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
    query = _join_keywords(keywords)
    if not query:
        typer.echo("query cannot be empty", err=True)
        raise typer.Exit(2)

    effective_debug = debug or _get_config_debug()
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
):
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
    )
    typer.echo(command)


@app.command("probe")
def probe_command(
    keywords: List[str] = typer.Argument(..., help="Search keywords or query expression."),
    directory: Path = typer.Option(Path("."), "--directory", "-d", exists=True, file_okay=False, dir_okay=True, resolve_path=True, help="Directory to probe."),
    pattern_mode: str = typer.Option("auto", "--mode", "-m", help="Pattern mode: auto, regex, or glob."),
    case_sensitive: bool = typer.Option(False, "--case-sensitive/--ignore-case", help="Enable or disable case-sensitive search."),
    timeout: float = typer.Option(60.0, "--timeout", "-t", help="Probe timeout in seconds."),
    multiline: bool = typer.Option(False, "--multiline", help="Enable multiline search."),
    file_globs: Optional[List[str]] = typer.Option(None, "--iglob", "-g", help="Case-insensitive file glob. Can be used multiple times."),
    path_only: bool = typer.Option(False, "--path-only", help="Probe relative file paths only."),
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

    effective_debug = debug or _get_config_debug()
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

    notice = result.get("implicit_notice")
    if notice:
        typer.echo(f"[grga] {notice}", err=True)
    typer.echo(f"elapsed: {result.get('elapsed_ms', 0)} ms", err=True)


@app.command("check")
def check_command(json_output: bool = typer.Option(False, "--json", help="Print raw JSON result.")):
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


@tools_app.command("check")
def tools_check_command(
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
    prefer_system: bool = typer.Option(False, "--prefer-system", help="Prefer system-installed tools over the managed cache."),
):
    _apply_prefer_system(prefer_system)
    result = check_tools()
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return
    typer.echo(f"platform: {result['platform']}")
    typer.echo(f"tools_dir: {result['tools_dir']}")
    for name, info in result["tools"].items():
        typer.echo(f"{name}: {info['path'] or 'missing'} ({info['source']})")
        if info.get("version"):
            typer.echo(f"  {info['version']}")


@tools_app.command("path")
def tools_path_command():
    typer.echo(str(current_tools_dir()))


@tools_app.command("root")
def tools_root_command():
    typer.echo(str(tools_root()))


@tools_app.command("list")
def tools_list_command():
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
        return

    try:
        result = install_tools(names=names, force=force, base_url=base_url, verify=verify, progress=lambda message: typer.echo(message))
    except VerifyError as exc:
        raise typer.Exit(emit_error(EXIT_VERIFY_FAIL, "verify_failed", str(exc), json_mode=json_output))
    except Exception as exc:
        raise typer.Exit(emit_error(EXIT_DOWNLOAD_FAIL, "download_failed", str(exc), json_mode=json_output))
    for item in result:
        typer.echo(json.dumps(item, ensure_ascii=False))


@tools_app.command("update")
def tools_update_command(
    names: Optional[List[str]] = typer.Argument(None, help="Tool names to update. Defaults to all supported tools."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Optional URL prefix for GitHub download mirrors."),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip sha256 verification (insecure)."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
):
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
    typer.echo(platform_key())


@skill_app.command("list")
def skill_list_command(json_output: bool = typer.Option(False, "--json", help="Print raw JSON result.")):
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
    from grga.skill_install import skills_root

    typer.echo(str(skills_root()))


@skill_app.command("install")
def skill_install_command(
    to: Path = typer.Option(..., "--to", help="Destination directory (each skill goes under <to>/<name>)."),
    name: Optional[str] = typer.Option(None, "--name", help="Install a single skill. Default: install every bundled skill."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing skill directories."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
):
    """Install bundled skill(s) into ``--to``.

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
    from grga.gui import main as gui_main

    gui_main()


@app.command("expand")
def expand_command(
    target: str = typer.Argument(..., help="Hit target as 'PATH:LINE' (e.g. docs/foo.md:42)."),
    level: str = typer.Option("chapter", "--level", "-l", help="Expansion level: lines | paragraph | chapter | document."),
    size: int = typer.Option(3, "--size", "-s", help="Line window size for --level lines (+/- size)."),
    directory: Path = typer.Option(Path("."), "--directory", "-d", exists=True, file_okay=False, dir_okay=True, resolve_path=True, help="Base directory the hit path is relative to."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON context."),
):
    """Expand a search hit into a wider context block.

    Levels degrade predictably: chapter -> paragraph -> lines(+/-size) -> lines(+/-0).
    The returned ``level`` field reflects the level actually achieved.
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
    if level not in ("lines", "paragraph", "chapter", "document"):
        typer.echo(f"level must be one of: lines, paragraph, chapter, document", err=True)
        raise typer.Exit(2)

    hit = Hit(path=path_part, line_number=line_number, text="")
    context = expand_hit(hit, level=level, size=size, base_dir=str(directory))

    if json_output:
        typer.echo(json.dumps(context.to_dict(), ensure_ascii=False, indent=2))
        return

    typer.echo(f"# {path_part}:{context.range[0]}-{context.range[1]}  level={context.level}" + ("  truncated" if context.truncated else ""), err=True)
    typer.echo(context.text)


@index_app.command("build")
def index_build_command(
    scope: Path = typer.Option(Path("."), "--scope", "-s", exists=True, resolve_path=True, help="Directory (or file) to index."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path (defaults to shared deep-search DB)."),
    min_term_count: int = typer.Option(2, "--min-count", help="Minimum occurrences for a keyword to be indexed."),
    max_terms: int = typer.Option(200, "--max-terms", help="Maximum keywords stored per file."),
    max_file_kb: int = typer.Option(512, "--max-file-kb", help="Skip files larger than this many KiB."),
    append: bool = typer.Option(False, "--append", help="Accumulate onto existing rows instead of replacing the scope."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON stats."),
):
    """Build ``keyword_docs`` / ``heading_docs`` for a directory scope.

    The v1 tables (keyword_counts / keyword_pairs) are never modified.
    """
    from grga.graph.build import build_graph_index

    stats = build_graph_index(
        scope,
        db_path=str(db_path) if db_path else None,
        max_terms_per_file=max_terms,
        min_term_count=min_term_count,
        max_file_bytes=max_file_kb * 1024,
        replace=not append,
        progress=(None if json_output else (lambda message: typer.echo(message, err=True))),
    )
    if json_output:
        typer.echo(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
        return
    typer.echo(
        f"indexed {stats.files_indexed}/{stats.files_scanned} files, "
        f"{stats.keyword_docs_written} keyword rows, "
        f"{stats.heading_docs_written} heading rows "
        f"({stats.md_files_indexed} md) in {stats.elapsed_seconds:.2f}s"
    )


@index_app.command("promote")
def index_promote_command(
    threshold: float = typer.Option(3, "--threshold", "-t", help="Minimum vote_count for a pair to be promoted."),
    directory: Optional[Path] = typer.Option(None, "--directory", "-d", resolve_path=True, help="Restrict to votes recorded for this scope."),
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
        directory=str(directory) if directory else None,
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


@app.command("suggest")
def suggest_command(
    query: List[str] = typer.Argument(..., help="Original query (natural language or keyword expression)."),
    intent: str = typer.Option("expand", "--intent", "-i", help="Suggest intent: expand | rerank | reformulate."),
    directory: Optional[Path] = typer.Option(None, "--directory", "-d", resolve_path=True, help="Scope key for vote recording."),
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
            directory=str(directory) if directory else None,
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


def _echo_preview_summary(preview: Optional[dict]) -> None:
    """Render a compact ``crossed_scopes_preview`` summary to stderr.

    Used on the human-readable ``related --mode graph --preview`` path (no
    ``--json``) so the preview signal is surfaced instead of silently
    discarded. ``None`` (no scope filter active) prints a short note.
    """
    if preview is None:
        typer.echo(
            "# preview: no --directory scope active, query already spans "
            "every scope (nothing to cross into)",
            err=True,
        )
        return
    would_add = preview.get("would_add", 0)
    scope_count = len(preview.get("top_other_scopes", []))
    typer.echo(
        f"# preview: --cross-scope would add {would_add} docs across "
        f"{scope_count} other scope(s)",
        err=True,
    )
    bridges = preview.get("top_bridge_keywords", [])
    if bridges:
        rendered = ", ".join(
            f"{b['keyword']} (polysemy?)" if b.get("suspected_polysemy")
            else b["keyword"]
            for b in bridges
        )
        typer.echo(f"# top bridge keywords: {rendered}", err=True)


@app.command("related")
def related_command(
    keywords: List[str] = typer.Argument(..., help="Seed keywords."),
    mode: str = typer.Option("graph", "--mode", "-m", help="Relation mode: graph | dir | heading."),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum number of related documents."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path (defaults to shared deep-search DB)."),
    directory: Optional[Path] = typer.Option(None, "--directory", "-d", resolve_path=True, help="Restrict to a previously indexed scope."),
    cross_scope: bool = typer.Option(False, "--cross-scope", help="mode=graph only: seed docs stay in --directory, PPR expansion crosses into every other scope."),
    same_scope: bool = typer.Option(False, "--same-scope", help="mode=heading only: restrict heading matches to --directory (reverts to pre-v2.2 default)."),
    preview: bool = typer.Option(False, "--preview", help="mode=graph only: also compute crossed_scopes_preview. Since v2.3 the preview shares the main query's PPR pass and costs at most one extra keyword_docs scan. With --json it is embedded in the envelope; without --json a compact summary is printed to stderr."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
):
    """List documents related to the given keywords via the L2 graph."""
    from grga.related import VALID_MODES, related as related_docs, related_graph_with_preview

    if mode not in VALID_MODES:
        typer.echo(f"mode must be one of: {', '.join(VALID_MODES)}", err=True)
        raise typer.Exit(2)
    seeds = [item for item in keywords if item.strip()]
    if not seeds:
        typer.echo("keywords cannot be empty", err=True)
        raise typer.Exit(2)
    if cross_scope and mode != "graph":
        typer.echo("--cross-scope only applies to --mode graph", err=True)
        raise typer.Exit(2)
    if same_scope and mode != "heading":
        typer.echo("--same-scope only applies to --mode heading", err=True)
        raise typer.Exit(2)
    if preview and mode != "graph":
        typer.echo("--preview only applies to --mode graph", err=True)
        raise typer.Exit(2)

    if mode == "graph" and (json_output or preview):
        payload = related_graph_with_preview(
            seeds,
            limit=limit,
            db_path=str(db_path) if db_path else None,
            directory=str(directory) if directory else None,
            cross_scope=cross_scope,
            preview=preview,
        )
        results = payload["results"]
        if json_output:
            out = {
                "results": [r.to_dict() for r in results],
                "crossed_scopes_preview": payload["crossed_scopes_preview"],
            }
            typer.echo(json.dumps(out, ensure_ascii=False, indent=2))
            return
        # Human-readable + --preview: render results, then a one-line
        # preview summary to stderr so the signal is not silently dropped.
        for item in results:
            kw = ("  <- " + ", ".join(item.keywords)) if item.keywords else ""
            typer.echo(f"{item.score:.4f}  {item.doc_path}{kw}")
        _echo_preview_summary(payload["crossed_scopes_preview"])
        typer.echo(f"{len(results)} related documents ({mode})", err=True)
        return

    results = related_docs(
        seeds,
        mode=mode,
        limit=limit,
        db_path=str(db_path) if db_path else None,
        directory=str(directory) if directory else None,
        cross_scope=cross_scope,
        same_scope=same_scope,
    )
    if json_output:
        typer.echo(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))
        return
    if not results:
        typer.echo("no related documents found", err=True)
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
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
):
    """Inspect a keyword's distribution across scopes and polysemy signals."""
    from grga.graph.inspect import inspect_keyword

    report = inspect_keyword(
        keyword,
        db_path=str(db_path) if db_path else None,
        limit=limit,
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
    if report["top_cooccurring_keywords"]:
        typer.echo("top_cooccurring_keywords:")
        for entry in report["top_cooccurring_keywords"]:
            scopes = ", ".join(entry["shared_scopes"])
            typer.echo(f"  {entry['weight']:8.3f}  {entry['keyword']}  [{scopes}]")
    if report["suspected_polysemy"]:
        typer.echo(f"suspected_polysemy: {report['polysemy_hint']}", err=True)


def main():
    app()
