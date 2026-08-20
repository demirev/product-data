"""Company-level facts, mined from the releases themselves.

Every release carries more about its sender than the `uploader` string the
scraper keeps: a dateline that names the city it was issued from, a stock ticker
if the sender is listed, an "About X" boilerplate paragraph, and a website. None
of it needs an external data source — it is already in the bodies we have.

`uploader_link` ("/news/divi-resorts/") is the company key. PR Newswire assigns
it, so it is stable across spelling drift in `uploader` and needs no entity
resolution of our own.

Two layers, mirroring how `py/vocab.py` precomputes sampling flags:

  1. Per-release facts, stamped onto each article by `release_facts`. They belong
     to the release, not the company — a company's dateline moves over the years,
     and that is signal rather than noise. `scrape_article` stamps new articles;
     the CLI backfills existing ones.

  2. A derived `companies` collection, rebuilt from those facts by `--rollup`.
     Nothing in it is authored, so it can be dropped and rebuilt after any change
     to the extractors.

    python py/company.py              # backfill per-release facts
    python py/company.py --rollup     # rebuild the companies collection

Steady state needs neither: `scrape_article` stamps facts inline, and the article
scraper calls `rollup` itself once its queue empties (`--rollup-when-idle`). Both
entry points are for catching up after an extractor change bumps a version.

Both are resumable. The backfill only touches articles whose `facts_version` is
behind, so re-running after an extractor change re-does exactly the stale ones.
"""
import argparse
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone

from pymongo import ReplaceOne, UpdateOne

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import get_db, ensure_indexes


FACTS_VERSION = "tier0-1"
ROLLUP_VERSION = "tier0-1"


# --- dateline ---------------------------------------------------------------
# Datelines are language-dependent ("Nov. 3, 2025", "15 de septiembre de 2023",
# "29 kwietnia 2026 r."), so rather than enumerate month names in every language
# we anchor on the wire marker, drop a date-shaped tail, and take the trailing
# ALL-CAPS run as the place. The caps run is also what separates the dateline
# from the subheadline that runs straight into it, PRN's markup carrying no
# boundary between them ("...Seventh Consecutive YearNEW YORK, Feb. 3, 2025").
WIRE = re.compile(r"/PRNewswire", re.I)
DATE_TAIL = re.compile(
	r"[,\s]*(?:\d{1,2}\s+(?:de\s+)?[\wÀ-ÿ\.]+\s+(?:de\s+)?\d{4}"
	r"|[\wÀ-ÿ]+\.?\s+\d{1,2},?\s*\d{4}"
	r"|\d{1,2}\.\s*[\wÀ-ÿ]+\s+\d{4})\s*$", re.UNICODE)
PLACE = re.compile(
	r"([A-ZÀ-Þ][A-ZÀ-Þ0-9\.\-'’]*(?:[ \-][A-ZÀ-Þ][A-ZÀ-Þ0-9\.\-'’]*)*"
	r"(?:\s+and\s+[A-ZÀ-Þ][A-ZÀ-Þ0-9\.\-'’ ]*)*)"
	r"(,\s*[A-ZÀ-Þ][A-Za-zÀ-ÿ\.\- ]{1,24})?$", re.UNICODE)

# --- tickers ----------------------------------------------------------------
EXCHANGES = (r"NASDAQ|NYSE\s?American|NYSE\s?MKT|NYSE|OTCQB|OTCQX|OTCMKTS|OTC\s?Markets|OTCBB|OTC|"
             r"TSXV|TSX-V|TSX|CSE|NEO|AMEX|LSE|AIM|Euronext|XETRA|ETR|FSE|FWB|SIX|BME|BIT|"
             r"ASX|NZX|SGX|HKEX|SEHK|TYO|TSE|KRX|KOSDAQ|KOSPI|BSE|NSE|JSE|B3|BMV|WSE|OSE|OMX|"
             r"Nasdaq\s?(?:First\s?North|Stockholm|Copenhagen|Helsinki)")
TICKER = re.compile(r"\(\s*(" + EXCHANGES + r")\s*:\s*([A-Z0-9][A-Z0-9\.\-]{0,7})\s*\)", re.I)

# Safe-harbour language exists because of the Private Securities Litigation
# Reform Act, so it is near-exclusive to registrants — a second, independent
# read on whether the sender is listed.
FWD_LOOKING = re.compile(
	r"forward[-\s]looking statements?|safe harbor (?:statement|provision)|"
	r"Private Securities Litigation Reform Act", re.I)

