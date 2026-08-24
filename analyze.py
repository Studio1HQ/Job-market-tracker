"""Load snapshots and chart hiring demand over time."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from tracker import DB_PATH, QUERY, ROOT

IMAGES = ROOT / "images"


def load_snapshots(db_path: Path | str = DB_PATH) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
            "SELECT * FROM snapshots", conn, parse_dates=["snapshot_date"]
        )
    finally:
        conn.close()
    return df


def salary_coverage(df: pd.DataFrame) -> tuple[float, float]:
    """Return (percent with salary, percent of those from detected_extensions)."""
    if df.empty:
        return 0.0, 0.0
    has_salary = df.salary_min.notna().mean() * 100
    disclosed = df[df.salary_min.notna()]
    if disclosed.empty:
        return float(has_salary), 0.0
    from_field = disclosed.salary_source.eq("detected_extensions").mean() * 100
    return float(has_salary), float(from_field)


def daily_churn(df: pd.DataFrame) -> pd.DataFrame:
    """New vs gone job keys between consecutive snapshot dates."""
    if df.empty:
        return pd.DataFrame(columns=["new", "gone"])
    by_day = df.groupby("snapshot_date")["job_key"].apply(set)
    if len(by_day) < 2:
        return pd.DataFrame(columns=["new", "gone"])
    churn = pd.DataFrame(
        {
            "new": [
                len(by_day.iloc[i] - by_day.iloc[i - 1])
                for i in range(1, len(by_day))
            ],
            "gone": [
                len(by_day.iloc[i - 1] - by_day.iloc[i])
                for i in range(1, len(by_day))
            ],
        },
        index=by_day.index[1:],
    )
    return churn


def chart_postings_by_city(
    df: pd.DataFrame,
    query: str = QUERY,
    out_path: Path | str | None = None,
) -> Path:
    daily = (
        df.groupby(["snapshot_date", "city"])["job_key"]
        .nunique()
        .unstack(fill_value=0)
        .sort_index()
    )
    fig, ax = plt.subplots(figsize=(11, 5))
    daily.plot(ax=ax, marker="o", linewidth=1.8, markersize=4)
    ax.set_title(f'Live "{query}" postings by city')
    ax.set_xlabel("")
    ax.set_ylabel("distinct postings")
    ax.grid(alpha=0.25)
    ax.legend(title=None, frameon=False, ncol=5)
    fig.tight_layout()
    path = Path(out_path) if out_path else IMAGES / "postings_by_city.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def chart_remote_share(
    df: pd.DataFrame,
    out_path: Path | str | None = None,
) -> Path:
    latest = df[df.snapshot_date == df.snapshot_date.max()]
    share = (
        latest.assign(is_remote=latest.work_mode.eq("remote"))
        .groupby("city")["is_remote"]
        .mean()
        .mul(100)
        .sort_values(ascending=True)
    )
    fig, ax = plt.subplots(figsize=(8, 4.5))
    share.plot.barh(ax=ax, color="#3b6ea5")
    ax.set_title("Share of postings classified remote")
    ax.set_xlabel("% of listings")
    ax.set_ylabel("")
    ax.bar_label(ax.containers[0], fmt="%.0f%%", padding=3)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    path = Path(out_path) if out_path else IMAGES / "remote_share.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Chart job-tracker snapshots")
    parser.add_argument("--db", default=str(DB_PATH), help="Path to jobs.db")
    args = parser.parse_args()

    df = load_snapshots(args.db)
    if df.empty:
        raise SystemExit(
            f"No snapshots in {args.db}. Run python tracker.py first."
        )

    has_salary, from_field = salary_coverage(df)
    print(
        f"{has_salary:.0f}% disclose; "
        f"{from_field:.0f}% of those via detected_extensions"
    )

    churn = daily_churn(df)
    if not churn.empty:
        print(churn.describe())
    else:
        print("Need at least two snapshot dates before churn can be computed.")

    postings_path = chart_postings_by_city(df)
    remote_path = chart_remote_share(df)
    print(f"wrote {postings_path}")
    print(f"wrote {remote_path}")


if __name__ == "__main__":
    main()
