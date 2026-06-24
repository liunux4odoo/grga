import json
from pathlib import Path
from typing import List, Optional

import typer

from grga.search import build_rga_command_preview, check_environment, search_directory
from grga.toolchain import check_tools, current_tools_dir, install_tools, platform_key, supported_tool_names, tool_specs, tools_root, update_tools

app = typer.Typer(no_args_is_help=True, help="Search files with ripgrep-all helpers.")
tools_app = typer.Typer(no_args_is_help=True, help="Manage bundled command line tools.")
app.add_typer(tools_app, name="tools")


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
):
    query = _join_keywords(keywords)
    if not query:
        typer.echo("keywords cannot be empty", err=True)
        raise typer.Exit(2)

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
