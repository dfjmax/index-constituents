"""Reconcile snapshots against dated change records into membership intervals.

Snapshots say who was a member; change records say exactly when membership moved.
Neither is sufficient. Snapshots are ground truth but sparse, and for the Wikipedia
era they lag: editors update the changes table from an S&P press release on the
effective date but often update the list article weeks later. Change records carry
the effective date and a citation, and are the better authority on *when*, but they
are incomplete, badly so before 2007.

So: derive membership from snapshots, then snap each boundary to a change record
where one plausibly matches. A record within `lag_days` before the window still
counts as a match, because that is the editor lag rather than a disagreement about
the facts. Every boundary carries the Confidence that says which case it was, and
every unmatched record and unexplained transition is reported as a Discrepancy.

Nothing here invents a date. Where the sources are silent, the interval boundary is
a snapshot date and is labeled as such rather than interpolated.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

# Editors update the list article from the press release on the effective date but
# sometimes months later: Cooper's removal sat in the article for ten months after
# the record that replaced it with Airgas. 120 days was measured too tight; a year
# and a week covers every stale list observed without letting a record reach across
# an intervening transition of the same ticker.
LAG_DAYS = 372

# Identifiers that cannot legitimately change over a member's life
STABLE_FIELDS = frozenset({"cik", "venue", "sp_date_added"})
GRACE_DAYS = 14

# A name the crowd resurrects this soon after a provider snapshot dropped it, with
# no record behind the return, is the stale handoff article and not an index event.
RESURRECTION_DAYS = 120

# A crowd-era gap no record explains at either end, short of a genuine demote-and-
# readmit (Noble was out 22 months), closed and reopened under the same company
# name: the article briefly lost the row. The worst observed was nine months.
GAP_MERGE_DAYS = 400


class Confidence(StrEnum):
    """How well a membership boundary is pinned down.

    EXACT      an effective date from a cited change record, reconciled against the
               surrounding snapshots. Note that the provider's stated entry date is
               deliberately *not* used to pin a boundary: a change record names both
               the joiner and the leaver with one date, whereas a bare entry date has
               no paired removal, so honoring it moves a start earlier while the
               member it replaced stays until the next snapshot. That put 503 names
               in the index for most of 2001. The stated date is carried as data
               instead, for callers to use with that caveat in mind.
    SNAPSHOT   the boundary is only known to fall between two snapshots; the date
               given is the later snapshot, so a stated start is never earlier than
               the true start and a stated end is never earlier than the true end;
               both err toward keeping a name in the universe slightly too long,
               never toward dropping it early
    UNRECONCILED
               a snapshot delta that no change record explains, in a window where
               change records do exist; something upstream is incomplete
    LEFT_CENSORED
               already a member at the earliest snapshot, so the true start is
               before the series begins and is not recoverable from these sources
    """

    EXACT = "exact"
    SNAPSHOT = "snapshot"
    UNRECONCILED = "unreconciled"
    LEFT_CENSORED = "left_censored"


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The full constituent set on one date. Ground truth, never inferred.

    `primary` marks a snapshot published by the index provider. Those are never
    second-guessed; a crowd-edited one can be discarded as a bad revision.
    """

    date: date
    tickers: frozenset[str]
    source: str
    names: dict[str, str] = field(default_factory=dict)
    attrs: dict[str, dict[str, str]] = field(default_factory=dict)
    primary: bool = False

    def __post_init__(self) -> None:
        if not self.tickers:
            raise ValueError(f"empty snapshot {self.source} {self.date}")


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """One dated index change. S&P always replaces, so most carry both sides."""

    date: date
    added: frozenset[str]
    removed: frozenset[str]
    source: str
    reason: str = ""
    refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Interval:
    """A ticker's membership run. `end` is None while still a member."""

    ticker: str
    start: date
    end: date | None
    start_confidence: Confidence
    end_confidence: Confidence | None
    company: str = ""

    def contains(self, day: date) -> bool:
        return self.start <= day and (self.end is None or day < self.end)


