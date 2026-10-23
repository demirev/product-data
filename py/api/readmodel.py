"""The read model behind the explorer: one document per product release, and one
per company that has any.

A product release is an article the served `launch` run called class 1. That set
is not stored anywhere — it is a three-way join of `predictions`, `articles` and
`companies` — and at 10^5 releases resolving it per request would be 10^5
lookups per page view. So this app keeps its own denormalized copy in
`fe_releases`, `fe_companies` and `fe_meta` and refreshes it in the background.

Everything here is a cache. It is derived entirely from the pipeline
collections, never written back to them, and safe to drop at any moment: the
next refresh rebuilds it. The `fe_` prefix is there so ownership is unambiguous
when you are looking at the database.

The build runs server-side, one aggregation per collection. Normalization (the
whitespace collapse, the `Continue Reading` cut, word-boundary truncation) is
expressed in aggregation operators rather than done in Python for the same
reason the join is: streaming 10^5 release bodies out to tidy them up and back
again is the expensive half of a rebuild.
"""
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from pymongo import ASCENDING, DESCENDING, TEXT

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_db


PRN_BASE = "https://www.prnewswire.com"

# Summary length, and how far into a body we bother looking for the end of the
# lede. Past a few thousand characters a release is boilerplate — "About X",
# forward-looking statements, contact blocks — which truncation drops anyway.
SUMMARY_CHARS = 400
BODY_SCAN_CHARS = 3000

# A refresh reads predictions written since the last one. It stamps the moment
# the build *started*, less this margin, so a prediction written while the
# aggregation was already past its article is picked up next time rather than
# skipped. Re-reading a release costs nothing: the merge replaces it.
CURSOR_OVERLAP = timedelta(minutes=1)

TASKS = ("launch", "ai")

_lock = threading.Lock()


def _normalized(expr, limit=None):
	"""`re.sub(r"\\s+", " ", value).strip()` as an aggregation expression.

	Scraped headers end in runs of tabs and newlines and bodies are full of
	them, and `$replaceAll` matches literals only — so the text is tokenised on
	non-space runs and the tokens re-joined. With `limit` the join stops at the
	last whole word that fits, which is the word-boundary truncation the summary
	needs.
	"""
	tokens = {"$map": {
		"input": {"$regexFindAll": {"input": {"$ifNull": [expr, ""]}, "regex": r"\S+"}},
		"in": "$$this.match",
	}}
	joined = {"$concat": [
		"$$value.text",
		{"$cond": [{"$eq": ["$$value.text", ""]}, "", " "]},
		"$$this",
	]}
	if limit is None:
		step = {"text": joined, "full": False}
	else:
		# `full` latches: once a word does not fit, shorter later words must not
		# slip in behind it, or the summary reads as if it resumed mid-sentence.
		step = {"$cond": [
			{"$or": ["$$value.full", {"$gt": [{"$strLenCP": joined}, limit]}]},
			{"text": "$$value.text", "full": True},
			{"text": joined, "full": False},
		]}
	return {"$getField": {"field": "text", "input": {"$reduce": {
		"input": tokens,
		"initialValue": {"text": "", "full": False},
		"in": step,
	}}}}


# PRN inlines image captions into the body behind a "Continue Reading" marker,
# so cut there — but only past the first 200 characters. A release whose marker
# comes up front has no lede to speak of, and its raw prefix is the better
# summary of the two.
_LEDE = {"$let": {
	"vars": {
		"body": {"$ifNull": ["$article.body", ""]},
		"cut": {"$indexOfCP": [{"$ifNull": ["$article.body", ""]}, "Continue Reading", 200]},
	},
	"in": {"$substrCP": ["$$body", 0, {"$cond": [
		{"$gte": ["$$cut", 0]},
		{"$min": ["$$cut", BODY_SCAN_CHARS]},
		BODY_SCAN_CHARS,
	]}]},
}}


