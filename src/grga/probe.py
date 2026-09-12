import json
import logging
import math
import re
import subprocess
import time

from pathlib import Path
from typing import Optional, List, Dict, Any

from grga.search import (
    _compile_search_query,
    _count_total_matches,
    _effective_glob_patterns,
    _evaluate_query_ast,
    _find_rg_command,
    _find_rga_command,
    _is_ignorable_search_error,
    _normalize_glob_patterns,
    _parse_rga_event_line,
    _parse_skipped_files,
    _shared_rga_options,
    _kill_process_group,
    _run_checked,
)

_PROBE_NEIGHBORS_LIMIT = 5

_logger = logging.getLogger("grga.probe")


def _log_debug(debug_enabled, message):
    if debug_enabled:
        _logger.debug(message)


def probe_directory(
    directory,
    keywords,
    pattern_mode="auto",
    case_sensitive=False,
    timeout=180,
    multiline=False,
    glob_patterns=None,
    path_only=False,
    sample=0,
    per_file=False,
    debug=False,
    implicit_operator="and",
    default_exclude=True,
    office_adapters=True,
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
    glob_patterns = _effective_glob_patterns(glob_patterns, default_exclude=default_exclude)

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
        office_adapters=office_adapters,
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


def _try_resolve_scope_for_directory(directory: str, db_path) -> Optional[int]:
    """Return the scope_id that covers *directory*, or ``None`` on any failure.

    Silently swallows every error (missing DB, no schema, malformed rows).
    The probe hot path must never break because the graph is unavailable.
    """
    try:
        from grga.graph.schema import connect, ensure_schema
        from grga.graph.ids import IdInterner
        conn = connect(db_path)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            return interner.resolve_scope_for_directory(directory)
        finally:
            conn.close()
    except Exception:
        return None


def _lookup_neighbors(seed: str, scope_id: int, db_path) -> Optional[List[Dict[str, Any]]]:
    """Return top-N ``{kw, npmi}`` neighbours for *seed* in *scope_id*.

    Returns ``None`` on any failure (silent degradation).  Returns ``[]``
    when the seed is known but has no qualifying neighbours — the caller
    can distinguish "no graph available" (``None``) from "no neighbours"
    (empty list).
    """
    try:
        from grga.graph.schema import connect, ensure_schema
        from grga.graph.ids import IdInterner
        from grga.related import _related_keywords_impl

        conn = connect(db_path)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            scope_name = interner.scope_name(scope_id)
            # related_keywords normalises + folds seeds, but we hand in a
            # single pre-folded seed and use the impl directly with a
            # single scope to keep it cheap.  We still go through the
            # public code path for correctness — just short-circuit the
            # scope-name resolution.
            from grga.related import _resolve_seed_args
            seed_folded = _resolve_seed_args(conn, [seed])
            if not seed_folded:
                # Seed not in vocabulary → empty list (key present, empty).
                return []
            from grga.related import _default_min_npmi
            result = _related_keywords_impl(
                conn,
                seed_folded,
                limit_per_seed=_PROBE_NEIGHBORS_LIMIT,
                min_npmi=_default_min_npmi(),
                scope_names=[scope_name],
                tag_names=None,
                tag_logic="or",
            )
            return [
                {"kw": r.kw, "npmi": round(float(r.npmi), 6)}
                for r in result.results
            ]
        finally:
            conn.close()
    except Exception:
        return None


def _lookup_complementary(seed: str, scope_id: int, db_path) -> Optional[List[Dict[str, Any]]]:
    """Return top-N complementary pairs for *seed* in *scope_id*.

    Shape of each item: ``{kw, cooccur_count, c_over_e, p_value}``.
    Sorted by ``c_over_e`` ascending (most complementary first).

    Returns ``None`` on any failure (silent degradation).  Returns ``[]``
    when the seed is known but has no qualifying complementary pairs.
    """
    try:
        from grga.graph.schema import connect, ensure_schema
        from grga.graph.ids import IdInterner
        from grga.related import _related_keywords_impl

        conn = connect(db_path)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            scope_name = interner.scope_name(scope_id)
            from grga.related import _resolve_seed_args
            seed_folded = _resolve_seed_args(conn, [seed])
            if not seed_folded:
                return []
            from grga.related import _default_min_npmi
            result = _related_keywords_impl(
                conn,
                seed_folded,
                limit_per_seed=_PROBE_NEIGHBORS_LIMIT,
                min_npmi=_default_min_npmi(),
                scope_names=[scope_name],
                tag_names=None,
                tag_logic="or",
            )
            return [
                {
                    "kw": c.kw,
                    "cooccur_count": float(c.cooccur_count),
                    "c_over_e": round(float(c.c_over_e), 6),
                    "p_value": float(c.p_value),
                    "via_seed": c.via_seed,
                }
                for c in result.complementary
            ]
        finally:
            conn.close()
    except Exception:
        return None


def _first_seed_from_query(query: dict) -> Optional[str]:
    """Extract the first user-supplied keyword from a compiled query.

    The query's ``terms`` are AST nodes (``{"kind": "term", "value": ...,
    "pattern": ...}``); we want the raw ``value`` as the graph seed, not
    the compiled regex pattern.  Returns ``None`` when there are no terms
    (which should never happen for a probe that reaches this point, but
    guarded anyway).
    """
    terms = query.get("terms") or []
    if not terms:
        return None
    first = terms[0]
    if isinstance(first, dict):
        return first.get("value")
    # Defensive: if terms is ever a list of strings, just use it.
    return str(first) if first else None


def _attach_neighbors(base: dict, target: Path, query: dict, debug: bool) -> dict:
    """Attach the ``neighbors`` field to *base* in-place, best-effort.

    * Only for content probes (caller decides — this helper does not check
      ``path_only``).
    * Uses the **first** seed only (probe is a focused probe; multi-seed
      neighbours belong to ``related``).
    * Any error → neighbours key is simply not added.  Never raises.
      The entire function is wrapped in try/except as a defence-in-depth
      guard so the probe hot path can never be broken by graph issues.
    """
    try:
        from grga.deep_search import DEFAULT_DB_PATH
        first_seed = _first_seed_from_query(query)
        if not first_seed:
            return base
        directory_str = str(target)

        scope_id = _try_resolve_scope_for_directory(directory_str, DEFAULT_DB_PATH)
        if scope_id is None:
            return base

        neighbors = _lookup_neighbors(first_seed, scope_id, DEFAULT_DB_PATH)
        if neighbors is None:
            return base
        base["neighbors"] = neighbors
        return base
    except Exception:
        return base


def _attach_complementary(base: dict, target: Path, query: dict, debug: bool) -> dict:
    """Attach the ``complementary`` field to *base* in-place, best-effort.

    Same lifecycle and degradation guarantees as :func:`_attach_neighbors`.
    Uses the **first** seed only — probe is a focused probe; multi-seed
    complementary pairs belong to ``related``.

    plan-2026-09-11-complementary-pairs.
    """
    try:
        from grga.deep_search import DEFAULT_DB_PATH
        first_seed = _first_seed_from_query(query)
        if not first_seed:
            return base
        directory_str = str(target)

        scope_id = _try_resolve_scope_for_directory(directory_str, DEFAULT_DB_PATH)
        if scope_id is None:
            return base

        comp = _lookup_complementary(first_seed, scope_id, DEFAULT_DB_PATH)
        if comp is None:
            return base
        base["complementary"] = comp
        return base
    except Exception:
        return base


def _probe_content(base, target, query, case_sensitive, multiline, glob_patterns, deadline, sample, per_file, debug, started, office_adapters=True):
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
        office_adapters=office_adapters,
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
            office_adapters=office_adapters,
        )
        if not precount.get("ok"):
            return _error("rga probe failed or timed out", base, started)
        base["total_lines"] = precount["total"]
        base["file_count"] = None
        base["warning"] = per_file_counts.get("warning")
        base["skipped_files"] = per_file_counts.get("skipped_files", [])
        _attach_neighbors(base, target, query, debug)
        _attach_complementary(base, target, query, debug)
        return json.dumps(base, ensure_ascii=False)

    files = per_file_counts["files"]
    total_lines = sum(item["matches"] for item in files)
    base["total_lines"] = total_lines
    base["file_count"] = len(files)
    base["warning"] = per_file_counts.get("warning")
    base["skipped_files"] = per_file_counts.get("skipped_files", [])
    if per_file:
        base["per_file"] = sorted(files, key=lambda item: item["matches"], reverse=True)

    if total_lines > 0:
        top_matches = max((item["matches"] for item in files), default=0)
        if top_matches / total_lines > 0.9:
            existing = base.get("warning")
            note = "single-file dominance"
            base["warning"] = f"{existing}; {note}" if existing else note

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
                office_adapters=office_adapters,
            )
            if sample_info is not None:
                base["sample"] = sample_info

    _attach_neighbors(base, target, query, debug)
    _attach_complementary(base, target, query, debug)
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
        completed = _run_checked(command, cwd=str(target), timeout=timeout, debug=debug)
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
    return json.dumps(base, ensure_ascii=False)