@dataclass(frozen=True, slots=True)
class Rename:
    """One symbol change, evidenced by identifier rather than inferred.

    A ticker change is not an index event and the changes table excludes them by
    policy, so a rename appears only as one run ending and another starting the same
    day. The pairing rests on the two runs having a CIK in common: a stated fact.
    Pairing by company-name similarity instead is guesswork that matches Longs Drug
    Stores to Family Dollar Stores, so a pair with no CIK in common is left unpaired
    and reported as an unexplained transition.

    `cik` is the identifier the two runs share. For a reorganization that is the old
    filer's: Walgreen Co and Walgreens Boots Alliance file separately, and the
    evidence of continuity is that the revisions just after the change still carried
    Walgreen's id on the continued row.
    """

    date: date
    old: str
    new: str
    cik: str
    company: str


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """A place where the sources disagree. Reported, never silently resolved."""

    date: date
    kind: str
    ticker: str
    detail: str


@dataclass(slots=True)
class Timeline:
    intervals: list[Interval]
    discrepancies: list[Discrepancy] = field(default_factory=list)
    renames: list[Rename] = field(default_factory=list)
    names: dict[tuple[str, date], str] = field(default_factory=dict)
    attributes: dict[tuple[str, date], dict[str, str]] = field(default_factory=dict)
    snapshot_dates: list[date] = field(default_factory=list)
    rejected: list[tuple[date, str]] = field(default_factory=list)

    def as_of(self, day: date) -> frozenset[str]:
        return frozenset(i.ticker for i in self.intervals if i.contains(day))

    def tickers(self) -> frozenset[str]:
        return frozenset(i.ticker for i in self.intervals)

    def coverage(self) -> tuple[date, date]:
        return self.snapshot_dates[0], self.snapshot_dates[-1]


@dataclass(slots=True)
class _Boundary:
    """An observed membership transition, before any record is matched to it."""

    ticker: str
    when: date
    opening: bool
    window_start: date
    confidence: Confidence = Confidence.SNAPSHOT


def _dedupe(snapshots: list[Snapshot]) -> list[Snapshot]:
    """One snapshot per date. Earlier entries win, so callers order by trust."""
    by_date: dict[date, Snapshot] = {}
    for snap in snapshots:
        by_date.setdefault(snap.date, snap)
    return sorted(by_date.values(), key=lambda s: s.date)


def _reject_bad_revisions(snaps: list[Snapshot]) -> tuple[list[Snapshot], list[tuple[date, str]]]:
    """Drop crowd-edited snapshots where names vanish and immediately come back.

    A ticker absent from one snapshot but present in the ones either side did not
    leave the index for two weeks and rejoin; the revision was mid-edit. One 2015
    revision had the table truncated to 477 names, which passes a plausible-size
    check and would otherwise publish 26 false removals of everything alphabetically
    before ALTR.

    A provider-published snapshot is never rejected this way. S&P's 2007-04-16 file
    trips exactly this test (Caremark, Phelps Dodge and Univision are absent from it
    and present in the article revisions either side), but there the file is right and
    the article was stale for weeks after the companies were acquired.
    """
    rejected: list[tuple[date, str]] = []
    keep = list(snaps)
    for previous, current, following in zip(snaps, snaps[1:], snaps[2:], strict=False):
        if current.primary:
            continue
        returning = (previous.tickers & following.tickers) - current.tickers
        if returning:
            keep.remove(current)
            names = ", ".join(sorted(returning)[:5])
            rejected.append((current.date,
                             f"{len(returning)} names vanish and return ({names}); {current.source}"))
    return keep, rejected


def _aliases(snapshots: list[Snapshot]) -> dict[str, str]:
    """Collapse spellings of one share-class ticker: BRKB and BRK-B, VIAB and VIA-B.

    Sources punctuate share classes inconsistently across revisions. Two tickers that
    are equal once separators are dropped are the same security, and the punctuated
    spelling is the canonical one.
    """
    seen = {t for snap in snapshots for t in snap.tickers}
    groups: dict[str, set[str]] = {}
    for ticker in seen:
        groups.setdefault(ticker.replace("-", ""), set()).add(ticker)

    return {t: max(variants, key=len) for variants in groups.values()
            if len(variants) > 1 for t in variants}


