# 方案：sidecar 缓存根迁移到 XDG cache（方案 A）

> **状态**：待实施（设计已拍板）
> **日期**：2026-09-11
> **决策人**：用户（方案 A）
> **关联代码**：`src/grga/paths.py`（新增 `resolve_cache_dir`）、`src/grga/structure/sidecar.py`（改默认根）
> **关联文档**：`docs/env-vars.md`、`skills/grga/references/troubleshooting.md`、`docs/design/tokenization.md`
> **前置**：数字短语方案已落地（`8a96a24`）

---

## 0. TL;DR

把 Markdown 结构 sidecar 缓存的默认根从 `Path.cwd() / ".grga" / "structure"` 改为 XDG cache 目录（`$XDG_CACHE_HOME/grga/structure`，平台默认 `~/.cache/grga/structure`），避免在每个运行 grga 的目录散落 `.grga/`。

**保留 `GRGA_SIDECAR_ROOT`** 作为 sidecar 专属覆盖（最高优先级），用户要拷贝缓存时直接拷该目录。

---

## 1. 问题背景

### 1.1 现象

在每个运行 grga 的目录下都会生成 `.grga/structure/` 缓存目录，污染用户目录。

### 1.2 根因

`structure/sidecar.py` 的 `default_sidecar_root()`：

```python
def default_sidecar_root() -> Path:
    override = os.environ.get("GRGA_SIDECAR_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.cwd() / ".grga" / "structure"   # ← 在 cwd 下散落
```

### 1.3 为什么改

- sidecar 是**证据层缓存**（Markdown 章节树），不是知识资产（`keyword_pairs`/`auto_ngram_dict`）——符合"知识可迁移、证据可丢弃"哲学
- 文件名已 hash 化（`sha1(绝对路径)[:16]` + stem 前 32 字符），**身份信息全在文件名里**，集中存放零冲突
- grga 已有 `paths.py` 中央路径解析（tools → `XDG_DATA_HOME`，state → `XDG_STATE_HOME`），cache 也该走同一模式
- 用户明确：**语料转移不是问题**，需要时把缓存目录一起拷走即可

---

## 2. 决策记录（用户拍板）

| # | 决策点 | 结论 | 理由 |
|---|---|---|---|
| D1 | 方案 | **A：默认根改 XDG cache** | 一行核心改动 + 符合既有 XDG 惯例；文件名 hash 化已支持集中存放 |
| D2 | 语料转移 | **不是问题** | 有需要把缓存一起拷走即可；不需要保留 cwd 跟随语义 |
| D3 | `GRGA_SIDECAR_ROOT` | **保留**（最高优先级） | 既有 env 覆盖继续有效；测试全部依赖它隔离；拷贝缓存 = 拷这个目录 |

---

## 3. 方案设计

### 3.1 `paths.py` 新增 `resolve_cache_dir()`

遵循 `resolve_state_dir()` 的既有模式：

```python
def resolve_cache_dir():
    """Return the grga cache directory (sidecar structure docs, ...).

    Priority:
      1. ``$GRGA_CACHE_DIR`` (explicit override)
      2. ``$XDG_CACHE_HOME/grga``
      3. platform default cache dir
         * Linux / macOS: ``~/.cache/grga``
         * Windows:       ``%LOCALAPPDATA%\\grga\\cache``
    """
    override = _env("GRGA_CACHE_DIR")
    if override:
        return Path(override)

    xdg_cache = _env("XDG_CACHE_HOME")
    if xdg_cache:
        return Path(xdg_cache) / APP_NAME

    if _is_windows():
        local = _env("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / APP_NAME / "cache"

    return Path.home() / ".cache" / APP_NAME
```

### 3.2 `sidecar.py` 改默认根

```python
def default_sidecar_root() -> Path:
    """Resolve the sidecar root, honouring ``GRGA_SIDECAR_ROOT`` when set."""
    override = os.environ.get("GRGA_SIDECAR_ROOT")
    if override:
        return Path(override).expanduser()
    from grga.paths import resolve_cache_dir  # 或顶层 import（sidecar 无循环依赖）
    return resolve_cache_dir() / "structure"
```

**优先级**：`GRGA_SIDECAR_ROOT` > `GRGA_CACHE_DIR` > `XDG_CACHE_HOME` > 平台默认。

