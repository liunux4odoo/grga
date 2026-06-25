import itertools
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable

from grga.search import _normalize_glob_patterns

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "grga"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
DEFAULT_DB_PATH = CONFIG_DIR / "deep_keywords.db"
DEFAULT_MODEL_ENV_NAMES = ("GRGA_OPENAI_MODEL", "OPENAI_MODEL")
DEFAULT_API_KEY_ENV_NAMES = ("GRGA_OPENAI_API_KEY", "OPENAI_API_KEY")
DEFAULT_BASE_URL_ENV_NAMES = ("GRGA_OPENAI_BASE_URL", "OPENAI_BASE_URL")
DEFAULT_DECAY_FACTOR = 0.9
DEFAULT_MIN_COUNT = 0.1
DEEP_SEARCH_DEFAULTS = {
    "llm_base_url": None,
    "llm_api_key": None,
    "llm_model_name": None,
    "pattern_mode": None,
    "case_sensitive": None,
    "max_count": None,
    "timeout": None,
    "before_context": None,
    "after_context": None,
    "multiline": None,
    "path_only": None,
    "max_related_keywords": 8,
    "repeat_count": 1,
    "max_extract_segments": 20,
    "db_path": None,
    "decay_factor": DEFAULT_DECAY_FACTOR,
    "min_keyword_count": DEFAULT_MIN_COUNT,
    "global_library": False,
}
SHARED_SEARCH_KEYS = {
    "pattern_mode": "mode",
    "case_sensitive": "case_sensitive",
    "max_count": "max_count",
    "timeout": "timeout",
    "before_context": "before_context",
    "after_context": "after_context",
    "multiline": "multiline",
    "path_only": "path_only",
}
SEARCH_DEFAULTS = {
    "pattern_mode": "auto",
    "case_sensitive": False,
    "max_count": None,
    "timeout": 600.0,
    "before_context": 3,
    "after_context": 3,
    "multiline": False,
    "path_only": False,
}


def load_deep_search_config(settings_path=None):
    path = Path(settings_path).expanduser() if settings_path else SETTINGS_PATH
    data, load_failed = _load_settings_data_safe(path)
    section = data.get("deep_search")
    if not isinstance(section, dict):
        section = {}
    completed = dict(section)
    changed = not isinstance(data.get("deep_search"), dict)
    for key, default in DEEP_SEARCH_DEFAULTS.items():
        if key not in completed:
            completed[key] = default
            changed = True
    if changed and not load_failed:
        data["deep_search"] = completed
        _save_settings_data(path, data)
    return {key: completed[key] for key in DEEP_SEARCH_DEFAULTS}


def _load_settings_data_safe(path):
    file_exists = path.exists() and path.stat().st_size > 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return (data if isinstance(data, dict) else {}), False
    except (OSError, ValueError):
        return {}, file_exists  # 如果读取失败且原文件有内容，标记为 failed 避免覆盖


