"""Interactive probe: pick a variant, type a seed, see top-20 neighbors.

Usage: python probe.py [V0|V1|V2|V3]   (default V2)
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    v = sys.argv[1] if len(sys.argv) > 1 else "V2"
    path = ROOT / f"results_{v}.json"
    if not path.exists():
        print(f"no {path}")
        sys.exit(2)
    res = json.loads(path.read_text(encoding="utf-8"))
    by_seed = {q["seed"]: q for q in res["queries"]}
    print(f"loaded {v}: {len(by_seed)} pre-queried seeds")
    print(f"config: {res['config']}")
    print(f"stats: {res['corpus_stats']}")
    print("known seeds:", ", ".join(by_seed))
    print("type a seed name (empty = quit).")
    while True:
        try:
            s = input("> ").strip()
        except EOFError:
            break
        if not s:
            break
        if s in by_seed:
            q = by_seed[s]
            if not q["seed_in_graph"]:
                print(f"  {s}: not in graph")
                continue
            for n in q["returned_neighbors"]:
                print(f"  {n['rank']:2d}. {n['term']}  w={n['weight']}")
        else:
            print(f"  {s}: not in golden seed list (this script only shows pre-computed queries)")
            print("  hint: edit golden.json and rerun build.py to add new seeds")


if __name__ == "__main__":
    main()