# Behavioural vocabulary, rolled up to a share per company, is what identifies
# the senders that dominate the corpus by volume but never launch anything:
# securities firms trawling for plaintiffs, and market-research publishers.
# Names do not do this job — "Greenberg Traurig, LLP" says nothing about law,
# and "Persistent Systems" is not a research house.
LITIGATION_VOCAB = re.compile(
	r"class action|lead plaintiff|securities fraud|investors who purchased|"
	r"shareholder rights|recover(?:ed)? (?:your )?losses|securities litigation|"
	r"investigation on behalf of (?:investors|shareholders)", re.I)
REPORT_VOCAB = re.compile(
	r"forecast period|\bCAGR\b|market size|vendor analysis|research report|"
	r"market research report|market is (?:expected|projected|estimated) to (?:grow|reach)|"
	r"segmented by", re.I)

# --- about / domains --------------------------------------------------------
# "About X" headings arrive delimited by a newline, a colon or an nbsp, and
# sometimes with no delimiter at all ("About SKSK Group, South Korea's ..."),
# which is why the uploader name is tried as a boundary of last resort.
ABOUT = re.compile(r"About\s+([A-Z][^\n:\xa0]{1,60}?)\s*(?:[:\n\xa0]|\s{2,})")
ABOUT_LOOSE = re.compile(r"About\s+(?=[A-Z])")
SECTION_BREAK = re.compile(
	r"About\s+[A-Z]|Media\s+Contact|Press\s+Contact|CONTACT|Contact:|"
	r"Investor\s+(?:Relations|Contact)|Forward[- ]Looking|SOURCE\s+[A-Z]|"
	r"Cautionary|Safe\s+Harbor|Disclaimer|View original content")

URL = re.compile(r"(?:https?://|www\.)([a-z0-9](?:[a-z0-9\-]*[a-z0-9])?(?:\.[a-z0-9\-]+)+)", re.I)
SOURCE_LINE = re.compile(r"SOURCE\s+(.{2,120}?)\s*$", re.M)

# BeautifulSoup's .text drops element boundaries, so a URL ending a paragraph
# runs into whatever follows it ("diviresorts.comSOURCE Divi Resorts"). Cutting
# the host at a known TLD is what recovers the domain.
TLDS = sorted("""com org net edu gov mil int io ai co us uk ca de fr es it nl se no fi dk pl
ch at be ie pt gr cz sk ro hu bg hr si ee lv lt ru ua cn jp kr in au nz sg hk tw my th id ph vn
za ng ke eg il ae sa qa tr br mx ar cl pe ve info biz tv me app dev tech cloud store online shop
news health law group life world global xyz ly fm gg to cc pro site space agency solutions systems
network media digital studio design finance capital ventures fund energy green eco care clinic
farm club live one team works city land house press today""".split(), key=len, reverse=True)

NON_COMPANY_DOMAINS = {
	"prnewswire.com", "cision.com", "prnasia.com", "newswire.ca", "email4pr.com",
	"businesswire.com", "globenewswire.com", "sec.gov",
	"linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
	"youtube.com", "youtu.be", "tiktok.com", "pinterest.com", "flickr.com", "vimeo.com",
	"apple.com", "apps.apple.com", "play.google.com", "google.com", "goo.gl",
	"bit.ly", "tinyurl.com", "ow.ly", "amazon.com", "wikipedia.org", "medium.com",
	"github.com", "zoom.us", "eventbrite.com", "hubspot.com", "wordpress.com",
}

LEGAL_SUFFIX = re.compile(
	r"\b(inc|incorporated|corp|corporation|co|company|llc|llp|lp|ltd|limited|plc|"
	r"holdings?|group|sa|nv|bv|ag|gmbh|ab|as|oyj|spa|srl|pte|bhd|kk|kg|pty)\b\.?")


def norm_name(s):
	s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
	s = LEGAL_SUFFIX.sub(" ", s)
	return re.sub(r"\s+", " ", s).strip()


def clean_domain(raw):
	labels = raw.strip(".").lower().split(".")
	for i in range(len(labels) - 1, 0, -1):
		for tld in TLDS:
			if labels[i] == tld:
				return ".".join(labels[:i + 1])
			if labels[i].startswith(tld):
				return ".".join(labels[:i] + [tld])
	return None


