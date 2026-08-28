"""External registry data for each company, keyed by `uploader_link`.

`py/company.py` reads a sender off its own press releases. That is Tier 0 and its
ceiling shows in the numbers: no founding date at all, `is_public` null for three
senders in four, and `org_type` sitting on its low-confidence default for 93% of
them. This is the tier that goes outside for the rest.

Six registries, resolved per company and stored whole in `company_enrichment`:

	I    Wikidata   the crosswalk hub — one match hands over CIK, LEI and ISIN
	II   SEC        tickers/exchange, cik-lookup, submissions, companyfacts, Form D
	III  GLEIF      legal entity, incorporation date, jurisdiction, parent
	IV   RDAP       the resolved domain's registration record
	V    IRS BMF    non-profits: EIN, NTEE code, revenue, ruling date
	VI   ROR        research institutions

Wikidata runs first on purpose. A P856 match yields P5531 (CIK), P1278 (LEI) and
P946 (ISIN), which turns three downstream fuzzy name matches into exact-key
lookups. Where no crosswalk exists the cascade falls to the domain, and only then
to name + location — and a name block has to be *unique* to count, because a
second candidate in the block means we do not know, not that we should guess.

Two modes:

	python py/enrich.py --refresh   # bulk sources -> staging collections
	python py/enrich.py             # resolve companies, biggest senders first
	python py/enrich.py --loop      # the same, forever (the `enrich` service)

Both are resumable, and resumption is version-based rather than time-based: a
company is due for a source only when that block is missing or its
`resolver_version` is behind. A source that has answered is never re-asked on a
schedule, so a fresh GLEIF golden copy refreshes *staging* without re-resolving
the companies that already matched. `--retry-misses` re-queues the non-matches
when you want a second look.

`py/company.py --rollup` folds a curated subset of all this onto `companies`;
see `ENRICHED_FIELDS` there for what crosses over and in what precedence.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone

import requests
from pymongo import UpdateOne
from requests.adapters import HTTPAdapter, Retry

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import get_db, ensure_indexes
from company import clean_domain, norm_name, to_int, us_state


ENRICHMENT_VERSION = "tier1-1"

# One version per source, bumped independently: a change to the Form D parser
# should re-resolve SEC without re-crawling 69k RDAP records. This is the only
# thing that makes a company due again once a source has answered.
RESOLVER_VERSIONS = {
	"wikidata": "wd-2",
	"sec": "sec-2",
	"gleif": "gleif-2",
	"rdap": "rdap-1",
	"irs": "irs-1",
	"ror": "ror-1",
}
SOURCES = tuple(RESOLVER_VERSIONS)


# --- HTTP -------------------------------------------------------------------
# Every source here is a public good being read at scale, so identify honestly.
# SEC goes further and *requires* it: requests whose User-Agent names no contact
# are refused outright, so `ENRICH_CONTACT` is effectively mandatory for II. It
# is deliberately not defaulted to anyone's address — the value is sent to six
# external services on every request, which makes it the operator's to choose.
CONTACT = os.environ.get("ENRICH_CONTACT", "").strip()
USER_AGENT = f"product-data-research/1.0 ({CONTACT})" if CONTACT else "product-data-research/1.0"
TIMEOUT = (10, 60)

# One Session for the process, for the reason documented in scrape_prn.py: bare
# `requests.get` re-resolves DNS every call, and sustained lookups at this rate
# exhaust Docker's embedded resolver.
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
for _scheme in ("https://", "http://"):
	SESSION.mount(_scheme, HTTPAdapter(pool_maxsize=4, max_retries=Retry(
		total=3, connect=3, read=3, status=3, backoff_factor=1.0,
		status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",),
		respect_retry_after_header=True)))

# Minimum seconds between calls per host. SEC publishes a 10 req/s ceiling and
# blocks over it; Wikimedia asks for serial requests rather than a rate; the
# registries have no published number, so `.com`/`.net` — which is most of the
# corpus and all lands on one Verisign endpoint — gets the most conservative
# gap. Anything unlisted falls back to DEFAULT_GAP.
HOST_GAP = {
	"www.sec.gov": 0.12,
	"data.sec.gov": 0.12,
	"www.wikidata.org": 0.15,
	"rdap.verisign.com": 0.5,
	"api.gleif.org": 0.25,
}
DEFAULT_GAP = 0.34
_last_call = {}


class EnrichError(Exception):
	"""A source we reached but could not use. `status_code` is None when the
	response was fine and its shape was not."""

	def __init__(self, message, status_code=None):
		super().__init__(message)
		self.status_code = status_code


def fetch(url, params=None, host=None, stream=False):
	"""GET `url`, rate-limited per host. Raises EnrichError on a bad status."""
	host = host or requests.utils.urlparse(url).netloc
	gap = HOST_GAP.get(host, DEFAULT_GAP)
	elapsed = time.monotonic() - _last_call.get(host, 0.0)
	if elapsed < gap:
		time.sleep(gap - elapsed)
	_last_call[host] = time.monotonic()

	response = SESSION.get(url, params=params, timeout=TIMEOUT, stream=stream)
	if response.status_code != 200:
		raise EnrichError(f"{url} -> {response.status_code}", response.status_code)
	return response


def fetch_json(url, params=None, host=None):
	response = fetch(url, params=params, host=host)
	try:
		return response.json()
	except ValueError as e:
		raise EnrichError(f"{url}: not JSON ({e})") from e


# 404 means the registry has no such record — an answer, not a failure. Anything
# else is transient and re-queued by the ordinary due check on the next pass.
ABSENT_STATUS = (400, 403, 404, 410)


def now():
	return datetime.now(timezone.utc)


def download(url, path, host=None):
	"""Stream a bulk file to `path`. Returns its size in bytes."""
	os.makedirs(os.path.dirname(path), exist_ok=True)
	tmp = path + ".part"
	response = fetch(url, host=host, stream=True)
	size = 0
	with open(tmp, "wb") as fh:
		for chunk in response.iter_content(chunk_size=1 << 20):
			fh.write(chunk)
			size += len(chunk)
	os.replace(tmp, path)
	return size


def zip_rows(path):
	"""Yield dict rows from the single CSV inside a zip, streamed."""
	with zipfile.ZipFile(path) as archive:
		inner = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
		with archive.open(inner) as fh:
			# newline="" so quoted fields carrying newlines survive; GLEIF has them.
			for row in csv.DictReader(io.TextIOWrapper(fh, "utf-8", newline="")):
				yield row


def nonempty(row, prefix=None):
	"""Drop empty columns — GLEIF's 338 are ~95% blank on any given record, so
	keeping only what is filled in is what makes "store the whole record" cheap."""
	out = {}
	for key, value in row.items():
		if value is None or value == "":
			continue
		out[key[len(prefix):] if prefix and key.startswith(prefix) else key] = value
	return out


def write_batches(collection, ops, batch_size=5000):
	"""Drain `ops` into `collection` in batches; returns the number written."""
	done = 0
	buffer = []
	for op in ops:
		buffer.append(op)
		if len(buffer) >= batch_size:
			collection.bulk_write(buffer, ordered=False)
			done += len(buffer)
			buffer = []
			if done % 250000 == 0:
				print(f"    {done}")
	if buffer:
		collection.bulk_write(buffer, ordered=False)
		done += len(buffer)
	return done


# --- bulk staging -----------------------------------------------------------
# Four of the six sources publish everything they have as one file, which is
# both politer and faster than a per-company API call. They land in staging
# collections so the resolver does indexed point lookups instead of holding
# 5.4M rows in RAM, and `source_meta` records what was loaded and when.

DATA_DIR = os.environ.get("ENRICH_DATA_DIR", "data/enrich")

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_CIK_LOOKUP_URL = "https://www.sec.gov/Archives/edgar/cik-lookup-data.txt"
GLEIF_PUBLISHES_URL = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes"
GLEIF_ISIN_URL = "https://mapping.gleif.org/api/v2/isin-lei"
ROR_RECORDS_URL = "https://zenodo.org/api/communities/ror-data/records"
IRS_BMF_URLS = [f"https://www.irs.gov/pub/irs-soi/{name}.csv"
                for name in ("eo1", "eo2", "eo3", "eo4", "eo_xx")]


def note_source(name, **fields):
	get_db()["source_meta"].update_one(
		{"_id": name}, {"$set": {"fetched_at": now(), **fields}}, upsert=True)
	print(f"  {name}: " + ", ".join(f"{k}={v}" for k, v in fields.items()))


def refresh_sec_tickers():
	"""SEC's ticker/exchange list — every current registrant with a symbol.

	One row per *symbol*, not per company: a filer with several share classes
	lists each separately (Alphabet files GOOG and GOOGL under CIK 1652044), so
	keying on the CIK would silently drop all but the last and leave the dropped
	symbols unmatchable. The resolver de-duplicates to CIKs where it needs to.
	"""
	collection = get_db()["sec_tickers"]
	payload = fetch_json(SEC_TICKERS_URL)
	fields = payload["fields"]
	collection.delete_many({})
	rows = (dict(zip(fields, row)) for row in payload["data"])
	n = write_batches(collection, (
		UpdateOne({"_id": f"{row['cik']:010d}:{(row.get('ticker') or '').upper()}"}, {"$set": {
			"cik": f"{row['cik']:010d}",
			"name": row["name"],
			"name_norm": norm_name(row["name"]),
			"ticker": (row.get("ticker") or "").upper() or None,
			"exchange": row.get("exchange"),
		}}, upsert=True) for row in rows))
	note_source("sec_tickers", url=SEC_TICKERS_URL, n_records=n)


def refresh_sec_cik_lookup():
	"""EDGAR's name->CIK index, former names included.

	Collapsed to `norm_name` so it can be joined against a company name that has
	been through the same normaliser. Names are kept as a list per key: a
	normalised name that maps to several CIKs is exactly the ambiguity the
	resolver refuses to guess through, and it can only see that if we keep them.
	"""
	path = os.path.join(DATA_DIR, "cik-lookup-data.txt")
	size = download(SEC_CIK_LOOKUP_URL, path)

	by_name = defaultdict(set)
	with open(path, "r", encoding="latin-1") as fh:
		for line in fh:
			# "SMITHFIELD FOODS INC:0000091388:" — the trailing colon is real.
			parts = line.rstrip("\n").rstrip(":").rsplit(":", 1)
			if len(parts) != 2 or not parts[1].isdigit():
				continue
			key = norm_name(parts[0])
			if key:
				by_name[key].add(parts[1].zfill(10))

	collection = get_db()["sec_cik_lookup"]
	collection.delete_many({})
	n = write_batches(collection, (
		UpdateOne({"_id": key}, {"$set": {"ciks": sorted(ciks)}}, upsert=True)
		for key, ciks in by_name.items()))
	note_source("sec_cik_lookup", url=SEC_CIK_LOOKUP_URL, n_records=n, bytes=size)


def gleif_publish():
	"""The current golden-copy publish: {file_type: {url, record_count}} + date."""
	payload = fetch_json(GLEIF_PUBLISHES_URL, params={"format": "json"})
	record = payload["data"][0]
	files = {}
	for kind in ("lei2", "rr"):
		csv_file = record[kind]["full_file"]["csv"]
		files[kind] = {"url": csv_file["url"], "n": csv_file["record_count"]}
	return record["publish_date"], files


def refresh_gleif():
	"""GLEIF golden copy: the legal-entity file, the relationship file, and the
	ISIN map.

	The ISIN map is the reason the third one is worth downloading: a GLEIF entity
	with zero ISINs is *positive evidence of not being listed*, which is a claim
	the release bodies can never support. Only LEIs that have at least one are
	stored — a miss means zero.
	"""
	publish_date, files = gleif_publish()
	db = get_db()

	path = os.path.join(DATA_DIR, "gleif-lei2.csv.zip")
	size = download(files["lei2"]["url"], path)
	collection = db["gleif_lei"]
	collection.delete_many({})
	n = write_batches(collection, (
		UpdateOne({"_id": row["LEI"]}, {"$set": {
			"name_norm": norm_name(row.get("Entity.LegalName")),
			"country": (row.get("Entity.HeadquartersAddress.Country")
			            or row.get("Entity.LegalAddress.Country") or None),
			"record": nonempty(row),
		}}, upsert=True)
		for row in zip_rows(path) if row.get("LEI")))
	note_source("gleif_lei", url=files["lei2"]["url"], publish_date=publish_date,
		n_records=n, bytes=size)

	path = os.path.join(DATA_DIR, "gleif-rr.csv.zip")
	size = download(files["rr"]["url"], path)
	collection = db["gleif_rr"]
	collection.delete_many({})
	n = write_batches(collection, (
		UpdateOne(
			{"_id": f"{row['Relationship.StartNode.NodeID']}"
			        f"-{row['Relationship.EndNode.NodeID']}"
			        f"-{row['Relationship.RelationshipType']}"},
			{"$set": {
				"start_lei": row["Relationship.StartNode.NodeID"],
				"end_lei": row["Relationship.EndNode.NodeID"],
				"type": row["Relationship.RelationshipType"],
				"status": row.get("Relationship.RelationshipStatus"),
			}}, upsert=True)
		for row in zip_rows(path)
		if row.get("Relationship.StartNode.NodeID") and row.get("Relationship.EndNode.NodeID")))
	note_source("gleif_rr", url=files["rr"]["url"], publish_date=publish_date, n_records=n,
		bytes=size)

	listing = fetch_json(GLEIF_ISIN_URL, host="mapping.gleif.org")["data"][0]
	url = listing["attributes"]["downloadLink"]
	path = os.path.join(DATA_DIR, "gleif-isin.zip")
	size = download(url, path, host="mapping.gleif.org")
	counts = Counter(row["LEI"] for row in zip_rows(path) if row.get("LEI"))
	collection = db["gleif_isin"]
	collection.delete_many({})
	n = write_batches(collection, (
		UpdateOne({"_id": lei}, {"$set": {"n_isins": count}}, upsert=True)
		for lei, count in counts.items()))
	note_source("gleif_isin", url=url, n_records=n, bytes=size,
		file=listing["attributes"]["fileName"])


def refresh_irs_bmf():
	"""IRS Business Master File — every tax-exempt organisation, by region."""
	collection = get_db()["irs_bmf"]
	collection.delete_many({})
	total = 0
	for url in IRS_BMF_URLS:
		path = os.path.join(DATA_DIR, os.path.basename(url))
		try:
			download(url, path)
		except EnrichError as e:
			print(f"  skipping {url}: {e}")
			continue
		with open(path, "r", encoding="latin-1", newline="") as fh:
			total += write_batches(collection, (
				UpdateOne({"_id": row["EIN"]}, {"$set": {
					"name_norm": norm_name(row.get("NAME")),
					"state": row.get("STATE") or None,
					"record": nonempty(row),
				}}, upsert=True)
				for row in csv.DictReader(fh) if row.get("EIN")))
	note_source("irs_bmf", url=", ".join(IRS_BMF_URLS), n_records=total)


def refresh_ror():
	"""ROR's Zenodo dump — research organisations, with a Wikidata crosswalk."""
	newest = fetch_json(ROR_RECORDS_URL, params={"q": "", "sort": "newest", "size": 1},
		host="zenodo.org")["hits"]["hits"][0]
	entry = next(f for f in newest["files"] if f["key"].endswith(".zip"))
	path = os.path.join(DATA_DIR, entry["key"])
	if not os.path.exists(path):
		download(entry["links"]["self"], path, host="zenodo.org")

	with zipfile.ZipFile(path) as archive:
		# Older dumps shipped v1 and v2 of the schema side by side and named the v2
		# file "..._schema_v2.json"; current ones carry v2 alone under the plain
		# name. Prefer the explicit one, fall back to the only one.
		name = next((n for n in archive.namelist() if n.endswith("schema_v2.json")),
			next(n for n in archive.namelist() if n.endswith(".json")))
		records = json.loads(archive.read(name))

	collection = get_db()["ror"]
	collection.delete_many({})
	n = write_batches(collection, (
		UpdateOne({"_id": record["id"]}, {"$set": {
			# Every name, not just the ror_display one: ROR calls the San Diego Zoo
			# "Zoological Society of San Diego" and files "San Diego Zoo Global" as
			# an alias, and a corpus sender answers to whichever it answers to.
			"name_norms": sorted({n for n in (norm_name(name.get("value"))
			                                  for name in record.get("names") or []) if n}),
			"wikidata_id": ror_external_id(record, "wikidata"),
			# ROR has a `domains` field of its own but leaves it empty on most
			# records, so the website link is where the domain actually lives.
			"domains": sorted({d for d in (
				[clean_domain(strip_scheme(link["value"]))
				 for link in record.get("links") or [] if link.get("type") == "website"]
				+ [clean_domain(d) for d in record.get("domains") or []]) if d}),
			"record": record,
		}}, upsert=True) for record in records))
	note_source("ror", url=entry["links"]["self"], n_records=n, file=entry["key"],
		schema_file=name)


