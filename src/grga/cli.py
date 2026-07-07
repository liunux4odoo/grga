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
index_app = typer.Typer(no_args_is_help=True, help="Build and manage the L2 graph index.")
app.add_typer(index_app, name="index")

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
    max_count: Optional[int] = typer.Option(None, "--max-count", "-n", help="Maximum number of matches."),
    timeout: float = typer.Option(600.0, "--timeout", "-t", help="Search timeout in seconds."),
    before_context: int = typer.Option(3, "--before-context", "-B", help="Lines of leading context."),
    after_context: int = typer.Option(3, "--after-context", "-A", help="Lines of trailing context."),
    multiline: bool = typer.Option(False, "--multiline", help="Enable multiline search."),
    file_globs: Optional[List[str]] = typer.Option(None, "--iglob", "-g", help="Case-insensitive file glob. Can be used multiple times."),
    path_only: bool = typer.Option(False, "--path-only", help="Search relative file paths only."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
    metadata: bool = typer.Option(True, "--metadata/--no-metadata", help="Attach Layer 2 hit metadata (occurrence, ratio, nearby)."),
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

    if metadata:
        from grga.meta import annotate_result_inplace, annotate_structure_inplace

        annotate_result_inplace(result, base_dir=str(directory))
        annotate_structure_inplace(result, base_dir=str(directory))

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
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite keyword library path."),
    decay_factor: Optional[float] = typer.Option(None, "--decay-factor", help="Decay multiplier applied to keyword counts before each run (0-1)."),
    min_keyword_count: Optional[float] = typer.Option(None, "--min-keyword-count", help="Drop keyword records whose count falls below this threshold."),
    global_library: Optional[bool] = typer.Option(None, "--global-library/--directory-library", help="Use the whole keyword library or only the current directory scope."),
    enable_tiered_search: Optional[bool] = typer.Option(None, "--tiered-search/--no-tiered-search", help="Run queries from strict to loose and stop once enough results are found."),
    min_results: Optional[int] = typer.Option(None, "--min-results", help="Minimum unique matches before tiered search stops relaxing."),
    max_tiers: Optional[int] = typer.Option(None, "--max-tiers", help="Maximum number of tiered query expressions to run."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
    metadata: bool = typer.Option(True, "--metadata/--no-metadata", help="Attach Layer 2 hit metadata (occurrence, ratio, nearby)."),
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


@app.command("related")
def related_command(
    keywords: List[str] = typer.Argument(..., help="Seed keywords."),
    mode: str = typer.Option("graph", "--mode", "-m", help="Relation mode: graph | dir | heading."),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum number of related documents."),
    db_path: Optional[Path] = typer.Option(None, "--db-path", "--db", help="SQLite graph library path (defaults to shared deep-search DB)."),
    directory: Optional[Path] = typer.Option(None, "--directory", "-d", resolve_path=True, help="Restrict to a previously indexed scope."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON result."),
):
    """List documents related to the given keywords via the L2 graph."""
    from grga.related import VALID_MODES, related as related_docs

    if mode not in VALID_MODES:
        typer.echo(f"mode must be one of: {', '.join(VALID_MODES)}", err=True)
        raise typer.Exit(2)
    seeds = [item for item in keywords if item.strip()]
    if not seeds:
        typer.echo("keywords cannot be empty", err=True)
        raise typer.Exit(2)

    results = related_docs(
        seeds,
        mode=mode,
        limit=limit,
        db_path=str(db_path) if db_path else None,
        directory=str(directory) if directory else None,
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


def main():
    app()
