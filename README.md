# Google Jobs snapshot tracker

Snapshot Google Jobs postings for one role across several cities every day, store each snapshot in SQLite, and chart how the market composition moves over time.

Job boards show you what's open today and forget it tomorrow. This collector builds the history yourself by writing one row per job per day, so the difference between yesterday and today becomes queryable. Data comes from the [SearchApi Google Jobs API](https://www.searchapi.io/docs/google-jobs?utm_source=Dev&utm_medium=Ambassador&utm_campaign=studio1hq.com).

Full write-up: **[link to the published article]**

## What it does

- Fetches Google Jobs results for one query across a list of cities, walking token-based pagination.
- Normalizes the 3 fields that resist analysis, which are relative posting dates, work mode, and free-text salary.
- Deduplicates on Google's own `htidocid`, since the API returns no `job_id`.
- Writes one row per job per snapshot date, so listing lifespan and daily churn fall out of the data.
- Renders a remote-share chart with sample sizes on the bars.

## Requirements

Python 3.11 or later, because the code uses `datetime.UTC` and PEP 604 type hints. You'll also need a [SearchApi account](https://www.searchapi.io/?utm_source=Dev&utm_medium=Ambassador&utm_campaign=studio1hq.com) for an API key. The free tier includes 100 searches, which covers setup and debugging.

## Install

Clone the repo, create a virtual environment, and install the pinned dependencies.

