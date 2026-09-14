#!/usr/bin/env python3
"""Merge one run's data/ output into whatever is on main now.

A run takes ~20 minutes (it sleeps between fetches). Anything pushed to main
in that window - a hand-edited `reviewed` flag, a code change, a stray run -
makes `git pull --rebase` choke on data/, because every file here is state,
not source. So the workflow does not rebase: it puts the run's output aside,
resets to origin/main, and calls this to fold the output back in.

Rules, per file (theirs = main, ours = this run):
  candidates.jsonl  theirs, plus ours rows whose id is not there yet. Rows
                    already on main keep main's version, so a `reviewed: true`
                    set by hand is never undone by a run that started earlier.
  state.json        ours, with `seen` = union and `runs` = max of both.
  health.json       ours (it describes the most recent run).
  subreddits.json   theirs, plus ours entries not there yet.

Idempotent: merging a run into the commit it started from yields the run's
own output.

usage: merge_data.py <ours_dir> <target_dir>
"""
import json
import sys
from pathlib import Path


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def row_key(row: dict) -> str:
    return row.get("id") or row.get("permalink") or json.dumps(row, sort_keys=True)


def merge_candidates(ours: Path, theirs: Path) -> int:
    base = read_rows(theirs)
    have = {row_key(r) for r in base}
    added = [r for r in read_rows(ours) if row_key(r) not in have]
    if added:
        theirs.parent.mkdir(parents=True, exist_ok=True)
        with theirs.open("a", encoding="utf-8") as fh:
            for r in added:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(added)


def merge_state(ours: Path, theirs: Path) -> None:
    o = load_json(ours, None)
    if o is None:
        return
    t = load_json(theirs, {})
    seen_t = t.get("seen", [])
    seen_o = o.get("seen", [])
    known = set(seen_t)
    merged = list(seen_t) + [s for s in seen_o if s not in known]
    o["seen"] = merged[-8000:]
    o["runs"] = max(int(o.get("runs", 0)), int(t.get("runs", 0)))
    save_json(theirs, o)


def merge_subreddits(ours: Path, theirs: Path) -> None:
    o = load_json(ours, None)
    if o is None:
        return
    t = load_json(theirs, {})
    for name, info in o.items():
        t.setdefault(name, info)
    save_json(theirs, t)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    ours, theirs = Path(sys.argv[1]), Path(sys.argv[2])
    n = merge_candidates(ours / "candidates.jsonl", theirs / "candidates.jsonl")
    merge_state(ours / "state.json", theirs / "state.json")
    merge_subreddits(ours / "subreddits.json", theirs / "subreddits.json")
    if (ours / "health.json").exists():
        theirs.mkdir(parents=True, exist_ok=True)
        (theirs / "health.json").write_bytes((ours / "health.json").read_bytes())
    print(f"merged: +{n} candidates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
