# reddit-monitor

Polls Reddit's **public Atom feeds** for Amazon-seller discussions worth joining,
scores them, and commits the hits to `data/candidates.jsonl`.

No Reddit API, no app registration, no credentials, no login. Standard library
only — there is nothing to install and no lockfile to maintain.

---

## Why feeds and not the API

The Reddit Data API is not a usable route for a small independent product: since
the Responsible Builder Policy, access runs through manual approval. The ordinary
syndication feeds are unaffected and serve everything this needs:

| Feed | Gives |
|---|---|
| `/r/<sub>/new/.rss` | 25 posts with **full body text**, author, timestamp, permalink |
| `/r/<sub>/comments/.rss` | 25 recent comments with full text and their thread |
| `/search/.rss?q=…` | Reddit-wide keyword search — catches threads outside the sub list |
| `<permalink>.rss` | One thread's comments — how many answers are already there |

Two traps worth knowing: these are **Atom**, not RSS 2.0, and `content` is
**double-escaped HTML**. Unescaping once leaves you with markup and no text.

## The rate limit

Measured, not copied from a blog:

- Roughly **one request per 60 seconds**. Two successful calls 102 seconds apart;
  everything between them was refused.
- `x-ratelimit-remaining` always reads `0.0`. It is useless as a guide — ignore it.
- **A rejected request also consumes the window.** A 429 inside a freshly reset
  window still failed. Retrying tightly makes things worse, not better.

So: `MIN_INTERVAL_S = 60`, and after a 429 wait 90 seconds and try once. That is
why a run fetching 3 feeds takes about two and a half minutes, most of it asleep.

---

## Setup

1. Create a repository and push these files.
2. **Settings → Actions → General → Workflow permissions** → *Read and write*.
   Without it the workflow cannot commit its findings back.
3. **Actions** tab → *monitor* → **Run workflow**. Don't wait for the schedule;
   the first run is the one that tells you whether this works at all.
4. Open `data/health.json`.

### Reading the first run

`health.json` records the HTTP status of every feed. This is the whole
diagnostic — there is no separate test workflow, because a run that works is
worth more than a test that passes.

- **`"status": 200`** — it works. It's running. Leave it.
- **`"status": 429` or `403` everywhere** — Reddit is throttling this runner's IP.
  GitHub-hosted runners share addresses with a lot of scraping traffic, which is
  exactly what gets rate-limited hardest. The workflow log will also carry a
  `::warning::` saying so. Move the job to a machine with its own address; the
  script itself needs no changes.

## What it costs

GitHub bills a **minimum of one minute per job**, so cost tracks the number of
runs, not their length.

| Repo | Every 20 min (72 runs/day) |
|---|---|
| **Public** | free — public repositories get unlimited Actions minutes |
| **Private** | ~180 min/day ≈ 5,400/month, against 2,000 free or 3,000 on Pro |

Private is not viable at a useful cadence without paying for minutes. Public is
free, but then the sub list and keywords are readable by anyone — including the
communities being monitored. Nothing in this repository names a company or a
product, and that is deliberate: keep it that way and a public repo is a generic
Amazon-seller feed reader rather than a discoverable marketing artefact.

To spend less: raise the cron interval, or drop `feeds_per_run` to 2.

---

## How it scores

Two vocabularies, because the obvious approach fails.

**`exact_fee_names`** are settlement-report constants — `MISSING_FROM_INBOUND`,
`Current Reserve Amount`. Unambiguous, so one hit is enough.

**`domain_terms`** are what sellers actually write: *inbound shortage*,
*reimbursement*, *storage fee*. Individually too common to act on, so they only
reach the top tier alongside a `problem_phrase` or a currency amount.

The first calibration ran the catalogue vocabulary alone against 25 live posts
and scored **zero**, while missing two obvious opportunities — *"how do you stay
on top of fees and reimbursement that Amazon owes you"* and *"at what point is an
FBA inbound shortage too old to ignore"*. Sellers do not type fee-catalogue
constants. That is why the second vocabulary exists.

| Tier | Meaning |
|---|---|
| **A** | A settlement-mechanics question with something concrete in it. Highest value. |
| **B** | Right subject area, nothing concrete yet, or advice-adjacent. |
| **C** | Tooling recommendation threads. Handle carefully. |

Tune by editing `config.json`. Nothing in the scoring is compiled in.

## Output

`data/candidates.jsonl` — one JSON object per line, append-only:

```json
{"tier":"A","signals":["domain:inbound shortage","problem:shortage"],
 "thread":"At what point do you consider an FBA inbound shortage too old to ignore?",
 "permalink":"https://www.reddit.com/r/...","age_hours":3.2,"reviewed":false,
 "body":"..."}
```

`data/state.json` holds the rotation cursor and the ids already seen, so runs
continue where the last one stopped and nothing is reported twice. Comment feeds
are collapsed per thread — a busy thread yields one row, not twelve.

Anything consuming this reads the file over the GitHub API or raw URL and filters
`reviewed == false`. Mark rows handled by flipping the flag and committing.

## Layout

```
monitor.py                       fetch, parse, score, store
config.json                      subreddits, queries, signal vocabularies
.github/workflows/monitor.yml    schedule, run, commit back
data/state.json                  rotation cursor + seen ids
data/candidates.jsonl            scored hits, append-only
data/health.json                 last status per feed — the IP diagnosis
```
