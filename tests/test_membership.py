"""Offline tests: the shipped artifact, the archive parser, and the reconciler.

The membership tests assert real index history (Enron gone after 2001, Lehman out
the month it failed, Tesla in on 2020-12-21) because a membership file that passes
every structural check while claiming Lehman was a member through 2012 is worthless.
No network.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import index_constituents as ic
from index_constituents import _wiki
from index_constituents._reconcile import ChangeEvent, Confidence, Snapshot, build
from index_constituents.sp500 import archive


def snap(day, tickers, source="test", names=None):
    return Snapshot(date=day, tickers=frozenset(tickers), source=source, names=names or {})


class TestArtifactIntegrity:
    def test_no_interval_ends_before_it_starts(self):
        assert [m for m in ic.members("sp500") if m.end and m.end < m.start] == []

    def test_no_ticker_holds_two_overlapping_runs(self):
        by_ticker: dict[str, list] = {}
        for m in ic.members("sp500"):
            by_ticker.setdefault(m.ticker, []).append(m)
        overlaps = [
            (a.ticker, a.end, b.start)
            for runs in by_ticker.values()
            for a, b in zip(sorted(runs, key=lambda m: m.start),
                            sorted(runs, key=lambda m: m.start)[1:], strict=False)
            if a.end and a.end > b.start
        ]
        assert overlaps == []

    def test_every_confidence_is_a_known_label(self):
        known = {c.value for c in Confidence}
        labels = {m.start_confidence for m in ic.members("sp500")}
        labels |= {m.end_confidence for m in ic.members("sp500") if m.end_confidence}
        assert labels <= known

    def test_open_runs_have_no_end_confidence(self):
        assert all(m.end_confidence is None for m in ic.members("sp500") if m.end is None)


class TestIndexSize:
    @pytest.mark.parametrize("day", [
        date(2001, 6, 30), date(2003, 6, 30), date(2005, 6, 30), date(2008, 6, 30),
        date(2012, 6, 30), date(2016, 6, 30), date(2020, 6, 30), date(2025, 6, 30),
    ])
    def test_membership_stays_in_the_historical_band(self, day):
        # the S&P 500 has held 487-507 names since 2000; outside that the build is wrong
        assert 487 <= len(ic.as_of("sp500", day)) <= 510

    def test_coverage_reaches_from_2000_to_the_present(self):
        first, last = ic.coverage("sp500")
        assert first == date(2000, 12, 29)
        assert last >= date(2026, 1, 1)

    def test_as_of_reproduces_every_primary_snapshot_exactly(self):
        """The strongest invariant available: on a date S&P published, we must agree.

        Catches boundary shifts that a plausible-size band lets through. Pinning
        starts to stated entry dates once put 503 names in the index for most of
        2001, which the size band passed and this test would not.
        """
        mismatches = []
        for snapshot in archive.snapshots():
            got = ic.as_of("sp500", snapshot.date)
            if got != snapshot.tickers:
                mismatches.append((snapshot.date, len(got), len(snapshot.tickers),
                                   sorted(got ^ snapshot.tickers)[:6]))
        assert mismatches == []


class TestKnownHistory:
    """Each case is a public fact about the index, not a property of this pipeline."""

    def test_enron_is_gone_after_2001(self):
        assert "ENE" in ic.as_of("sp500", date(2001, 6, 30))
        assert "ENE" not in ic.as_of("sp500", date(2002, 6, 30))

    def test_lehman_leaves_in_september_2008(self):
        assert "LEH" in ic.as_of("sp500", date(2008, 6, 30))
        assert "LEH" not in ic.as_of("sp500", date(2008, 12, 31))

    def test_worldcom_is_gone_after_its_2002_bankruptcy(self):
        assert "WCOM" in ic.as_of("sp500", date(2001, 6, 30))
        assert "WCOM" not in ic.as_of("sp500", date(2003, 6, 30))

    def test_bear_stearns_leaves_in_2008(self):
        assert "BSC" in ic.as_of("sp500", date(2007, 6, 30))
        assert "BSC" not in ic.as_of("sp500", date(2009, 1, 1))

    def test_tesla_joins_on_its_announced_date(self):
        assert "TSLA" not in ic.as_of("sp500", date(2020, 12, 20))
        assert "TSLA" in ic.as_of("sp500", date(2020, 12, 22))

    def test_kodak_survives_to_2010(self):
        assert "EK" in ic.as_of("sp500", date(2009, 6, 30))
        assert "EK" not in ic.as_of("sp500", date(2011, 6, 30))

    def test_apple_is_a_member_throughout(self):
        for year in (2001, 2008, 2015, 2025):
            assert "AAPL" in ic.as_of("sp500", date(year, 6, 30))

    def test_removed_members_are_retained_so_a_universe_cannot_condition_on_survival(self):
        # the whole point: these are long gone and must still be present
        assert {"ENE", "LEH", "WCOM", "BSC", "EK"} <= ic.tickers("sp500")

    def test_alcoa_ends_at_the_2016_split(self):
        assert "AA" in ic.as_of("sp500", date(2016, 6, 30))
        assert "AA" not in ic.as_of("sp500", date(2017, 6, 30))

    @pytest.mark.parametrize("ticker", ["A", "MMM", "AFL", "ADBE", "AEP", "ALL"])
    def test_long_standing_members_hold_one_unbroken_run(self, ticker):
        # a mid-edit revision in 2015 dropped everything alphabetically before ALTR,
        # which published a three-week false gap for 26 names until it was rejected
        runs = [m for m in ic.members("sp500") if m.ticker == ticker]
        assert len(runs) == 1, [(str(r.start), str(r.end)) for r in runs]

    def test_single_revision_typos_are_not_in_the_universe(self):
        # AGL appears in exactly one Wikipedia revision, which is mid-edit; S&P's own
        # files carry GAS across the whole era. SPG-PJ is a preferred class.
        assert "GAS" in ic.tickers("sp500")
        assert "AGL" not in ic.tickers("sp500")
        assert "SPG-PJ" not in ic.tickers("sp500")

    def test_names_the_provider_dropped_do_not_resurrect_after_its_last_file(self):
        # Caremark, Phelps Dodge, Univision and Sabre all left in March 2007; S&P's
        # 2007-04-16 file omits them and the stale article still carried them, so the
        # first crowd snapshots showed each "returning". The file wins.
        for ticker in ("CMX", "PD", "UVN", "TSG"):
            starts = [m.start for m in ic.members("sp500") if m.ticker == ticker]
            assert date(2007, 5, 15) not in starts, ticker
        assert ic.as_of("sp500", date(2007, 6, 30)) >= {"ANF", "KFT"}   # never left
        assert not {"PD", "UVN", "TSG"} & ic.as_of("sp500", date(2007, 6, 30))

    @pytest.mark.parametrize("ticker", ["ANF", "DDR", "HST", "KFT", "BCR"])
    def test_members_the_article_briefly_lost_hold_one_unbroken_run(self, ticker):
        # around the 2007 handoff the article was missing four live members for nine
        # months; C.R. Bard vanished for five weeks in 2008. No record explains any
        # of it, and the company name is unchanged across each gap.
        runs = [m for m in ic.members("sp500") if m.ticker == ticker]
        assert len(runs) == 1, [(str(r.start), str(r.end)) for r in runs]

    def test_a_record_dates_the_removal_the_article_never_showed(self):
        # Dayforce was taken private 2026-02-04 but its row sat in the article for
        # seven months; the record that seated Ciena closes the run at the record date
        day = next(m for m in ic.members("sp500") if m.ticker == "DAY")
        assert day.end == date(2026, 2, 9)
        assert day.end_confidence == "exact"
        assert "DAY" not in ic.as_of("sp500", date(2026, 3, 1))

    def test_a_record_outranks_a_year_long_stale_list(self):
        # Cooper redomesticated to Ireland on 2009-09-28 and Airgas took its seat,
        # but the article carried Cooper until July 2010, and only added Priceline
        # (seated 2009-11-03) then too. Both records win over both stale rows.
        runs = {m.ticker: m for m in ic.members("sp500")}
        assert min(m.end for m in ic.members("sp500") if m.ticker == "CBE") == date(2009, 9, 28)
        assert runs["ARG"].start == date(2009, 9, 28)
        assert runs["PCLN"].start == date(2009, 11, 3)
        assert "CBE" not in ic.as_of("sp500", date(2010, 1, 1))
        assert {"ARG", "PCLN"} <= ic.as_of("sp500", date(2010, 1, 1))

    def test_a_symbol_that_changed_hands_is_two_issuers_not_one_run(self):
        # a record stating one ticker as both removed and added is a proven swap:
        # Nicor's GAS became AGL's GAS; 21st Century Fox's FOXA/FOX became Fox Corp's
        gas = [m for m in ic.members("sp500") if m.ticker == "GAS"]
        assert [(m.company, str(m.start), str(m.end)) for m in gas] == [
            ("NICOR Inc.", "2000-12-29", "2011-12-12"),
            ("AGL Resources Inc.", "2011-12-12", "2016-07-01")]
        foxa = [m for m in ic.members("sp500") if m.ticker == "FOXA"]
        assert len(foxa) == 2 and all(m.end == date(2019, 3, 19) for m in foxa[:1])
        assert foxa[0].cik == "0001308161" and foxa[1].cik == "0001754301"

    def test_the_cb_run_spans_two_companies_and_says_so(self):
        # Chubb Corp's seat went to Extra Space Storage on 2016-01-19 while ACE
        # renamed itself Chubb Limited and took the CB symbol the same day, so the
        # ticker never blinked and the run covers both. The changes-table rows that
        # would split it were rewritten by an editor from the modern company's point
        # of view, so the split cannot be sourced; the trap is reported instead.
        runs = [m for m in ic.members("sp500") if m.ticker == "CB"]
        assert len(runs) == 1 and runs[0].end is None
        assert any(d.ticker == "CB" and d.kind == "issuer_changed"
                   for d in ic.discrepancies("sp500"))


class TestIdentifiers:
    def test_cik_is_a_ten_digit_sec_key_where_present(self):
        ciks = {m.cik for m in ic.members("sp500") if m.cik}
        assert ciks
        assert all(len(c) == 10 and c.isdigit() for c in ciks)

    def test_one_cik_per_ticker_and_it_survives_renames(self):
        # FB and META are one company under two symbols; the CIK says so, which is
        # the whole reason to carry it
        by_ticker = {m.ticker: m.cik for m in ic.members("sp500") if m.cik}
        assert by_ticker.get("FB") == by_ticker.get("META") == "0001326801"

    def test_venue_is_a_known_listing_market(self):
        assert {m.venue for m in ic.members("sp500") if m.venue} <= {"NYSE", "NASDAQ", "CBOE"}

    def test_sub_industry_is_a_classification_not_a_headquarters(self):
        # read positionally, this column used to yield "North Chicago, Illinois"
        states = ("Illinois", "California", "New York", "Texas", "Ohio", "Minnesota")
        leaked = [m.gics_sub_industry for m in ic.members("sp500")
                  if m.gics_sub_industry.endswith(states)]
        assert leaked == []

    def test_sector_is_a_name_and_never_its_numeric_code(self):
        # one 2006 workbook has a shifted header and yielded "20.0" for Industrials
        sectors = {m.gics_sector for m in ic.members("sp500") if m.gics_sector}
        assert sectors
        # both spellings of the sector GICS renamed to Communication Services in 2018
        # are expected: the era value is kept rather than restated as the modern one
        assert sectors <= _wiki.GICS_SECTORS | {"Telecommunications Services"}

    def test_stated_entry_dates_are_not_used_to_extend_runs_before_coverage(self):
        # AEP's stated S&P entry date is 1957, but the data starts in 2000 and
        # honoring it would describe the 20th-century index by its survivors alone
        aep = next(m for m in ic.members("sp500") if m.ticker == "AEP")
        assert aep.sp_date_added == "1957-03-04"
        assert aep.start == date(2000, 12, 29)
        assert aep.start_confidence == "left_censored"

    def test_one_bad_revision_cannot_name_a_run(self):
        # the revision that restored the Tyco row in 2010 briefly said "Tyson Foods";
        # a name needs two snapshots behind it before it is the run's name
        tyco = next(m for m in ic.members("sp500")
                    if m.ticker == "TYC" and m.start == date(2010, 8, 26))
        assert tyco.company == "Tyco International"

    def test_identifier_coverage_is_reported_and_partial(self):
        # 69% for CIK is the ceiling: the column only exists from 2014, so members
        # that left before then have none, and no free source fills that in
        coverage = ic.summary("sp500")["identifier_coverage"]
        assert 700 <= coverage["cik"] <= ic.summary("sp500")["tickers"]
        assert coverage["gics_sector"] > coverage["cik"]


class TestRenames:
    def test_known_symbol_changes_are_present(self):
        pairs = {(r.old, r.new) for r in ic.renames("sp500")}
        assert {("WLP", "ANTM"), ("WAG", "WBA"), ("PCLN", "BKNG"),
                ("COH", "TPR"), ("CBG", "CBRE")} <= pairs

    def test_the_shared_cik_belongs_to_at_least_one_side(self):
        # a reorganization gives the continuing business a new filer id, so WAG and
        # WBA never share a *published* CIK; the pairing rests on a CIK the sources
        # stated for both runs, which is the old one carried on the continued row
        published = {m.ticker: m.cik for m in ic.members("sp500")}
        for r in ic.renames("sp500"):
            assert r.cik
            assert r.cik in (published[r.old], published[r.new]), r

    def test_reorganization_renames_are_caught_not_only_pure_symbol_changes(self):
        pairs = {(r.old, r.new) for r in ic.renames("sp500")}
        assert ("WAG", "WBA") in pairs      # Walgreen Co -> Walgreens Boots Alliance
        assert ("AA", "ARNC") in pairs      # Alcoa Inc -> Arconic
        assert ("ARNC", "HWM") in pairs     # Arconic -> Howmet Aerospace

    def test_dual_share_classes_are_never_paired_as_a_rename(self):
        # one issuer, one CIK, but two distinct securities that coexist
        pairs = {(r.old, r.new) for r in ic.renames("sp500")}
        assert ("GOOG", "GOOGL") not in pairs
        assert ("GOOGL", "GOOG") not in pairs

    def test_a_rename_is_two_adjacent_runs_not_an_overlap(self):
        # a ticker can hold several runs (CMCSK was a member in 2002 and again in
        # 2015), so the run that matters is the one meeting the rename date
        members = ic.members("sp500")
        for r in ic.renames("sp500"):
            assert any(m.ticker == r.old and m.end == r.date for m in members), r
            assert any(m.ticker == r.new and m.start == r.date for m in members), r

    def test_renames_never_pair_unrelated_companies(self):
        # name similarity used to match Longs Drug Stores to Family Dollar Stores on
        # the shared word "stores"; a shared CIK cannot do that
        pairs = {(r.old, r.new) for r in ic.renames("sp500")}
        assert ("LDG", "FDO") not in pairs
        assert ("AHP", "ASD") not in pairs

    def test_a_rename_ambiguous_across_share_classes_is_reported_not_asserted(self):
        # UA-C ends where UAA begins under one filer, but UA carried the same filer
        # across the boundary: the C line took the UA symbol, the A line became UAA,
        # and nothing in the sources says so. Asserting UA-C -> UAA would stitch two
        # share classes into one series.
        pairs = {(r.old, r.new) for r in ic.renames("sp500")}
        assert ("UA-C", "UAA") not in pairs
        assert any(d.kind == "ambiguous_rename" and d.ticker == "UAA"
                   for d in ic.discrepancies("sp500"))

    def test_two_issuers_splitting_on_one_day_are_never_paired_as_a_rename(self):
        # FOXA and FOX both swapped issuers on 2019-03-19 (21st Century Fox became
        # Fox Corporation); a stale CIK vote must not weld one's A line to the
        # other's B line
        pairs = {(r.old, r.new) for r in ic.renames("sp500")}
        for a, b in (("FOXA", "FOX"), ("FOX", "FOXA")):
            assert (a, b) not in pairs


class TestFetchApi:
    def test_spans_cover_membership_and_nothing_more(self):
        for s in ic.spans("sp500"):
            assert s.start <= s.first_member
            if s.last_member is not None:
                assert s.end == s.last_member
            else:
                assert s.end is None

    def test_a_window_excludes_members_that_had_already_left(self):
        window = ic.spans("sp500", start=date(2005, 1, 1), end=date(2010, 1, 1))
        got = {s.ticker for s in window}
        assert "ENE" not in got          # Enron left in 2001
        assert {"BSC", "LEH", "AAPL"} <= got

    def test_a_window_clips_the_fetch_end_but_not_an_early_removal(self):
        window = {s.ticker: s for s in ic.spans("sp500", start=date(2005, 1, 1),
                                                end=date(2010, 1, 1))}
        assert window["LEH"].end == date(2008, 9, 16)   # its real removal, not the window
        assert window["AAPL"].end == date(2010, 1, 1)   # still a member, so clipped

    def test_padding_moves_the_fetch_start_but_not_membership(self):
        plain = {s.ticker: s for s in ic.spans("sp500", start=date(2005, 1, 1))}
        padded = {s.ticker: s for s in ic.spans("sp500", start=date(2005, 1, 1), pad_days=250)}
        assert padded["AAPL"].start == plain["AAPL"].start - timedelta(days=250)
        assert padded["AAPL"].first_member == plain["AAPL"].first_member

    def test_one_span_per_symbol_even_when_it_was_a_member_twice(self):
        by_ticker = [s.ticker for s in ic.spans("sp500")]
        assert len(by_ticker) == len(set(by_ticker))
        twice = [s for s in ic.spans("sp500") if s.runs > 1]
        assert twice, "expected at least one symbol with two membership runs"

    def test_securities_follow_rename_chains(self):
        chains = {s.tickers for s in ic.securities("sp500") if len(s.tickers) > 1}
        assert ("AA", "ARNC", "HWM") in chains
        assert ("WLP", "ANTM", "ELV") in chains

    def test_a_security_reports_the_symbol_it_traded_under_on_a_day(self):
        howmet = next(s for s in ic.securities("sp500") if "HWM" in s.tickers)
        assert howmet.ticker_on(date(2010, 6, 30)) == "AA"
        assert howmet.ticker_on(date(2018, 6, 30)) == "ARNC"
        assert howmet.ticker_on(date(2025, 6, 30)) == "HWM"
        assert howmet.ticker == "HWM"

    def test_dual_classes_stay_separate_securities(self):
        # one issuer, two tradable lines: they must not collapse into one series
        groups = [s for s in ic.securities("sp500") if {"GOOG", "GOOGL"} & set(s.tickers)]
        assert len(groups) == 2

    def test_every_member_appears_in_exactly_one_security(self):
        seen = [t for s in ic.securities("sp500") for t in s.tickers]
        assert len(seen) == len(set(seen))
        assert set(seen) == set(ic.tickers("sp500"))

    def test_windowed_tickers_match_the_spans_for_that_window(self):
        start, end = date(2005, 1, 1), date(2010, 1, 1)
        assert ic.tickers("sp500", start, end) == {s.ticker for s in ic.spans("sp500", start, end)}

    def test_every_day_of_a_span_is_covered_by_its_security(self):
        howmet = next(s for s in ic.securities("sp500") if "HWM" in s.tickers)
        assert howmet.held_on(date(2015, 6, 30))
        assert howmet.ticker_on(date(1999, 1, 1)) is None


class TestArchiveParser:
    def test_every_vendored_snapshot_holds_exactly_500_names(self):
        snapshots = archive.snapshots()
        assert snapshots, "no vendored archive files found"
        assert {len(s.tickers) for s in snapshots} == {500}

    def test_snapshots_are_sorted_and_span_the_archive_era(self):
        snapshots = archive.snapshots()
        assert [s.date for s in snapshots] == sorted(s.date for s in snapshots)
        assert snapshots[0].date == date(2000, 12, 29)
        assert snapshots[-1].date == date(2007, 4, 16)

    def test_the_2009_capture_is_dated_by_its_contents_not_its_capture(self):
        # "S&P 500 April 16th, 2007" was captured in September 2009; trusting the
        # capture date would misdate that snapshot by two years
        assert date(2007, 4, 16) in {s.date for s in archive.snapshots()}
        assert date(2009, 9, 3) not in {s.date for s in archive.snapshots()}


class TestReconciler:
    def test_first_snapshot_members_are_left_censored(self):
        timeline = build([snap(date(2001, 1, 1), "AB"), snap(date(2001, 2, 1), "AB")])
        assert {i.start_confidence for i in timeline.intervals} == {Confidence.LEFT_CENSORED}

    def test_a_matching_record_pins_the_boundary_to_its_effective_date(self):
        timeline = build(
            [snap(date(2011, 1, 1), "AB"), snap(date(2011, 3, 1), "AC")],
            [ChangeEvent(date=date(2011, 2, 10), added=frozenset("C"), removed=frozenset("B"),
                         source="test")],
            records_complete_from=date(2011, 1, 1),
        )
        runs = {i.ticker: i for i in timeline.intervals}
        assert runs["C"].start == date(2011, 2, 10)
        assert runs["C"].start_confidence is Confidence.EXACT
        assert runs["B"].end == date(2011, 2, 10)

    def test_an_unexplained_change_falls_back_to_the_later_snapshot(self):
        timeline = build(
            [snap(date(2011, 1, 1), "AB"), snap(date(2011, 3, 1), "AC")],
            [], records_complete_from=date(2011, 1, 1),
        )
        runs = {i.ticker: i for i in timeline.intervals}
        assert runs["C"].start == date(2011, 3, 1)
        assert runs["C"].start_confidence is Confidence.UNRECONCILED
        assert {d.kind for d in timeline.discrepancies} == {"unexplained_addition",
                                                            "unexplained_removal"}

    def test_silence_before_records_begin_is_snapshot_not_unreconciled(self):
        timeline = build(
            [snap(date(2002, 1, 1), "AB"), snap(date(2002, 3, 1), "AC")],
            [], records_complete_from=date(2011, 1, 1),
        )
        runs = {i.ticker: i for i in timeline.intervals}
        assert runs["C"].start_confidence is Confidence.SNAPSHOT
        assert timeline.discrepancies == []

    def test_a_record_the_snapshots_never_confirm_is_reported_as_phantom(self):
        timeline = build(
            [snap(date(2011, 1, 1), "AB"), snap(date(2011, 3, 1), "AB")],
            [ChangeEvent(date=date(2011, 2, 1), added=frozenset("Z"), removed=frozenset(),
                         source="test")],
            records_complete_from=date(2011, 1, 1),
        )
        assert [d.kind for d in timeline.discrepancies] == ["phantom_addition"]

    def test_a_record_before_the_window_is_a_stale_list_not_a_miss(self):
        # the editor lag case: the record is right, the article updated late
        timeline = build(
            [snap(date(2011, 1, 1), "AB"), snap(date(2011, 4, 1), "ABC")],
            [ChangeEvent(date=date(2010, 12, 1), added=frozenset("C"), removed=frozenset(),
                         source="test")],
            records_complete_from=date(2011, 1, 1),
        )
        runs = {i.ticker: i for i in timeline.intervals}
        assert runs["C"].start == date(2010, 12, 1)
        assert [d.kind for d in timeline.discrepancies] == ["stale_list"]

    def test_share_class_spellings_collapse_to_one_ticker(self):
        timeline = build([snap(date(2011, 1, 1), ["BRKB", "A"]),
                          snap(date(2011, 2, 1), ["BRK-B", "A"])])
        assert "BRKB" not in timeline.tickers()
        assert "BRK-B" in timeline.tickers()
        assert len(timeline.intervals) == 2

    def test_as_of_is_half_open_on_the_end_date(self):
        timeline = build(
            [snap(date(2011, 1, 1), "AB"), snap(date(2011, 3, 1), "A")],
            [ChangeEvent(date=date(2011, 2, 10), added=frozenset(), removed=frozenset("B"),
                         source="test")],
        )
        assert "B" in timeline.as_of(date(2011, 2, 9))
        assert "B" not in timeline.as_of(date(2011, 2, 10))

    def test_empty_input_is_an_error_not_an_empty_timeline(self):
        with pytest.raises(ValueError):
            build([])

    def test_a_record_stating_both_sides_of_a_swap_splits_the_run(self):
        timeline = build(
            [snap(date(2011, 1, 1), ["GAS", "X"]), snap(date(2011, 6, 1), ["GAS", "X"])],
            [ChangeEvent(date=date(2011, 3, 1), added=frozenset({"GAS"}),
                         removed=frozenset({"GAS"}), source="test")],
        )
        runs = [(i.start, i.end) for i in timeline.intervals if i.ticker == "GAS"]
        assert runs == [(date(2011, 1, 1), date(2011, 3, 1)), (date(2011, 3, 1), None)]

    def test_a_record_claiming_to_add_an_incumbent_is_not_honored(self):
        # the retroactively rewritten Millipore row says CB joined in 2010 while its
        # own citation names ACE; an addition of a name the article already carries
        # is that rewrite, not a split
        timeline = build(
            [snap(date(2010, 1, 1), ["CB", "MIL"]), snap(date(2010, 12, 1), ["CB"])],
            [ChangeEvent(date=date(2010, 7, 14), added=frozenset({"CB"}),
                         removed=frozenset({"MIL"}), source="test")],
        )
        runs = [(i.start, i.end) for i in timeline.intervals if i.ticker == "CB"]
        assert runs == [(date(2010, 1, 1), None)]

    def test_a_record_closes_the_leaver_the_article_never_dropped(self):
        timeline = build(
            [snap(date(2011, 1, 1), ["DAY", "X"]), snap(date(2011, 6, 1), ["DAY", "CIEN"])],
            [ChangeEvent(date=date(2011, 2, 9), added=frozenset({"CIEN"}),
                         removed=frozenset({"DAY"}), source="test")],
            records_complete_from=date(2011, 1, 1),
        )
        runs = {i.ticker: i for i in timeline.intervals}
        assert runs["DAY"].end == date(2011, 2, 9)
        assert runs["DAY"].end_confidence is Confidence.EXACT

    def test_a_crowd_resurrection_of_a_provider_dropped_name_is_ignored(self):
        timeline = build([
            Snapshot(date=date(2007, 1, 1), tickers=frozenset("PQ"), source="sp_archive",
                     primary=True, names={"P": "Provider Co"}),
            Snapshot(date=date(2007, 4, 16), tickers=frozenset("Q"), source="sp_archive",
                     primary=True),
            snap(date(2007, 5, 15), "PQ"),
            snap(date(2007, 6, 15), "PQ"),
        ])
        assert {d.kind for d in timeline.discrepancies} == {"resurrection"}
        assert [(i.start, i.end) for i in timeline.intervals if i.ticker == "P"] == [
            (date(2007, 1, 1), date(2007, 4, 16))]
        assert "P" not in timeline.as_of(date(2007, 6, 1))

    def test_a_short_unexplained_gap_in_the_same_company_is_closed(self):
        names = {"G": "One Company"}
        timeline = build([
            snap(date(2008, 1, 1), "G", names=names), snap(date(2008, 8, 1), "X"),
            snap(date(2008, 9, 1), "X"), snap(date(2008, 10, 1), "G", names=names),
        ])
        assert {d.kind for d in timeline.discrepancies} == {"false_gap"}
        assert [(i.start, i.end) for i in timeline.intervals if i.ticker == "G"] == [
            (date(2008, 1, 1), None)]

    def test_a_gap_onto_a_recycled_ticker_is_not_closed(self):
        # Washington Mutual left and Waste Management took the symbol: same ticker,
        # different company, so the two runs stay two
        timeline = build([
            snap(date(2008, 1, 1), "W", names={"W": "Washington Mutual"}),
            snap(date(2008, 10, 1), "X"),
            snap(date(2008, 11, 1), "X"),
            snap(date(2008, 12, 1), "W", names={"W": "Waste Management"}),
        ])
        assert [i.company for i in timeline.intervals if i.ticker == "W"] == \
            ["Washington Mutual", "Waste Management"]


class TestBadRevisionRejection:
    def test_a_revision_where_names_vanish_and_return_is_dropped(self):
        timeline = build([snap(date(2011, 1, 1), "ABC"),
                          snap(date(2011, 2, 1), "AC"),      # B briefly missing
                          snap(date(2011, 3, 1), "ABC")])
        assert [d for d, _ in timeline.rejected] == [date(2011, 2, 1)]
        assert len([i for i in timeline.intervals if i.ticker == "B"]) == 1

    def test_a_provider_snapshot_is_never_rejected(self):
        # S&P's 2007-04-16 file trips the same test (Caremark and friends are absent
        # from it and present in the stale article revisions either side), and there
        # the file is right
        timeline = build([snap(date(2007, 3, 1), "ABC"),
                          Snapshot(date=date(2007, 4, 16), tickers=frozenset("AC"),
                                   source="sp_archive", primary=True),
                          snap(date(2007, 5, 1), "ABC")])
        assert timeline.rejected == []
        assert len([i for i in timeline.intervals if i.ticker == "B"]) == 2

    def test_a_genuine_removal_is_not_mistaken_for_a_bad_revision(self):
        timeline = build([snap(date(2011, 1, 1), "ABC"), snap(date(2011, 2, 1), "AC"),
                          snap(date(2011, 3, 1), "AC")])
        assert timeline.rejected == []
        assert [i.end for i in timeline.intervals if i.ticker == "B"] == [date(2011, 2, 1)]
