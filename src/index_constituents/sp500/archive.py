"""S&P's own constituent files, recovered from the Wayback Machine.

Authoritative for 2000-12-29 to 2007-04-16, the era no other free source covers at
ticker level. Files are vendored under data/sp500/raw by scripts/vendor_sp_archive.py
and are frozen: S&P took the directory down, so there is nothing to refresh.

Two layouts, both quirky:

  500_YYYYMMDD_C.xls   tab-separated text despite the extension. Date in the
                       filename. Columns: Symbol Company Country GICS Sector Price
  sp500_gics_*.xls     real BIFF workbook. The as-of date is prose in a cell above
                       the header ("effective after the close July 16, 2003"), the
                       header row floats between rows 1 and 4, and column spellings
                       drift across revisions (Stock_Name / Company Name /
                       SECTOR_NAME).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from .._reconcile import Snapshot

RAW = Path(__file__).resolve().parents[3] / "data" / "sp500" / "raw"

# Only the equity line matters; these appear in the ticker column of some revisions.
NON_EQUITY = {"", "-", "CASH", "USD", "TOTAL"}

# Every spelling S&P actually used across the 22 workbooks: "July 16, 2003",
# "Febrary 9, 2006" (sic), "April 16th, 2007", "1/9/2007", "02/21/03".
_DATE_PATTERNS = (
    (r"(?:january|february|febrary|march|april|may|june|july|august|september|october|november|december)"
     r"\s+\d{1,2}(?:st|nd|rd|th)?,\s*\d{4}", ("%B %d, %Y",)),
    (r"\d{1,2}/\d{1,2}/\d{4}", ("%m/%d/%Y",)),
    (r"\d{1,2}/\d{1,2}/\d{2}", ("%m/%d/%y",)),
)

_FIXUPS = ((r"febrary", "february"), (r"(\d{1,2})(?:st|nd|rd|th)\b", r"\1"))


def _parse_prose_date(text: str) -> date | None:
    low = text.strip().lower()
    for pattern, formats in _DATE_PATTERNS:
        m = re.search(pattern, low)
        if not m:
            continue
        token = m.group(0)
        for wrong, right in _FIXUPS:
            token = re.sub(wrong, right, token)
        for fmt in formats:
            try:
                return datetime.strptime(token.title() if "%B" in fmt else token, fmt).date()
            except ValueError:
                continue
    return None


def _sector(value: str) -> str:
    """A sector name, or "" if the cell holds its numeric code instead.

    One workbook (2006-12-07) has a shifted header that names Sector_Name over the
    column actually holding the code, so the cell reads "20.0" rather than
    "Industrials". A GICS sector is never a number.
    """
    text = value.strip()
    try:
        float(text)
    except ValueError:
        return text
    return ""


def _clean(ticker: str) -> str:
    # S&P pads the column to a fixed width and marks share classes with a dot
    return ticker.strip().upper().replace(".", "-")


def read_tsv(path: Path) -> Snapshot:
    as_of = datetime.strptime(re.search(r"500_(\d{8})_C", path.name).group(1), "%Y%m%d").date()
    lines = path.read_text(errors="replace").splitlines()
    header = [h.strip().lower() for h in lines[0].split("\t")]
    col = {name: header.index(name) for name in ("symbol", "company", "sector") if name in header}

    names, attrs = {}, {}
    for line in lines[1:]:
        cells = line.split("\t")
        if len(cells) <= col["symbol"]:
            continue
        ticker = _clean(cells[col["symbol"]])
        if ticker in NON_EQUITY:
            continue
        names[ticker] = cells[col["company"]].strip() if "company" in col else ""
        sector = _sector(cells[col["sector"]]) if "sector" in col else ""
        attrs[ticker] = {"gics_sector": sector} if sector else {}

    return Snapshot(date=as_of, tickers=frozenset(names), source=f"sp_archive:{path.name}",
                    names=names, attrs=attrs, primary=True)


def read_workbook(path: Path) -> Snapshot:
    import xlrd

    sheet = xlrd.open_workbook(path).sheet_by_index(0)
    rows = [[str(c.value) for c in sheet.row(r)] for r in range(sheet.nrows)]

    header_row = next(r for r, cells in enumerate(rows[:8])
                      if any(c.strip().lower() in ("ticker", "symbol") for c in cells))
    header = [re.sub(r"[^a-z]", "", c.lower()) for c in rows[header_row]]

    def find(*candidates: str) -> int | None:
        return next((header.index(c) for c in candidates if c in header), None)

    i_ticker = find("ticker", "symbol")
    i_name = find("stockname", "companyname", "company")
    i_sector = find("sectorname")

    # The as-of date is prose above the header and bears no relation to the capture
    # date: one file captured in 2009 is the April 2007 list. Falling back to the
    # capture date would silently misdate a snapshot by years, so refuse instead.
    as_of = next((d for cells in rows[:header_row] for d in [_parse_prose_date(" ".join(cells))] if d), None)
    if as_of is None:
        raise ValueError(f"no as-of date in the {header_row} row(s) above the header of {path.name}")

    names, attrs = {}, {}
    for cells in rows[header_row + 1:]:
        if i_ticker >= len(cells):
            continue
        ticker = _clean(cells[i_ticker])
        if ticker in NON_EQUITY:
            continue
        names[ticker] = cells[i_name].strip() if i_name is not None and i_name < len(cells) else ""
        sector = _sector(cells[i_sector]) if i_sector is not None and i_sector < len(cells) else ""
        attrs[ticker] = {"gics_sector": sector} if sector else {}

    return Snapshot(date=as_of, tickers=frozenset(names), source=f"sp_archive:{path.name}",
                    names=names, attrs=attrs, primary=True)


GLOBS = ("500_*_C.tsv", "sp500_gics_*.xls")


def snapshots(raw: Path | None = None) -> list[Snapshot]:
    """Every vendored S&P file, oldest first.

    Where a prose date and a filename date collide on the same day the TSV wins: it
    names its own as-of date, while a workbook's date is prose written by hand.
    """
    root = raw or RAW
    out = []
    for pattern in GLOBS:
        reader = read_tsv if pattern.endswith(".tsv") else read_workbook
        out += [reader(p) for p in sorted(root.glob(pattern))]

    by_date: dict[date, Snapshot] = {}
    for snap in sorted(out, key=lambda s: (s.date, ".tsv" not in s.source)):
        by_date.setdefault(snap.date, snap)
    return sorted(by_date.values(), key=lambda s: s.date)
