import fnmatch
import json
import logging
import os
import queue
import re
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path

from grga.toolchain import add_tools_to_path, find_tool

_logger = logging.getLogger("grga.search")


# Maximum compressed blob size (bytes) that rga keeps in its preprocess cache.
# rga's own default is 2 MiB, which means large documents whose extracted text
# compresses beyond that (e.g. 17-28 MB novels) are re-extracted on *every*
# scan — the single biggest slowdown on the 11 GB corpus that triggered this.
# 50 MiB covers the largest observed blob (4.86 MB) with ample headroom.
RGA_CACHE_MAX_BLOB_LEN = 52428800

# File types excluded by default from content searches. Entries are rga
# ``--iglob`` exclusion globs (``!`` prefix, case-insensitive). Two rules:
#   1. Only archives + text-less binaries — formats rga cannot turn into text,
#      so excluding them loses no matches while avoiding recursive archive
#      decompression and binary sniffing on every scan.
#   2. NEVER document formats (docx/pdf/pptx/xlsx/doc/xls/...). Searching work
#      documents is grga's main job; excluding them would drop most matches.
# ``--no-default-exclude`` empties this list (user ``-g`` globs still apply).
DEFAULT_EXCLUDE_GLOBS = (
    # Archives — rga recursively decompresses these on every scan.
    "!*.zip",
    "!*.rar",
    "!*.7z",
    "!*.tar",
    "!*.gz",
    "!*.xz",
    "!*.bz2",
    "!*.tgz",
    "!*.tbz2",
    "!*.txz",
    "!*.zst",
    "!*.lz4",
    "!*.lzma",
    "!*.jar",
    "!*.war",
    "!*.ear",
    # Executables / libraries / bytecode — no extractable text.
    "!*.exe",
    "!*.dll",
    "!*.so",
    "!*.dylib",
    "!*.o",
    "!*.a",
    "!*.lib",
    "!*.class",
    "!*.pyc",
    "!*.whl",
    # Installers / packages / disk images.
    "!*.deb",
    "!*.rpm",
    "!*.appimage",
    "!*.apk",
    "!*.msi",
    "!*.dmg",
    "!*.pkg",
    "!*.iso",
    "!*.img",
    "!*.bin",
    # Media — rga has no adapter for these, so they never produce text
    # matches. Images (png/jpg/...) are intentionally NOT excluded: users
    # search image-heavy corpora by filename via ``--path-only``, and rga
    # skips them anyway, so there is no scan cost to keeping them visible.
    "!*.mp3",
    "!*.mp4",
    "!*.mkv",
    "!*.avi",
    "!*.mov",
    "!*.flac",
    "!*.wav",
    "!*.ogg",
    "!*.webm",
    # Transient / dependency directories — high noise, no corpus value.
    # Hidden dot-dirs (.git/.stversions/.env) are NOT listed here: rga skips
    # hidden files by default, and ``grga index build`` skips them in its
    # Python walk, so adding them would be dead weight. These are the
    # non-hidden transient dirs that both rga search and the build walk WOULD
    # descend into unless explicitly excluded. ``**/x/**`` (rg/fnmatch loose
    # semantics, ``*`` crosses ``/``) covers nested + root; build-side
    # conversion expands root+ nested for fnmatch's stricter ``**``.
    "!**/node_modules/**",
    "!**/__pycache__/**",
    "!**/.venv/**",
    "!**/venv/**",
    "!**/.mypy_cache/**",
    "!**/.pytest_cache/**",
    "!**/.ruff_cache/**",
    "!**/dist/**",
    "!**/build/**",
    "!**/target/**",
    "!**/.grga/**",
    "!**/.stversions/**",
)


def _to_build_globs():
    """Translate rga-style ``DEFAULT_EXCLUDE_GLOBS`` into build-walk globs.

    ``grga index build`` enumerates files with a Python fnmatch matcher
    (``_compile_glob_matcher``), which has two differences from ripgrep's
    glob semantics:

    - ripgrep's leading ``!`` negation is not understood by fnmatch; the
      ``!`` is stripped so ``!*.zip`` becomes ``*.zip`` (a positive
      "exclude" pattern for the build walk's ``exclude_globs``).
    - ripgrep's ``**/x/**`` matches ``x`` at the root (zero path prefix);
      Python fnmatch's ``**`` is equivalent to ``*`` and a leading
      ``**/`` requires a non-empty prefix, so root-level ``node_modules/``
      would be missed. Each directory pattern is therefore expanded into a
      root form (``x/**``) plus a nested form (``**/x/**``).

    Keeps build and search on the *same* exclusion list (single source of
    truth) while bridging the two glob dialects.
    """
    out = []
    for pattern in DEFAULT_EXCLUDE_GLOBS:
        body = pattern[1:] if pattern.startswith("!") else pattern
        if body.startswith("**/"):
            tail = body[3:]
            out.append(body)
            out.append(tail)
        else:
            out.append(body)
    return tuple(out)


