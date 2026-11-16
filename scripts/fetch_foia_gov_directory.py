"""Fetch the official FOIA.gov agency components list (raw JSON:API pages).

Runs on a GitHub Actions runner (the development sandbox cannot reach
foia.gov). Standard library only. The API key comes from FOIA_GOV_API_KEY
(an api.data.gov key; the public DEMO_KEY is used when unset).

    python scripts/fetch_foia_gov_directory.py --out raw-folder

Each page is stored as page-NNNN.json = {"fetched_url", "fetched_at",
"status", "body"}. No key or header is ever written to disk.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

START = "https://api.foia.gov/api/agency_components?include=agency&page%5Blimit%5D=50"
ALLOWED_HOST = "api.foia.gov"
MAX_PAGES = 200
MAX_BYTES = 8 * 1024 * 1024
MAX_WAIT = 900  # longer Retry-After means the key's quota is spent: fail fast


def fetch(url, key, attempts=6):
    if urlsplit(url).scheme != "https" or urlsplit(url).hostname != ALLOWED_HOST:
        raise SystemExit(f"refusing to follow a link off {ALLOWED_HOST}: {url}")
    delay = 30
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers={"X-API-Key": key, "Accept": "application/vnd.api+json",
                                                       "User-Agent": "foia-log-requests-directory-sync"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise SystemExit("page exceeds size bound")
                return response.status, json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                wait = int(exc.headers.get("Retry-After") or delay)
                if wait > MAX_WAIT:
                    raise SystemExit(f"HTTP {exc.code}: API quota exhausted (Retry-After {wait}s). The shared DEMO_KEY "
                                     "is rate limited; add a free api.data.gov key as the FOIA_GOV_API_KEY secret.")
                print(f"HTTP {exc.code}; retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                delay *= 2
                continue
            raise SystemExit(f"HTTP {exc.code} from {url}")
        except urllib.error.URLError as exc:
            if attempt + 1 < attempts:
                time.sleep(delay)
                delay *= 2
                continue
            raise SystemExit(f"network error: {exc.reason}")
    raise SystemExit("unreachable")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--start", default=START)
    parser.add_argument("--pause", type=float, default=2.0, help="Seconds between pages")
    args = parser.parse_args(argv)
    key = os.environ.get("FOIA_GOV_API_KEY") or "DEMO_KEY"
    print("using " + ("the FOIA_GOV_API_KEY secret" if key != "DEMO_KEY" else "the shared DEMO_KEY"), file=sys.stderr)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    url, seen, number = args.start, set(), 0
    while url:
        if url in seen or number >= MAX_PAGES:
            raise SystemExit("pagination loop or page limit reached")
        seen.add(url)
        number += 1
        status, body = fetch(url, key)
        record = {"fetched_url": url, "fetched_at": datetime.now(timezone.utc).replace(microsecond=0)
                  .isoformat().replace("+00:00", "Z"), "status": status, "body": body}
        (out / f"page-{number:04d}.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        links = body.get("links") or {}
        nxt = links.get("next")
        url = nxt.get("href") if isinstance(nxt, dict) else nxt
        print(f"page {number}: {len(body.get('data') or [])} components", file=sys.stderr)
        if number == 1 and body.get("data"):
            first = body["data"][0]
            print("component attribute keys: " + ", ".join(sorted(first.get("attributes") or {})), file=sys.stderr)
            print("relationship keys: " + ", ".join(sorted(first.get("relationships") or {})), file=sys.stderr)
        if url:
            time.sleep(args.pause)
    print(json.dumps({"pages": number, "out": str(out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
