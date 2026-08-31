"""Snapshot Google Jobs postings for one role across several cities."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import requests

SEARCHAPI_URL = "https://www.searchapi.io/api/v1/search"
ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "jobs.db"

CITIES = [
    "Austin,Texas,United States",
    "Denver,Colorado,United States",
    "Seattle,Washington,United States",
    "Atlanta,Georgia,United States",
    "New York,New York,United States",
]
QUERY = "backend engineer"

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_date   TEXT NOT NULL,
    job_key         TEXT NOT NULL,
    query           TEXT NOT NULL,
    city            TEXT NOT NULL,
    title           TEXT,
    company         TEXT,
    location        TEXT,
    via             TEXT,
    posted_at_raw   TEXT,
    posted_date     TEXT,
    posted_precision TEXT,
    work_mode       TEXT,
    schedule        TEXT,
    salary_raw      TEXT,
    salary_min      REAL,
    salary_max      REAL,
    salary_source   TEXT,
    position        INTEGER,
    description     TEXT,
    skills          TEXT,
    years_experience INTEGER,
    PRIMARY KEY (snapshot_date, city, query, job_key)
);
CREATE INDEX IF NOT EXISTS idx_key_date ON snapshots(job_key, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_city_date ON snapshots(city, snapshot_date);
"""

RELATIVE_RE = re.compile(r"(\d+)\s*(\+?)\s*(minute|hour|day|week|month)s?\s+ago", re.I)
FLOOR_HINT = re.compile(r"\b(over|more than|at least)\b", re.I)
IMMEDIATE = {"just posted", "just now", "today", "posted today"}

UNIT_DELTA = {
    "minute": lambda n: timedelta(minutes=n),
    "hour": lambda n: timedelta(hours=n),
    "day": lambda n: timedelta(days=n),
    "week": lambda n: timedelta(weeks=n),
    "month": lambda n: timedelta(days=30 * n),
}

TAG_RE = re.compile(r"<[^>]+>")
REMOTE_LOC = re.compile(r"\b(remote|anywhere|work from home|telecommute)\b", re.I)
HYBRID_HINT = re.compile(
    r"\b(hybrid|"
    r"\d+\s*days?\s*(?:per|a)\s*week\s*(?:in|at|from)|"
    r"return[- ]to[- ]office)\b",
    re.I,
)
ONSITE_HINT = re.compile(r"\b(on-?site|in[- ]office|in[- ]person)\b", re.I)

AMOUNT = r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*([KkMm])?"
SEPARATOR = r"\s*(?:-|to|\u2013|\u2014)\s*"
PERIOD = r"(?:\s*(?:per|a|an|/)\s*(hour|hr|year|yr|month|mo|week|wk))?"

FIELD_RANGE = re.compile(r"\$?\s?" + AMOUNT + SEPARATOR + r"\$?\s?" + AMOUNT + PERIOD, re.I)
FIELD_SINGLE = re.compile(r"\$?\s?" + AMOUNT + PERIOD, re.I)
DESC_RANGE = re.compile(r"\$\s?" + AMOUNT + SEPARATOR + r"\$?\s?" + AMOUNT + PERIOD, re.I)
DESC_SINGLE = re.compile(r"\$\s?" + AMOUNT + PERIOD, re.I)
NON_USD = re.compile(
    r"[\u00a3\u20ac\u00a5\u20b9]|\b(?:EUR|GBP|CAD|AUD|INR)\b|\b(?:C|A|CA|AU|NZ)\$",
    re.I,
)

ANNUALIZE = {
    "hour": 2080,
    "hr": 2080,
    "week": 52,
    "wk": 52,
    "month": 12,
    "mo": 12,
    "year": 1,
    "yr": 1,
}

HTIDOCID_RE = re.compile(r"htidocid=([^&#]+)")
YEARS_RE = re.compile(
    r"(\d+)\s*\+?\s*years?(?:\s+of)?(?:\s+experience)?",
    re.I,
)
SKILL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("postgresql", re.compile(r"\bpostgres(?:ql)?\b", re.I)),
    ("kubernetes", re.compile(r"\bkubernetes\b|\bk8s\b", re.I)),
    ("typescript", re.compile(r"\btypescript\b", re.I)),
    ("javascript", re.compile(r"\bjavascript\b", re.I)),
    ("terraform", re.compile(r"\bterraform\b", re.I)),
    ("graphql", re.compile(r"\bgraphql\b", re.I)),
    ("fastapi", re.compile(r"\bfastapi\b", re.I)),
    ("django", re.compile(r"\bdjango\b", re.I)),
    ("spring", re.compile(r"\bspring\s*boot\b|\bspring\b", re.I)),
    ("python", re.compile(r"\bpython\b", re.I)),
    ("java", re.compile(r"\bjava\b", re.I)),
    ("golang", re.compile(r"\bgolang\b", re.I)),
    ("rust", re.compile(r"\brust\b", re.I)),
    ("kafka", re.compile(r"\bkafka\b", re.I)),
    ("redis", re.compile(r"\bredis\b", re.I)),
    ("docker", re.compile(r"\bdocker\b", re.I)),
    ("aws", re.compile(r"\baws\b|\bamazon web services\b", re.I)),
    ("gcp", re.compile(r"\bgcp\b|\bgoogle cloud\b", re.I)),
    ("azure", re.compile(r"\bazure\b", re.I)),
    ("grpc", re.compile(r"\bgrpc\b", re.I)),
]