def _apply_aliases(snapshots: list[Snapshot], events: list[ChangeEvent],
                   alias: dict[str, str]) -> tuple[list[Snapshot], list[ChangeEvent]]:
    if not alias:
        return snapshots, events

    def fix(tickers):
        return frozenset(alias.get(t, t) for t in tickers)

    snapshots = [
        Snapshot(date=s.date, tickers=fix(s.tickers), source=s.source, primary=s.primary,
                 names={alias.get(t, t): n for t, n in s.names.items()},
                 attrs={alias.get(t, t): v for t, v in s.attrs.items()})
        for s in snapshots
    ]
    events = [
        ChangeEvent(date=e.date, added=fix(e.added), removed=fix(e.removed),
                    source=e.source, reason=e.reason, refs=e.refs)
        for e in events
    ]
    return snapshots, events


def _observe(snaps: list[Snapshot]) -> list[_Boundary]:
    """Transitions visible in the snapshots alone."""
    out = []
    for previous, current in zip(snaps, snaps[1:], strict=False):
        for ticker in sorted(current.tickers - previous.tickers):
            out.append(_Boundary(ticker, current.date, True, previous.date))
        for ticker in sorted(previous.tickers - current.tickers):
            out.append(_Boundary(ticker, current.date, False, previous.date))
    return out


def _run_of(spans: list[tuple[str, date, date | None]], ticker: str, day: date) -> date | None:
    """The start date of the membership run of `ticker` covering `day`."""
    return next((start for t, start, end in spans
                 if t == ticker and start <= day and (end is None or day < end)), None)


def _resolve_attributes(
    snaps: list[Snapshot], spans: list[tuple[str, date, date | None]],
) -> tuple[dict[tuple[str, date], dict[str, str]], dict[tuple[str, date], str],
           dict[tuple[str, date], Counter[str]], list[Discrepancy]]:
    """Attributes and company name per membership run, keyed by (ticker, run start).

    Per run rather than per ticker, because a ticker can be reused by a different
    company: CB was Chubb Corp before ACE Limited renamed itself Chubb Ltd and took
    the symbol, and one CIK cannot be right for both.
    """
    votes: dict[tuple[str, date, str], Counter[str]] = {}
    named: dict[tuple[str, date], Counter[str]] = {}
    for snap in snaps:
        for ticker, stated in snap.attrs.items():
            start = _run_of(spans, ticker, snap.date)
            if start is None:
                continue
            for fieldname, value in stated.items():
                if value:
                    votes.setdefault((ticker, start, fieldname), Counter())[value] += 1
        for ticker, name in snap.names.items():
            start = _run_of(spans, ticker, snap.date)
            if name and start is not None:
                named.setdefault((ticker, start), Counter())[name] += 1

    attributes: dict[tuple[str, date], dict[str, str]] = {}
    conflicts: list[Discrepancy] = []
    for (ticker, start, fieldname), counted in votes.items():
        # A CIK or a listing venue cannot change during a run, so sources disagreeing
        # means one is wrong and the majority settles it: a single bad revision gave
        # Hanesbrands Avery Dennison's CIK, against 36 stating the right one. A sector
        # name does change, so there the first statement is the era-accurate one.
        attributes.setdefault((ticker, start), {})[fieldname] = (
            counted.most_common(1)[0][0] if fieldname in STABLE_FIELDS else next(iter(counted)))

        # Two well-supported values for a stable identifier mean the run spans an
        # issuer change. Usually a reorganization that keeps the business (Disney's
        # 2019 holding company, Google becoming Alphabet), occasionally a symbol
        # changing hands outright: CB was Chubb Corp until ACE renamed itself Chubb
        # Ltd and took the ticker, so that one run covers two companies and its price
        # series is not continuous. Reported, never resolved away.
        if fieldname == "cik":
            supported = [v for v, n in counted.items() if n >= 3]
            if len(supported) > 1:
                conflicts.append(Discrepancy(
                    start, "issuer_changed", ticker,
                    f"run from {start} states several CIKs ({', '.join(sorted(supported))}); "
                    f"published value is the most stated"))

    # the earliest name stated by at least two snapshots, so one bad revision cannot
    # name the run (the returning Tyco row said "Tyson Foods" for a day) while era
    # drift still resolves to the era spelling, since the earlier style always reaches
    # two snapshots before anyone restyles it. A run seen once keeps its only name.
    names = {key: next((n for n, c in counted.items() if c >= 2), next(iter(counted)))
             for key, counted in named.items()}
    observed = {key: counted for (t, s, fieldname), counted in votes.items()
                if fieldname == "cik" for key in [(t, s)]}
    return attributes, names, observed, conflicts


