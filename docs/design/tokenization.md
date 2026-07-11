# Tokenisation (schema v6): hybrid ASCII regex + jieba + auto n-gram

> **Status**: canonical. Introduced with schema v6 replacing the pure-regex
> tokeniser used through v1-v5.
> **Origin**: `experiments/autongram_2026_07_11/` — see REPORT.md for the
> full data and decisions.
> **Also read**: `docs/design/graph-l2-postmortem-2026-07-10.md` §4.4
> (铁律 2: 抗语义先验、允许数学恒等式加工).

## 0. TL;DR

`grga index build` tokenises with a **two-branch hybrid**:

- **ASCII / code identifiers** — regex `[A-Za-z][A-Za-z0-9_+#.-]{0,38}[A-Za-z0-9_+#]`.
  Preserves programming tokens (`foo-bar`, `v1.2.3`, `c++`, `install_docs.md`,
  `self.x`) as single units. Identical to the previous mainline regex ASCII
  branch, so code-search callers see **zero regression**.
- **CJK runs** — `jieba` in precise mode, seeded with a **per-scope auto
  dictionary** discovered by unsupervised n-gram phrase mining (frequency +
  NPMI + branch entropy). Long entities like `章北海` / `面壁者` /
  `万有引力号` / `联合国行星防御理事会` / `赫尔辛根默斯肯` / `PDC主席` /
  `艾AA` end up as one token instead of being greedily glued into 2-20 char
  garbage blobs.

The auto dictionary lives in the new `auto_ngram_dict` SQL table
(one row per phrase, per scope). Each `build` refines it in place (default
2 iterations, `--ngram-iters=N` to override).

## 1. Motivation (why replace the regex)

The pre-v6 regex `[\u4e00-\u9fff]{2,20}` was a greedy CJK-run matcher:

- **Long-sentence pollution**: 20 % of the term vocabulary on 三体 (2.7 MB)
  was 20-character sentence fragments (`一下将下方千亩大小的地方全都笼罩在了其` etc.);
  measured 24,322 such rows in the shared deep-search DB, all noise.
- **Seed loss by accident**: entity names like `罗辑` / `汪淼` / `云天明`
  only appeared as standalone tokens when they happened to be flanked by
  punctuation. `罗辑` df went from **4** (regex) to **855** (hybrid). Search
  recall for character names collapsed depending on typesetting luck.
- **Manual dictionaries do not scale**: prior experiments proved a 19-word
  manual jieba `user_dict` fixed the problem on《凡人修仙传》but nobody
  maintains one per scope.

Experiment autongram_2026_07_11 proved the fix is achievable with **pure
mathematical judgement, zero language priors** (see F5).

## 2. Pipeline

Per invocation of `build_graph_index`:

```
Pass 0 — enumerate files, split_chunks, cache text in memory.

Pass 1 — repeat `ngram_iters` times (default 2):
    load auto_dict[scope]                (empty on first pass ever)
    tokenizer = fresh jieba.Tokenizer + add_word(each dict phrase)
    for each chunk:
        tokenize_hybrid_with_pos    (ASCII regex + jieba CJK)
        merge_auto_phrases          (greedy longest match, positional
                                     adjacency required)
    detect_ngram_phrases(all lines) (frequency + NPMI + branch entropy)
    upsert new phrases into auto_dict
    persist auto_ngram_dict to SQL

Pass 2 — using the LAST chunk tokenisation:
    chunk_df, pair_count from _keywords_from_tokens(...)  (casefold + stopwords)
    write chunks / keyword_chunks / heading_docs
    materialise keyword_pairs[chunk_cooccur] with segment NPMI + min_pair_count
```

`jieba.Tokenizer()` (instance, not module singleton) means concurrent
builds and repeated test runs never leak dictionary state. Positional
adjacency in `merge_auto_phrases` prevents `"艾, AA"` (comma between) from
being folded into `"艾AA"`.

## 3. Detection judgement (pure mathematical gates)

Three gates, applied in order to every window of 2..`max_ngram_n` adjacent
tokens (frozen defaults):

| Gate | Formula | Default |
|---|---|---|
| Frequency | `count(w) >= min_count` | 3 |
| Solidity | `min over splits { NPMI(left, right) }` (Bouma 2009) | ≥ 0.3 |
| Branch entropy | `min(H_L(w), H_R(w))` nat | ≥ 1.0 |

Solidity uses **a single N = total token count** for every probability so
NPMI stays in `[-1, 1]` for any n. Splitting the N per n-level was tried
in the experiment; a unit test on a fully-co-occurring toy corpus
(`甲乙丙 × 5`) caught the bug — see `experiments/.../FINDINGS.md` F9.2.
Do not re-attempt without a bounds regression test.

### Two stop-word tables (do not merge them)

`grga.graph.build` maintains **two** disjoint stop-word concepts:

