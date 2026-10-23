"""Monthly counts of scraped releases and launch-positive releases -> monthly.csv.

Months are by `published_at`. Launch flags come from the latest `launch` run in
`model_runs`, the version the explorer serves. Releases from blacklisted senders
are left out of both series.
"""
import csv
from pathlib import Path

from pymongo import MongoClient

db = MongoClient("mongodb://localhost:27017")["prn"]
blocked = db["sender_blacklist"].distinct("_id")
version = db["model_runs"].find_one({"task": "launch"}, sort=[("trained_at", -1)])["_id"]
month = {"$dateToString": {"format": "%Y-%m", "date": "$published_at"}}

total = {r["_id"]: r["n"] for r in db["articles"].aggregate([
	{"$match": {"status": "scraped", "published_at": {"$type": "date"}, "uploader_link": {"$nin": blocked}}},
	{"$group": {"_id": month, "n": {"$sum": 1}}},
])}

launch = {r["_id"]: r["n"] for r in db["predictions"].aggregate([
	{"$match": {"model_version": version, "predicted_class": 1}},
	{"$lookup": {"from": "articles", "localField": "article_id", "foreignField": "_id", "as": "a"}},
	{"$unwind": "$a"},
	{"$match": {"a.status": "scraped", "a.published_at": {"$type": "date"}, "a.uploader_link": {"$nin": blocked}}},
	{"$group": {"_id": {"$dateToString": {"format": "%Y-%m", "date": "$a.published_at"}}, "n": {"$sum": 1}}},
])}

with open(Path(__file__).parent / "monthly.csv", "w", newline="") as f:
	w = csv.writer(f)
	w.writerow(["month", "total", "launch", "launch_version"])
	for m in sorted(total):
		w.writerow([m, total[m], launch.get(m, 0), version])
