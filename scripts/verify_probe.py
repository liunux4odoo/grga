"""Minimum backend verification for grga.probe.

Creates a small temp corpus, runs probe_directory in three modes
(basic counting, implicit_operator consistency, sampling), and asserts
the invariants documented in docs/CLI.md.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from grga.probe import probe_directory  # noqa: E402
from grga.search import check_environment  # noqa: E402


def _write(base: Path, rel: str, text: str) -> None:
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_fixture(root: Path) -> None:
    _write(root, "a.txt", "foo alpha\nfoo bar\nbar beta\nbaz\n")
    _write(root, "sub/b.md", "foo and bar together\nfoo alone\nnothing here\n")
    _write(root, "sub/c.py", "print('hello')\n# no keywords\n")


def _probe(root: Path, keywords: str, **kwargs):
    raw = probe_directory(str(root), keywords, **kwargs)
    return json.loads(raw)


def _assert(cond, message):
    if not cond:
        raise AssertionError(message)


def test_basic_count(root: Path) -> None:
    result = _probe(root, "foo", per_file=True)
    _assert(result["ok"], f"probe failed: {result}")
    _assert(result["total_lines"] == 4, f"expected 4 foo lines, got {result['total_lines']}")
    _assert(result["file_count"] == 2, f"expected 2 files, got {result['file_count']}")
    paths = {item["path"] for item in result["per_file"]}
    _assert(paths == {"a.txt", "sub/b.md"}, f"unexpected per_file paths: {paths}")
    print(f"[ok] basic_count: total_lines={result['total_lines']}, files={result['file_count']}")


def test_implicit_operator(root: Path) -> None:
    and_result = _probe(root, "foo bar", implicit_operator="and")
    or_result = _probe(root, "foo bar", implicit_operator="or")
    _assert(and_result["ok"] and or_result["ok"], "probe failed")
    _assert(and_result["pcre2"] is True, "AND query should use pcre2 lookaheads")
    _assert(
        and_result["total_lines"] <= or_result["total_lines"],
        f"AND ({and_result['total_lines']}) must be <= OR ({or_result['total_lines']})",
    )
    _assert(and_result["total_lines"] == 2, f"expected 2 AND lines, got {and_result['total_lines']}")
    print(
        f"[ok] implicit_operator: AND={and_result['total_lines']} <= OR={or_result['total_lines']}"
    )


def test_sampling(root: Path) -> None:
    result = _probe(root, "foo", sample=10)
    _assert(result["ok"], "probe failed")
    sample = result.get("sample")
    _assert(sample is not None, "sample should be present when total_lines > 0")
    _assert(0 < sample["sampled_lines"] <= 10, f"bad sampled_lines: {sample}")
    _assert(sample["avg_line_bytes"] > 0, f"bad avg_line_bytes: {sample}")
    expected = sample["avg_line_bytes"] * result["total_lines"]
    _assert(sample["est_total_bytes"] == expected, f"est_total_bytes mismatch: {sample}")
    print(
        f"[ok] sampling: sampled={sample['sampled_lines']}, "
        f"avg={sample['avg_line_bytes']}B, est={sample['est_total_bytes']}B"
    )


def test_path_only(root: Path) -> None:
    result = _probe(root, "b\\.md", path_only=True, per_file=True)
    _assert(result["ok"], f"probe failed: {result}")
    _assert(result["file_count"] == 1, f"expected 1 path, got {result['file_count']}")
    _assert(
        result["per_file"][0]["path"] == "sub/b.md",
        f"unexpected path: {result['per_file']}",
    )
    print(f"[ok] path_only: files={result['file_count']}")


def main() -> int:
    env = check_environment()
    if not env.get("ok"):
        print(f"skipping: environment not ready: {env.get('errors')}", file=sys.stderr)
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="grga-probe-verify-"))
    try:
        _build_fixture(tmp)
        test_basic_count(tmp)
        test_implicit_operator(tmp)
        test_sampling(tmp)
        test_path_only(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("all probe checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
