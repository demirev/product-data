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


FACTS_VERSION = "tier0-2"
ROLLUP_VERSION = "tier1-1"


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

# Cutting the host at a known TLD is what recovers a domain from text: the
# glued-together tail below needs a TLD to cut at, and so does the reduction to
# a registrable domain. The list is curated rather than the full IANA set on
# purpose — the last label is matched by prefix as well as in full, and against
# 1,400 TLDs almost any glued word carries some TLD as a prefix
# ("example.foobar" -> "example.foo"), which would turn junk into
# plausible-looking domains instead of leaving it alone.
TLDS = frozenset("""com org net edu gov mil int io ai co us uk ca de fr es it nl se no fi dk pl
ch at be ie pt gr cz sk ro hu bg hr si ee lv lt ru ua cn jp kr in au nz sg hk tw my th id ph vn
za ng ke eg il ae sa qa tr br mx ar cl pe ve eu asia africa is ag ms so sh su mn cr sc nyc
bd bh bm bn bo cx cy do ec et ge gh gl gy im ir jm jo kh kn kw ky kz la lb li lk lu ma mc
mk ml mm mo mt mu np om pa pk pn pr ps py rs st tl tn tt tz ug uy uz ws zm
info biz tv me app dev tech cloud store online shop bank bio credit vc realtor legal wine
news health law group life world global xyz ly fm gg gle to cc pro site space agency link
solutions systems network media digital studio design finance capital ventures fund money
energy green eco earth solar care clinic farm club live one team works city land house
press today events games ink zone work aero coffee foundation dating movie buzz
security auto travel express gold reit markets exchange video game watch haus swiss
build realty jobs coach plus academy institute company insurance""".split())

# Registry second levels: under "co.uk" or "com.br" the next label down is the
# registrant's, so the suffix is two labels deep and a blind reduction to the
# last two would strip the company's own name. Generated rather than
# enumerated, because country registries reuse a small vocabulary of them — a
# spurious entry only leaves one extra label on the domain, whereas a missing
# one loses the name.
MULTI_TLDS = frozenset(
	f"{second}.{cc}"
	for second in "com co org net edu gov ac go or ne gob govt mil sch nom asn ltd plc id in on".split()
	for cc in TLDS if len(cc) == 2)

# Matched against the registrable domain `clean_domain` returns, so a
# subdomain ("news.cision.com", "mma.prnewswire.com") is caught by the same
# exact comparison as the bare host.
NON_COMPANY_DOMAINS = {
	"prnewswire.com", "cision.com", "prnasia.com", "newswire.ca", "email4pr.com",
	"businesswire.com", "globenewswire.com", "sec.gov",
	"asianetnews.net", "kyodonewsprwire.jp", "24-7pressrelease.com",
	"linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
	"youtube.com", "youtu.be", "tiktok.com", "pinterest.com", "flickr.com", "vimeo.com",
	"apple.com", "google.com", "goo.gl", "goo.su", "forms.gle",
	"bit.ly", "tinyurl.com", "ow.ly", "lnk.to", "amazon.com", "wikipedia.org", "medium.com",
	"github.com", "blogspot.com", "zoom.us", "eventbrite.com", "hubspot.com", "wordpress.com",
}

LEGAL_SUFFIX = re.compile(
	r"\b(inc|incorporated|corp|corporation|co|company|llc|llp|lp|ltd|limited|plc|"
	r"holdings?|group|sa|nv|bv|ag|gmbh|ab|as|oyj|spa|srl|pte|bhd|kk|kg|pty)\b\.?")


def norm_name(s):
	s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
	s = LEGAL_SUFFIX.sub(" ", s)
	return re.sub(r"\s+", " ", s).strip()


