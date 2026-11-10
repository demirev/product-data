"""Monthly release, launch and AI-launch counts per sender -> firm_months_<launch version>__<ai version>.csv.gz.

Releases counted: scraped, dated, sender not blacklisted, English (the rule in
03-tech-waves/keywords.py). A launch is a positive of the newest `launch` run; an AI
launch is a launch that is also a positive of the newest `ai` run. Only senders with at
least one launch from WAVE_START on are kept, with their full history.

Columns: uploader_link, month, releases, launches, ai_launches

    python extract.py
"""
import re
from collections import defaultdict
from pathlib import Path

import pandas as pd
from pymongo import MongoClient

WAVE_START = "2022-12"

db = MongoClient("mongodb://localhost:27017")["prn"]
versions = {t: db["model_runs"].find_one({"task": t}, sort=[("trained_at", -1)])["_id"] for t in ("launch", "ai")}
launch = {p["article_id"] for p in db["predictions"].find({"model_version": versions["launch"], "predicted_class": 1}, {"article_id": 1})}
ai = {p["article_id"] for p in db["predictions"].find({"model_version": versions["ai"], "predicted_class": 1}, {"article_id": 1})} & launch

counts = defaultdict(lambda: [0, 0, 0])
cursor = db["articles"].find(
	{"status": "scraped", "published_at": {"$type": "date"}, "uploader_link": {"$nin": db["sender_blacklist"].distinct("_id")}},
	{"header": 1, "published_at": 1, "uploader_link": 1, "lede": {"$substrCP": [{"$ifNull": ["$body", ""]}, 0, 1001]}},
	batch_size=5000)
for i, d in enumerate(cursor):
	lede = d["lede"]
	lede = re.sub(r"\S*$", "", lede) if len(lede) > 1000 else lede
	text = re.sub(r"\s+", " ", (d.get("header") or "") + " " + lede).strip()
	if len(re.findall(r" (the|and|of|to) ", text.lower())) >= 4:
		c = counts[d.get("uploader_link"), d["published_at"].strftime("%Y-%m")]
		c[0] += 1
		c[1] += d["_id"] in launch
		c[2] += d["_id"] in ai
	if i % 200000 == 0:
		print(i, flush=True)

fm = pd.DataFrame([(*k, *v) for k, v in counts.items()], columns=["uploader_link", "month", "releases", "launches", "ai_launches"])
wave = fm[(fm.month >= WAVE_START) & (fm.launches > 0)].uploader_link.unique()
fm = fm[fm.uploader_link.isin(wave)].sort_values(["uploader_link", "month"])
fm.to_csv(Path(__file__).parent / f"firm_months_{versions['launch']}__{versions['ai']}.csv.gz", index=False)
print(len(wave), "senders,", len(fm), "sender-months")