def _proven_renames(spans: list[tuple[str, date, date | None]],
                    names: dict[tuple[str, date], str],
                    observed: dict[tuple[str, date], Counter[str]],
                    split: set[tuple[str, date]],
                    discrepancies: list[Discrepancy]) -> list[Rename]:
    """A run ending and another starting on one day, both under the same CIK.

    Ticker changes are not index events and the changes table excludes them by
    policy, so a rename shows up only as this shape. Equal CIKs are what makes the
    pairing safe: two unrelated companies swapping on one day have different ones,
    and two share classes of one issuer never open and close on the same day, so
    GOOG and GOOGL are left alone. Runs whose boundary a change record explained
    count too: S&P sometimes files a symbol change as an index event, and Alcoa
    becoming Arconic and then Howmet is one SEC filer throughout.

    A shared CIK with a *third* run of the same filer spanning the boundary is
    reported instead of paired: when Under Armor's C line UA-C ended in 2016 the
    A line became UAA, but the C line had merely taken over the UA symbol, and
    which line continued into which is not stated by anything. Asserting a pairing
    there would stitch two share classes into one series. For the same reason a
    boundary a record-proven issuer split created is never rename evidence: FOXA
    and FOX both split on the day 21st Century Fox became Fox Corporation, and a
    stale CIK vote either side of that date must not weld one issuer's A line to
    the other's B line.
    """
    def ciks(ticker: str, start: date) -> set[str]:
        """Every CIK stated for this run, not just the published one.

        A reorganization gives the continuing business a new filer id, so Walgreen Co
        and Walgreens Boots Alliance never share a published CIK, but the revisions
        right after the symbol change still carry the old one, which is the evidence
        that the row continued rather than a new company appearing.
        """
        return set(observed.get((ticker, start), ()))

    starting: dict[date, list[tuple[str, date]]] = {}
    ending: dict[date, list[tuple[str, date]]] = {}
    for ticker, start, end in spans:
        starting.setdefault(start, []).append((ticker, start))
        if end is not None:
            ending.setdefault(end, []).append((ticker, start))

    out = []
    for when in sorted(set(starting) & set(ending)):
        available = [o for o in ending[when] if (o[0], when) not in split]
        for ticker, start in starting[when]:
            if (ticker, when) in split:
                continue
            mine = ciks(ticker, start)
            if not mine:
                continue
            old = next((o for o in available if o[0] != ticker and ciks(*o) & mine), None)
            if old is None:
                continue
            spanning = [other for other, o_start, o_end in spans
                        if other not in (ticker, old[0]) and o_start <= when
                        and (o_end is None or o_end > when) and ciks(other, o_start) & mine]
            if spanning:
                discrepancies.append(Discrepancy(
                    when, "ambiguous_rename", ticker,
                    f"{old[0]} ends where {ticker} begins under one filer, but "
                    f"{spanning[0]} held the same filer across the boundary too; "
                    f"which line continued into which is not stated, so no rename is asserted"))
                continue
            available.remove(old)
            shared = sorted(ciks(*old) & mine)[0]
            out.append(Rename(date=when, old=old[0], new=ticker, cik=shared,
                              company=names.get((ticker, start), "")))
    return out