def _effective_glob_patterns(glob_patterns, default_exclude=True):
    """Merge the default exclusion globs with the caller's globs.

    Default excludes come first so ripgrep's "last matching glob wins"
    semantics let explicit user globs override them; ``default_exclude=False``
    (``--no-default-exclude``) returns only the caller's globs.
    """
    user_globs = _normalize_glob_patterns(glob_patterns)
    if not default_exclude:
        return user_globs
    merged = list(DEFAULT_EXCLUDE_GLOBS)
    for glob in user_globs:
        if glob not in merged:
            merged.append(glob)
    return merged


# ── process-group-safe subprocess helpers ──────────────────────────────────
#
# rga forks rga-preproc → (pandoc | pdftotext | etc.) for binary files.
# When a subprocess timeout fires, killing only the rga process leaves
# those child preprocessors orphaned.  They keep the stdout pipe open,
# which makes the *next* communicate()/wait() call block indefinitely —
# the "hang with no output" symptom reported by users.
#
# Fix: start each child in its own session (process group) and, on timeout,
# killpg the *entire* group so rga-preproc/pandoc die together with rga.


def _kill_process_group(process):
    """Send SIGKILL to the entire process group of *process*.

    Safe to call after ``process.kill()`` — if the process is already dead
    ``os.getpgid`` raises ProcessLookupError which we swallow.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def _run_checked(command, cwd=None, timeout=None, debug=False):
    """Drop-in replacement for ``subprocess.run`` with process-group timeout.

    Behaves identically to::

        subprocess.run(command, cwd=cwd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, check=False)

    but starts the process with ``start_new_session=True`` and, on
    ``TimeoutExpired``, kills the **entire process group** (rga + rga-preproc
    + pandoc) instead of only rga.  The original ``TimeoutExpired`` is
    re-raised so existing ``except subprocess.TimeoutExpired`` handlers
    continue to work unchanged.
    """
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError:
        raise

    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        raise subprocess.TimeoutExpired(
            cmd=command,
            timeout=timeout,
            output=stdout,
            stderr=stderr,
        )

    return subprocess.CompletedProcess(
        args=command,
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _log_debug(debug_enabled, message):
    if debug_enabled:
        _logger.debug(message)


def configure_logging(debug=False, log_dir=None):
    """Configure logging for grga. When debug is enabled, logs go to both console and file."""
    if not debug:
        return
    log_dir = Path(log_dir) if log_dir else Path.home() / ".config" / "grga"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "debug.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Suppress verbose logs from low-level libraries
    for noisy_logger in ["httpcore", "httpx", "openai", "urllib3", "requests"]:
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    formatter = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    # File handler
    file_handler = logging.FileHandler(str(log_file), mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    _logger.debug(f"Debug logging enabled, output: {log_file}")


PatternMode = str


def _find_rga_command():
    add_tools_to_path()
    return find_tool("rga")


def _find_rg_command():
    add_tools_to_path()
    return find_tool("rg")


def check_environment():
    add_tools_to_path()
    rg = find_tool("rg")
    rga = find_tool("rga")
    pandoc = find_tool("pandoc")

    errors = []
    warnings = []

    if rga is None:
        errors.append("未找到 rga 命令，请先安装 ripgrep_all")
    if rg is None:
        errors.append("未找到 rg 命令，请先安装 ripgrep")
    # ``rga-preproc`` is ripgrep-all's internal adapter runner. grga never
    # invokes it directly (search/expand/graph all go through ``rga``), so
    # its presence is not a meaningful signal — rga dispatches adapters
    # itself. We only warn about ``pandoc`` because the pandoc adapter is
    # a separate binary rga shells out to for epub/docx/odt; without it
    # those formats fail to preprocess while PDFs (poppler) still work.
    if pandoc is None:
        warnings.append("未找到 pandoc，将不支持 epub/docx/odt 等文档")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "paths": {
            "rg": rg,
            "rga": rga,
            "pandoc": pandoc,
        },
    }


def search_directory(
    directory,
    keywords,
    pattern_mode="auto",
    case_sensitive=False,
    max_count=None,
    timeout=600,
    before_context=3,
    after_context=3,
    multiline=False,
    glob_patterns=None,
    glob_exclude=False,
    path_only=False,
    debug=False,
    max_total=None,
    strict_cap=False,
    implicit_operator="and",
    default_exclude=True,
    office_adapters=True,
):
    target = Path(directory).expanduser().resolve()
    _log_debug(debug, f"搜索目录: {target}")
    if not target.exists():
        _log_debug(debug, f"错误: 目录不存在: {target}")
        return json.dumps(
            {"ok": False, "error": f"directory does not exist: {target}", "matches": []},
            ensure_ascii=False,
        )
    if not target.is_dir():
        _log_debug(debug, f"错误: 路径不是目录: {target}")
        return json.dumps(
            {"ok": False, "error": f"path is not a directory: {target}", "matches": []},
            ensure_ascii=False,
        )

    glob_patterns = _effective_glob_patterns(glob_patterns, default_exclude=default_exclude)

    query = _compile_search_query(keywords, pattern_mode, debug=debug, implicit_operator=implicit_operator)
    patterns = query["patterns"]
    if not patterns:
        _log_debug(debug, "错误: 关键词为空")
        return json.dumps(
            {"ok": False, "error": "keywords cannot be empty", "matches": []},
            ensure_ascii=False,
        )
    _log_debug(debug, f"编译后的查询模式: {patterns}")
    _log_debug(debug, f"解析后的查询词: {query.get('terms', [])}")

    if path_only:
        _log_debug(debug, "模式: 仅搜索路径")
        rg_path = _find_rg_command()
        _log_debug(debug, f"rg 路径: {rg_path}")
        if rg_path is None:
            _log_debug(debug, "错误: 未找到 rg 命令")
            return json.dumps(
                {"ok": False, "error": "rg command not found", "matches": []},
                ensure_ascii=False,
            )
        path_result = _search_relative_paths(
            rg_path,
            target,
            query,
            case_sensitive=case_sensitive,
            max_count=max_count,
            glob_patterns=glob_patterns,
            timeout=timeout,
            debug=debug,
            max_total=max_total,
        )
        if not path_result.get("ok"):
            _log_debug(debug, f"路径搜索失败: {path_result.get('error')}")
            return json.dumps(path_result, ensure_ascii=False)
        _log_debug(debug, f"路径搜索完成，找到 {len(path_result.get('matches', []))} 个匹配")
        response = {
            "ok": True,
            "patterns": patterns,
            "directory": str(target),
            "matches": path_result.get("matches", []),
            "truncated": bool(path_result.get("truncated")),
        }
        if query.get("implicit_notice"):
            response["implicit_notice"] = query["implicit_notice"]
        return json.dumps(response, ensure_ascii=False)

    _log_debug(debug, "模式: 内容搜索")
    rga_path = _find_rga_command()
    _log_debug(debug, f"rga 路径: {rga_path}")
    if rga_path is None:
        _log_debug(debug, "错误: 未找到 rga 命令")
        return json.dumps(
            {"ok": False, "error": "rga command not found", "matches": []},
            ensure_ascii=False,
        )

    if strict_cap and max_total is not None:
        precount = _count_total_matches(
            rga_path,
            target,
            query,
            case_sensitive=case_sensitive,
            multiline=multiline,
            glob_patterns=glob_patterns,
            timeout=timeout,
            debug=debug,
            office_adapters=office_adapters,
        )
        if precount.get("ok") and precount["total"] > max_total:
            _log_debug(debug, f"strict-cap 触发: {precount['total']} > {max_total}")
            return json.dumps(
                {
                    "ok": False,
                    "error": "too_many_matches",
                    "total_hits": precount["total"],
                    "message": (
                        f"Query matched {precount['total']} lines, exceeding cap of {max_total}. "
                        "Narrow the query or raise --max-total."
                    ),
                    "matches": [],
                },
                ensure_ascii=False,
            )

    command = _build_command(
        rga_path,
        query,
        case_sensitive=case_sensitive,
        max_count=max_count,
        before_context=before_context,
        after_context=after_context,
        multiline=multiline,
        glob_patterns=glob_patterns,
        glob_exclude=glob_exclude,
        office_adapters=office_adapters,
    )
    _log_debug(debug, f"执行命令: {' '.join(command)}")

    stream = _stream_rga_search(command, cwd=str(target), timeout=timeout, max_total=max_total, debug=debug)
    if stream.get("timed_out"):
        _log_debug(debug, "错误: 搜索超时")
        return json.dumps(
            {"ok": False, "error": "rga search timed out", "matches": []},
            ensure_ascii=False,
        )

    matches = _apply_query_submatches(stream["matches"], query, case_sensitive)
    truncated = bool(stream.get("truncated"))
    # Descriptive shape counter (design §7.3 / plan-2026-07-11 P2.2):
    # under ``truncated: true`` the caller cannot tell "cap on one giant
    # file" from "cap across many files" — the response looks identical.
    # ``total_files_matched`` closes that gap without evaluating anything:
    # it is a pure count of distinct file paths that produced at least
    # one visible **real** match, cheap to derive from the already parsed
    # events, and safe under §1.5.1 naming (an integer, not a verdict).
    # ``is_context`` rows are excluded so the field name matches intent:
    # context lines only surround real matches and always share their
    # path anyway, but the explicit filter keeps the derivation readable.
    # We stop short of a "files scanned but no hits" total because that
    # would need a full second rga pass under truncation.
    matched_paths = {
        m["path"] for m in matches if m.get("path") and not m.get("is_context")
    }
    total_files_matched = len(matched_paths)
    _log_debug(debug, f"解析后的匹配数量: {len(matches)}，truncated={truncated}")

    returncode = stream.get("returncode", 0)
    stderr_text = stream.get("stderr", "")
    if truncated:
        acceptable_return = returncode in (0, 1) or returncode is None or returncode < 0
    else:
        acceptable_return = returncode in (0, 1)

    if not acceptable_return:
        error = stderr_text.strip() or "rga search failed"
        if _is_ignorable_search_error(error):
            _log_debug(debug, f"可忽略的错误: {error[:200]}")
            skipped = _parse_skipped_files(error)
            _log_debug(debug, f"跳过文件数: {len(skipped)}")
            warning_msg = f"{len(skipped)} file(s) skipped due to preprocessing errors"
            response = {
                "ok": True,
                "patterns": patterns,
                "directory": str(target),
                "warning": warning_msg,
                "skipped_files": skipped,
                "matches": matches,
                "truncated": truncated,
                "total_files_matched": total_files_matched,
            }
            if query.get("implicit_notice"):
                response["implicit_notice"] = query["implicit_notice"]
            return json.dumps(response, ensure_ascii=False)
        _log_debug(debug, f"搜索失败: {error}")
        return json.dumps(
            {
                "ok": False,
                "error": error,
                "matches": [],
            },
            ensure_ascii=False,
        )

    _log_debug(debug, f"搜索成功，共 {len(matches)} 个结果（含上下文），truncated={truncated}")
    response = {
        "ok": True,
        "patterns": patterns,
        "directory": str(target),
        "matches": matches,
        "truncated": truncated,
        "total_files_matched": total_files_matched,
    }
    if query.get("implicit_notice"):
        response["implicit_notice"] = query["implicit_notice"]
    return json.dumps(response, ensure_ascii=False)


def build_rga_command_preview(
    keywords,
    pattern_mode="auto",
    case_sensitive=False,
    max_count=None,
    before_context=3,
    after_context=3,
    multiline=False,
    glob_patterns=None,
    glob_exclude=False,
    path_only=False,
    implicit_operator="and",
    default_exclude=True,
    office_adapters=True,
):
    rga_path = _find_rga_command() or "rga"
    glob_patterns = _effective_glob_patterns(glob_patterns, default_exclude=default_exclude)
    query = _compile_search_query(keywords, pattern_mode, implicit_operator=implicit_operator)
    if not query["patterns"]:
        return ""
    if path_only:
        command = ["python3", "-m", "grga.search", "--path-only"]
        if not case_sensitive:
            command.append("--ignore-case")
        if max_count is not None:
            command.extend(["--max-count", str(max_count)])
        for glob_pattern in _normalize_glob_patterns(glob_patterns):
            command.extend(["--iglob", glob_pattern])
        if query["pcre2"]:
            command.append("-P")
        for pattern in query["patterns"]:
            command.extend(["-e", pattern])
        command.append(".")
        return " ".join(_quote_command_part(part) for part in command)

    command = _build_command(
        rga_path,
        query,
        case_sensitive=case_sensitive,
        max_count=max_count,
        before_context=before_context,
        after_context=after_context,
        multiline=multiline,
        glob_patterns=glob_patterns,
        glob_exclude=glob_exclude,
        office_adapters=office_adapters,
    )
    return " ".join(_quote_command_part(part) for part in command)


def _normalize_query_text(keywords):
    if isinstance(keywords, str):
        return keywords.strip()
    return " || ".join(item.strip() for item in keywords if isinstance(item, str) and item.strip())


def _empty_query():
    return {
        "ast": None,
        "patterns": [],
        "pcre2": False,
        "terms": [],
        "implicit_and": False,
        "implicit_notice": None,
    }


def _compile_search_query(keywords, pattern_mode, debug=False, implicit_operator="and"):
    text = _normalize_query_text(keywords)
    _log_debug(debug, f"原始查询文本: {repr(text)}")
    _log_debug(debug, f"匹配模式: {pattern_mode}")
    _log_debug(debug, f"隐式操作符: {implicit_operator}")
    if not text:
        return _empty_query()
    tokens = _tokenize_query(text)
    _log_debug(debug, f"分词结果: {tokens}")
    ast, implicit_used = _parse_query(tokens, implicit_operator=implicit_operator)
    _log_debug(debug, f"AST 结构: {ast}")
    _log_debug(debug, f"隐式邻接是否触发: {implicit_used}")
    ast = _compile_query_terms(ast, pattern_mode)
    if ast is None:
        return _empty_query()
    has_and = _ast_has_kind(ast, "and")
    _log_debug(debug, f"包含 AND 逻辑: {has_and}")
    pattern = _compile_query_pattern(ast, has_and)
    terms = _query_terms(ast)
    _log_debug(debug, f"查询词项: {terms}")
    implicit_and = bool(implicit_used and implicit_operator == "and")
    notice = None
    if implicit_and:
        joined = " AND ".join(f'"{term["value"]}"' for term in terms)
        notice = f'multi-term query treated as AND: "{text}" → {joined}'
    return {
        "ast": ast,
        "patterns": [pattern],
        "pcre2": has_and,
        "terms": terms,
        "implicit_and": implicit_and,
        "implicit_notice": notice,
    }


def _normalize_glob_patterns(glob_patterns):
    if isinstance(glob_patterns, str):
        try:
            return [item.strip() for item in shlex.split(glob_patterns) if item.strip()]
        except ValueError:
            return [item.strip() for item in glob_patterns.split() if item.strip()]
    if glob_patterns is None:
        return []
    return [item.strip() for item in glob_patterns if isinstance(item, str) and item.strip()]


def _is_ignorable_search_error(error):
    lower = error.lower()
    return "preprocessor command failed" in lower or "error: copying adapter output to stdout" in lower


def _parse_skipped_files(stderr):
    stderr = (stderr or "").strip()
    if not stderr:
        return []

    skipped = []
    seen = set()
    current_path = None
    for raw_line in stderr.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        path = _extract_skipped_path(line)
        if path:
            current_path = path
        reason = _classify_skip_reason(line)
        if reason is None:
            continue
        key = (current_path, reason)
        if key in seen:
            continue
        seen.add(key)
        skipped.append({"path": current_path, "reason": reason})

    if not skipped:
        skipped.append({"path": None, "reason": "部分文件预处理失败"})
    return skipped


def _extract_skipped_path(line):
    for marker in ("rga-preproc:", "for input:", "while running"):
        index = line.find(marker)
        if index != -1:
            candidate = line[index + len(marker):].strip().strip("'\"")
            candidate = candidate.split(": ", 1)[0].strip().strip("'\"")
            if candidate and ("/" in candidate or candidate.startswith("./")):
                return candidate
    match = re.search(r"([\w./\-]+\.[A-Za-z0-9]+)", line)
    if match:
        return match.group(1)
    return None


def _classify_skip_reason(line):
    lower = line.lower()
    if "preprocessor command failed" in lower:
        return "预处理命令失败"
    if "error: copying adapter output to stdout" in lower:
        return "适配器输出复制失败"
    return None


def _search_relative_paths(rg_path, target, query, case_sensitive=False, max_count=None, glob_patterns=None, timeout=600, debug=False, max_total=None):
    flags = 0 if case_sensitive else re.IGNORECASE
    compiled_terms = [re.compile(term["pattern"], flags) for term in query["terms"]]
    _log_debug(debug, f"编译的正则词项: {[term['pattern'] for term in query['terms']]}")

    command = [rg_path, "--files", "--no-require-git"]
    for glob_pattern in _normalize_glob_patterns(glob_patterns):
        command.extend(["--iglob", glob_pattern])
    _log_debug(debug, f"路径搜索命令: {' '.join(command)}")

    try:
        completed = _run_checked(command, cwd=str(target), timeout=timeout, debug=debug)
    except subprocess.TimeoutExpired:
        _log_debug(debug, "错误: rg 文件列表超时")
        return {"ok": False, "error": "rg file listing timed out", "matches": []}

    _log_debug(debug, f"rg 返回码: {completed.returncode}")
    if completed.returncode not in (0, 1):
        error = completed.stderr.strip() or "rg file listing failed"
        _log_debug(debug, f"路径搜索失败: {error}")
        return {"ok": False, "error": error, "matches": []}

    file_count = len(completed.stdout.splitlines())
    _log_debug(debug, f"找到文件总数: {file_count}")

    matches = []
    skipped_count = 0
    truncated = False
    for line in completed.stdout.splitlines():
        relative_path = line.replace("\\", "/")
        if relative_path.startswith("./"):
            relative_path = relative_path[2:]
        if not relative_path:
            continue
        if not _evaluate_query_ast(query["ast"], relative_path, flags):
            skipped_count += 1
            continue
        submatches = _path_submatches(relative_path, compiled_terms)
        matches.append(
            {
                "path": relative_path,
                "line_number": 1,
                "absolute_offset": None,
                "text": relative_path,
                "is_context": False,
                "submatches": submatches,
            }
        )
        if max_count is not None and len(matches) >= max_count:
            break
        if max_total is not None and len(matches) >= max_total:
            truncated = True
            break

    _log_debug(debug, f"路径过滤后匹配: {len(matches)} 个，跳过: {skipped_count} 个，truncated={truncated}")
    return {"ok": True, "matches": matches, "truncated": truncated}


def _tokenize_query(text):
    tokens = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char == "(":
            tokens.append(("lparen", "("))
            index += 1
            continue
        if char == ")":
            tokens.append(("rparen", ")"))
            index += 1
            continue
        if text.startswith("&&", index):
            tokens.append(("and", "&&"))
            index += 2
            continue
        if text.startswith("||", index):
            tokens.append(("or", "||"))
            index += 2
            continue
        if char == '"':
            end = text.find('"', index + 1)
            if end == -1:
                tokens.append(("term", text[index + 1:]))
                index = length
            else:
                tokens.append(("term", text[index + 1:end]))
                index = end + 1
            continue
        start = index
        while index < length:
            current = text[index]
            if current.isspace() or current in "()":
                break
            if text.startswith("&&", index) or text.startswith("||", index):
                break
            index += 1
        tokens.append(("term", text[start:index]))
    return tokens


def _parse_query(tokens, implicit_operator="and"):
    if implicit_operator not in ("and", "or"):
        raise ValueError("implicit_operator must be one of: and, or")
    parser = {"tokens": tokens, "pos": 0, "implicit_operator": implicit_operator, "implicit_used": False}
    ast = _parse_or(parser)
    return ast, parser["implicit_used"]


def _peek_token(parser):
    if parser["pos"] < len(parser["tokens"]):
        return parser["tokens"][parser["pos"]]
    return (None, None)


def _next_token(parser):
    token = _peek_token(parser)
    parser["pos"] += 1
    return token


def _parse_or(parser):
    nodes = [_parse_and(parser)]
    while True:
        kind, _ = _peek_token(parser)
        if kind == "or":
            _next_token(parser)
            nodes.append(_parse_and(parser))
        elif kind in ("term", "lparen") and parser["implicit_operator"] == "or":
            parser["implicit_used"] = True
            nodes.append(_parse_and(parser))
        else:
            break
    nodes = [node for node in nodes if node is not None]
    if not nodes:
        return None
    if len(nodes) == 1:
        return nodes[0]
    return {"kind": "or", "children": nodes}


def _parse_and(parser):
    nodes = [_parse_atom(parser)]
    while True:
        kind, _ = _peek_token(parser)
        if kind == "and":
            _next_token(parser)
            nodes.append(_parse_atom(parser))
        elif kind in ("term", "lparen") and parser["implicit_operator"] == "and":
            parser["implicit_used"] = True
            nodes.append(_parse_atom(parser))
        else:
            break
    nodes = [node for node in nodes if node is not None]
    if not nodes:
        return None
    if len(nodes) == 1:
        return nodes[0]
    return {"kind": "and", "children": nodes}


def _parse_atom(parser):
    kind, value = _peek_token(parser)
    if kind == "lparen":
        _next_token(parser)
        node = _parse_or(parser)
        if _peek_token(parser)[0] == "rparen":
            _next_token(parser)
        return node
    if kind == "term":
        _next_token(parser)
        if value == "":
            return None
        return {"kind": "term", "value": value}
    if kind == "rparen":
        return None
    _next_token(parser)
    return None


def _compile_query_terms(ast, pattern_mode):
    if ast is None:
        return None
    if ast["kind"] == "term":
        ast["pattern"] = _term_to_regex(ast["value"], pattern_mode)
        return ast
    ast["children"] = [child for child in (_compile_query_terms(c, pattern_mode) for c in ast["children"]) if child is not None]
    if not ast["children"]:
        return None
    if len(ast["children"]) == 1:
        return ast["children"][0]
    return ast


def _term_to_regex(keyword, pattern_mode):
    if pattern_mode not in ("auto", "regex", "glob"):
        raise ValueError("pattern_mode must be one of: auto, regex, glob")
    mode = _detect_pattern_mode(keyword) if pattern_mode == "auto" else pattern_mode
    if mode == "glob":
        translated = fnmatch.translate(keyword)
        if translated.startswith("(?s:") and translated.endswith(")\\Z"):
            translated = translated[4:-3]
        return translated
    re.compile(keyword)
    return keyword


def _ast_has_kind(ast, kind):
    if ast is None:
        return False
    if ast["kind"] == kind:
        return True
    if ast["kind"] == "term":
        return False
    return any(_ast_has_kind(child, kind) for child in ast["children"])


def _query_terms(ast):
    if ast is None:
        return []
    if ast["kind"] == "term":
        return [ast]
    terms = []
    for child in ast["children"]:
        terms.extend(_query_terms(child))
    return terms


def _compile_query_pattern(ast, has_and):
    if ast is None:
        return ""
    if not has_and:
        return _compile_or_pattern(ast)
    return _compile_lookahead_pattern(ast)


def _compile_or_pattern(ast):
    if ast["kind"] == "term":
        return ast["pattern"]
    return "|".join(f"(?:{_compile_or_pattern(child)})" for child in ast["children"])


def _compile_lookahead_pattern(ast):
    return _compile_predicate(ast)


def _compile_predicate(ast):
    if ast["kind"] == "term":
        return f"(?=.*(?:{ast['pattern']}))"
    if ast["kind"] == "and":
        return "".join(_compile_predicate(child) for child in ast["children"])
    return "(?:" + "|".join(f"(?:{_compile_predicate(child)})" for child in ast["children"]) + ")"


def _evaluate_query_ast(ast, text, flags):
    if ast is None:
        return False
    if ast["kind"] == "term":
        return re.search(ast["pattern"], text, flags) is not None
    if ast["kind"] == "and":
        return all(_evaluate_query_ast(child, text, flags) for child in ast["children"])
    return any(_evaluate_query_ast(child, text, flags) for child in ast["children"])


def _apply_query_submatches(matches, query, case_sensitive):
    if not query["pcre2"]:
        return matches
    flags = 0 if case_sensitive else re.IGNORECASE
    compiled_terms = [re.compile(term["pattern"], flags) for term in query["terms"]]
    for match in matches:
        if match.get("is_context"):
            continue
        match["submatches"] = _path_submatches(match.get("text", ""), compiled_terms)
    return matches


def _path_submatches(path, compiled_patterns):
    submatches = []
    for pattern in compiled_patterns:
        for match in pattern.finditer(path):
            start, end = match.span()
            if end <= start:
                continue
            submatches.append({"match": match.group(0), "start": start, "end": end})
    return submatches


def _build_command(rga_path, query, case_sensitive=False, max_count=None, before_context=3, after_context=3, multiline=False, glob_patterns=None, glob_exclude=False, office_adapters=True):
    command = [rga_path, "--json", "--line-number", "--column", "--no-require-git"]
    command.extend(_shared_rga_options(query, case_sensitive=case_sensitive, multiline=multiline, glob_patterns=glob_patterns, office_adapters=office_adapters))
    if max_count is not None:
        command.extend(["--max-count", str(max_count)])
    if before_context:
        command.extend(["--before-context", str(before_context)])
    if after_context:
        command.extend(["--after-context", str(after_context)])
    for pattern in query["patterns"]:
        command.extend(["-e", pattern])
    command.append(".")
    return command


def _shared_rga_options(query, case_sensitive=False, multiline=False, glob_patterns=None, office_adapters=True):
    """Return the subset of rga options that every rga-based command shares.

    Callers append their own leading subcommand flags (``--json``,
    ``--count-matches``, ...) and are responsible for adding ``-e <pattern>``
    entries and the trailing search path.

    ``office_adapters=False`` excludes the ``pandoc_office`` custom adapter
    (registered in rga's user config by :func:`ensure_rga_office_config`),
    restoring the old "xlsx/pptx/doc/xls/ppt invisible" behaviour for that
    single invocation. The exclusion is passed per-invocation via rga's
    ``--rga-adapters=-<name>`` so the default path stays zero-extra-args.
    """
    options = [f"--rga-cache-max-blob-len={RGA_CACHE_MAX_BLOB_LEN}"]
    if not office_adapters:
        options.append("--rga-adapters=-pandoc_office")
    if query["pcre2"]:
        options.append("-P")
    if multiline:
        options.extend(["--multiline", "--multiline-dotall"])
    if not case_sensitive:
        options.append("--ignore-case")
    for glob_pattern in _normalize_glob_patterns(glob_patterns):
        options.extend(["--iglob", glob_pattern])
    return options


def _quote_command_part(part):
    return shlex.quote(str(part))


def _detect_pattern_mode(keyword):
    has_glob = any(char in keyword for char in "*?[")
    has_regex = any(char in keyword for char in "(){}+|^$\\")
    return "glob" if has_glob and not has_regex else "regex"


def _parse_rga_json(output):
    matches = []
    for line in output.splitlines():
        match = _parse_rga_event_line(line)
        if match is not None:
            matches.append(match)
    return matches


def _parse_rga_event_line(line):
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    event_type = event.get("type")
    if event_type not in ("match", "context"):
        return None
    data = event.get("data", {})
    path = data.get("path", {}).get("text", "")
    lines = data.get("lines", {}).get("text", "")
    text = lines.rstrip("\n")
    submatches = data.get("submatches", [])
    return {
        "path": path,
        "line_number": data.get("line_number"),
        "absolute_offset": data.get("absolute_offset"),
        "text": text,
        "is_context": event_type == "context",
        "submatches": [_normalize_submatch(item, text) for item in submatches],
    }


def _stream_rga_search(command, cwd, timeout, max_total=None, debug=False):
    """Run rga and stream its JSON output line by line.

    Stops reading and kills the process once ``max_total`` real (non-context)
    matches are collected, so metadata is only ever computed on the truncated
    slice. Returns a dict with ``matches``, ``truncated``, ``returncode``,
    ``stderr`` and ``timed_out``.
    """
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        return {"matches": [], "truncated": False, "returncode": 1, "stderr": str(exc), "timed_out": False}

    stderr_chunks = []

    def _drain_stderr():
        try:
            for chunk in process.stderr:
                stderr_chunks.append(chunk)
        except (ValueError, OSError):
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    line_queue: "queue.Queue[str]" = queue.Queue()
    reader_done = threading.Event()

    def _read_stdout():
        try:
            for line in process.stdout:
                line_queue.put(line)
        except (ValueError, OSError):
            pass
        finally:
            reader_done.set()

    reader_thread = threading.Thread(target=_read_stdout, daemon=True)
    reader_thread.start()

    matches = []
    real_hits = 0
    truncated = False
    timed_out = False
    deadline = time.monotonic() + timeout if timeout else None

    def _consume(line):
        nonlocal real_hits, truncated
        match = _parse_rga_event_line(line)
        if match is None:
            return False
        matches.append(match)
        if not match["is_context"]:
            real_hits += 1
            if max_total is not None and real_hits >= max_total:
                truncated = True
                return True
        return False

    while not truncated and not timed_out:
        remaining = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
        try:
            line = line_queue.get(timeout=min(remaining, 0.2) if remaining is not None else 0.2)
        except queue.Empty:
            if reader_done.is_set() and line_queue.empty():
                break
            continue
        if _consume(line):
            break

    # Final drain: once the reader has finished, process any lines that were
    # enqueued between our last poll and reader_done being observed, so the
    # last rga event is never dropped.
    if not truncated and not timed_out and reader_done.is_set():
        while True:
            try:
                line = line_queue.get_nowait()
            except queue.Empty:
                break
            if _consume(line):
                break

    if timed_out or truncated:
        _kill_process(process)

    returncode = process.poll()
    if returncode is None:
        try:
            returncode = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_process(process)
            returncode = process.poll()
    stderr_thread.join(timeout=1)
    stderr_text = "".join(stderr_chunks)
    if debug and stderr_text.strip():
        _log_debug(debug, f"stderr: {stderr_text.strip()[:500]}")
    return {
        "matches": matches,
        "truncated": truncated,
        "returncode": returncode,
        "stderr": stderr_text,
        "timed_out": timed_out,
    }


def _kill_process(process):
    """Kill rga AND its children (rga-preproc → pandoc) via process group."""
    _kill_process_group(process)
    try:
        if process.stdout:
            process.stdout.close()
    except (OSError, ValueError):
        pass
    try:
        if process.stderr:
            process.stderr.close()
    except (OSError, ValueError):
        pass


def _count_total_matches(rga_path, target, query, case_sensitive=False, multiline=False, glob_patterns=None, timeout=600, debug=False, office_adapters=True):
    """Estimate the total number of matching lines via ``rga --count-matches``."""
    command = [rga_path, "--count-matches", "--no-require-git", "--no-filename"]
    command.extend(_shared_rga_options(query, case_sensitive=case_sensitive, multiline=multiline, glob_patterns=glob_patterns, office_adapters=office_adapters))
    for pattern in query["patterns"]:
        command.extend(["-e", pattern])
    command.append(".")
    _log_debug(debug, f"预计数命令: {' '.join(command)}")
    try:
        completed = _run_checked(command, cwd=str(target), timeout=timeout, debug=debug)
    except subprocess.TimeoutExpired:
        return {"ok": False, "total": 0}
    if completed.returncode not in (0, 1):
        return {"ok": False, "total": 0}
    total = 0
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            total += int(line)
        except ValueError:
            continue
    return {"ok": True, "total": total}



def _normalize_submatch(item, text):
    start = item.get("start")
    end = item.get("end")
    if isinstance(start, int) and isinstance(end, int):
        start = _byte_offset_to_char_offset(text, start)
        end = _byte_offset_to_char_offset(text, end)
    return {
        "match": item.get("match", {}).get("text", ""),
        "start": start,
        "end": end,
    }


def _byte_offset_to_char_offset(text, byte_offset):
    if byte_offset <= 0:
        return 0
    encoded = text.encode("utf-8")
    if byte_offset >= len(encoded):
        return len(text)
    return len(encoded[:byte_offset].decode("utf-8", errors="ignore"))
