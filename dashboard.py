"""Filterable Streamlit dashboard over the snapshot database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from analyze import load_snapshots
from tracker import DB_PATH, QUERY, SCHEMA, migrate, new_posting_rows

st.set_page_config(page_title="Job market tracker", layout="wide")


@st.cache_data(ttl=60)
def load_data(db_path: str) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.close()
    return load_snapshots(db_path)


def new_today(db_path: str, snapshot_date: str, query: str) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    migrate(conn)
    rows = new_posting_rows(conn, snapshot_date, query)
    conn.close()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(row) for row in rows])


def main() -> None:
    st.title("Job market tracker")
    db_path = str(DB_PATH)
    if not Path(db_path).exists():
        st.error("No jobs.db yet. Run `python tracker.py` first.")
        return

    df = load_data(db_path)
    if df.empty:
        st.warning("The database has no snapshot rows.")
        return

    cities = sorted(df.city.dropna().unique())
    min_date = df.snapshot_date.min().date()
    max_date = df.snapshot_date.max().date()

    left, right = st.columns(2)
    with left:
        selected_cities = st.multiselect("City", cities, default=cities)
    with right:
        date_range = st.date_input(
            "Date range",
            value=(min_date, max_date),
            min_value=min_date,
            max_value=max_date,
        )

    if isinstance(date_range, tuple) and len(date_range) == 2:
        start, end = date_range
    else:
        start = end = date_range

    view = df[
        df.city.isin(selected_cities)
        & (df.snapshot_date.dt.date >= start)
        & (df.snapshot_date.dt.date <= end)
    ]
    daily = (
        view.groupby(["snapshot_date", "city"])["job_key"]
        .nunique()
        .unstack(fill_value=0)
        .sort_index()
    )
    st.subheader(f'Live "{QUERY}" postings')
    if daily.empty:
        st.info("No rows in this filter.")
    else:
        st.line_chart(daily)

    latest = df.snapshot_date.max().date().isoformat()
    st.subheader(f"New postings on {latest}")
    fresh = new_today(db_path, latest, QUERY)
    if selected_cities and not fresh.empty:
        fresh = fresh[fresh.city.isin(selected_cities)]
    if fresh.empty:
        st.info("No new keys versus the previous snapshot (or this is day 1).")
    else:
        show = fresh[
            [
                col
                for col in (
                    "city",
                    "title",
                    "company",
                    "work_mode",
                    "salary_min",
                    "skills",
                    "years_experience",
                )
                if col in fresh.columns
            ]
        ]
        st.dataframe(show, use_container_width=True, hide_index=True)

    if "skills" in view.columns and view.skills.notna().any():
        st.subheader("Skills in this range")
        skilled = view.dropna(subset=["skills"]).copy()
        skilled["skill"] = skilled.skills.str.split(",")
        exploded = skilled.explode("skill")
        exploded = exploded[exploded.skill.astype(str).str.len() > 0]
        counts = (
            exploded.groupby("skill")["job_key"].nunique().sort_values(ascending=False)
        )
        st.bar_chart(counts.head(15))


if __name__ == "__main__":
    main()
