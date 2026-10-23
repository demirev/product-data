"""Query parsing and the reads behind every endpoint.

The whole app reads three collections, all built by `readmodel.py` and all
denormalized enough that no request needs a join. What is left is turning query
strings into Mongo filters — and doing it strictly, because a researcher who
mistypes a filter should be told, not silently served the unfiltered set.
"""
import re
from datetime import datetime, timedelta, timezone

from pymongo import ASCENDING, DESCENDING


PER_PAGE_DEFAULT = 24
PER_PAGE_MAX = 200
# CSV is streamed, so the cap is not about memory — it is about not handing
# somebody a 400 MB download because they forgot to filter.
CSV_MAX_ROWS = 50_000

RELEASE_SORTS = {
	"-published_at": [("published_at", DESCENDING), ("_id", ASCENDING)],
	"published_at": [("published_at", ASCENDING), ("_id", ASCENDING)],
	"-confidence": [("confidence", DESCENDING), ("_id", ASCENDING)],
}
COMPANY_SORTS = {
	"-n_product_releases": [("n_product_releases", DESCENDING), ("name_norm", ASCENDING)],
	"name": [("name_norm", ASCENDING)],
	"-last_product_release": [("last_product_release", DESCENDING), ("name_norm", ASCENDING)],
	"founded_year": [("founded_year", ASCENDING), ("name_norm", ASCENDING)],
}

# `is_public` is three-valued and `unknown` is a real answer, so the three
# states have to partition the set exactly: null and missing both mean unknown.
PUBLIC_STATES = {"true": True, "false": False, "unknown": None}


class BadQuery(ValueError):
	"""An invalid query parameter. The API turns these into 400s; anything else
	raised out of here is a bug and should surface as a 500."""


def _one(params, name, allowed):
	value = params.get(name)
	if value in (None, ""):
		return None
	if value not in allowed:
		raise BadQuery(f"{name} must be one of {', '.join(sorted(allowed))}")
	return value


def _number(params, name, cast, low=None, high=None):
	value = params.get(name)
	if value in (None, ""):
		return None
	try:
		value = cast(value)
	except ValueError:
		raise BadQuery(f"{name} must be a number")
	if low is not None and value < low:
		raise BadQuery(f"{name} must be at least {low}" + (f" and at most {high}" if high else ""))
	if high is not None and value > high:
		raise BadQuery(f"{name} must be at most {high}")
	return value


def _instant(params, name, end_of_day=False):
	"""Parse a date or datetime bound. A bare date as an upper bound means the
	whole of that day, which is what a date picker implies and not what a naive
	`<=` would give."""
	value = params.get(name)
	if value in (None, ""):
		return None
	try:
		parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		raise BadQuery(f"{name} must be an ISO-8601 date or datetime")
	if parsed.tzinfo is not None:
		parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
	if end_of_day and len(value) == 10:
		parsed += timedelta(days=1) - timedelta(microseconds=1)
	return parsed


def page_of(params):
	page = _number(params, "page", int, low=1) or 1
	per_page = _number(params, "per_page", int, low=1, high=PER_PAGE_MAX) or PER_PAGE_DEFAULT
	return page, per_page


def release_filter(params):
	found = {}

	q = (params.get("q") or "").strip()
	if q:
		found["$text"] = {"$search": q}

	published = {}
	since, until = _instant(params, "from"), _instant(params, "to", end_of_day=True)
	if since:
		published["$gte"] = since
	if until:
		published["$lte"] = until
	if published:
		found["published_at"] = published

	categories = [c for c in params.getlist("category") if c]
	if categories:
		found["category"] = {"$in": categories}

	org_types = [o for o in params.getlist("org_type") if o]
	if org_types:
		found["company_org_type"] = {"$in": org_types}

	company = params.get("company")
	if company:
		found["company_id"] = company

	public = _one(params, "is_public", PUBLIC_STATES)
	if public:
		found["company_is_public"] = PUBLIC_STATES[public]

	ai = _one(params, "ai", ("true", "false"))
	if ai:
		found["is_ai"] = ai == "true"

	minimum = _number(params, "min_confidence", float, low=0, high=1)
	if minimum:
		found["confidence"] = {"$gte": minimum}

	return found


