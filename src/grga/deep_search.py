import itertools
import json
import logging
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

_logger = logging.getLogger("grga.deep_search")


def _log_debug(debug_enabled, message):
    if debug_enabled:
        _logger.debug(message)

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
    debug=False,
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
            debug=debug,
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
    debug=False,
):
    _log_debug(debug, f"深度搜索开始: query={query}, directory={directory}")
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
    _log_debug(debug, f"数据库路径: {database_file}")
    client, model_name = _make_openai_client(llm_base_url, llm_api_key, llm_model_name)
    _log_debug(debug, f"使用模型: {model_name}")

    library = KeywordLibrary(database_file, decay_factor=decay_factor, min_count=min_keyword_count)
    try:
        library.decay(directory_key)
        _log_debug(debug, "关键词库衰减完成")

        history = library.history(directory_key, [], limit=30, global_library=global_library)
        _log_debug(debug, f"关键词历史记录数: {len(history)}")

        initial_plan = _build_query_plan(
            client,
            model_name,
            query,
            history,
            max_related_keywords,
            debug=debug,
        )
        _log_debug(debug, f"初始查询计划: groups={initial_plan.get('keyword_groups', [])}")
        
        # 从 keyword_groups 生成两两组合的查询；如果没有分组则降级为表达式
        keyword_groups = initial_plan.get("keyword_groups")
        current_expressions = _keyword_groups_to_queries(keyword_groups)
        if not current_expressions:
            fallback_expr = _clean_expression(initial_plan.get("query_expression")) or _terms_to_or_expression(initial_plan.get("keywords", [])) or query
            current_expressions = [fallback_expr]
        
        # 展开所有关键词用于去重和历史记录
        all_keywords = []
        for group in keyword_groups or []:
            all_keywords.extend(group)
        current_keywords = _normalize_keywords(all_keywords or initial_plan.get("keywords", []))
        library.add_keywords("user", directory_key, current_keywords)
        library.commit()

        used_keywords = {item.casefold() for item in current_keywords}
        all_matches = []
        iterations = []
        seen_expressions = set()
        iteration = 0

        # 执行所有两两组合的批量搜索（最简版本：只执行一轮，不做迭代扩展）
        for current_expression in current_expressions:
            normalized_expression = _normalize_expression_key(current_expression)
            _log_debug(debug, f"--- 搜索 {iteration} ---")
            _log_debug(debug, f"查询表达式: {normalized_expression}")
            if normalized_expression in seen_expressions:
                _log_debug(debug, f"跳过: 表达式重复")
                continue
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
                _log_debug(debug, f"搜索失败: {search_result.get('error')}")
                iterations.append({"index": iteration, "query_expression": normalized_expression, "error": search_result.get("error")})
                iteration += 1
                continue

            matches = search_result.get("matches", [])
            match_count = sum(1 for item in matches if not item.get("is_context"))
            _log_debug(debug, f"搜索完成: {match_count} 个匹配（不含上下文）, 总数 {len(matches)}")
            
            # 为每个匹配标记来自哪个查询表达式（用于后续排序）
            for match in matches:
                match["from_query"] = normalized_expression
            all_matches.extend(matches)
            
            iterations.append({
                "index": iteration,
                "query_expression": normalized_expression,
                "match_count": match_count,
            })
            iteration += 1

        stop_reason = "batch_complete"
        merged_matches = _merge_and_sort_matches(all_matches, keyword_groups)
        _log_debug(debug, f"深度搜索完成: 共 {len(merged_matches)} 个去重结果, 停止原因: {stop_reason}")
        return {
            "ok": True,
            "query": query,
            "initial_query_expression": "; ".join(current_expressions),
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


def _build_query_plan(client, model_name, query, history, max_keywords, debug=False):
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
                        "output_schema": {"keyword_groups": [["核心词", "近义词1", "近义词2"]], "query_expression": "grga 查询表达式（可选，代码会优先用 keyword_groups）"},
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        debug=debug,
    )
    return _normalize_plan(payload)


def _extract_segment_keywords(client, model_name, query, segments, max_keywords_per_segment=12, debug=False):
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
        debug=debug,
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


def _select_related_plan(client, model_name, query, candidates, history, max_keywords, debug=False):
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
                        "output_schema": {"keyword_groups": [["核心词", "近义词1", "近义词2"]], "query_expression": "grga 查询表达式（可选，代码会优先用 keyword_groups）"},
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        debug=debug,
    )
    return _normalize_plan(payload)