def release_pipeline(launch_version, ai_version, since, article_ids=None):
	"""Predictions for the served launch run → `fe_releases`, joins and all.
	`article_ids` narrows the pass to those articles, whatever their age."""
	match = {"model_version": launch_version, "predicted_class": 1}
	if since is not None:
		match["predicted_at"] = {"$gt": since}
	if article_ids is not None:
		match["article_id"] = {"$in": article_ids}

	stages = [
		{"$match": match},
		{"$lookup": {
			"from": "articles", "localField": "article_id",
			"foreignField": "_id", "as": "article",
		}},
		{"$unwind": "$article"},
		# A prediction for an unfetched stub should not exist, but if one does
		# the read model must not invent a release with no body or date.
		{"$match": {"article.status": "scraped", "article.published_at": {"$type": "date"}}},
		{"$lookup": {
			"from": "companies", "localField": "article.uploader_link",
			"foreignField": "_id", "as": "company",
		}},
		{"$unwind": {"path": "$company", "preserveNullAndEmptyArrays": True}},
	]

	if ai_version:
		# The `ai` head runs only over launch positives, so most of these miss
		# until it has caught up — and a miss is `is_ai: null`, not `false`.
		stages += [
			{"$lookup": {
				"from": "predictions",
				"let": {"aid": "$article_id"},
				"pipeline": [
					{"$match": {"$expr": {"$and": [
						{"$eq": ["$model_version", ai_version]},
						{"$eq": ["$article_id", "$$aid"]},
					]}}},
					{"$project": {"predicted_class": 1, "class_probabilities": 1}},
				],
				"as": "ai",
			}},
			{"$unwind": {"path": "$ai", "preserveNullAndEmptyArrays": True}},
		]
		is_ai = {"$cond": [
			{"$eq": [{"$ifNull": ["$ai.predicted_class", None]}, None]},
			None,
			{"$eq": ["$ai.predicted_class", 1]},
		]}
		ai_confidence = {"$ifNull": [
			{"$arrayElemAt": [{"$ifNull": ["$ai.class_probabilities", []]}, 1]}, None,
		]}
	else:
		is_ai, ai_confidence = {"$literal": None}, {"$literal": None}

	return stages + [
		{"$set": {
			"fe_title": _normalized("$article.header"),
			"fe_summary": _normalized(_LEDE, SUMMARY_CHARS),
			"fe_company_name": _normalized({"$ifNull": ["$company.name", "$article.uploader"]}),
		}},
		{"$project": {
			"_id": "$article_id",
			"title": "$fe_title",
			"summary": "$fe_summary",
			"url": {"$concat": [PRN_BASE, {"$ifNull": ["$article.url", ""]}]},
			"published_at": "$article.published_at",
			"category": {"$ifNull": ["$article.category", None]},
			"dateline": {
				"city": {"$ifNull": ["$article.dateline_city", None]},
				"region": {"$ifNull": ["$article.dateline_region", None]},
			},
			# $setUnion for its dedupe: a release that names its ticker three
			# times yields three identical extractions.
			"tickers": {"$setUnion": [{"$map": {
				"input": {"$ifNull": ["$article.tickers", []]},
				"in": {"exchange": "$$this.exchange", "symbol": "$$this.symbol"},
			}}, []]},
			"confidence": {"$arrayElemAt": ["$class_probabilities", 1]},
			"is_ai": is_ai,
			"ai_confidence": ai_confidence,
			"company_id": {"$ifNull": ["$article.uploader_link", None]},
			"company_name": "$fe_company_name",
			"company_org_type": {"$ifNull": ["$company.org_type", None]},
			"company_is_public": {"$ifNull": ["$company.is_public", None]},
			"company_country": {"$ifNull": ["$company.country", None]},
			"company_domain": {"$ifNull": ["$company.domain", None]},
			# Neither filtered nor sorted on: carried so the CSV export stays a
			# single streamed pass over `fe_releases` with no second join.
			"company_region": {"$ifNull": ["$company.region", None]},
			"company_hq_city": {"$ifNull": ["$company.hq_city", "$company.hq.city"]},
			"company_ticker": {"$ifNull": ["$company.ticker", None]},
			"dedupe_key": {"$concat": [
				{"$toLower": "$fe_title"}, "\u0000",
				{"$ifNull": ["$article.uploader_link", ""]},
			]},
		}},
		{"$merge": {
			"into": "fe_releases", "on": "_id",
			"whenMatched": "replace", "whenNotMatched": "insert",
		}},
	]


