"""Build data/sp500/membership.csv from the vendored S&P files plus Wikipedia.

    uv run python -m index_constituents.sp500.build [--every-days 14]

Era split, and why:

    2000-12-29 .. 2007-04-16   S&P's own constituent files, recovered from the
                               Wayback Machine. Authoritative, 41 dated snapshots,
                               irregular: 2001 and 2002 are year-end only.
    2007-04-16 .. today        Revision history of the Wikipedia list article,
                               sampled every two weeks.
    2011 .. today              The Wikipedia changes table, for exact effective
                               dates. Thin for 2007-2010 and empty before, which is
                               why the archive era cannot use it.

The snapshot sources overlap for three weeks in 2007 and they disagree there:
Caremark, Phelps Dodge and Univision all left the index in March 2007, S&P's
2007-04-16 file correctly omits them, and the list article still carried them in May.
So the archive owns every date up to its last file, and the article is only consulted
after it.

Writes membership.csv (the artifact), discrepancies.csv (every place the sources
disagreed, unresolved) and summary.json.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date, timedelta
from pathlib import Path

from .. import _reconcile, _wiki
from . import archive

DATA = Path(__file__).resolve().parents[3] / "data" / "sp500"

LIST_ARTICLE = "List of S&P 500 companies"
CHANGES_ARTICLE = "Historical components of the S&P 500"
ARTICLE_FROM = date(2007, 1, 1)

# The index has held 487-507 names since 2000; below this a revision was caught
# mid-edit and is not a real index state.
MIN_MEMBERS = 470

# The changes table averages 8 rows a year for 2007-2010 against a true rate near 20,
# and is empty before 2007. Only from 2011 does silence mean something is missing
# rather than that the table is thin.
RECORDS_COMPLETE_FROM = date(2011, 1, 1)

# Sampling density trades Wikipedia edit noise against boundary precision. Measured
# over 7/14/21/30 days, all of 14 and up produce the same 330-odd exact boundaries
# (exact dates come from the change records, not from sampling), while 7 days doubles
# the unexplained transitions and the phantom records by surfacing mid-edit revisions.
# 14 keeps the noise low and still halves the window on snapshot-bounded boundaries.
EVERY_DAYS = 14

# a run shorter than this with no record at either end is reported as suspect
SUSPECT_RUN_DAYS = 120

PROBE_DATES = (date(2001, 6, 30), date(2005, 6, 30), date(2010, 6, 30),
               date(2015, 6, 30), date(2020, 6, 30), date(2025, 6, 30))


def build(every_days: int = EVERY_DAYS) -> dict:
    snapshots = archive.snapshots()
    handoff = snapshots[-1].date
    print(f"archive: {len(snapshots)} snapshots, {snapshots[0].date} -> {handoff}")

    article = [s for s in _wiki.snapshots(LIST_ARTICLE, start=ARTICLE_FROM, every_days=every_days)
               if len(s.tickers) >= MIN_MEMBERS]
    events = _wiki.changes(CHANGES_ARTICLE)
    print(f"article: {len(article)} snapshots kept, {len(events)} change records")

    timeline = _reconcile.build(snapshots + [s for s in article if s.date > handoff], events,
                                records_complete_from=RECORDS_COMPLETE_FROM)

    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "membership.csv").open("w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["ticker", "company", "start", "end", "start_confidence", "end_confidence",
                         "cik", "venue", "gics_sector", "gics_sub_industry", "sp_date_added"])
        for i in timeline.intervals:
            stated = timeline.attributes.get((i.ticker, i.start), {})
            writer.writerow([i.ticker, i.company, i.start, i.end or "", i.start_confidence.value,
                             i.end_confidence.value if i.end_confidence else "",
                             stated.get("cik", ""), stated.get("venue", ""),
                             stated.get("gics_sector", ""), stated.get("gics_sub_industry", ""),
                             stated.get("sp_date_added", "")])

    with (DATA / "discrepancies.csv").open("w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["date", "kind", "ticker", "detail"])
        for d in sorted(timeline.discrepancies, key=lambda d: (d.date, d.kind, d.ticker)):
            writer.writerow([d.date, d.kind, d.ticker, d.detail])

    # A short run that no change record explains at either end is the signature of
    # Wikipedia vandalism that outlived one snapshot: "Alpha Athletic Asphalt" (AAA)
    # sat in the list for 17 days in 2025. Surfaced for review, never auto-deleted.
    suspect = [i for i in timeline.intervals
               if i.end and i.start_confidence.value == "unreconciled"
               and (i.end_confidence and i.end_confidence.value == "unreconciled")
               and (i.end - i.start) < timedelta(days=SUSPECT_RUN_DAYS)]

    with (DATA / "renames.csv").open("w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["date", "old_ticker", "new_ticker", "cik", "company"])
        for r in timeline.renames:
            writer.writerow([r.date, r.old, r.new, r.cik, r.company])

    confidence = {c: sum(1 for i in timeline.intervals if i.start_confidence.value == c)
                  for c in sorted({i.start_confidence.value for i in timeline.intervals})}
    kinds = {k: sum(1 for d in timeline.discrepancies if d.kind == k)
             for k in sorted({d.kind for d in timeline.discrepancies})}
    first, last = timeline.coverage()
    summary = {
        "index": "sp500",
        "sources": {"archive_snapshots": len(snapshots), "article_snapshots": len(article),
                    "change_records": len(events), "handoff": str(handoff)},
        "coverage": [str(first), str(last)],
        "tickers": len(timeline.tickers()),
        "intervals": len(timeline.intervals),
        "start_confidence": confidence,
        "discrepancies": kinds,
        "rejected_revisions": [{"date": str(d), "why": why} for d, why in timeline.rejected],
        "proven_renames": len(timeline.renames),
        "suspect_runs": [{"ticker": i.ticker, "company": i.company, "start": str(i.start),
                          "end": str(i.end), "days": (i.end - i.start).days} for i in suspect],
        "identifier_coverage": {
            field: sum(1 for i in timeline.intervals
                       if timeline.attributes.get((i.ticker, i.start), {}).get(field))
            for field in ("cik", "venue", "gics_sector", "gics_sub_industry", "sp_date_added")},
        "members_on": {str(d): len(timeline.as_of(d)) for d in PROBE_DATES},
    }
    (DATA / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    print(f"{len(timeline.intervals)} intervals, {len(timeline.tickers())} tickers, {first} -> {last}")
    print(f"confidence:    {confidence}")
    print(f"discrepancies: {kinds}")
    print(f"proven renames (shared CIK): {len(timeline.renames)}")
    print(f"identifiers: {summary['identifier_coverage']} of {len(timeline.tickers())}")
    print(f"suspect runs: {[(i.ticker, (i.end - i.start).days) for i in suspect]}")
    print(f"rejected revisions: {len(timeline.rejected)}")
    for when, why in timeline.rejected:
        print(f"   {when}  {why}")
    print(f"members on probes: {summary['members_on']}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--every-days", type=int, default=EVERY_DAYS,
                        help=f"article revision sampling interval (default {EVERY_DAYS})")
    build(every_days=parser.parse_args().every_days)


if __name__ == "__main__":
    main()