```bash
git clone https://github.com/Studio1HQ/Tracking-job-market-trends-with-Google-Jobs-snapshots.git
cd Tracking-job-market-trends-with-Google-Jobs-snapshots
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Export your API key so it stays out of the repository.

```bash
export SEARCHAPI_KEY="your_key_here"      # Windows PowerShell: $env:SEARCHAPI_KEY="your_key_here"
```

The key is read at request time rather than at import, so *analyze.py* and the test suite run without one.

## Configuration

Edit 2 constants at the top of *tracker.py*.

```python
CITIES = [
    "Austin,Texas,United States",
    "Denver,Colorado,United States",
    "Seattle,Washington,United States",
    "Atlanta,Georgia,United States",
    "New York,New York,United States",
]
QUERY = "backend engineer"
```

Use fully qualified location strings. Passing `"Austin"` and `"Austin,Texas,United States"` can resolve to different places, and a silent resolution change mid-series looks like a demand shift in your charts. Every run logs the `location_used` value the API resolved, so watch it for drift.

## Usage

Start with the cheapest possible check, which costs 1 search and confirms your key works and rows land in the database.

```bash
python tracker.py --one-city --max-pages 1
```

Run a full snapshot across every configured city once that succeeds.

```bash
python tracker.py
```

Generate the chart from whatever snapshots exist.

```bash
python analyze.py
```

Output lands in *images/remote_share.png*. The script selects matplotlib's Agg backend, so it runs headless under cron and CI, and it exits cleanly against an empty database rather than raising.

### CLI flags

| Flag | Default | Purpose |
|---|---|---|
| `--one-city` | off | Fetch only the first entry in `CITIES`, for cheap smoke tests |
| `--max-pages N` | 2 | Pages per city. Each page returns about 10 results, so this sets your ceiling |

## Scheduling

Snapshot at a consistent hour so each run samples a comparable point in the posting cycle. This cron entry runs at 06:15 daily and appends output to a log.

```cron
15 6 * * * cd /home/you/jobtracker && /usr/bin/env SEARCHAPI_KEY=xxx ./venv/bin/python tracker.py >> run.log 2>&1
```

GitHub Actions works too, and *.github/workflows/snapshot.yml* commits the database back to the repo after each run, giving you free hosting plus a version history of every snapshot. Add `SEARCHAPI_KEY` under Settings, Secrets and variables, Actions.

Scheduled runners drift by up to an hour under load and skip entirely during outages, so expect gaps and plot against actual snapshot dates rather than assuming a continuous series.

## Data model

The table holds 1 row per job per snapshot date, keyed on `(snapshot_date, city, query, job_key)`.

| Column | Notes |
|---|---|
| `snapshot_date` | Date the row was collected. The only date you can fully trust |
| `job_key` | `htidocid` from `sharing_link`, or a SHA-1 of title, company, and location as fallback |
| `query`, `city` | The search that produced the row |
| `title`, `company`, `location`, `via` | Straight from the API |
| `posted_at_raw` | Original relative string, kept for re-parsing |
| `posted_date` | Absolute date derived from the snapshot date |
| `posted_precision` | `day`, `approx`, `floor`, `missing`, or `unparsed` |
| `work_mode` | `remote`, `hybrid`, `onsite`, or `unknown` |
| `schedule` | Full-time, contract, and so on |
| `salary_raw` | Original salary string, so parser fixes can be applied retroactively |
| `salary_min`, `salary_max` | Annualized USD |
| `salary_source` | `detected_extensions` or `description` |
| `position` | Rank in the API response. Useful for spotting collector artifacts |

`INSERT OR REPLACE` makes runs idempotent, so re-running after a partial failure repairs the day instead of duplicating it.

### Migrating an existing database

`CREATE TABLE IF NOT EXISTS` skips tables that already exist, columns included, so adding a column to `SCHEMA` does nothing to a database created earlier. `migrate()` runs on every snapshot, checks `PRAGMA table_info`, and issues `ALTER TABLE` for anything missing. Existing history survives. Databases created before `salary_raw` existed are upgraded automatically on the next run.

## How the parsers work

### Posting dates

Google returns relative text such as `3 days ago`, `Just posted`, or `30+ days ago`. Each string only means something relative to when you fetched it, which is why the snapshot timestamp is stored on every row. The `posted_precision` flag records how much to trust the derived date, and `30+ days ago` is a floor with no visible upper bound rather than a date.

About 17% of listings carry no `posted_at` at all, so those rows get `missing` and your freshness metrics describe the remainder.

### Work mode

3 signals are layered, cheapest first. The `work_from_home` flag is used when present, then the location string, then the first 2,000 characters of the description.

HTML is stripped before that window is measured. Descriptions arrive as markup, and tags consume roughly 7% of the budget, which is enough to push a work-mode sentence out of range and misclassify a listing that stated its arrangement plainly.

`ONSITE_HINT` and `HYBRID_HINT` are deliberately separate. Folding "onsite" into the hybrid pattern makes "you will work onsite with the platform team" classify as hybrid, which inflates the exact number this project exists to measure.

### Salary

2 passes with different strictness. The structured `detected_extensions.salary` field is known to hold a salary, so a currency symbol is optional there, which matters because Google writes values like `125K a year` with no `$`. Free description text keeps the `$` anchor to stop version numbers and dates matching as money.

`NON_USD` rejects £, €, ¥, ₹, the EUR, GBP, CAD, AUD, and INR codes, and `C$`, `A$`, `CA$`, `AU$`, `NZ$`. Without it a pound range such as `50,000` to `65,000` half-parses, because the range pattern breaks on the second symbol while the single-amount pattern still matches the first number, landing a firm 50000 in your database as dollars with no error.

The table below lists what the parser still gets wrong.

| Input | Result |
|---|---|
| `$55 - $70 an hour` | Annualized at 2,080 hours, wrong for part-time or short contracts |
| `Up to $180,000 a year` | A ceiling recorded as a point value |
| `Equity grant of $2,000 - $8,000. Base salary $140,000 - $170,000.` | Discarded, because equity wins the regex race and then fails the sanity floor |

## Tests

39 unit tests cover the parsers, the pagination retry loop, the dedup key, and the schema round trip. No API key or network access is required.

```bash
pytest tests/ -v
```

## Cost

Quota use is `cities × queries × pages × days` searches per month. The default 5-city, single-query, 2-page daily run comes to 300 searches, which is 3% of SearchApi's Developer plan. Billing is by monthly plan rather than per call, so the number to watch is whether you stay under your allocation. Current rates are on the [SearchApi pricing page](https://www.searchapi.io/pricing?utm_source=Dev&utm_medium=Ambassador&utm_campaign=studio1hq.com).

Only successful requests count, so a rate-limited retry costs time rather than quota. The hourly cap is 20% of the monthly allocation.

## Known limitations

**Pagination caps your counts.** Each page returns about 10 results, so `max_pages` sets a hard ceiling on postings per city. A city that hits the ceiling is reporting your fetch settings rather than its market. Raise `max_pages` until cities come back under the ceiling, or treat the counts as a fixed-depth sample and say so.

**Coverage varies by region.** Google Jobs aggregates from job boards rather than indexing employers directly, so counts across countries are not comparable and a low count can mean thin ingestion rather than thin hiring.

**Reposted listings inflate everything.** A refreshed posting can surface with a new `htidocid`, so the dedup key treats it as new. Watch for the same title and company reappearing at intervals.

**Ranks are not meaningful.** Result ordering is personalized and location-derived, so `position` reflects Google's ranking for a synthetic location. Counts are reliable, ranks are not.

**Non-USD markets are dropped, not converted.** Real multi-currency support means storing the symbol and a conversion rate, not loosening the regex.

## What the first snapshot showed

A single run across 5 cities on 2026-08-21 returned 104 postings.

| City | Live postings | Remote | Hybrid | Onsite | Unknown |
|---|---|---|---|---|---|
| Atlanta | 19 | 21% | 16% | 11% | 53% |
| Austin | 25 | 20% | 4% | 8% | 68% |
| Denver | 20 | 30% | 10% | 0% | 60% |
| New York | 20 | 35% | 5% | 0% | 60% |
| Seattle | 20 | 25% | 15% | 0% | 60% |

Percentages are rounded, so Atlanta sums to 101%. Read the rest carefully too. Remote percentages rest on 4 to 7 listings per city, where a single posting moves the number by 4 to 5 points, so the gap between New York and Austin carries no weight. The `unknown` majority means most listings describe their work arrangement nowhere in the first 2,000 characters, which splits the metric in 2, since remote runs at 20% to 35% of all listings but 44% to 88% of listings the classifier could place.

Austin's 25 is a collector artifact rather than deeper coverage. Every Austin row carries a `position` between 1 and 10, so a smoke test and a full 2-page pull landed on the same date.

Salary disclosure measured 9% on that snapshot, under the earlier `$`-anchored parser that rejected every unsigned amount. Treat it as a floor. The corrected rate arrives with the first snapshot written under `salary_raw`.

## Project layout

```text
.
├── tracker.py                    # fetch, normalize, store
├── analyze.py                    # queries and the remote-share chart
├── tests/test_tracker.py         # parser unit tests, no API key needed
├── requirements.txt
├── images/                       # chart output
├── jobs.db                       # created on first run
└── .github/workflows/snapshot.yml
```

## License

MIT