def release_facts(header, body, uploader=None):
	"""Everything about the sender that can be read off a single release."""
	body = body or ""
	out = {"facts_version": FACTS_VERSION}

	wire = WIRE.search(body[:2000]) or WIRE.search(body)
	if wire:
		lead = body[max(0, wire.start() - 140):wire.start()].rstrip().rstrip("-–—").rstrip()
		place = PLACE.search(DATE_TAIL.sub("", lead).rstrip(" , "))
		if place:
			city = re.sub(r"\s+", " ", place.group(1)).strip(" ,")
			region = (place.group(2) or "").strip(" ,")
			if len(city) > 1:
				out["dateline_city"] = city
				if region:
					out["dateline_region"] = re.sub(r"\s+", " ", region)

	uploader_norm = norm_name(uploader)
	tickers = []
	for match in TICKER.finditer(body):
		# Whose ticker is it? The words just before the paren are a weak hint
		# only — a securities firm names itself just as close to a ticker as an
		# issuer does — so the rollup decides ownership by concentration.
		lead = norm_name(body[max(0, match.start() - 90):match.start()])
		first = uploader_norm.split(" ")[0] if uploader_norm else ""
		tickers.append({
			"exchange": match.group(1).upper().replace(" ", ""),
			"symbol": match.group(2).upper(),
			"near_name": bool(uploader_norm) and (
				uploader_norm in lead or (len(first) > 3 and first in lead.split())),
		})
	if tickers:
		out["tickers"] = tickers

	out["fwd_looking"] = bool(FWD_LOOKING.search(body))
	out["litigation_vocab"] = bool(LITIGATION_VOCAB.search(body))
	out["report_vocab"] = bool(REPORT_VOCAB.search(body))

	spans = [(a.end(), a.group(1).strip()) for a in ABOUT.finditer(body)]
	if uploader_norm:
		for a in ABOUT_LOOSE.finditer(body):
			for width in (len(uploader_norm) + 12, len(uploader_norm) + 6, len(uploader_norm)):
				if norm_name(body[a.end():a.end() + width]) == uploader_norm:
					spans.append((a.end() + width, body[a.end():a.end() + width].strip()))
					break
	abouts = []
	for end, subject in sorted(set(spans)):
		nxt = SECTION_BREAK.search(body, end)
		text = re.sub(r"\s+", " ", body[end:nxt.start() if nxt else end + 2500]).strip()
		if len(text) > 80:
			abouts.append({"subject": subject, "text": text[:1500]})
	if abouts:
		own = [a for a in abouts if uploader_norm and norm_name(a["subject"]) == uploader_norm]
		out["about"] = (own or abouts)[-1]

	domains = []
	for match in URL.finditer(body):
		domain = clean_domain(match.group(1))
		if not domain:
			continue
		domain = domain.removeprefix("www.")
		if domain in NON_COMPANY_DOMAINS or domain.endswith(".prnewswire.com"):
			continue
		if domain not in domains:
			domains.append(domain)
	if domains:
		out["domains"] = domains[:10]

	source = SOURCE_LINE.search(body)
	if source:
		out["source_org"] = re.sub(r"\s+", " ", source.group(1)).strip()
	return out


FACT_FIELDS = ("dateline_city", "dateline_region", "tickers", "fwd_looking", "litigation_vocab",
               "report_vocab", "about", "domains", "source_org", "facts_version")


def backfill(batch_size=1000):
	"""Stamp `release_facts` onto scraped articles whose facts are stale."""
	articles = get_db()["articles"]
	query = {"status": "scraped", "facts_version": {"$ne": FACTS_VERSION}}
	todo = articles.count_documents(query)
	print(f"Stamping release facts on {todo} articles (version {FACTS_VERSION})")

	done, ops = 0, []
	cursor = articles.find(query, {"header": 1, "body": 1, "uploader": 1}, batch_size=200)
	for doc in cursor:
		facts = release_facts(doc.get("header"), doc.get("body"), doc.get("uploader"))
		unset = {f: "" for f in FACT_FIELDS if f not in facts}
		update = {"$set": facts}
		if unset:  # a re-run must not leave last version's fields behind
			update["$unset"] = unset
		ops.append(UpdateOne({"_id": doc["_id"]}, update))
		if len(ops) >= batch_size:
			articles.bulk_write(ops, ordered=False)
			done += len(ops)
			ops = []
			print(f"  {done}/{todo}")
	if ops:
		articles.bulk_write(ops, ordered=False)
		done += len(ops)
	print(f"Stamped {done}")


GOV_NAME = re.compile(r"^(u\.?s\.?|united states)\b|\b(department|ministry|bureau) of\b", re.I)
ACADEMIC_NAME = re.compile(r"\b(universit|college)\w*\b", re.I)
NONPROFIT_ABOUT = re.compile(
	r"non-?profit|501\(c\)|charit(?:y|able)|\bfoundation\b|\bassociation\b|"
	r"\bunion\b|\bsociety\b|advocacy", re.I)