# `fe_releases` → `fe_companies`, rebuilt whole every refresh: a group and a
# lookup over a collection two orders of magnitude smaller than `articles`.
COMPANY_PIPELINE = [
	{"$match": {"company_id": {"$ne": None}}},
	{"$group": {
		"_id": {"company": "$company_id", "category": {"$ifNull": ["$category", "uncategorized"]}},
		"name": {"$first": "$company_name"},
		"n": {"$sum": 1},
		"first": {"$min": "$published_at"},
		"last": {"$max": "$published_at"},
		"n_ai": {"$sum": {"$cond": [{"$eq": ["$is_ai", True]}, 1, 0]}},
	}},
	{"$group": {
		"_id": "$_id.company",
		"name": {"$first": "$name"},
		"n_product_releases": {"$sum": "$n"},
		"first_product_release": {"$min": "$first"},
		"last_product_release": {"$max": "$last"},
		"n_ai_releases": {"$sum": "$n_ai"},
		"product_categories": {"$push": {"k": "$_id.category", "v": "$n"}},
	}},
	{"$lookup": {"from": "companies", "localField": "_id", "foreignField": "_id", "as": "company"}},
	{"$unwind": {"path": "$company", "preserveNullAndEmptyArrays": True}},
	# The whole company document with the product-release rollup laid over it.
	{"$replaceWith": {"$mergeObjects": [{"$ifNull": ["$company", {}]}, {
		"_id": "$_id",
		"name": "$name",
		"name_norm": {"$ifNull": ["$company.name_norm", {"$toLower": "$name"}]},
		"n_product_releases": "$n_product_releases",
		"first_product_release": "$first_product_release",
		"last_product_release": "$last_product_release",
		"n_ai_releases": "$n_ai_releases",
		"product_categories": {"$arrayToObject": "$product_categories"},
	}]}},
	# Per-release evidence that the rollup already summarised into the fields
	# above it; nothing in the app reads it and it is most of the document.
	{"$unset": ["dateline_places", "domains", "aliases", "source_orgs"]},
	{"$out": "fe_companies"},
]


def ensure_indexes(db):
	"""Indexes for the read model only. `db.ensure_indexes` covers the pipeline
	collections and stays that way — these are this app's to create and drop
	along with the collections they sit on."""
	releases = db["fe_releases"]
	releases.create_index([("published_at", DESCENDING)])
	releases.create_index([("category", ASCENDING)])
	releases.create_index([("company_id", ASCENDING)])
	releases.create_index([("confidence", DESCENDING)])
	releases.create_index([("is_ai", ASCENDING)])
	releases.create_index([("company_org_type", ASCENDING)])
	releases.create_index([("dedupe_key", ASCENDING)])
	releases.create_index([("title", TEXT), ("summary", TEXT)], name="text")

	companies = db["fe_companies"]
	companies.create_index([("n_product_releases", DESCENDING)])
	companies.create_index([("org_type", ASCENDING)])
	companies.create_index([("is_public", ASCENDING)])
	companies.create_index([("country", ASCENDING)])
	companies.create_index([("name_norm", ASCENDING)])


def _dedupe(db):
	"""Collapse releases republished across PRN categories; return the count.

	Some senders file the same release under several categories, and PRN gives
	each copy its own slug — so the copies are distinct articles carrying
	distinct predictions, and the only thing tying them together is the sender
	and the headline. The earliest copy is the one that survives.

	This sweeps the whole collection rather than only what the merge just wrote,
	because an incremental pass cannot see the copy that arrived last week.
	"""
	stale = []
	for group in db["fe_releases"].aggregate([
		{"$sort": {"published_at": ASCENDING, "_id": ASCENDING}},
		{"$group": {"_id": "$dedupe_key", "keep": {"$first": "$_id"}, "ids": {"$push": "$_id"}}},
		{"$match": {"$expr": {"$gt": [{"$size": "$ids"}, 1]}}},
	], allowDiskUse=True):
		stale += [i for i in group["ids"] if i != group["keep"]]

	if stale:
		db["fe_releases"].delete_many({"_id": {"$in": stale}})
	return len(stale)