def strip_scheme(url):
	return re.sub(r"^[a-z]+://", "", (url or "").strip(), flags=re.I).split("/")[0]


def ror_name(record):
	names = record.get("names") or []
	primary = next((n for n in names if "ror_display" in (n.get("types") or [])), None)
	return (primary or (names[0] if names else {})).get("value")


def ror_external_id(record, kind):
	for external in record.get("external_ids") or []:
		if external.get("type") == kind:
			values = external.get("all") or []
			return external.get("preferred") or (values[0] if values else None)
	return None


BULK_SOURCES = {
	"sec_tickers": (refresh_sec_tickers, ["sec_tickers"]),
	"sec_cik_lookup": (refresh_sec_cik_lookup, ["sec_cik_lookup"]),
	"ror": (refresh_ror, ["ror"]),
	"irs_bmf": (refresh_irs_bmf, ["irs_bmf"]),
	"gleif": (refresh_gleif, ["gleif_lei", "gleif_rr", "gleif_isin"]),
}


def refresh(force=False, only=None):
	"""Load the bulk sources into staging. Cheapest first, so each is verifiable
	before the 477 MB one runs.

	Without `force` a source whose staging is already populated is left alone —
	nothing here re-downloads on a schedule, because a newer golden copy does not
	invalidate a company that has already matched.
	"""
	db = get_db()
	for name, (loader, collections) in BULK_SOURCES.items():
		if only and name not in only:
			continue
		populated = all(db[c].estimated_document_count() for c in collections)
		if populated and not force:
			print(f"  {name}: already staged, skipping (--refresh to force)")
			continue
		print(f"Refreshing {name}")
		loader()


