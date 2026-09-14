#!/usr/bin/env python3
"""
Feed monitor for public Amazon-seller discussions.

Reads Reddit's public Atom feeds (no API, no credentials, no login), scores
entries against a keyword model, and appends anything interesting to
data/candidates.jsonl. Standard library only - no pip install, no lockfile.

Designed to run on a short schedule and do a little each time. State lives in
data/ and is committed back by the workflow, so consecutive runs continue where
the previous one stopped.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = DATA / "state.json"
CANDIDATES_PATH = DATA / "candidates.jsonl"
HEALTH_PATH = DATA / "health.json"
SUBREDDITS_PATH = DATA / "subreddits.json"

# Reddit's public feeds allow roughly one request per minute per client.
# The x-ratelimit-remaining header always reads 0.0 and is useless as a guide;
# a rejected request also consumes the window, so never retry tightly.
MIN_INTERVAL_S = 60
BACKOFF_AFTER_429_S = 90


# --------------------------------------------------------------------------
# config / state
# --------------------------------------------------------------------------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def build_rotation(cfg: dict) -> list[dict]:
    """Every feed we know how to fetch, in a stable order."""
    feeds: list[dict] = []
    for sub in cfg["subreddits"]["core"]:
        feeds.append({"kind": "new", "sub": sub,
                      "url": f"https://www.reddit.com/r/{sub}/new/.rss"})
        feeds.append({"kind": "comments", "sub": sub,
                      "url": f"https://www.reddit.com/r/{sub}/comments/.rss"})
    for sub in cfg["subreddits"].get("fringe", []):
        feeds.append({"kind": "new", "sub": sub,
                      "url": f"https://www.reddit.com/r/{sub}/new/.rss"})
    for q in cfg["search_queries"]:
        feeds.append({"kind": "search", "sub": None, "query": q,
                      "url": "https://www.reddit.com/search/.rss?"
                             + urllib.parse.urlencode({"q": q, "sort": "new", "t": "week"})})
    return feeds


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def fetch(url: str, user_agent: str, timeout: int = 25) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:                       # network, DNS, TLS, timeout
        print(f"    ! {type(e).__name__}: {e}", file=sys.stderr)
        return 0, ""


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.S)


def _tag(entry: str, name: str) -> str:
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", entry, re.S)
    return m.group(1) if m else ""


def parse_entries(xml: str) -> list[dict]:
    out = []
    for raw in ENTRY_RE.findall(xml):
        link = re.search(r'<link[^>]*href="([^"]+)"', raw)
        # Feed content is HTML escaped twice; one pass leaves you with markup.
        body = html.unescape(html.unescape(_tag(raw, "content")))
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body).strip()
        out.append({
            "id": _tag(raw, "id").strip(),
            "title": html.unescape(re.sub(r"\s+", " ", _tag(raw, "title"))).strip(),
            "author": re.sub(r"<[^>]+>", "", _tag(raw, "author")).strip().split("http")[0],
            "updated": _tag(raw, "updated").strip(),
            "permalink": link.group(1) if link else "",
            "subreddit": (re.search(r'<category term="([^"]+)"', raw) or [None, ""])[1]
                         if re.search(r'<category term="([^"]+)"', raw) else "",
            "body": body,
        })
    return out


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

MONEY = re.compile(r"[$€£]\s?\d[\d.,]*|\b\d+[.,]\d{2}\b")


def compile_signals(cfg: dict) -> dict:
    """Two vocabularies, deliberately.

    `exact` holds the machine-readable fee-type constants as they appear in a
    settlement report. They are unambiguous, so one hit is enough - but sellers
    almost never type them.

    `domain` holds the words sellers actually use for the same things
    ("inbound shortage", "reimbursement", "storage fee"). Individually these
    are too common to act on, so they only reach the top tier alongside a
    problem word or an amount. Scoring the catalogue alone misses most of the
    real opportunities; this is what that mistake cost on the first calibration.
    """
    def rx(words):
        out = []
        for w in words:
            pat = re.escape(w)
            # Word-boundary single words so "payout" does not fire inside
            # "payouts-are-fine" prose and short terms stay honest.
            if re.fullmatch(r"[\w&-]+", w):
                pat = rf"\b{pat}\b"
            out.append((w, re.compile(pat, re.I)))
        return out
    s = cfg["signals"]
    return {
        "exact": rx(s["exact_fee_names"]),
        "domain": rx(s["domain_terms"]),
        "problem": rx(s["problem_phrases"]),
        "tier_b": rx(s["tier_b_keywords"]),
        "tier_c": [(p, re.compile(p, re.I)) for p in s["tier_c_patterns"]],
    }


def score(entry: dict, sig: dict) -> tuple[str | None, list[str]]:
    """Return (tier, matched signals). Cheap and deterministic on purpose:
    the expensive judgement happens later, over a short list."""
    text = f"{entry['title']} {entry['body']}"

    exact = [w for w, r in sig["exact"] if r.search(text)]
    domain = [w for w, r in sig["domain"] if r.search(text)]
    problem = [w for w, r in sig["problem"] if r.search(text)]
    has_money = bool(MONEY.search(text))

    # Tier A - a settlement-mechanics question we can answer better than anyone.
    # Either they quoted the report verbatim, or they described the pain in
    # their own words and there is something concrete to grab hold of.
    if exact or (domain and (problem or has_money)):
        hits = ([f"exact:{w}" for w in exact]
                + [f"domain:{w}" for w in domain]
                + [f"problem:{w}" for w in problem])
        if has_money:
            hits.append("amount")
        return "A", hits

    # Tier B - the right subject area, but nothing concrete yet, or
    # advice-adjacent territory where we answer mechanics only.
    b = [w for w, r in sig["tier_b"] if r.search(text)]
    if domain or b:
        return "B", [f"domain:{w}" for w in domain] + [f"kw:{w}" for w in b]

    c = [p for p, r in sig["tier_c"] if r.search(text)]
    if c:
        return "C", [f"pat:{p}" for p in c]

    return None, []


def age_hours(updated: str) -> float | None:
    try:
        ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except ValueError:
        return None


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        print("config.json missing or invalid", file=sys.stderr)
        return 1

    ua = os.environ.get("MONITOR_UA") or cfg["user_agent"]
    batch = int(os.environ.get("MONITOR_BATCH") or cfg["feeds_per_run"])
    max_age = float(cfg["max_age_hours"])

    state = load_json(STATE_PATH, {"cursor": 0, "seen": [], "runs": 0})
    seen = set(state.get("seen", []))
    rotation = build_rotation(cfg)
    cursor = state.get("cursor", 0) % len(rotation)

    health = load_json(HEALTH_PATH, {})
    found = {"A": 0, "B": 0, "C": 0}
    new_rows: list[dict] = []
    statuses: list[int] = []
    run_threads: set[tuple[str, str]] = set()

    print(f"{len(rotation)} feeds in rotation, taking {batch} from position {cursor}")

    for i in range(batch):
        feed = rotation[(cursor + i) % len(rotation)]
        if i > 0:
            time.sleep(MIN_INTERVAL_S)

        status, xml = fetch(feed["url"], ua)
        if status == 429:
            print(f"  429 {feed['url']} - backing off {BACKOFF_AFTER_429_S}s")
            time.sleep(BACKOFF_AFTER_429_S)
            status, xml = fetch(feed["url"], ua)

        statuses.append(status)
        entries = parse_entries(xml) if status == 200 else []
        health[feed["url"]] = {
            "status": status,
            "entries": len(entries),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        print(f"  {status} {len(entries):>3} entries  {feed['url']}")

        sig = compile_signals(cfg)
        for e in entries:
            if not e["id"] or e["id"] in seen:
                continue
            seen.add(e["id"])
            age = age_hours(e["updated"])
            if age is not None and age > max_age:
                continue
            tier, hits = score(e, sig)
            if not tier:
                continue

            # Comment feeds title entries as "/u/someone on <thread title>".
            # A busy thread produces many near-identical rows; one is enough,
            # the agent will open the thread anyway.
            thread = e["title"].split(" on ", 1)[-1] if e["title"].startswith("/u/") else e["title"]
            key = (thread, tier)
            if key in run_threads:
                continue
            run_threads.add(key)

            found[tier] += 1
            new_rows.append({
                "thread": thread,
                "tier": tier,
                "signals": hits,
                "title": e["title"],
                "author": e["author"],
                "subreddit": e["subreddit"] or feed.get("sub") or "",
                "permalink": e["permalink"],
                "updated": e["updated"],
                "age_hours": round(age, 1) if age is not None else None,
                "from_feed": feed["kind"],
                "body": e["body"][:1500],
                "seen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "reviewed": False,
            })

    # Every Nth run, spend one request finding communities we are not watching
    # yet. Guessing subreddit names from a chat window is slow and wrong; the
    # rotation has minutes to spare and nobody is waiting on it.
    disc = cfg.get("discovery", {})
    every = int(disc.get("every_n_runs", 0) or 0)
    queries = disc.get("queries", [])
    if every and queries and state.get("runs", 0) % every == 0:
        q = queries[(state.get("runs", 0) // every) % len(queries)]
        time.sleep(MIN_INTERVAL_S)
        url = ("https://www.reddit.com/subreddits/search/.rss?"
               + urllib.parse.urlencode({"q": q}))
        status, xml = fetch(url, ua)
        known = load_json(SUBREDDITS_PATH, {})
        if status == 200:
            for e in parse_entries(xml):
                m = re.search(r"/r/([^/\"]+)", e["permalink"])
                if not m:
                    continue
                name = m.group(1)
                if name not in known:
                    known[name] = {"first_seen_via": q,
                                   "blurb": e["body"][:180],
                                   "watching": False}
            save_json(SUBREDDITS_PATH, known)
        print(f"  discovery [{status}] '{q}' -> {len(known)} subreddits known")

    if new_rows:
        CANDIDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CANDIDATES_PATH.open("a", encoding="utf-8") as fh:
            for row in new_rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    # Keep the seen-list bounded; ids age out well before they come back around.
    state["seen"] = list(seen)[-8000:]
    state["cursor"] = (cursor + batch) % len(rotation)
    state["runs"] = state.get("runs", 0) + 1
    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["last_statuses"] = statuses
    save_json(STATE_PATH, state)
    save_json(HEALTH_PATH, health)

    ok = sum(1 for s in statuses if s == 200)
    print(f"\n{ok}/{len(statuses)} feeds ok | new candidates A={found['A']} B={found['B']} C={found['C']}")

    if ok == 0:
        # Every feed refused. Most likely this runner's IP is being throttled
        # or blocked - the one thing worth shouting about.
        print("::warning::no feed returned 200 - this runner's IP may be blocked by Reddit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
