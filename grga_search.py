import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

PatternMode = str


def _find_rga_command():
    rga_path = shutil.which("rga")
    return rga_path


def check_environment():
    rg = shutil.which("rg")
    rga = shutil.which("rga")
    rga_preproc = shutil.which("rga-preproc")
    pandoc = shutil.which("pandoc")

    errors = []
    warnings = []

    if rga is None:
        errors.append("未找到 rga 命令，请先安装 ripgrep_all")
    if rg is None:
        errors.append("未找到 rg 命令，请先安装 ripgrep")
    if rga_preproc is None:
        warnings.append("未找到 rga-preproc，将不支持 office 等非文本文档")
    if pandoc is None:
        warnings.append("未找到 pandoc，将不支持 office 等非文本文档")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "paths": {
            "rg": rg,
            "rga": rga,
            "rga-preproc": rga_preproc,
            "pandoc": pandoc,
        },
    }


def search_directory(
    directory,
    keywords,
    pattern_mode="auto",
    case_sensitive=True,
    max_count=None,
    timeout=600,
    before_context=5,
    after_context=5,
    multiline=False,
    glob_patterns=None,
    glob_exclude=False,
    path_only=False,
):
    rga_path = _find_rga_command()

    target = Path(directory).expanduser().resolve()
    if not os.path.exists(str(target)):
        return json.dumps(
            {"ok": False, "error": f"directory does not exist: {target}", "matches": []},
            ensure_ascii=False,
        )
    if not os.path.isdir(str(target)):
        return json.dumps(
            {"ok": False, "error": f"path is not a directory: {target}", "matches": []},
            ensure_ascii=False,
        )

    query = _compile_search_query(keywords, pattern_mode)
    patterns = query["patterns"]
    if not patterns:
        return json.dumps(
            {"ok": False, "error": "keywords cannot be empty", "matches": []},
            ensure_ascii=False,
        )

    if path_only:
        matches = _search_relative_paths(
            target,
            query,
            case_sensitive=case_sensitive,
            max_count=max_count,
            glob_patterns=glob_patterns,
            glob_exclude=glob_exclude,
        )
        return json.dumps(
            {
                "ok": True,
                "patterns": patterns,
                "directory": str(target),
                "matches": matches,
            },
            ensure_ascii=False,
        )

    if rga_path is None:
        return json.dumps(
            {"ok": False, "error": "rga command not found", "matches": []},
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
    )

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
        return json.dumps(
            {"ok": False, "error": "rga search timed out", "matches": []},
            ensure_ascii=False,
        )

    matches = _apply_query_submatches(_parse_rga_json(completed.stdout), query, case_sensitive)
    if completed.returncode not in (0, 1):
        error = completed.stderr.strip() or "rga search failed"
        if _is_ignorable_search_error(error):
            return json.dumps(
                {
                    "ok": True,
                    "patterns": patterns,
                    "directory": str(target),
                    "warning": error,
                    "matches": matches,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ok": False,
                "error": error,
                "matches": [],
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "ok": True,
            "patterns": patterns,
            "directory": str(target),
            "matches": matches,
        },
        ensure_ascii=False,
    )


def build_rga_command_preview(
    keywords,
    pattern_mode="auto",
    case_sensitive=True,
    max_count=None,
    before_context=5,
    after_context=5,
    multiline=False,
    glob_patterns=None,
    glob_exclude=False,
    path_only=False,
):
    rga_path = _find_rga_command() or "rga"
    query = _compile_search_query(keywords, pattern_mode)
    if not query["patterns"]:
        return ""
    if path_only:
        command = ["python3", "grga_search.py", "--path-only"]
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
    )
    return " ".join(_quote_command_part(part) for part in command)


def _normalize_query_text(keywords):
    if isinstance(keywords, str):
        return keywords.strip()
    return " || ".join(item.strip() for item in keywords if isinstance(item, str) and item.strip())


def _compile_search_query(keywords, pattern_mode):
    text = _normalize_query_text(keywords)
    if not text:
        return {"ast": None, "patterns": [], "pcre2": False, "terms": []}
    ast = _parse_query(_tokenize_query(text))
    ast = _compile_query_terms(ast, pattern_mode)
    if ast is None:
        return {"ast": None, "patterns": [], "pcre2": False, "terms": []}
    has_and = _ast_has_kind(ast, "and")
    pattern = _compile_query_pattern(ast, has_and)
    return {"ast": ast, "patterns": [pattern], "pcre2": has_and, "terms": _query_terms(ast)}


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


