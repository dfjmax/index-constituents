# index-constituents

Point-in-time index membership: who was in the index on a given day, including the members that later left and the date they left.

```python
from datetime import date
import index_constituents as ic

ic.as_of("sp500", date(2004, 6, 30))  # frozenset of the 500 era tickers
ic.members("sp500")  # 1,180 dated membership runs
ic.tickers("sp500", start, end)  # tickers that were members in a window
ic.renames("sp500")  # 51 symbol changes, evidenced by CIK
ic.discrepancies("sp500")  # every place the sources disagreed
```

What this avoids is the bias you introduce by reconstructing a historical universe from *today's*
membership list. Ask it for 2004, and you get the 500 names that were actually in the index in 2004, spelled the way they were spelled then: `ENE`, `WCOM`, `BSC`, `EK`.

## Pulling prices for the universe

`spans` gives one row per symbol with the window worth downloading:

```python
for span in ic.spans("sp500", start=date(2005, 1, 1), end=date(2020, 1, 1),
                     pad_days=250):
    bars = my_provider.fetch(span.ticker, span.start, span.end)
```

`span.start` includes the warm-up padding; `span.first_member` and `last_member` are the unpadded membership bounds, and `end` is `None` for a symbol still in the index. A symbol that was a member
twice gets one span covering both runs, because you download a series once. For a 2005-2010 backtest that is 668 symbols.

The era ticker is the symbol to request for most of the table, but not all, and asking for the rest by their 2000s spelling returns the wrong company's prices (`BSC` is an exchange-traded note now;
`FPL` is a fund). `renames.csv` carries what the sources can prove about where a line moved; the full era-ticker → provider-symbol table is maintained with the downloader, not in this library.

When a continuous series across a symbol change matters, group by company instead:

```python
for sec in ic.securities("sp500"):
    sec.tickers  # ('AA', 'ARNC', 'HWM'): one company, three symbols
    sec.ticker_on(day)  # the symbol it traded under that day
    sec.cik, sec.venue, sec.gics_sector
```

1,124 symbols collapse to 1,074 securities; 46 used more than one symbol. Dual share classes stay separate: `GOOG` and `GOOGL` are two tradable lines, not one renamed.
`ic.to_frame("sp500")` returns the raw table as a pandas DataFrame if you would rather work in pandas (pandas is not a dependency).

Two membership rows need care even with the mapping in hand: the fused `CB` run (below) covers two companies, so its price series is not continuous through January 2016; and a provider's series for a
twice-used symbol like `T` or `WM` is spliced from more than one company, so use the run dates, not the series, to attribute returns.

## Why this is stitched from two sources

There is a single authoritative source, S&P Dow Jones Indices, and they sell it. Index membership is their product, so free access to the history does not exist. What does exist is byproducts:
Wikipedia is crowd-maintained, SEC filings are an artifact of fund disclosure, and the Wayback Machine happens to hold S&P's own files from before they stopped publishing them. Each covers a different
slice, so stitching is the only free path.

