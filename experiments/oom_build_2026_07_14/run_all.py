"""Matrix driver for oom_build_2026_07_14.

Runs the memory profiler ``bench.py`` across a (corpus, config) matrix in
isolated subprocesses. Each cell runs ``BENCH_RUNS`` times (default 3).

Environment variables:

* ``BENCH_RUNS``            — repetitions per cell (default 3)
* ``BENCH_ONLY``            — comma-separated corpus labels to include
                              (default: S,M — L/XL synthesised on demand)
* ``BENCH_CONFIGS``         — comma-separated config labels (see CONFIGS below)
* ``BENCH_TRACEMALLOC``     — corpus labels where tracemalloc runs
                              (default: S only; adds ~4x overhead per F0)
* ``BENCH_SAMPLER_INTERVAL``— sampler polling (default 0.1s)
* ``BENCH_TIMEOUT_SEC``     — per-run timeout (default 1800s)

Corpora resolution:

* ``S``  → ``/media/lqw/DATA/sync/temp/三体.txt`` (2.6 MB)
* ``M``  → ``/media/lqw/DATA/sync/temp/《凡人修仙传》.txt`` (22 MB)
* ``L``  → merged S+M in a temp dir (~24 MB, exercises multi-doc path)
* ``XL`` → 3× duplication of L (~72 MB) synthesised on demand

Results land in ``data/results/<corpus>_<config>_run<i>.json``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "data" / "results"
CORPORA_DIR = HERE / "data" / "corpora"

PY = "/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3"

SANTI = Path("/media/lqw/DATA/sync/temp/三体.txt")
FANREN = Path("/media/lqw/DATA/sync/temp/《凡人修仙传》（精校全本）作者：忘语.txt")


CONFIGS = {
    "baseline":     dict(ngram_iters=2, refresh_ngram="replace", max_ngram_n=4),
    "iters1":       dict(ngram_iters=1, refresh_ngram="replace", max_ngram_n=4),
    "never":        dict(ngram_iters=0, refresh_ngram="never",   max_ngram_n=4),
    "ngram_n3":     dict(ngram_iters=2, refresh_ngram="replace", max_ngram_n=3),
    "ngram_n5":     dict(ngram_iters=2, refresh_ngram="replace", max_ngram_n=5),
}


def _assert_source_exists(src: Path) -> None:
    """Fail loudly if a corpus source file is missing / unreadable.

    Symlink-based L synthesis silently produces zero readable files when
    the source moves — a stale bench run then reports a tiny "peak" with
    no error, which can burn a debugging cycle. Catch it here.
    """
    if not src.is_file():
        raise FileNotFoundError(
            f"corpus source missing: {src} — set SANTI / FANREN in run_all.py "
            f"or place the file at that path"
        )


def _synth_L(dst: Path) -> Path:
    """Build the L corpus: symlink S + M into a shared dir.

    Validates each source exists before linking, and re-creates broken
    symlinks (e.g. after the underlying source moved). Bare
    ``link.exists()`` returns False for a broken symlink but
    ``link.symlink_to`` would then raise FileExistsError, so we clear
    dangling links first.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for src in (SANTI, FANREN):
        _assert_source_exists(src)
        link = dst / src.name
        if link.is_symlink() and not link.exists():
            link.unlink()          # dangling: clean up before relink
        if not link.exists():
            link.symlink_to(src)
    return dst