- `_CORE_STOP_BOUNDARIES` (26 characters, detection-only): only true
  structural particles (`的了和与或是在这那...`). Used as an **n-gram window
  boundary** so `罗辑` + `说` cannot form a bigram. **Must not** include
  `者` / `人` / `号` / `家` — those are 构词后缀 (`面壁者` / `破壁人` /
  `万有引力号`) and blocking them structurally kills recall (experiment F5).
- `_STOPWORDS` (small English + Chinese filler set, index-only): applied
  by `_keywords_from_tokens` when materialising `keyword_chunks`. Larger
  and free to be aggressive because it only filters *stored* keywords, not
  *possible phrases*.

## 4. Rejoin rule for mixed CJK / ASCII windows

Detected phrases are stored as strings; jieba consumes them via
`add_word`. Because Chinese typesetting puts CJK and ASCII directly
adjacent (`艾AA`, `PDC主席`) while consecutive English words are
space-separated (`nano center`), rejoin **per adjacent pair**:

| prev | next | separator | example |
|---|---|:---:|---|
| CJK | CJK | (none) | `章`+`北海` → `章北海` |
| CJK | ASCII | (none) | `艾`+`AA` → `艾AA` |
| ASCII | CJK | (none) | `PDC`+`主席` → `PDC主席` |
| ASCII | ASCII | single space | `nano`+`center` → `nano center` |

The experiment tried two whole-window rules (`"".join` and `" ".join`) and
both failed; **only** the pair-by-pair rule handles all four adjacencies
correctly. See `experiments/.../FINDINGS.md` F11.

## 5. Per-scope dictionary contract

`auto_ngram_dict` is keyed by `(dir_id, phrase)`. Two implications callers
must be aware of:

1. **Scope determines both search scope AND vocabulary.** Indexing a
   Chinese novel and a Rust codebase into the same scope will mix their
   dictionaries; index them under different scopes to keep vocabularies
   apart. The post-mortem 2026-07-10 §4.2 established that "hub is a
   sub-corpus attribute" — the dictionary follows the same reasoning.
2. `--append` preserves the current dictionary and merges new discoveries
   in (`INSERT OR REPLACE` on `(dir_id, phrase)`, so stats freshen on
   re-detection). `--replace` (the default) wipes the scope's dict rows
   along with its `keyword_chunks` / `chunks` rows.

## 6. Iteration count

The dictionary converges quickly:

| iter | phrases discovered on 三体 |
|---:|---:|
| 1 | 2124 (from a bare jieba) |
| 2 | 2615 (once 章北海-class phrases fold into single tokens) |
| 3 | +68 marginal |

Defaults to `ngram_iters=2`. Increase for larger corpora if `--json` output
shows the iteration-2 dict is still growing meaningfully.

## 7. Performance envelope

On 三体 (2.7 MB, 12,059 lines) end-to-end:

| tokeniser | build time |
|---|---:|
| old regex (v5) | ~0.5 s |
| hybrid + ngram iters=2 (v6) | ~35-40 s |

The dominant cost is jieba precise-mode segmentation (~5 s per pass; two
detection passes + one materialisation pass = ~15 s of tokenisation) plus
the SQL writes (much larger `keyword_chunks` / `keyword_pairs` due to
finer CJK segmentation). On code-only corpora the CJK branch is a no-op
and the overhead is much smaller.

## 8. Public API compatibility

- `extract_chunk_keywords(text) -> Counter`: retained. Still regex-based,
  so any external caller passing a per-chunk string gets identical output
  to the pre-v6 shape. New pipeline does **not** call it; internal callers
  went through `_keywords_from_tokens(hybrid_tokens)`.
- `build_graph_index(...)`: new keyword-only kwargs `ngram_iters` (default
  2) and `max_ngram_n` (default 4). Existing callers keep working with
  the frozen defaults.
- New public helpers (all in `grga.graph.build`):
  `tokenize_hybrid`, `tokenize_hybrid_with_pos`, `merge_auto_phrases`,
  `detect_ngram_phrases`, `_load_auto_dict`, `_persist_auto_dict`. The
  underscore-prefixed ones are load/save internals and may change; the
  three public ones are stable.

## 9. When to revisit

Re-open this decision only if one of the following changes:

- New empirical evidence that jieba tokenisation harms code-search recall
  in a way the ASCII regex branch does not cover.
- A caller task that actively wants ASCII phrase discovery (currently
  detection admits mixed CJK/ASCII windows but auto_dict merges only when
  the phrase re-appears in the source; a pure-ASCII compound like
  `"install docs"` would end up in the dict but never re-merge because
  jieba never sees ASCII text).
- Schema evolves past v6 in a way that makes per-scope vocab awkward.

Do **not** revisit for "let's shrink the stop-word list" or "let's tune
the NPMI/entropy thresholds by hand". Those knobs were burnt in by the
autongram experiment; the deviation cost is well documented and small
changes rarely improve, only introduce drift.
