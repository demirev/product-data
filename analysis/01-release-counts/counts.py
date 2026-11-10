"""Monthly counts of scraped releases, launch-positive releases and AI products -> monthly.csv.

Months are by `published_at`. Launch and AI flags come from the latest `launch` and
`ai` runs in `model_runs`, the versions the explorer serves. An AI product is a
release positive under both. Releases from blacklisted senders are left out of all
series.
"""
import csv
from pathlib import Path

from pymongo import MongoClient

db = MongoClient("mongodb://localhost:27017")["prn"]
blocked = db["sender_blacklist"].distinct("_id")
version, ai_version = (db["model_runs"].find_one({"task": t}, sort=[("trained_at", -1)])["_id"] for t in ("launch", "ai"))
month = {"$dateToString": {"format": "%Y-%m", "date": "$published_at"}}

total = {r["_id"]: r["n"] for r in db["articles"].aggregate([
	{"$match": {"status": "scraped", "published_at": {"$type": "date"}, "uploader_link": {"$nin": blocked}}},
	{"$group": {"_id": month, "n": {"$sum": 1}}},
])}

launch = {r["_id"]: r for r in db["predictions"].aggregate([
	{"$match": {"model_version": version, "predicted_class": 1}},
	{"$lookup": {"from": "articles", "localField": "article_id", "foreignField": "_id", "as": "a"}},
	{"$unwind": "$a"},
	{"$match": {"a.status": "scraped", "a.published_at": {"$type": "date"}, "a.uploader_link": {"$nin": blocked}}},
	{"$lookup": {
		"from": "predictions", "localField": "article_id", "foreignField": "article_id",
		"pipeline": [{"$match": {"model_version": ai_version, "predicted_class": 1}}, {"$limit": 1}],
		"as": "ai",
	}},
	{"$group": {
		"_id": {"$dateToString": {"format": "%Y-%m", "date": "$a.published_at"}},
		"n": {"$sum": 1}, "ai": {"$sum": {"$size": "$ai"}},
	}},
])}

with open(Path(__file__).parent / "monthly.csv", "w", newline="") as f:
	w = csv.writer(f)
	w.writerow(["month", "total", "launch", "ai", "launch_version", "ai_version"])
	for m in sorted(total):
		r = launch.get(m, {"n": 0, "ai": 0})
		w.writerow([m, total[m], r["n"], r["ai"], version, ai_version])