def _drop_resurrections(boundaries: list[_Boundary], snaps: list[Snapshot],
                        records: dict[tuple[str, bool], list[date]], grace_days: int,
                        discrepancies: list[Discrepancy]) -> list[_Boundary]:
    """Drop crowd re-additions of names a provider snapshot had already dropped.

    The article was stale for months after S&P's last file: Caremark, Phelps Dodge,
    Univision and Saber had all left by March 2007, the 2007-04-16 file correctly
    omitted them, and the article still carried them, so each "returned" in the
    first crowd snapshots. A provider-published snapshot outranks a later crowd one
    (the hierarchy the rejection test already uses), and no record backs the
    return, so the resurrection is dropped rather than published as a membership.

    Scoped to soon after the provider file: a later return is an index event, not a
    stale list; Tyco came back in 2010 with a record behind it, Noble in 2011
    without one, and both are genuine readmissions.
    """
    primaries = [s for s in snaps if s.primary]
    if not primaries:
        return boundaries
    known = frozenset().union(*(s.tickers for s in primaries))

    keep = []
    for boundary in boundaries:
        primary = max((s for s in primaries if s.date <= boundary.when),
                      key=lambda s: s.date, default=None)
        backed = any(primary.date <= d <= boundary.when + timedelta(days=grace_days)
                     for d in records.get((boundary.ticker, True), ())) if primary else False
        if (boundary.opening and boundary.confidence is not Confidence.EXACT and primary
                and boundary.ticker in known and boundary.ticker not in primary.tickers
                and (boundary.when - primary.date).days <= RESURRECTION_DAYS and not backed):
            discrepancies.append(Discrepancy(
                boundary.when, "resurrection", boundary.ticker,
                f"absent from the provider snapshot of {primary.date} but back in the crowd "
                f"snapshots by {boundary.when} with no record behind the return; the file wins"))
            continue
        keep.append(boundary)
    return keep


def _merge_false_gaps(boundaries: list[_Boundary], snaps: list[Snapshot],
                      primary_dates: list[date],
                      discrepancies: list[Discrepancy]) -> list[_Boundary]:
    """Close crowd-era gaps where the same row vanished and came back unexplained.

    For nine months the article around the S&P handoff was missing four members it
    had never lost (Abercrombie & Fitch, DDR, Host, Kraft), and C.R. Bard vanished
    for five weeks in 2008. Nothing on either side of those gaps has a record, and
    the company name is unchanged across them, so the article lost the row rather
    than the index losing the member. A genuine demotion and readmission is longer
    (Noble was out 22 months) or has a record behind its return (Tyco,
    Ingersoll-Rand), and a recycled ticker never matches on the company name
    (WM: Washington Mutual, then Waste Management).
    """
    snap_at = {s.date: s for s in snaps}
    crowd_dates = set(snap_at) - set(primary_dates)

    by_ticker: dict[str, list[_Boundary]] = {}
    for boundary in boundaries:
        by_ticker.setdefault(boundary.ticker, []).append(boundary)

    dropped: set[tuple[str, date, bool]] = set()
    for ticker, group in by_ticker.items():
        group.sort(key=lambda b: (b.when, b.opening))
        for gone, back in zip(group, group[1:], strict=False):
            if (gone.opening or not back.opening
                    or gone.when in primary_dates or back.when in primary_dates
                    or gone.when not in crowd_dates or back.when not in crowd_dates
                    or Confidence.EXACT in (gone.confidence, back.confidence)):
                continue
            gap = (back.when - gone.when).days
            if gap <= 0 or gap > GAP_MERGE_DAYS:
                continue
            before = snap_at[gone.window_start].names.get(ticker, "")
            after = snap_at[back.when].names.get(ticker, "")
            if not before or before != after:
                continue
            dropped.add((ticker, gone.when, False))
            dropped.add((ticker, back.when, True))
            discrepancies.append(Discrepancy(
                back.when, "false_gap", ticker,
                f"vanished for {gap} days and returned as the same company with no record "
                f"at either boundary; the article lost the row, so the gap is closed"))

    return [b for b in boundaries if (b.ticker, b.when, b.opening) not in dropped]