# --- location normalisation -------------------------------------------------
# Every external source states a region in a code — SEC "VA", IRS "CA", GLEIF
# "US-DE", ROR "US-CA" — while the dateline states it as PR Newswire prints it,
# in AP style ("Calif.", "N.C.", "W.Va."). This table is the bridge, and it is
# also what closes the README's second known gap, where regions were stored
# unnormalised. It doubles as the test for "is this dateline in the US at all",
# which is what cuts the GLEIF and IRS name blocks down to a country.
_US_REGIONS = """
AL Alabama|Ala.; AK Alaska; AZ Arizona|Ariz.; AR Arkansas|Ark.; CA California|Calif.;
CO Colorado|Colo.; CT Connecticut|Conn.; DE Delaware|Del.; DC District of Columbia|D.C.|Washington DC;
FL Florida|Fla.; GA Georgia|Ga.; HI Hawaii; ID Idaho; IL Illinois|Ill.; IN Indiana|Ind.;
IA Iowa; KS Kansas|Kan.; KY Kentucky|Ky.; LA Louisiana|La.; ME Maine; MD Maryland|Md.;
MA Massachusetts|Mass.; MI Michigan|Mich.; MN Minnesota|Minn.; MS Mississippi|Miss.;
MO Missouri|Mo.; MT Montana|Mont.; NE Nebraska|Neb.; NV Nevada|Nev.; NH New Hampshire|N.H.;
NJ New Jersey|N.J.; NM New Mexico|N.M.; NY New York|N.Y.; NC North Carolina|N.C.;
ND North Dakota|N.D.; OH Ohio; OK Oklahoma|Okla.; OR Oregon|Ore.; PA Pennsylvania|Pa.;
RI Rhode Island|R.I.; SC South Carolina|S.C.; SD South Dakota|S.D.; TN Tennessee|Tenn.;
TX Texas; UT Utah; VT Vermont|Vt.; VA Virginia|Va.; WA Washington|Wash.;
WV West Virginia|W.Va.; WI Wisconsin|Wis.; WY Wyoming|Wyo.; PR Puerto Rico|P.R.;
VI Virgin Islands; GU Guam
"""


def _region_key(value):
	"""Fold a region to letters only, so "N.C.", "NC" and "N. C." are one key."""
	return re.sub(r"[^a-z]", "", (value or "").lower())


US_REGION_BY_KEY = {}
for _entry in _US_REGIONS.replace("\n", " ").split(";"):
	_entry = _entry.strip()
	if not _entry:
		continue
	_code, _names = _entry.split(" ", 1)
	for _name in [_code] + _names.split("|"):
		US_REGION_BY_KEY[_region_key(_name)] = _code


def us_state(region):
	"""USPS code for a region as any of our sources spells it, or None.

	None is also the answer for a foreign dateline, which is exactly what the
	IRS and GLEIF resolvers need in order to know not to try a US name block.
	"""
	return US_REGION_BY_KEY.get(_region_key(region))


def to_int(value):
	"""`value` as an int, or None — external sources spell a missing number as
	an empty string, a blank-padded field, or the literal "0000"."""
	try:
		return int(str(value).strip())
	except (TypeError, ValueError):
		return None


def clean_domain(raw):
	"""The registrable domain behind a URL match, or None.

	Two artefacts sit between the match and a usable key, both from
	BeautifulSoup's .text dropping element boundaries. A URL ending a paragraph
	runs into whatever follows it ("diviresorts.comSOURCE Divi Resorts"), and a
	sentence-ending period does the same one label further out, minting a label
	that can even be a real TLD ("1440foods.com.Media Contact" -> .media). Both
	are repaired at the tail, because that is the only place they occur — inside
	the host the dots survive.

	What is left is reduced to registrable domain + public suffix, dropping
	"ir." and "investors.": RDAP and Wikidata are keyed on that form, and it is
	also what makes one company's URLs count as one domain in the rollup.
	"""
	raw = raw.strip(".")
	labels = raw.lower().split(".")
	parts = raw.split(".")  # same labels, original case: the tail tests read it
	if len(labels) < 2:
		return None

	glued = max((t for t in TLDS if labels[-1].startswith(t)), key=len, default=None)
	rest = parts[-1][len(glued):] if glued else ""
	if labels[-1] not in TLDS and glued and parts[-1][:len(glued)].islower() and (
			# What follows has to read as a new word — a capital ("comSOURCE",
			# "brFoto"), a digit or a dash ("gov-", "com36150"), or the next URL
			# ("example.comhttps://..."). Demanding that is what leaves the TLDs
			# missing from the list above alone: ".security" and ".inc" open
			# with .se and .in but run on in lowercase, so nothing is cut off
			# them.
			not rest[:1].islower() or rest[:4].lower() == "http"):
		labels[-1] = glued

	while len(labels) > 2 and labels[-2] in TLDS and (
			# With a real TLD one label back, this label is the sentence the
			# period ran into when it is no TLD at all ("honor.com.https") or
			# when it starts a word where the TLD before it does not
			# ("1440foods.com.Media", "winchestercarlisle.com.PR" — .pr is a
			# registry suffix, but nobody writes one in caps). Lowercase spares
			# the hosts that genuinely nest: "acbel.com.tw", "www.realtor.org".
			labels[-1] not in TLDS
			or parts[len(labels) - 1][:1].isupper() and not parts[len(labels) - 2][:1].isupper()):
		labels.pop()

	if not labels[-1].isalpha():
		return None  # TLDs are letters, so "britecap.com1.866.623.4990" is a phone number

	depth = 3 if ".".join(labels[-2:]) in MULTI_TLDS else 2
	return ".".join(labels[-depth:]) if len(labels) >= depth else None


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
		if not domain or domain in NON_COMPANY_DOMAINS:
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


