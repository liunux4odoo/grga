import json
import logging
import math
import re
import subprocess
import time

from pathlib import Path

from grga.search import (
    _compile_search_query,
    _count_total_matches,
    _evaluate_query_ast,
    _find_rg_command,
    _find_rga_command,
    _is_ignorable_search_error,
    _normalize_glob_patterns,
    _parse_rga_event_line,
    _parse_skipped_files,
    _shared_rga_options,
)

_logger = logging.getLogger("grga.probe")


def _log_debug(debug_enabled, message):
    if debug_enabled:
        _logger.debug(message)


def probe_directory(
    directory,
    keywords,
    pattern_mode="auto",
    case_sensitive=False,
    timeout=60,
    multiline=False,
    glob_patterns=None,
    path_only=False,
    sample=0,
    per_file=False,
    debug=False,
    implicit_operator="and",
):
    """Lightweight, read-only probe for how a query scales on disk.

    Runs ``rga --count-matches`` (or ``rg --files`` for path-only) plus an
    optional sampling pass, and returns structured metadata so a caller can
    decide whether to tighten or loosen the query before executing a full
    search. It never fetches match bodies or context, never rewrites the
    query, and never makes a "too many / too few" judgement.
    """
    started = time.monotonic()
    deadline = started + timeout if timeout else None
    target = Path(directory).expanduser().resolve()
    _log_debug(debug, f"探测目录: {target}")

    query = _compile_search_query(keywords, pattern_mode, debug=debug, implicit_operator=implicit_operator)
    patterns_seed = query.get("patterns") or []
    base = _empty_response(target, patterns_seed, query, implicit_operator)

    if not target.exists():
        return _error("directory does not exist: %s" % target, base, started)
    if not target.is_dir():
        return _error("path is not a directory: %s" % target, base, started)
    if not patterns_seed:
        return _error("keywords cannot be empty", base, started)

    if path_only:
        return _probe_paths(
            base,
            target,
            query,
            case_sensitive=case_sensitive,
            glob_patterns=glob_patterns,
            deadline=deadline,
            per_file=per_file,
            debug=debug,
            started=started,
        )

    return _probe_content(
        base,
        target,
        query,
        case_sensitive=case_sensitive,
        multiline=multiline,
        glob_patterns=glob_patterns,
        deadline=deadline,
        sample=sample,
        per_file=per_file,
        debug=debug,
        started=started,
    )


def _empty_response(target, patterns, query, implicit_operator):
    return {
        "ok": True,
        "directory": str(target),
        "patterns": list(patterns),
        "terms": query.get("terms", []),
        "pcre2": query.get("pcre2", False),
        "implicit_operator": implicit_operator,
        "implicit_notice": query.get("implicit_notice"),
        "total_lines": 0,
        "file_count": 0,
        "truncated": False,
        "warning": None,
        "skipped_files": [],
    }


def _remaining(deadline):
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    return remaining if remaining > 0 else 0


def _probe_content(base, target, query, case_sensitive, multiline, glob_patterns, deadline, sample, per_file, debug, started):
    rga_path = _find_rga_command()
    if rga_path is None:
        return _error("rga command not found", base, started)

    per_file_timeout = _remaining(deadline)
    if per_file_timeout == 0:
        return _error("rga probe timed out", base, started)

    per_file_counts = _count_per_file(
        rga_path,
        target,
        query,
        case_sensitive=case_sensitive,
        multiline=multiline,
        glob_patterns=glob_patterns,
        timeout=per_file_timeout,
        debug=debug,
    )
    if not per_file_counts.get("ok"):
        fallback_timeout = _remaining(deadline)
        if fallback_timeout == 0:
            return _error("rga probe timed out", base, started)
        precount = _count_total_matches(
            rga_path,
            target,
            query,
            case_sensitive=case_sensitive,
            multiline=multiline,
            glob_patterns=glob_patterns,
            timeout=fallback_timeout,
            debug=debug,
        )
        if not precount.get("ok"):
            return _error("rga probe failed or timed out", base, started)
        base["total_lines"] = precount["total"]
        base["file_count"] = None
        base["warning"] = per_file_counts.get("warning")
        base["skipped_files"] = per_file_counts.get("skipped_files", [])
        base["elapsed_ms"] = _elapsed_ms(started)
        return json.dumps(base, ensure_ascii=False)

    files = per_file_counts["files"]
    total_lines = sum(item["matches"] for item in files)
    base["total_lines"] = total_lines
    base["file_count"] = len(files)
    base["warning"] = per_file_counts.get("warning")
    base["skipped_files"] = per_file_counts.get("skipped_files", [])
    if per_file:
        base["per_file"] = sorted(files, key=lambda item: item["matches"], reverse=True)

    if sample and total_lines > 0:
        sample_timeout = _remaining(deadline)
        if sample_timeout == 0:
            base["truncated"] = True
        else:
            sample_info = _sample_match_bytes(
                rga_path,
                target,
                query,
                case_sensitive=case_sensitive,
                multiline=multiline,
                glob_patterns=glob_patterns,
                timeout=sample_timeout,
                sample=sample,
                file_count=len(files),
                total_lines=total_lines,
                debug=debug,
            )
            if sample_info is not None:
                base["sample"] = sample_info

    base["elapsed_ms"] = _elapsed_ms(started)
    return json.dumps(base, ensure_ascii=False)


