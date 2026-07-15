# oom_build_2026_07_14 — memory profile report

Generated from 7 runs (7 clean, 0 errored).

## 1. Peak RSS by (corpus, config)

| corpus | config | runs | corpus_mb | wall_s p50 | peak_rss p50 | peak phase | ru_max p50 | tm_traced? |
|---|---|---:|---:|---:|---:|---|---:|:-:|
| M | i2_rep_n4 | 3 | 21.6 | 490.4 | 2473.7 | ngram_iter_start | 2477.9 | n |
| S | i2_rep_n4 | 4 | 2.6 | 61.1 | 570.6 | pairs_scan_end | 570.6 | y |

## 2. Where RSS grew (per-phase Δru_maxrss, MB)

Rows aggregated across runs of the same (corpus, config); median Δ shown.

| corpus | config | phase | Δ MB p50 | extras (sample) |
|---|---|---|---:|---|
| M | i2_rep_n4 | scan_start | +28.5 |  |
| M | i2_rep_n4 | scan_end | +81.5 | total_docs=1 total_chunks=12254 |
| M | i2_rep_n4 | ngram_iter_end | +1184.0 | iter=1 iters=2 chunks=12254 phrases=33977 |
| S | i2_rep_n4 | scan_start | +28.5 |  |
| S | i2_rep_n4 | scan_end | +9.1 | total_docs=1 total_chunks=1360 |
| S | i2_rep_n4 | ngram_iter_end | +198.1 | iter=1 iters=2 chunks=1360 phrases=2893 |
| S | i2_rep_n4 | pairs_scan_end | +155.0 | surviving=182025 |

## 3. Peak RSS vs corpus size (baseline config)

| corpus_mb | peak_rss_mb | ratio |
|---:|---:|---:|
| 2.6 | 570.6 | 220.3× |
| 21.6 | 2473.7 | 114.3× |

Linear fit: `peak_rss_mb = 99.9 × corpus_mb + 311.9` (R² = 1.000).

Extrapolation at 100 MB corpus → **10302 MB peak RSS**.
Extrapolation at 500 MB corpus → **50260 MB peak RSS**.

## 4. Measurement consistency (sampler vs ru_maxrss)

| corpus | config | sampler_peak | ru_maxrss | delta MB | ratio |
|---|---|---:|---:|---:|---:|
| M | i2_rep_n4 | 2473.7 | 2478.2 | -4.5 | 0.998 |
| M | i2_rep_n4 | 2473.8 | 2477.9 | -4.1 | 0.998 |
| M | i2_rep_n4 | 2473.4 | 2477.8 | -4.4 | 0.998 |
| S | i2_rep_n4 | 570.4 | 570.4 | +0.0 | 1.000 |
| S | i2_rep_n4 | 570.8 | 570.8 | +0.0 | 1.000 |
| S | i2_rep_n4 | 570.5 | 570.5 | +0.0 | 1.000 |
| S | i2_rep_n4 | 1533.3 | 1605.6 | -72.3 | 0.955 |

## 5. Python-heap vs total RSS (tracemalloc runs)

How much of the peak RSS is Python-allocator-visible objects?

| corpus | config | phase | tm_peak MB | ru_maxrss MB | py_share |
|---|---|---|---:|---:|---:|
| S | i2_n4 | scan_start | 2.9 | 31.5 | 9.33% |
| S | i2_n4 | scan_end | 13.3 | 43.5 | 30.53% |
| S | i2_n4 | ngram_iter_start | 9.3 | 43.5 | 21.29% |
| S | i2_n4 | ngram_iter_end | 336.4 | 693.9 | 48.48% |
| S | i2_n4 | ngram_iter_start | 60.9 | 693.9 | 8.78% |
| S | i2_n4 | ngram_iter_end | 358.9 | 745.7 | 48.14% |
| S | i2_n4 | ngram_finalize_start | 61.5 | 745.7 | 8.25% |
| S | i2_n4 | ngram_finalize_end | 136.2 | 745.7 | 18.26% |
| S | i2_n4 | stopword_derived | 136.5 | 745.7 | 18.31% |
| S | i2_n4 | materialise_start | 136.4 | 745.7 | 18.29% |
| S | i2_n4 | pairs_start | 183.5 | 745.7 | 24.61% |
| S | i2_n4 | pairs_scan_start | 174.7 | 745.7 | 23.43% |
| S | i2_n4 | pairs_scan_end | 496.2 | 1546.7 | 32.08% |
| S | i2_n4 | pairs_done | 496.3 | 1605.6 | 30.91% |
| S | i2_n4 | done | 174.8 | 1605.6 | 10.89% |