# --- external enrichment ----------------------------------------------------
# `py/enrich.py` resolves each company against six public registries and stores
# what it finds whole, per source, in `company_enrichment`. This is the seam
# where a curated subset of that crosses over onto `companies`.
#
# It has to happen inside `rollup` because the rollup writes with `ReplaceOne` —
# a separate pass would be overwritten by the next rebuild. That is also the
# right place for it: the enrichment is then picked up automatically by the
# `--rollup-when-idle` the article scraper already runs.


def first_of(*candidates):
	"""First (value, source) pair carrying a value, or (None, None).

	Every cascade below is written as an ordered list of these, so the precedence
	is readable as data rather than as a chain of ifs, and the source that won
	is recorded next to the value it produced.
	"""
	for value, source in candidates:
		if value not in (None, "", [], {}):
			return value, source
	return None, None


def year_of(value):
	"""Leading year of an ISO-ish date string, if it is a plausible one."""
	match = re.match(r"(\d{4})", str(value or ""))
	year = int(match.group(1)) if match else None
	return year if year and 1600 <= year <= datetime.now(timezone.utc).year else None


def domicile_code(value):
	"""A jurisdiction as ISO 3166-2, or None if it cannot be made into one.

	Form D writes it out ("CALIFORNIA"), SEC abbreviates it ("VA"), GLEIF already
	codes it ("US-DE"). Anything else — notably EDGAR's own country codes, where
	Toyota is incorporated in "M0" — is dropped rather than passed through, so
	the field means one thing everywhere it is set.
	"""
	if not value:
		return None
	if re.fullmatch(r"[A-Z]{2}-[A-Z0-9]{1,3}", value):
		return value
	state = us_state(value)
	return f"US-{state}" if state else None


# Which external type claims are worth believing. ROR's own vocabulary is
# checked before the IRS, because a university is in the Business Master File
# too and "academic" is the more specific answer.
ROR_ORG_TYPES = {"education": "academic", "government": "government",
                 "nonprofit": "nonprofit", "healthcare": "operating_company"}
# Wikidata P31 values, by the QIDs `enrich.ORG_CLASSES` already recognises.
WIKIDATA_ORG_TYPES = {"Q3918": "academic", "Q189004": "academic", "Q875538": "academic",
                      "Q163740": "nonprofit", "Q157031": "nonprofit",
                      "Q327333": "government", "Q2085381": "academic",
                      "Q31855": "academic", "Q16917": "operating_company"}


def external_org_type(enrichment):
	"""(org_type, method, confidence) from the registries, or None.

	Only the *positive* identifications are here. Nothing external says "this is
	a law firm trawling for plaintiffs" or "this is a market-research publisher",
	which are the classifications that matter most for this corpus — those stay
	behavioural, and `enrichment_fields` only lets an external answer through
	when the heuristic fell back to its default.
	"""
	ror = enrichment.get("ror") or {}
	if ror.get("matched"):
		for kind in ror.get("types") or []:
			if kind in ROR_ORG_TYPES:
				return ROR_ORG_TYPES[kind], "ror_type", "high"
	if (enrichment.get("irs") or {}).get("matched"):
		return "nonprofit", "irs_bmf", "high"
	facts = (enrichment.get("wikidata") or {}).get("facts") or {}
	for qid in facts.get("instance_of_qids") or []:
		if qid in WIKIDATA_ORG_TYPES:
			return WIKIDATA_ORG_TYPES[qid], "wikidata_p31", "medium"
	return None