def _synth_XL(dst: Path, copies: int = 3) -> Path:
    """Build the XL corpus: copy the L set ``copies`` times with unique names.

    Uses content copies (not symlinks) so ripgrep / iter_text_files sees
    them as distinct docs. Skips work if already present.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for i in range(copies):
        for src in (SANTI, FANREN):
            _assert_source_exists(src)
            out = dst / f"copy{i}_{src.name}"
            if not out.exists():
                shutil.copy2(src, out)
    return dst


def _count_readable_txt(corpus: Path) -> int:
    """Return the number of .txt files that would actually be indexed."""
    if corpus.is_file():
        return 1 if corpus.suffix.lower() == ".txt" else 0
    return sum(1 for p in corpus.rglob("*.txt")
               if p.is_file() and p.stat().st_size > 0)


def resolve_corpus(label: str) -> Path:
    if label == "S":
        _assert_source_exists(SANTI)
        corpus = SANTI
    elif label == "M":
        _assert_source_exists(FANREN)
        corpus = FANREN
    elif label == "L":
        corpus = _synth_L(CORPORA_DIR / "L")
    elif label == "XL":
        corpus = _synth_XL(CORPORA_DIR / "XL", copies=3)
    else:
        raise ValueError(f"unknown corpus label: {label!r}")
    n = _count_readable_txt(corpus)
    if n == 0:
        raise RuntimeError(
            f"corpus {label!r} resolved to {corpus} but has 0 readable .txt "
            f"files — refusing to run a bench cell over an empty corpus"
        )
    return corpus


def run_cell(
    corpus_label: str,
    config_label: str,
    *,
    runs: int,
    use_tracemalloc: bool,
    sampler_interval: float,
    timeout: int,
) -> None:
    corpus_path = resolve_corpus(corpus_label)
    cfg = CONFIGS[config_label]
    for i in range(runs):
        out = RESULTS_DIR / f"{corpus_label}_{config_label}_run{i}.json"
        if out.exists():
            print(f"[SKIP] {out.name} exists")
            continue
        args = [
            PY, str(HERE / "bench.py"),
            "--corpus", str(corpus_path),
            "--corpus-label", corpus_label,
            "--out", str(out),
            "--run-id", str(i),
            "--ngram-iters", str(cfg["ngram_iters"]),
            "--refresh-ngram", cfg["refresh_ngram"],
            "--max-ngram-n", str(cfg["max_ngram_n"]),
            "--sampler-interval", str(sampler_interval),
        ]
        if use_tracemalloc:
            args.append("--tracemalloc")
        print(f"[RUN ] {out.name}  cfg={cfg}  tracemalloc={use_tracemalloc}")
        t0 = time.perf_counter()
        try:
            r = subprocess.run(args, timeout=timeout, capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            print(f"[TOUT] {out.name}  after {timeout}s")
            # Write a stub so analyze.py sees the cell existed but ran out of budget.
            out.write_text(json.dumps({
                "corpus_label": corpus_label,
                "config_label": config_label,
                "run_id": i,
                "error": f"timeout_after_{timeout}s",
                "config": cfg,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            continue
        dt = time.perf_counter() - t0
        status = "OK" if r.returncode == 0 else f"EXIT{r.returncode}"
        # SIGKILL from OOM killer is -9.
        oom_hint = " OOM?" if r.returncode < 0 else ""
        print(f"[{status:>4}] {out.name}  {dt:6.1f}s{oom_hint}")
        if r.returncode != 0 and not out.exists():
            # bench.py crashed before writing — record what we know.
            out.write_text(json.dumps({
                "corpus_label": corpus_label,
                "config_label": config_label,
                "run_id": i,
                "error": f"exit_{r.returncode}",
                "stderr_tail": r.stderr[-2000:] if r.stderr else "",
                "config": cfg,
            }, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    only = os.environ.get("BENCH_ONLY", "S,M").split(",")
    configs = os.environ.get("BENCH_CONFIGS", "baseline,iters1,ngram_n3").split(",")
    tm_labels = set(os.environ.get("BENCH_TRACEMALLOC", "S").split(","))
    runs = int(os.environ.get("BENCH_RUNS", "3"))
    interval = float(os.environ.get("BENCH_SAMPLER_INTERVAL", "0.1"))
    timeout = int(os.environ.get("BENCH_TIMEOUT_SEC", "1800"))

    only = [c.strip() for c in only if c.strip()]
    configs = [c.strip() for c in configs if c.strip()]
    for corpus_label in only:
        for config_label in configs:
            if config_label not in CONFIGS:
                print(f"[WARN] unknown config {config_label}, skipping")
                continue
            run_cell(
                corpus_label, config_label,
                runs=runs,
                use_tracemalloc=(corpus_label in tm_labels),
                sampler_interval=interval,
                timeout=timeout,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
