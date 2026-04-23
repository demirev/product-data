import os

from pymongo import MongoClient, ASCENDING

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
	db["predictions"].create_index(
		[("article_id", ASCENDING), ("model_version", ASCENDING)],
		unique=True,
	)
