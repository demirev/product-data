import argparse
import random
import sys
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter, Retry

ET = ZoneInfo("America/New_York")
PUBDATE_FORMATS = ("%d %b, %Y, %H:%M", "%b %d, %Y, %H:%M")


def parse_published_at(raw):
	"""Parse a PRN 'published date' string into a UTC-aware datetime.

	Handles the two observed shapes, both suffixed with 'ET':
	  '21 Nov, 2023, 01:00 ET'
	  'Oct 07, 2024, 17:47 ET'
	Returns None on empty input or unknown shape (caller decides what to do).
	"""
	if not raw:
		return None
	s = raw.strip()
	if not s.endswith("ET"):
		return None
	body = s[:-2].rstrip().rstrip(",").rstrip()
	for fmt in PUBDATE_FORMATS:
		try:
			dt = datetime.strptime(body, fmt)
		except ValueError:
			continue
		return dt.replace(tzinfo=ET).astimezone(timezone.utc)
	return None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_db, ensure_indexes
from vocab import vocab_flags
from company import release_facts, rollup


all_categories = [
	# business and money
	"automotive-transportation",
	"business-technology",
	"entertainment-media",
	"financial-services",
	"general-business",
	# science and tech
	"consumer-technology",
	"energy",
	"environment",
	"heavy-industry-manufacturing",
	"telecommunications",
	# lifestyle and health
	"consumer-products-retail",
	"health",
	"sports",
	"travel",
]


class ScrapingError(Exception):
	"""A page we reached but could not use. `status_code` is None for a good
	response whose markup we could not parse."""

	def __init__(self, message, status_code=None):
		super().__init__(message)
		self.status_code = status_code


# A release that 404s is gone from the wire for good, so retrying it forever
# only costs requests. Every other failure — DNS, timeouts, 5xx, unparseable
# markup — is treated as transient and retried on later passes until the cap.
GONE_STATUS = (404, 410)
MAX_ATTEMPTS = 3

# (connect, read). Without one a hung socket blocks the pass indefinitely.
TIMEOUT = (10, 30)

# One Session for the process, so a pass over the queue costs one DNS lookup and
# one TLS handshake rather than one of each per article. Doing it per-request is
# what produced the 94k-article DNS wipeout: bare `requests.get` re-resolves
# every time, and sustained lookups at that rate exhaust Docker's embedded
# resolver, which then answers EAI_AGAIN in bursts. The retries below cover the
# blips that survive that — the transport retries them itself, before the caller
# ever counts an attempt.
SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
	total=3, connect=3, read=3, status=3, backoff_factor=0.5,
	status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))))


def scrape_all_links(
	categories=all_categories,
	start_date="2023-08-09",
	end_date="2024-02-05",
	n_pages=15,
):
	"""Harvest article links from PRN category listings and upsert them as pending articles."""
	articles = get_db()["articles"]
	base_url = "https://www.prnewswire.com/news-releases"

	for category in categories:
		start = datetime.strptime(start_date, "%Y-%m-%d")
		end = datetime.strptime(end_date, "%Y-%m-%d")
		this_date = start
		while this_date <= end:
			date_str = this_date.strftime("%Y-%m-%d")
			category_url = f"{base_url}/{category}-latest-news/{category}-latest-news-list/"
			y, m, d = date_str.split("-")
			url = f"{category_url}?month={m}&day={d}&year={y}&hour=00"

			for page_no in range(1, n_pages):
				page_url = url + f"&page={page_no}&pagesize=100"
				print(f"Getting all links for {page_url}")
				try:
					hrefs = scrape_links_on_page(page_url)
				except Exception as e:
					print(f"Failed to scrape links for {date_str} page {page_no}: {e}")
					continue

				now = datetime.now(timezone.utc)
				for href in hrefs:
					article_id = href.split("/")[-1].split(".")[0]
					articles.update_one(
						{"_id": article_id},
						{
							"$setOnInsert": {
								"url": href,
								"category": category,
								"listing_date": date_str,
								"discovered_at": now,
								"status": "pending",
								"attempts": 0,
							}
						},
						upsert=True,
					)

			this_date += timedelta(days=1)

	return True


def scrape_links_on_page(url, selector=".newsreleaseconsolidatelink"):
	hrefs = []
	response = SESSION.get(url, timeout=TIMEOUT)
	if response.status_code != 200:
		raise ScrapingError("Failed to scrape the URL")
	soup = BeautifulSoup(response.content, "html.parser")
	for link in soup.select(selector):
		hrefs.append(link["href"])
	return hrefs


def scrape_all_articles(categories=None):
	"""Fetch all pending articles and fill in their content.

	With `categories` left None every pending article is scraped, including the
	category-less stubs written by `scrape_sitemap.py` and `scrape_wayback.py`.
	Returns the number scraped successfully, which is what tells the caller
	whether any derived data downstream is now stale.

	Picks up `failed` articles below `MAX_ATTEMPTS` as well as `pending` ones, so
	an outage costs a pass rather than the articles: a network blip that fails
	everything in flight heals on the next pass instead of retiring 94k releases
	permanently, which is what happened on 2026-08-19. `gone` is the terminal
	status and is never re-queued.
	"""
	articles = get_db()["articles"]
	query = {"$or": [{"status": "pending"},
	                 {"status": "failed", "attempts": {"$lt": MAX_ATTEMPTS}}]}
	if categories:
		query["category"] = {"$in": categories}

	pending = list(articles.find(query, {"_id": 1, "url": 1}))
	random.shuffle(pending)
	print(f"Scraping {len(pending)} pending articles")

	done = 0
	for doc in pending:
		article_link = doc["url"]
		print(f"Scraping {article_link}")
		try:
			scraped = scrape_article(article_link, prefix="https://www.prnewswire.com")
			articles.update_one(
				{"_id": doc["_id"]},
				{
					"$set": {
						**scraped,
						"status": "scraped",
						"scraped_at": datetime.now(timezone.utc),
					},
					"$inc": {"attempts": 1},
				},
			)
			done += 1
		except Exception as e:
			gone = getattr(e, "status_code", None) in GONE_STATUS
			print(f"{'Gone' if gone else 'Failed'}: {article_link}: {e}")
			articles.update_one(
				{"_id": doc["_id"]},
				{
					"$set": {"status": "gone" if gone else "failed", "last_error": str(e)},
					"$inc": {"attempts": 1},
				},
			)
	return done