def _save_settings_data(path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass


def resolve_deep_search_options(cli_options, settings_path=None):
    path = Path(settings_path).expanduser() if settings_path else SETTINGS_PATH
    settings, _ = _load_settings_data_safe(path)
    config = load_deep_search_config(path)
    resolved = {}
    for key, default in DEEP_SEARCH_DEFAULTS.items():
        value = cli_options.get(key)
        if value is None:
            value = config.get(key)
        if value is None and key in SHARED_SEARCH_KEYS:
            value = _coerce_search_value(key, settings.get(SHARED_SEARCH_KEYS[key]))
        if value is None and key in SEARCH_DEFAULTS:
            value = SEARCH_DEFAULTS[key]
        if value is None:
            value = default
        resolved[key] = value
    return resolved


def _coerce_search_value(key, value):
    if value is None:
        return None
    if key in ("case_sensitive", "multiline", "path_only"):
        return bool(value)
    if key == "pattern_mode":
        text = str(value).strip()
        return text or None
    if key in ("max_count", "before_context", "after_context"):
        return _coerce_int(value)
    if key == "timeout":
        return _coerce_float(value)
    return value


def _coerce_int(value):
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _coerce_float(value):
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


class KeywordLibrary:
    def __init__(self, path, decay_factor=DEFAULT_DECAY_FACTOR, min_count=DEFAULT_MIN_COUNT):
        self.path = Path(path).expanduser()
        self.decay_factor = decay_factor
        self.min_count = min_count
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS keyword_counts (
                scope TEXT NOT NULL,
                directory TEXT NOT NULL,
                term TEXT NOT NULL,
                count REAL NOT NULL DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (scope, directory, term)
            );
            CREATE TABLE IF NOT EXISTS keyword_pairs (
                scope TEXT NOT NULL,
                directory TEXT NOT NULL,
                keyword_a TEXT NOT NULL,
                keyword_b TEXT NOT NULL,
                count REAL NOT NULL DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (scope, directory, keyword_a, keyword_b)
            );
            CREATE TABLE IF NOT EXISTS keyword_sources (
                directory TEXT NOT NULL,
                keyword TEXT NOT NULL,
                path TEXT NOT NULL,
                line_number INTEGER NOT NULL DEFAULT 0,
                count REAL NOT NULL DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (directory, keyword, path, line_number)
            );
            CREATE INDEX IF NOT EXISTS idx_pairs_dir ON keyword_pairs (directory, count);
            CREATE INDEX IF NOT EXISTS idx_counts_dir ON keyword_counts (directory, count);
            CREATE INDEX IF NOT EXISTS idx_sources_kw ON keyword_sources (directory, keyword);
            """
        )
        self.conn.commit()

    def decay(self, directory):
        if not (0 < self.decay_factor < 1):
            return
        params = (self.decay_factor, directory)
        self.conn.execute("UPDATE keyword_counts SET count = count * ? WHERE directory = ?", params)
        self.conn.execute("UPDATE keyword_pairs SET count = count * ? WHERE directory = ?", params)
        self.conn.execute("UPDATE keyword_sources SET count = count * ? WHERE directory = ?", params)
        self.conn.execute("DELETE FROM keyword_counts WHERE directory = ? AND count < ?", (directory, self.min_count))
        self.conn.execute("DELETE FROM keyword_pairs WHERE directory = ? AND count < ?", (directory, self.min_count))
        self.conn.execute("DELETE FROM keyword_sources WHERE directory = ? AND count < ?", (directory, self.min_count))
        self.conn.commit()

    def add_keywords(self, scope, directory, keywords):
        terms = _normalize_keywords(keywords)
        if not terms:
            return
        now = _now()
        for term in terms:
            self.conn.execute(
                """
                INSERT INTO keyword_counts (scope, directory, term, count, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(scope, directory, term)
                DO UPDATE SET count = count + 1, updated_at = excluded.updated_at
                """,
                (scope, directory, term, now),
            )
        pair_rows = [
            (scope, directory, left, right, now)
            for left, right in itertools.combinations(sorted(terms, key=lambda item: item.casefold()), 2)
        ]
        if pair_rows:
            self.conn.executemany(
                """
                INSERT INTO keyword_pairs (scope, directory, keyword_a, keyword_b, count, updated_at)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(scope, directory, keyword_a, keyword_b)
                DO UPDATE SET count = count + 1, updated_at = excluded.updated_at
                """,
                pair_rows,
            )

    def add_sources(self, directory, keywords, path, line_number):
        terms = _normalize_keywords(keywords)
        if not terms or not path:
            return
        line = int(line_number) if isinstance(line_number, int) else 0
        now = _now()
        self.conn.executemany(
            """
            INSERT INTO keyword_sources (directory, keyword, path, line_number, count, updated_at)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(directory, keyword, path, line_number)
            DO UPDATE SET count = count + 1, updated_at = excluded.updated_at
            """,
            [(directory, term, path, line, now) for term in terms],
        )

    def commit(self):
        self.conn.commit()

    def history(self, directory, seed_keywords, limit=30, global_library=False):
        seeds = [item for item in _normalize_keywords(seed_keywords)]
        params = []
        where = []
        if not global_library:
            where.append("directory = ?")
            params.append(directory)
        if seeds:
            placeholders = ",".join("?" for _ in seeds)
            lowered = [item.casefold() for item in seeds]
            where.append(f"(LOWER(keyword_a) IN ({placeholders}) OR LOWER(keyword_b) IN ({placeholders}))")
            params.extend(lowered)
            params.extend(lowered)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.conn.execute(
            f"SELECT scope, keyword_a, keyword_b, count FROM keyword_pairs{clause} ORDER BY count DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        pairs = [
            {"source": row["scope"], "keywords": [row["keyword_a"], row["keyword_b"]], "count": round(row["count"], 3)}
            for row in rows
        ]
        if pairs or seeds:
            return pairs
        count_params = [] if global_library else [directory]
        count_clause = "" if global_library else " WHERE directory = ?"
        rows = self.conn.execute(
            f"SELECT scope, term, count FROM keyword_counts{count_clause} ORDER BY count DESC LIMIT ?",
            (*count_params, limit),
        ).fetchall()
        return [{"source": row["scope"], "keywords": [row["term"]], "count": round(row["count"], 3)} for row in rows]

    def close(self):
        self.conn.close()


def deep_search_directory(
    directory,
    query,
    llm_base_url=None,
    llm_api_key=None,
    llm_model_name=None,
    pattern_mode="auto",
    case_sensitive=False,
    max_count=None,
    timeout=600,
    before_context=3,
    after_context=3,
    multiline=False,
    glob_patterns=None,
    path_only=False,
    max_related_keywords=8,
    repeat_count=1,
    max_extract_segments=20,
    db_path=None,
    decay_factor=DEFAULT_DECAY_FACTOR,
    min_keyword_count=DEFAULT_MIN_COUNT,
    global_library=False,
    grga_command=None,
):
    try:
        result = _deep_search_directory(
            directory=directory,
            query=str(query or "").strip(),
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            llm_model_name=llm_model_name,
            pattern_mode=pattern_mode,
            case_sensitive=case_sensitive,
            max_count=max_count,
            timeout=timeout,
            before_context=before_context,
            after_context=after_context,
            multiline=multiline,
            glob_patterns=glob_patterns,
            path_only=path_only,
            max_related_keywords=max_related_keywords,
            repeat_count=repeat_count,
            max_extract_segments=max_extract_segments,
            db_path=db_path,
            decay_factor=decay_factor,
            min_keyword_count=min_keyword_count,
            global_library=global_library,
            grga_command=grga_command,
        )
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "matches": []}
    return json.dumps(result, ensure_ascii=False)


def _deep_search_directory(
    directory,
    query,
    llm_base_url=None,
    llm_api_key=None,
    llm_model_name=None,
    pattern_mode="auto",
    case_sensitive=False,
    max_count=None,
    timeout=600,
    before_context=3,
    after_context=3,
    multiline=False,
    glob_patterns=None,
    path_only=False,
    max_related_keywords=8,
    repeat_count=1,
    max_extract_segments=20,
    db_path=None,
    decay_factor=DEFAULT_DECAY_FACTOR,
    min_keyword_count=DEFAULT_MIN_COUNT,
    global_library=False,
    grga_command=None,
):
    if not query:
        return {"ok": False, "error": "query cannot be empty", "matches": []}
    if max_related_keywords <= 0:
        return {"ok": False, "error": "max_related_keywords must be greater than 0", "matches": []}
    if repeat_count < 0:
        return {"ok": False, "error": "repeat_count cannot be negative", "matches": []}
    if max_extract_segments <= 0:
        return {"ok": False, "error": "max_extract_segments must be greater than 0", "matches": []}

    target = Path(directory).expanduser().resolve()
    directory_key = str(target)
    database_file = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    client, model_name = _make_openai_client(llm_base_url, llm_api_key, llm_model_name)

    library = KeywordLibrary(database_file, decay_factor=decay_factor, min_count=min_keyword_count)
    try:
        library.decay(directory_key)

        initial_plan = _build_query_plan(
            client,
            model_name,
            query,
            library.history(directory_key, [], limit=30, global_library=global_library),
            max_related_keywords,
        )
        current_expression = _clean_expression(initial_plan.get("query_expression")) or _terms_to_or_expression(initial_plan.get("keywords", [])) or query
        current_keywords = _normalize_keywords(initial_plan.get("keywords", []))
        library.add_keywords("user", directory_key, current_keywords)
        library.commit()

        used_keywords = {item.casefold() for item in current_keywords}
        all_matches = []
        iterations = []
        seen_expressions = set()
        iteration = 0

        while True:
            normalized_expression = _normalize_expression_key(current_expression)
            if normalized_expression in seen_expressions:
                stop_reason = "duplicate_expression"
                break
            seen_expressions.add(normalized_expression)

            search_result = _run_grga_search(
                target,
                current_expression,
                pattern_mode=pattern_mode,
                case_sensitive=case_sensitive,
                max_count=max_count,
                timeout=timeout,
                before_context=before_context,
                after_context=after_context,
                multiline=multiline,
                glob_patterns=glob_patterns,
                path_only=path_only,
                grga_command=grga_command,
            )
            if not search_result.get("ok"):
                if iteration == 0:
                    return search_result
                iterations.append({"index": iteration, "query_expression": normalized_expression, "error": search_result.get("error")})
                stop_reason = "search_failed"
                break

            matches = search_result.get("matches", [])
            all_matches.extend(matches)
            segments = _segments_from_matches(matches, max_extract_segments)
            candidate_keywords = []
            if segments:
                for item in _extract_segment_keywords(client, model_name, query, segments):
                    keywords = item.get("keywords", [])
                    if not keywords:
                        continue
                    library.add_keywords("document", directory_key, keywords)
                    library.add_sources(directory_key, keywords, item.get("path", ""), item.get("line_number"))
                    candidate_keywords.extend(keywords)
                library.commit()

            iteration_record = {
                "index": iteration,
                "query_expression": normalized_expression,
                "match_count": sum(1 for item in matches if not item.get("is_context")),
                "extracted_keywords": _top_keywords(candidate_keywords, max_related_keywords),
            }

            if iteration >= repeat_count:
                iterations.append(iteration_record)
                stop_reason = "max_iterations"
                break
            if not candidate_keywords:
                iterations.append(iteration_record)
                stop_reason = "no_candidates"
                break

            next_plan = _select_related_plan(
                client,
                model_name,
                query,
                candidate_keywords,
                library.history(directory_key, candidate_keywords, limit=30, global_library=global_library),
                max_related_keywords,
            )
            next_keywords = _normalize_keywords(next_plan.get("keywords", []))
            new_seeds = [item for item in next_keywords if item.casefold() not in used_keywords]
            iteration_record["next_query_expression"] = next_plan.get("query_expression")
            iteration_record["new_seeds"] = new_seeds
            iterations.append(iteration_record)

            if not new_seeds:
                stop_reason = "no_new_seeds"
                break

            used_keywords.update(item.casefold() for item in new_seeds)
            next_expression = _clean_expression(next_plan.get("query_expression")) or _terms_to_or_expression(new_seeds)
            if not next_expression:
                stop_reason = "no_new_seeds"
                break
            current_expression = next_expression
            iteration += 1

        merged_matches = _merge_and_sort_matches(all_matches)
        return {
            "ok": True,
            "query": query,
            "initial_query_expression": initial_plan.get("query_expression") or current_expression,
            "patterns": [item["query_expression"] for item in iterations if item.get("query_expression")],
            "directory": directory_key,
            "iterations": iterations,
            "stop_reason": stop_reason,
            "db_path": str(database_file),
            "matches": merged_matches,
        }
    finally:
        library.close()


def _make_openai_client(base_url, api_key, model_name):
    resolved_base_url = base_url or _first_env(DEFAULT_BASE_URL_ENV_NAMES)
    resolved_api_key = api_key or _first_env(DEFAULT_API_KEY_ENV_NAMES)
    resolved_model_name = model_name or _first_env(DEFAULT_MODEL_ENV_NAMES)
    if not resolved_api_key:
        raise RuntimeError("OpenAI API key is required; set --llm-api-key or GRGA_OPENAI_API_KEY")
    if not resolved_model_name:
        raise RuntimeError("OpenAI model name is required; set --llm-model or GRGA_OPENAI_MODEL")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is required for deep search") from exc
    kwargs = {"api_key": resolved_api_key}
    if resolved_base_url:
        kwargs["base_url"] = resolved_base_url
    return OpenAI(**kwargs), resolved_model_name


def _first_env(names):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _build_query_plan(client, model_name, query, history, max_keywords):
    payload = _chat_json(
        client,
        model_name,
        [
            {"role": "system", "content": _planner_system_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "将用户查询转换为 grga 查询表达式",
                        "query": query,
                        "max_keywords": max_keywords,
                        "history": history,
                        "syntax": "term、\"phrase with spaces\"、A || B、A && B、(A || B) && C。未显式连接的相邻项按 OR 处理。",
                        "output_schema": {"keywords": ["关键词"], "query_expression": "grga 查询表达式"},
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    )
    return _normalize_plan(payload)


def _extract_segment_keywords(client, model_name, query, segments, max_keywords_per_segment=12):
    payload = _chat_json(
        client,
        model_name,
        [
            {"role": "system", "content": _extractor_system_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "逐段提取有检索价值的实体、名词、动词等关键词",
                        "query": query,
                        "segments": segments,
                        "output_schema": {"segments": [{"id": "段落ID", "keywords": ["关键词"]}]},
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    )
    items = payload.get("segments", []) if isinstance(payload, dict) else []
    segment_index = {segment["id"]: segment for segment in segments}
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        segment_id = str(item.get("id", ""))
        source = segment_index.get(segment_id, {})
        keywords = _normalize_keywords(item.get("keywords", []))
        if max_keywords_per_segment > 0:
            keywords = keywords[:max_keywords_per_segment]
        normalized.append(
            {
                "id": segment_id,
                "keywords": keywords,
                "path": source.get("path", ""),
                "line_number": source.get("line_number"),
            }
        )
    return normalized


def _select_related_plan(client, model_name, query, candidates, history, max_keywords):
    payload = _chat_json(
        client,
        model_name,
        [
            {"role": "system", "content": _planner_system_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "从候选关键词中选择与用户查询相关性最高的关键词并组成 grga 查询表达式",
                        "query": query,
                        "max_keywords": max_keywords,
                        "candidate_keywords": _top_keywords(candidates, max(50, max_keywords * 5)),
                        "history": history,
                        "syntax": "用 || 组合同义词、近义词，用 && 表示必须共现的概念，可使用括号分组。",
                        "output_schema": {"keywords": ["关键词"], "query_expression": "grga 查询表达式"},
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    )
    return _normalize_plan(payload)


def _planner_system_prompt():
    return "你是检索查询规划器。只输出 JSON 对象。关键词必须是有检索价值的实体、名词、动词或短语。避免泛词、虚词、形容词和过长句子。查询表达式必须符合 grga 语法。"


def _extractor_system_prompt():
    return "你是文档关键词抽取器。只输出 JSON 对象。为每个段落提取有检索价值的实体、名词、动词或短语，去掉泛词、虚词和重复词。"


def _chat_json(client, model_name, messages):
    try:
        response = client.chat.completions.create(model=model_name, messages=messages, temperature=0.1, response_format={"type": "json_object"})
    except Exception as exc:
        if "response_format" not in str(exc):
            raise
        response = client.chat.completions.create(model=model_name, messages=messages, temperature=0.1)
    content = response.choices[0].message.content or "{}"
    return _loads_json_object(content)


def _loads_json_object(content):
    text = str(content).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        return {}
    return data


def _normalize_plan(payload):
    if not isinstance(payload, dict):
        return {"keywords": [], "query_expression": ""}
    return {"keywords": _normalize_keywords(payload.get("keywords", [])), "query_expression": _clean_expression(payload.get("query_expression"))}


def _clean_expression(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value.strip())


def _normalize_keywords(values):
    if isinstance(values, str):
        values = re.split(r"[,，;；\n]", values)
    if not isinstance(values, Iterable):
        return []
    seen = set()
    normalized = []
    for value in values:
        text = str(value).strip().strip("'\"")
        text = re.sub(r"\s+", " ", text)
        if len(text) < 2 or len(text) > 40:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def _terms_to_or_expression(keywords):
    terms = [_quote_query_term(item) for item in _normalize_keywords(keywords)]
    if not terms:
        return ""
    if len(terms) == 1:
        return terms[0]
    return "(" + " || ".join(terms) + ")"


def _quote_query_term(term):
    escaped = re.escape(str(term).strip())
    if " " in escaped:
        return '"' + escaped.replace('"', '\\"') + '"'
    return escaped


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _run_grga_search(
    directory,
    expression,
    pattern_mode="auto",
    case_sensitive=False,
    max_count=None,
    timeout=600,
    before_context=3,
    after_context=3,
    multiline=False,
    glob_patterns=None,
    path_only=False,
    grga_command=None,
):
    command = _normalize_grga_command(grga_command) + [
        "search",
        expression,
        "--directory",
        str(directory),
        "--mode",
        pattern_mode,
        "--timeout",
        str(timeout),
        "--before-context",
        str(before_context),
        "--after-context",
        str(after_context),
        "--json",
    ]
    command.append("--case-sensitive" if case_sensitive else "--ignore-case")
    if max_count is not None:
        command.extend(["--max-count", str(max_count)])
    if multiline:
        command.append("--multiline")
    if path_only:
        command.append("--path-only")
    for pattern in _normalize_glob_patterns(glob_patterns):
        command.extend(["--iglob", pattern])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=float(timeout) + 10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "grga search timed out", "matches": []}
    try:
        result = json.loads(completed.stdout)
    except ValueError:
        result = None
    if result is not None:
        return result
    if completed.returncode != 0:
        return {"ok": False, "error": completed.stderr.strip() or completed.stdout.strip() or "grga search failed", "matches": []}
    return {"ok": False, "error": completed.stdout.strip() or "invalid grga json output", "matches": []}


def _normalize_grga_command(grga_command):
    if not grga_command:
        return [sys.executable, "-m", "grga"]
    if isinstance(grga_command, str):
        return shlex.split(grga_command)
    return [str(item) for item in grga_command]


def _segments_from_matches(matches, limit):
    segments = []
    for index, item in enumerate(matches):
        if item.get("is_context"):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        segments.append(
            {
                "id": str(index),
                "path": item.get("path", ""),
                "line_number": item.get("line_number"),
                "text": text[:2000],
            }
        )
        if len(segments) >= limit:
            break
    return segments


def _top_keywords(keywords, limit):
    counter = Counter(_normalize_keywords(keywords))
    return [item for item, _ in counter.most_common(limit)]


def _merge_and_sort_matches(matches):
    deduped = {}
    for item in matches:
        key = (item.get("path", ""), item.get("line_number"), item.get("text", ""))
        existing = deduped.get(key)
        if existing is None or (existing.get("is_context") and not item.get("is_context")):
            deduped[key] = item
    return sorted(deduped.values(), key=_match_sort_key)


def _match_sort_key(item):
    line_number = item.get("line_number")
    if not isinstance(line_number, int):
        line_number = 0
    return (str(item.get("path", "")), line_number, 1 if item.get("is_context") else 0, str(item.get("text", "")))


def _normalize_expression_key(expression):
    return re.sub(r"\s+", " ", str(expression or "").strip())