def _search_relative_paths(target, query, case_sensitive=True, max_count=None, glob_patterns=None, glob_exclude=False):
    matches = []
    flags = 0 if case_sensitive else re.IGNORECASE
    compiled_terms = [re.compile(term["pattern"], flags) for term in query["terms"]]
    normalized_globs = _normalize_glob_patterns(glob_patterns)
    ignore_patterns = _load_gitignore_patterns(target)
    for root, dirs, files in os.walk(target):
        dirs.sort()
        files.sort()
        for name in files:
            path = Path(root) / name
            relative_path = path.relative_to(target).as_posix()
            if not _path_allowed_by_gitignore(relative_path, ignore_patterns):
                continue
            if not _path_allowed_by_globs(relative_path, normalized_globs, glob_exclude):
                continue
            if not _evaluate_query_ast(query["ast"], relative_path, flags):
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
                return matches
    return matches


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


def _parse_query(tokens):
    parser = {"tokens": tokens, "pos": 0}
    ast = _parse_or(parser)
    return ast


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
        elif kind in ("term", "lparen"):
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
    while _peek_token(parser)[0] == "and":
        _next_token(parser)
        nodes.append(_parse_atom(parser))
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


def _path_allowed_by_globs(path, glob_patterns, glob_exclude=False):
    if not glob_patterns:
        return True
    has_include = any(not pattern.startswith("!") for pattern in glob_patterns)
    allowed = not has_include
    for pattern in glob_patterns:
        negated = pattern.startswith("!")
        raw_pattern = pattern[1:] if negated else pattern
        if _path_matches_iglob(path, raw_pattern):
            allowed = not negated
    return allowed


def _path_matches_iglob(path, pattern):
    path = path.casefold()
    pattern = pattern.casefold()
    return fnmatch.fnmatch(path, pattern) or (pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]))


def _load_gitignore_patterns(target):
    gitignore_path = Path(target) / ".gitignore"
    try:
        with open(gitignore_path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    patterns = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped)
    return patterns


def _path_allowed_by_gitignore(path, patterns):
    if not patterns:
        return True
    allowed = True
    for pattern in patterns:
        negated = pattern.startswith("!")
        raw_pattern = pattern[1:] if negated else pattern
        if _path_matches_gitignore(path, raw_pattern):
            allowed = negated
    return allowed


def _path_matches_gitignore(path, pattern):
    pattern = pattern.lstrip("/")
    if pattern.endswith("/"):
        pattern = pattern + "**"
    candidates = [pattern]
    if not pattern.startswith("**/"):
        candidates.append("**/" + pattern)
    for candidate in candidates:
        if fnmatch.fnmatch(path, candidate):
            return True
        if candidate.startswith("**/") and fnmatch.fnmatch(path, candidate[3:]):
            return True
    return False


def _path_submatches(path, compiled_patterns):
    submatches = []
    for pattern in compiled_patterns:
        for match in pattern.finditer(path):
            start, end = match.span()
            if end <= start:
                continue
            submatches.append({"match": match.group(0), "start": start, "end": end})
    return submatches


def _build_command(rga_path, query, case_sensitive=True, max_count=None, before_context=5, after_context=5, multiline=False, glob_patterns=None, glob_exclude=False):
    command = [rga_path, "--json", "--line-number", "--column", "--no-require-git"]
    if query["pcre2"]:
        command.append("-P")
    if multiline:
        command.extend(["--multiline", "--multiline-dotall"])
    if not case_sensitive:
        command.append("--ignore-case")
    if max_count is not None:
        command.extend(["--max-count", str(max_count)])
    if before_context:
        command.extend(["--before-context", str(before_context)])
    if after_context:
        command.extend(["--after-context", str(after_context)])
    for glob_pattern in _normalize_glob_patterns(glob_patterns):
        command.extend(["--iglob", glob_pattern])
    for pattern in query["patterns"]:
        command.extend(["-e", pattern])
    command.append(".")
    return command


def _quote_command_part(part):
    return shlex.quote(str(part))


def _detect_pattern_mode(keyword):
    has_glob = any(char in keyword for char in "*?[")
    has_regex = any(char in keyword for char in "(){}+|^$\\")
    return "glob" if has_glob and not has_regex else "regex"


def _parse_rga_json(output):
    matches = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type not in ("match", "context"):
            continue

        data = event.get("data", {})
        path = data.get("path", {}).get("text", "")
        lines = data.get("lines", {}).get("text", "")
        text = lines.rstrip("\n")
        submatches = data.get("submatches", [])
        matches.append(
            {
                "path": path,
                "line_number": data.get("line_number"),
                "absolute_offset": data.get("absolute_offset"),
                "text": text,
                "is_context": event_type == "context",
                "submatches": [_normalize_submatch(item, text) for item in submatches],
            }
        )
    return matches


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
