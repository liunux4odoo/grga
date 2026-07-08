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
# Deep-search iterations need many raw hits per query for ranking, so the
# per-call cap is generous, but still bounded so a runaway query on a huge
# corpus cannot wedge the caller. Pass max_total explicitly to override.
DEEP_SEARCH_MAX_TOTAL = 20000
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
    "enable_tiered_search": True,
    "min_results": 5,
    "max_tiers": 6,
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
    enable_tiered_search=True,
    min_results=5,
    max_tiers=6,
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
            enable_tiered_search=enable_tiered_search,
            min_results=min_results,
            max_tiers=max_tiers,
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
    enable_tiered_search=True,
    min_results=5,
    max_tiers=6,
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

    advanced_plan = _build_advanced_search_plan(client, model_name, query, max_related_keywords, debug=debug)
    strategy = advanced_plan.get("strategy") or "fallback"
    _log_debug(debug, f"高级规划: strategy={strategy}, reason={advanced_plan.get('reason', '')}")
    # 供 horizontal / multi_hop 结果不足时回退到本地分层搜索使用
    tiered_fallback_kwargs = {
        "max_related_keywords": max_related_keywords,
        "database_file": database_file,
        "decay_factor": decay_factor,
        "min_keyword_count": min_keyword_count,
        "global_library": global_library,
        "enable_tiered_search": enable_tiered_search,
        "min_results": min_results,
        "max_tiers": max_tiers,
    }
    if strategy == "horizontal":
        return _execute_horizontal_plan(
            target=target,
            directory_key=directory_key,
            query=query,
            plan=advanced_plan,
            client=client,
            model_name=model_name,
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
            max_extract_segments=max_extract_segments,
            tiered_fallback_kwargs=tiered_fallback_kwargs,
            debug=debug,
        )
    if strategy == "multi_hop":
        return _execute_multihop_plan(
            target=target,
            directory_key=directory_key,
            query=query,
            plan=advanced_plan,
            client=client,
            model_name=model_name,
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
            max_extract_segments=max_extract_segments,
            max_hops=max(1, max_related_keywords),
            tiered_fallback_kwargs=tiered_fallback_kwargs,
            debug=debug,
        )

    # direct / keyword_expand / fallback 均落到本地分层搜索。
    return _run_tiered_search(
        target=target,
        directory_key=directory_key,
        query=query,
        client=client,
        model_name=model_name,
        strategy=strategy,
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
        max_related_keywords=max_related_keywords,
        database_file=database_file,
        decay_factor=decay_factor,
        min_keyword_count=min_keyword_count,
        global_library=global_library,
        enable_tiered_search=enable_tiered_search,
        min_results=min_results,
        max_tiers=max_tiers,
        debug=debug,
    )


def _run_tiered_search(
    target,
    directory_key,
    query,
    client,
    model_name,
    strategy="keyword_expand",
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
    max_related_keywords=8,
    database_file=None,
    decay_factor=DEFAULT_DECAY_FACTOR,
    min_keyword_count=DEFAULT_MIN_COUNT,
    global_library=False,
    enable_tiered_search=True,
    min_results=5,
    max_tiers=6,
    debug=False,
):
    """本地分层关键词搜索，可独立作为深度搜索兜底路径复用。

    根据 strategy 调整侧重点：
    - direct: 不需要扩展，只跑最严格的 1 层
    - keyword_expand: 走完整分层（主词 → 同义词扩展 → 兜底 OR）
    - fallback / 其他: 走完整分层
    """
    database_file = Path(database_file).expanduser() if database_file else DEFAULT_DB_PATH

    effective_enable_tiered = enable_tiered_search
    effective_max_tiers = max_tiers
    if strategy == "direct":
        effective_max_tiers = 1
        _log_debug(debug, "strategy=direct: 只运行最严格的 1 层查询")
    elif strategy == "keyword_expand":
        _log_debug(debug, "strategy=keyword_expand: 使用分层同义词扩展搜索")

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

        keyword_groups = initial_plan.get("keyword_groups")
        llm_tiers = initial_plan.get("query_tiers") or []
        local_tiers = _keyword_groups_to_tiers(keyword_groups, query=query, max_tiers=effective_max_tiers)

        current_expressions = []
        seen_expr_keys = set()

        def _append_expr(expression):
            expression = _clean_expression(expression)
            if not expression:
                return
            key = _normalize_expression_key(expression)
            if key in seen_expr_keys:
                return
            seen_expr_keys.add(key)
            current_expressions.append(expression)

        if effective_enable_tiered:
            for tier in llm_tiers:
                _append_expr(tier)
            for tier in local_tiers:
                _append_expr(tier)
        else:
            for expr in _keyword_groups_to_queries(keyword_groups, query=query):
                _append_expr(expr)

        if not current_expressions:
            fallback_expr = (
                _clean_expression(initial_plan.get("query_expression"))
                or _terms_to_or_expression(initial_plan.get("keywords", []))
                or query
            )
            _append_expr(fallback_expr)

        if effective_max_tiers and effective_max_tiers > 0:
            current_expressions = current_expressions[:effective_max_tiers]

        _log_debug(debug, f"分层查询计划: {current_expressions}")

        # 展开所有关键词用于去重和历史记录
        all_keywords = []
        for group in keyword_groups or []:
            all_keywords.extend(group)
        current_keywords = _normalize_keywords(all_keywords or initial_plan.get("keywords", []))
        library.add_keywords("user", directory_key, current_keywords)
        library.commit()

        all_matches = []
        iterations = []
        iteration = 0
        stop_reason = "batch_complete"
        min_results_threshold = max(1, int(min_results or 0)) if effective_enable_tiered else 0

        for current_expression in current_expressions:
            normalized_expression = _normalize_expression_key(current_expression)
            _log_debug(debug, f"--- Tier {iteration} ---")
            _log_debug(debug, f"查询表达式: {normalized_expression}")

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
                iterations.append(
                    {
                        "index": iteration,
                        "tier": iteration,
                        "query_expression": normalized_expression,
                        "error": search_result.get("error"),
                    }
                )
                iteration += 1
                continue

            matches = search_result.get("matches", [])
            match_count = sum(1 for item in matches if not item.get("is_context"))
            _log_debug(
                debug,
                f"Tier {iteration} 完成: {match_count} 个匹配（不含上下文），总数 {len(matches)}",
            )

            # 为每个匹配标记来自哪个 tier 和查询表达式（用于后续排序）
            for match in matches:
                match["from_query"] = normalized_expression
                match["from_tier"] = iteration
            all_matches.extend(matches)

            iterations.append(
                {
                    "index": iteration,
                    "tier": iteration,
                    "query_expression": normalized_expression,
                    "match_count": match_count,
                }
            )
            iteration += 1

            if enable_tiered_search:
                unique_hits = {
                    (item.get("path", ""), item.get("line_number"), item.get("text", ""))
                    for item in all_matches
                    if not item.get("is_context")
                }
                if len(unique_hits) >= min_results_threshold:
                    stop_reason = "enough_results"
                    _log_debug(
                        debug,
                        f"命中数 {len(unique_hits)} >= 阈值 {min_results_threshold}，停止后续分层",
                    )
                    break

        merged_matches = _merge_and_sort_matches(all_matches, keyword_groups)
        _log_debug(debug, f"深度搜索完成: 共 {len(merged_matches)} 个去重结果, 停止原因: {stop_reason}")
        return {
            "ok": True,
            "query": query,
            "strategy": strategy,
            "initial_query_expression": "; ".join(current_expressions),
            "patterns": [item["query_expression"] for item in iterations if item.get("query_expression")],
            "directory": directory_key,
            "iterations": iterations,
            "stop_reason": stop_reason,
            "db_path": str(database_file),
            "keyword_groups": keyword_groups or [],
            "matches": merged_matches,
        }
    finally:
        library.close()