def enrichment_fields(enrichment, company):
	"""The flat fields `companies` carries over from `company_enrichment`.

	`company` is the heuristic document as `rollup` has just built it; the
	returned dict is applied on top of it, so anything it names wins and anything
	it leaves out keeps the value the release bodies produced.
	"""
	if not enrichment:
		return {}

	wikidata = enrichment.get("wikidata") or {}
	facts = wikidata.get("facts") or {}
	sec = enrichment.get("sec") or {}
	submissions = sec.get("submissions") or {}
	xbrl = sec.get("facts") or {}
	form_d = next((f.get("data") for f in sec.get("form_d") or [] if f.get("data")), {})
	gleif = enrichment.get("gleif") or {}
	gleif_record = gleif.get("record") or {}
	rdap = enrichment.get("rdap") or {}
	irs = enrichment.get("irs") or {}
	irs_record = irs.get("record") or {}
	ror = enrichment.get("ror") or {}

	out = {
		"enrichment_sources": enrichment.get("matched_sources") or [],
		"enriched_at": enrichment.get("enriched_at"),
		"enrichment_version": enrichment.get("enrichment_version"),
	}
	for field, block, key in (("wikidata_id", wikidata, "qid"), ("cik", sec, "cik"),
	                          ("lei", gleif, "lei"), ("ein", irs, "ein"),
	                          ("ror_id", ror, "id")):
		if block.get("matched") and block.get(key):
			out[field] = block[key]

	# --- age ---------------------------------------------------------------
	# Direct statements of a founding date before proxies for one. GLEIF has
	# Smithfield incorporating in 1997 and the SEC first hearing from it in 1994,
	# but the company was founded in 1936 and only Wikidata says so; the IRS
	# ruling month and the first filing date are the last resort precisely
	# because they date a registration rather than a founding.
	founded, source = first_of(
		(facts.get("inception_year"), "wikidata_p571"),
		(year_of(gleif_record.get("Entity.EntityCreationDate")), "gleif_creation_date"),
		(ror.get("established"), "ror_established"),
		(to_int(form_d.get("year_of_inc")), "sec_form_d"),
		(irs.get("ruling_year"), "irs_ruling"),
		(year_of(submissions.get("first_filing_date")), "sec_first_filing"),
	)
	if founded:
		out["founded_year"] = founded
		out["founded_year_src"] = source
	# The only age signal that exists for a sender no registry knows, and 69,059
	# of 98,860 companies have a domain to read it off.
	registered = year_of(rdap.get("registration_date"))
	if registered:
		out["domain_registered_year"] = registered

	# --- location ----------------------------------------------------------
	# One source supplies the whole address where it can, so country, region and
	# city are not stitched together from three registries that disagree; only a
	# field the winner leaves blank is filled from further down.
	business = submissions.get("business_address") or {}
	sec_state = us_state(business.get("stateOrCountry"))
	addresses = []
	# Only when the address is a US state. EDGAR codes foreign locations in a
	# scheme of its own ("M0" for Japan) and files the country in the city line,
	# so a foreign filer would otherwise contribute country "M0", city "JAPAN".
	# GLEIF and Wikidata state a real ISO country, so they answer for those.
	if sec.get("matched") and sec_state:
		addresses.append({"src": "sec", "country": "US", "region": sec_state,
			"city": (business.get("city") or "").title() or None})
	if gleif_record.get("Entity.HeadquartersAddress.Country"):
		addresses.append({"src": "gleif",
			"country": gleif_record["Entity.HeadquartersAddress.Country"],
			"region": (gleif_record.get("Entity.HeadquartersAddress.Region") or "").rsplit("-", 1)[-1] or None,
			"city": (gleif_record.get("Entity.HeadquartersAddress.City") or "").title() or None})
	if facts.get("country"):
		addresses.append({"src": "wikidata", "country": facts["country"],
			"region": None, "city": facts.get("hq_label")})
	if ror.get("country"):
		addresses.append({"src": "ror", "country": ror["country"], "region": ror.get("region"),
			"city": ror.get("city")})
	if irs.get("matched"):
		addresses.append({"src": "irs", "country": "US", "region": irs_record.get("STATE"),
			"city": (irs_record.get("CITY") or "").title() or None})
	dateline_region = us_state((company.get("hq") or {}).get("region"))
	if dateline_region:
		addresses.append({"src": "dateline", "country": "US", "region": dateline_region,
			"city": ((company.get("hq") or {}).get("city") or "").title() or None})

	if addresses:
		primary = addresses[0]
		out["country"] = primary["country"]
		out["location_src"] = primary["src"]
		out["region"] = primary["region"] or next(
			(a["region"] for a in addresses if a["region"]), None)
		out["hq_city"] = primary["city"] or next(
			(a["city"] for a in addresses if a["city"]), None)

	# Where it is incorporated, which is not where it sits: Stampli is a Delaware
	# company headquartered in Mountain View, and both facts are covariates.
	domicile, source = first_of(
		(domicile_code(submissions.get("stateOfIncorporation")), "sec_state_of_inc"),
		(gleif_record.get("Entity.LegalJurisdiction"), "gleif_jurisdiction"),
		(domicile_code(form_d.get("jurisdiction_of_inc")), "sec_form_d"),
		(facts.get("formation_location"), "wikidata_p740"),
	)
	if domicile:
		out["domicile"] = domicile
		out["domicile_src"] = source

	# --- sector and legal form ---------------------------------------------
	if submissions.get("sic"):
		out["sic"] = submissions["sic"]
		out["sic_desc"] = submissions.get("sicDescription")
	if irs.get("ntee_code"):
		out["ntee_code"] = irs["ntee_code"]
	legal_form, source = first_of(
		(gleif_record.get("Entity.LegalForm.EntityLegalFormCode"), "gleif_elf"),
		(gleif_record.get("Entity.LegalForm.OtherLegalForm"), "gleif_other"),
		(submissions.get("entityType"), "sec_entity_type"),
	)
	if legal_form:
		out["legal_form"] = legal_form
		out["legal_form_src"] = source

	# --- size ---------------------------------------------------------------
	# With the unit, always. A foreign private issuer reports in its own
	# currency — Toyota's revenue is 29.9 trillion JPY — so a bare number here
	# would be three orders of magnitude wrong the moment anyone compared it.
	revenue = xbrl.get("revenue") or {}
	amount, source = first_of((revenue.get("value"), "sec_xbrl"),
	                          (irs.get("revenue_amt"), "irs_bmf"))
	if amount is not None:
		out["revenue"] = amount
		out["revenue_unit"] = revenue.get("unit") if source == "sec_xbrl" else "USD"
		out["revenue_year"] = (year_of(revenue.get("end")) if source == "sec_xbrl"
		                       else year_of(irs_record.get("TAX_PERIOD")))
		out["revenue_src"] = source
	assets = xbrl.get("assets") or {}
	amount, source = first_of((assets.get("value"), "sec_xbrl"),
	                          (irs.get("asset_amt"), "irs_bmf"))
	if amount is not None:
		out["assets"] = amount
		out["assets_unit"] = assets.get("unit") if source == "sec_xbrl" else "USD"
		out["assets_src"] = source
	if facts.get("employees"):
		out["employees"] = facts["employees"]
		out["employees_year"] = facts.get("employees_year")

	# --- parent -------------------------------------------------------------
	# The README's standing gap: a subsidiary inherits its parent's listing
	# because the release bodies cannot tell them apart. These fields are what
	# tell them apart, and `external_public` below acts on them.
	if facts.get("parent_qid"):
		out["parent_wikidata_id"] = facts["parent_qid"]
		out["parent_name"] = facts.get("parent_label")
	if gleif.get("parent_lei"):
		out["parent_lei"] = gleif["parent_lei"]
		out["parent_relation"] = gleif.get("parent_type")
		out.setdefault("parent_name", gleif.get("parent_name"))

	out.update(external_public(enrichment, company, out))

	external = external_org_type(enrichment)
	# Only when the heuristic fell back to its default. `law_firm`,
	# `research_publisher` and `stock_commentary` are read off behaviour and are
	# the whole point of the classification — a registry calling Pomerantz an
	# "operating company" is true and useless.
	if external and company.get("org_type") == "operating_company":
		out["org_type"], out["org_type_method"], out["org_type_confidence"] = external
	return out