The same is true of identity. The historical ticker-to-entity map is commercial too (CUSIP is licensed; FIGI is free but has no coverage of dead 2000s securities; it returns `No identifier found` for
Lehman's CUSIP and maps "Eastman Kodak" to `KODK`, the post-bankruptcy entity). This library sidesteps that entirely by using only sources that carry the era ticker themselves, so there is no name
matching anywhere in the pipeline.

| Era                     | Source                                                                  | What it gives                                                    |
|-------------------------|-------------------------------------------------------------------------|------------------------------------------------------------------|
| 2000-12-29 → 2007-04-16 | S&P's own constituent files, via the Wayback Machine                    | 41 dated snapshots: ticker, company, GICS sector, close          |
| 2007-04-16 → today      | Revision history of the Wikipedia list article, sampled every two weeks | 360 snapshots with era tickers                                   |
| 2011 → today            | The Wikipedia changes table                                             | 306 dated add/remove records, mostly cited to S&P press releases |

S&P published files named `500_YYYYMMDD_C.xls` which are tab-separated text despite the extension, plus a `sp500.xls` GICS workbook that was overwritten in place; the archive kept 22 distinct
revisions of it. Together they are the authoritative record for the era where every other free option is guesswork, and they are the reason this repo exists. They are vendored under `data/sp500/raw/`
with a `manifest.json` pinning each file's source URL, Wayback timestamp and SHA-256. That half is frozen history:
S&P took the directory down, so there is nothing to refresh.

## Confidence

No date in the output is interpolated. Every boundary says how well it is pinned:

| Confidence      | Count | Meaning                                                                                                                                                                                                                                                    |
|-----------------|-------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `exact`         | 361   | an effective date from a cited change record                                                                                                                                                                                                               |
| `left_censored` | 500   | already a member at the first snapshot, so the true start predates the data                                                                                                                                                                                |
| `snapshot`      | 255   | only known to fall between two snapshots; the date given is the later one, so a stated start is never earlier than the truth and neither is a stated end; both err toward keeping a name in the universe a little too long, never toward dropping it early |
| `unreconciled`  | 64    | a snapshot change no record explains, in an era where records should be complete                                                                                                                                                                           |

If you only want well-dated boundaries, filter on `start_confidence == "exact"`.

`discrepancies.csv` records every place the sources disagreed, unresolved:

- **`stale_list`** (162): the change record and the article disagree, and the record wins. The dominant case is editor lag: the changes table is updated from the press release on the effective date
  while the list article follows weeks (for Cooper Industries, ten months) later. It also covers a record that removed a name the article never dropped at all (Dayforce sat in the list for seven
  months after Thoma Bravo took it private).
- **`unexplained_addition` / `unexplained_removal`** (64 / 61): the article shows a change with no record behind it. Most are ticker renames, which S&P does not treat as index events and which the
  changes table excludes *by editorial policy*. They are reported rather than guessed at: pairing them up by company name is exactly the kind of heuristic this library refuses to ship.
- **`issuer_changed`** (27): one run states two well-supported CIKs. Usually a reorganization that keeps the business (Disney's 2019 holding company, Google becoming Alphabet), occasionally a symbol
  changing hands outright: `CB` was Chubb Corp until ACE renamed itself Chubb Ltd and took the ticker the same day its old seat went to Extra Space Storage, so the ticker never blinked and one run
  covers two companies. Its price series is *not* continuous through January 2016; the download mapping splits it at that date.
- **`issuer_split`** (3): a record stating one ticker as both removed and added in a day, which splits the run in two: Nicor's `GAS` became AGL's, and 21st Century Fox's `FOXA`/`FOX` became Fox
  Corporation's. An addition-only claim on an incumbent ticker is *not* honored; the Millipore row was rewritten by an editor to say `CB`
  joined in 2010, though its own citation names ACE.
- **`resurrection`** (4): the article re-listed Caremark, Phelps Dodge, Univision and Sabre after S&P's own final file had already dropped them; the file wins.
- **`false_gap`** (5): the article briefly lost rows it should never have dropped (Abercrombie, DDR, Host and Kraft for nine months around the 2007 handoff; C.R. Bard for five weeks). Same company
  either side, no record at either boundary; the gap is closed.
- **`ambiguous_rename`** (1): `UA-C` ends where `UAA` begins under one filer, but
  `UA` held the same filer across the boundary, so which share class continued into which is not stated. No rename is asserted.
- **`phantom_addition` / `phantom_removal`** (9 / 5): a record no snapshot confirms.

Four Wikipedia revisions are rejected outright and listed in `summary.json`. A ticker absent from one revision but present either side did not leave the index for two weeks and rejoin; the revision
was mid-edit. One 2015 revision had the table truncated to 477 names (plausible enough to pass a size check) and would otherwise have published a three-week false removal for the 26 names
alphabetically before
`ALTR`. Provider-published snapshots are exempt: S&P's 2007-04-16 file trips the same test, and there the file is right and the article was stale.

## Identifiers

A ticker is not a stable key: it gets renamed (`FB`→`META`), recycled (`BSC` is an ETN now; `WM` was Washington Mutual before Waste Management took it) and suffixed (`VRTS`→`VRTSE` when Nasdaq flagged
a late filer). So each row carries whatever the sources state alongside it, never inferred and never matched:

| Column              | Coverage         | Notes                                                                                                          |
|---------------------|------------------|----------------------------------------------------------------------------------------------------------------|
| `cik`               | 809 / 1180 runs  | SEC Central Index Key. Permanent, and survives renames: `FB` and `META` are both `0001326801`.                 |
| `venue`             | 950 / 1180 runs  | NYSE, NASDAQ or CBOE, taken from the markup (`{{NyseSymbol}}` vs `{{NasdaqSymbol}}`).                          |
| `gics_sector`       | 1163 / 1180 runs | Era value, so pre-2018 rows read `Telecommunication Services` rather than the modern `Communication Services`. |
| `gics_sub_industry` | 837 / 1180 runs  |                                                                                                                |
| `sp_date_added`     | 744 / 1180 runs  | S&P's own stated entry date, back to `1957-03-04`. Informational only; see below.                              |

**CIK stops at 69% of runs for a structural reason.** Wikipedia only added the column in 2013-14, so members that left before then have none: `ABK` Ambac, `ABS` Albertson's,
`AHP` American Home Products. The older revisions put the *ticker*, not a number, in their `CIK=` SEC links, so there is nothing to recover. Filling those in means matching company names against SEC's
registrant list, which is the class of guesswork this library refuses.

**CUSIP and ISIN are deliberately absent.** The only free source is SPY's N-PORT filings, which begin in 2019-11 and carry name plus CUSIP but no ticker, so joining them needs name matching. For the
era where an identifier would actually help, CUSIP is behind the same license wall as the membership data itself.

**`sp_date_added` is data, not a boundary.** It is tempting to use it to pin starts, and it does not work: a change record names the joiner and the leaver with one date, whereas a bare entry date has
no paired removal, so honoring it moves a start earlier while the member it replaced stays until the next snapshot. Doing that put 503 names in the index for most of 2001. For the 500 `left_censored`
members the stated date also predates the data entirely, and reaching back to 1957 would describe the 20th-century index by its survivors alone, precisely the bias this exists to avoid.

## Known limitations

- **2001 and 2002 have only year-end snapshots**, so intra-year changes in those two years are unrecorded. Boundaries there are `snapshot`-confidence with a window up to a year wide. 2003 onward has
  several snapshots per year.
- **Nothing before 2000-12-29.** That is the earliest S&P file the archive holds.
- **Ticker renames stay two runs, but are listed.** `FB` ends and `META` begins on 2022-06-22 as separate runs, because a symbol change is not an index event.
  `renames.csv` pairs 51 of them (`WLP`→`ANTM`, `WAG`→`WBA`, `PCLN`→`BKNG`), each proven by the two symbols sharing one CIK, never by name similarity. A pair where either side predates the CIK column
  is left unpaired and reported instead; the pre-2014 symbol changes are few and belong in the price-download mapping, where they can be hand-checked against the provider, rather than guessed at here.
- **The changes table is itself crowd-edited and erodes.** In 2026 an editor rewrote the 2010 Millipore and 2016 Chubb rows from the modern company's point of view, which is why the fused `CB` run
  cannot be split from the sources: the record that would prove the split no longer says what it said. Records are honored over the article, but never over arithmetic: an addition of a name the
  snapshots already carry is reported, not applied.
- **The 2007 handoff is a real disagreement, not a seam.** The two snapshot sources overlap for three weeks and conflict: Caremark, Phelps Dodge and Univision all left the index in March 2007, S&P's
  2007-04-16 file correctly omits them, and the article still carried them in May. The archive therefore owns every date up to its last file.

## Rebuilding

```bash
uv sync
uv run python -m index_constituents.sp500.build          # writes data/sp500/*.csv
uv run python -m index_constituents.sp500.vendor         # re-fetch the frozen archive
uv run pytest
```

The build needs the network; reading `membership.csv` does not and has no dependencies beyond the standard library. `--every-days` sets the article sampling interval. Two weeks is the default on
measurement: exact dates come from the change records, not from sampling, while 7-day sampling doubles the unexplained transitions by surfacing revisions caught mid-edit. Records may be matched across
up to a year of article lag (Cooper's removal sat in the article for ten months after the Airgas record), but never past an intervening transition of the same ticker, and never past a
provider-published snapshot.

## Layout

```
data/sp500/
  raw/              42 vendored archive files, immutable
  manifest.json     url + wayback timestamp + sha256 per file
  membership.csv    the artifact
  renames.csv       symbol changes, evidenced by a shared CIK
  discrepancies.csv every source disagreement, unresolved
  summary.json      build statistics
src/index_constituents/
  __init__.py       the reader: the only public API
  _wiki.py          article revisions + changes table (index-agnostic)
  _reconcile.py     snapshots + records -> intervals (index-agnostic set arithmetic)
  sp500/
    archive.py      the S&P file formats
    build.py        era wiring, thresholds, CSV output
    vendor.py       Wayback discovery and download
```

## Adding an index

Add a folder next to `sp500/` with its own `build.py` naming the article titles, the plausible size band and the era dates. `_wiki.py` and `_reconcile.py` are index-agnostic and need no changes,
verified against the S&P 400 and 600 articles, which parse as-is (400 and 602 tickers, 400 dated midcap change records).

Nasdaq-100, the Dow, FTSE 100, DAX and Russell 1000 need parser work first: their articles put the constituent table behind different markup with no symbol templates, and the current parser returns
nothing for them.

Note that S&P only left public constituent files for the 500 (41 snapshots back to 2000); the 400 and 600 have three and two 2006 files respectively, so their history is effectively article-only and
starts in 2007.

## License

MIT for the code. The vendored S&P files are S&P Dow Jones Indices' material, retrieved from the public web via the Internet Archive and included here for reference; the Wikipedia-derived data is CC
BY-SA.
