# Job market tracker

Snapshot Google Jobs postings for one role across five US cities, store each day's sample in SQLite, and chart hiring demand over time.

Repo: https://github.com/Fimber/Tracking-job-market-trends-with-Google-Jobs-snapshots

## Setup

Python 3.11+ and a [SearchApi](https://www.searchapi.io/docs/google-jobs) key.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:SEARCHAPI_KEY = "your_key_here"
```

## Run

```powershell
python tracker.py              # 5 cities x 2 pages = 10 searches
python tracker.py --one-city --max-pages 1   # smoke test, 1 search
python analyze.py              # salary coverage, churn, two PNG charts
python -m unittest discover -s tests -v
```

`jobs.db` is created on the first successful snapshot. Charts land in `images/`.
