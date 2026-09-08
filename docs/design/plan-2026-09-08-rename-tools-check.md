# plan-2026-09-08 · rename `grga tools check` → `grga tools status` + probe orphan fix

> 状态：**已确认，code agent 实施完成**。
> 触发：用户指令「grga check 可以合并到 grga tools 里吗？顺便看看 CLI 命令/子命令如何归组更好」 +
> `grga probe "小堆"` 卡住無任何输出超时。
> **结论先行**：不合并 `grga check` (top-level) —— 它是 search-stack viability gate，退出码≠0 有语义。**将 `grga tools check` 改名为 `grga tools status`**，消除同名歧义；`grga tools check` 作为 deprecated alias 保留 6 个月。

## 1 · 目标 (WHAT)

将 `grga tools check` 子命令更名为 `grga tools status`，输出 schema 与行为**原样不变**。旧命令名 `grga tools check` 保留为 deprecated alias（stderr 打印 deprecation warning，委派到 `status` 逻辑）。`grga check` (顶层) **不作任何改动**。

## 2 · 背景与理由

| 命令 | 功能 | 信赖链 | 退出码 |
|---|---|---|---|
| `grga check` (top-level) | 检查 rg/ra/pandoc 三棵树是否可用 | `search.check_environment()` | **≠0 on missing rg/ra** |
| `grga tools check` | 列出全部 6 个托管工具 (path/source/version) | `toolchain.check_tools()` | **0 always** (只报告) |

两条 `check` 命名冲突，却服务**不同 caller persona**：
- daily caller / agent: "能搜吗？" → `grga check` (exit-driven gate)
- tool admin / CI: "装了什么？来自哪儿？版本号？" → `grga tools check` (inventory)

grga skill 的 reflection loop 使用 `probe/search/expand/related/suggest` + `graph inspect`，**不使用任何 `check` 命令** → 本次改动对 skill 套件**零影响**。

## 3 · 硬约束 (Constraints)

- **Additive 原则** (grga CLI 演进偏好): `grga tools check` 必须**继续工作**，不能 break 任何现有 invocation。
- `grga check` (top-level) 代码路径**完全不动**。
- Deprecated alias 必须**至少**向 stderr 打印 deprecation 警告。
- `status` 与 `check` 在 `--json` 输出上**结构相同** (schema 字段名/值不变)。
- py_compile 必须通过。
- 现有 141 项测试**零回归**。

## 4 · 实施清单 (WHAT to change)

| # | 文件 | 改动 |
|---|---|---|
| 1 | `src/grga/cli.py` | 将 `tools_check_command` → `tools_status_command`，注册名 `"check"` → `"status"`；新增 `@tools_app.command("check")` deprecated alias，打印警告后委派 |
| 2 | `docs/CLI.md` | L33/L563/L569/L677/L680/L690: `grga tools check` → `grga tools status`；L33 命令概览表同步 |
| 3 | `docs/install-and-usage.md` | L80: `grga tools check` → `grga tools status` |
| 4 | `docs/dev-release.md` | L56: `grga tools check --json` → `grga tools status --json` |
| 5 | `docs/api.md` | L680/L683: 更新标题 + 字段描述 |
| 6 | `CHANGELOG.md` | `[Unreleased]` 下加 `###` 条目 |
| — | `docs/design/decisions.md` L595/611 | **不改** —— 历史决策记录 |
| — | `docs/design/release-0.2.0-plan.md` L119/245 | **不改** —— 历史计划归档 |

> ⚠️ decisions.md / release-0.2.0-plan.md 里的 `grga tools check` 引用是**历史语境** (记录过去的设计决策/计划)，不是当前 CLI 契约。改名它们只会让历史文档失真。**跳过。**

### cli.py 结构建议 (HOW 留给 code agent)

```python
@tools_app.command("status")
def tools_status_command(json_output=..., prefer_system=...):
    """Report which managed tools are installed and where they resolve from."""
    ...  # 原 tools_check_command 逻辑原封不动

@tools_app.command("check")
def tools_check_deprecated(json_output=..., prefer_system=...):
    """Deprecated alias for `grga tools status`."""
    typer.echo("[grga] `tools check` is deprecated — use `tools status` instead.", err=True)
    tools_status_command(json_output=json_output, prefer_system=prefer_system)
```

## 5 · 边界维度 (8-dim checklist)

- **空集**: `GRGA_TOOLS_DIR` 为空 / 没装任何工具 → `status` 输出 `missing`; alias 仍工作 ✓
- **单元素**: 只装 `rg` → 输出 5 个 missing ✓
- **唯一性**: 无变更，不涉及
- **冲突**: 两个命令名 `check`/`status` 共存 → alias 委托即可 ✓
- **重叠**: 无变更
- **自反**: `status` 输出不包含 `status` itself ✓
- **可结合性**: `--json` + `--prefer-system` 组合在 alias 上传递 ✓
- **降级路径**: alias 失败(不该失败,就是委托)→ 用户看到 deprecation warning + 原输出 ✓

## 6 · 验收标准 (Executable)

```bash
# code agent 在 /data/myrepo/grga 下跑：

# 1. py_compile
python3 -c "import ast,sys; ast.parse(open('src/grga/cli.py').read())" && echo OK

# 2. help 显示 status 而非 check 作为主命名
python3 -m grga tools --help 2>&1 | grep -E 'status|check'

# 3. status 输出 schema 与旧 check 相同
python3 -m grga tools status --json | python3 -c "import json,sys; d=json.load(sys.stdin); assert set(d)=={'platform','tools_dir','tools'}" && echo OK

# 4. 别名打印 deprecation warning + 仍输出正确结果
python3 -m grga tools check --json 2>/tmp/stderr | python3 -c "import json,sys; d=json.load(sys.stdin); assert set(d)=={'platform','tools_dir','tools'}"
[grga] `tools check` is deprecated in /tmp/stderr && echo OK

# 5. grga check (top-level) 完全不受影响
python3 -m grga --help 2>&1 | grep 'check'  # 应仍列 check

# 6. 现有测试零回归
python3 -m pytest tests/ -q 2>&1 | tail -5
```