def _honor_record_sides(runs: dict[str, list[tuple[date, Confidence, date | None, Confidence | None]]],
                         events: list[ChangeEvent], snaps: list[Snapshot],
                         claimed: set[tuple[str, bool, date]],
                         discrepancies: list[Discrepancy]) -> set[tuple[str, date]]:
    """Let a matched record's other side act on a run the snapshots never moved.

    S&P replaces, so a record states the joiner and the leaver with one date, and
    both statements hold even where the article only ever showed one:

    - the article never dropped the leaver: Dayforce was taken private on 2026-02-04
      and its row sat in the article for seven months, so the record that seated
      Ciena closes the run at the record date.
    - a record states both sides of a symbol swap: on 2016-01-19 Chubb Corp's seat
      went to Extra Space Storage while ACE renamed itself Chubb Limited and took
      the CB symbol the same day, so the CB row never blinked and one run covered
      two companies. A record naming one ticker as both removed and added (AGL
      taking over Nicor's GAS symbol, 21st Century Fox becoming Fox Corporation)
      splits the run in two at the record date.

    An addition of a ticker the article already carried is *not* honored without
    the swap statement. The changes table is retroactively edited from the modern
    company's point of view (the 2010 Millipore row now says CB joined, though
    its own citation names ACE), and a record claiming to add an incumbent is the
    signature of exactly that rewrite, not of a split.

    Every boundary this writes is dated by a record, never inferred, and each is
    reported so it can be checked against the record's citation.
    """
    def prior(day: date) -> Snapshot | None:
        return next((s for s in reversed(snaps) if s.date < day), None)

    split: set[tuple[str, date]] = set()
    for event in sorted(events, key=lambda e: e.date):
        if not snaps[0].date <= event.date <= snaps[-1].date:
            continue
        matched = any((ticker, direction, event.date) in claimed
                      for ticker in event.added | event.removed for direction in (True, False))
        swap = event.added & event.removed
        if not matched and not swap:
            continue
        article = prior(event.date)
        if article is None:
            continue
        for ticker in sorted(event.added | event.removed):
            swap_side = ticker in swap and ticker in article.tickers
            leaving = ticker in event.removed and ticker in article.tickers \
                and (ticker, False, event.date) not in claimed
            if not (swap_side or leaving):
                continue
            entries = runs.get(ticker, [])
            for i, (start, start_how, end, end_how) in enumerate(entries):
                if not (start < event.date and (end is None or end > event.date)):
                    continue
                entries[i] = (start, start_how, event.date, Confidence.EXACT)
                discrepancies.append(Discrepancy(
                    event.date, "stale_list", ticker,
                    f"record dated {event.date} removed this name, but the list still "
                    f"carried it at {article.date}; trusting the record"))
                if leaving:
                    claimed.add((ticker, False, event.date))
                if swap_side:
                    entries.insert(i + 1, (event.date, Confidence.EXACT, end, end_how))
                    claimed.add((ticker, True, event.date))
                    split.add((ticker, event.date))
                    discrepancies.append(Discrepancy(
                        event.date, "issuer_split", ticker,
                        f"record dated {event.date} removes and adds this symbol in one "
                        f"day; the run is split into two issuers"))
                break
        for entries in runs.values():
            entries.sort(key=lambda e: e[0])
    return split