def classify_org_type(company):
	"""Which kind of sender is this? Returns (org_type, method, confidence).

	Ordered, first match wins. The point of the non-default types is that they
	never launch a product of their own: they comment on other companies', or
	they publish reports about markets. They are also, by volume, the head of the
	corpus — the four largest senders are two research publishers and two
	securities firms.
	"""
	domain = company.get("domain") or ""
	name = company.get("name") or ""
	about = (company.get("about") or {}).get("text", "")
	ticker_share = (company.get("ticker") or {}).get("share_of_releases", 0)

	if domain.endswith((".gov", ".mil")) or GOV_NAME.search(name):
		return "government", "domain_or_name", "high"
	if domain.endswith(".edu") or ACADEMIC_NAME.search(name):
		return "academic", "domain_or_name", "high"
	if company.get("litigation_vocab_share", 0) >= 0.5:
		return "law_firm", "litigation_vocab", "high"
	if company.get("report_vocab_share", 0) >= 0.5:
		return "research_publisher", "report_vocab", "high"
	if company.get("tickers_mentioned_distinct", 0) >= 5 and ticker_share < 0.1:
		return "stock_commentary", "third_party_tickers", "medium"
	if domain.endswith(".org") and NONPROFIT_ABOUT.search(about):
		return "nonprofit", "domain_and_about", "medium"
	return "operating_company", "default", "low"


def decide_public(company):
	"""Is the sender listed? Returns (is_public, method, confidence).

	Ticker ownership is decided by concentration, not by which name sits nearest
	the symbol: an issuer repeats one ticker across nearly every release (Veeva,
	NYSE:VEEV, 97.8%) while a securities firm cycles through hundreds (Pomerantz
	mentions 1,001 distinct symbols and owns none). Proximity to the sender's own
	name fails outright here — The Schall Law Firm scores 99.5% on it.
	"""
	ticker_share = (company.get("ticker") or {}).get("share_of_releases", 0)
	n = company["n_releases"]

	if ticker_share >= 0.3:
		return True, "self_ticker", "high"
	if company.get("fwd_looking_share", 0) >= 0.5:
		return True, "safe_harbor_boilerplate", "medium"
	if company.get("tickers_mentioned_distinct", 0) >= 5 and ticker_share < 0.1:
		return False, "third_party_ticker_mentions", "medium"
	if n >= 5 and ticker_share < 0.1 and company.get("fwd_looking_share", 0) < 0.1:
		# Keyed on the modal share, not on whether any ticker appears at all:
		# HelloNation names one symbol across 2,473 releases and is plainly not
		# listed, but a rule demanding zero mentions leaves it undecided.
		return False, "no_consistent_ticker", "low"
	# One release is not evidence of anything, and half of all senders have
	# exactly one. Leaving them null is what keeps the base rate honest.
	return None, "insufficient_evidence", None


