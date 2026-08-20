"""Back-fill article link stubs from PRNewswire's monthly archive sitemaps.

PRN's dated category listings only reach back 365 days. Older than that, a URL
like `?month=05&day=01&year=2024` does not 404 — it 301s to the undated listing
and quietly serves *today's* releases. So `scrape_prn.py --start-date` cannot
discover anything beyond a year back, and pointing it there would stamp today's
articles with an ancient `listing_date`.

`robots.txt` points at https://www.prnewswire.com/sitemap-gz.xml, an index of
~188 gzipped monthly files (`Sitemap_Index_<Mon>_<YYYY>.xml.gz`) covering Dec
2010 onward — ~2.35M release URLs. Discovery is the only thing that expires:
the release pages themselves stay live indefinitely, and `scrape_article`
parses a 2011 release as happily as a 2026 one. So this script writes nothing
but pending stubs, and the ordinary `scrape_prn.py --no-links` pass fills them
in from the live pages.

Roughly 70 of the monthly files are served short by PRN — truncated mid-stream
or empty, deterministically (Content-Length is stable across retries). A
truncated gzip still decodes up to the cut, so whatever came out is kept and the
month is flagged. `--list` shows every month against the ~16-20k releases a
healthy one carries; run `scrape_wayback.py` for the ones that come up short.

    python py/scrape/scrape_sitemap.py --list
    python py/scrape/scrape_sitemap.py --from 2025-03 --to 2025-10
    python py/scrape/scrape_sitemap.py --months 2024-05,2024-06 --dry-run
"""
import argparse
import os
import re
import sys
import time
import zlib

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import ensure_indexes, upsert_link_stubs


INDEX_URL = "https://www.prnewswire.com/sitemap-gz.xml"