def company_filter(params):
	found = {}

	# Not a filter so much as a permalink: one company's card at a stable URL,
	# which is what a release card and the overview tables link to.
	company = params.get("company")
	if company:
		found["_id"] = company

	q = (params.get("q") or "").strip()
	if q:
		# `fe_companies` holds one document per sender with a product release —
		# thousands, not the hundred thousand in `companies` — so an unanchored
		# scan for a substring beats making the researcher guess a prefix.
		found["name"] = {"$regex": re.escape(q), "$options": "i"}

	org_types = [o for o in params.getlist("org_type") if o]
	if org_types:
		found["org_type"] = {"$in": org_types}

	countries = [c for c in params.getlist("country") if c]
	if countries:
		found["country"] = {"$in": countries}

	public = _one(params, "is_public", PUBLIC_STATES)
	if public:
		found["is_public"] = PUBLIC_STATES[public]

	ticker = _one(params, "has_ticker", ("true", "false"))
	if ticker:
		found["ticker"] = {"$ne": None} if ticker == "true" else None

	releases = {}
	low = _number(params, "min_releases", int, low=0)
	high = _number(params, "max_releases", int, low=0)
	if low:
		releases["$gte"] = low
	if high:
		releases["$lte"] = high
	if releases:
		found["n_product_releases"] = releases

	founded = {}
	since = _number(params, "founded_from", int, low=0, high=2200)
	until = _number(params, "founded_to", int, low=0, high=2200)
	if since:
		founded["$gte"] = since
	if until:
		founded["$lte"] = until
	if founded:
		found["founded_year"] = founded

	return found


def sort_of(params, sorts):
	key = params.get("sort") or next(iter(sorts))
	if key not in sorts:
		raise BadQuery(f"sort must be one of {', '.join(sorts)}")
	return sorts[key]


def page_result(collection, found, sort, page, per_page, projection=None):
	cursor = collection.find(found, projection).sort(sort).skip((page - 1) * per_page).limit(per_page)
	return {
		"items": [serializable(doc) for doc in cursor],
		"total": collection.count_documents(found),
		"page": page,
		"per_page": per_page,
	}


def serializable(value):
	"""Mongo's datetimes are naive UTC; everything leaves here as ISO-8601 with
	an explicit Z, so the browser cannot read a timestamp as local time."""
	if isinstance(value, datetime):
		return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
	if isinstance(value, dict):
		return {k: serializable(v) for k, v in value.items()}
	if isinstance(value, list):
		return [serializable(v) for v in value]
	return value


def meta(db):
	return db["fe_meta"].find_one({"_id": "meta"}) or {}


def overview(db, served):
	"""Everything the Overview tab renders, in one round of aggregations.

	The time series is returned by month whatever granularity is asked for: 12
	years of releases is ~150 rows, small enough that rolling months up into
	quarters or years belongs in the browser rather than in a second request.
	"""
	releases = db["fe_releases"]
	total = releases.count_documents({})

	def counts(field, limit=None):
		stages = [
			{"$group": {"_id": f"${field}", "n": {"$sum": 1}}},
			{"$sort": {"n": DESCENDING, "_id": ASCENDING}},
		]
		if limit:
			stages.append({"$limit": limit})
		return [{"key": g["_id"], "n": g["n"]} for g in releases.aggregate(stages)]

	by_category = counts("category", limit=10)
	series = [
		{"month": g["_id"], "n": g["n"], "n_ai": g["n_ai"]}
		for g in releases.aggregate([
			{"$group": {
				"_id": {"$dateToString": {"date": "$published_at", "format": "%Y-%m"}},
				"n": {"$sum": 1},
				"n_ai": {"$sum": {"$cond": [{"$eq": ["$is_ai", True]}, 1, 0]}},
			}},
			{"$sort": {"_id": ASCENDING}},
		])
	]

	return {
		"n_releases": total,
		"n_companies": db["fe_companies"].count_documents({}),
		"n_ai_releases": releases.count_documents({"is_ai": True}) if served.get("ai") else None,
		"published_from": serializable(served.get("facets", {}).get("published_from")),
		"published_to": serializable(served.get("facets", {}).get("published_to")),
		"series": series,
		"by_category": by_category,
		# The bar chart is "top 10 plus other", and the remainder is only
		# meaningful against the whole — including releases with no category.
		"other_categories": total - sum(c["n"] for c in by_category),
		"by_org_type": counts("company_org_type"),
		"latest": [serializable(d) for d in releases.find(
			{}, {"dedupe_key": 0}).sort([("published_at", DESCENDING)]).limit(10)],
		"top_companies": [serializable(d) for d in db["fe_companies"].find(
			{},
			{"name": 1, "org_type": 1, "n_product_releases": 1, "n_ai_releases": 1,
			 "last_product_release": 1},
		).sort([("n_product_releases", DESCENDING), ("name_norm", ASCENDING)]).limit(10)],
	}


