"""Migrate scraped press-release data from the on-disk layout used by
../ai-products/data into the Mongo schema used by this project.

Run locally, not in Docker. Example:

    python py/scrape/migrate.py \
        --articles-dir ../ai-products/data/articles \
        --links-dir ../ai-products/data/links
"""
import argparse
import ast
import json
import os
import sys
from datetime import datetime, timezone

from pymongo import UpdateOne

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_db, ensure_indexes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrape_prn import parse_published_at, ET


def migrate_articles(articles_dir, batch_size=500):
	"""Walk articles_dir/<category>/<slug>.json and upsert into the articles collection.

	Existing docs get their content fields overwritten by disk contents; this is
	intentional — migration treats disk as authoritative. Use $set so a previously
	pending doc (from a listing scrape) is promoted to status=scraped.
	"""
	articles = get_db()["articles"]
	seen = imported = parse_errors = empty_body = unparsed_dates = repr_recovered = 0
	ops = []

	for category in sorted(os.listdir(articles_dir)):
		cat_dir = os.path.join(articles_dir, category)
		if not os.path.isdir(cat_dir):
			continue
		for name in os.listdir(cat_dir):
			if not name.endswith(".json"):
				continue
			seen += 1
			path = os.path.join(cat_dir, name)
			try:
				with open(path, "r", encoding="utf-8") as f:
					data = json.load(f)
			except Exception as e:
				# Legacy: a handful of files were written as Python repr() rather
				# than JSON (single quotes, escaped single quotes). ast.literal_eval
				# handles those safely without evaluating code.
				try:
					with open(path, "r", encoding="utf-8") as f:
						data = ast.literal_eval(f.read())
					if not isinstance(data, dict):
						raise ValueError(f"literal_eval returned {type(data).__name__}, not dict")
					repr_recovered += 1
				except Exception as e2:
					parse_errors += 1
					print(f"[parse error] {path}: {e}; fallback: {e2}")
					continue

			body = data.get("body")
			if not body:
				empty_body += 1
				print(f"[skip: no body] {path}")
				continue

			slug = name[:-5]  # strip ".json"
			scraped_at = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
			doc = {
				"url": f"/news-releases/{slug}.html",
				"category": category,
				"status": "scraped",
				"body": body,
				"scraped_at": scraped_at,
			}
			if "header" in data:
				doc["header"] = data["header"]
			if "uploader" in data:
				doc["uploader"] = data["uploader"]
			if "uploader_link" in data:
				doc["uploader_link"] = data["uploader_link"]
			if "date" in data:
				raw_date = data["date"]
				doc["published_date"] = raw_date
				parsed = parse_published_at(raw_date)
				if parsed is not None:
					doc["published_at"] = parsed
					doc["listing_date"] = parsed.astimezone(ET).strftime("%Y-%m-%d")
				elif raw_date:
					unparsed_dates += 1

			ops.append(UpdateOne(
				{"_id": slug},
				{
					"$set": doc,
					"$setOnInsert": {"discovered_at": scraped_at, "attempts": 0},
				},
				upsert=True,
			))
			imported += 1

			if len(ops) >= batch_size:
				articles.bulk_write(ops, ordered=False)
				ops = []
				print(f"Articles: {imported}/{seen} upserted so far")

	if ops:
		articles.bulk_write(ops, ordered=False)

	print(f"Articles done. seen={seen} imported={imported} repr_recovered={repr_recovered} "
		f"parse_errors={parse_errors} empty_body={empty_body} unparsed_dates={unparsed_dates}")


def migrate_links(links_dir, batch_size=1000):
	"""Walk links_dir/<category>/*.txt and upsert pending article stubs.

	Uses $setOnInsert only — never demotes a scraped doc back to pending, and
	never overwrites content fields on docs already migrated from articles.
	"""
	articles = get_db()["articles"]
	now = datetime.now(timezone.utc)
	seen = imported = malformed = 0
	ops = []

	for category in sorted(os.listdir(links_dir)):
		cat_dir = os.path.join(links_dir, category)
		if not os.path.isdir(cat_dir):
			continue
		for name in os.listdir(cat_dir):
			if not name.endswith(".txt"):
				continue
			path = os.path.join(cat_dir, name)
			try:
				with open(path, "r", encoding="utf-8") as f:
					lines = f.readlines()
			except Exception as e:
				print(f"[read error] {path}: {e}")
				continue

			for raw in lines:
				line = raw.strip()
				if not line:
					continue
				seen += 1
				slug_part = line.split("/")[-1]
				slug = slug_part.split(".")[0]
				if not slug:
					malformed += 1
					print(f"[malformed] {path}: {raw!r}")
					continue

				ops.append(UpdateOne(
					{"_id": slug},
					{"$setOnInsert": {
						"url": line,
						"category": category,
						"discovered_at": now,
						"status": "pending",
						"attempts": 0,
					}},
					upsert=True,
				))
				imported += 1

				if len(ops) >= batch_size:
					articles.bulk_write(ops, ordered=False)
					ops = []

	if ops:
		articles.bulk_write(ops, ordered=False)

	print(f"Links done. seen={seen} stub_upserts={imported} malformed={malformed}")


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Migrate on-disk PRN data into Mongo.")
	parser.add_argument("--articles-dir", default="../ai-products/data/articles")
	parser.add_argument("--links-dir", default="../ai-products/data/links")
	parser.add_argument("--no-articles", action="store_true")
	parser.add_argument("--no-links", action="store_true")
	args = parser.parse_args()

	ensure_indexes()

	if not args.no_articles:
		migrate_articles(args.articles_dir)

	if not args.no_links:
		migrate_links(args.links_dir)

	print("Migration complete.")
