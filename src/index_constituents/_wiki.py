"""Wikipedia as an index-membership source: list revisions and the changes table.

Two independent readings of the same pages, which is the point: they cross-check.

`snapshots()` walks the revision history of the list article and parses the
constituent table out of each revision, giving the index as Wikipedia believed it to
be on that date. For the S&P 500 that history runs from 2005-09 and the ticker column
appears on 2007-05-31; earlier revisions are a bullet list of company names and yield
no tickers, so they are skipped.

`changes()` parses the dated addition/removal table, which gives exact effective
dates between revisions. It is dense from 2007 and nearly empty before (2001 and
2002 have no rows at all), so it cannot reconstruct the early era on its own. That
is what sp_archive is for.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import date, datetime

from ._reconcile import ChangeEvent, Snapshot

API = "https://en.wikipedia.org/w/api.php"
UA = "index-constituents (https://github.com/dfjmax/index-constituents)"

# {{NyseSymbol|MMM}}, {{NasdaqSymbol|AAPL}}, {{BZX link|XYZ}}
SYMBOL_TEMPLATE = re.compile(r"\{\{\s*(?:NyseSymbol|NasdaqSymbol|BZX link|NASDAQ|NYSE)\s*\|\s*([A-Za-z0-9.\-]+)",
                             re.IGNORECASE)
MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december")
PROSE_DATE = re.compile(rf"({MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})", re.IGNORECASE)
ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _get(**params) -> dict:
    params.setdefault("format", "json")
    params.setdefault("formatversion", "2")
    url = f"{API}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
        return json.loads(r.read())


def _parse_date(text: str) -> date | None:
    if m := ISO_DATE.search(text):
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if m := PROSE_DATE.search(text):
        return datetime.strptime(f"{m.group(1).title()} {m.group(2)} {m.group(3)}", "%B %d %Y").date()
    return None


def _strip_markup(cell: str) -> str:
    cell = re.sub(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", "", cell, flags=re.DOTALL)
    cell = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", cell)
    cell = re.sub(r"\{\{[^}]*\}\}", "", cell)
    cell = re.sub(r"<[^>]+>", "", cell)
    return cell.replace("'''", "").replace("''", "").strip()


def _rows(wikitext: str, table_id: str | None = None) -> list[list[str]]:
    """Cells of the first wikitable, split on the || and ! separators."""
    start = wikitext.find(f'id="{table_id}"') if table_id else -1
    start = wikitext.rfind("{|", 0, start) if start > 0 else wikitext.find("{|")
    if start < 0:
        return []
    end = wikitext.find("\n|}", start)
    body = wikitext[start:end if end > 0 else len(wikitext)]

    out = []
    for chunk in body.split("\n|-")[1:]:
        cells = []
        for line in chunk.splitlines():
            line = line.strip()
            if not line or line.startswith(("|+", "{|", "|}")):
                continue
            if line[0] in "|!":
                # a row may be one line of || separated cells or one line per cell,
                # and either spelling may lead with a redundant pipe
                for cell in re.split(r"\|\||!!", line[1:]):
                    cells.append(cell.lstrip("|!").strip())
        if cells:
            out.append(cells)
    return out


BARE_SYMBOL = re.compile(r"[A-Z]{1,5}(?:[.\-][A-Z])?")

# The eleven GICS sectors, used to locate the unlabeled classification cells
GICS_SECTORS = frozenset({
    "Communication Services", "Consumer Discretionary", "Consumer Staples", "Energy",
    "Financials", "Health Care", "Industrials", "Information Technology", "Materials",
    "Real Estate", "Utilities", "Telecommunication Services",
})


def _ticker(cell: str) -> str | None:
    """A ticker from a cell, whether templated or written as plain text."""
    if m := SYMBOL_TEMPLATE.search(cell):
        return m.group(1).upper().replace(".", "-")
    bare = _strip_markup(cell)
    return bare.upper().replace(".", "-") if BARE_SYMBOL.fullmatch(bare) else None


CIK = re.compile(r"^0\d{9}$")
VENUES = {"nysesymbol": "NYSE", "nasdaqsymbol": "NASDAQ", "bzx link": "CBOE"}


# Header spellings actually used across the revision history. Editors renamed and
# rehyphenated these columns repeatedly: "date first added" became "date added" in
# 2023, "gics sub industry" gained its hyphen in 2021, "company" became "security".
_FIELDS = {
    "security": ("security", "company"),
    "cik": ("cik",),
    "sp_date_added": ("date added", "date first added"),
    "gics_sector": ("gics sector",),
    "gics_sub_industry": ("gics sub-industry", "gics sub industry"),
}


def _stated(plain: list[str], columns: dict[str, int], field: str) -> str:
    """The value of a named column in one row, or "" if this revision lacks it."""
    i = next((columns[name] for name in _FIELDS[field] if name in columns), None)
    return plain[i] if i is not None and i < len(plain) else ""


def _header_columns(rows: list[list[str]]) -> dict[str, int]:
    """Column name -> index, from the table's own header row.

    Reading these fields by position instead puts the headquarters in the
    sub-industry column the moment a row omits its classification or a revision
    reorders the table ("North Chicago, Illinois" is not a GICS sub-industry). The
    header names every column, so it is the only thing worth trusting here.
    """
    for cells in rows[:3]:
        plain = [re.sub(r"\s+", " ", _strip_markup(c)).strip().lower() for c in cells]
        if any(p in ("symbol", "ticker", "ticker symbol") for p in plain):
            return {name: i for i, name in enumerate(plain) if name}
    return {}


def _tickers_from_table(wikitext: str) -> tuple[frozenset[str], dict[str, str], dict[str, dict[str, str]]]:
    """Tickers, company names, and whatever else the row states.

    Only the 2007-08 revisions wrote the symbol as plain text; everything later uses a
    symbol template. Allowing the plain-text fallback in a templated table misreads an
    all-caps company name as a ticker, which is how ANSYS, ONEOK and SAIC once entered
    the universe as tickers in rows whose template was malformed. So the fallback is
    used only when the table has no templates at all, and a template-less row in a
    templated table is skipped rather than guessed at.
    """
    rows = _rows(wikitext)
    templated = any(SYMBOL_TEMPLATE.search("|".join(cells)) for cells in rows)
    columns = _header_columns(rows)

    names: dict[str, str] = {}
    attrs: dict[str, dict[str, str]] = {}
    for cells in rows:
        if templated:
            m = SYMBOL_TEMPLATE.search("|".join(cells))
            if m is None:
                continue
            ticker = m.group(1).upper().replace(".", "-")
            venue = VENUES.get(re.search(r"\{\{\s*([A-Za-z ]+?)\s*\|", m.group(0)).group(1).lower(), "")
        else:
            ticker = next((t for c in cells if (t := _ticker(c))), None)
            if ticker is None:
                continue
            venue = ""

        plain = [_strip_markup(c) for c in cells]
        cik, added = _stated(plain, columns, "cik"), _stated(plain, columns, "sp_date_added")
        sector = _stated(plain, columns, "gics_sector")

        names[ticker] = _stated(plain, columns, "security") or next(
            (p for p in plain if len(p) > 3 and not _ticker(p) and not p.startswith(("http", "["))), "")
        stated = {
            "venue": venue,
            "cik": cik if CIK.fullmatch(cik) else "",
            "sp_date_added": added if ISO_DATE.fullmatch(added) else "",
            "gics_sector": sector if sector in GICS_SECTORS else "",
            "gics_sub_industry": _stated(plain, columns, "gics_sub_industry"),
        }
        attrs[ticker] = {k: v for k, v in stated.items() if v}
    return frozenset(names), names, attrs


def revisions(title: str, start: date | None = None, batch: int = 500) -> list[tuple[str, str]]:
    """(timestamp, revid) oldest first."""
    out, cont = [], None
    while True:
        params = dict(action="query", prop="revisions", titles=title, rvlimit=str(batch),
                      rvprop="timestamp|ids", rvdir="newer")
        if start:
            params["rvstart"] = f"{start.isoformat()}T00:00:00Z"
        if cont:
            params["rvcontinue"] = cont
        data = _get(**params)
        page = data["query"]["pages"][0]
        out += [(r["timestamp"], r["revid"]) for r in page.get("revisions", [])]
        cont = data.get("continue", {}).get("rvcontinue")
        if not cont:
            return out


def snapshots(title: str = "List of S&P 500 companies", start: date | None = None,
              every_days: int = 7, source: str = "wikipedia") -> list[Snapshot]:
    """One snapshot per `every_days`, from the last revision on or before each step.

    Sampling rather than parsing all 3,000+ revisions: consecutive revisions are
    mostly copy-edits, and a real index change is picked up within the step. Change
    events supply the exact dates, so the sampling rate only bounds how long an
    unannounced change can hide.
    """
    revs = revisions(title, start=start)
    if not revs:
        return []

    picked, last_day = [], None
    for ts, revid in revs:
        day = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").date()
        if last_day is None or (day - last_day).days >= every_days:
            picked.append((day, revid))
            last_day = day
    if picked[-1][1] != revs[-1][1]:
        picked.append((datetime.strptime(revs[-1][0], "%Y-%m-%dT%H:%M:%SZ").date(), revs[-1][1]))

    out = []
    for batch_start in range(0, len(picked), 50):  # the API caps revids at 50 per query
        chunk = picked[batch_start:batch_start + 50]
        day_of = {revid: day for day, revid in chunk}
        data = _get(action="query", prop="revisions", revids="|".join(str(r) for _, r in chunk),
                    rvprop="content|ids", rvslots="main")
        for page in data["query"]["pages"]:
            for rev in page.get("revisions", []):
                tickers, names, attrs = _tickers_from_table(rev["slots"]["main"]["content"])
                if len(tickers) >= 400:  # below that the revision predates the ticker column
                    out.append(Snapshot(date=day_of[rev["revid"]], tickers=tickers,
                                        source=f"{source}:{rev['revid']}", names=names,
                                        attrs=attrs))
        time.sleep(0.2)
    return sorted(out, key=lambda s: s.date)


def changes(title: str = "Historical components of the S&P 500",
            source: str = "wikipedia-changes") -> list[ChangeEvent]:
    """Dated additions and removals from the changes table.

    Rows are one change each, but S&P replaces rather than adds, so a row usually
    names both sides. Same-date rows are merged into one event.
    """
    wikitext = _get(action="parse", page=title, prop="wikitext")["parse"]["wikitext"]

    # Effective Date | Added Ticker | Added Security | Removed Ticker | Removed Security | Reason | Refs
    merged: dict[date, tuple[set[str], set[str], list[str], list[str]]] = {}
    for cells in _rows(wikitext, table_id="changes"):
        if len(cells) < 6:
            continue
        when = _parse_date(_strip_markup(cells[0]))
        if when is None:
            continue

        a, r, reasons, refs = merged.setdefault(when, (set(), set(), [], []))
        if added := _ticker(cells[1]):
            a.add(added)
        if removed := _ticker(cells[3]):
            r.add(removed)
        if reason := _strip_markup(cells[5]):
            reasons.append(reason)
        refs += re.findall(r"url\s*=\s*(\S+?)[\s|}]", cells[6] if len(cells) > 6 else "")

    return [ChangeEvent(date=when, added=frozenset(a), removed=frozenset(r), source=source,
                        reason="; ".join(dict.fromkeys(reasons))[:300], refs=tuple(dict.fromkeys(refs)))
            for when, (a, r, reasons, refs) in sorted(merged.items()) if a or r]
