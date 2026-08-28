import os
from datetime import datetime, timezone

from pymongo import MongoClient, ASCENDING, UpdateOne

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "prn")

_client = None


def get_db():
	global _client
	if _client is None:
		_client = MongoClient(MONGO_URI)
	return _client[MONGO_DB]


def ensure_indexes():
	db = get_db()
	db["articles"].create_index([("status", ASCENDING), ("category", ASCENDING)])
	db["articles"].create_index([("listing_date", ASCENDING)])
	# Back the labeler's sampling queries; see py/vocab.py for why these fields
	# are precomputed rather than matched with a regex at query time.
	db["articles"].create_index([("status", ASCENDING), ("ai_vocab", ASCENDING)])
	db["articles"].create_index([("status", ASCENDING), ("launch_vocab", ASCENDING)])
	# Backs both the prediction upsert and the $lookup joins that drive the
	# classifier's resume and gating; see classify_press_releases.
	db["predictions"].create_index(
		[("article_id", ASCENDING), ("model_version", ASCENDING)],
		unique=True,
	)
	# `uploader_link` is the company key; the rollup and every per-company query
	# read on it, and it also backs the facts backfill's resume filter.
	db["articles"].create_index([("uploader_link", ASCENDING)])
	db["articles"].create_index([("status", ASCENDING), ("facts_version", ASCENDING)])
	db["companies"].create_index([("is_public", ASCENDING)])
	db["companies"].create_index([("org_type", ASCENDING)])
	db["companies"].create_index([("n_releases", ASCENDING)])
	db["companies"].create_index([("domain", ASCENDING)])
	# One document per training run, _id = the version its predictions carry.
	db["model_runs"].create_index([("task", ASCENDING), ("trained_at", ASCENDING)])

	# External enrichment; see py/enrich.py. `company_enrichment` shares its _id
	# with `companies` (the uploader_link), so the join needs no index of its own
	# — these back `--stats` and the crosswalk lookups instead.
	db["company_enrichment"].create_index([("wikidata.qid", ASCENDING)], sparse=True)
	db["company_enrichment"].create_index([("sec.cik", ASCENDING)], sparse=True)
	db["company_enrichment"].create_index([("gleif.lei", ASCENDING)], sparse=True)
	db["company_enrichment"].create_index([("enrichment_version", ASCENDING)])
	# Staging for the bulk sources; every one of these backs a step in the
	# matching cascade. The name indexes are compound with a region because that
	# is the narrower block the resolver tries first — and they serve the
	# name-only fallback too, since a query on the leading field uses the
	# compound index as a prefix.
	db["sec_tickers"].create_index([("ticker", ASCENDING)])
	db["sec_tickers"].create_index([("name_norm", ASCENDING)])
	# One row per symbol, so the CIK is a lookup key rather than the _id.
	db["sec_tickers"].create_index([("cik", ASCENDING)])
	db["gleif_lei"].create_index([("name_norm", ASCENDING), ("country", ASCENDING)])
	db["gleif_rr"].create_index([("start_lei", ASCENDING), ("type", ASCENDING)])
	db["irs_bmf"].create_index([("name_norm", ASCENDING), ("state", ASCENDING)])
	db["ror"].create_index([("wikidata_id", ASCENDING)], sparse=True)
	db["ror"].create_index([("domains", ASCENDING)])
	db["ror"].create_index([("name_norms", ASCENDING)])


def upsert_link_stubs(slugs, source, batch_size=1000):
	"""Insert pending article stubs for `slugs`; return (inserted, already_present).

	`$setOnInsert` only, so a slug that has already been scraped keeps its body
	and status: back-fills are idempotent, safe to re-run, and can never demote a
	scraped doc back to pending.

	`source` records where the link was discovered ("sitemap", "wayback"). These
	stubs deliberately carry no `category` or `listing_date` — neither archive
	exposes one, and the authoritative publication date arrives with the body
	when `scrape_prn.py --no-links` fills the stub in.
	"""
	articles = get_db()["articles"]
	now = datetime.now(timezone.utc)
	inserted = 0

	for i in range(0, len(slugs), batch_size):
		chunk = slugs[i:i + batch_size]
		result = articles.bulk_write([
			UpdateOne(
				{"_id": slug},
				{"$setOnInsert": {
					"url": f"/news-releases/{slug}.html",
					"discovered_at": now,
					"source": source,
					"status": "pending",
					"attempts": 0,
				}},
				upsert=True,
			)
			for slug in chunk
		], ordered=False)
		inserted += result.upserted_count

	return inserted, len(slugs) - inserted