def external_public(enrichment, company, carried):
	"""Listing status from the registries, or {} to keep the heuristic's answer.

	The heuristic reads concentration of ticker mentions, which is the best a
	press release can support. A registry can do better in both directions: a
	symbol in SEC's own list settles it, and a GLEIF entity with no ISINs is
	positive evidence of *not* being listed, which no body text ever supplies.
	"""
	sec = enrichment.get("sec") or {}
	listing = sec.get("tickers_exchange") or {}
	facts = (enrichment.get("wikidata") or {}).get("facts") or {}
	gleif = enrichment.get("gleif") or {}

	if listing.get("ticker"):
		return {"is_public": True, "is_public_method": "sec_ticker_exchange",
		        "is_public_confidence": "high",
		        "ticker": {**(company.get("ticker") or {}), "exchange": listing["exchange"],
		                   "symbol": listing["ticker"], "source": "sec"},
		        "listings": listing.get("listings") or []}
	if facts.get("exchanges"):
		return {"is_public": True, "is_public_method": "wikidata_exchange",
		        "is_public_confidence": "medium"}

	# The README's standing gap, closed on direct evidence rather than a guess.
	# The rollup called this company public because one symbol recurs across its
	# releases; SEC's own list says that symbol belongs to a filer by another
	# name, and an independent registry knows this company as an entity in its
	# own right. Toyota Motor North America prints NYSE:TM in 64% of its
	# releases, and NYSE:TM is Toyota Motor Corp's.
	conflict = sec.get("ticker_conflict")
	identified = any((enrichment.get(source) or {}).get("matched")
	                 for source in ("gleif", "ror", "wikidata"))
	if conflict and identified and company.get("is_public_method") == "self_ticker":
		return {"is_public": False, "is_public_method": "ticker_belongs_to_other_filer",
		        "is_public_confidence": "medium",
		        "ticker_filer": conflict}

	# Withheld when the heuristic already found a concentrated self-ticker: GLEIF
	# maps ISINs it has been told about, and silence there is weaker evidence
	# than a symbol repeated across a company's own releases.
	if (gleif.get("matched") and gleif.get("n_isins") == 0
			and not (company.get("is_public") is True
			         and company.get("is_public_confidence") == "high")):
		return {"is_public": False, "is_public_method": "gleif_no_isin",
		        "is_public_confidence": "medium"}
	return {}