def scrape_article(link, prefix="https://www.prnewswire.com"):
	if prefix is not None:
		link = prefix + link

	response = SESSION.get(link, timeout=TIMEOUT)
	if response.status_code != 200:
		raise ScrapingError(f"Unable to access the link. Status code: {response.status_code}",
			response.status_code)

	soup = BeautifulSoup(response.content, "html.parser")
	header = soup.select_one(".detail-headline h1")
	body = soup.select_one(".release-body")
	uploader = soup.select_one(".meta + a strong")
	uploader_link = soup.select_one(".meta + a")
	date = soup.select_one(".mb-no")

	if not body:
		raise ScrapingError("Unable to find the body of the article.")

	scraped = {"body": body.text}
	if header:
		scraped["header"] = header.text
	if uploader:
		scraped["uploader"] = uploader.text
	if uploader_link:
		scraped["uploader_link"] = uploader_link["href"]
	if date:
		raw = date.text
		scraped["published_date"] = raw
		parsed = parse_published_at(raw)
		if parsed is not None:
			scraped["published_at"] = parsed

	# Precomputed so the labeler can sample on an index instead of scanning.
	scraped.update(vocab_flags(scraped.get("header"), scraped.get("body")))
	# Company facts the release carries about its own sender; rolled up per
	# company by `py/company.py --rollup`.
	scraped.update(release_facts(
		scraped.get("header"), scraped.get("body"), scraped.get("uploader")))
	return scraped


IDLE_SLEEP_SECONDS = 24 * 60 * 60


def resolve_link_date_range(explicit_start, explicit_end):
	"""Pick the date range for link scraping.

	If start/end are given explicitly, use them. Otherwise compute end=today and
	start = latest listing_date already in Mongo (inclusive, so today's
	incrementally-growing listings get re-scraped), falling back to 180 days ago
	when the collection is empty.
	"""
	end_date = explicit_end or datetime.now(timezone.utc).strftime("%Y-%m-%d")
	if explicit_start:
		return explicit_start, end_date

	latest = get_db()["articles"].find_one(
		{"listing_date": {"$exists": True}},
		sort=[("listing_date", -1)],
		projection={"listing_date": 1},
	)
	if latest and latest.get("listing_date"):
		return latest["listing_date"], end_date
	return (datetime.now(timezone.utc) - timedelta(days=180)).strftime("%Y-%m-%d"), end_date


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="A script to scrape press releases")
	parser.add_argument("--no-links", action="store_true", help="skip scraping links")
	parser.add_argument("--no-articles", action="store_true", help="skip scraping articles")
	parser.add_argument("--start-date", type=str, help="YYYY-MM-DD (overrides DB-derived start)")
	parser.add_argument("--end-date", type=str, help="YYYY-MM-DD")
	parser.add_argument("--categories", type=str, help="comma separated list of categories")
	parser.add_argument("--loop", action="store_true", help="run forever with a sleep between passes")
	parser.add_argument("--rollup-when-idle", action="store_true",
		help="rebuild the companies collection after any article pass that scraped something")
	parser.add_argument("--idle-sleep", type=int, default=IDLE_SLEEP_SECONDS,
		help="seconds to sleep between passes when in --loop mode")
	args = parser.parse_args()

	print("Starting")
	print(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

	ensure_indexes()
	categories = args.categories.split(",") if args.categories else list(all_categories)

	while True:
		random.shuffle(categories)

		if not args.no_links:
			start_date, end_date = resolve_link_date_range(args.start_date, args.end_date)
			print(f"Scraping links {start_date} → {end_date}")
			scrape_all_links(categories=categories, start_date=start_date, end_date=end_date)

		scraped = 0
		if not args.no_articles:
			# Filtered only when categories were asked for explicitly. Sitemap and
			# wayback stubs carry no category, so an `$in` over the default list
			# would skip every stub the back-fills exist to create.
			scraped = scrape_all_articles(
				categories=args.categories.split(",") if args.categories else None)

		# An emptied queue is the cheapest moment to rebuild `companies`: the
		# releases just scraped already carry their facts (scrape_article stamps
		# them inline), so the corpus is both at rest and fully stamped. Skipped
		# when the pass scraped nothing, since the rebuild reads the whole corpus
		# to produce a result identical to the one already there.
		if args.rollup_when_idle and scraped:
			try:
				rollup()
			except Exception as e:
				# `companies` is derived and the next pass rebuilds it from
				# scratch, so a failure here is not worth losing the loop over.
				print(f"Rollup failed: {e}")

		if not args.loop:
			break

		print(f"Idle; sleeping {args.idle_sleep}s")
		time.sleep(args.idle_sleep)

	print(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
	print("Done")