# --- CSV --------------------------------------------------------------------
# One row per release / per company, whatever the page size on screen. Both
# exports carry the served model versions so a downloaded slice can be cited
# and reproduced.

RELEASE_CSV_COLUMNS = [
	"release_id", "title", "url", "published_at", "category", "dateline_city",
	"dateline_region", "launch_confidence", "is_ai", "ai_confidence",
	"launch_model_version", "ai_model_version", "company_id", "company_name",
	"company_domain", "company_org_type", "company_is_public", "company_country",
	"company_region", "company_hq_city", "company_ticker_exchange",
	"company_ticker_symbol", "summary",
]

COMPANY_CSV_COLUMNS = [
	"company_id", "name", "prn_url", "domain", "org_type", "org_type_confidence",
	"is_public", "is_public_confidence", "is_public_method", "ticker_exchange",
	"ticker_symbol", "country", "region", "hq_city", "founded_year", "employees",
	"revenue", "revenue_unit", "revenue_year", "sic", "sic_desc", "legal_form",
	"wikidata_id", "cik", "lei", "ror_id", "ein", "parent_name", "n_press_releases",
	"n_product_releases", "n_ai_releases", "first_product_release",
	"last_product_release", "top_categories", "about",
]


def _cell(value):
	"""Empty is empty, not the string `None`. Booleans go out as true/false so a
	spreadsheet and a dataframe read them the same way."""
	if value is None:
		return ""
	if isinstance(value, bool):
		return "true" if value else "false"
	if isinstance(value, datetime):
		return serializable(value)
	return value


def release_csv_row(doc, versions):
	ticker = doc.get("company_ticker") or {}
	return [_cell(v) for v in (
		doc["_id"], doc.get("title"), doc.get("url"), doc.get("published_at"),
		doc.get("category"), (doc.get("dateline") or {}).get("city"),
		(doc.get("dateline") or {}).get("region"), doc.get("confidence"),
		doc.get("is_ai"), doc.get("ai_confidence"),
		versions.get("launch"), versions.get("ai"),
		doc.get("company_id"), doc.get("company_name"), doc.get("company_domain"),
		doc.get("company_org_type"), doc.get("company_is_public"),
		doc.get("company_country"), doc.get("company_region"), doc.get("company_hq_city"),
		ticker.get("exchange"), ticker.get("symbol"), doc.get("summary"),
	)]


def company_csv_row(doc, prn_base):
	ticker = doc.get("ticker") or {}
	categories = sorted((doc.get("product_categories") or {}).items(),
		key=lambda kv: (-kv[1], kv[0]))
	return [_cell(v) for v in (
		doc["_id"], doc.get("name"), prn_base + doc["_id"], doc.get("domain"),
		doc.get("org_type"), doc.get("org_type_confidence"), doc.get("is_public"),
		doc.get("is_public_confidence"), doc.get("is_public_method"),
		ticker.get("exchange"), ticker.get("symbol"), doc.get("country"),
		doc.get("region"), doc.get("hq_city") or (doc.get("hq") or {}).get("city"),
		doc.get("founded_year"), doc.get("employees"), doc.get("revenue"),
		doc.get("revenue_unit"), doc.get("revenue_year"), doc.get("sic"),
		doc.get("sic_desc"), doc.get("legal_form"), doc.get("wikidata_id"),
		doc.get("cik"), doc.get("lei"), doc.get("ror_id"), doc.get("ein"),
		doc.get("parent_name"), doc.get("n_releases"), doc.get("n_product_releases"),
		doc.get("n_ai_releases"), doc.get("first_product_release"),
		doc.get("last_product_release"),
		";".join(f"{slug}:{n}" for slug, n in categories),
		(doc.get("about") or {}).get("text"),
	)]