def api_key() -> str:
    """Read SEARCHAPI_KEY only when a live request is about to go out."""
    key = os.environ.get("SEARCHAPI_KEY")
    if not key:
        raise SystemExit("Set SEARCHAPI_KEY before fetching.")
    return key


def fetch_city(
    query: str,
    location: str,
    max_pages: int = 2,
    pause: float = 1.5,
    max_retries: int = 2,
) -> tuple[list[dict], str | None]:
    """Fetch job listings for one query/location pair. Returns (jobs, location_used)."""
    jobs: list[dict] = []
    token: str | None = None
    location_used: str | None = None
    page = 0
    retries = 0

    while page < max_pages:
        params = {
            "engine": "google_jobs",
            "q": query,
            "location": location,
            "gl": "us",
            "hl": "en",
            "api_key": api_key(),
        }
        if token:
            params["next_page_token"] = token

        try:
            resp = requests.get(SEARCHAPI_URL, params=params, timeout=95)
        except requests.RequestException as exc:
            print(f"  [{location}] page {page + 1} network error: {exc}")
            break

        if resp.status_code == 429:
            if retries >= max_retries:
                print(f"  [{location}] still limited after {max_retries} retries, stopping")
                break
            retries += 1
            print(f"  [{location}] rate limited, retry {retries} in 60s")
            time.sleep(60)
            continue
        if resp.status_code != 200:
            print(
                f"  [{location}] page {page + 1} HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
            break

        retries = 0
        data = resp.json()
        location_used = location_used or data.get("search_parameters", {}).get(
            "location_used"
        )
        page_jobs = data.get("jobs", [])
        jobs.extend(page_jobs)
        print(f"  [{location}] page {page + 1}: {len(page_jobs)} jobs")

        token = data.get("pagination", {}).get("next_page_token")
        page += 1
        if not token or not page_jobs:
            break
        time.sleep(pause)

    return jobs, location_used


def parse_posted_at(raw: str | None, snapshot: date) -> tuple[str | None, str]:
    """Convert '3 days ago' into an ISO date plus a precision flag."""
    if not raw:
        return None, "missing"

    text = raw.strip().lower()
    if text in IMMEDIATE:
        return snapshot.isoformat(), "day"

    match = RELATIVE_RE.search(text)
    if not match:
        return None, "unparsed"

    n, plus, unit = int(match.group(1)), match.group(2), match.group(3).lower()
    posted = snapshot - UNIT_DELTA[unit](n)

    if plus == "+" or FLOOR_HINT.search(text):
        precision = "floor"  # true date is this old or older
    elif unit in ("minute", "hour", "day"):
        precision = "day"
    else:
        precision = "approx"  # weeks and months round hard

    return posted.isoformat(), precision


def classify_work_mode(job: dict) -> str:
    """Return one of: remote, hybrid, onsite, unknown."""
    flagged = job.get("detected_extensions", {}).get("work_from_home")
    location = job.get("location") or ""
    head = TAG_RE.sub(" ", job.get("description") or "")[:2000]

    location_says_remote = bool(REMOTE_LOC.search(location))
    hybrid = bool(HYBRID_HINT.search(head))
    onsite = bool(ONSITE_HINT.search(head))

    if flagged is True or location_says_remote:
        return "hybrid" if (hybrid or onsite) else "remote"
    if hybrid:
        return "hybrid"
    if onsite or flagged is False:
        return "onsite"
    return "unknown"


def _to_number(amount: str, suffix: str | None) -> float:
    value = float(amount.replace(",", ""))
    if suffix and suffix.lower() == "k":
        value *= 1_000
    elif suffix and suffix.lower() == "m":
        value *= 1_000_000
    return value


def _extract(
    text: str, rng: re.Pattern, single: re.Pattern
) -> tuple[float, float] | None:
    match = rng.search(text)
    if match:
        low = _to_number(match.group(1), match.group(2))
        high = _to_number(match.group(3), match.group(4))
        period = (match.group(5) or "year").lower()
    else:
        match = single.search(text)
        if not match:
            return None
        low = high = _to_number(match.group(1), match.group(2))
        period = (match.group(3) or "year").lower()

    factor = ANNUALIZE.get(period, 1)
    low, high = low * factor, high * factor
    if low > high:
        low, high = high, low
    if not (10_000 <= low <= 2_000_000):
        return None
    return low, high


def parse_salary(job: dict) -> dict:
    """Annualized salary range from the structured field, falling back to the description."""
    field = job.get("detected_extensions", {}).get("salary")
    if field and not NON_USD.search(field):
        found = _extract(field, FIELD_RANGE, FIELD_SINGLE)
        if found:
            return {
                "salary_min": found[0],
                "salary_max": found[1],
                "salary_source": "detected_extensions",
            }

    description = TAG_RE.sub(" ", job.get("description") or "")[:4000]
    if description:
        found = _extract(description, DESC_RANGE, DESC_SINGLE)
        if found:
            return {
                "salary_min": found[0],
                "salary_max": found[1],
                "salary_source": "description",
            }

    return {"salary_min": None, "salary_max": None, "salary_source": None}


def strip_html(text: str | None) -> str:
    cleaned = TAG_RE.sub(" ", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def extract_skills(text: str | None) -> tuple[str | None, int | None]:
    """Keyword-match frameworks, cloud, and years of experience from a description."""
    blob = strip_html(text)
    if not blob:
        return None, None
    skills = [name for name, pattern in SKILL_PATTERNS if pattern.search(blob)]
    years = None
    match = YEARS_RE.search(blob)
    if match:
        years = int(match.group(1))
        if years > 40:
            years = None
    return (",".join(skills) if skills else None), years


def job_key(job: dict) -> str:
    """Google's htidocid, or a content hash when the sharing link is missing."""
    match = HTIDOCID_RE.search(job.get("sharing_link") or "")
    if match:
        return unquote(match.group(1))
    seed = "|".join(
        [
            (job.get("title") or "").strip().lower(),
            (job.get("company_name") or "").strip().lower(),
            (job.get("location") or "").strip().lower(),
        ]
    )
    return "h:" + hashlib.sha1(seed.encode()).hexdigest()[:16]


def migrate(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS skips existing tables, so add columns explicitly."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)")}
    for column, decl in [
        ("salary_raw", "TEXT"),
        ("description", "TEXT"),
        ("skills", "TEXT"),
        ("years_experience", "INTEGER"),
    ]:
        if column not in existing:
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {column} {decl}")
    conn.commit()


def save_snapshot(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    columns = list(rows[0].keys())
    placeholders = ", ".join("?" * len(columns))
    sql = (
        f"INSERT OR REPLACE INTO snapshots ({', '.join(columns)}) "
        f"VALUES ({placeholders})"
    )
    conn.executemany(sql, [tuple(r[c] for c in columns) for r in rows])
    conn.commit()
    return len(rows)


def normalize_jobs(
    jobs: list[dict],
    city: str,
    query: str,
    snapshot: date,
) -> list[dict]:
    """Turn raw API job objects into snapshot rows."""
    posted = [
        parse_posted_at(
            j.get("detected_extensions", {}).get("posted_at"), snapshot
        )
        for j in jobs
    ]
    city_label = city.split(",")[0]
    rows = []
    for j, p in zip(jobs, posted):
        skills, years = extract_skills(
            " ".join(filter(None, [j.get("title"), j.get("description")]))
        )
        rows.append(
            {
                "snapshot_date": snapshot.isoformat(),
                "job_key": job_key(j),
                "query": query,
                "city": city_label,
                "title": j.get("title"),
                "company": j.get("company_name"),
                "location": j.get("location"),
                "via": (j.get("via") or "").replace("via ", ""),
                "posted_at_raw": j.get("detected_extensions", {}).get("posted_at"),
                "posted_date": p[0],
                "posted_precision": p[1],
                "work_mode": classify_work_mode(j),
                "schedule": j.get("detected_extensions", {}).get("schedule"),
                "salary_raw": j.get("detected_extensions", {}).get("salary"),
                "position": j.get("position"),
                "description": strip_html(j.get("description"))[:12_000] or None,
                "skills": skills,
                "years_experience": years,
                **parse_salary(j),
            }
        )
    return rows


def run_snapshot(
    cities: list[str] | None = None,
    query: str = QUERY,
    max_pages: int = 2,
    db_path: Path | str = DB_PATH,
    pause_between_cities: float = 2.0,
) -> int:
    """Fetch, normalize, and store one snapshot. Returns rows written."""
    snapshot = datetime.now(tz=UTC).date()
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    migrate(conn)
    written = 0

    for city in cities or CITIES:
        jobs, location_used = fetch_city(query, city, max_pages=max_pages)
        rows = normalize_jobs(jobs, city, query, snapshot)
        if rows:
            saved = save_snapshot(conn, rows)
            written += saved
            print(
                f"{city.split(',')[0]}: saved {saved} rows "
                f"(resolved as {location_used})"
            )
        else:
            print(f"{city.split(',')[0]}: no rows")
        time.sleep(pause_between_cities)

    send_slack_alerts(conn, snapshot.isoformat(), query)
    conn.close()
    return written


def previous_snapshot_date(
    conn: sqlite3.Connection, snapshot_date: str, query: str
) -> str | None:
    row = conn.execute(
        "SELECT MAX(snapshot_date) FROM snapshots "
        "WHERE snapshot_date < ? AND query = ?",
        (snapshot_date, query),
    ).fetchone()
    return row[0] if row and row[0] else None


def new_posting_rows(
    conn: sqlite3.Connection, snapshot_date: str, query: str
) -> list[sqlite3.Row]:
    """Rows present today and absent from the previous snapshot for this query."""
    conn.row_factory = sqlite3.Row
    prev = previous_snapshot_date(conn, snapshot_date, query)
    if not prev:
        return []
    return list(
        conn.execute(
            """
            SELECT * FROM snapshots
            WHERE snapshot_date = ? AND query = ?
              AND job_key NOT IN (
                  SELECT job_key FROM snapshots
                  WHERE snapshot_date = ? AND query = ?
              )
            ORDER BY city, company, title
            """,
            (snapshot_date, query, prev, query),
        )
    )


def alert_criteria() -> dict:
    floor_raw = os.environ.get("ALERT_SALARY_FLOOR", "").strip()
    companies = [
        part.strip()
        for part in os.environ.get("ALERT_COMPANIES", "").split(",")
        if part.strip()
    ]
    return {
        "salary_floor": float(floor_raw) if floor_raw else None,
        "remote_only": os.environ.get("ALERT_REMOTE_ONLY", "").lower()
        in {"1", "true", "yes"},
        "companies": companies,
    }


def matches_alert(row: sqlite3.Row | dict, criteria: dict) -> bool:
    work_mode = row["work_mode"]
    salary_min = row["salary_min"]
    company = row["company"] or ""
    if criteria["remote_only"] and work_mode != "remote":
        return False
    if criteria["salary_floor"] is not None:
        if salary_min is None or salary_min < criteria["salary_floor"]:
            return False
    if criteria["companies"]:
        lowered = company.lower()
        if not any(name.lower() in lowered for name in criteria["companies"]):
            return False
    return True


def format_alert_text(rows: list[sqlite3.Row], query: str) -> str:
    lines = [f"*{len(rows)} new {query} posting(s)*"]
    for row in rows[:20]:
        salary = (
            f"${int(row['salary_min'] / 1000)}k+"
            if row["salary_min"]
            else "salary n/a"
        )
        skills = row["skills"] or "skills n/a"
        lines.append(
            f"• *{row['title']}* at {row['company']} ({row['city']}) — "
            f"{row['work_mode']} — {salary} — {skills}"
        )
    if len(rows) > 20:
        lines.append(f"…and {len(rows) - 20} more")
    return "\n".join(lines)


def send_slack_alerts(
    conn: sqlite3.Connection, snapshot_date: str, query: str
) -> int:
    """Push filtered new postings to Slack. No-op without SLACK_WEBHOOK_URL."""
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        return 0
    matches = [
        row
        for row in new_posting_rows(conn, snapshot_date, query)
        if matches_alert(row, alert_criteria())
    ]
    if not matches:
        print("alerts: no new postings matched filters")
        return 0
    text = format_alert_text(matches, query)
    resp = requests.post(webhook, json={"text": text}, timeout=15)
    if resp.status_code >= 300:
        print(f"alerts: Slack HTTP {resp.status_code}: {resp.text[:200]}")
        return 0
    print(f"alerts: posted {len(matches)} new listing(s) to Slack")
    return len(matches)


def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot Google Jobs postings")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=2,
        help="Pages per city (each page is one API search)",
    )
    parser.add_argument(
        "--one-city",
        action="store_true",
        help="Fetch only the first city (cheap smoke test)",
    )
    parser.add_argument(
        "--query",
        default=QUERY,
        help="Google Jobs search query",
    )
    args = parser.parse_args()
    cities = CITIES[:1] if args.one_city else CITIES
    run_snapshot(cities=cities, query=args.query, max_pages=args.max_pages)


if __name__ == "__main__":
    main()