def rollup():
	"""Rebuild `companies` from the per-release facts."""
	db = get_db()
	articles = db["articles"]
	projection = {"uploader": 1, "uploader_link": 1, "published_at": 1, "category": 1,
	              "dateline_city": 1, "dateline_region": 1, "tickers": 1, "fwd_looking": 1,
	              "litigation_vocab": 1, "report_vocab": 1, "about": 1, "domains": 1,
	              "source_org": 1}
	print("Reading per-release facts")

	acc = {}
	seen = 0
	for doc in articles.find({"status": "scraped", "facts_version": FACTS_VERSION},
	                         projection, batch_size=500):
		slug = doc.get("uploader_link")
		if not slug:
			continue
		company = acc.get(slug)
		if company is None:
			company = acc[slug] = {
				"names": Counter(), "source_orgs": Counter(), "categories": Counter(),
				"places": Counter(), "domains": Counter(), "own_tickers": Counter(),
				"all_tickers": Counter(), "n": 0, "fwd": 0, "lit": 0, "rep": 0,
				"n_ticker_releases": 0, "first": None, "last": None,
				"about": None, "about_at": None, "n_about": 0,
			}
		company["n"] += 1
		company["names"][re.sub(r"\s+", " ", (doc.get("uploader") or "")).strip()] += 1
		if doc.get("source_org"):
			company["source_orgs"][doc["source_org"]] += 1
		if doc.get("category"):
			company["categories"][doc["category"]] += 1
		if doc.get("dateline_city"):
			company["places"][(doc["dateline_city"], doc.get("dateline_region") or "")] += 1
		for domain in doc.get("domains") or []:
			company["domains"][domain] += 1
		symbols = {(t["exchange"], t["symbol"]) for t in doc.get("tickers") or []}
		for sym in symbols:
			company["all_tickers"][sym] += 1
		if symbols:
			company["n_ticker_releases"] += 1
		# One vote per release, so a release repeating its ticker ten times does
		# not outweigh ten releases naming ten different ones.
		for sym in {(t["exchange"], t["symbol"]) for t in doc.get("tickers") or [] if t.get("near_name")}:
			company["own_tickers"][sym] += 1
		company["fwd"] += bool(doc.get("fwd_looking"))
		company["lit"] += bool(doc.get("litigation_vocab"))
		company["rep"] += bool(doc.get("report_vocab"))
		published = doc.get("published_at")
		if published:
			if company["first"] is None or published < company["first"]:
				company["first"] = published
			if company["last"] is None or published > company["last"]:
				company["last"] = published
		if doc.get("about"):
			company["n_about"] += 1
			if company["about_at"] is None or (published and published > company["about_at"]):
				company["about"] = doc["about"]
				company["about_at"] = published
		seen += 1
		if seen % 100000 == 0:
			print(f"  {seen} releases, {len(acc)} companies")

	print(f"Building {len(acc)} company documents")
	now = datetime.now(timezone.utc)
	ops = []
	for slug, c in acc.items():
		n = c["n"]
		company = {
			"_id": slug,
			"name": c["names"].most_common(1)[0][0] if c["names"] else None,
			"aliases": [name for name, _ in c["names"].most_common(5) if name],
			"source_orgs": [org for org, _ in c["source_orgs"].most_common(3)],
			"n_releases": n,
			"first_release": c["first"],
			"last_release": c["last"],
			"categories": dict(c["categories"].most_common()),
			"tickers_mentioned_distinct": len(c["all_tickers"]),
			"n_releases_with_ticker": c["n_ticker_releases"],
			"fwd_looking_share": round(c["fwd"] / n, 3),
			"litigation_vocab_share": round(c["lit"] / n, 3),
			"report_vocab_share": round(c["rep"] / n, 3),
			"rollup_version": ROLLUP_VERSION,
			"built_at": now,
		}
		company["name_norm"] = norm_name(company["name"])

		# The modal ticker is the candidate; a lone match across thousands of
		# releases is noise, so it has to recur before it counts as owned.
		owned = c["own_tickers"] or c["all_tickers"]
		if owned:
			(exchange, symbol), count = owned.most_common(1)[0]
			if count >= 2 or n <= 3:
				company["ticker"] = {
					"exchange": exchange, "symbol": symbol,
					"share_of_releases": round(count / n, 3),
				}
		company["third_party_ticker_ratio"] = round(
			1 - (sum(c["own_tickers"].values()) / max(sum(c["all_tickers"].values()), 1)), 2)

		if c["places"]:
			(city, region), count = c["places"].most_common(1)[0]
			company["hq"] = {"city": city, "region": region or None,
			                 "share": round(count / n, 3), "method": "modal_dateline"}
			company["dateline_places"] = [
				{"city": city, "region": region or None, "n": count}
				for (city, region), count in c["places"].most_common(5)]
		if c["domains"]:
			company["domain"] = c["domains"].most_common(1)[0][0]
			company["domains"] = [{"domain": d, "n": k} for d, k in c["domains"].most_common(5)]
		if c["about"]:
			company["about"] = {**c["about"], "as_of": c["about_at"],
			                    "n_releases_with_about": c["n_about"]}

		is_public, method, confidence = decide_public(company)
		if is_public is not True:
			# Kia America mentions NYSE:CARS and BAE Systems mentions BA without
			# owning either. Keeping the symbol on a sender we have not called
			# public would read as a listing; the mention survives in
			# `tickers_mentioned_distinct`.
			company.pop("ticker", None)
		company["is_public"] = is_public
		company["is_public_method"] = method
		company["is_public_confidence"] = confidence
		org_type, org_method, org_confidence = classify_org_type(company)
		company["org_type"] = org_type
		company["org_type_method"] = org_method
		company["org_type_confidence"] = org_confidence

		ops.append(ReplaceOne({"_id": slug}, company, upsert=True))
		if len(ops) >= 1000:
			db["companies"].bulk_write(ops, ordered=False)
			ops = []
	if ops:
		db["companies"].bulk_write(ops, ordered=False)
	print(f"Wrote {len(acc)} companies")


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Mine company facts from press releases")
	parser.add_argument("--rollup", action="store_true",
		help="rebuild the companies collection from already-stamped release facts")
	args = parser.parse_args()

	ensure_indexes()
	if args.rollup:
		rollup()
	else:
		backfill()