# --- I. Wikidata ------------------------------------------------------------
# Resolved by domain through `list=exturlusage`, which is an index lookup on the
# `externallinks` table and so answers in one call per company. It returns every
# item that *cites* the domain, though, not only the one it belongs to —
# "sandiegozoo.org" comes back with six items of which four merely reference it —
# so candidates are verified against P856 before any of them counts as a match.

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_HOST = "www.wikidata.org"

P_WEBSITE = "P856"
P_INCEPTION = "P571"
P_COUNTRY = "P17"
P_HQ = "P159"
P_INDUSTRY = "P452"
P_INSTANCE_OF = "P31"
P_PARENT = "P749"
P_OWNED_BY = "P127"
P_EMPLOYEES = "P1128"
P_EXCHANGE = "P414"
P_CIK = "P5531"
P_LEI = "P1278"
P_ISIN = "P946"
P_CRUNCHBASE = "P2088"
P_PERMID = "P3347"
P_ISO2 = "P297"
P_POINT_IN_TIME = "P585"
P_FORMATION_LOCATION = "P740"

# P31 values that say "this item is an organisation". Used as a tie-break when a
# domain resolves to several items, and as the guard that keeps a founder's
# personal item from being mistaken for the company.
ORG_CLASSES = frozenset("""
Q43229 Q4830453 Q783794 Q891723 Q6881511 Q167037 Q4830453 Q1058914 Q18388277
Q163740 Q157031 Q31855 Q3918 Q189004 Q875538 Q327333 Q2085381 Q16917 Q4287745
Q431289 Q5341295 Q748019 Q7278 Q79913 Q484652 Q15911314 Q1391145 Q11032
Q1616075 Q14350 Q1058914 Q170584 Q3623811 Q846759 Q2659904 Q192350
""".split())
HUMAN = "Q5"


def wikidata_api(params):
	"""Call the Wikidata API with the shared etiquette parameters.

	`maxlag` is what makes a bulk read polite: the API refuses rather than adding
	to replication lag, and we back off instead of pushing through.
	"""
	for attempt in range(4):
		payload = fetch_json(WIKIDATA_API, host=WIKIDATA_HOST, params={
			**params, "format": "json", "formatversion": "2", "maxlag": "5"})
		error = payload.get("error") or {}
		if error.get("code") != "maxlag":
			if error:
				raise EnrichError(f"wikidata: {error.get('code')}: {error.get('info')}")
			return payload
		time.sleep(5 * (attempt + 1))
	raise EnrichError("wikidata: maxlag did not clear")


def wd_claims(entity, prop):
	"""The `mainsnak` datavalues of `prop`, best rank first, novalue dropped."""
	statements = [s for s in (entity.get("claims") or {}).get(prop) or []
	              if s.get("rank") != "deprecated"
	              and (s.get("mainsnak") or {}).get("snaktype") == "value"]
	return [s for s in statements if s.get("rank") == "preferred"] or statements


def wd_ids(entity, prop):
	return [s["mainsnak"]["datavalue"]["value"]["id"] for s in wd_claims(entity, prop)]


def wd_strings(entity, prop):
	return [s["mainsnak"]["datavalue"]["value"] for s in wd_claims(entity, prop)
	        if isinstance(s["mainsnak"]["datavalue"]["value"], str)]


def wd_year(entity, prop):
	"""Earliest year stated for `prop`.

	Earliest, not best-ranked: an inception with several values is usually a
	company that was refounded or renamed, and the founding we want is the first.
	Wikidata times carry a leading sign and a precision; anything vaguer than a
	year is dropped rather than rounded into a false precision.
	"""
	years = []
	for statement in wd_claims(entity, prop):
		value = statement["mainsnak"]["datavalue"]["value"]
		if value.get("precision", 0) < 9:
			continue
		match = re.match(r"([+-])(\d{4})", value.get("time", ""))
		if match and match.group(1) == "+":
			years.append(int(match.group(2)))
	return min(years) if years else None


def wd_quantity(entity, prop):
	"""Most recent (amount, year) for a quantity property qualified by P585."""
	best = (None, None)
	for statement in wd_claims(entity, prop):
		amount = statement["mainsnak"]["datavalue"]["value"].get("amount")
		year = None
		for qualifier in (statement.get("qualifiers") or {}).get(P_POINT_IN_TIME, []):
			match = re.match(r"\+(\d{4})", (qualifier.get("datavalue") or {})
			                 .get("value", {}).get("time", ""))
			if match:
				year = int(match.group(1))
		if amount is not None and (best[0] is None or (year or 0) >= (best[1] or 0)):
			best = (int(float(amount.lstrip("+"))), year)
	return best


def wikidata_labels(qids):
	"""Label (and, for countries, ISO 3166-1 alpha-2) for each QID, cached.

	Referenced items repeat enormously across the corpus — a few hundred
	countries and industries stand behind a hundred thousand companies — so the
	cache turns what would be millions of lookups into a few thousand.
	"""
	if not qids:
		return {}
	collection = get_db()["wikidata_refs"]
	known = {doc["_id"]: doc for doc in collection.find({"_id": {"$in": list(qids)}})}
	missing = [q for q in qids if q not in known]

	for i in range(0, len(missing), 50):
		chunk = missing[i:i + 50]
		payload = wikidata_api({"action": "wbgetentities", "ids": "|".join(chunk),
			"props": "labels", "languages": "en"})
		ops = []
		for qid in chunk:
			entity = (payload.get("entities") or {}).get(qid) or {}
			doc = {"_id": qid,
			       "label": ((entity.get("labels") or {}).get("en") or {}).get("value")}
			known[qid] = doc
			ops.append(UpdateOne({"_id": qid}, {"$setOnInsert": doc}, upsert=True))
		if ops:
			collection.bulk_write(ops, ordered=False)
	return known