def _meta(db, runs, built_at):
	"""The served runs, the build stamp, and the facet values the filter panels
	need — computed here, once, rather than on every request."""
	bounds = list(db["fe_releases"].aggregate([{"$group": {
		"_id": None,
		"from": {"$min": "$published_at"},
		"to": {"$max": "$published_at"},
	}}]))

	return {
		"_id": "meta",
		"built_at": built_at,
		"launch": runs["launch"],
		"ai": runs["ai"],
		"n_releases": db["fe_releases"].count_documents({}),
		"n_companies": db["fe_companies"].count_documents({}),
		"facets": {
			"categories": sorted(c for c in db["fe_releases"].distinct("category") if c),
			"org_types": sorted(o for o in db["fe_companies"].distinct("org_type") if o),
			"countries": sorted(c for c in db["fe_companies"].distinct("country") if c),
			# Three-valued, and `null` is a real answer: a sender whose releases
			# carry no concentrated ticker and which no registry matched. Counted
			# rather than `distinct`, because a company document that never got
			# an `is_public` key at all is also unknown and distinct would miss it.
			"is_public": [name for value, name in
				((True, "true"), (False, "false"), (None, "unknown"))
				if db["fe_companies"].count_documents({"is_public": value}, limit=1)],
			"published_from": bounds[0]["from"] if bounds else None,
			"published_to": bounds[0]["to"] if bounds else None,
		},
	}


def refresh(force=False, wait=False):
	"""Bring the read model up to date. Safe to call concurrently — a caller
	that arrives while a build is running is told so, or with `wait` queued.

	Full rebuild when the served version of either head has moved (every stored
	`is_ai` is keyed to the `ai` run that produced it, and every release to the
	`launch` run that selected it), incremental otherwise.
	"""
	if not _lock.acquire(blocking=wait):
		return {"status": "busy"}

	started = time.monotonic()
	try:
		db = get_db()
		runs = {
			task: db["model_runs"].find_one({"task": task}, sort=[("trained_at", DESCENDING)])
			for task in TASKS
		}
		launch, ai = runs["launch"], runs["ai"]
		if launch is None:
			# Nothing has been trained yet: there is no product-release set to
			# serve, and the app must still come up and say so.
			return {"status": "no_model", "launch_version": None, "ai_version": None}

		previous = db["fe_meta"].find_one({"_id": "meta"}) or {}
		served = (launch["_id"], ai["_id"] if ai else None)
		known = (
			(previous.get("launch") or {}).get("_id"),
			(previous.get("ai") or {}).get("_id"),
		)
		full = force or served != known or previous.get("built_at") is None

		built_at = datetime.now(timezone.utc) - CURSOR_OVERLAP
		blocked = sorted(db["sender_blacklist"].distinct("_id"))
		if full:
			# The old version's positives are not this version's; start clean
			# rather than merge one run's answers over another's.
			db["fe_releases"].drop()
		ensure_indexes(db)

		ai_version = ai["_id"] if ai else None
		db["predictions"].aggregate(
			release_pipeline(launch["_id"], ai_version, None if full else previous["built_at"]),
			allowDiskUse=True,
		)
		# Senders taken off the blacklist since the last build: their releases
		# are older than the incremental cursor, so they are re-read by article.
		restored = set(previous.get("blacklist") or []) - set(blocked)
		if restored and not full:
			db["predictions"].aggregate(release_pipeline(
				launch["_id"], ai_version, None,
				db["articles"].distinct("_id", {"uploader_link": {"$in": list(restored)}}),
			), allowDiskUse=True)
		# Re-read rather than `blocked`, so a sender blacklisted mid-build is out too.
		db["fe_releases"].delete_many(
			{"company_id": {"$in": db["sender_blacklist"].distinct("_id")}})
		deduped = _dedupe(db)
		if db["fe_releases"].count_documents({}, limit=1):
			db["fe_releases"].aggregate(COMPANY_PIPELINE, allowDiskUse=True)
			# $out replaces the collection; its indexes come back with it, but a
			# first build has none to preserve.
			ensure_indexes(db)
		else:
			db["fe_companies"].delete_many({})
		# The blacklist as read at the start, so a sender removed mid-build is
		# still seen as restored by the next refresh.
		db["fe_meta"].replace_one(
			{"_id": "meta"}, {**_meta(db, runs, built_at), "blacklist": blocked}, upsert=True)

		return {
			"status": "ok",
			"mode": "full" if full else "incremental",
			"releases": db["fe_releases"].count_documents({}),
			"companies": db["fe_companies"].count_documents({}),
			"deduped": deduped,
			"launch_version": launch["_id"],
			"ai_version": ai["_id"] if ai else None,
			"seconds": round(time.monotonic() - started, 1),
		}
	finally:
		_lock.release()


if __name__ == "__main__":
	print(refresh(force="--force" in sys.argv))
