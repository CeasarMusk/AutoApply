"""Regenerate the Keryx snapshot bundled with AutoApply (src/autoapply/sources/data/).

    uv run python scripts/update_keryx_snapshot.py [path/to/keryx/checkout]

With no path, downloads data/jobs.json and data/boards.json from GitHub (two requests).
Users don't need this: `autoapply keryx sync` updates their own copy in ~/.autoapply/keryx.
"""

from __future__ import annotations

import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from autoapply.sources import keryx

OUT = Path(__file__).resolve().parent.parent / "src" / "autoapply" / "sources" / "data"


def load(name: str, checkout: Path | None) -> dict:
    if checkout:
        return json.loads((checkout / "data" / name).read_text())
    r = httpx.get(f"{keryx.RAW}/{name}", headers={"User-Agent": "AutoApply snapshot script"},
                  timeout=120, follow_redirects=True)
    r.raise_for_status()
    return r.json()


def main() -> None:
    checkout = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for name, convert in (("boards.json", keryx.compact_boards), ("jobs.json", keryx.compact_jobs)):
        data = convert(load(name, checkout))
        data["fetched_at"] = stamp
        path = OUT / f"{name}.gz"
        # mtime=0 keeps the file byte-identical when the data hasn't changed
        with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as gz:
            gz.write(json.dumps(data, separators=(",", ":")).encode())
        print(f"{path.name}: {len(data['rows']):,} rows, {path.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
