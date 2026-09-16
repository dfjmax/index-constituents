"""Fetch S&P's own constituent files from the Wayback Machine into data/sp500/raw/.

S&P published these at www2.standardandpoors.com/spf/xls/index/ and stopped around
2007; the archive is the only remaining copy, so this is frozen history. Run once,
commit the result, never re-fetch. Two families:

  500_YYYYMMDD_C.xls  tab-separated despite the extension, as-of date in the filename
  sp500.xls           real BIFF workbook, as-of date only in a prose cell

Wayback rate-limits aggressively and returns empty bodies rather than 429, so every
response is size-checked and retried.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

RAW = Path(__file__).resolve().parents[3] / "data" / "sp500" / "raw"
DIRECTORY = "www2.standardandpoors.com/spf/xls/index/"
UA = "index-constituents vendoring script (https://github.com/dfjmax/index-constituents)"

CDX = (
    "http://web.archive.org/cdx/search/cdx?url={prefix}&matchType=prefix"
    "&output=json&fl=timestamp,original,statuscode,digest{extra}"
)
SNAPSHOT = "http://web.archive.org/web/{ts}id_/{url}"


def get(url: str, attempts: int = 5) -> bytes:
    for i in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=90) as r:
                body = r.read()
            if body:
                return body
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"    retry {i + 1}: {e}", file=sys.stderr)
        time.sleep(5 * (i + 1))
    raise RuntimeError(f"no body after {attempts} attempts: {url}")


def discover(prefix: str = "500", workbooks: bool = True) -> list[tuple[str, str, str]]:
    """(timestamp, url, kind) for every archived file we can use.

    Two queries: the dated TSVs are one capture per unique URL, so they collapse on
    urlkey. The workbooks are all served from the same sp500.xls URL and would
    collapse to a single row, losing four years of revisions, so they are deduped on
    content digest instead.
    """
    found: dict[str, tuple[str, str, str]] = {}

    rows = json.loads(get(CDX.format(prefix=DIRECTORY, extra="&collapse=urlkey")))[1:]
    for ts, url, status, _digest in rows:
        if status == "200" and re.search(rf"/{prefix}_\d{{8}}_C\.xls$", url):
            found.setdefault(re.search(rf"{prefix}_(\d{{8}})_C", url).group(1), (ts, url, "tsv"))

    if workbooks:
        rows = json.loads(get(CDX.format(prefix=DIRECTORY + "sp500.xls", extra="")))[1:]
        for ts, url, status, digest in rows:
            if status == "200" and re.search(r"/sp500\.xls", url):
                found.setdefault(digest, (ts, url, "workbook"))

    return sorted(found.values(), key=lambda r: r[0])


def main() -> None:
    raw = RAW
    raw.mkdir(parents=True, exist_ok=True)
    targets = discover()
    print(f"discovered {len(targets)} archived files")

    manifest = []
    for ts, url, kind in targets:
        stem = re.search(r"/([^/]+?)(?:\?.*)?$", url).group(1).removesuffix(".xls")
        name = f"{stem}.tsv" if kind == "tsv" else f"sp500_gics_{ts}.xls"
        path = raw / name
        if path.exists():
            body = path.read_bytes()
            print(f"  have {name}")
        else:
            body = get(SNAPSHOT.format(ts=ts, url=url))
            path.write_bytes(body)
            print(f"  got  {name}  {len(body)} bytes")
            time.sleep(3)
        manifest.append({
            "file": name,
            "kind": kind,
            "source_url": url,
            "wayback_timestamp": ts,
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        })

    (raw.parent / "manifest.json").write_text(json.dumps(
        {"directory": DIRECTORY, "retrieved_via": "web.archive.org", "files": manifest}, indent=2) + "\n")
    print(f"\nwrote data/sp500/manifest.json ({len(manifest)} files)")


if __name__ == "__main__":
    main()