def country_iso2(qid):
	"""ISO 3166-1 alpha-2 for a country item, cached alongside its label.

	Fetched one claim at a time rather than with the whole entity: a country's
	full JSON runs to megabytes, and there are only ~200 of them to ever ask for.
	"""
	if not qid:
		return None
	collection = get_db()["wikidata_refs"]
	doc = collection.find_one({"_id": qid}) or {}
	if "iso2" in doc:
		return doc["iso2"]
	try:
		claims = wikidata_api({"action": "wbgetclaims", "entity": qid,
			"property": P_ISO2}).get("claims") or {}
		values = [c["mainsnak"]["datavalue"]["value"] for c in claims.get(P_ISO2, [])
		          if (c.get("mainsnak") or {}).get("snaktype") == "value"]
		iso2 = values[0] if values else None
	except EnrichError:
		iso2 = None
	collection.update_one({"_id": qid}, {"$set": {"iso2": iso2}}, upsert=True)
	return iso2


def wikidata_facts(entity):
	"""The handful of claims `rollup` actually reads, pre-walked.

	Parsing them here rather than in `rollup` keeps the claim-shape knowledge in
	one place, and means a rebuild of `companies` never has to walk 30 statement
	groups per company again.
	"""
	hq_ids = wd_ids(entity, P_HQ)
	country_ids = wd_ids(entity, P_COUNTRY)
	parent_ids = wd_ids(entity, P_PARENT) or wd_ids(entity, P_OWNED_BY)
	industry_ids = wd_ids(entity, P_INDUSTRY)
	instance_ids = wd_ids(entity, P_INSTANCE_OF)
	labels = wikidata_labels(list({*hq_ids, *country_ids, *parent_ids,
	                               *industry_ids, *instance_ids}))
	employees, employees_year = wd_quantity(entity, P_EMPLOYEES)

	def label(qid):
		return (labels.get(qid) or {}).get("label")

	return {
		"qid": entity["id"],
		"label": ((entity.get("labels") or {}).get("en") or {}).get("value"),
		"description": ((entity.get("descriptions") or {}).get("en") or {}).get("value"),
		"inception_year": wd_year(entity, P_INCEPTION),
		"country_qid": country_ids[0] if country_ids else None,
		"country": country_iso2(country_ids[0]) if country_ids else None,
		"hq_qid": hq_ids[0] if hq_ids else None,
		"hq_label": label(hq_ids[0]) if hq_ids else None,
		"formation_location": next((label(q) for q in wd_ids(entity, P_FORMATION_LOCATION)), None),
		"instance_of": [label(q) for q in instance_ids],
		"instance_of_qids": instance_ids,
		"industry": [label(q) for q in industry_ids],
		"parent_qid": parent_ids[0] if parent_ids else None,
		"parent_label": label(parent_ids[0]) if parent_ids else None,
		"employees": employees,
		"employees_year": employees_year,
		"exchanges": [label(q) for q in wd_ids(entity, P_EXCHANGE)],
		"cik": next(iter(wd_strings(entity, P_CIK)), None),
		"lei": next(iter(wd_strings(entity, P_LEI)), None),
		"isin": next(iter(wd_strings(entity, P_ISIN)), None),
		"crunchbase_id": next(iter(wd_strings(entity, P_CRUNCHBASE)), None),
		"permid": next(iter(wd_strings(entity, P_PERMID)), None),
		"websites": wd_strings(entity, P_WEBSITE),
	}


def trim_entity(entity):
	"""The entity minus statement references.

	References are provenance for the claim, not data about the company, and on a
	well-cited item they are the bulk of the JSON. Everything else — claims,
	qualifiers, labels, aliases, descriptions, sitelinks — is kept.
	"""
	for statements in (entity.get("claims") or {}).values():
		for statement in statements:
			statement.pop("references", None)
	return entity


# The URL an item cites, not just the fact that it cites one, is what separates
# a company from its own press coverage. A P856 value is a site root
# ("https://www.smithfieldfoods.com/"); a reference URL is a page
# ("https://science.sandiegozoo.org/oliver-ryder-phd"). Filtering on that before
# fetching anything cuts sandiegozoo.org from six candidates to two, and bounds
# the outliers — one corpus domain is cited by 499 items, and fetching all of
# them would cost ten API calls to reject nine and a half.
SITE_ROOT = re.compile(r"^[a-z]+://[^/]+/?$", re.I)
MAX_CANDIDATES = 50


def wikidata_candidates(domain):
	"""QIDs of items citing `domain` as a site root, subdomains included."""
	payload = wikidata_api({"action": "query", "list": "exturlusage",
		"euquery": domain, "eunamespace": "0", "eulimit": "500", "euprop": "title|url"})
	rows = (payload.get("query") or {}).get("exturlusage") or []

	roots, cited = [], []
	for row in rows:
		title, url = row.get("title"), row.get("url") or ""
		if not title or not title.startswith("Q"):
			continue
		if clean_domain(strip_scheme(url)) != domain:
			continue
		bucket = roots if SITE_ROOT.match(url) else cited
		if title not in bucket:
			bucket.append(title)
	# Path-cited items are considered only when nothing cites the bare root —
	# a company whose own site is only ever linked deep does exist, but once any
	# item names the root, the ones quoting article URLs are its press coverage.
	return (roots or cited)[:MAX_CANDIDATES], len(rows)


def wikidata_entities(qids):
	entities = {}
	for i in range(0, len(qids), 50):
		payload = wikidata_api({"action": "wbgetentities", "ids": "|".join(qids[i:i + 50]),
			"props": "info|labels|descriptions|aliases|claims|sitelinks"})
		entities.update(payload.get("entities") or {})
	return entities


def score_candidate(entity, domain):
	"""How well `entity` answers to `domain`; None when it does not at all.

	The test is P856 reduced to a registrable domain, which is what separates the
	company from the four items that merely cite its site. Ranking among the
	survivors prefers an organisation over anything else, and an exact host over
	a subdomain — "sandiegozoo.org" holds both the zoo and its parent society.
	"""
	instance_ids = set(wd_ids(entity, P_INSTANCE_OF))
	if HUMAN in instance_ids:
		return None
	hosts = [strip_scheme(url) for url in wd_strings(entity, P_WEBSITE)]
	if not any(clean_domain(host) == domain for host in hosts):
		return None
	exact = any(re.sub(r"^www\.", "", host) == domain for host in hosts)
	return (bool(instance_ids & ORG_CLASSES), exact, len(entity.get("claims") or {}))


def resolve_wikidata(company):
	"""Match `company` to a Wikidata item. Returns a block, matched or not."""
	domain = company.get("domain")
	if not domain:
		return {"matched": False, "reason": "no_domain"}

	qids, n_citing = wikidata_candidates(domain)
	if not qids:
		return {"matched": False, "reason": "no_candidates", "n_citing": n_citing}

	entities = wikidata_entities(qids)
	scored = []
	for qid in qids:
		entity = entities.get(qid)
		if not entity or "claims" not in entity:
			continue
		score = score_candidate(entity, domain)
		if score is not None:
			scored.append((score, qid, entity))
	if not scored:
		return {"matched": False, "reason": "no_p856_match",
		        "n_candidates": len(qids), "n_citing": n_citing}

	scored.sort(key=lambda row: row[0], reverse=True)
	_, qid, entity = scored[0]
	return {
		"matched": True,
		"qid": qid,
		"matched_by": "p856_domain",
		"confidence": "high" if len(scored) == 1 else "medium",
		"n_candidates": len(qids),
		"n_citing": n_citing,
		"facts": wikidata_facts(entity),
		"entity": trim_entity(entity),
	}


# --- II. SEC ----------------------------------------------------------------
# Four artefacts behind one CIK. The ticker/exchange list and the name->CIK index
# are bulk and already staged; submissions (identity, filing history) and
# companyfacts (XBRL) are per-CIK, which is the cheaper way round here — only
# ~10k senders are registrants, so ~10k requests beats 3 GB of bulk zip whose
# 99% we would discard.

SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_COMPANYFACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

# Annual forms only. A quarterly value would make `revenue` mean something
# different for a 10-Q filer than for a 10-K one, which is worse than a gap.
ANNUAL_FORMS = ("10-K", "20-F", "40-F", "10-KT", "11-K")

# What a size covariate needs, in the order a concept should be preferred when a
# filer reports several: the post-ASC-606 revenue tags first, the legacy ones
# after. Each resolves to at most one (value, period) pair per company.
FACT_CONCEPTS = {
	"revenue": ("us-gaap", ["RevenueFromContractWithCustomerExcludingAssessedTax",
	                        "RevenueFromContractWithCustomerIncludingAssessedTax",
	                        "Revenues", "SalesRevenueNet", "SalesRevenueGoodsNet"]),
	"assets": ("us-gaap", ["Assets"]),
	"liabilities": ("us-gaap", ["Liabilities"]),
	"equity": ("us-gaap", ["StockholdersEquity",
	                       "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
	"net_income": ("us-gaap", ["NetIncomeLoss", "ProfitLoss"]),
	"operating_income": ("us-gaap", ["OperatingIncomeLoss"]),
	"gross_profit": ("us-gaap", ["GrossProfit"]),
	"cash": ("us-gaap", ["CashAndCashEquivalentsAtCarryingValue"]),
	"rnd_expense": ("us-gaap", ["ResearchAndDevelopmentExpense"]),
	"eps_basic": ("us-gaap", ["EarningsPerShareBasic"]),
	"shares_outstanding": ("dei", ["EntityCommonStockSharesOutstanding"]),
	"public_float": ("dei", ["EntityPublicFloat"]),
}


def latest_fact(facts, taxonomy, concepts):
	"""The most recent annual observation of the first concept that has one.

	`companyfacts` reports every value a filer ever tagged, restated ones
	included, so "most recent" is by period end and then by filing date — the
	last word on the latest period, not merely the last row.
	"""
	available = (facts.get(taxonomy) or {})
	for concept in concepts:
		units = (available.get(concept) or {}).get("units") or {}
		for unit, observations in units.items():
			annual = [o for o in observations if o.get("form") in ANNUAL_FORMS and o.get("end")]
			if not annual:
				continue
			best = max(annual, key=lambda o: (o["end"], o.get("filed") or ""))
			return {
				"concept": concept, "unit": unit, "value": best.get("val"),
				"start": best.get("start"), "end": best["end"],
				"fy": best.get("fy"), "fp": best.get("fp"),
				"form": best.get("form"), "filed": best.get("filed"),
			}
	return None


def distil_companyfacts(payload):
	facts = payload.get("facts") or {}
	out = {"entityName": payload.get("entityName"),
	       "n_concepts": {tax: len(concepts) for tax, concepts in facts.items()}}
	for field, (taxonomy, concepts) in FACT_CONCEPTS.items():
		found = latest_fact(facts, taxonomy, concepts)
		if found:
			out[field] = found
	return out


def distil_submissions(payload):
	"""The identity record, plus the two things the filing list is good for: how
	long this filer has existed, and what it actually files."""
	recent = (payload.get("filings") or {}).get("recent") or {}
	dates = [d for d in recent.get("filingDate") or [] if d]
	# `filings.files` pages the history past the most recent 1000, and each page
	# advertises the range it covers — so the true first filing is readable
	# without fetching a single extra page.
	older = [f.get("filingFrom") for f in (payload.get("filings") or {}).get("files") or []
	         if f.get("filingFrom")]
	addresses = payload.get("addresses") or {}
	return {
		"cik": str(payload.get("cik") or "").zfill(10),
		"name": payload.get("name"),
		"entityType": payload.get("entityType"),
		"sic": payload.get("sic") or None,
		"sicDescription": payload.get("sicDescription") or None,
		"ownerOrg": payload.get("ownerOrg") or None,
		"ein": (payload.get("ein") or "").strip() or None,
		"lei": (payload.get("lei") or None),
		"tickers": payload.get("tickers") or [],
		"exchanges": payload.get("exchanges") or [],
		"stateOfIncorporation": payload.get("stateOfIncorporation") or None,
		"fiscalYearEnd": payload.get("fiscalYearEnd") or None,
		"category": payload.get("category") or None,
		"phone": (payload.get("phone") or "").strip() or None,
		"formerNames": payload.get("formerNames") or [],
		"business_address": addresses.get("business") or None,
		"mailing_address": addresses.get("mailing") or None,
		"first_filing_date": min(dates + older) if (dates or older) else None,
		"last_filing_date": max(dates) if dates else None,
		"n_recent_filings": len(recent.get("form") or []),
		"form_counts": dict(Counter(recent.get("form") or []).most_common(10)),
	}


# Form D's primary document is filed under a versioned namespace ("X0707" and
# later), so the parser matches on the local element name rather than pinning a
# schema version that will move under it.
def xml_local(node):
	return node.tag.rsplit("}", 1)[-1]


def xml_find(root, *path):
	"""Text at `path` of nested local element names, or None.

	Nested rather than flat because Form D reuses generic leaf names inside
	several containers — `yearOfInc/value` and `dateOfFirstSale/value` are both
	just "value", and a flat search would return whichever the filer put first.
	"""
	node = root
	for name in path:
		node = next((child for child in node.iter() if xml_local(child) == name), None)
		if node is None:
			return None
	return (node.text or "").strip() or None


def parse_form_d(xml_bytes):
	"""The declared facts a Form D carries: age, jurisdiction, revenue band, raise.

	This is the only place a private company states its own founding year and a
	revenue bucket to a regulator, which is what makes it worth a request per
	filing for senders that will never appear in companyfacts.
	"""
	from xml.etree import ElementTree

	root = ElementTree.fromstring(xml_bytes)
	people = []
	for node in root.iter():
		if xml_local(node) != "relatedPersonInfo":
			continue
		name = " ".join(filter(None, (xml_find(node, "relatedPersonName", part)
			for part in ("firstName", "middleName", "lastName"))))
		people.append({
			"name": name or None,
			"roles": [(child.text or "").strip() for child in node.iter()
			          if xml_local(child) == "relationship" and (child.text or "").strip()],
		})
	return {
		"entity_name": xml_find(root, "primaryIssuer", "entityName"),
		"entity_type": xml_find(root, "primaryIssuer", "entityType"),
		"year_of_inc": xml_find(root, "yearOfInc", "value"),
		"within_five_years": xml_find(root, "yearOfInc", "withinFiveYears"),
		"jurisdiction_of_inc": xml_find(root, "jurisdictionOfInc"),
		"issuer_city": xml_find(root, "issuerAddress", "city"),
		"issuer_state": xml_find(root, "issuerAddress", "stateOrCountry"),
		"industry_group": xml_find(root, "industryGroupType"),
		"revenue_range": xml_find(root, "revenueRange"),
		"total_offering_amount": xml_find(root, "totalOfferingAmount"),
		"total_amount_sold": xml_find(root, "totalAmountSold"),
		"total_remaining": xml_find(root, "totalRemaining"),
		"minimum_investment": xml_find(root, "minimumInvestmentAccepted"),
		"n_already_invested": xml_find(root, "totalNumberAlreadyInvested"),
		"has_non_accredited": xml_find(root, "hasNonAccreditedInvestors"),
		"date_of_first_sale": xml_find(root, "dateOfFirstSale", "value"),
		"related_persons": people[:20],
	}


# Enough to date the funding history without turning a well-funded startup into
# a dozen requests; the filing list itself keeps the full timeline either way.
MAX_FORM_D = 5


def fetch_form_d(cik, recent):
	"""Every Form D in `recent`, most recent first, parsed up to MAX_FORM_D."""
	forms = recent.get("form") or []
	filings = [{
		"form": forms[i],
		"filed": (recent.get("filingDate") or [])[i],
		"accession": (recent.get("accessionNumber") or [])[i],
		"document": (recent.get("primaryDocument") or [])[i],
	} for i in range(len(forms)) if forms[i] in ("D", "D/A")]

	for filing in filings[:MAX_FORM_D]:
		# `primaryDocument` names the XSL-rendered view ("xslFormDX01/primary_doc.xml");
		# the machine-readable XML is that same basename at the accession root.
		document = os.path.basename(filing["document"] or "") or "primary_doc.xml"
		url = SEC_ARCHIVE.format(cik=int(cik), document=document,
			accession=(filing["accession"] or "").replace("-", ""))
		try:
			filing["data"] = parse_form_d(fetch(url, host="www.sec.gov").content)
		except Exception as e:  # one malformed filing is not a failed company
			filing["error"] = str(e)[:200]
	return filings


def match_cik(company, wikidata):
	"""(cik, matched_by, evidence) for `company`; cik is None when it is unknown.

	Wikidata's P5531 first because it is a curated crosswalk; then the ticker,
	but only when the filer it names answers to the same name; then the name
	alone, and only when it is unambiguous.

	The corroboration on the ticker path is not belt-and-braces. `companies.ticker`
	is the rollup's concentration guess, and for a subsidiary that guess is the
	*parent's* symbol — Toyota Motor North America prints NYSE:TM in 64% of its
	releases. Following it blindly would look up Toyota Motor Corp's CIK and
	launder a known-bad inference into an authoritative identifier. So a
	mismatch is not merely rejected: it is recorded, because "this company's
	modal ticker belongs to a differently-named filer" is itself the evidence
	that settles the subsidiary case in `company.py:external_public`.
	"""
	stated = ((wikidata or {}).get("facts") or {}).get("cik")
	if stated and stated.strip().isdigit():
		return stated.strip().zfill(10), "wikidata_p5531", {}

	db = get_db()
	key = company.get("name_norm") or norm_name(company.get("name"))
	evidence = {}
	ticker = (company.get("ticker") or {}).get("symbol")
	if ticker:
		# Distinct CIKs, because one filer answering to two symbols is one
		# company, while two filers sharing a symbol across exchanges is not.
		rows = list(db["sec_tickers"].find({"ticker": ticker.upper()}))
		ciks = {row["cik"] for row in rows}
		if len(ciks) == 1:
			if key and key in {row["name_norm"] for row in rows}:
				return rows[0]["cik"], "ticker_and_name", {}
			evidence["ticker_conflict"] = {
				"ticker": ticker.upper(), "cik": rows[0]["cik"], "filer_name": rows[0]["name"]}

	if key:
		ciks = db["sec_tickers"].distinct("cik", {"name_norm": key})
		if len(ciks) == 1:
			return ciks[0], "name_sec_tickers", evidence
		entry = db["sec_cik_lookup"].find_one({"_id": key})
		if entry and len(entry["ciks"]) == 1:
			return entry["ciks"][0], "name_cik_lookup", evidence
		if entry:
			return None, "name_ambiguous", evidence
		return None, "name_no_match", evidence
	return None, "no_key", evidence


def resolve_sec(company, wikidata):
	cik, how, evidence = match_cik(company, wikidata)
	if not cik:
		return {"matched": False, "reason": how, **evidence}

	block = {"matched": True, "cik": cik, "matched_by": how, **evidence,
	         "confidence": {"wikidata_p5531": "high",
	                        "ticker_and_name": "high"}.get(how, "medium")}

	listings = list(get_db()["sec_tickers"].find({"cik": cik}))
	if listings:
		block["tickers_exchange"] = {
			"name": listings[0]["name"],
			"ticker": listings[0]["ticker"],
			"exchange": listings[0]["exchange"],
			"listings": [{"ticker": row["ticker"], "exchange": row["exchange"]}
			             for row in listings],
		}

	try:
		payload = fetch_json(SEC_SUBMISSIONS.format(cik=cik), host="data.sec.gov")
	except EnrichError as e:
		if e.status_code not in ABSENT_STATUS:
			raise
		return {"matched": False, "reason": f"submissions_{e.status_code}", "cik": cik,
		        **evidence}
	block["submissions"] = distil_submissions(payload)
	block["form_d"] = fetch_form_d(cik, (payload.get("filings") or {}).get("recent") or {})

	try:
		block["facts"] = distil_companyfacts(
			fetch_json(SEC_COMPANYFACTS.format(cik=cik), host="data.sec.gov"))
	except EnrichError as e:
		if e.status_code not in ABSENT_STATUS:
			raise
		block["facts"] = None  # no XBRL: a non-filer, or a filer predating it
	return block


# --- III. GLEIF -------------------------------------------------------------
# The LEI is the identifier a company gets from a regulator rather than from a
# catalogue, so it carries two things nothing else here does reliably: an
# incorporation date, and a jurisdiction distinct from the headquarters address.
# The ISIN count is the third: an entity GLEIF knows with zero ISINs is positive
# evidence of *not* being listed, which no press release can ever supply.

GLEIF_RECORD_URL = "https://api.gleif.org/api/v1/lei-records/{lei}"

# A GLEIF record for a US company is filed under whichever state chartered it,
# so a name block has to be cut by country, not by state, to hold together.
def gleif_country(company, wikidata, sec):
	"""Best available country for `company`, or None.

	Three sources because no one of them covers much: Wikidata knows a fifth of
	the corpus, SEC's business address only registrants, and the dateline only
	names a region at all about half the time.
	"""
	facts = (wikidata or {}).get("facts") or {}
	if facts.get("country"):
		return facts["country"]
	business = ((sec or {}).get("submissions") or {}).get("business_address") or {}
	if us_state(business.get("stateOrCountry")):
		return "US"
	return "US" if us_state((company.get("hq") or {}).get("region")) else None


def resolve_gleif(company, wikidata, sec):
	stated = ((wikidata or {}).get("facts") or {}).get("lei")
	db = get_db()

	if stated:
		record = db["gleif_lei"].find_one({"_id": stated.strip().upper()})
		if record:
			return gleif_block(record, "wikidata_p1278", "high")

	key = company.get("name_norm") or norm_name(company.get("name"))
	if not key:
		return {"matched": False, "reason": "no_key"}

	# Two entities of the same name in the same country is exactly the case we
	# cannot resolve, so it is recorded as ambiguous rather than guessed at.
	country = gleif_country(company, wikidata, sec)
	if country:
		hits = list(db["gleif_lei"].find({"name_norm": key, "country": country}).limit(3))
		if len(hits) == 1:
			return gleif_block(hits[0], "name_country", "medium")
		if hits:
			return {"matched": False, "reason": "name_country_ambiguous",
			        "n_candidates": len(hits)}

	# No country to cut by — which is the common case, since most senders are in
	# no registry and print no region in their dateline. The block widens to all
	# 3.4M entities worldwide, and is still only accepted when exactly one comes
	# back. Widening this way trades recall for nothing: a same-named entity in
	# another country makes the block ambiguous and the match is refused, so it
	# can lose a match but not invent one.
	hits = list(db["gleif_lei"].find({"name_norm": key}).limit(3))
	if len(hits) == 1:
		return gleif_block(hits[0], "name" if not country else "name_any_country", "low")
	return {"matched": False, "reason": "name_ambiguous" if hits else "name_no_match",
	        "n_candidates": len(hits)}


def gleif_block(record, matched_by, confidence):
	lei = record["_id"]
	db = get_db()
	isins = db["gleif_isin"].find_one({"_id": lei}) or {}
	# "Is ultimately consolidated by" is the accounting parent; the direct one is
	# what a corporate-structure control wants, so it is preferred.
	parent = db["gleif_rr"].find_one(
		{"start_lei": lei, "type": "IS_DIRECTLY_CONSOLIDATED_BY", "status": "ACTIVE"}) or \
		db["gleif_rr"].find_one(
			{"start_lei": lei, "type": "IS_ULTIMATELY_CONSOLIDATED_BY", "status": "ACTIVE"})
	parent_lei = parent["end_lei"] if parent else None
	parent_record = db["gleif_lei"].find_one({"_id": parent_lei}) if parent_lei else None

	return {
		"matched": True,
		"lei": lei,
		"matched_by": matched_by,
		"confidence": confidence,
		"n_isins": isins.get("n_isins", 0),
		"parent_lei": parent_lei,
		"parent_type": parent["type"] if parent else None,
		"parent_name": (parent_record or {}).get("record", {}).get("Entity.LegalName"),
		"record": record["record"],
	}


# --- IV. RDAP ---------------------------------------------------------------
# The registration date of a company's own domain is the one age signal that
# exists for the senders none of the registries know: no Wikidata item, no CIK,
# no LEI, but 69,059 of 98,860 have a domain.

RDAP_BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
_rdap_services = None


def rdap_base(domain):
	"""The RDAP service for a domain's TLD, from IANA's bootstrap file.

	Bootstrapping rather than going through a redirector keeps one hop out of
	69k lookups and, more usefully, names the host we are hitting so the rate
	limiter can pace per registry — `.com` and `.net` are one Verisign endpoint
	carrying most of the corpus.
	"""
	global _rdap_services
	if _rdap_services is None:
		_rdap_services = {}
		for tlds, urls in fetch_json(RDAP_BOOTSTRAP_URL, host="data.iana.org")["services"]:
			for tld in tlds:
				_rdap_services[tld.lower()] = urls[0].rstrip("/")
	return _rdap_services.get(domain.rsplit(".", 1)[-1].lower())


def resolve_rdap(company):
	domain = company.get("domain")
	if not domain:
		return {"matched": False, "reason": "no_domain"}
	base = rdap_base(domain)
	if not base:
		return {"matched": False, "reason": "no_rdap_service", "domain": domain}

	url = f"{base}/domain/{domain}"
	try:
		record = fetch_json(url, host=requests.utils.urlparse(base).netloc)
	except EnrichError as e:
		if e.status_code in ABSENT_STATUS:
			return {"matched": False, "reason": f"rdap_{e.status_code}", "domain": domain}
		raise

	events = {e.get("eventAction"): e.get("eventDate")
	          for e in record.get("events") or [] if e.get("eventAction")}
	registrar = next((entity for entity in record.get("entities") or []
	                  if "registrar" in (entity.get("roles") or [])), None)
	return {
		"matched": True,
		"domain": domain,
		"matched_by": "domain",
		"confidence": "high",
		"registration_date": events.get("registration"),
		"expiration_date": events.get("expiration"),
		"last_changed": events.get("last changed"),
		"registrar": vcard_name(registrar),
		"status": record.get("status") or [],
		"nameservers": [ns.get("ldhName") for ns in record.get("nameservers") or []],
		"secure_dns": (record.get("secureDNS") or {}).get("delegationSigned"),
		"record": record,
	}


def vcard_name(entity):
	"""The `fn` line of a jCard, which is where RDAP hides the registrar name."""
	for item in ((entity or {}).get("vcardArray") or [None, []])[1]:
		if item and item[0] == "fn":
			return item[3]
	return None


# --- V. IRS BMF -------------------------------------------------------------
# The Business Master File is the only source here that states a non-profit's
# actual revenue, and RULING — the month its exemption was granted — dates the
# organisation as well as an incorporation record would.

def resolve_irs(company, sec):
	db = get_db()
	ein = ((sec or {}).get("submissions") or {}).get("ein")
	if ein:
		record = db["irs_bmf"].find_one({"_id": re.sub(r"\D", "", ein).zfill(9)})
		if record:
			return irs_block(record, "sec_ein", "high")

	key = company.get("name_norm") or norm_name(company.get("name"))
	if not key:
		return {"matched": False, "reason": "no_key"}

	# The dateline names a state less than half the time, and the rollup's `hq`
	# inherits that gap — the San Diego Zoo Wildlife Alliance has no region at
	# all. So the block widens rather than giving up: name + state first, then
	# name alone across all 1.96M organisations. Widening is safe here only
	# because the block still has to come back with exactly one row.
	state = us_state((company.get("hq") or {}).get("region"))
	if state:
		hits = list(db["irs_bmf"].find({"name_norm": key, "state": state}).limit(3))
		if len(hits) == 1:
			return irs_block(hits[0], "name_state", "medium")
		if hits:
			return {"matched": False, "reason": "name_state_ambiguous", "n_candidates": len(hits)}

	hits = list(db["irs_bmf"].find({"name_norm": key}).limit(3))
	if len(hits) == 1:
		return irs_block(hits[0], "name", "low")
	return {"matched": False, "reason": "name_ambiguous" if hits else "name_no_match",
	        "n_candidates": len(hits)}


def irs_block(record, matched_by, confidence):
	row = record["record"]
	ruling = (row.get("RULING") or "").strip()
	return {
		"matched": True,
		"ein": record["_id"],
		"matched_by": matched_by,
		"confidence": confidence,
		# YYYYMM, and "000000" for the organisations the IRS never dated.
		"ruling_year": int(ruling[:4]) if len(ruling) >= 4 and ruling[:4] != "0000" else None,
		"ntee_code": row.get("NTEE_CD") or None,
		"subsection": row.get("SUBSECTION") or None,
		"revenue_amt": to_int(row.get("REVENUE_AMT")),
		"income_amt": to_int(row.get("INCOME_AMT")),
		"asset_amt": to_int(row.get("ASSET_AMT")),
		"record": row,
	}


# --- VI. ROR ----------------------------------------------------------------
# Research organisations — universities, institutes, museums, the funded side of
# a zoo. ROR carries its own Wikidata crosswalk, so a company matched in I is
# matched here for free and exactly.

def resolve_ror(company, wikidata):
	db = get_db()
	qid = (wikidata or {}).get("qid")
	if qid:
		record = db["ror"].find_one({"wikidata_id": qid})
		if record:
			return ror_block(record, "wikidata_id", "high")

	domain = company.get("domain")
	if domain:
		hits = list(db["ror"].find({"domains": domain}).limit(3))
		if len(hits) == 1:
			return ror_block(hits[0], "domain", "high")

	key = company.get("name_norm") or norm_name(company.get("name"))
	if key:
		hits = list(db["ror"].find({"name_norms": key}).limit(3))
		if len(hits) == 1:
			return ror_block(hits[0], "name", "medium")
		if hits:
			return {"matched": False, "reason": "name_ambiguous", "n_candidates": len(hits)}
	return {"matched": False, "reason": "no_match"}


def ror_block(record, matched_by, confidence):
	row = record["record"]
	location = (row.get("locations") or [{}])[0].get("geonames_details") or {}
	return {
		"matched": True,
		"id": record["_id"],
		"matched_by": matched_by,
		"confidence": confidence,
		"name": ror_name(row),
		"established": row.get("established"),
		"types": row.get("types") or [],
		"status": row.get("status"),
		"country": location.get("country_code"),
		"city": location.get("name"),
		"region": location.get("country_subdivision_code"),
		"record": row,
	}


# --- the resolve pass -------------------------------------------------------
# Fixed order, because the cascade is a dependency chain: Wikidata hands SEC a
# CIK and GLEIF a LEI, and SEC hands the IRS an EIN.

RESOLVE_ORDER = ("wikidata", "sec", "gleif", "rdap", "irs", "ror")

COMPANY_FIELDS = {"name": 1, "name_norm": 1, "domain": 1, "hq": 1, "ticker": 1,
                  "n_releases": 1, "org_type": 1}


def batched(iterable, size):
	"""Chunks of `size`, the last one short. Written out rather than inlined into
	the pass because getting the tail chunk wrong silently drops the remainder,
	which is exactly what a run smaller than one batch consists of."""
	batch = []
	for item in iterable:
		batch.append(item)
		if len(batch) >= size:
			yield batch
			batch = []
	if batch:
		yield batch


def due_sources(enrichment, only=None, retry_misses=False):
	"""Which sources still owe this company an answer.

	Version, not time. A block that answered stands until its resolver version
	moves, so a fresh bulk download never re-crawls a company that has matched.
	An `error` block is the exception — that is an unanswered question, not a
	recorded "no".
	"""
	due = []
	for source in RESOLVE_ORDER:
		if only and source not in only:
			continue
		block = (enrichment or {}).get(source)
		if (not block
				or block.get("resolver_version") != RESOLVER_VERSIONS[source]
				or block.get("error")
				or (retry_misses and not block.get("matched"))):
			due.append(source)
	return due


def resolve_company(company, enrichment, due):
	"""Run the due resolvers for one company; returns only the blocks that ran.

	Each source is wrapped on its own: a GLEIF timeout must not cost the RDAP
	record that came back fine, and the failure is recorded on its own block so
	the next pass retries exactly it.
	"""
	blocks = dict(enrichment or {})
	written = {}
	for source in RESOLVE_ORDER:
		if source not in due:
			continue
		try:
			if source == "wikidata":
				block = resolve_wikidata(company)
			elif source == "sec":
				block = resolve_sec(company, blocks.get("wikidata"))
			elif source == "gleif":
				block = resolve_gleif(company, blocks.get("wikidata"), blocks.get("sec"))
			elif source == "rdap":
				block = resolve_rdap(company)
			elif source == "irs":
				block = resolve_irs(company, blocks.get("sec"))
			else:
				block = resolve_ror(company, blocks.get("wikidata"))
		except Exception as e:
			# Layered onto whatever this source last returned rather than
			# replacing it. A re-resolve after a version bump that then hits a
			# rate limit must not cost the match it already had; `due_sources`
			# re-queues on `error`, so the retry still happens either way.
			previous = blocks.get(source) or {}
			block = {**previous, "error": f"{type(e).__name__}: {e}"[:300],
			         "attempts": (previous.get("attempts") or 0) + 1}
		block["fetched_at"] = now()
		block["resolver_version"] = RESOLVER_VERSIONS[source]
		blocks[source] = written[source] = block
	return written, [s for s in RESOLVE_ORDER if (blocks.get(s) or {}).get("matched")]


def resolve(limit=None, only=None, company_ids=None, retry_misses=False, batch_size=200):
	"""Resolve companies against the external sources, biggest senders first.

	Descending `n_releases` is what makes the pass interruptible: the head of the
	corpus carries most of the releases, so stopping after any number of
	companies still leaves the enrichment where it matters most.
	"""
	db = get_db()
	companies = db["companies"]
	enrichment = db["company_enrichment"]

	query = {"_id": {"$in": company_ids}} if company_ids else {}
	# Read to exhaustion up front rather than iterated lazily. Each chunk below
	# spends minutes in rate-limited HTTP before asking for the next one, and a
	# find cursor left idle that long is reaped by the server — which surfaces
	# as CursorNotFound (code 43) partway through an otherwise healthy pass.
	ids = [c["_id"] for c in companies.find(query, {"_id": 1}).sort("n_releases", -1)]
	print(f"Resolving up to {limit or len(ids)} of {len(ids)} companies "
	      f"({', '.join(only or RESOLVE_ORDER)})")

	done = seen = skipped = 0
	for chunk_ids in batched(ids, batch_size):
		# Re-read by id, restoring the descending order that `$in` does not
		# preserve; a company deleted since the id scan just drops out.
		found = {c["_id"]: c for c in
		         companies.find({"_id": {"$in": chunk_ids}}, COMPANY_FIELDS)}
		chunk = [found[i] for i in chunk_ids if i in found]
		# One lookup per chunk rather than per company: most of a steady-state
		# pass is companies that are already current, and this is what makes
		# deciding that cost nothing. The two big blobs are excluded because
		# nothing here reads them — the due check wants version strings, and the
		# dependent resolvers want the crosswalk ids in `wikidata.facts`.
		existing = {d["_id"]: d for d in enrichment.find(
			{"_id": {"$in": [c["_id"] for c in chunk]}},
			{"wikidata.entity": 0, "rdap.record": 0})}
		for company in chunk:
			seen += 1
			due = due_sources(existing.get(company["_id"]), only, retry_misses)
			if not due:
				skipped += 1
				continue
			written, matched = resolve_company(company, existing.get(company["_id"]), due)
			enrichment.update_one({"_id": company["_id"]}, {"$set": {
				**written,
				"domain": company.get("domain"),
				"name": company.get("name"),
				"name_norm": company.get("name_norm"),
				"matched_sources": matched,
				"enriched_at": now(),
				"enrichment_version": ENRICHMENT_VERSION,
			}}, upsert=True)
			done += 1
			if done % 25 == 0:
				print(f"  {done} resolved, {skipped} already current, {seen} scanned")
			if limit is not None and done >= limit:
				break
		if limit is not None and done >= limit:
			break
	print(f"Resolved {done}, skipped {skipped} already current, scanned {seen}")
	return done


def stats():
	"""Per-source match rate and how the matches were made."""
	enrichment = get_db()["company_enrichment"]
	total = enrichment.estimated_document_count()
	print(f"{total} companies enriched\n")
	print(f"{'source':10} {'matched':>9} {'rate':>7}   matched_by / miss reasons")
	for source in RESOLVE_ORDER:
		matched = enrichment.count_documents({f"{source}.matched": True})
		how = Counter()
		for doc in enrichment.find({f"{source}": {"$exists": True}},
		                           {f"{source}.matched_by": 1, f"{source}.reason": 1}):
			block = doc[source]
			how[block.get("matched_by") or f"-{block.get('reason', 'error')}"] += 1
		mix = ", ".join(f"{k} {v}" for k, v in how.most_common(5))
		print(f"{source:10} {matched:>9} {matched / max(total, 1):>6.1%}   {mix}")


IDLE_SLEEP_SECONDS = 6 * 60 * 60


if __name__ == "__main__":
	parser = argparse.ArgumentParser(
		description="Resolve companies against external registries")
	parser.add_argument("--refresh", action="store_true",
		help="re-download the bulk sources into staging, even if already staged")
	parser.add_argument("--no-resolve", action="store_true",
		help="stop after the bulk refresh")
	parser.add_argument("--only", type=str,
		help=f"comma-separated subset of {','.join(RESOLVE_ORDER)} (or of the bulk "
		     f"source names, with --refresh)")
	parser.add_argument("--companies", type=str,
		help="comma-separated uploader_links, instead of the whole collection")
	parser.add_argument("--limit", type=int, help="stop after this many companies")
	parser.add_argument("--retry-misses", action="store_true",
		help="re-ask sources that previously recorded a non-match")
	parser.add_argument("--stats", action="store_true",
		help="print per-source match rates and exit")
	parser.add_argument("--loop", action="store_true",
		help="run forever with a sleep between passes")
	parser.add_argument("--idle-sleep", type=int, default=IDLE_SLEEP_SECONDS,
		help="seconds to sleep between passes when in --loop mode")
	args = parser.parse_args()

	ensure_indexes()
	if not CONTACT:
		print("WARNING: ENRICH_CONTACT is unset. SEC refuses requests whose "
		      "User-Agent names no contact, so every SEC lookup will fail; set it "
		      "to an address you are willing to be contacted at.")
	# `--only` names either resolver sources or bulk sources, so it is checked
	# against both and then split — otherwise `--refresh --only gleif_lei` would
	# refresh correctly and then silently resolve nothing at all.
	only = set(args.only.split(",")) if args.only else None
	if only:
		unknown = only - set(RESOLVE_ORDER) - set(BULK_SOURCES)
		if unknown:
			parser.error(f"unknown source(s): {', '.join(sorted(unknown))}")

	if args.stats:
		stats()
		sys.exit(0)

	sources = only & set(RESOLVE_ORDER) if only else None
	refresh_first = args.refresh
	while True:
		# Staging is loaded on the first pass and left alone after it: the bulk
		# sources back the *matching*, and a company that has already matched is
		# not re-resolved just because a newer golden copy exists. `--refresh` is
		# honoured once and then dropped, so a looping service does not re-fetch
		# 600 MB every time it wakes up.
		refresh(force=refresh_first, only=only & set(BULK_SOURCES) if only else None)
		refresh_first = False
		if not args.no_resolve and (sources or not only):
			resolve(limit=args.limit, only=sources,
				company_ids=args.companies.split(",") if args.companies else None,
				retry_misses=args.retry_misses)
		if not args.loop:
			break
		print(f"Idle; sleeping {args.idle_sleep}s")
		time.sleep(args.idle_sleep)
