"""Point-in-time index membership: who was in the index on a given day.

Members that later left are kept, with the date they left, so a universe built from
this does not condition on survival. What this avoids is the bias you introduce by
reconstructing a historical universe from today's membership list.

    from datetime import date
    import index_constituents as ic

    ic.as_of("sp500", date(2004, 6, 30))       # frozenset of tickers, as of that day
    ic.members("sp500")                        # every membership run ever
    ic.tickers("sp500", start, end)            # tickers that were members in a window
    ic.renames("sp500")                        # symbol changes, evidenced by CIK
    ic.discrepancies("sp500")                  # every place the sources disagreed

To pull prices for a bias-free universe, ask for the download spans and loop.
`renames` says where a renamed line's history lives (providers keep it under the
new symbol), and a dead ticker is often a different security today (BSC is an ETN
now), so map era tickers to provider symbols before requesting.

    for span in ic.spans("sp500", start=date(2005, 1, 1), end=date(2020, 1, 1), pad_days=250):
        bars = my_provider.fetch(span.ticker, span.start, span.end)

`spans` gives one row per symbol with the exact window it was ever a member, padded
at the front for indicator warm-up. `securities` is the same data grouped by company
with rename chains followed, for when a continuous series across a symbol change
matters:

    for sec in ic.securities("sp500"):
        sec.tickers            # ('AA', 'ARNC', 'HWM'): one company, three symbols
        sec.ticker_on(day)     # the symbol it traded under on that day

Reads data/<index>/membership.csv, built by `python -m index_constituents.sp500.build`
from S&P's own archived constituent files and Wikipedia. Building needs the network;
reading does not, and has no dependencies beyond the standard library.

Every boundary carries a `confidence` saying how well its date is pinned:

    exact          an effective date from a cited change record
    snapshot       only known to fall between two snapshots; the date is the later
                   one, so a stated start is never earlier than the truth and
                   neither is a stated end; both err toward keeping a name in
                   the universe slightly too long, never toward dropping it early
    unreconciled   a snapshot change no record explains, where records should exist
    left_censored  already a member at the first snapshot, so the true start is
                   before the data begins

If you need only well-dated boundaries, filter on confidence.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import cache
from pathlib import Path

# In a wheel the artifacts sit inside the package; in a checkout (or editable
# install) they stay at the repo root next to the raw sources they were built from.
DATA = Path(__file__).resolve().parent / "data"
if not (DATA / "sp500").is_dir():
    DATA = Path(__file__).resolve().parent.parent.parent / "data"

__all__ = [
    "Discrepancy",
    "Listing",
    "Membership",
    "Rename",
    "Security",
    "Span",
    "as_of",
    "coverage",
    "discrepancies",
    "members",
    "renames",
    "securities",
    "spans",
    "summary",
    "tickers",
    "to_frame",
]


@dataclass(frozen=True, slots=True)
class Membership:
    """One membership run. `end` is None while the ticker is still a member."""

    ticker: str
    company: str
    start: date
    end: date | None
    start_confidence: str
    end_confidence: str | None
    cik: str = ""
    venue: str = ""
    gics_sector: str = ""
    gics_sub_industry: str = ""
    sp_date_added: str = ""

    def contains(self, day: date) -> bool:
        return self.start <= day and (self.end is None or day < self.end)


@dataclass(frozen=True, slots=True)
class Rename:
    """A symbol change where both symbols share one SEC filer id.

    Not an index event, so it appears in membership.csv as two runs. Use this to
    stitch a continuous price series across the change: WLP and ANTM are one company.
    """

    date: date
    old: str
    new: str
    cik: str
    company: str


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """One place the sources disagreed, reproduced from discrepancies.csv."""

    date: date
    kind: str
    ticker: str
    detail: str


@dataclass(frozen=True, slots=True)
class Listing:
    """One symbol a security traded under while it was in the index."""

    ticker: str
    start: date
    end: date | None

    def contains(self, day: date) -> bool:
        return self.start <= day and (self.end is None or day < self.end)


@dataclass(frozen=True, slots=True)
class Security:
    """One company across every symbol it used while in the index."""

    company: str
    cik: str
    venue: str
    gics_sector: str
    gics_sub_industry: str
    listings: tuple[Listing, ...]

    @property
    def ticker(self) -> str:
        """The most recent symbol, which is the natural name for the series."""
        return self.listings[-1].ticker

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(listing.ticker for listing in self.listings))

    @property
    def start(self) -> date:
        return self.listings[0].start

    @property
    def end(self) -> date | None:
        return None if any(listing.end is None for listing in self.listings) \
            else max(listing.end for listing in self.listings)

    def ticker_on(self, day: date) -> str | None:
        return next((listing.ticker for listing in self.listings if listing.contains(day)), None)

    def held_on(self, day: date) -> bool:
        return self.ticker_on(day) is not None


@dataclass(frozen=True, slots=True)
class Span:
    """A symbol and the window of history worth downloading for it.

    `start` includes any warm-up padding asked for, so it can precede membership;
    `first_member` and `last_member` are the unpadded membership bounds.
    """

    ticker: str
    start: date
    end: date | None
    first_member: date
    last_member: date | None
    runs: int
    company: str
    cik: str
    venue: str


def _parse(value: str) -> date | None:
    return datetime.strptime(value, "%Y-%m-%d").date() if value else None


@cache
def members(index: str = "sp500") -> tuple[Membership, ...]:
    path = DATA / index / "membership.csv"
    if not path.exists():
        raise FileNotFoundError(f"no membership.csv for {index!r}; build it with "
                                f"`python -m index_constituents.{index}.build`")
    with path.open(newline="") as f:
        return tuple(
            Membership(
                ticker=row["ticker"],
                company=row["company"],
                start=_parse(row["start"]),
                end=_parse(row["end"]),
                start_confidence=row["start_confidence"],
                end_confidence=row["end_confidence"] or None,
                cik=row.get("cik", ""),
                venue=row.get("venue", ""),
                gics_sector=row.get("gics_sector", ""),
                gics_sub_industry=row.get("gics_sub_industry", ""),
                sp_date_added=row.get("sp_date_added", "")
            ) for row in csv.DictReader(f)
        )


@cache
def renames(index: str = "sp500") -> tuple[Rename, ...]:
    path = DATA / index / "renames.csv"
    if not path.exists():
        return ()
    with path.open(newline="") as f:
        return tuple(
            Rename(
                date=_parse(r["date"]),
                old=r["old_ticker"],
                new=r["new_ticker"],
                cik=r["cik"],
                company=r["company"]
            ) for r in csv.DictReader(f)
        )


@cache
def discrepancies(index: str = "sp500") -> tuple[Discrepancy, ...]:
    path = DATA / index / "discrepancies.csv"
    if not path.exists():
        return ()
    with path.open(newline="") as f:
        return tuple(
            Discrepancy(
                date=_parse(r["date"]),
                kind=r["kind"],
                ticker=r["ticker"],
                detail=r["detail"]
            ) for r in csv.DictReader(f)
        )


def as_of(index: str, day: date) -> frozenset[str]:
    return frozenset(m.ticker for m in members(index) if m.contains(day))


def tickers(index: str = "sp500", start: date | None = None,
            end: date | None = None) -> frozenset[str]:
    """Every ticker that was a member at any point in the window."""
    return frozenset(m.ticker for m in members(index) if _overlaps(m, start, end))


def coverage(index: str = "sp500") -> tuple[date, date]:
    rows = members(index)
    return min(m.start for m in rows), max((m.end for m in rows if m.end), default=date.today())


def summary(index: str = "sp500") -> dict:
    return json.loads((DATA / index / "summary.json").read_text())


def _overlaps(m: Membership, start: date | None, end: date | None) -> bool:
    return (end is None or m.start <= end) and (start is None or m.end is None or m.end > start)


def spans(index: str = "sp500", start: date | None = None,
          end: date | None = None, pad_days: int = 0) -> tuple[Span, ...]:
    """One download window per symbol: everything you need to fetch, and no more.

    The window covers every run the symbol had, so a symbol that was a member twice
    yields one span spanning both: you download a series once, and a gap in the
    middle costs nothing. `pad_days` extends the start backwards for indicator
    warm-up; it is not membership, just how much history to pull.

    `end` is None for a symbol that is still a member, meaning "up to now".
    """
    grouped: dict[str, list[Membership]] = {}
    for member in members(index):
        if _overlaps(member, start, end):
            grouped.setdefault(member.ticker, []).append(member)

    out = []
    for ticker, runs in sorted(grouped.items()):
        first_member = min(r.start for r in runs)
        last_member = None if any(r.end is None for r in runs) else max(r.end for r in runs)

        fetch_from = max(first_member, start) if start else first_member
        fetch_to = last_member if end is None else min(last_member or end, end)

        newest = max(runs, key=lambda r: r.start)
        out.append(
            Span(
                ticker=ticker,
                start=fetch_from - timedelta(days=pad_days),
                end=fetch_to,
                first_member=first_member,
                last_member=last_member,
                runs=len(runs),
                company=newest.company,
                cik=newest.cik,
                venue=newest.venue
            )
        )
    return tuple(out)


def securities(index: str = "sp500", start: date | None = None, end: date | None = None) -> tuple[Security, ...]:
    """Membership grouped by company, with rename chains followed.

    Alcoa, Arconic and Howmet are one Security with three listings, so a continuous
    price series can be assembled across the symbol changes. Dual share classes stay
    separate securities even though they share an issuer: GOOG and GOOGL are two
    tradable lines, not one renamed.
    """
    parent: dict[str, str] = {}

    def find(t: str) -> str:
        parent.setdefault(t, t)
        while parent[t] != t:
            parent[t] = parent[parent[t]]
            t = parent[t]
        return t

    for r in renames(index):
        a, b = find(r.old), find(r.new)
        if a != b:
            parent[b] = a

    groups: dict[str, list[Membership]] = {}
    for m in members(index):
        if _overlaps(m, start, end):
            groups.setdefault(find(m.ticker), []).append(m)

    out = []
    for runs in groups.values():
        runs.sort(key=lambda x: x.start)
        newest = runs[-1]
        out.append(
            Security(
                company=newest.company,
                cik=newest.cik,
                venue=newest.venue,
                gics_sector=newest.gics_sector,
                gics_sub_industry=newest.gics_sub_industry,
                listings=tuple(Listing(ticker=m.ticker, start=m.start, end=m.end) for m in runs)
            )
        )
    return tuple(sorted(out, key=lambda s: (s.listings[0].start, s.ticker)))


def to_frame(index: str = "sp500"):
    """membership.csv as a pandas DataFrame. pandas are not a dependency of this package."""
    try:
        import pandas as pd
    except ImportError as e:  # noqa: TRY003
        raise ImportError("to_frame needs pandas; install it or use members()") from e
    return pd.read_csv(DATA / index / "membership.csv", parse_dates=["start", "end"])
