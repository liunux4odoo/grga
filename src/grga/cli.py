import json
from pathlib import Path
from typing import List, Optional

import typer

from grga.deep_search import deep_search_directory, resolve_deep_search_options
from grga.search import build_rga_command_preview, check_environment, configure_logging, search_directory
from grga.toolchain import check_tools, current_tools_dir, install_tools, platform_key, supported_tool_names, tool_specs, tools_root, update_tools

app = typer.Typer(no_args_is_help=True, help="Search files with ripgrep-all helpers.")
tools_app = typer.Typer(no_args_is_help=True, help="Manage bundled command line tools.")
app.add_typer(tools_app, name="tools")

CONFIG_PATH = Path.home() / ".config" / "grga" / "settings.json"


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


def _print_result(result):
    if not result.get("ok"):
        typer.echo(result.get("error", "search failed"), err=True)
        raise typer.Exit(1)

    if result.get("warning"):
        typer.echo(f"warning: {result['warning']}", err=True)
    skipped = result.get("skipped_files") or []
    if skipped:
        typer.echo(f"skipped: {len(skipped)} records", err=True)

    matches = result.get("matches", [])
    for item in matches:
        path = item.get("path", "")
        line_number = item.get("line_number")
        text = item.get("text", "")
        separator = "-" if item.get("is_context") else ":"
        if isinstance(line_number, int):
            typer.echo(f"{path}{separator}{line_number}{separator}{text}")
        else:
            typer.echo(f"{path}{separator}{text}")

    match_count = sum(1 for item in matches if not item.get("is_context"))
    file_count = len({item.get("path", "") for item in matches})
    typer.echo(f"{match_count} matches in {file_count} files", err=True)


@app.command("search")
def search_command(
    keywords: List[str] = typer.Argument(..., help="Search keywords or query expression."),
    directory: Path = typer.Option(Path("."), "--directory", "-d", exists=True, file_okay=False, dir_okay=True, resolve_path=True, help="Directory to search."),
    pattern_mode: str = typer.Option("auto", "--mode", "-m", help="Pattern mode: auto, regex, or glob."),
    case_sensitive: bool = typer.Option(False, "--case-sensitive/--ignore-case", help="Enable or disable case-sensitive search."),
    max_count: Optional[int] = typer.Option(None, "--max-count", "-n", help="Maximum number of matches."),
    timeout: float = typer.Option(600.0, "--timeout", "-t", help="Search timeout in seconds."),
    before_context: int = typer.Option(3, "--before-context", "-B", help="Lines of leading context."),
    after_context: int = typer.Option(3, "--after-context", "-A", help="Lines of trailing context."),
    multiline: bool = typer.Option(False, "--multiline", help="Enable multiline search."),
    file_globs: Optional[List[str]] = typer.Option(None, "--iglob", "-g", help="Case-insensitive file glob. Can be used multiple times."),
    path_only: bool = typer.Option(False, "--path-only", help="Search relative file paths only."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
    debug: bool = typer.Option(False, "--debug", help="Print debug information during search."),
):
    query = _join_keywords(keywords)
    if not query:
        typer.echo("keywords cannot be empty", err=True)
        raise typer.Exit(2)

    effective_debug = debug or _get_config_debug()
    configure_logging(debug=effective_debug)

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
        )
        result = json.loads(raw)
    except Exception as exc:
        if json_output:
            typer.echo(json.dumps({"ok": False, "error": str(exc), "matches": []}, ensure_ascii=False, indent=2))
        else:
            typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        raise typer.Exit(0 if result.get("ok") else 1)

    _print_result(result)


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
    db_path: Optional[Path] = typer.Option(None, "--db-path", help="SQLite keyword library path."),
    decay_factor: Optional[float] = typer.Option(None, "--decay-factor", help="Decay multiplier applied to keyword counts before each run (0-1)."),
    min_keyword_count: Optional[float] = typer.Option(None, "--min-keyword-count", help="Drop keyword records whose count falls below this threshold."),
    global_library: Optional[bool] = typer.Option(None, "--global-library/--directory-library", help="Use the whole keyword library or only the current directory scope."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
    debug: bool = typer.Option(False, "--debug", help="Print debug information during search."),
):
    query = _join_keywords(keywords)
    if not query:
        typer.echo("query cannot be empty", err=True)
        raise typer.Exit(2)

    effective_debug = debug or _get_config_debug()
    configure_logging(debug=effective_debug)

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
        debug=effective_debug,
    )
    result = json.loads(raw)
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
def tools_check_command(json_output: bool = typer.Option(False, "--json", help="Print raw JSON result.")):
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
):
    try:
        result = install_tools(names=names, force=force, base_url=base_url, progress=lambda message: typer.echo(message))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    for item in result:
        typer.echo(json.dumps(item, ensure_ascii=False))


@tools_app.command("update")
def tools_update_command(
    names: Optional[List[str]] = typer.Argument(None, help="Tool names to update. Defaults to all supported tools."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Optional URL prefix for GitHub download mirrors."),
):
    try:
        result = update_tools(names=names, base_url=base_url, progress=lambda message: typer.echo(message))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    for item in result:
        typer.echo(json.dumps(item, ensure_ascii=False))


@tools_app.command("platform")
def tools_platform_command():
    typer.echo(platform_key())


@app.command("gui")
def gui_command():
    from grga.gui import main as gui_main

    gui_main()


def main():
    app()