MONTH_NUMBERS = {m: i for i, m in enumerate(
	"Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}

# A healthy month carries 16-20k releases. Well under that means a truncated or
# stale file on PRN's side rather than a quiet month, so it is worth flagging
# even when the gzip stream itself decoded cleanly (Mar 2025 is a valid file
# holding 4,994 of ~18,000 releases).
HEALTHY_RELEASES = 12000

INDEX_LOC_RE = re.compile(r"<loc>\s*([^<\s]+?)\s*</loc>")
RELEASE_LOC_RE = re.compile(
	r"<loc>\s*https://www\.prnewswire\.com(/news-releases/[^<\s?]+?\.html)\s*</loc>")
# The index carries at least one typo'd filename (`Sitemap_Index_Dec_4202`),
# which is an empty file; anything that does not parse as a month is skipped.
SITEMAP_NAME_RE = re.compile(r"Sitemap_Index_([A-Z][a-z]{2})_(\d{4})\.xml\.gz$")


def fetch_monthly_sitemaps(timeout=60):
	"""Return [(month, url), ...] from the archive index, sorted by month."""
	response = requests.get(INDEX_URL, timeout=timeout)
	response.raise_for_status()

	months = []
	for url in INDEX_LOC_RE.findall(response.text):
		match = SITEMAP_NAME_RE.search(url)
		if not match:
			continue
		name, year = match.group(1), int(match.group(2))
		if name not in MONTH_NUMBERS or not 2000 <= year <= 2100:
			print(f"Skipping unparseable sitemap name: {url}")
			continue
		months.append((f"{year}-{MONTH_NUMBERS[name]:02d}", url))

	return sorted(months)


def read_sitemap(url, timeout=300):
	"""Fetch one monthly sitemap; return (slugs, truncated).

	Decompression is fed in chunks so that a file cut off mid-stream still yields
	every release decoded before the cut, rather than raising and losing the lot.
	`truncated` reports whether the gzip stream ended cleanly.
	"""
	raw = requests.get(url, timeout=timeout).content

	truncated = False
	if raw[:2] == b"\x1f\x8b":
		decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
		chunks = []
		for i in range(0, len(raw), 65536):
			try:
				chunks.append(decompressor.decompress(raw[i:i + 65536]))
			except zlib.error:
				break
		else:
			try:
				chunks.append(decompressor.flush())
			except zlib.error:
				pass
		truncated = not decompressor.eof
		raw = b"".join(chunks)

	seen = set()
	slugs = []
	for path in RELEASE_LOC_RE.findall(raw.decode("utf-8", "replace")):
		slug = path[len("/news-releases/"):-len(".html")]
		if slug and "/" not in slug and slug not in seen:
			seen.add(slug)
			slugs.append(slug)

	return slugs, truncated


def select_months(available, start, end, explicit):
	"""Filter (month, url) pairs by an explicit list or an inclusive range."""
	if explicit:
		wanted = {m.strip() for m in explicit.split(",") if m.strip()}
		chosen = [(m, u) for m, u in available if m in wanted]
		for missing in sorted(wanted - {m for m, _ in chosen}):
			print(f"No archive sitemap for {missing}")
		return chosen

	return [
		(m, u) for m, u in available
		if (start is None or m >= start) and (end is None or m <= end)
	]


if __name__ == "__main__":
	parser = argparse.ArgumentParser(
		description="Back-fill article stubs from PRN's monthly archive sitemaps.")
	parser.add_argument("--list", action="store_true",
		help="report each month's release count and exit without writing")
	parser.add_argument("--from", dest="start", type=str, help="first month, YYYY-MM")
	parser.add_argument("--to", dest="end", type=str, help="last month, YYYY-MM")
	parser.add_argument("--months", type=str, help="comma separated YYYY-MM list")
	parser.add_argument("--all", action="store_true", help="every month in the index")
	parser.add_argument("--dry-run", action="store_true", help="count without writing")
	parser.add_argument("--sleep", type=float, default=1.0,
		help="seconds between sitemap fetches")
	args = parser.parse_args()

	if not (args.list or args.all or args.start or args.end or args.months):
		parser.error("pass --list, --all, --months, or --from/--to")

	available = fetch_monthly_sitemaps()
	print(f"Archive index: {len(available)} monthly sitemaps, "
		f"{available[0][0]} → {available[-1][0]}")

	months = available if args.all else select_months(
		available, args.start, args.end, args.months)
	if not months:
		print("Nothing selected")
		sys.exit(0)

	if not (args.list or args.dry_run):
		ensure_indexes()

	print(f"Reading {len(months)} month(s)\n")
	print(f"{'month':9} {'releases':>9} {'new':>9} {'known':>9}  state")

	total_new = total_known = 0
	degraded = []
	for month, url in months:
		try:
			slugs, truncated = read_sitemap(url)
		except Exception as e:
			print(f"{month:9} {'-':>9} {'-':>9} {'-':>9}  FAILED: {e}")
			degraded.append(month)
			continue

		short = truncated or len(slugs) < HEALTHY_RELEASES
		if short:
			degraded.append(month)
		state = "truncated" if truncated else ("thin" if short else "ok")

		if args.list or args.dry_run:
			print(f"{month:9} {len(slugs):9,} {'-':>9} {'-':>9}  {state}")
		else:
			new, known = upsert_link_stubs(slugs, source="sitemap")
			total_new += new
			total_known += known
			print(f"{month:9} {len(slugs):9,} {new:9,} {known:9,}  {state}")

		if args.sleep:
			time.sleep(args.sleep)

	if not (args.list or args.dry_run):
		print(f"\n{total_new:,} stubs inserted, {total_known:,} already known.")
		print("Run `scrape_prn.py --no-links` (or the article-scraper service) to fill them in.")

	if degraded:
		print(f"\n{len(degraded)} month(s) truncated or thin on PRN's side:")
		print("  " + ", ".join(degraded))
		print("  Recover these with: python py/scrape/scrape_wayback.py --months "
			+ ",".join(degraded))