def rollup(use_enrichment=True):
	"""Rebuild `companies` from the per-release facts, plus external enrichment.

	`use_enrichment=False` rebuilds the heuristic-only document — which is what
	makes the overwrite in `enrichment_fields` reversible, and how you measure
	how often the rules read off the release bodies were right on their own.
	"""
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
	slugs = list(acc)
	enriched_total = 0

	for start in range(0, len(slugs), 1000):
		chunk = slugs[start:start + 1000]
		# Fetched a chunk at a time rather than all at once: the enrichment
		# documents carry whole registry records, and the three blobs excluded
		# here are most of their weight and none of what crosses over.
		enrichment = {} if not use_enrichment else {
			doc["_id"]: doc for doc in db["company_enrichment"].find(
				{"_id": {"$in": chunk}},
				{"wikidata.entity": 0, "rdap.record": 0, "ror.record": 0,
				 "sec.form_d.data.related_persons": 0})}
		ops = []
		for slug in chunk:
			c = acc[slug]
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

			# External evidence last, so it sits on top of everything the release
			# bodies produced and can overrule it where a registry knows better.
			carried = enrichment_fields(enrichment.get(slug), company)
			company.update(carried)
			enriched_total += bool(carried)

			ops.append(ReplaceOne({"_id": slug}, company, upsert=True))
		if ops:
			db["companies"].bulk_write(ops, ordered=False)
	print(f"Wrote {len(acc)} companies, {enriched_total} carrying external enrichment")


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Mine company facts from press releases")
	parser.add_argument("--rollup", action="store_true",
		help="rebuild the companies collection from already-stamped release facts")
	parser.add_argument("--no-enrichment", action="store_true",
		help="rebuild without the external fields from company_enrichment")
	args = parser.parse_args()

	ensure_indexes()
	if args.rollup:
		rollup(use_enrichment=not args.no_enrichment)
	else:
		backfill()
