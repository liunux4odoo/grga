"""Generate L synthetic corpus: 10 pseudo-independent files by taking M
corpus (凡人修仙传), shuffling lines within each file, and replacing
~15% of tokens with vocab-external synonyms (simulate content variety
across an author's works).

Ensures file-aligned batching gets meaningful multi-file input.
"""
import os, random, re, sys
from pathlib import Path

random.seed(20260716)
src = Path("/media/lqw/DATA/sync/temp/《凡人修仙传》（精校全本）作者：忘语.txt")
out_dir = Path(sys.argv[1])
out_dir.mkdir(parents=True, exist_ok=True)
for f in out_dir.glob("*.txt"):
    f.unlink()

text = src.read_text(encoding="utf-8", errors="replace")
lines = [ln for ln in text.splitlines() if ln.strip()]
print(f"source: {len(lines)} non-empty lines")

# Fake vocab replacement pool — CJK chars unlikely to be in the source
# (so we introduce genuine new tokens, not just re-arrange)
EXTRA_POOL = list("骉犇羴淼焱鑫贔猋鱻掱赑龘靐飝雥虪犴虤蠿虪叕吅哵咍囍嚞囯圕壱毊灥燚玊珜珶珛珙珃玊瑆瑈瑃瑺瓁甠")

def _mutate_line(ln, p=0.15):
    out = []
    for ch in ln:
        if '\u4e00' <= ch <= '\u9fff' and random.random() < p:
            out.append(random.choice(EXTRA_POOL))
        else:
            out.append(ch)
    return "".join(out)

# Each of N files is a FULL variant of M (shuffle+mutate independently)
# so total L ≈ N × |M|. This produces file-diversity for file-aligned
# batching, not just row-diversity.
N = 10
for i in range(N):
    piece = lines[:]                                     # full copy
    random.shuffle(piece)                                # each file shuffled differently
    piece = [_mutate_line(ln) for ln in piece]           # ~15% char mutations
    out = out_dir / f"part{i:02d}.txt"
    out.write_text("\n".join(piece), encoding="utf-8")
    print(f"  {out.name}: {len(piece)} lines")

total_bytes = sum(f.stat().st_size for f in out_dir.glob("*.txt"))
print(f"total: {N} files, {total_bytes/1e6:.1f} MB")
