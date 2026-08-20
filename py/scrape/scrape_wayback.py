"""Back-fill article link stubs from the Wayback Machine's CDX index.

Companion to `scrape_sitemap.py`, for the months PRN's own archive sitemaps come
up short on: ~70 of the ~188 monthly files are served truncated or empty, and a
few decode cleanly while holding only a fraction of the month.

**CDX windows are scoped by capture date, not publication date.** A month's
window returns every release the crawler happened to touch that month, most of
which was published earlier — for Sep 2025 it yields ~27k releases of which only
~41% are Sep 2025 publications. So the counts printed here are not month totals,
and recall for any single target month lands around 60% rather than the ~100%
the raw numbers suggest. Every slug is still a real release worth having; they
just land across the corpus rather than in the month asked for. Run it alongside
`scrape_sitemap.py` rather than instead of it — for Sep 2025 the sitemap holds
647 releases Wayback never returned.

Because captures lag publication, `--pad-days` extends each window past the end
of the month, so releases published on the 30th and first crawled on the 3rd
still turn up.

Reach for it second: it is much the slower of the two, several minutes per
month. Broad queries are also refused — the CDX endpoint 504s on
`url=prnewswire.com/news-releases*` — so queries go out one window at a time
against the narrower `/news-releases/` prefix. A failure is the only signal that
a window matched too much, so a failed window is halved and retried until it
succeeds or bottoms out at a single day.

Only the URLs come from Wayback. The releases are still live on prnewswire.com,
so the stubs written here are filled in by the ordinary `scrape_prn.py
--no-links` pass, reading the original pages rather than snapshots.

    python py/scrape/scrape_wayback.py --months 2024-06,2024-07
    python py/scrape/scrape_wayback.py --from 2025-06 --to 2025-09
    python py/scrape/scrape_wayback.py --months 2025-06 --dry-run
"""
import argparse
import calendar
import os
import sys
import time
from datetime import date, timedelta
from urllib.parse import urlsplit

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import ensure_indexes, upsert_link_stubs


CDX_URL = "https://web.archive.org/cdx/search/cdx"

# The bare `/news-releases/` prefix. The wildcard form (`/news-releases*`) matches
# far more and times out before returning anything.
URL_PREFIX = "prnewswire.com/news-releases/"


def cdx_slugs(start, end, timeout=600, sleep=1.0, depth=0):
	"""Release slugs captured between `start` and `end` inclusive.

	Halves the window and retries on failure. The endpoint gives no usable signal
	that a range is too broad other than timing out or 5xx-ing, and the recursion
	bottoms out at a single day, which it will always serve.
	"""
	indent = "  " * (depth + 1)
	params = {
		"url": URL_PREFIX,
		"matchType": "prefix",
		"from": start.strftime("%Y%m%d"),
		"to": end.strftime("%Y%m%d"),
		"filter": "statuscode:200",
		"collapse": "urlkey",
		"fl": "original",
		"limit": 500000,
	}

	try:
		response = requests.get(CDX_URL, params=params, timeout=timeout)
		response.raise_for_status()
		text = response.text
	except Exception as e:
		reason = type(e).__name__
		if start >= end:
			print(f"{indent}{start} gave up: {reason}")
			return set()
		midpoint = start + (end - start) // 2
		print(f"{indent}{start}→{end} failed ({reason}); splitting")
		time.sleep(sleep)
		left = cdx_slugs(start, midpoint, timeout, sleep, depth + 1)
		time.sleep(sleep)
		return left | cdx_slugs(midpoint + timedelta(days=1), end, timeout, sleep, depth + 1)

	slugs = set()
	for line in text.splitlines():
		line = line.strip()
		if not line:
			continue
		# Captures keep their query strings, so the same release shows up under
		# ?utm_source=..., ?fbclid=... and bare. Drop the query before dedup.
		path = urlsplit(line).path
		if not path.startswith("/news-releases/") or not path.endswith(".html"):
			continue
		slug = path[len("/news-releases/"):-len(".html")]
		if slug and "/" not in slug:
			slugs.add(slug)

	print(f"{indent}{start}→{end}: {len(slugs):,} slugs")
	return slugs


def months_in_range(start, end, explicit):
	"""Expand an explicit YYYY-MM list or an inclusive range into month keys."""
	if explicit:
		return sorted({m.strip() for m in explicit.split(",") if m.strip()})

	first, last = date(*map(int, start.split("-")), 1), date(*map(int, end.split("-")), 1)
	months = []
	while first <= last:
		months.append(first.strftime("%Y-%m"))
		first = date(first.year + first.month // 12, first.month % 12 + 1, 1)
	return months


if __name__ == "__main__":
	parser = argparse.ArgumentParser(
		description="Back-fill article stubs from the Wayback Machine CDX index.")
	parser.add_argument("--from", dest="start", type=str, help="first month, YYYY-MM")
	parser.add_argument("--to", dest="end", type=str, help="last month, YYYY-MM")
	parser.add_argument("--months", type=str, help="comma separated YYYY-MM list")
	parser.add_argument("--dry-run", action="store_true", help="count without writing")
	parser.add_argument("--pad-days", type=int, default=14,
		help="days to extend each window past month end, since captures lag publication")
	parser.add_argument("--timeout", type=int, default=600,
		help="seconds before a CDX window is considered failed and split")
	parser.add_argument("--sleep", type=float, default=2.0,
		help="seconds between CDX requests")
	args = parser.parse_args()

	if not args.months and not (args.start and args.end):
		parser.error("pass --months, or both --from and --to")

	months = months_in_range(args.start, args.end, args.months)
	print(f"Querying {len(months)} month(s) from the Wayback CDX index; "
		"expect several minutes each.\n")

	if not args.dry_run:
		ensure_indexes()

	total_new = total_known = 0
	for month in months:
		year, mon = (int(p) for p in month.split("-"))
		start = date(year, mon, 1)
		end = date(year, mon, calendar.monthrange(year, mon)[1]) + timedelta(days=args.pad_days)

		print(f"{month} (captures {start} → {end}):")
		slugs = sorted(cdx_slugs(start, end, args.timeout, args.sleep))
		if not slugs:
			print(f"  nothing captured in {month}\n")
			continue

		if args.dry_run:
			print(f"  {len(slugs):,} releases captured in window (dry run, nothing written)\n")
			continue

		new, known = upsert_link_stubs(slugs, source="wayback")
		total_new += new
		total_known += known
		print(f"  {len(slugs):,} releases captured in window → {new:,} new, "
			f"{known:,} already known\n")
		time.sleep(args.sleep)

	if not args.dry_run:
		print(f"{total_new:,} stubs inserted, {total_known:,} already known.")
		print("Run `scrape_prn.py --no-links` (or the article-scraper service) to fill them in.")
