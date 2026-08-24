"""Unit and integration tests for the job market tracker."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

import analyze
import tracker

SNAPSHOT = date(2026, 8, 21)

SHARING = (
    "https://www.google.com/search?ibp=htl;jobs"
    "&htidocid=2_EkUK_X1ZOKUz-CAAAAAA%3D%3D&hl=en-US"
)


def sample_job(**overrides) -> dict:
    job = {
        "position": 1,
        "title": "Senior Backend Engineer",
        "company_name": "Acme Corp",
        "location": "Austin, TX",
        "via": "via LinkedIn",
        "description": "About this role. Build APIs in Python.",
        "extensions": ["3 days ago", "Full-time"],
        "detected_extensions": {
            "posted_at": "3 days ago",
            "schedule": "Full-time",
            "salary": "$150K a year",
            "work_from_home": True,
        },
        "apply_link": "https://example.com/apply",
        "sharing_link": SHARING,
    }
    detected = dict(job["detected_extensions"])
    if "detected_extensions" in overrides:
        detected.update(overrides.pop("detected_extensions"))
    job.update(overrides)
    job["detected_extensions"] = detected
    return job


class ParsePostedAtTests(unittest.TestCase):
    def test_missing(self):
        self.assertEqual(tracker.parse_posted_at(None, SNAPSHOT), (None, "missing"))
        self.assertEqual(tracker.parse_posted_at("", SNAPSHOT), (None, "missing"))

    def test_immediate(self):
        for text in ("Just posted", "just now", "Today", "posted today"):
            self.assertEqual(
                tracker.parse_posted_at(text, SNAPSHOT),
                (SNAPSHOT.isoformat(), "day"),
                msg=text,
            )

    def test_days_ago(self):
        posted, precision = tracker.parse_posted_at("3 days ago", SNAPSHOT)
        self.assertEqual(posted, "2026-08-18")
        self.assertEqual(precision, "day")

    def test_hours_ago(self):
        posted, precision = tracker.parse_posted_at("22 hours ago", SNAPSHOT)
        self.assertEqual(posted, SNAPSHOT.isoformat())
        self.assertEqual(precision, "day")

    def test_weeks_are_approximate(self):
        posted, precision = tracker.parse_posted_at("2 weeks ago", SNAPSHOT)
        self.assertEqual(posted, (SNAPSHOT - timedelta(weeks=2)).isoformat())
        self.assertEqual(precision, "approx")

    def test_floor_plus(self):
        posted, precision = tracker.parse_posted_at("30+ days ago", SNAPSHOT)
        self.assertEqual(posted, "2026-07-22")
        self.assertEqual(precision, "floor")

    def test_floor_hint(self):
        _, precision = tracker.parse_posted_at("over 3 months ago", SNAPSHOT)
        self.assertEqual(precision, "floor")

    def test_unparsed(self):
        self.assertEqual(
            tracker.parse_posted_at("last Tuesday", SNAPSHOT),
            (None, "unparsed"),
        )


class ClassifyWorkModeTests(unittest.TestCase):
    def test_flagged_remote(self):
        job = sample_job()
        self.assertEqual(tracker.classify_work_mode(job), "remote")

    def test_remote_flag_with_hybrid_becomes_hybrid(self):
        job = sample_job(description="This is a hybrid role in Austin.")
        self.assertEqual(tracker.classify_work_mode(job), "hybrid")

    def test_remote_flag_with_onsite_mention_becomes_hybrid(self):
        job = sample_job(description="You will work onsite with the platform team.")
        self.assertEqual(tracker.classify_work_mode(job), "hybrid")

    def test_location_says_remote(self):
        job = sample_job(
            location="Remote",
            detected_extensions={"work_from_home": None, "posted_at": "1 day ago"},
        )
        job["detected_extensions"]["work_from_home"] = None
        self.assertEqual(tracker.classify_work_mode(job), "remote")

    def test_hybrid_from_days_per_week(self):
        job = sample_job(
            detected_extensions={"work_from_home": None},
            description="Work 3 days per week in the office downtown.",
        )
        self.assertEqual(tracker.classify_work_mode(job), "hybrid")

    def test_onsite_not_folded_into_hybrid(self):
        job = sample_job(
            detected_extensions={"work_from_home": None},
            description="You will work onsite with the platform team.",
        )
        self.assertEqual(tracker.classify_work_mode(job), "onsite")

    def test_flagged_false_is_onsite(self):
        job = sample_job(detected_extensions={"work_from_home": False})
        self.assertEqual(tracker.classify_work_mode(job), "onsite")

    def test_unknown_when_no_signal(self):
        job = sample_job(
            detected_extensions={"work_from_home": None},
            description="Build APIs in Python.",
        )
        self.assertEqual(tracker.classify_work_mode(job), "unknown")

    def test_benefits_boilerplate_after_2000_chars_is_ignored(self):
        padding = "x" * 2000
        job = sample_job(
            detected_extensions={"work_from_home": None},
            description=padding + " We offer remote and work from home options.",
        )
        self.assertEqual(tracker.classify_work_mode(job), "unknown")

    def test_html_tags_are_stripped_before_window(self):
        padding = "<p>xxxx</p>" * 180
        job = sample_job(
            detected_extensions={"work_from_home": None},
            description=padding + " you will work onsite with the platform team",
        )
        self.assertEqual(tracker.classify_work_mode(job), "onsite")


class ParseSalaryTests(unittest.TestCase):
    def test_structured_range(self):
        job = sample_job(
            detected_extensions={"salary": "$132,500 - $157,500 a year"}
        )
        result = tracker.parse_salary(job)
        self.assertEqual(result["salary_min"], 132500)
        self.assertEqual(result["salary_max"], 157500)
        self.assertEqual(result["salary_source"], "detected_extensions")

    def test_hourly_annualizes(self):
        job = sample_job(detected_extensions={"salary": "$55 - $70 an hour"})
        result = tracker.parse_salary(job)
        self.assertEqual(result["salary_min"], 55 * 2080)
        self.assertEqual(result["salary_max"], 70 * 2080)

    def test_ceiling_recorded_as_point(self):
        job = sample_job(detected_extensions={"salary": "Up to $180,000 a year"})
        result = tracker.parse_salary(job)
        self.assertEqual(result["salary_min"], 180000)
        self.assertEqual(result["salary_max"], 180000)

    def test_non_dollar_discarded(self):
        job = sample_job(detected_extensions={"salary": "£50,000 - £65,000 a year"})
        result = tracker.parse_salary(job)
        self.assertIsNone(result["salary_min"])
        self.assertIsNone(result["salary_source"])

    def test_equity_wins_then_fails_sanity_floor(self):
        job = sample_job(
            detected_extensions={"salary": None},
            description=(
                "Equity grant of $2,000 - $8,000. "
                "Base salary $140,000 - $170,000."
            ),
        )
        result = tracker.parse_salary(job)
        self.assertIsNone(result["salary_min"])

    def test_falls_back_to_description(self):
        job = sample_job(
            detected_extensions={"salary": None},
            description="Compensation: $140,000 - $170,000 a year.",
        )
        result = tracker.parse_salary(job)
        self.assertEqual(result["salary_min"], 140000)
        self.assertEqual(result["salary_max"], 170000)
        self.assertEqual(result["salary_source"], "description")

    def test_k_suffix(self):
        job = sample_job(detected_extensions={"salary": "$150K a year"})
        result = tracker.parse_salary(job)
        self.assertEqual(result["salary_min"], 150000)

    def test_unsigned_k_from_structured_field(self):
        job = sample_job(detected_extensions={"salary": "125K a year"})
        result = tracker.parse_salary(job)
        self.assertEqual(result["salary_min"], 125000)
        self.assertEqual(result["salary_max"], 125000)
        self.assertEqual(result["salary_source"], "detected_extensions")

    def test_swaps_inverted_range(self):
        job = sample_job(detected_extensions={"salary": "$170,000 - $140,000 a year"})
        result = tracker.parse_salary(job)
        self.assertEqual(result["salary_min"], 140000)
        self.assertEqual(result["salary_max"], 170000)


class JobKeyTests(unittest.TestCase):
    def test_extracts_htidocid(self):
        self.assertEqual(tracker.job_key(sample_job()), "2_EkUK_X1ZOKUz-CAAAAAA==")

    def test_hash_fallback_is_stable(self):
        job = sample_job(sharing_link="")
        first = tracker.job_key(job)
        second = tracker.job_key(job)
        self.assertTrue(first.startswith("h:"))
        self.assertEqual(first, second)

    def test_hash_changes_with_title(self):
        a = tracker.job_key(sample_job(sharing_link="", title="A"))
        b = tracker.job_key(sample_job(sharing_link="", title="B"))
        self.assertNotEqual(a, b)


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "jobs.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.executescript(tracker.SCHEMA)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_idempotent_replace(self):
        rows = tracker.normalize_jobs(
            [sample_job()], "Austin,Texas,United States", tracker.QUERY, SNAPSHOT
        )
        self.assertEqual(tracker.save_snapshot(self.conn, rows), 1)
        rows[0]["title"] = "Updated Title"
        self.assertEqual(tracker.save_snapshot(self.conn, rows), 1)
        count = self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        title = self.conn.execute("SELECT title FROM snapshots").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(title, "Updated Title")

    def test_normalize_fills_expected_columns(self):
        rows = tracker.normalize_jobs(
            [sample_job()], "Austin,Texas,United States", tracker.QUERY, SNAPSHOT
        )
        row = rows[0]
        self.assertEqual(row["city"], "Austin")
        self.assertEqual(row["via"], "LinkedIn")
        self.assertEqual(row["posted_date"], "2026-08-18")
        self.assertEqual(row["work_mode"], "remote")
        self.assertEqual(row["salary_min"], 150000)
        self.assertEqual(row["salary_raw"], "$150K a year")

    def test_migrate_adds_salary_raw(self):
        self.conn.execute("DROP TABLE snapshots")
        self.conn.executescript(
            """
            CREATE TABLE snapshots (
                snapshot_date TEXT NOT NULL,
                job_key TEXT NOT NULL,
                query TEXT NOT NULL,
                city TEXT NOT NULL,
                PRIMARY KEY (snapshot_date, city, query, job_key)
            );
            """
        )
        tracker.migrate(self.conn)
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(snapshots)")}
        self.assertIn("salary_raw", cols)


class FetchCityTests(unittest.TestCase):
    def _response(self, status, payload):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = payload
        resp.text = json.dumps(payload)
        return resp

    @patch.dict("os.environ", {"SEARCHAPI_KEY": "test-key"})
    @patch("tracker.time.sleep")
    @patch("tracker.requests.get")
    def test_walks_pages_and_stops(self, mock_get, _sleep):
        page1 = {
            "search_parameters": {"location_used": "Austin,Texas,United States"},
            "jobs": [sample_job()],
            "pagination": {"next_page_token": "tok-2"},
        }
        page2 = {
            "search_parameters": {"location_used": "Austin,Texas,United States"},
            "jobs": [sample_job(title="Staff Backend Engineer", sharing_link="")],
            "pagination": {},
        }
        mock_get.side_effect = [
            self._response(200, page1),
            self._response(200, page2),
        ]
        jobs, location_used = tracker.fetch_city(
            "backend engineer", "Austin,Texas,United States", max_pages=2, pause=0
        )
        self.assertEqual(len(jobs), 2)
        self.assertEqual(location_used, "Austin,Texas,United States")
        self.assertEqual(mock_get.call_count, 2)
        second_params = mock_get.call_args_list[1].kwargs["params"]
        self.assertEqual(second_params["next_page_token"], "tok-2")

    @patch.dict("os.environ", {"SEARCHAPI_KEY": "test-key"})
    @patch("tracker.time.sleep")
    @patch("tracker.requests.get")
    def test_retries_429_without_skipping_page(self, mock_get, mock_sleep):
        payload = {
            "search_parameters": {"location_used": "Austin,Texas,United States"},
            "jobs": [sample_job()],
            "pagination": {},
        }
        limited = MagicMock()
        limited.status_code = 429
        limited.text = "rate limited"
        mock_get.side_effect = [limited, self._response(200, payload)]
        jobs, _ = tracker.fetch_city(
            "backend engineer", "Austin,Texas,United States", max_pages=1, pause=0
        )
        self.assertEqual(len(jobs), 1)
        mock_sleep.assert_called_with(60)

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_api_key_exits(self):
        with self.assertRaises(SystemExit):
            tracker.api_key()


def seed_history(db_path: Path, days: int = 30) -> None:
    """Write a 30-day fixture series so charts and churn have something to plot."""
    cities = ["Austin", "Denver", "Seattle", "Atlanta", "New York"]
    baselines = {"Austin": 47, "Denver": 22, "Seattle": 38, "Atlanta": 29, "New York": 41}
    conn = sqlite3.connect(db_path)
    conn.executescript(tracker.SCHEMA)
    rows = []
    start = SNAPSHOT - timedelta(days=days - 1)
    for offset in range(days):
        day = start + timedelta(days=offset)
        week = offset // 7
        for city in cities:
            count = baselines[city]
            if city == "Austin":
                count = max(20, 47 - week * 4)
            elif city == "Denver":
                count = 22 + week * 3
            for i in range(count):
                remote = i % 5 == 0 if city != "New York" else i % 8 == 0
                has_salary = i % 3 == 0
                rows.append(
                    {
                        "snapshot_date": day.isoformat(),
                        "job_key": f"{city}-{i}",
                        "query": tracker.QUERY,
                        "city": city,
                        "title": f"Backend Engineer {i}",
                        "company": "Acme" if i % 17 else "Staffing Co",
                        "location": city,
                        "via": "LinkedIn",
                        "posted_at_raw": "3 days ago",
                        "posted_date": (day - timedelta(days=3)).isoformat(),
                        "posted_precision": "day",
                        "work_mode": "remote" if remote else "onsite",
                        "schedule": "Full-time",
                        "salary_min": 140000.0 if has_salary else None,
                        "salary_max": 170000.0 if has_salary else None,
                        "salary_source": "detected_extensions" if has_salary else None,
                        "position": i + 1,
                    }
                )
    tracker.save_snapshot(conn, rows)
    conn.close()


class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "jobs.db"
        self.images = Path(self.tmp.name) / "images"
        seed_history(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_salary_coverage_and_churn(self):
        df = analyze.load_snapshots(self.db)
        has_salary, from_field = analyze.salary_coverage(df)
        self.assertGreater(has_salary, 20)
        self.assertEqual(from_field, 100)
        churn = analyze.daily_churn(df)
        self.assertEqual(len(churn), 29)
        self.assertTrue((churn["new"] >= 0).all())

    def test_charts_write_pngs(self):
        df = analyze.load_snapshots(self.db)
        postings = analyze.chart_postings_by_city(
            df, out_path=self.images / "postings_by_city.png"
        )
        remote = analyze.chart_remote_share(df, out_path=self.images / "remote_share.png")
        self.assertTrue(postings.exists() and postings.stat().st_size > 0)
        self.assertTrue(remote.exists() and remote.stat().st_size > 0)

    def test_run_snapshot_with_mocked_fetch(self):
        with (
            patch("tracker.fetch_city") as mock_fetch,
            patch("tracker.time.sleep"),
        ):
            mock_fetch.return_value = ([sample_job()], "Austin,Texas,United States")
            written = tracker.run_snapshot(
                cities=["Austin,Texas,United States"],
                max_pages=1,
                db_path=self.db,
                pause_between_cities=0,
            )
        self.assertGreaterEqual(written, 1)
        df = analyze.load_snapshots(self.db)
        self.assertIn("Austin", set(df.city))


if __name__ == "__main__":
    unittest.main()