def _count_per_file(rga_path, target, query, case_sensitive, multiline, glob_patterns, timeout, debug, office_adapters=True):
    """Return per-file match counts via ``rga --count-matches --with-filename``."""
    command = [rga_path, "--count-matches", "--with-filename", "--no-require-git"]
    command.extend(_shared_rga_options(query, case_sensitive=case_sensitive, multiline=multiline, glob_patterns=glob_patterns, office_adapters=office_adapters))
    for pattern in query["patterns"]:
        command.extend(["-e", pattern])
    command.append(".")
    _log_debug(debug, f"逐文件计数命令: {' '.join(command)}")
    try:
        completed = _run_checked(command, cwd=str(target), timeout=timeout, debug=debug)
    except subprocess.TimeoutExpired:
        return {"ok": False}

    stderr_text = completed.stderr or ""
    warning = None
    skipped_files = []
    if completed.returncode not in (0, 1):
        error = stderr_text.strip()
        if error and _is_ignorable_search_error(error):
            skipped_files = _parse_skipped_files(error)
            warning = f"{len(skipped_files)} file(s) skipped due to preprocessing errors"
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


def _sample_match_bytes(rga_path, target, query, case_sensitive, multiline, glob_patterns, timeout, sample, file_count, total_lines, debug, office_adapters=True):
    """Estimate total matched-text size by sampling up to ``sample`` lines."""
    per_file_cap = max(1, math.ceil(sample / max(1, file_count)))
    command = [rga_path, "--json", "--line-number", "--no-require-git", "--max-count", str(per_file_cap)]
    command.extend(_shared_rga_options(query, case_sensitive=case_sensitive, multiline=multiline, glob_patterns=glob_patterns, office_adapters=office_adapters))
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
            start_new_session=True,
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
    """Kill rga AND its children (rga-preproc → pandoc) via process group."""
    _kill_process_group(process)
    try:
        if process.stdout:
            process.stdout.close()
    except (OSError, ValueError):
        pass


def _error(message, base, started):
    payload = dict(base)
    payload["ok"] = False
    payload["error"] = message
    return json.dumps(payload, ensure_ascii=False)