def _planner_system_prompt():
    return """你是检索查询规划器。只输出 JSON 对象。

将用户查询分解为 **概念组**：每个组是一个核心概念及其近义词、同义词。同一组内的词是可替换关系（OR）。

【输出规则】
1. 每个概念组包含 1-3 个近义词
2. 组之间是不同的核心概念
3. 总共产出 2-5 个概念组
4. 关键词必须是有检索价值的实体、名词、动词或短语。避免泛词、虚词、形容词和过长句子

【示例】
查询："神经网络在图像分类中的应用"
输出：
{
  "keyword_groups": [
    ["神经网络", "深度学习", "深度神经网络"],
    ["图像分类", "图片识别", "视觉分类"],
    ["应用", "使用", "实践"]
  ]
}"""


def _extractor_system_prompt():
    return "你是文档关键词抽取器。只输出 JSON 对象。为每个段落提取有检索价值的实体、名词、动词或短语，去掉泛词、虚词和重复词。"


def _chat_json(client, model_name, messages, debug=False):
    if debug:
        _log_debug(True, f"发送给 LLM ({model_name}):")
        for msg in messages:
            _log_debug(True, f"  [{msg['role']}] {str(msg['content'])[:500]}")
    try:
        response = client.chat.completions.create(model=model_name, messages=messages, temperature=0.1, response_format={"type": "json_object"})
    except Exception as exc:
        if "response_format" not in str(exc):
            raise
        response = client.chat.completions.create(model=model_name, messages=messages, temperature=0.1)
    content = response.choices[0].message.content or "{}"
    if debug:
        _log_debug(True, f"LLM 返回: {content[:1000]}")
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
        return {"keyword_groups": [], "keywords": [], "query_expression": ""}
    keyword_groups = payload.get("keyword_groups")
    if not isinstance(keyword_groups, list):
        keyword_groups = []
    cleaned_groups = []
    for group in keyword_groups:
        if isinstance(group, str):
            terms = _normalize_keywords([group])
        elif isinstance(group, list):
            terms = _normalize_keywords(group)
        else:
            continue
        if terms:
            cleaned_groups.append(terms)
    return {
        "keyword_groups": cleaned_groups,
        "keywords": _normalize_keywords(payload.get("keywords", [])),
        "query_expression": _clean_expression(payload.get("query_expression")),
    }


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


def _keyword_groups_to_queries(keyword_groups):
    """将关键词分组转换为两两组合的查询列表。
    
    例如：[[a,a1], [b,b1], [c,c1]] -> [(a||a1) && (b||b1), (a||a1) && (c||c1), (b||b1) && (c||c1)]
    """
    if not keyword_groups or len(keyword_groups) < 2:
        return []
    group_exprs = [_terms_to_or_expression(group) for group in keyword_groups if group]
    if len(group_exprs) < 2:
        return []
    queries = []
    import itertools
    for left, right in itertools.combinations(group_exprs, 2):
        queries.append(f"{left} && {right}")
    return queries


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


def _merge_and_sort_matches(matches, keyword_groups=None):
    """合并去重并按匹配的关键字组数量排序。
    
    如果一条结果被多个两两组合查询命中，说明它匹配了更多的概念组，
    排序时优先展示（按 from_query 数量降序）。
    """
    deduped = {}
    for item in matches:
        key = (item.get("path", ""), item.get("line_number"), item.get("text", ""))
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = item
        elif existing.get("is_context") and not item.get("is_context"):
            deduped[key] = item
        else:
            # 合并 from_query 信息
            existing_queries = set()
            if isinstance(existing.get("from_query"), str):
                existing_queries.add(existing["from_query"])
            elif isinstance(existing.get("from_query"), set):
                existing_queries = existing["from_query"]
            if isinstance(item.get("from_query"), str):
                existing_queries.add(item["from_query"])
            elif isinstance(item.get("from_query"), set):
                existing_queries.update(item["from_query"])
            deduped[key]["from_query"] = existing_queries

    # 清理 from_query 并计算匹配深度
    results = []
    for item in deduped.values():
        from_queries = item.get("from_query")
        if isinstance(from_queries, (set, list)):
            item["_match_depth"] = len(from_queries)
        else:
            item["_match_depth"] = 1
        item.pop("from_query", None)
        results.append(item)

    return sorted(results, key=_match_sort_key)


def _match_sort_key(item):
    """排序键：先按匹配深度降序，再按文件路径、行号升序。"""
    line_number = item.get("line_number")
    if not isinstance(line_number, int):
        line_number = 0
    # 优先按 _match_depth 降序（取负值放在元组第一个位置）
    depth = -(item.get("_match_depth", 1))
    return (depth, str(item.get("path", "")), line_number, 1 if item.get("is_context") else 0, str(item.get("text", "")))


def _normalize_expression_key(expression):
    return re.sub(r"\s+", " ", str(expression or "").strip())
