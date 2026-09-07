# Plan: index build 支持 include/exclude glob 文件过滤

**日期**：2026-09-07
**状态**：语义已拍板（弱语义 + 宽松匹配不区分大小写），可委派 code agent
**范围**：`grga index build` / `grga index rebuild` 新增两个可选参数

---

## 1. 目标

让 `index build` / `index rebuild` 支持按文件名/路径模式过滤语料：

- **包含 glob**：只索引匹配的文件
- **排除 glob**：跳过匹配的文件（优先级高于包含）

**动机**：当前文件枚举只按后缀白名单 + `--max-file-kb` 过滤，无法排除
"某些目录/文件永远不进库"（如临时目录、日志、生成物、外语文件）。
用户 09-07 提出该需求，明确复用 search 侧 `_normalize_glob_patterns`
的逻辑。

**非目标**：
- 不改 search / probe / related 等读侧动词
- 不新增 glob 语法（不发明第三种模式）
- 不改变知识资产（auto_ngram_dict / keyword_pairs）的既有生命周期

---

## 2. 接口契约

### 2.1 CLI 参数（`index build` 和 `index rebuild` 都加）

| 参数 | 类型 | 语义 |
|---|---|---|
| `--include-glob` | 可重复（`List[str]`） | 只索引匹配**任一** pattern 的文件；不传 = 全收 |
| `--exclude-glob` | 可重复（`List[str]） | 匹配**任一** pattern 的文件被跳过；优先级高于 include |

- 两个参数都复用 `_normalize_glob_patterns` 的归一化逻辑（字符串按
  shlex 拆成 pattern 列表、去空白、去空串）
- 匹配对象：**相对 scope 根目录（`--directory`）的路径**（`rel_path`），
  与 build 内部 `seen_rel_paths` / `_classify_doc` 使用的路径一致
- 匹配方式：**宽松匹配（fnmatch 语义，`*` 跨 `/`）**，**不区分大小写**

### 2.2 语义优先级

```
include_globs 为空 → 全部候选文件通过 include 检查
include_globs 非空 → 必须命中至少一个
exclude_globs 非空 → 命中任一即排除（exclude 赢）
```

### 2.3 增量语义（已拍板：弱语义）

**被 glob 排除的文件只"不参与本次 build"，不代表从库中移除。**

- 排除文件的**既有索引行（keyword_chunks / keyword_docs / docs /
  heading_docs）原样保留**，不被删除
- `keyword_pairs` 的 npmi 仍基于**完整语料**计算（因为所有文件的
  `keyword_chunks` 行都在，未删除），不因本次过滤而收缩
- `auto_ngram_dict` 不受影响（短语是知识资产，不随本次过滤变动）
- 下次 build 去掉 glob 后，被排除文件重新参与——无需重建旧行

**⚠️ 与现有 removed 机制的冲突（必须处理的陷阱）**：
build 模式现有逻辑用 `seen_rel_paths`（本扫描实际 yield 的文件）判定
"DB 里有但磁盘上没有 = 消失 → 删除行"。若 glob 排除的文件不进入
`seen_rel_paths`，会被**误判为消失而被删**——违反弱语义。
**硬约束**：被 glob 排除但存在磁盘上的文件，必须计入"本扫描见过"，
不得进入 removed 路径。（机制如何实现——如扩展 skip 回调携带路径让
caller 计入 seen_rel_paths——是 code agent 的实现自由。）

---

## 3. 边界行为（8 维清单）

| 维度 | 期望行为 |
|---|---|
| **空集** | 所有文件都被 exclude → build 成功、无新增、**不删任何既有行**（弱语义：库保持原样，`keyword_pairs` 不重算） |
| **单元素** | 只有一个文件命中 include → 只索引它 |
| **唯一性** | glob 匹配基于 rel_path，与 scope 无关（不涉及跨 scope） |
| **冲突** | include 与 exclude 同时命中同一文件 → **exclude 赢**（明确优先级） |
| **重叠** | include 命中 ⊆ 被 exclude 命中 → 文件被排除 |
| **自反** | 不适用（glob 不产生"结果"概念） |
| **可结合性** | `--include-glob a --include-glob b` = OR；`--exclude-glob a --exclude-glob b` = OR；include + exclude = AND NOT |
| **降级路径** | ① `--directory` 指向**单个文件**：glob 过滤**应用**（单文件被 exclude → 本次 build 无输入，但既有行保留）；② glob 语法错误：不 crash，按不匹配处理（复用 search 侧容错，`shlex.split` 失败回退 `split()`）；③ `rebuild` 模式：全清后只重建匹配文件（弱语义下 rebuild 的"清空"是 rebuild 自身语义，与 glob 无关） |

### 3.1 需要额外覆盖的边界

- **batch_chunks > 0 + build 模式**：batched 分支已有 removed 处理
  （注释称"not currently exercised"），glob 过滤会首次锻炼该路径——
  测试必须覆盖 build+batch+glob 组合，且验证排除文件行保留
- **`--max-file-kb` 与 glob 的叠加**：先过后缀白名单 → 再过 glob → 再过
  大小门槛（glob 先于大小，因为 glob 是路径级、更便宜）。注：`too_large`
  文件走现有 removed 路径（与 glob 无关，是既有行为，本次不改）
- **`.` 开头的隐藏文件**：现有逻辑直接跳过（`name.startswith(".")`），
  glob 不改变这一行为
- **大小写**：`*.MD` 能匹配 `foo.md`；`Docs/*.txt` 能匹配 `docs/a.txt`
- **宽松匹配限制**：`docs/*.md` 也会匹配 `docs/sub/b.md`（`*` 跨 `/`），
  这是**已知且接受的** v1 语义（与 rga `--iglob '*.md'` 用户心智一致），
  写入文档，不视为 bug

---

## 4. 已拍板决策（2026-09-07）

| 决策点 | 结论 |
|---|---|
| 排除文件的旧行 | **弱语义**：保留，不删除（用户明确拍板） |
| 匹配引擎 | **fnmatch 宽松匹配**（`*` 跨 `/`），零依赖 |
| 大小写 | **不区分大小写**（用户明确拍板） |
| 单文件 `--directory` | **应用 glob 过滤** |

---

## 5. 验收标准（机械验证）

### 5.1 单元测试（tests/unit/）

1. `iter_text_files` 传 include/exclude globs：
   - include 命中 → 收；include 未命中 → 不收
   - exclude 命中 → 不收
   - include 与 exclude 同时命中 → 不收（exclude 赢）
   - include 为空 → 全收（现有行为不变）
   - 单文件 scope + 被 exclude → 无文件 yield
2. **大小写**：pattern `*.MD` 匹配 `foo.md`；`*.MD` 匹配 `foo.MD`；
   `README*` 匹配 `readme.md`
3. `_normalize_glob_patterns` 复用验证：字符串 / 多 pattern / shlex 失败回退

### 5.2 集成测试（tests/integration/）——弱语义核心验证

1. **排除文件行保留（最关键的验收）**：建 scope（含 a.md / b.md）→
   `--exclude-glob 'b.md'` build → **b.md 的既有行仍在**（`graph inspect`
   或 DB 行数验证 b 的 keyword_docs 未删），且 b 未被重新读入
2. **可恢复性**：exclude build 后去掉 glob 再 build → b 正常参与
3. **include 过滤**：`--include-glob 'a.md'` build → 只索引 a
4. **build + batch + glob**：组合跑通，排除文件行保留
5. **rebuild + glob**：全清后只重建匹配文件
6. **大小写匹配集成**：`--exclude-glob '*.MD'` 排除 `b.md`

### 5.3 文档更新

- `docs/build-tuning.md`：参数表加两行 + 说明"glob 过滤是弱语义（只影响
  本次 build 读入，不移除既有行）"+"宽松匹配（`*` 跨目录、不区分大小写）"
- `docs/CLI.md`：`index build` / `index rebuild` 参数列表

### 5.4 回归

- 全量 pytest 通过（现有 141 项 + 新增）
- 不传 glob 时行为与现状 bit-exact（无 glob 参数 → `_normalize_glob_patterns`
  返回空 → include 全收 / exclude 空，removed 逻辑行为不变）

---

## 6. 硬约束

- **不引入外部库**（glob 匹配用标准库）
- **不改变现有无 glob 调用路径的行为**（默认参数空列表）
- **不改变 read-side 动词**（search / probe / related / expand）
- **参数归一化复用 `_normalize_glob_patterns`**（不复制一份）
- **auto_ngram_dict 生命周期不变**
- **弱语义硬约束**：被 glob 排除且存在于磁盘的文件，**不得被 removed
  路径删除**（必须计入"本扫描见过"）
- **大小写不敏感匹配**（无论运行平台）

---

## 7. 决策留痕

- 09-07 用户提出：`index build` 时用 glob 过滤文件；两个参数（include /
  exclude），复用 `_normalize_glob_patterns` 逻辑
- 09-07 分析：最初推荐强语义（排除 = 移除，利用 `seen_rel_paths` →
  `removed_doc_ids` 机制），**用户否决**，明确选择弱语义：排除文件只不参与
  本次 build，不从库中移除
- 09-07 用户拍板：宽松匹配（fnmatch）、不区分大小写；单文件 scope 应用
  glob（按建议）
- 09-07 识别陷阱：弱语义与现有 removed 检测机制冲突（被排除文件会被
  `seen_rel_paths` 缺失误判为消失）——升级为硬约束，委派时须显式覆盖