def build(snapshots: list[Snapshot], events: list[ChangeEvent] | None = None,
          records_complete_from: date | None = None, lag_days: int = LAG_DAYS,
          grace_days: int = GRACE_DAYS) -> Timeline:
    """Membership intervals from snapshots, with boundaries refined by change records.

    `records_complete_from` is the date the record source starts claiming complete
    coverage. Before it, a transition no record explains is normal and labeled
    SNAPSHOT; at or after it, the same silence means the record source has a hole
    and is labeled UNRECONCILED.
    """
    if not snapshots:
        raise ValueError("no snapshots")
    snapshots, events = _apply_aliases(snapshots, sorted(events or [], key=lambda e: e.date),
                                       _aliases(snapshots))
    snaps, rejected = _reject_bad_revisions(_dedupe(snapshots))

    discrepancies: list[Discrepancy] = []
    boundaries = _observe(snaps)
    primary_dates = [s.date for s in snaps if s.primary]

    # records by (ticker, direction), nearest-first to each transition
    records: dict[tuple[str, bool], list[date]] = {}
    for event in events:
        for ticker in event.added:
            records.setdefault((ticker, True), []).append(event.date)
        for ticker in event.removed:
            records.setdefault((ticker, False), []).append(event.date)

    claimed: set[tuple[str, bool, date]] = set()
    for boundary in boundaries:
        key = (boundary.ticker, boundary.opening)
        earliest = boundary.window_start - timedelta(days=lag_days)
        # Editors often update the list a few days before the effective date, so a
        # record dated just after the revision that first shows the change is still
        # the right date. But a record may never be honored past a provider-published
        # snapshot: if S&P's own file lists a name on a date, no record can make it
        # absent then.
        latest = boundary.when + timedelta(days=grace_days)
        blocking = next((d for d in primary_dates if boundary.when <= d < latest), None)
        if blocking is not None:
            latest = boundary.when
        candidates = [d for d in records.get(key, ())
                      if earliest <= d <= latest and (*key, d) not in claimed]
        if not candidates:
            if records_complete_from and boundary.when >= records_complete_from:
                boundary.confidence = Confidence.UNRECONCILED
                discrepancies.append(Discrepancy(
                    boundary.when, "unexplained_addition" if boundary.opening else "unexplained_removal",
                    boundary.ticker,
                    f"{'appeared' if boundary.opening else 'vanished'} between "
                    f"{boundary.window_start} and {boundary.when} with no change record"))
            continue

        best = max(candidates)  # the latest record still inside the window
        boundary.when = best
        boundary.confidence = Confidence.EXACT
        claimed.add((*key, best))
        if best < boundary.window_start:
            discrepancies.append(Discrepancy(
                best, "stale_list", boundary.ticker,
                f"record dates this {'addition' if boundary.opening else 'removal'} to {best}, "
                f"but the list still disagreed at {boundary.window_start}; trusting the record"))

    boundaries = _drop_resurrections(boundaries, snaps, records, grace_days, discrepancies)
    boundaries = _merge_false_gaps(boundaries, snaps, primary_dates, discrepancies)

    runs: dict[str, list[tuple[date, Confidence, date | None, Confidence | None]]] = {}
    open_runs: dict[str, tuple[date, Confidence]] = {
        t: (snaps[0].date, Confidence.LEFT_CENSORED) for t in snaps[0].tickers
    }
    for boundary in sorted(boundaries, key=lambda b: (b.when, b.opening)):
        if boundary.opening:
            open_runs.setdefault(boundary.ticker, (boundary.when, boundary.confidence))
        elif boundary.ticker in open_runs:
            start, start_how = open_runs.pop(boundary.ticker)
            runs.setdefault(boundary.ticker, []).append(
                (start, start_how, max(boundary.when, start), boundary.confidence))

    # past the last snapshot only the records speak, so trust them outright
    for event in [e for e in events if e.date > snaps[-1].date]:
        for ticker in sorted(event.removed):
            if ticker in open_runs:
                start, start_how = open_runs.pop(ticker)
                runs.setdefault(ticker, []).append((start, start_how, event.date, Confidence.EXACT))
        for ticker in sorted(event.added):
            open_runs.setdefault(ticker, (event.date, Confidence.EXACT))

    for ticker, (start, how) in open_runs.items():
        runs.setdefault(ticker, []).append((start, how, None, None))

    split = _honor_record_sides(runs, events, snaps, claimed, discrepancies)

    # after every claim on a record has been made, whatever is left over never
    # happened in the snapshots: an unsupported record, reported rather than applied
    for (ticker, opening), dates in records.items():
        for when in dates:
            if (ticker, opening, when) not in claimed and snaps[0].date <= when <= snaps[-1].date:
                discrepancies.append(Discrepancy(
                    when, "phantom_addition" if opening else "phantom_removal", ticker,
                    f"record dated {when} matches no transition in the snapshots"))

    spans = sorted(((t, s, e) for t, entries in runs.items() for s, _, e, _ in entries),
                   key=lambda r: (r[0], r[1]))
    attributes, names, observed_ciks, conflicts = _resolve_attributes(snaps, spans)
    discrepancies += conflicts

    intervals = [
        Interval(ticker=t, start=s, end=e, start_confidence=sc, end_confidence=ec,
                 company=names.get((t, s), ""))
        for t, entries in runs.items() for s, sc, e, ec in entries
    ]
    intervals.sort(key=lambda i: (i.ticker, i.start))

    return Timeline(intervals=intervals, discrepancies=discrepancies,
                    renames=_proven_renames(spans, names, observed_ciks, split, discrepancies),
                    names=names, attributes=attributes,
                    snapshot_dates=[s.date for s in snaps], rejected=rejected)