def _probe_paths(base, target, query, case_sensitive, glob_patterns, deadline, per_file, debug, started):
    rg_path = _find_rg_command()
    if rg_path is None:
        return _error("rg command not found", base, started)

    timeout = _remaining(deadline)
    if timeout == 0:
        return _error("rg file listing timed out", base, started)

    flags = 0 if case_sensitive else re.IGNORECASE
    command = [rg_path, "--files", "--no-require-git"]
    for glob_pattern in _normalize_glob_patterns(glob_patterns):
        command.extend(["--iglob", glob_pattern])
    _log_debug(debug, f"路径探测命令: {' '.join(command)}")

    try:
        completed = subprocess.run(
            command,
            cwd=str(target),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _error("rg file listing timed out", base, started)

    if completed.returncode not in (0, 1):
        error = completed.stderr.strip() or "rg file listing failed"
        return _error(error, base, started)

    matched = []
    for line in completed.stdout.splitlines():
        relative_path = line.replace("\\", "/")
        if relative_path.startswith("./"):
            relative_path = relative_path[2:]
        if not relative_path:
            continue
        if _evaluate_query_ast(query["ast"], relative_path, flags):
            matched.append(relative_path)

    base["total_lines"] = len(matched)
    base["file_count"] = len(matched)
    if per_file:
        base["per_file"] = [{"path": path, "matches": 1} for path in matched]
    base["elapsed_ms"] = _elapsed_ms(started)
    return json.dumps(base, ensure_ascii=False)


def _count_per_file(rga_path, target, query, case_sensitive, multiline, glob_patterns, timeout, debug):
    """Return per-file match counts via ``rga --count-matches --with-filename``."""
    command = [rga_path, "--count-matches", "--with-filename", "--no-require-git"]
    command.extend(_shared_rga_options(query, case_sensitive=case_sensitive, multiline=multiline, glob_patterns=glob_patterns))
    for pattern in query["patterns"]:
        command.extend(["-e", pattern])
    command.append(".")
    _log_debug(debug, f"逐文件计数命令: {' '.join(command)}")
    try:
        completed = subprocess.run(
            command,
            cwd=str(target),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False}

    stderr_text = completed.stderr or ""
    warning = None
    skipped_files = []
    if completed.returncode not in (0, 1):
        error = stderr_text.strip()
        if error and _is_ignorable_search_error(error):
            warning = error
            skipped_files = _parse_skipped_files(error)
        else:
            return {"ok": False, "warning": error or None}

    files = []
    for line in completed.stdout.splitlines():
        path, count = _split_count_line(line)
        if path is None:
            continue
        files.append({"path": path, "matches": count})
    return {"ok": True, "files": files, "warning": warning, "skipped_files": skipped_files}


def _split_count_line(line):
    line = line.rstrip("\n")
    if not line:
        return None, None
    path, sep, count_text = line.rpartition(":")
    if not sep:
        return None, None
    path = path.replace("\\", "/")
    if path.startswith("./"):
        path = path[2:]
    try:
        count = int(count_text.strip())
    except ValueError:
        return None, None
    return path, count


def _sample_match_bytes(rga_path, target, query, case_sensitive, multiline, glob_patterns, timeout, sample, file_count, total_lines, debug):
    """Estimate total matched-text size by sampling up to ``sample`` lines."""
    per_file_cap = max(1, math.ceil(sample / max(1, file_count)))
    command = [rga_path, "--json", "--line-number", "--no-require-git", "--max-count", str(per_file_cap)]
    command.extend(_shared_rga_options(query, case_sensitive=case_sensitive, multiline=multiline, glob_patterns=glob_patterns))
    for pattern in query["patterns"]:
        command.extend(["-e", pattern])
    command.append(".")
    _log_debug(debug, f"抽样命令: {' '.join(command)}")

    try:
        process = subprocess.Popen(
            command,
            cwd=str(target),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError:
        return None

    sampled_lines = 0
    sum_bytes = 0
    deadline = time.monotonic() + timeout if timeout else None
    try:
        for line in process.stdout:
            if deadline is not None and time.monotonic() > deadline:
                break
            event = _parse_rga_event_line(line)
            if event is None or event.get("is_context"):
                continue
            sum_bytes += len(event.get("text", "").encode("utf-8"))
            sampled_lines += 1
            if sampled_lines >= sample:
                break
    finally:
        _kill(process)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    if sampled_lines == 0:
        return None
    avg = round(sum_bytes / sampled_lines)
    return {
        "sampled_lines": sampled_lines,
        "avg_line_bytes": avg,
        "est_total_bytes": avg * total_lines,
    }


def _kill(process):
    try:
        process.kill()
    except (OSError, ValueError):
        pass
    try:
        if process.stdout:
            process.stdout.close()
    except (OSError, ValueError):
        pass


def _elapsed_ms(started):
    return int((time.monotonic() - started) * 1000)


def _error(message, base, started):
    payload = dict(base)
    payload["ok"] = False
    payload["error"] = message
    payload["elapsed_ms"] = _elapsed_ms(started)
    return json.dumps(payload, ensure_ascii=False)