# 意图兜底：让 LLM 根据问题动态生成"原文答案场景可能出现的动作/情境词"，
# 相比硬编码词表，可覆盖任意语言、任意领域、任意意图类别。
def _generate_intent_verbs_with_llm(client, model_name, query, entity_terms=None, debug=False):
    """调用 LLM 生成意图动词/情境关键词。失败或不适用时返回空列表。"""
    if client is None or not model_name:
        return []
    try:
        payload = _chat_json(
            client,
            model_name,
            [
                {"role": "system", "content": _intent_verb_system_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": "为本地文档检索生成意图动词/情境关键词",
                            "query": query,
                            "entity_terms": list(entity_terms or []),
                            "output_schema": {
                                "intent": "问题意图类别（英文短标签，如 acquisition/death/reason/relation/time/none）",
                                "verbs": ["动作或情境词，可为单字或短词"],
                                "reason": "为什么选这些词",
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            debug=debug,
        )
    except Exception as exc:
        _log_debug(debug, f"意图动词生成失败: {exc}")
        return []
    if not isinstance(payload, dict):
        return []
    raw_verbs = payload.get("verbs")
    if not isinstance(raw_verbs, list):
        return []
    verbs = []
    seen = set()
    for item in raw_verbs:
        text = str(item or "").strip().strip("'\"")
        if not text or len(text) > 20:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        verbs.append(text)
    return verbs


def _intent_verb_system_prompt():
    return """你是本地文档检索的意图分析器。只输出 JSON 对象。

任务：根据用户问题，列出**原文答案场景中可能出现**的 5-15 个动作、情境或状态关键词。
这些关键词会与已识别的对象名一起做 (对象) && (关键词) 检索，用来命中那些
只用泛称+动作描写、不含问题里功能性词汇的原文段落。

规则：
1. verbs 里放的是**原文可能出现的具体动作/情境/状态词**，不是问题里的抽象疑问词
   - 反例：不要放"如何、怎么、为什么、是否"
   - 正例：问"如何获得X" → 放 [捡, 拾, 买, 得到, 意外, 发现, 继承, 夺取, 交换...]
2. 尽量穷举原文可能采用的多种表达方式（同义、近义、场景相关）
3. 支持任意语言，按原文可能的语言选择关键词
4. 词长控制在 1-6 字/字符；对中日韩语言，单字动词/名词是有价值的（如 死/亡/买/年）
5. 如果问题**不是**在询问某种动作、事件、时间、原因、关系（例如纯粹的清单/统计/属性描述），
   返回 "verbs": [] 并将 intent 设为 "none"，让上层跳过意图兜底
6. 如果已提供 entity_terms，动词应围绕"主体如何对这些实体做某事"来选

输出 JSON：
{
  "intent": "acquisition | death | time | reason | relation | location | method | none | ...",
  "verbs": ["词1", "词2", ...],
  "reason": "简短说明"
}
"""


def _build_intent_fallback_query(client, model_name, query, entity_terms, keyword_groups, debug=False):
    """为需要"动作/情境"证据的问题构造 (对象 || 别名) && (意图关键词) 兜底查询。

    意图关键词由 LLM 根据问题动态生成，不再依赖硬编码词表，天然覆盖任意语言/领域。
    如果 LLM 判定问题不适用意图兜底（返回空 verbs），本函数返回空串，上层会跳过。

    对象项来自 entity_terms + keyword_groups 里最像"具体对象别名集合"的组
    （判定标准：组内所有词共享至少 1 个中文/字母字符，避免把功能/动作类同义词
    组当成对象组）。
    """
    verbs = _generate_intent_verbs_with_llm(client, model_name, query, entity_terms=entity_terms, debug=debug)
    if not verbs:
        return ""
    object_terms = []
    seen = set()

    def _add(term):
        term = _clean_expression(str(term or ""))
        if not term:
            return
        key = term.casefold()
        if key in seen:
            return
        seen.add(key)
        object_terms.append(term)

    for term in entity_terms or []:
        _add(term)
    # 仅接受“具体对象”概念组：组内多数词（默认 ≥60%）共享同一个 CJK/字母字符，
    # 视为“同一对象的别名集合”。如 ['神秘小瓶','小瓶','青色小瓶','此宝'] 中 3/4
    # 含 '瓶'，仍判定为对象组，并只保留含主导字符的词（丢弃 '此宝' 这类离群别名）。
    # 排除功能动词/属性近义词组（如 ['催熟灵药','催熟','加速生长','灵药催熟'] 无主导字符）。
    for group in keyword_groups or []:
        if not isinstance(group, (list, tuple)) or len(group) < 2:
            continue
        for term in _object_terms_by_dominant_char(group):
            _add(term)
    if not object_terms:
        return ""
    object_expr = _terms_to_or_expression(object_terms)
    verb_expr = _or_expression_from_terms(verbs)
    if not object_expr or not verb_expr:
        return ""
    return f"{object_expr} && {verb_expr}"


def _or_expression_from_terms(terms):
    """把任意（含单字符）关键词拼成 grga OR 表达式，绕过 _normalize_keywords 的长度下限。

    意图动词里存在 '捡'、'拾'、'死'、'年' 等单字符高价值词，
    不能被 _normalize_keywords 的 len<2 规则静默丢弃。
    """
    quoted = []
    seen = set()
    for term in terms:
        text = str(term or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        quoted.append(_quote_query_term(text))
    if not quoted:
        return ""
    if len(quoted) == 1:
        return quoted[0]
    return "(" + " || ".join(quoted) + ")"


def _object_terms_by_dominant_char(terms, min_ratio=0.6):
    """从一个概念组里挑出属于“同一对象别名”的词。

    做法：把每个词拆成“语义单元”——CJK 表意文字按单字（每字即一个词素），
    字母/数字按整词（连续字母数字串，小写化）。统计每个单元出现在多少个词里，
    取覆盖率最高者（主导单元）；若覆盖率 ≥ min_ratio（默认 60%），则该组视为
    对象组，只保留含主导单元的词（丢弃离群别名）；否则返回空列表。

    - 容忍 ['掌天瓶','青色小瓶','神秘瓶子','此宝'] 里 '此宝' 不含 '瓶'：
      主导单元 '瓶' 覆盖 3/4=0.75 ≥ 0.6，保留 3 个含 '瓶' 的词。
    - 字母语言用整词而非单字母，避免 ['bottle','treasure'] 因共享字母 'e' 误判：
      主导单元 'bottle' 覆盖 3/4，保留 3 个含 bottle 的词，丢弃 'treasure'。
    """
    unique_terms = []
    seen = set()
    for term in terms:
        text = str(term or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique_terms.append(text)
    if len(unique_terms) < 2:
        return []
    unit_counts = {}
    term_units = []
    for text in unique_terms:
        units = _semantic_units(text)
        term_units.append(units)
        for unit in units:
            unit_counts[unit] = unit_counts.get(unit, 0) + 1
    if not unit_counts:
        return []
    dominant_unit, dominant_count = max(unit_counts.items(), key=lambda item: item[1])
    if dominant_count / len(unique_terms) < min_ratio:
        return []
    return [text for text, units in zip(unique_terms, term_units) if dominant_unit in units]


def _semantic_units(text):
    """把词拆成语义单元集合：CJK 单字（逐字）+ 字母数字整词（小写）。"""
    units = set()
    for ch in str(text):
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF:  # CJK 表意文字，逐字作为词素
            units.add(ch)
    for word in re.findall(r"[0-9A-Za-z]+", str(text)):
        if len(word) >= 2:  # 单字母不作为词素，避免 e/t/a 之类误配
            units.add(word.casefold())
    return units


def _is_significant_char(ch):
    if ch.isalpha():
        return True
    code = ord(ch)
    # CJK Unified Ideographs 基础区
    return 0x4E00 <= code <= 0x9FFF


def _build_advanced_search_plan(client, model_name, query, max_steps, debug=False):
    payload = _chat_json(
        client,
        model_name,
        [
            {"role": "system", "content": _advanced_planner_system_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "判断是否需要高级扩展搜索，并输出可执行搜索计划",
                        "query": query,
                        "max_steps": max_steps,
                        "syntax": "term、\"phrase with spaces\"、A || B、A && B、(A || B) && C。多跳步骤可使用 {{answer_key}} 引用前序答案。",
                        "strategies": ["direct", "keyword_expand", "horizontal", "multi_hop", "hybrid", "fallback"],
                        "output_schema": {
                            "strategy": "direct|keyword_expand|horizontal|multi_hop|hybrid|fallback",
                            "reason": "为什么选择该策略",
                            "steps": [
                                {
                                    "id": "唯一ID",
                                    "type": "search_extract",
                                    "query_template": "grga 查询表达式",
                                    "depends_on": ["前序 answer_key，可选"],
                                    "goal": "本步骤目标",
                                    "answer_key": "抽取结果变量名，可选",
                                    "metric": "数值指标名，可选",
                                }
                            ],
                            "synthesis": {"format": "最终答案组织方式"},
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        debug=debug,
    )
    return _normalize_advanced_plan(payload, max_steps=max_steps)


def _normalize_advanced_plan(payload, max_steps=8):
    if not isinstance(payload, dict):
        return {"strategy": "fallback", "steps": []}
    strategy = str(payload.get("strategy") or "fallback").strip().lower().replace("-", "_")
    if strategy not in {"direct", "keyword_expand", "horizontal", "multi_hop", "hybrid", "fallback"}:
        strategy = "fallback"
    steps = []
    raw_steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    for index, raw in enumerate(raw_steps[: max(1, max_steps)]):
        if not isinstance(raw, dict):
            continue
        expression = _clean_expression(raw.get("query_template") or raw.get("query_expression") or raw.get("query"))
        if not expression:
            continue
        answer_key = _clean_answer_key(raw.get("answer_key") or raw.get("id") or f"step_{index + 1}")
        depends_on = raw.get("depends_on") if isinstance(raw.get("depends_on"), list) else []
        steps.append(
            {
                "id": _clean_answer_key(raw.get("id") or answer_key or f"step_{index + 1}"),
                "type": str(raw.get("type") or "search_extract"),
                "query_template": expression,
                "depends_on": [_clean_answer_key(item) for item in depends_on if _clean_answer_key(item)],
                "goal": str(raw.get("goal") or "").strip(),
                "answer_key": answer_key,
                "metric": str(raw.get("metric") or "").strip() or None,
            }
        )
    if strategy in {"horizontal", "multi_hop", "hybrid"} and not steps:
        strategy = "fallback"
    if strategy == "hybrid":
        strategy = "multi_hop" if any(step.get("depends_on") or "{{" in step.get("query_template", "") for step in steps) else "horizontal"
    if strategy not in {"horizontal", "multi_hop"}:
        steps = []
    return {
        "strategy": strategy,
        "reason": str(payload.get("reason") or "").strip(),
        "steps": steps,
        "synthesis": payload.get("synthesis") if isinstance(payload.get("synthesis"), dict) else {},
    }


def _advanced_planner_system_prompt():
    return """你是 grga 高级搜索规划器。只输出 JSON 对象。

你要判断用户问题是否需要在本地文档中进行高级扩展搜索。

策略说明：
- direct：单次关键词搜索即可，不需要扩展
- keyword_expand：只需要同义词、别名或关键词扩展，交给普通深度搜索处理
- horizontal：需要把同一问题按年份、实体、地区、项目、部门、产品等维度拆成多个并列子查询
- multi_hop：后续查询依赖前一步从文档中抽取出的答案
- hybrid：同时包含横向拆分和多跳依赖
- fallback：不确定或不适合高级扩展

只有确实需要并列子查询或依赖式多跳查询时，才选择 horizontal、multi_hop 或 hybrid。不要因为示例中的具体词而套模板。

步骤规则：
1. query_template 必须是适合 grga 的短关键词表达式，不要写完整长句
2. 可使用 A || B 表示同义词，可使用 A && B 表示必须共现
3. 多跳依赖使用 {{answer_key}} 引用前序抽取结果
4. answer_key 使用英文、数字或下划线；横向维度可以使用年份、实体名或字段名
5. 不要编造事实，只规划要搜索什么
6. 如果不需要高级扩展，输出 strategy 为 direct、keyword_expand 或 fallback，steps 可为空
"""


def _clean_answer_key(value):
    text = re.sub(r"\s+", "_", str(value or "").strip())
    text = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "", text)
    return text[:40]


def _run_advanced_fallbacks(
    *,
    target,
    directory_key,
    query,
    client,
    model_name,
    pattern_mode,
    case_sensitive,
    max_count,
    timeout,
    before_context,
    after_context,
    multiline,
    glob_patterns,
    path_only,
    grga_command,
    tiered_fallback_kwargs,
    entity_terms,
    known_patterns,
    iteration_offset,
    debug=False,
):
    """当 horizontal / multi_hop 未能满足需求时，跑分层兜底 + 意图兜底。

    返回 (extra_matches, extra_patterns, extra_iterations, stop_suffix)。
    - extra_matches: 已按 advanced_step / from_query / from_tier 标记完毕，可直接 extend
    - extra_patterns: 未出现在 known_patterns 中的新表达式
    - extra_iterations: 供 iterations 列表 append，index 自 iteration_offset 起编号
    - stop_suffix: 形如 "" / "+tiered_fallback" / "+tiered_fallback+intent_fallback"
    """
    extra_matches = []
    extra_patterns = []
    extra_iterations = []
    stop_suffix = ""
    if not tiered_fallback_kwargs:
        return extra_matches, extra_patterns, extra_iterations, stop_suffix

    _log_debug(debug, f"触发分层兜底搜索: {query}")
    fallback = _run_tiered_search(
        target=target,
        directory_key=directory_key,
        query=query,
        client=client,
        model_name=model_name,
        strategy="fallback",
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
        debug=debug,
        **tiered_fallback_kwargs,
    )
    if not fallback.get("ok"):
        return extra_matches, extra_patterns, extra_iterations, stop_suffix

    known_pattern_set = set(known_patterns or ())

    fallback_matches = fallback.get("matches", [])
    for match in fallback_matches:
        match.setdefault("from_query", "fallback_tiered")
        match["advanced_step"] = "fallback_tiered"
    extra_matches.extend(fallback_matches)
    for pattern in fallback.get("patterns", []):
        if pattern and pattern not in known_pattern_set:
            known_pattern_set.add(pattern)
            extra_patterns.append(pattern)
    extra_iterations.append(
        {
            "index": iteration_offset + len(extra_iterations),
            "step_id": "fallback_tiered",
            "strategy": "tiered_fallback",
            "query_expression": "; ".join(fallback.get("patterns", [])),
            "match_count": sum(1 for item in fallback_matches if not item.get("is_context")),
            "stop_reason": fallback.get("stop_reason"),
        }
    )
    stop_suffix = "+tiered_fallback"

    # 意图感知兜底：把已抽取到的实体 + 分层规划里的对象概念组，
    # 与 LLM 动态生成的意图动词组合，命中泛称+动作描写的原文场景。
    intent_expr = _build_intent_fallback_query(
        client,
        model_name,
        query,
        entity_terms=entity_terms,
        keyword_groups=fallback.get("keyword_groups") or [],
        debug=debug,
    )
    if not intent_expr:
        return extra_matches, extra_patterns, extra_iterations, stop_suffix

    _log_debug(debug, f"触发意图兜底搜索: {intent_expr}")
    intent_result = _run_grga_search(
        target,
        intent_expr,
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
    intent_matches = intent_result.get("matches", []) if intent_result.get("ok") else []
    for match in intent_matches:
        match.setdefault("from_query", "fallback_intent")
        match["advanced_step"] = "fallback_intent"
        # 意图兜底直接对齐问题意图，最相关，给最高优先级
        match["from_tier"] = -1
    extra_matches.extend(intent_matches)
    if intent_expr and intent_expr not in known_pattern_set:
        extra_patterns.append(intent_expr)
    extra_iterations.append(
        {
            "index": iteration_offset + len(extra_iterations),
            "step_id": "fallback_intent",
            "strategy": "intent_fallback",
            "query_expression": intent_expr,
            "match_count": sum(1 for item in intent_matches if not item.get("is_context")),
            "error": None if intent_result.get("ok") else intent_result.get("error"),
        }
    )
    if intent_matches:
        stop_suffix = f"{stop_suffix}+intent_fallback"
    return extra_matches, extra_patterns, extra_iterations, stop_suffix


def _execute_horizontal_plan(
    target,
    directory_key,
    query,
    plan,
    client,
    model_name,
    pattern_mode,
    case_sensitive,
    max_count,
    timeout,
    before_context,
    after_context,
    multiline,
    glob_patterns,
    path_only,
    grga_command,
    max_extract_segments,
    tiered_fallback_kwargs=None,
    debug=False,
):
    all_matches = []
    iterations = []
    fields = {}
    citations = []
    patterns = []
    for index, step in enumerate(plan.get("steps", [])):
        expression = _clean_expression(step.get("query_template"))
        if not expression:
            continue
        patterns.append(expression)
        search_result = _run_grga_search(
            target,
            expression,
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
        matches = search_result.get("matches", []) if search_result.get("ok") else []
        for match in matches:
            match["from_query"] = expression
            match["advanced_step"] = step.get("id")
        all_matches.extend(matches)
        extracted = _extract_step_answer(client, model_name, query, step, matches, max_extract_segments=max_extract_segments, debug=debug)
        answer_key = step.get("answer_key") or step.get("id") or str(index)
        fields[answer_key] = extracted.get("answer") or "未找到"
        citations.extend(extracted.get("citations", []))
        iterations.append(
            {
                "index": index,
                "step_id": step.get("id"),
                "strategy": "horizontal",
                "query_expression": expression,
                "goal": step.get("goal"),
                "answer_key": answer_key,
                "answer": fields[answer_key],
                "confidence": extracted.get("confidence", 0),
                "match_count": sum(1 for item in matches if not item.get("is_context")),
                "error": None if search_result.get("ok") else search_result.get("error"),
            }
        )

    stop_reason = "horizontal_complete"
    # 若所有横向子查询都未抽取到答案，则回退到原始 query 的分层搜索 + 意图兜底
    if tiered_fallback_kwargs and fields and all(value == "未找到" for value in fields.values()):
        _log_debug(debug, f"横向所有子查询均未找到答案，触发分层兜底搜索: {query}")
        entity_terms = [value for value in fields.values() if value and value != "未找到"]
        extra_matches, extra_patterns, extra_iterations, stop_suffix = _run_advanced_fallbacks(
            target=target,
            directory_key=directory_key,
            query=query,
            client=client,
            model_name=model_name,
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
            tiered_fallback_kwargs=tiered_fallback_kwargs,
            entity_terms=entity_terms,
            known_patterns=patterns,
            iteration_offset=len(iterations),
            debug=debug,
        )
        all_matches.extend(extra_matches)
        patterns.extend(extra_patterns)
        iterations.extend(extra_iterations)
        if stop_suffix:
            stop_reason = f"{stop_reason}{stop_suffix}"

    merged_matches = _merge_and_sort_matches(all_matches)
    return {
        "ok": True,
        "query": query,
        "strategy": "horizontal",
        "initial_query_expression": "; ".join(patterns),
        "patterns": patterns,
        "directory": directory_key,
        "plan": plan,
        "answer": {"text": _format_horizontal_answer(query, fields), "fields": fields, "citations": citations},
        "iterations": iterations,
        "stop_reason": stop_reason,
        "matches": merged_matches,
    }


def _execute_multihop_plan(
    target,
    directory_key,
    query,
    plan,
    client,
    model_name,
    pattern_mode,
    case_sensitive,
    max_count,
    timeout,
    before_context,
    after_context,
    multiline,
    glob_patterns,
    path_only,
    grga_command,
    max_extract_segments,
    max_hops=4,
    tiered_fallback_kwargs=None,
    debug=False,
):
    context = {}
    all_matches = []
    iterations = []
    citations = []
    reasoning_chain = []
    patterns = []
    stop_reason = "multi_hop_complete"
    for index, step in enumerate(plan.get("steps", [])[:max_hops]):
        missing = [dep for dep in step.get("depends_on", []) if not context.get(dep)]
        if missing:
            stop_reason = "missing_dependency"
            iterations.append({"index": index, "step_id": step.get("id"), "strategy": "multi_hop", "missing": missing})
            break
        expression = _clean_expression(_render_query_template(step.get("query_template", ""), context))
        if not expression:
            continue
        patterns.append(expression)
        search_result = _run_grga_search(
            target,
            expression,
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
        matches = search_result.get("matches", []) if search_result.get("ok") else []
        for match in matches:
            match["from_query"] = expression
            match["advanced_step"] = step.get("id")
        all_matches.extend(matches)
        extracted = _extract_step_answer(client, model_name, query, step, matches, max_extract_segments=max_extract_segments, debug=debug)
        answer_key = step.get("answer_key") or step.get("id")
        answer = extracted.get("answer")
        if answer_key and answer:
            context[answer_key] = answer
        citations.extend(extracted.get("citations", []))
        chain_item = {
            "step": step.get("id"),
            "goal": step.get("goal"),
            "query_expression": expression,
            "answer_key": answer_key,
            "answer": answer,
            "confidence": extracted.get("confidence", 0),
        }
        reasoning_chain.append(chain_item)
        iterations.append(
            {
                "index": index,
                "step_id": step.get("id"),
                "strategy": "multi_hop",
                "query_expression": expression,
                "goal": step.get("goal"),
                "answer_key": answer_key,
                "answer": answer or "未找到",
                "confidence": extracted.get("confidence", 0),
                "match_count": sum(1 for item in matches if not item.get("is_context")),
                "error": None if search_result.get("ok") else search_result.get("error"),
            }
        )
        if not answer:
            stop_reason = "answer_not_found"
            break

    # 多跳未能完整解答（未找到答案或依赖缺失）时，回退到原始 query 的分层搜索
    # + 意图感知兜底，避免遗漏文档里用泛称表述的关键场景。
    if tiered_fallback_kwargs and stop_reason in {"answer_not_found", "missing_dependency"}:
        _log_debug(debug, f"多跳停止于 {stop_reason}，触发兜底搜索: {query}")
        extra_matches, extra_patterns, extra_iterations, stop_suffix = _run_advanced_fallbacks(
            target=target,
            directory_key=directory_key,
            query=query,
            client=client,
            model_name=model_name,
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
            tiered_fallback_kwargs=tiered_fallback_kwargs,
            entity_terms=list(context.values()),
            known_patterns=patterns,
            iteration_offset=len(iterations),
            debug=debug,
        )
        all_matches.extend(extra_matches)
        patterns.extend(extra_patterns)
        iterations.extend(extra_iterations)
        if stop_suffix:
            stop_reason = f"{stop_reason}{stop_suffix}"

    merged_matches = _merge_and_sort_matches(all_matches)
    return {
        "ok": True,
        "query": query,
        "strategy": "multi_hop",
        "initial_query_expression": "; ".join(patterns),
        "patterns": patterns,
        "directory": directory_key,
        "plan": plan,
        "answer": {"text": _format_multihop_answer(query, context, reasoning_chain), "fields": context, "reasoning_chain": reasoning_chain, "citations": citations},
        "iterations": iterations,
        "stop_reason": stop_reason,
        "matches": merged_matches,
    }


def _render_query_template(template, context):
    def replace(match):
        return str(context.get(match.group(1), ""))

    return re.sub(r"\{\{([a-zA-Z0-9_]+)\}\}", replace, str(template or ""))


def _extract_step_answer(client, model_name, original_query, step, matches, max_extract_segments=20, debug=False):
    if client is None or not model_name:
        return {"answer": None, "confidence": 0, "citations": [], "missing": True}
    try:
        return _extract_step_answer_with_llm(client, model_name, original_query, step, matches, max_extract_segments=max_extract_segments, debug=debug)
    except Exception as exc:
        _log_debug(debug, f"LLM 步骤抽取失败: {exc}")
        return {"answer": None, "confidence": 0, "citations": [], "missing": True}


def _extract_step_answer_with_llm(client, model_name, original_query, step, matches, max_extract_segments=20, debug=False):
    segments = _segments_from_matches(matches, max_extract_segments)
    if not segments:
        return {"answer": None, "confidence": 0, "citations": [], "missing": True}
    payload = _chat_json(
        client,
        model_name,
        [
            {"role": "system", "content": _step_answer_extractor_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "从搜索结果中抽取当前步骤答案",
                        "original_query": original_query,
                        "step_goal": step.get("goal"),
                        "answer_key": step.get("answer_key"),
                        "segments": segments,
                        "output_schema": {"answer": "答案或 null", "confidence": 0.0, "evidence_ids": ["片段ID"], "quote": "关键原文引用", "missing": False},
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        debug=debug,
    )
    answer = payload.get("answer")
    answer = str(answer).strip() if answer is not None else None
    if not answer or answer.lower() == "null":
        answer = None
    evidence_ids = {str(item) for item in payload.get("evidence_ids", []) if item is not None}
    if not evidence_ids and answer:
        evidence_ids = {segments[0]["id"]}
    citations = []
    for segment in segments:
        if str(segment.get("id")) in evidence_ids:
            citations.append({"path": segment.get("path", ""), "line_number": segment.get("line_number"), "quote": payload.get("quote") or segment.get("text", "")})
    return {"answer": answer, "confidence": _safe_float(payload.get("confidence"), 0), "citations": citations, "missing": bool(payload.get("missing", answer is None))}


def _format_horizontal_answer(query, fields):
    if not fields:
        return "未找到足够信息回答该问题。"
    lines = [f"{query}："]
    for key in sorted(fields.keys(), reverse=True):
        lines.append(f"- {key}：{fields[key]}")
    return "\n".join(lines)


def _format_multihop_answer(query, context, reasoning_chain):
    if context:
        lines = [f"{query}："]
        for key, value in context.items():
            lines.append(f"- {key}：{value}")
        return "\n".join(lines)
    if reasoning_chain:
        lines = [f"{query}："]
        for item in reasoning_chain:
            lines.append(f"- {item.get('goal')}：{item.get('answer') or '未找到'}")
        return "\n".join(lines)
    return "未找到足够信息回答该问题。"


def _step_answer_extractor_prompt():
    return """你是本地文档搜索结果答案抽取器。只输出 JSON 对象。

根据原始问题、当前步骤目标和搜索片段，抽取当前步骤答案。

要求：
1. 只能使用给定搜索片段，不要使用外部知识
2. 证据不足时 answer 返回 null，missing 返回 true
3. answer 应尽量简短，适合作为下一步搜索变量
4. 如果是数值指标，保留单位
5. 如果是地点，保留完整地名
6. 如果是人物，保留完整姓名

输出 JSON：
{
  "answer": "答案或 null",
  "confidence": 0.0,
  "evidence_ids": ["片段ID"],
  "quote": "关键原文引用",
  "missing": false
}
"""


def _safe_float(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
                        "task": "将用户查询转换为 grga 分层查询计划",
                        "query": query,
                        "max_keywords": max_keywords,
                        "history": history,
                        "syntax": "term、\"phrase with spaces\"、A || B、A && B、(A || B) && C。未显式连接的相邻项按 OR 处理。",
                        "output_schema": {
                            "keyword_groups": [["核心实体", "别名1", "别名2"]],
                            "query_tiers": [
                                "最严格：核心组主词全部 AND",
                                "次严：组内展开同义词后 AND",
                                "较宽：丢弃次要概念组",
                                "兜底：全部 OR",
                            ],
                            "query_expression": "可选，代码优先使用 query_tiers 或 keyword_groups",
                        },
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
                        "task": "逐段提取有检索价值的实体、名词和短语关键词",
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
                        "output_schema": {"keyword_groups": [["核心实体", "别名1", "别名2"]], "query_expression": "grga 查询表达式（可选，代码会优先用 keyword_groups）"},
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

将用户查询拆成 **概念组**：同一组内必须是同一实体、同一称呼、同一别名、同义词或近义词，组内词是可替换关系（OR）。只有用户查询同时包含多个必须共现的独立概念时，才输出多个概念组；不要把同一人物、同一对象的外号、评价、身份词拆成多个概念组。

【输出规则】
1. 单个人名、地名、外号、术语或短语查询，只输出 1 个概念组
2. 每个概念组包含 1-5 个可替换关键词，第 1 个词必须是最标准、检索价值最高的**主词**
3. 多个概念组之间表示必须同时出现的不同核心概念
4. 关键词必须是有检索价值的实体、名词或短语。避免泛词、虚词、形容词和过长句子

【分层查询 query_tiers（可选但推荐）】
输出一个从严到松的查询表达式列表，代码会按顺序执行，命中数达标即停止：
- 第 1 层最严格：每个概念组只取主词，用 && 连接（如 "神经网络 && 图像分类"）
- 第 2 层：每组主词 + 少量高价值同义词做 OR，再组间 && （如 "(神经网络 || 深度学习) && 图像分类"）
- 第 3 层：组内展开更多同义词
- 后续层可逐步丢弃最不重要的概念组，或改用 || 兜底
- 单概念查询只需要 2-3 层（主词 → 主词+关键别名 → 全部别名 OR）
- 不要重复相同表达式；表达式必须符合 grga 语法

【示例】
查询："二愣子"
输出：
{
  "keyword_groups": [["二愣子", "二楞子", "愣头青", "憨子"]],
  "query_tiers": [
    "二愣子",
    "二愣子 || 二楞子",
    "二愣子 || 二楞子 || 愣头青 || 憨子"
  ]
}

查询："神经网络在图像分类中的应用"
输出：
{
  "keyword_groups": [
    ["神经网络", "深度学习", "深度神经网络"],
    ["图像分类", "图片识别", "视觉分类"]
  ],
  "query_tiers": [
    "神经网络 && 图像分类",
    "(神经网络 || 深度学习) && (图像分类 || 图片识别)",
    "(神经网络 || 深度学习 || 深度神经网络) && (图像分类 || 图片识别 || 视觉分类)",
    "神经网络 || 深度学习 || 图像分类 || 图片识别"
  ]
}"""


def _extractor_system_prompt():
    return "你是文档关键词抽取器。只输出 JSON 对象。为每个段落提取有检索价值的实体、名词或短语，去掉泛词、虚词和重复词。"


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
        return {"keyword_groups": [], "keywords": [], "query_expression": "", "query_tiers": []}
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
    query_tiers = []
    seen_tiers = set()
    raw_tiers = payload.get("query_tiers")
    if isinstance(raw_tiers, list):
        for item in raw_tiers:
            expression = _clean_expression(item if isinstance(item, str) else item.get("query") if isinstance(item, dict) else "")
            if not expression:
                continue
            key = _normalize_expression_key(expression)
            if key in seen_tiers:
                continue
            seen_tiers.add(key)
            query_tiers.append(expression)
    return {
        "keyword_groups": cleaned_groups,
        "keywords": _normalize_keywords(payload.get("keywords", [])),
        "query_expression": _clean_expression(payload.get("query_expression")),
        "query_tiers": query_tiers,
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


def _keyword_groups_to_queries(keyword_groups, query=None):
    groups = [group for group in (keyword_groups or []) if group]
    if not groups:
        return []
    if len(groups) == 1 or _is_single_concept_query(query):
        terms = []
        for group in groups:
            terms.extend(group)
        expression = _terms_to_or_expression(terms)
        return [expression] if expression else []
    group_exprs = [_terms_to_or_expression(group) for group in groups]
    group_exprs = [item for item in group_exprs if item]
    if len(group_exprs) < 2:
        return group_exprs
    return [" && ".join(group_exprs)]


def _keyword_groups_to_tiers(keyword_groups, query=None, max_tiers=6):
    """由概念组本地生成从严到松的分层查询表达式（去重、按顺序）。"""
    groups = [group for group in (keyword_groups or []) if group]
    if not groups:
        return []

    tiers = []
    seen = set()

    def _push(expression):
        expression = _clean_expression(expression)
        if not expression:
            return
        key = _normalize_expression_key(expression)
        if key in seen:
            return
        seen.add(key)
        tiers.append(expression)

    single_concept = len(groups) == 1

    if single_concept:
        terms = []
        for group in groups:
            terms.extend(group)
        terms = _normalize_keywords(terms)
        if terms:
            _push(_quote_query_term(terms[0]))
        if len(terms) >= 2:
            _push(_terms_to_or_expression(terms[:2]))
        if len(terms) > 2:
            _push(_terms_to_or_expression(terms))
    else:
        primary_exprs = [_quote_query_term(group[0]) for group in groups if group]
        if len(primary_exprs) >= 2:
            _push(" && ".join(primary_exprs))
        expanded_exprs = [_terms_to_or_expression(group) for group in groups]
        expanded_exprs = [item for item in expanded_exprs if item]
        if len(expanded_exprs) >= 2:
            _push(" && ".join(expanded_exprs))
        for drop in range(1, len(expanded_exprs) - 1):
            kept = expanded_exprs[: len(expanded_exprs) - drop]
            if len(kept) >= 2:
                _push(" && ".join(kept))
            elif kept:
                _push(kept[0])
        all_terms = []
        for group in groups:
            all_terms.extend(group)
        _push(_terms_to_or_expression(all_terms))

    if max_tiers and max_tiers > 0:
        tiers = tiers[:max_tiers]
    return tiers


def _is_single_concept_query(query):
    text = str(query or "").strip()
    if not text:
        return False
    if any(marker in text for marker in ("&&", "||", "，", ",", "；", ";", "和", "与", "及", "以及")):
        return False
    return len(re.findall(r"[\w\u4e00-\u9fff]+", text)) <= 1


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
    max_total=DEEP_SEARCH_MAX_TOTAL,
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
    # Deep-search planner uses explicit ||/&& expressions, so preserve the v1
    # implicit-OR semantics for its subprocess invocations regardless of the
    # user-facing default.
    command.append("--implicit-or")
    # Bound total hits per iteration so a broad LLM-planned query cannot
    # blow up the caller. Pass max_total=0 to disable.
    cap = 0 if max_total is None else int(max_total)
    command.extend(["--max-total", str(max(cap, 0))])
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
    """合并去重并按 tier 层级（更严格的 tier 优先）与匹配的关键字组数量排序。

    如果一条结果被多个两两组合查询命中，说明它匹配了更多的概念组，
    排序时优先展示（按 from_query 数量降序）；同时更早（更严格）的 tier 排在前面。
    """
    deduped = {}
    for item in matches:
        key = (item.get("path", ""), item.get("line_number"), item.get("text", ""))
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = item
        elif existing.get("is_context") and not item.get("is_context"):
            # 保留非上下文匹配，但仍合并 tier / from_query 信息
            merged = item
            _merge_query_info(merged, existing)
            deduped[key] = merged
        else:
            _merge_query_info(existing, item)

    # 清理临时字段并计算匹配深度
    results = []
    for item in deduped.values():
        from_queries = item.get("from_query")
        if isinstance(from_queries, (set, list)):
            item["_match_depth"] = len(from_queries)
        else:
            item["_match_depth"] = 1
        tiers = item.get("from_tier")
        if isinstance(tiers, (set, list)) and tiers:
            item["_best_tier"] = min(int(t) for t in tiers if isinstance(t, int))
        elif isinstance(tiers, int):
            item["_best_tier"] = tiers
        else:
            item["_best_tier"] = 0
        item.pop("from_query", None)
        item.pop("from_tier", None)
        results.append(item)

    return sorted(results, key=_match_sort_key)


def _merge_query_info(target, other):
    existing_queries = set()
    if isinstance(target.get("from_query"), str):
        existing_queries.add(target["from_query"])
    elif isinstance(target.get("from_query"), (set, list)):
        existing_queries.update(target["from_query"])
    if isinstance(other.get("from_query"), str):
        existing_queries.add(other["from_query"])
    elif isinstance(other.get("from_query"), (set, list)):
        existing_queries.update(other["from_query"])
    if existing_queries:
        target["from_query"] = existing_queries

    existing_tiers = set()
    for source in (target.get("from_tier"), other.get("from_tier")):
        if isinstance(source, int):
            existing_tiers.add(source)
        elif isinstance(source, (set, list)):
            for value in source:
                if isinstance(value, int):
                    existing_tiers.add(value)
    if existing_tiers:
        target["from_tier"] = existing_tiers


def _match_sort_key(item):
    """排序键：先按更严格 tier 升序，再按匹配深度降序，再按文件路径、行号升序。"""
    line_number = item.get("line_number")
    if not isinstance(line_number, int):
        line_number = 0
    tier = item.get("_best_tier", 0)
    if not isinstance(tier, int):
        tier = 0
    depth = -(item.get("_match_depth", 1))
    return (tier, depth, str(item.get("path", "")), line_number, 1 if item.get("is_context") else 0, str(item.get("text", "")))


def _normalize_expression_key(expression):
    return re.sub(r"\s+", " ", str(expression or "").strip())