### 3.3 兼容性

- **测试**：全部测试已用 `monkeypatch.setenv("GRGA_SIDECAR_ROOT", ...)` 隔离，**不依赖 cwd 默认值**——零破坏
- **`test_sidecar.py::test_default_sidecar_root_uses_env`**：只测 env 覆盖，未测默认值——需**新增**默认值测试
- **既有 `.grga/structure/` 旧缓存**：不自动迁移（纯缓存，mtime 失效即重建）；文档注明旧目录可手动删除

---

## 4. 风险与对策

| 风险 | 等级 | 对策 |
|---|---|---|
| 旧 `.grga/structure/` 残留 | 低 | 纯缓存自动重建，文档注明可删 |
| 跨用户/跨机器缓存共享冲突 | 低 | hash 文件名含绝对路径，不同机器路径不同 → 各自独立 |
| 新默认值破坏测试 | 无 | 测试已全部显式设 `GRGA_SIDECAR_ROOT` |
| 与 `expand --level chapter` 冷启动交互 | 低 | 行为不变，只是缓存位置不同 |

---

## 5. 验收标准

### 5.1 `test_paths.py` 新增（复刻 `resolve_state_dir` 模式）

1. `resolve_cache_dir` 显式 `GRGA_CACHE_DIR` 覆盖
2. `GRGA_CACHE_DIR` 空串/纯空白忽略
3. `XDG_CACHE_HOME` 时 → `<xdg>/grga`
4. `GRGA_CACHE_DIR` 胜过 `XDG_CACHE_HOME`
5. posix 平台默认 → `~/.cache/grga`
6. windows 平台默认 → `%LOCALAPPDATA%\grga\cache`
7. windows 无 LOCALAPPDATA → `~/AppData/Local/grga/cache`
8. 不创建目录（解析不副作用）

### 5.2 `test_sidecar.py` 更新

9. `test_default_sidecar_root_uses_env` 保留
10. **新增**：无 env 时默认根 = `resolve_cache_dir() / "structure"`（monkeypatch `paths.resolve_cache_dir` 或 `XDG_CACHE_HOME`）

### 5.3 回归

11. 全量 pytest 全绿（现有 sidecar 测试零改动）
12. `test_sidecar.py` 其余测试（hash、mtime、cache hit、corrupt recovery）不变

---

## 6. 实施范围

- **改**：`src/grga/paths.py`（新增 `resolve_cache_dir` + docstring）
- **改**：`src/grga/structure/sidecar.py`（`default_sidecar_root` 改走 `resolve_cache_dir()`）
- **改**：`src/grga/env_loader.py`（注释：`GRGA_SIDECAR_ROOT` 说明更新 + 新增 `GRGA_CACHE_DIR` 模板行）
- **改**：`docs/env-vars.md`（新增 `GRGA_CACHE_DIR`；更新 `GRGA_SIDECAR_ROOT` 默认值说明）
- **改**：`skills/grga/references/troubleshooting.md`（`.grga/structure/` → `$GRGA_SIDECAR_ROOT` 或 cache 路径）
- **新增**：测试（上述 5.1/5.2）
- **不改**：`structure/md.py`、`expand.py`、`meta.py`、`graph/build.py`（它们只调 `load_or_build`，签名不变）、schema、CLI、契约

---

## 7. 决策留痕（为什么是这个方案）

- **为什么不是 B（内存 LRU）**：search 是热路径，进程冷启动重 parse 大量 md 有成本；落盘缓存 + mtime 失效已经够轻。用户没有要求进程内缓存。
- **为什么不是 C（并入索引 DB）**：无索引时 search 仍需轻量结构缓存；sidecar 是独立于索引的证据层，不该耦合。
- **为什么保留 `GRGA_SIDECAR_ROOT`**：全部测试依赖它隔离；用户要手动控制缓存位置/拷贝时可用；与 `GRGA_CACHE_DIR` 不冲突（前者更specific）。
- **为什么新增 `GRGA_CACHE_DIR` 而非直接复用 `GRGA_SIDECAR_ROOT`**：统一到 paths.py 中央解析，未来其他 cache（rga cache blob 等）可复用；sidecar 专属 override 保留给特定用途。
